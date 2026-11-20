"""本地调用 pi（earendil-works/pi 的编程 agent，CLI 叫 `pi`）的统一入口。

    pi --mode json --session-dir <PI_SESSION_DIR> [--session <会话文件>]
       [--model <provider/id>] [--thinking <level>] [--tools read,grep,find,ls]
       [--append-system-prompt <临时文件>] [-e pi_extensions/cc_lark_mcp.ts]
       [--skill ~/.claude/skills] [--skill <cwd>/.claude/skills]
    （prompt 从 stdin 进：管道进来的 stdin 会拼在首条 prompt 前，没有 argv 长度上限）

装法：`sudo npm install -g @earendil-works/pi-coding-agent`（1.5.2 起包名从
@mariozechner 迁到 @earendil-works）。

线格式（JSON 模式，1.5.2 实测）：第一行是会话头 {type:"session", id, cwd}，之后是
agent_start / turn_start / message_start / message_update（assistantMessageEvent 是
text_delta / thinking_delta / toolcall_* 增量）/ message_end（权威消息：content、usage、
cost、stopReason、errorMessage）/ tool_execution_start|update|end / turn_end /
agent_end / agent_settled。stdout 只有 JSONL，告警和日志在 stderr。
⚠️ 模型出错（stopReason=error/aborted）时 JSON 模式退出码仍是 0，要自己看 message_end。

会话：pi 的会话是一个 jsonl 文件，id 在会话头里。默认按 cwd 分目录存，换了 cwd 再用
`--session <id>` 会在终端里问「Fork this session into current directory? [y/N]」，
stdin 不是终端就当 N 直接退出。所以这里用 `--session-dir` 钉一个扁平目录，续轮时按
id 找到文件、用 `--session <文件路径>` 打开（跨 cwd 也直接续上，实测）。文件不在了
（换机器 / 被清）就退回新会话。

模型：`provider/id`，如 google/gemini-3.8-flash、openai-codex/gpt-5.5、
anthropic/claude-…。凭据走 pi 自己的（~/.pi/agent/auth.json，`pi auth login <provider>`），
加上环境变量里的各家 API key。本机没单独配 GEMINI_API_KEY 时，借 agy 那把 Gemini
API key（AGY_API_KEY，PI_GEMINI_KEY_FROM 可改来源）。不在 pi 模型表里的 id（如
gemini-4-neon）pi 会在 stderr 警告一句然后照样当自定义 id 用。

推理强度：`--thinking off|minimal|low|medium|high|xhigh|max`；cc-lark 的 none = off。

MCP：pi 没有 --mcp-config，全局 ~/.pi/agent/mcp.json 又是所有会话共用的（并发话题
的 CC_LARK_* 会串）。所以每轮把 cc-lark 运行时 MCP 配置（Claude 同款 JSON）放进
环境变量 CC_LARK_PI_MCP_CONFIG，用 `-e pi_extensions/cc_lark_mcp.ts` 这个扩展在
会话里 registerMcpServer（exposure=direct）。pi 会把 server 名里的 `-` 换成 `_`，
工具名是 `mcp__cc_lark__<tool>`，提示词里有一段转译。

权限：pi 没有审批机制，工具默认全开。profile 关了 PI_DANGEROUS_SKIP 且本轮不是
bypassPermissions 时只给只读工具（read,grep,find,ls），MCP 工具不受影响。

Claude Code 的规则 / 记忆 / skill：pi 自己会沿 cwd 往上加载 AGENTS.md / CLAUDE.md、
扫 ~/.agents/skills；~/.claude/skills 和项目 .claude/skills 用 --skill 带进去；全局
~/.claude/CLAUDE.md 和记忆索引拼进系统提示（见 claude_context.py）。
CC_LARK_PI_CLAUDE_CONTEXT=0 关掉。

计费：按所选 provider 计。每条 assistant 消息自带 usage + cost（美元，pi 按模型表
估算），footer 显示上下文占用 + 本轮花费。
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import tempfile
from typing import Callable, Optional

from bot_config import resolve_claude_wall_clock_limit
from claude_context import CLAUDE_SKILLS_DIR, build_claude_context_brief
from claude_runner import (
    _fire_callback,
    _has_children,
    cc_lark_mcp_config,
    is_fatal_error_text,
)

IDLE_TIMEOUT = 600  # 无输出且无子进程 → 视为挂死
STUCK_CHILD_TIMEOUT = int(os.getenv("STUCK_CHILD_TIMEOUT_SEC", "3600"))
_CHECK_INTERVAL = 30

PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
# /effort 面板用的档位（cc-lark 习惯写 none，runner 里换成 off）
PI_EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

DEFAULT_MODEL = "google/gemini-3.8-flash"
DEFAULT_SESSION_DIR = os.path.expanduser("~/.feishu-claude/pi-sessions")
EXTENSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_extensions", "cc_lark_mcp.ts")
READ_ONLY_TOOLS = "read,grep,find,ls"

# 给 /model 用的静态清单（别名, 显示名）；别名在 commands.MODEL_ALIASES 里解析
PI_MODEL_BUTTONS: list[tuple[str, str]] = [
    ("pi-flash", "🚀 Gemini 3.8 Flash"),
    ("pi-neon", "✴️ Gemini 4 Neon"),
    ("pi-argon", "🔆 Gemini 4 Argon"),
    ("pi-gemini-pro", "💎 Gemini 3.1 Pro"),
    ("pi-gpt", "🤖 GPT-5.5（ChatGPT 订阅，要先 pi auth login openai-codex）"),
]

# 上下文窗口：先问 `pi --list-models`（准），拿不到按家族猜；PI_CONTEXT_WINDOW 可覆盖
_DEFAULT_CONTEXT_WINDOW = 200_000
_window_cache: dict[str, int] = {}

# pi 工具名 → 卡片认识的名字（dispatcher._format_tool 按名字挑图标和主参数）
_TOOL_DISPLAY = {"find": "glob", "ls": "list_dir"}


def resolve_pi_bin(configured: Optional[str] = None) -> str:
    if configured:
        return os.path.expanduser(configured)
    found = shutil.which("pi")
    if found:
        return found
    for cand in ("/usr/bin/pi", "/usr/local/bin/pi", "/opt/homebrew/bin/pi", "~/.local/bin/pi"):
        path = os.path.expanduser(cand)
        if os.path.exists(path):
            return path
    return "pi"


def normalize_thinking(effort: Optional[str]) -> Optional[str]:
    """cc-lark 的 effort → pi 的 --thinking；空 / auto / default = 不传（跟 pi 自己的默认）。"""
    e = (effort or "").strip().lower()
    if not e or e in ("auto", "default"):
        return None
    if e == "none":
        return "off"
    if e not in PI_THINKING_LEVELS:
        raise ValueError(f"invalid pi thinking level {e!r}; expected one of {list(PI_THINKING_LEVELS)}")
    return e


def default_effort(profile_name: Optional[str] = None) -> Optional[str]:
    """profile 级默认强度：<PROFILE>_PI_EFFORT 优先，再退到 PI_EFFORT；非法值当没配。"""
    prof = (profile_name or "").strip().upper()
    raw = (os.getenv(f"{prof}_PI_EFFORT") if prof else None) or os.getenv("PI_EFFORT") or ""
    try:
        return normalize_thinking(raw)
    except ValueError:
        return None


def default_model(profile_name: Optional[str] = None) -> str:
    """没 /model 覆盖时用的模型：<PROFILE>_PI_MODEL > PI_MODEL > google/gemini-3.8-flash。"""
    prof = (profile_name or "").strip().upper()
    raw = (os.getenv(f"{prof}_PI_MODEL") if prof else None) or os.getenv("PI_MODEL") or ""
    return raw.strip() or DEFAULT_MODEL


def resolve_model(model: Optional[str], profile_name: Optional[str] = None) -> str:
    """会话里记的模型 → 传给 pi 的 provider/id。

    别名（pi-flash …）在这里解析；不是 pi 的模型（话题没 /model 覆盖时会话里记的是
    profile 默认，比如 Claude bot 的 opus[1m]）一律换成 pi 的默认模型。
    """
    fallback = default_model(profile_name)
    raw = (model or "").strip()
    if not raw:
        return fallback
    from bot_config import normalize_model_for_runner
    return normalize_model_for_runner(raw, "pi", fallback=fallback) or fallback


def session_dir() -> str:
    return os.path.expanduser(os.getenv("PI_SESSION_DIR") or DEFAULT_SESSION_DIR)


def find_session_file(session_id: Optional[str], sessions: Optional[str] = None) -> Optional[str]:
    """按会话 id 找 pi 的会话文件（`<时间戳>_<id>.jsonl`）；找不到返回 None。"""
    sid = (session_id or "").strip()
    if not sid or not re.fullmatch(r"[0-9A-Za-z-]{8,64}", sid):
        return None
    hits = sorted(glob.glob(os.path.join(sessions or session_dir(), f"*_{sid}.jsonl")))
    return hits[-1] if hits else None


def session_cost(session_id: Optional[str], sessions: Optional[str] = None) -> Optional[float]:
    """会话累计花费（美元）：会话文件里每条 assistant 消息的 usage.cost.total 求和。"""
    path = find_session_file(session_id, sessions)
    if not path:
        return None
    total = 0.0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    msg = (json.loads(line).get("message") or {})
                except json.JSONDecodeError:
                    continue
                if msg.get("role") == "assistant":
                    cost = ((msg.get("usage") or {}).get("cost") or {}).get("total")
                    if isinstance(cost, (int, float)):
                        total += float(cost)
    except OSError:
        return None
    return total


def _parse_window(text: str) -> Optional[int]:
    m = re.fullmatch(r"([\d.]+)\s*([KkMm]?)", (text or "").strip())
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2).lower()
    return int(round(n * (1_000_000 if unit == "m" else 1000 if unit == "k" else 1)))


def parse_list_models(text: str) -> dict[str, int]:
    """`pi --list-models` 的表格 → {"provider/model": 上下文窗口}。"""
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        cols = line.split()
        if len(cols) < 3 or cols[0] == "provider":
            continue
        window = _parse_window(cols[2])
        if window:
            out[f"{cols[0]}/{cols[1]}".lower()] = window
    return out


def _guess_window(model: str) -> int:
    low = (model or "").lower()
    if "gemini" in low or "[1m]" in low:
        return 1_000_000
    return _DEFAULT_CONTEXT_WINDOW


async def context_window_for(model: Optional[str], pi_bin: Optional[str] = None, env: Optional[dict] = None) -> int:
    override = (os.getenv("PI_CONTEXT_WINDOW") or "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    key = (model or "").strip().lower().split(":", 1)[0]
    if not key:
        return _DEFAULT_CONTEXT_WINDOW
    if key in _window_cache:
        return _window_cache[key]
    window = 0
    try:
        search = key.split("/", 1)[-1]
        proc = await asyncio.create_subprocess_exec(
            resolve_pi_bin(pi_bin), "--list-models", search,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env or None,
            start_new_session=True,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        table = parse_list_models(out.decode("utf-8", errors="replace"))
        window = table.get(key) or (table.get(f"google/{key}") if "/" not in key else 0) or 0
    except Exception:  # noqa: BLE001 — 查不到就按家族猜
        window = 0
    window = window or _guess_window(key)
    _window_cache[key] = window
    return window


def _with_claude_context(append_system_prompt: Optional[str], cwd: Optional[str]) -> str:
    base = append_system_prompt or ""
    if os.getenv("CC_LARK_PI_CLAUDE_CONTEXT", "1") == "0":
        return base
    try:
        brief = build_claude_context_brief(
            cwd,
            skills_note=(
                "~/.claude/skills 里的 skill 已经用 --skill 带进来了（~/.agents/skills 里的 lark-* 那批"
                "pi 本来就会扫），都在你的 skill 列表里，按名字正常用。"
            ),
            include_project_rules=False,
        )
    except Exception as exc:  # noqa: BLE001 — 接不上 Claude 的上下文不该拖垮这一轮
        print(f"[run_pi] Claude 上下文跳过: {type(exc).__name__}: {exc}", flush=True)
        return base
    if not brief:
        return base
    return f"{base}\n\n{brief}" if base else brief


def _skill_args(cwd: Optional[str]) -> list[str]:
    if os.getenv("CC_LARK_PI_CLAUDE_CONTEXT", "1") == "0":
        return []
    args: list[str] = []
    dirs = [CLAUDE_SKILLS_DIR]
    if cwd:
        dirs.append(os.path.join(os.path.expanduser(cwd), ".claude", "skills"))
    for d in dirs:
        if os.path.isdir(d):
            args += ["--skill", d]
    return args


def _message_text(message: dict) -> str:
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _tool_name(name: str) -> str:
    return _TOOL_DISPLAY.get(name, name)


def _tool_done_input(name: str, args: dict, event: dict) -> dict:
    """工具跑完再报一次同名同参的事件，卡片把那一行改成 ✅ / ❌。"""
    inp = dict(args or {})
    failed = bool(event.get("isError"))
    if name == "bash":
        structured = (event.get("result") or {}).get("structuredContent") or {}
        inp["status"] = "error" if failed and "exit_code" not in structured else "completed"
        if "exit_code" in structured:
            inp["exit_code"] = structured.get("exit_code")
    else:
        # 卡片的通用完成标记（_ 开头的键不进工具主参数，不会被当成另一个工具）
        inp["_agy_state"] = "ERROR" if failed else "DONE"
    return inp


def _context_usage(usage: dict) -> dict:
    """pi 的 usage → footer 认的 Anthropic 风格字段（input 不含缓存命中部分，和 Claude 一致）。"""
    return {
        "input_tokens": int(usage.get("input") or 0),
        "cache_read_input_tokens": int(usage.get("cacheRead") or 0),
        "cache_creation_input_tokens": int(usage.get("cacheWrite") or 0),
        "output_tokens": int(usage.get("output") or 0),
    }


class _ResumeFailed(Exception):
    pass


async def run_pi(
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
    pi_bin: Optional[str] = None,
    agent_dir: Optional[str] = None,
    dangerously_skip_permissions: bool = True,
    idle_timeout_sec: int = IDLE_TIMEOUT,
    sessions_dir: Optional[str] = None,
) -> tuple[str, Optional[str], bool]:
    """返回 (full_text, session_id, used_fresh_session_fallback)。"""
    del on_status  # 那是 PTY 限流告警通道，pi 的重试只记日志
    profile_name = (extra_env or {}).get("CC_LARK_PROFILE")
    try:
        thinking = normalize_thinking(effort)
    except ValueError:
        # 派活 / 定时任务可能带别家后端才有的档位（codex 的 ultra）：别为这个整轮失败
        print(f"[run_pi] 忽略 pi 不认识的推理强度 {effort!r}", flush=True)
        thinking = None
    thinking = thinking or default_effort(profile_name)
    resolved_model = resolve_model(model, profile_name)
    trust_all = bool(dangerously_skip_permissions) or (permission_mode or "").lower() in (
        "bypasspermissions", "bypass_permissions",
    )
    idle_limit = idle_timeout_sec if idle_timeout_sec > 0 else IDLE_TIMEOUT
    sessions = os.path.expanduser(sessions_dir) if sessions_dir else session_dir()
    os.makedirs(sessions, exist_ok=True)

    env = os.environ.copy()
    env["CC_LARK_MIRROR_OFF"] = "1"
    env.setdefault("PI_SKIP_VERSION_CHECK", "1")
    env.setdefault("PI_TELEMETRY", "0")
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    if agent_dir:
        env["PI_CODING_AGENT_DIR"] = os.path.expanduser(agent_dir)
    if not env.get("GEMINI_API_KEY"):
        src = os.getenv("PI_GEMINI_KEY_FROM", "AGY_API_KEY")
        if src and env.get(src):
            env["GEMINI_API_KEY"] = env[src]
    mcp_cfg = cc_lark_mcp_config(extra_env, log_tag="run_pi")
    if mcp_cfg:
        env["CC_LARK_PI_MCP_CONFIG"] = json.dumps(mcp_cfg, ensure_ascii=False)
    else:
        env.pop("CC_LARK_PI_MCP_CONFIG", None)

    system_prompt = _with_claude_context(append_system_prompt, cwd)
    tmp_dir = tempfile.mkdtemp(prefix="cc-lark-pi-")
    prompt_file = ""
    if system_prompt:
        prompt_file = os.path.join(tmp_dir, "append_system_prompt.md")
        fd = os.open(prompt_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system_prompt)

    async def _run_once(session_file: Optional[str]) -> tuple[str, Optional[str], Optional[int], str]:
        cmd = [resolve_pi_bin(pi_bin), "--mode", "json", "--session-dir", sessions]
        if session_file:
            cmd += ["--session", session_file]
        if resolved_model:
            cmd += ["--model", resolved_model]
        if thinking:
            cmd += ["--thinking", thinking]
        if not trust_all:
            cmd += ["--tools", READ_ONLY_TOOLS]
        if prompt_file:
            cmd += ["--append-system-prompt", prompt_file]
        if mcp_cfg and os.path.isfile(EXTENSION_PATH):
            cmd += ["-e", EXTENSION_PATH]
        cmd += _skill_args(cwd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.path.expanduser("~"),
            env=env,
            # agent_end 会把本轮全部消息再吐一遍，长任务一行能到几 MB
            limit=256 * 1024 * 1024,
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
        last_text = ""
        new_session_id: Optional[str] = None
        text_started_in_message = False
        tool_args: dict[str, tuple[str, dict]] = {}
        last_started_tool = ""
        last_usage: dict = {}
        turn_tokens = 0
        turn_cost = 0.0
        error_msg = ""
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
                        f"pi 单轮执行超过 wall-clock 最终上限（{int(wall_clock_limit)}秒），已终止进程。"
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
                                f"pi 执行超时（{threshold}秒有子进程但 pi 端无任何新输出），已终止进程。"
                                f"常见原因：tail -f / watch / npm run dev 等永不退出的阻塞命令。"
                            )
                        raise RuntimeError(f"pi 执行超时（{threshold}秒无输出且无活跃子进程），已终止进程")
                    continue
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = data.get("type")

                if etype == "session":
                    new_session_id = data.get("id") or new_session_id

                elif etype == "message_start":
                    if (data.get("message") or {}).get("role") == "assistant":
                        text_started_in_message = False

                elif etype == "message_update":
                    ev = data.get("assistantMessageEvent") or {}
                    if ev.get("type") == "text_delta":
                        chunk = ev.get("delta") or ""
                        if not chunk:
                            continue
                        # 工具调用前后是两条 assistant 消息，中间补个空行，别把两段话粘在一起
                        if not text_started_in_message and full_text and not full_text.endswith("\n"):
                            chunk = "\n\n" + chunk
                        text_started_in_message = True
                        full_text += chunk
                        await _fire_callback(on_text_chunk, chunk)

                elif etype == "message_end":
                    msg = data.get("message") or {}
                    if msg.get("role") != "assistant":
                        continue
                    text = _message_text(msg).strip()
                    if text:
                        last_text = text
                    usage = msg.get("usage") or {}
                    if usage:
                        last_usage = _context_usage(usage)
                        turn_tokens += int(usage.get("totalTokens") or 0)
                        cost = (usage.get("cost") or {}).get("total")
                        if isinstance(cost, (int, float)):
                            turn_cost += float(cost)
                    stop = str(msg.get("stopReason") or "")
                    if stop in ("error", "aborted"):
                        error_msg = str(msg.get("errorMessage") or stop)
                    elif stop:
                        error_msg = ""

                elif etype == "tool_execution_start":
                    name = str(data.get("toolName") or "tool")
                    args = data.get("args") if isinstance(data.get("args"), dict) else {}
                    last_started_tool = str(data.get("toolCallId") or "")
                    tool_args[last_started_tool] = (name, args)
                    await _fire_callback(on_tool_use, _tool_name(name), dict(args))

                elif etype == "tool_execution_end":
                    tid = str(data.get("toolCallId") or "")
                    name, args = tool_args.pop(tid, (str(data.get("toolName") or "tool"), data.get("args") or {}))
                    # 卡片只会把「同一工具的后续状态」并进最后一行。pi 会并行跑工具，别的工具
                    # 已经起了一行时再报完成就成了重复的一行，所以只给最后起的那个补 ✅。
                    if tid == last_started_tool:
                        await _fire_callback(on_tool_use, _tool_name(name), _tool_done_input(name, args, data))

                elif etype == "auto_retry_start":
                    print(
                        f"[run_pi] 上游出错自动重试 {data.get('attempt')}/{data.get('maxAttempts')}: "
                        f"{str(data.get('errorMessage') or '')[:200]}",
                        flush=True,
                    )

                elif etype == "compaction_end" and data.get("errorMessage"):
                    print(f"[run_pi] 压缩上下文失败: {str(data.get('errorMessage'))[:200]}", flush=True)
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
            print(f"[run_pi] stderr: {stderr_text[-500:]}", flush=True)
        if session_file and not new_session_id and "No session found" in stderr_text:
            raise _ResumeFailed(stderr_text[-200:])

        if last_usage:
            usage_out = dict(last_usage)
            usage_out["_context_window"] = await context_window_for(resolved_model, pi_bin, env)
            if turn_tokens:
                usage_out["_turn_tokens"] = turn_tokens
            if turn_cost:
                usage_out["_turn_cost_usd"] = round(turn_cost, 6)
            await _fire_callback(on_usage, usage_out)

        if error_msg:
            exc = RuntimeError(f"pi 执行出错：{error_msg[:800]}")
            if new_session_id:
                exc.cc_session_id = new_session_id
                exc.cc_retryable_resume = not is_fatal_error_text(error_msg)
            raise exc
        return (last_text or full_text).strip(), new_session_id, proc.returncode, stderr_text

    used_fresh_session_fallback = False
    try:
        session_file = find_session_file(session_id, sessions) if session_id else None
        if session_id and not session_file:
            print(f"[run_pi] 会话文件不在了，开新会话; sid={session_id}", flush=True)
            used_fresh_session_fallback = True
        try:
            final_text, new_session_id, returncode, stderr_text = await _run_once(session_file)
        except _ResumeFailed as exc:
            print(f"[run_pi] resume failed, retrying with fresh session; sid={session_id} err={exc}", flush=True)
            final_text, new_session_id, returncode, stderr_text = await _run_once(None)
            used_fresh_session_fallback = True
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if returncode not in (0, None):
        if final_text:
            return final_text, new_session_id, used_fresh_session_fallback
        exc = RuntimeError(f"pi exited with code {returncode}: {stderr_text[-500:] or 'no stderr'}")
        if returncode > 0 and new_session_id and not is_fatal_error_text(stderr_text):
            exc.cc_session_id = new_session_id
            exc.cc_retryable_resume = True
        raise exc
    return final_text, new_session_id, used_fresh_session_fallback


def format_cost(usd: Optional[float]) -> str:
    if not isinstance(usd, (int, float)):
        return ""
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"
