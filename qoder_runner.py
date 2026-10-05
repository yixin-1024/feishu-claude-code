"""本地调用 Qoder CLI（qodercli）的统一入口。

    qodercli -p --output-format stream-json --include-partial-messages
             --permission-mode bypass_permissions
             [--session-id <uuid> | -r <uuid>] [-m <model>]
             [--reasoning-effort <level>] [--append-system-prompt <text>]
             [--mcp-config <json>] [--disallowed-tools <a,b,…>]
             [--config-dir <dir>]
    （prompt 从 stdin 进，避开 argv 长度上限）

为什么它接起来省事：qodercli 的 stream-json 和 `claude --print --output-format
stream-json` 是同一套线格式（system/init → stream_event/content_block_delta →
assistant → result），工具名也和 Claude Code 一致（Bash/Read/Edit/…），所以解析与
grok_runner 同构。`--include-partial-messages` 不在 `--help` 里，但 1.1.65 实测可用，
不加的话正文是一整条一整条到的。

会话：session id **稳定**——首轮自己生成 UUID 用 `--session-id` 钉死，续轮
`-r <同一 id>`。⚠️ qoder 的会话按**工作目录**归档（~/.qoder/projects/<cwd>），换了
cwd 再 `-r` 会 rc=42 + "Error resuming session"，这里识别后退回新会话。

模型：`Auto` / `Ultimate` / `Performance` / `Efficient` 这几个档位，加 Qwen / Kimi /
GLM / DeepSeek / MiniMax 等具体模型（`qodercli --list-models` 看全表）。传了不存在的
模型名不会报错，CLI 会在 stderr 说一句然后退回 Auto。

计费：qoder 不回 token 数（usage 全是 0），只给 `credits` 和
`usage.context_usage_ratio`（上下文占比）。footer 显示占比 + 本轮 credits。

MCP：`--mcp-config` 吃 Claude 同款 JSON，cc-lark 运行时工具注入后名字也一样是
`mcp__cc-lark__*`，所以提示词直接用 Claude 那份。

鉴权：默认沿用本机 `qodercli login` / Qoder 桌面 App 的登录态（~/.qoder）；
服务器上可以配 QODER_PERSONAL_ACCESS_TOKEN（CLI 优先用它）。

Claude Code 的规则 / 记忆 / skill：qoder 不读 CLAUDE.md、看不到 ~/.claude/skills 和
Claude 的记忆目录。每轮把缺的 skill 软链进 ~/.qoder/skills，再把全局 CLAUDE.md、
项目 CLAUDE.md 路径、记忆索引拼进系统提示（见 claude_context.py）。
CC_LARK_QODER_CLAUDE_CONTEXT=0 关掉。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import select
import shutil
import signal
import subprocess
import time
import uuid
from datetime import datetime
from typing import Callable, Optional

from bot_config import PERMISSION_MODE, resolve_claude_wall_clock_limit
from claude_context import build_claude_context_brief, link_claude_skills
from claude_runner import (
    _extract_text_content,
    _fire_callback,
    _has_children,
    cc_lark_mcp_config,
    is_fatal_error_text,
)

IDLE_TIMEOUT = 300  # 无输出且无子进程 → 视为挂死
STUCK_CHILD_TIMEOUT = int(os.getenv("STUCK_CHILD_TIMEOUT_SEC", "3600"))  # 有子进程但 qoder 端持续无输出（tail -f / npm run dev 类）
_CHECK_INTERVAL = 30

# qodercli --reasoning-effort 的合法值（1.1.65 的报错信息里列的）
QODER_EFFORT_LEVELS = ("auto", "none", "low", "medium", "high", "xhigh", "max", "ultracode")

# qodercli 的权限模式是下划线写法；cc-lark 全局 PERMISSION_MODE 是 Claude 的驼峰写法
QODER_PERMISSION_MODES = (
    "default", "accept_edits", "bypass_permissions", "dont_ask", "auto", "plan",
)
_CAMEL_TO_QODER_MODE = {
    "acceptedits": "accept_edits",
    "bypasspermissions": "bypass_permissions",
    "dontask": "dont_ask",
}

# qoder 自带的「自我唤醒 / 定时 / 盯事件」工具：cc-lark 每轮结束就 killpg，没人兑现。
# 跨轮的等待和定时走 cc-lark 的 wake_me_in / schedule_cron。
DEFAULT_DISALLOWED_TOOLS = "Workflow,ScheduleWakeup,Monitor,CronCreate,CronDelete,CronList"

QODER_HOME = os.path.expanduser("~/.qoder")

# `-r` 指向的会话不存在 / 不在本 cwd 下
_RESUME_FAILED_RC = 42
_RESUME_FAILED_MARK = "Error resuming session"


def resolve_qoder_bin(configured: Optional[str] = None) -> str:
    if configured:
        return os.path.expanduser(configured)
    found = shutil.which("qodercli")
    if found:
        return found
    home_bin = os.path.expanduser("~/.local/bin/qodercli")
    if os.path.exists(home_bin):
        return home_bin
    return "qodercli"


def _normalize_permission_mode(mode: Optional[str], dangerous_skip: bool) -> str:
    m = (mode or "").strip()
    if m in QODER_PERMISSION_MODES:
        return m
    mapped = _CAMEL_TO_QODER_MODE.get(m.lower())
    if mapped:
        return mapped
    if dangerous_skip:
        return "bypass_permissions"
    fallback = (PERMISSION_MODE or "").strip()
    if fallback in QODER_PERMISSION_MODES:
        return fallback
    return _CAMEL_TO_QODER_MODE.get(fallback.lower(), "default")


def _normalize_effort(effort: Optional[str]) -> Optional[str]:
    e = (effort or "").strip().lower()
    if not e:
        return None
    if e not in QODER_EFFORT_LEVELS:
        raise ValueError(
            f"invalid Qoder effort {e!r}; expected one of {list(QODER_EFFORT_LEVELS)}"
        )
    return e


def _with_claude_context(
    append_system_prompt: Optional[str], cwd: Optional[str], config_dir: Optional[str],
) -> str:
    """把 Claude Code 的 skill 同步进 qoder，并把规则 / 记忆拼到系统提示后面。"""
    base = append_system_prompt or ""
    if os.getenv("CC_LARK_QODER_CLAUDE_CONTEXT", "1") == "0":
        return base
    skills_root = os.path.join(
        os.path.expanduser(config_dir) if config_dir else QODER_HOME, "skills"
    )
    try:
        linked = link_claude_skills(skills_root)
        if linked:
            print(f"[run_qoder] 同步 Claude skill → {skills_root}: {', '.join(linked)}", flush=True)
        brief = build_claude_context_brief(
            cwd,
            skills_note=(
                f"~/.claude/skills 里的 skill 已经软链进 {skills_root}，lark-* 那批在 "
                "~/.agents/skills，都在你的 skill 列表里，按名字正常用。"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — 接不上 Claude 的上下文不该拖垮这一轮
        print(f"[run_qoder] Claude 上下文跳过: {type(exc).__name__}: {exc}", flush=True)
        return base
    if not brief:
        return base
    return f"{base}\n\n{brief}" if base else brief


# ── 套餐额度（/usage）────────────────────────────────────────────
# credits 余量只在交互界面的 /usage 里有：`-p "/usage"` 会被当成本地命令吃掉但什么都
# 不输出，`qodercli status -o json` 只有账号信息。所以开一个伪终端跑交互式 qodercli，
# 敲 /usage 读屏幕再退出——不调模型、不花 credits，一次 6~8 秒。
QODER_USAGE_CWD = os.path.expanduser("~/.feishu-claude/qoder-usage")
_USAGE_CACHE_TTL = 30
_usage_cache: dict[tuple, tuple[float, dict]] = {}


def _screen_text(raw: bytes) -> str:
    """TUI 输出转纯文本：光标右移换成空格（不然词会粘在一起），其余控制序列去掉。"""
    t = raw.decode("utf-8", "replace")
    t = re.sub(r"\x1b\[(\d*)C", lambda m: " " * int(m.group(1) or 1), t)
    t = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", t)
    t = re.sub(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]", "", t)
    t = re.sub(r"\x1b[()][A-Za-z0-9]", "", t)
    return t.replace("\r", "\n")


def _last(pattern: str, text: str):
    found = re.findall(pattern, text, re.IGNORECASE)
    return found[-1] if found else None


def _num(v: str) -> float:
    return float(v.replace(",", ""))


def parse_qoder_usage_screen(text: str) -> dict:
    """从 /usage 面板的文字里抠套餐额度；TUI 会重绘，同一项取最后一次出现的。"""
    out: dict = {}
    plan = _last(r"Qoder\s*Plan\s*:\s*([^\n]+?)\s*(?:\n|$)", text)
    if plan:
        out["plan"] = re.sub(r"\s+", " ", plan).strip()
        # 被吃掉空格的「ProTrial」补回空格
        out["plan"] = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", out["plan"])
    exp = _last(r"Plan\s*Expires\s*At\s*:\s*([^\n]+?)\s*(?:\n|$)", text)
    if exp:
        out["expires"] = exp.strip()
        m = re.search(r"([A-Za-z]{3})\s*(\d{1,2}),\s*(\d{4})\s*at\s*(\d{1,2}):(\d{2})", exp)
        if m:
            try:
                dt = datetime.strptime(" ".join(m.groups()[:3]) + f" {m.group(4)}:{m.group(5)}", "%b %d %Y %H:%M")
                out["expires"] = dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
    for key, label in (("plan_credits", r"Plan\s*Credits\s*Used"), ("addon_credits", r"Add-?\s*on\s*Credits\s*Used")):
        pair = _last(label + r"\s*:\s*([\d.,]+)\s*/\s*([\d.,]+)", text)
        if pair:
            out[key] = (_num(pair[0]), _num(pair[1]))
    org = _last(r"Org\s*Resource\s*Package\s*:\s*([^\n]+?)\s*(?:\n|$)", text)
    if org:
        out["org_package"] = org.strip()
    return out


def fetch_qoder_plan_usage(
    qoder_bin: Optional[str] = None,
    config_dir: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = 25.0,
) -> dict:
    """跑一次交互式 /usage，返回 parse_qoder_usage_screen 的结果；失败抛 RuntimeError。"""
    import fcntl
    import pty
    import struct
    import termios

    key = (qoder_bin or "", config_dir or "", bool(api_key))
    hit = _usage_cache.get(key)
    if hit and time.time() - hit[0] < _USAGE_CACHE_TTL:
        return hit[1]

    os.makedirs(QODER_USAGE_CWD, exist_ok=True)
    cmd = [resolve_qoder_bin(qoder_bin)]
    if config_dir:
        cmd += ["--config-dir", os.path.expanduser(config_dir)]
    env = dict(os.environ, TERM="xterm-256color", COLUMNS="140", LINES="50", CC_LARK_MIRROR_OFF="1")
    if api_key:
        env["QODER_PERSONAL_ACCESS_TOKEN"] = api_key
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 140, 0, 0))
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave, cwd=QODER_USAGE_CWD,
                            env=env, start_new_session=True, close_fds=True)
    os.close(slave)
    buf = bytearray()
    deadline = time.time() + timeout

    def pump(sec: float, until: Optional[str] = None) -> bool:
        end = min(time.time() + sec, deadline)
        while time.time() < end:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return False
                if not chunk:
                    return False
                buf.extend(chunk)
                if until and re.search(until, _screen_text(bytes(buf)), re.IGNORECASE):
                    return True
        return False

    try:
        # 首次在这个目录启动会问「信任这个文件夹吗」，默认项就是信任
        if pump(15, r"Type\s*your\s*message|Trust\s*folder"):
            if re.search(r"Trust\s*folder", _screen_text(bytes(buf)), re.IGNORECASE) and \
                    not re.search(r"Type\s*your\s*message", _screen_text(bytes(buf)), re.IGNORECASE):
                os.write(master, b"\r")
                pump(10, r"Type\s*your\s*message")
        mark = len(buf)
        os.write(master, b"/usage")
        pump(0.8)
        os.write(master, b"\r")
        pump(15, r"Plan\s*Credits\s*Used\s*:\s*[\d.,]+\s*/\s*[\d.,]+")
        pump(0.8)  # 让加购那一行也画出来
        result = parse_qoder_usage_screen(_screen_text(bytes(buf[mark:])))
    finally:
        try:
            os.write(master, b"\x1b")
            os.write(master, b"/quit\r")
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait(timeout=2)
        os.close(master)
    if "plan_credits" not in result:
        tail = _screen_text(bytes(buf))[-300:].strip()
        raise RuntimeError(f"没读到 /usage 面板：{tail[-160:]!r}")
    _usage_cache[key] = (time.time(), result)
    return result


# 各模型的上下文窗口（交互界面 /model 面板里的 ▦ 那一列，1.1.65）。qoder 只回占比、
# 不回 token 数，footer 的「已用 / 窗口」靠「占比 × 窗口」换算。没列的都是 200K；
# 用 /context-window 改过窗口的，用 QODER_CONTEXT_WINDOW 覆盖。
_DEFAULT_CONTEXT_WINDOW = 200_000
_MODEL_CONTEXT_WINDOWS = {"performance": 272_000}


def context_window_for(model: Optional[str]) -> int:
    override = (os.getenv("QODER_CONTEXT_WINDOW") or "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    return _MODEL_CONTEXT_WINDOWS.get((model or "").strip().lower(), _DEFAULT_CONTEXT_WINDOW)


def _usage_from_result(
    data: dict, model: Optional[str] = None,
    message_credits: Optional[dict[str, tuple[float, bool]]] = None,
) -> dict:
    """qoder 的 token 数全是 0，真正有用的是上下文占比和 credits。

    credits 有两层：result.total_credits 是这个会话**累计**扣的；本轮扣了多少要把本轮
    每条 assistant 消息的 usage.credits 加起来，且只算 billable 的——限时免费的模型
    （Qwen3.8-Flash）消息上照样记 credits，但 billable=false，不扣。
    """
    usage = data.get("usage") or {}
    out = {
        k: v for k, v in usage.items()
        if k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens")
        and isinstance(v, (int, float)) and v > 0
    }
    ratio = usage.get("context_usage_ratio")
    if isinstance(ratio, (int, float)) and ratio > 0:
        window = context_window_for(model)
        out["_context_ratio"] = float(ratio)
        out["_context_window"] = window
        out["_context_tokens"] = int(round(float(ratio) * window))
    credits = data.get("total_credits")
    if isinstance(credits, (int, float)) and credits > 0:
        out["_session_credits"] = float(credits)
    if message_credits:
        out["_turn_credits"] = round(sum(c for c, billable in message_credits.values() if billable), 4)
        out["_turn_free_credits"] = round(sum(c for c, billable in message_credits.values() if not billable), 4)
    return out


def format_credits_suffix(usage: dict) -> str:
    """footer / 状态行用的 credits 片段，如「本轮 6.87 credits · 会话累计 28.09」。"""
    turn = usage.get("_turn_credits")
    session = usage.get("_session_credits")
    free = usage.get("_turn_free_credits") or 0
    parts = []
    if isinstance(turn, (int, float)):
        if turn <= 0 and free > 0:
            parts.append("本轮免费")
        elif turn > 0 or not session:
            parts.append(f"本轮 {turn:.2f} credits")
    if isinstance(session, (int, float)) and session > 0 and (
        not isinstance(turn, (int, float)) or session - (turn or 0) > 0.005
    ):
        parts.append(f"会话累计 {session:.2f} credits")
    return " · ".join(parts)


async def run_qoder(
    message: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    permission_mode: Optional[str] = None,
    on_text_chunk: Optional[Callable[[str], None]] = None,
    on_tool_use: Optional[Callable[[str, dict], None]] = None,
    on_process_start: Optional[Callable[[asyncio.subprocess.Process], None]] = None,
    on_usage: Optional[Callable[[dict], None]] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
    append_system_prompt: Optional[str] = None,
    extra_env: Optional[dict] = None,
    effort: Optional[str] = None,
    qoder_bin: Optional[str] = None,
    config_dir: Optional[str] = None,
    api_key: Optional[str] = None,
    dangerously_skip_permissions: bool = True,
    idle_timeout_sec: int = IDLE_TIMEOUT,
) -> tuple[str, Optional[str], bool]:
    """返回 (full_text, session_id, used_fresh_session_fallback)。"""
    del on_status  # qoder 没有独立的状态事件通道，正文/工具事件已够用

    resolved_effort = _normalize_effort(effort)
    resolved_mode = _normalize_permission_mode(
        permission_mode, dangerously_skip_permissions
    )
    idle_limit = idle_timeout_sec if idle_timeout_sec > 0 else IDLE_TIMEOUT
    mcp_cfg = cc_lark_mcp_config(extra_env, log_tag="run_qoder")
    system_prompt = _with_claude_context(append_system_prompt, cwd, config_dir)

    async def _run_once(
        active_session_id: Optional[str],
    ) -> tuple[str, Optional[str], Optional[int], str]:
        cmd = [
            resolve_qoder_bin(qoder_bin),
            "-p",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--permission-mode", resolved_mode,
        ]
        deny_tools = os.getenv("CC_LARK_QODER_DISALLOWED_TOOLS", DEFAULT_DISALLOWED_TOOLS).strip()
        if deny_tools:
            cmd += ["--disallowed-tools", deny_tools]
        # 首轮自己钉一个 UUID，这样 session id 从一开始就归我们掌握
        planned_session_id = active_session_id or str(uuid.uuid4())
        if active_session_id:
            cmd += ["-r", active_session_id]
        else:
            cmd += ["--session-id", planned_session_id]
        if model:
            cmd += ["-m", model]
        if resolved_effort:
            cmd += ["--reasoning-effort", resolved_effort]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        if mcp_cfg:
            cmd += ["--mcp-config", json.dumps(mcp_cfg)]
        if config_dir:
            cmd += ["--config-dir", os.path.expanduser(config_dir)]

        env = os.environ.copy()
        # 别让 bot spawn 出来的 agent 被 session-mirror hook 镜像回 Lark
        env["CC_LARK_MIRROR_OFF"] = "1"
        if api_key:
            env["QODER_PERSONAL_ACCESS_TOKEN"] = api_key
        if extra_env:
            env.update(extra_env)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.path.expanduser("~"),
            env=env,
            limit=10 * 1024 * 1024,
            # 与其它后端一致：独立进程组，/stop 时 killpg 不会误伤 main.py
            start_new_session=True,
        )
        await _fire_callback(on_process_start, proc)

        # stderr 边跑边收：等到 EOF 再读的话，CLI 往 stderr 写满管道缓冲就会卡死
        stderr_task = asyncio.ensure_future(proc.stderr.read())
        try:
            proc.stdin.write(message.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 进程秒退（参数错 / resume 失败），交给下面按 rc + stderr 判
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass

        full_text = ""
        new_session_id = planned_session_id
        # 按 content block 的 index 分开攒工具入参：qoder 并行发起多个工具时，几个
        # tool_use 块的流是交错到的（实测 Write 的入参被随后的 list_crons 冲掉过）
        pending_tools: dict[tuple, dict] = {}
        reported_tool_ids: set[str] = set()
        # 本轮各条 assistant 消息的 credits（同一条消息会分几次事件到，按 id 取最大）。
        # result 里的 total_credits 是**整个会话累计**，本轮花了多少只能自己加。
        message_credits: dict[str, tuple[float, bool]] = {}
        # 开了 --include-partial-messages 后，credits 不在 assistant 事件里，而是在每条
        # 消息收尾的 stream_event/message_delta.usage 里；用 message_start 的 id 对上号
        current_message_id: dict = {}

        def _record_credits(msg_id: str, credits, billable) -> None:
            if not msg_id or not isinstance(credits, (int, float)):
                return
            prev = message_credits.get(msg_id, (0.0, True))[0]
            message_credits[msg_id] = (max(prev, float(credits)), billable is not False)

        idle_seconds = 0
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        wall_clock_limit = resolve_claude_wall_clock_limit(extra_env)

        try:
            while True:
                if wall_clock_limit > 0 and loop.time() - start_time >= wall_clock_limit:
                    proc.kill()
                    await proc.wait()
                    raise RuntimeError(
                        f"Qoder 单轮执行超过 wall-clock 最终上限（{int(wall_clock_limit)}秒），已终止进程。"
                    )

                try:
                    raw_line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=_CHECK_INTERVAL
                    )
                    idle_seconds = 0
                except asyncio.TimeoutError:
                    idle_seconds += _CHECK_INTERVAL
                    has_kids = _has_children(proc.pid)
                    threshold = STUCK_CHILD_TIMEOUT if has_kids else idle_limit
                    if idle_seconds >= threshold:
                        proc.kill()
                        await proc.wait()
                        if has_kids:
                            raise RuntimeError(
                                f"Qoder 执行超时（{threshold}秒有子进程但 Qoder 端无任何新输出），已终止进程。"
                                f"常见原因：tail -f / watch / npm run dev 等永不退出的阻塞命令。"
                            )
                        raise RuntimeError(
                            f"Qoder 执行超时（{threshold}秒无输出且无活跃子进程），已终止进程"
                        )
                    continue

                if not raw_line:  # EOF
                    break

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = data.get("type")

                if event_type == "system":
                    if data.get("subtype") == "init" and data.get("session_id"):
                        new_session_id = data["session_id"]

                elif event_type == "stream_event":
                    evt = data.get("event", {})
                    evt_type = evt.get("type")
                    # 子 agent 的流和主流各自从 index 0 数起，键里要带上 parent
                    block_key = (data.get("parent_tool_use_id"), evt.get("index"))

                    if evt_type == "message_start":
                        current_message_id[data.get("parent_tool_use_id")] = (
                            (evt.get("message") or {}).get("id")
                            or f"anon-{len(current_message_id)}-{len(message_credits)}"
                        )
                    elif evt_type == "message_delta":
                        delta_usage = evt.get("usage") or {}
                        _record_credits(
                            current_message_id.get(data.get("parent_tool_use_id"), ""),
                            delta_usage.get("credits"),
                            delta_usage.get("billable", True),
                        )

                    if evt_type == "content_block_delta":
                        delta = evt.get("delta", {})
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            chunk = delta.get("text", "")
                            # parent_tool_use_id 非空 = 子 agent（Agent 工具）内部的流，
                            # 那是它写给主 agent 看的中间稿，不进给用户的正文
                            if chunk and not data.get("parent_tool_use_id"):
                                full_text += chunk
                                await _fire_callback(on_text_chunk, chunk)
                        elif delta_type == "input_json_delta":
                            pending = pending_tools.get(block_key)
                            if pending is not None:
                                pending["json"] += delta.get("partial_json", "")

                    elif evt_type == "content_block_start":
                        block = evt.get("content_block", {})
                        if block.get("type") == "tool_use":
                            pending_tools[block_key] = {
                                "name": block.get("name", ""),
                                "id": block.get("id") or "",
                                "input": block.get("input") or {},
                                "json": "",
                            }

                    elif evt_type == "content_block_stop":
                        # 入参攒齐了才报一次：交错的流里「先报名字、后补入参」会在卡片上
                        # 留下一行没入参的残影
                        pending = pending_tools.pop(block_key, None)
                        if pending and pending["id"] not in reported_tool_ids:
                            inp = pending["input"]
                            if pending["json"].strip():
                                try:
                                    inp = json.loads(pending["json"])
                                except json.JSONDecodeError:
                                    pass
                            if pending["id"]:
                                reported_tool_ids.add(pending["id"])
                            await _fire_callback(on_tool_use, pending["name"], inp)

                elif event_type == "assistant":
                    msg = data.get("message") or {}
                    msg_usage = msg.get("usage") or {}
                    _record_credits(msg.get("id") or "", msg_usage.get("credits"),
                                    msg_usage.get("billable", True))
                    # 整条 assistant 消息里带着完整的 tool_use；流里漏掉的（没有 partial
                    # 事件 / 没收到 stop）在这里补报
                    if not data.get("parent_tool_use_id"):
                        for block in (data.get("message") or {}).get("content") or []:
                            if not isinstance(block, dict) or block.get("type") != "tool_use":
                                continue
                            tool_id = block.get("id") or ""
                            if tool_id and tool_id in reported_tool_ids:
                                continue
                            if tool_id:
                                reported_tool_ids.add(tool_id)
                            await _fire_callback(on_tool_use, block.get("name", ""), block.get("input") or {})

                elif event_type == "result":
                    sid = data.get("session_id")
                    if sid:
                        new_session_id = sid
                    final_text = _extract_text_content(data.get("result", ""))
                    # 出错也走 result（is_error=true / subtype=error_*），result 里是错误文案。
                    # 与 claude/grok 对齐：识别为错误就 raise，把可 resume 的 session id
                    # 带出去让 dispatcher 续跑，而不是把错误伪装成回答发给用户。
                    if data.get("is_error") or str(data.get("subtype", "")).startswith("error"):
                        detail = final_text or data.get("error") or data.get("subtype") or "unknown error"
                        exc = RuntimeError(f"Qoder 执行出错：{detail}")
                        exc.cc_session_id = new_session_id
                        exc.cc_retryable_resume = not is_fatal_error_text(str(detail))
                        raise exc
                    if final_text:
                        full_text = final_text
                    usage = _usage_from_result(data, model, message_credits)
                    if usage:
                        await _fire_callback(on_usage, usage)
        except BaseException:
            stderr_task.cancel()
            raise

        await proc.wait()
        stderr_text = (await stderr_task).decode("utf-8", errors="replace").strip()
        if stderr_text:
            # 退回 Auto 之类的提示只打在 stderr，留个痕方便查"为什么不是我选的模型"
            print(f"[run_qoder] stderr: {stderr_text[:500]}", flush=True)
        return full_text.strip(), new_session_id, proc.returncode, stderr_text

    final_text, new_session_id, returncode, stderr_text = await _run_once(session_id)
    used_fresh_session_fallback = False

    # resume 失败（会话按 cwd 归档，换了目录 / 换了 config-dir / 会话被删）：
    # rc=42 + "Error resuming session" → 退回新会话。returncode<0 = 被信号杀
    # （/stop、restart），那是人为中断，绝不能 fallback 再拉一个新进程。
    if (
        session_id and not final_text and returncode is not None and returncode > 0
        and (returncode == _RESUME_FAILED_RC or _RESUME_FAILED_MARK in stderr_text)
    ):
        print(
            f"[run_qoder] resume failed (code={returncode}), retrying with fresh session; "
            f"sid={session_id} cwd={cwd} stderr={stderr_text[:200]!r}",
            flush=True,
        )
        final_text, new_session_id, returncode, stderr_text = await _run_once(None)
        used_fresh_session_fallback = True

    if returncode != 0:
        detail = stderr_text or "no stderr"
        if final_text:
            return final_text, new_session_id, used_fresh_session_fallback
        exc = RuntimeError(f"qodercli exited with code {returncode}: {detail}")
        if returncode is not None and returncode > 0 and new_session_id and not is_fatal_error_text(stderr_text):
            exc.cc_session_id = new_session_id
            exc.cc_retryable_resume = True
        raise exc

    return final_text, new_session_id, used_fresh_session_fallback
