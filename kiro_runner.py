"""本地调用 Kiro CLI（kiro-cli，AWS 的 Kiro）的统一入口。

    kiro-cli chat --agent-engine v2 --output-format stream-json
                  --agent <本轮临时 agent> [--trust-all-tools]
                  [--resume-id <uuid>] [--model <model>] [--effort <level>]
    （prompt 从 stdin 进，避开 argv 长度上限）

线格式（stream-json，2.28.0 实测）：每行一个 ACP 事件——
  runStarted → metadata{sessionId, contextUsagePercentage}
  → sessionUpdate{update.sessionUpdate = agent_message_chunk / tool_call / tool_call_update}
  → metadata{meteringUsage:[{value, unit:"credit"}]}（本轮扣的 credits，可能好几条，求和）
  → runFinished{finalText, status, stopReason}；出错是 runError{stage, message}。

⚠️ 必须钉 `--agent-engine v2`：`--resume-id` 指向的会话在 v2 里找不到时，CLI 会自己
切到 v3（KAS）引擎、拿这个 id 新开一个会话，事件格式也换成 session_info_update /
promptTurnSummaries。钉了 v2 后找不到会话会给 runError(stage=init, "Session not
found")，这里识别后退回新会话。v3 的几种事件也顺手解析，CLI 以后改默认值不至于全瞎。

系统提示 + MCP：kiro 没有 --append-system-prompt / --mcp-config，这两样都写在 agent
配置里。每轮在 ~/.kiro/agents/ 写一个临时 agent（prompt = cc-lark 的 Lark 提示 +
Claude Code 的规则/记忆，mcpServers = cc-lark 运行时 MCP），`--agent` 指过去，跑完
删掉。会话里存的是对话历史，换个同内容的临时 agent 续轮不影响上下文（实测）。

模型：auto / claude-opus-5.5 / claude-sonnet-5.5 / gpt-5.6-* / deepseek-3.2 …（`kiro-cli
chat --list-models` 看全表和倍率）。传了不存在的模型会 runError："The model … is not
available"。

计费：按 credits（套餐每月额度）。footer 显示上下文占比 + 本轮 credits；kiro 只给
占比、不给 token 数，「已用 / 窗口」按模型窗口换算。

鉴权：沿用本机 `kiro-cli login` 的登录态（Mac 上是 IAM Identity Center / Pro）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from typing import Callable, Optional

from bot_config import resolve_claude_wall_clock_limit
from claude_context import build_claude_context_brief, link_claude_skills
from claude_runner import (
    _fire_callback,
    _has_children,
    cc_lark_mcp_config,
    is_fatal_error_text,
)

IDLE_TIMEOUT = 600  # 无输出且无子进程 → 视为挂死
STUCK_CHILD_TIMEOUT = int(os.getenv("STUCK_CHILD_TIMEOUT_SEC", "3600"))
_CHECK_INTERVAL = 30

# kiro-cli --effort（2.28.0 help 里的例子）；模型不支持推理时 CLI 只在 stderr 警告一句
KIRO_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

KIRO_HOME = os.path.expanduser("~/.kiro")
KIRO_AGENTS_DIR = os.path.join(KIRO_HOME, "agents")
KIRO_SKILLS_DIR = os.path.join(KIRO_HOME, "skills")
_TEMP_AGENT_PREFIX = "cc-lark-"
_STALE_AGENT_SEC = 6 * 3600

# 临时 agent 的资源：默认 agent 会读的项目规则 / steering / skill，自定义 agent 不写就没有
DEFAULT_RESOURCES = [
    "file://AGENTS.md",
    "file://.kiro/steering/**/*.md",
    "file://~/.kiro/steering/**/*.md",
    "skill://.kiro/skills/**/SKILL.md",
    "skill://~/.kiro/skills/**/SKILL.md",
]

# 各模型的上下文窗口（`kiro-cli chat --list-models` 的 context_window_tokens，2.28.0）。
# 没列的按 1M 算（auto 和新模型都是 1M）；KIRO_CONTEXT_WINDOW 可覆盖。
_DEFAULT_CONTEXT_WINDOW = 1_000_000
_MODEL_CONTEXT_WINDOWS = {
    "claude-opus-4.5": 200_000,
    "claude-sonnet-4.5": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4.5": 200_000,
    "deepseek-3.2": 164_000,
    "minimax-m2.5": 196_000,
    "minimax-m2.1": 196_000,
    "glm-5": 200_000,
    "qwen3-coder-next": 256_000,
}

# 给 /model 用的静态清单（id, 倍率, 说明）；以 `--list-models` 为准，拿不到时兜底
KIRO_MODELS: list[tuple[str, float, str]] = [
    ("auto", 1.0, "按任务自动选模型"),
    ("claude-opus-5.5", 2.0, "Claude Opus 5.5（预览，1M）"),
    ("claude-sonnet-5.5", 1.3, "Claude Sonnet 5.5（预览，1M）"),
    ("claude-opus-5", 2.2, "Claude Opus 5（1M）"),
    ("claude-sonnet-5", 1.3, "Claude Sonnet 5（1M）"),
    ("claude-haiku-4.5", 0.4, "Claude Haiku 4.5"),
    ("gpt-5.6-sol", 4.4, "GPT-5.6 Sol（预览，1M）"),
    ("gpt-5.6-terra", 2.2, "GPT-5.6 Terra（预览，1M）"),
    ("gpt-5.6-luna", 0.6, "GPT-5.6 Luna（预览，1M）"),
    ("glm-5", 0.5, "GLM-5"),
    ("deepseek-3.2", 0.25, "DeepSeek V3.2（预览）"),
    ("minimax-m2.5", 0.25, "MiniMax M2.5"),
    ("qwen3-coder-next", 0.05, "Qwen3 Coder Next（预览）"),
]

_RESUME_FAILED_MARKS = ("Session not found", "load_session failed")


def resolve_kiro_bin(configured: Optional[str] = None) -> str:
    if configured:
        return os.path.expanduser(configured)
    found = shutil.which("kiro-cli")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/kiro-cli", "~/.local/bin/kiro-cli"):
        path = os.path.expanduser(cand)
        if os.path.exists(path):
            return path
    return "kiro-cli"


def _normalize_effort(effort: Optional[str]) -> Optional[str]:
    e = (effort or "").strip().lower()
    if not e or e in ("auto", "default"):
        return None
    if e not in KIRO_EFFORT_LEVELS:
        raise ValueError(
            f"invalid Kiro effort {e!r}; expected one of {list(KIRO_EFFORT_LEVELS)}"
        )
    return e


def default_effort(profile_name: Optional[str] = None) -> Optional[str]:
    """profile 级默认强度：<PROFILE>_KIRO_EFFORT 优先，再退到 KIRO_EFFORT；非法值当没配。"""
    prof = (profile_name or "").strip().upper()
    raw = (os.getenv(f"{prof}_KIRO_EFFORT") if prof else None) or os.getenv("KIRO_EFFORT") or ""
    try:
        return _normalize_effort(raw)
    except ValueError:
        return None


def context_window_for(model: Optional[str]) -> int:
    override = (os.getenv("KIRO_CONTEXT_WINDOW") or "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    return _MODEL_CONTEXT_WINDOWS.get((model or "").strip().lower(), _DEFAULT_CONTEXT_WINDOW)


_models_cache: dict[str, tuple[float, list[dict]]] = {}


def list_kiro_models(kiro_bin: Optional[str] = None, timeout: float = 30.0) -> list[dict]:
    """`kiro-cli chat --list-models --format json` 的 models 数组；缓存 10 分钟，失败返回 []。"""
    key = kiro_bin or ""
    hit = _models_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        out = subprocess.run(
            [resolve_kiro_bin(kiro_bin), "chat", "--list-models", "--format", "json"],
            capture_output=True, text=True, timeout=timeout,
            env=dict(os.environ, CC_LARK_MIRROR_OFF="1"),
        )
        models = json.loads(out.stdout or "{}").get("models") or []
    except Exception:  # noqa: BLE001 — 拿不到就用静态表
        return []
    if models:
        _models_cache[key] = (time.time(), models)
    return models


# ── 套餐额度（/usage）────────────────────────────────────────────
# credits 余量只在交互界面的 /usage 面板里有（"Credits (0.47 of 1000 covered in plan)"）。
# 开一个伪终端跑交互式 kiro-cli，敲 /usage 读屏再退出——不调模型、不花 credits。
KIRO_USAGE_CWD = os.path.expanduser("~/.feishu-claude/kiro-usage")
_USAGE_CACHE_TTL = 30
_usage_cache: dict[str, tuple[float, dict]] = {}


def _screen_text(raw: bytes) -> str:
    """TUI 输出转纯文本：光标右移换成空格，其余控制序列去掉。"""
    t = raw.decode("utf-8", "replace")
    t = re.sub(r"\x1b\[(\d*)C", lambda m: " " * int(m.group(1) or 1), t)
    t = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", t)
    t = re.sub(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]", "", t)
    t = re.sub(r"\x1b[()][A-Za-z0-9]", "", t)
    return t.replace("\r", "\n")


def parse_kiro_usage_screen(text: str) -> dict:
    """从 /usage 面板抠套餐额度；TUI 会重绘，同一项取最后一次出现的。"""
    out: dict = {}
    head = re.findall(r"Estimated\s+Usage\s*\|\s*resets\s+on\s+([0-9-]+)\s*\|\s*([^\n]+)", text, re.I)
    if head:
        out["resets"] = head[-1][0].strip()
        out["plan"] = re.sub(r"\s+", " ", head[-1][1]).strip()
    cred = re.findall(r"Credits\s*\(\s*([\d.,]+)\s+of\s+([\d.,]+)\s+covered\s+in\s+plan\s*\)", text, re.I)
    if cred:
        used, total = (float(x.replace(",", "")) for x in cred[-1])
        out["plan_credits"] = (used, total)
    over = re.findall(r"Overages?\s*[:(]?\s*([^\n)]+)", text, re.I)
    if over:
        out["overage"] = over[-1].strip()
    return out


def fetch_kiro_plan_usage(kiro_bin: Optional[str] = None, timeout: float = 40.0) -> dict:
    """跑一次交互式 /usage，返回 parse_kiro_usage_screen 的结果；失败抛 RuntimeError。"""
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios

    key = kiro_bin or ""
    hit = _usage_cache.get(key)
    if hit and time.time() - hit[0] < _USAGE_CACHE_TTL:
        return hit[1]
    os.makedirs(KIRO_USAGE_CWD, exist_ok=True)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 140, 0, 0))
    proc = subprocess.Popen(
        [resolve_kiro_bin(kiro_bin), "chat", "--agent-engine", "v2"],
        stdin=slave, stdout=slave, stderr=slave, cwd=KIRO_USAGE_CWD,
        env=dict(os.environ, TERM="xterm-256color", CC_LARK_MIRROR_OFF="1"),
        start_new_session=True, close_fds=True,
    )
    os.close(slave)
    buf = bytearray()
    deadline = time.time() + timeout

    def pump(sec: float, until: Optional[str] = None, start: int = 0) -> bool:
        end = min(time.time() + sec, deadline)
        while time.time() < end:
            ready, _, _ = select.select([master], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return False
            if not chunk:
                return False
            buf.extend(chunk)
            if b"\x1b[6n" in chunk:  # 终端问光标位置，不答它会一直等
                os.write(master, b"\x1b[1;1R")
            if until and re.search(until, _screen_text(bytes(buf[start:])), re.I):
                return True
        return False

    try:
        pump(25, r"ask a question|describe a task")
        mark = len(buf)
        os.write(master, b"/usage")
        pump(1.0)
        os.write(master, b"\r")
        pump(15, r"Credits\s*\(\s*[\d.,]+\s+of\s+[\d.,]+", start=mark)
        pump(0.6)
        result = parse_kiro_usage_screen(_screen_text(bytes(buf[mark:])))
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


def _with_claude_context(append_system_prompt: Optional[str], cwd: Optional[str]) -> str:
    """把 Claude Code 的 skill 软链进 ~/.kiro/skills，规则 / 记忆拼到系统提示后面。"""
    base = append_system_prompt or ""
    if os.getenv("CC_LARK_KIRO_CLAUDE_CONTEXT", "1") == "0":
        return base
    try:
        linked = link_claude_skills(KIRO_SKILLS_DIR)
        if linked:
            print(f"[run_kiro] 同步 Claude skill → {KIRO_SKILLS_DIR}: {', '.join(linked)}", flush=True)
        brief = build_claude_context_brief(
            cwd,
            skills_note=(
                f"~/.claude/skills 里的 skill 已经软链进 {KIRO_SKILLS_DIR}（lark-* 那批也在），"
                "都在你的 skill 列表里，按名字正常用。"
            ),
        )
    except Exception as exc:  # noqa: BLE001 — 接不上 Claude 的上下文不该拖垮这一轮
        print(f"[run_kiro] Claude 上下文跳过: {type(exc).__name__}: {exc}", flush=True)
        return base
    if not brief:
        return base
    return f"{base}\n\n{brief}" if base else brief


def _cleanup_stale_agents(agents_dir: str) -> None:
    """进程被硬杀时 finally 跑不到，临时 agent 会留下来；顺手清掉几小时前的。"""
    try:
        now = time.time()
        for name in os.listdir(agents_dir):
            if name.startswith(_TEMP_AGENT_PREFIX) and name.endswith(".json"):
                path = os.path.join(agents_dir, name)
                if now - os.path.getmtime(path) > _STALE_AGENT_SEC:
                    os.remove(path)
    except OSError:
        pass


def write_temp_agent(
    system_prompt: str,
    mcp_cfg: Optional[dict],
    agents_dir: str = KIRO_AGENTS_DIR,
    trust_all: bool = True,
) -> tuple[str, str]:
    """写本轮的临时 agent 配置，返回 (agent 名, 文件路径)。"""
    os.makedirs(agents_dir, exist_ok=True)
    _cleanup_stale_agents(agents_dir)
    name = f"{_TEMP_AGENT_PREFIX}{uuid.uuid4().hex[:12]}"
    cfg = {
        "name": name,
        "description": "cc-lark 每轮生成的临时 agent（跑完即删）",
        "prompt": system_prompt or None,
        "mcpServers": (mcp_cfg or {}).get("mcpServers") or {},
        "tools": ["*"],
        "allowedTools": ["*"] if trust_all else [],
        "resources": list(DEFAULT_RESOURCES),
        "hooks": {},
        "toolsSettings": {},
        # 用户自己在 ~/.kiro/settings/mcp.json 里配的 MCP 照样加载
        "useLegacyMcpJson": True,
    }
    path = os.path.join(agents_dir, f"{name}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)
    return name, path


def _tool_display(update: dict) -> tuple[str, dict]:
    """tool_call 事件 → (卡片上显示的工具名, 入参)。MCP 工具对齐成 mcp__<server>__<tool>。"""
    meta = ((update.get("_meta") or {}).get("kiro")) or {}
    name = meta.get("toolName") or update.get("title") or update.get("kind") or "tool"
    server = meta.get("mcpServerName")
    if server:
        name = f"mcp__{server}__{name}"
    raw = update.get("rawInput")
    inp = {k: v for k, v in raw.items() if k != "__tool_use_purpose"} if isinstance(raw, dict) else {}
    if not inp and isinstance(raw, dict) and raw.get("__tool_use_purpose"):
        inp = {"purpose": raw["__tool_use_purpose"]}
    return name, inp


def _sum_credits(items) -> float:
    total = 0.0
    for it in items or []:
        if isinstance(it, dict):
            v = it.get("value", it.get("usage"))
            if isinstance(v, (int, float)):
                total += float(v)
    return total


def _usage(ctx_pct: Optional[float], credits: Optional[float], model: Optional[str]) -> dict:
    out: dict = {}
    if isinstance(ctx_pct, (int, float)) and ctx_pct > 0:
        window = context_window_for(model)
        ratio = float(ctx_pct) / 100.0
        out["_context_ratio"] = ratio
        out["_context_window"] = window
        out["_context_tokens"] = int(round(ratio * window))
    if isinstance(credits, (int, float)):
        out["_turn_credits"] = round(float(credits), 4)
    return out


class _ResumeFailed(Exception):
    pass


async def run_kiro(
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
    kiro_bin: Optional[str] = None,
    dangerously_skip_permissions: bool = True,
    idle_timeout_sec: int = IDLE_TIMEOUT,
    agents_dir: str = KIRO_AGENTS_DIR,
) -> tuple[str, Optional[str], bool]:
    """返回 (full_text, session_id, used_fresh_session_fallback)。"""
    del on_status  # kiro 没有独立的状态事件通道
    # /effort 没覆盖时用 profile 默认（KIRO_EFFORT）；session 里只存覆盖值
    resolved_effort = _normalize_effort(effort) or default_effort((extra_env or {}).get("CC_LARK_PROFILE"))
    trust_all = bool(dangerously_skip_permissions) or (permission_mode or "").lower() in (
        "bypasspermissions", "bypass_permissions",
    )
    idle_limit = idle_timeout_sec if idle_timeout_sec > 0 else IDLE_TIMEOUT
    mcp_cfg = cc_lark_mcp_config(extra_env, log_tag="run_kiro")
    system_prompt = _with_claude_context(append_system_prompt, cwd)
    agent_name, agent_path = write_temp_agent(system_prompt, mcp_cfg, agents_dir, trust_all)

    async def _run_once(active_session_id: Optional[str]) -> tuple[str, Optional[str], Optional[int], str]:
        cmd = [
            resolve_kiro_bin(kiro_bin), "chat",
            "--agent-engine", "v2",
            "--output-format", "stream-json",
            "--agent", agent_name,
        ]
        if trust_all:
            cmd.append("--trust-all-tools")
        if active_session_id:
            cmd += ["--resume-id", active_session_id]
        if model:
            cmd += ["--model", model]
        if resolved_effort:
            cmd += ["--effort", resolved_effort]

        env = os.environ.copy()
        env["CC_LARK_MIRROR_OFF"] = "1"
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items() if v is not None})

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.path.expanduser("~"),
            env=env,
            limit=10 * 1024 * 1024,
            start_new_session=True,
        )
        await _fire_callback(on_process_start, proc)
        stderr_task = asyncio.ensure_future(proc.stderr.read())
        try:
            proc.stdin.write(message.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass

        full_text = ""
        new_session_id = active_session_id
        reported_tool_ids: set[str] = set()
        ctx_pct: Optional[float] = None
        credits: Optional[float] = None
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
                        f"Kiro 单轮执行超过 wall-clock 最终上限（{int(wall_clock_limit)}秒），已终止进程。"
                    )
                try:
                    raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=_CHECK_INTERVAL)
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
                                f"Kiro 执行超时（{threshold}秒有子进程但 Kiro 端无任何新输出），已终止进程。"
                                f"常见原因：tail -f / watch / npm run dev 等永不退出的阻塞命令。"
                            )
                        raise RuntimeError(f"Kiro 执行超时（{threshold}秒无输出且无活跃子进程），已终止进程")
                    continue
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = data.get("type")
                body = data.get("data") or {}
                sid = body.get("sessionId")
                # 主会话 id 认第一条带 id 的事件和 runFinished；子 agent 的事件 id 不一样，别被它覆盖
                if sid and (etype == "runFinished" or not new_session_id):
                    new_session_id = sid

                if etype == "metadata":
                    pct = body.get("contextUsagePercentage")
                    if isinstance(pct, (int, float)):
                        ctx_pct = float(pct)
                    if body.get("meteringUsage"):
                        credits = (credits or 0.0) + _sum_credits(body["meteringUsage"])

                elif etype == "sessionUpdate":
                    upd = body.get("update") or {}
                    kind = upd.get("sessionUpdate")
                    if kind == "agent_message_chunk":
                        content = upd.get("content") or {}
                        chunk = content.get("text") if content.get("type", "text") == "text" else ""
                        # 子 agent（delegate）的流带的是别的 sessionId，不进给用户的正文
                        if chunk and (not new_session_id or not sid or sid == new_session_id):
                            full_text += chunk
                            await _fire_callback(on_text_chunk, chunk)
                    elif kind == "tool_call":
                        tid = upd.get("toolCallId") or ""
                        if tid and tid in reported_tool_ids:
                            continue
                        if tid:
                            reported_tool_ids.add(tid)
                        name, inp = _tool_display(upd)
                        await _fire_callback(on_tool_use, name, inp)
                    elif kind == "session_info_update":  # v3 引擎
                        meta = (upd.get("_meta") or {}).get("kiro") or {}
                        cu = (meta.get("contextUsage") or {}).get("usagePercentage")
                        if isinstance(cu, (int, float)):
                            ctx_pct = float(cu)
                        if meta.get("promptTurnSummaries"):
                            credits = (credits or 0.0) + _sum_credits(meta["promptTurnSummaries"])

                elif etype == "runError":
                    msg = str(body.get("message") or "unknown error")
                    if active_session_id and body.get("stage") == "init" and any(
                        m in msg for m in _RESUME_FAILED_MARKS
                    ):
                        raise _ResumeFailed(msg)
                    exc = RuntimeError(f"Kiro 执行出错：{msg}")
                    if new_session_id:
                        exc.cc_session_id = new_session_id
                        exc.cc_retryable_resume = not is_fatal_error_text(msg) and "not available" not in msg
                    raise exc

                elif etype == "runFinished":
                    final_text = body.get("finalText")
                    if isinstance(final_text, str) and final_text.strip() and not body.get("finalTextTruncated"):
                        full_text = final_text
                    status = str(body.get("status") or "success")
                    if status not in ("success", "completed"):
                        detail = body.get("stopReason") or status
                        exc = RuntimeError(f"Kiro 执行出错：{detail}")
                        exc.cc_session_id = new_session_id
                        exc.cc_retryable_resume = True
                        raise exc
        except BaseException:
            stderr_task.cancel()
            try:
                if proc.returncode is None:
                    proc.kill()
            except ProcessLookupError:
                pass
            raise

        await proc.wait()
        stderr_text = (await stderr_task).decode("utf-8", errors="replace").strip()
        if stderr_text:
            print(f"[run_kiro] stderr: {stderr_text[-500:]}", flush=True)
        usage = _usage(ctx_pct, credits, model)
        if usage:
            await _fire_callback(on_usage, usage)
        return full_text.strip(), new_session_id, proc.returncode, stderr_text

    used_fresh_session_fallback = False
    try:
        try:
            final_text, new_session_id, returncode, stderr_text = await _run_once(session_id)
        except _ResumeFailed as exc:
            # 会话被删 / 换了机器 / 存储不在了：退回新会话
            print(f"[run_kiro] resume failed, retrying with fresh session; sid={session_id} err={exc}", flush=True)
            final_text, new_session_id, returncode, stderr_text = await _run_once(None)
            used_fresh_session_fallback = True
    finally:
        try:
            os.remove(agent_path)
        except OSError:
            pass

    if returncode not in (0, None):
        if final_text:
            return final_text, new_session_id, used_fresh_session_fallback
        exc = RuntimeError(f"kiro-cli exited with code {returncode}: {stderr_text[-500:] or 'no stderr'}")
        if returncode > 0 and new_session_id and not is_fatal_error_text(stderr_text):
            exc.cc_session_id = new_session_id
            exc.cc_retryable_resume = True
        raise exc
    return final_text, new_session_id, used_fresh_session_fallback
