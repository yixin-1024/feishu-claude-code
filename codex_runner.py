"""本地调用 Codex CLI 的统一入口。

使用 `codex exec --json`，接口对齐 claude_runner.run_claude，方便 dispatcher
按 profile 在 Claude/Codex 间分发。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import time
from typing import Any, Callable, Optional

IDLE_TIMEOUT = 3600

# codex `exec` 往 stderr 打的一批「无害噪音」——它们永远会出现、跟成败无关，绝不能被当成
# 报错冒充真因（真因如「用量耗尽」是走 stdout 的 JSON error 事件报的，见 _consume_exec_event）：
#   - "Reading additional input from stdin..." —— exec 模式的固定横幅；
#   - "codex_models_manager::" —— models 缓存过期告警，codex 会自动重拉自愈；
#   - "Shell cwd was reset to ..." —— 沙箱 cwd 复位提示。
_BENIGN_STDERR_PATTERNS = (
    "Reading additional input from stdin",
    "codex_models_manager::",
    "Shell cwd was reset to",
)


def _clean_stderr(text: str) -> str:
    """剥掉已知无害的 stderr 噪音行，避免它冒充成真正的错误被抛给用户。"""
    lines = [
        ln for ln in (text or "").splitlines()
        if ln.strip() and not any(p in ln for p in _BENIGN_STDERR_PATTERNS)
    ]
    return "\n".join(lines).strip()

# ── 自动续跑（codex 后端"做到一半就停"的治本） ──────────────────────
# codex `exec` 一次只跑一轮 agentic pass 就返回：它做完一段有界的活、吐一句
# "我正在… / 接下来…" 然后进程退出，turn 结束。对多步/长任务，用户看到的就是
# "永远做到一半、要手动催『继续』"。这里在 runner 内部加一个自动续跑循环：给模型
# 一个只在【整件事真正全部完成】时才输出的完成标记；只要这一轮 pass 没吐标记，就
# resume 同一 session 自动催它继续，直到吐标记 / 触顶（轮数或墙钟预算）。标记本身
# 从流式与最终文本里剥掉，用户看不到。
_DONE_SENTINEL = "⟦CC_TASK_DONE⟧"

_CONTINUE_SYSTEM_HINT = (
    "【运行环境：自动续跑】你运行在一个会自动让你续跑的环境里——你这一轮回复结束后，"
    "只要任务还没真正全部完成，系统会自动把你唤醒继续，无需用户催促。因此：任务未完成时"
    "不要停下等待、不要只汇报进度或计划就收尾、不要问『要我继续吗』；请持续推进直到交付"
    "全部要求的产物。仅当【整件任务确实已全部完成】时，在你最终消息的最后单独一行原样输出"
    "完成标记：{sentinel}。任务尚未全部完成时，绝对不要输出该标记。"
    "\n⚠️ 既然环境已经会自动续跑你，就【不要】为了『回来接着干自己没干完的活』或『稍后回来自检/复核』"
    "去调 wake_me_in / schedule_cron 给自己排唤醒——那是多余的，会在任务早已干完后 fire 出"
    "『该自动唤醒已过期』的噪音。wake_me_in 只在你必须等一个【真实墙钟事件】（等 CI 跑完、等部署、"
    "等限流恢复、或用户明确要的定时提醒）时才用；『继续推进本任务』一律靠本轮内的自动续跑，别排 wake。"
    "\n(Environment auto-continues you: keep working across turns until everything is truly "
    "done; do NOT stop to report progress or ask to continue. Emit the exact marker "
    "{sentinel} on its own final line ONLY when the whole task is fully complete. Since you are "
    "auto-continued, do NOT call wake_me_in/schedule_cron just to resume your own unfinished work "
    "or self-check later — use wake_me_in only to await a real wall-clock event (CI, deploy, "
    "rate-limit recovery, or an explicit timed reminder).)"
)

_CONTINUE_NUDGE = (
    "继续未完成的工作，直到全部要求的产物都交付完毕。若已全部完成，在最后单独一行输出 "
    "{sentinel}；若还没完成，就继续推进、不要输出该标记，也不要只汇报进度/计划就停下。"
)


def _auto_continue_enabled(extra_env: Optional[dict]) -> bool:
    """全局 CODEX_AUTO_CONTINUE（默认开）；<PROFILE>_CODEX_AUTO_CONTINUE 可 per-profile 覆盖。"""
    def _truthy(v: str) -> bool:
        return (v or "").strip().lower() not in ("0", "false", "no", "off")

    profile = ((extra_env or {}).get("CC_LARK_PROFILE") or "").strip().upper()
    if profile:
        override = os.getenv(f"{profile}_CODEX_AUTO_CONTINUE")
        if override is not None:
            return _truthy(override)
    return _truthy(os.getenv("CODEX_AUTO_CONTINUE", "1"))


# API 侧 reasoning.effort 接受的取值（sol 额外支持 ultra；-m 指定模型时由服务端校验）。
_VALID_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


def _reasoning_effort(
    extra_env: Optional[dict],
    explicit: Optional[str] = None,
) -> Optional[str]:
    """解析 codex reasoning effort：会话显式覆盖优先，其次 profile/global env。

    环境默认顺序为 <PROFILE>_CODEX_REASONING_EFFORT 优先，
    退回全局 CODEX_REASONING_EFFORT；未设则返回 None（不注入，交给 config.toml/默认）。

    走 `-c model_reasoning_effort=` 而非改全局 ~/.codex/config.toml —— 后者会被 Codex
    桌面 app 存盘时整份覆盖回默认档（实测 sol 被打回 low）。-c 覆盖每次 spawn 显式生效、
    不落盘、不受桌面 app 干扰（已实测：-c 的值压过 config.toml 的值直达 API）。
    """
    if explicit is not None:
        value = str(explicit).strip().lower()
        if value:
            if value not in _VALID_EFFORTS:
                raise ValueError(
                    f"invalid Codex reasoning effort {explicit!r}; "
                    f"expected one of {sorted(_VALID_EFFORTS)}"
                )
            return value

    profile = ((extra_env or {}).get("CC_LARK_PROFILE") or "").strip().upper()
    raw = None
    if profile:
        raw = os.getenv(f"{profile}_CODEX_REASONING_EFFORT")
    if raw is None:
        raw = os.getenv("CODEX_REASONING_EFFORT")
    if raw is None:
        return None
    val = raw.strip().lower()
    if not val:
        return None
    if val not in _VALID_EFFORTS:
        print(
            f"[codex_runner] 忽略非法 reasoning effort={raw!r}（可选 {sorted(_VALID_EFFORTS)}）",
            file=sys.stderr,
        )
        return None
    return val


def _max_continue_passes() -> int:
    try:
        return max(0, int(os.getenv("CODEX_MAX_CONTINUE", "8")))
    except ValueError:
        return 8


def _continue_budget_sec() -> float:
    try:
        return max(0.0, float(os.getenv("CODEX_CONTINUE_BUDGET_SEC", "2700")))
    except ValueError:
        return 2700.0


def resolve_codex_bin(configured: Optional[str] = None) -> str:
    if configured:
        return configured
    found = shutil.which("codex")
    if found:
        return found
    app_path = "/Applications/Codex.app/Contents/Resources/codex"
    if os.path.exists(app_path):
        return app_path
    return "codex"


def _to_toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _to_toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_to_toml_string(v) for v in values) + "]"


def _cc_lark_mcp_config_flags(extra_env: Optional[dict]) -> list[str]:
    """Build per-spawn Codex config overrides for the cc-lark runtime MCP server."""
    if not (extra_env or {}).get("CC_LARK_THREAD_ID"):
        return []
    if os.getenv("CC_LARK_WAKE_MCP", "1") == "0":
        return []

    cc_server = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cc_mcp_server.py")
    if not os.path.isfile(cc_server):
        return []

    cc_env = {
        k: str(v) for k, v in (extra_env or {}).items()
        if k.startswith("CC_LARK_")
        and k != "CC_LARK_CONTROL_TOKEN"
        and v is not None
    }
    # 能力闸门支持 per-profile 覆盖（<PROFILE>_<FLAG> 优先于全局 <FLAG>）。
    from bot_config import resolve_cc_lark_gates
    cc_env.update(resolve_cc_lark_gates((extra_env or {}).get("CC_LARK_PROFILE") or ""))

    config = [
        ("mcp_servers.cc-lark.command", _to_toml_string(sys.executable)),
        ("mcp_servers.cc-lark.args", _to_toml_array([cc_server])),
        ("mcp_servers.cc-lark.startup_timeout_sec", "30"),
    ]
    for key in sorted(cc_env):
        config.append((f"mcp_servers.cc-lark.env.{key}", _to_toml_string(cc_env[key])))

    flags: list[str] = []
    for key, value in config:
        flags.extend(["-c", f"{key}={value}"])
    return flags


async def _fire_callback(cb, *args):
    if cb is None:
        return
    if asyncio.iscoroutinefunction(cb):
        await cb(*args)
    else:
        cb(*args)


def _extract_text_fragment(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_extract_text_fragment(x) for x in node)
    if isinstance(node, dict):
        for key in ("text", "delta", "text_delta", "content", "message", "output_text"):
            if key in node:
                value = _extract_text_fragment(node.get(key))
                if value:
                    return value
        return "".join(_extract_text_fragment(v) for v in node.values())
    return ""


def _compose_agent_text(messages: list[str], current_agent_text: str) -> str:
    parts = [m.strip() for m in messages if isinstance(m, str) and m.strip()]
    if current_agent_text.strip():
        parts.append(current_agent_text.strip())
    return "\n\n".join(parts).strip()


def _consume_exec_event(
    evt: dict[str, Any],
    messages: list[str],
    current_agent_text: str,
) -> tuple[Optional[str], list[str], str, bool, Optional[tuple[str, dict]], Optional[dict], Optional[str]]:
    thread_id: Optional[str] = None
    changed = False
    tool: Optional[tuple[str, dict]] = None
    usage: Optional[dict] = None
    error_msg: Optional[str] = None
    event_type = str(evt.get("type") or "").strip().lower()

    if event_type == "thread.started":
        thread_id = str(evt.get("thread_id") or "").strip() or None
        if not thread_id and isinstance(evt.get("thread"), dict):
            thread_id = str(evt["thread"].get("id") or "").strip() or None

    if isinstance(evt.get("usage"), dict):
        usage = _normalize_usage(evt["usage"])

    item = evt.get("item") if isinstance(evt.get("item"), dict) else {}
    item_type = str(item.get("type") or "").strip().lower()
    is_agent_item = item_type in ("agent_message", "assistant_message")
    if item_type in ("command_execution", "exec_command"):
        command = str(item.get("command") or "").strip()
        tool = ("bash", {
            "command": command,
            "output": item.get("aggregated_output") or item.get("output") or "",
            "exit_code": item.get("exit_code"),
            "status": item.get("status") or (
                "completed" if event_type == "item.completed" else "in_progress"
            ),
        })
    elif item_type in ("tool_call", "function_call"):
        name = str(item.get("name") or item.get("tool_name") or item_type)
        tool = (name, item)

    if event_type in ("item.delta", "response.output_text.delta", "assistant_message.delta", "message.delta"):
        delta = (
            _extract_text_fragment(evt.get("delta"))
            or _extract_text_fragment(evt.get("text_delta"))
            or _extract_text_fragment(evt.get("text"))
            or _extract_text_fragment(item.get("delta"))
            or _extract_text_fragment(item.get("text_delta"))
        )
        if delta:
            if not current_agent_text:
                current_agent_text = delta
            elif delta.startswith(current_agent_text):
                current_agent_text = delta
            elif not current_agent_text.endswith(delta):
                current_agent_text += delta
            changed = True

    if event_type in ("item.updated", "item.completed") and is_agent_item:
        full_text = (
            _extract_text_fragment(item.get("text"))
            or _extract_text_fragment(item.get("content"))
            or _extract_text_fragment(item.get("message"))
        ).strip()
        if full_text:
            current_agent_text = full_text
            changed = True
        if event_type == "item.completed" and current_agent_text.strip():
            finalized = current_agent_text.strip()
            if not messages or messages[-1] != finalized:
                messages.append(finalized)
                changed = True
            current_agent_text = ""

    if event_type in ("turn.completed", "response.completed", "thread.completed"):
        fallback_text = (
            _extract_text_fragment(evt.get("output_text"))
            or _extract_text_fragment(evt.get("text"))
        ).strip()
        if fallback_text and (not messages or messages[-1] != fallback_text):
            messages.append(fallback_text)
            changed = True
        if current_agent_text.strip():
            finalized = current_agent_text.strip()
            if not messages or messages[-1] != finalized:
                messages.append(finalized)
                changed = True
            current_agent_text = ""

    # codex 把「用量耗尽 / API 报错 / 限流」等真正的失败作为 stdout JSON 事件报出来
    # （type:"error" 顶层 message，或 type:"turn.failed" 的 error.message），而不是走 stderr。
    # 这里把它抠出来，交给 _run_codex_once 在没有可展示文本时顶上去当真因。
    if event_type == "error":
        error_msg = str(evt.get("message") or "").strip() or None
        if not error_msg and isinstance(evt.get("error"), dict):
            error_msg = str(evt["error"].get("message") or "").strip() or None
    elif event_type == "turn.failed":
        err = evt.get("error")
        if isinstance(err, dict):
            error_msg = str(err.get("message") or "").strip() or None
        elif err:
            error_msg = str(err).strip() or None

    return thread_id, messages, current_agent_text, changed, tool, usage, error_msg


def _normalize_usage(raw: dict[str, Any]) -> dict[str, int]:
    """Map Codex JSON usage fields onto footer field names.

    Codex reports cached tokens as part of input_tokens. For context occupancy,
    count input_tokens once and keep cached_input_tokens only as metadata.
    """
    input_tokens = int(raw.get("input_tokens", 0) or 0)
    cached = int(raw.get("cached_input_tokens", 0) or 0)
    output_tokens = int(raw.get("output_tokens", 0) or 0)
    reasoning = int(raw.get("reasoning_output_tokens", 0) or 0)

    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cached:
        usage["_cached_input_tokens"] = cached
    if reasoning:
        usage["reasoning_output_tokens"] = reasoning
    return usage


def _find_codex_session_file(thread_id: Optional[str]) -> str:
    if not thread_id:
        return ""
    root = os.path.expanduser("~/.codex/sessions")
    if not os.path.isdir(root):
        return ""
    suffix = f"{thread_id}.jsonl"
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(suffix):
                return os.path.join(dirpath, name)
    return ""


def _read_last_context_usage(thread_id: Optional[str]) -> dict[str, int]:
    """Read Codex last-turn context usage from the persisted session file."""
    fpath = _find_codex_session_file(thread_id)
    if not fpath:
        return {}
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return {}
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        payload = row.get("payload") or {}
        if row.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        usage = _normalize_usage(info.get("last_token_usage") or {})
        window = int(info.get("model_context_window") or 0)
        if window:
            usage["_context_window"] = window
        return usage
    return {}


async def _run_codex_once(
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
    codex_bin: Optional[str] = None,
    sandbox_mode: Optional[str] = None,
    approval_policy: Optional[str] = None,
    dangerous_bypass_level: int = 0,
    idle_timeout_sec: int = IDLE_TIMEOUT,
    extra_env: Optional[dict] = None,
    sentinel: str = "",
    reasoning_effort: Optional[str] = None,
) -> tuple[str, Optional[str], bool]:
    """跑一次 codex exec pass。返回 (final_text, thread_id, saw_sentinel)。

    saw_sentinel: 本轮输出里是否出现了 `sentinel`（=模型宣告整件任务已全部完成）。
    sentinel 会从流式增量与最终文本里剥掉，用户/卡片都看不到它。"""
    del permission_mode, on_status
    saw_sentinel = False

    def _strip_sentinel(s: str) -> str:
        return s.replace(sentinel, "") if sentinel and s else s

    prompt = message
    if append_system_prompt:
        prompt = f"{append_system_prompt}\n\n{message}"

    global_flags = [
        "-s", sandbox_mode or "danger-full-access",
        "-a", approval_policy or "never",
    ]
    config_flags: list[str] = []
    dangerous_bypass_level = max(0, min(2, int(dangerous_bypass_level or 0)))
    if dangerous_bypass_level == 1:
        config_flags.extend(["-c", f"approval_policy={_to_toml_string(approval_policy or 'never')}"])
    config_flags.extend(_cc_lark_mcp_config_flags(extra_env))
    effort = _reasoning_effort(extra_env, reasoning_effort)
    if effort:
        # 裸值形式（已实测 `-c model_reasoning_effort=ultra` 直达 API 且压过 config.toml）。
        config_flags.extend(["-c", f"model_reasoning_effort={effort}"])

    exec_flags = ["--json", "--skip-git-repo-check"]
    if model:
        exec_flags.extend(["-m", model])
    if dangerous_bypass_level >= 2:
        exec_flags.append("--dangerously-bypass-approvals-and-sandbox")

    cmd = [resolve_codex_bin(codex_bin), *global_flags, "exec"]
    if session_id:
        cmd.append("resume")
    cmd.extend(config_flags)
    cmd.extend(exec_flags)
    if session_id:
        cmd.append(session_id)
    cmd.append(prompt)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or os.path.expanduser("~"),
        start_new_session=True,
        limit=10 * 1024 * 1024,
    )
    await _fire_callback(on_process_start, proc)

    stderr_task = asyncio.create_task(proc.stderr.read() if proc.stderr else asyncio.sleep(0, result=b""))
    thread_id: Optional[str] = None
    messages: list[str] = []
    current_agent_text = ""
    last_emitted = ""
    stdout_lines: list[str] = []
    codex_error: Optional[str] = None
    idle_timeout_sec = max(0, int(idle_timeout_sec or 0))

    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=idle_timeout_sec or None,
                )
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    proc.terminate()
                await proc.wait()
                raise RuntimeError(f"Codex 长时间无输出（>{idle_timeout_sec}s），进程已终止。")

            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            stdout_lines.append(line)
            if not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt_thread_id, messages, current_agent_text, changed, tool, usage, evt_error = _consume_exec_event(
                evt, messages, current_agent_text,
            )
            if evt_thread_id and not thread_id:
                thread_id = evt_thread_id
            if evt_error and not codex_error:
                # 保留最先报出的那条（type:"error" 往往带完整信息，如「X 时恢复」）；
                # 后续 turn.failed 通常只是更简短的复述，不覆盖。
                codex_error = evt_error
            if tool:
                await _fire_callback(on_tool_use, tool[0], tool[1])
            if usage:
                await _fire_callback(on_usage, usage)
            if changed:
                live_text = _compose_agent_text(messages, current_agent_text)
                if sentinel and sentinel in live_text:
                    saw_sentinel = True
                    live_text = _strip_sentinel(live_text)
                if live_text and live_text != last_emitted:
                    delta = live_text[len(last_emitted):] if live_text.startswith(last_emitted) else live_text
                    await _fire_callback(on_text_chunk, delta)
                    last_emitted = live_text
    finally:
        if proc.returncode is None:
            await proc.wait()

    stderr_raw = await stderr_task
    stderr_text = _clean_stderr(stderr_raw.decode("utf-8", errors="replace")) if isinstance(stderr_raw, bytes) else ""
    final_text = _compose_agent_text(messages, current_agent_text)
    if sentinel and sentinel in final_text:
        saw_sentinel = True
        final_text = _strip_sentinel(final_text).strip()
    if not final_text and codex_error:
        # 没有任何可展示文本时，把 codex 报的真因（如「用量耗尽，X 时恢复」）顶上来，
        # 而不是让那条无害的 stdin 横幅冒充报错。
        final_text = codex_error
    if not final_text:
        merged = ("\n".join(stdout_lines) + "\n" + stderr_text).strip()
        final_text = merged[-3500:] if merged else "Codex 没有返回可展示内容。"
    persisted_usage = _read_last_context_usage(thread_id)
    if persisted_usage:
        await _fire_callback(on_usage, persisted_usage)
    # 有真因（codex_error）或非零退出、且没有可展示文本时，抛出真因。codex 命中用量限制时
    # 常以 returncode=0 退出，所以不能只靠退出码判断——否则真因会被静默吞掉。
    if not _compose_agent_text(messages, current_agent_text) and (
        codex_error or proc.returncode not in (0, None)
    ):
        raise RuntimeError(
            codex_error or stderr_text or final_text or f"codex exec exited with {proc.returncode}"
        )
    return final_text, thread_id, saw_sentinel


async def run_codex(
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
    should_stop: Optional[Callable[[], bool]] = None,
    append_system_prompt: Optional[str] = None,
    codex_bin: Optional[str] = None,
    sandbox_mode: Optional[str] = None,
    approval_policy: Optional[str] = None,
    dangerous_bypass_level: int = 0,
    idle_timeout_sec: int = IDLE_TIMEOUT,
    extra_env: Optional[dict] = None,
    reasoning_effort: Optional[str] = None,
) -> tuple[str, Optional[str], bool]:
    """codex 后端入口。默认在内部自动续跑，直到模型宣告整件任务完成或触顶。

    单轮 `codex exec` 只跑一段有界的活就返回（"做到一半"的根因）。这里循环 resume
    同一 session 自动催它继续，直到：① 模型输出完成标记；② 轮数达到 CODEX_MAX_CONTINUE；
    ③ 累计墙钟超过 CODEX_CONTINUE_BUDGET_SEC。关掉自动续跑（CODEX_AUTO_CONTINUE=0 或
    <PROFILE>_CODEX_AUTO_CONTINUE=0）时退回单轮老行为。流式回调、session、usage 逐轮透传，
    卡片持续更新；最终返回最后一轮的干净文本（已剥完成标记）。"""
    # 记住每一轮 codex 进程句柄：若某轮被外部信号打断（/stop、/restart 通过
    # attach_process 对 proc 组发 SIGTERM/SIGKILL → returncode 变负），必须立刻停止
    # 续跑循环，否则会在用户已叫停后又 spawn 新的 codex pass。
    _last_proc: dict[str, Any] = {"p": None}

    async def _track_start(proc):
        _last_proc["p"] = proc
        await _fire_callback(on_process_start, proc)

    def _aborted_by_signal() -> bool:
        p = _last_proc.get("p")
        rc = getattr(p, "returncode", None)
        return isinstance(rc, int) and rc < 0

    def _stop_requested() -> bool:
        if _aborted_by_signal():
            return True
        if should_stop is None:
            return False
        try:
            return bool(should_stop())
        except Exception:
            # 外部取消探针不应让 runner 自身崩溃；进程信号仍是第二道兜底。
            return False

    common = dict(
        model=model, reasoning_effort=reasoning_effort,
        cwd=cwd, permission_mode=permission_mode,
        on_text_chunk=on_text_chunk, on_tool_use=on_tool_use,
        on_process_start=_track_start, on_usage=on_usage, on_status=on_status,
        codex_bin=codex_bin, sandbox_mode=sandbox_mode, approval_policy=approval_policy,
        dangerous_bypass_level=dangerous_bypass_level, idle_timeout_sec=idle_timeout_sec,
        extra_env=extra_env,
    )

    if _stop_requested():
        return "", session_id, False

    if not _auto_continue_enabled(extra_env):
        return await _run_codex_once(
            message, session_id=session_id, append_system_prompt=append_system_prompt,
            sentinel="", **common,
        )

    sentinel = _DONE_SENTINEL
    max_passes = _max_continue_passes()
    budget = _continue_budget_sec()
    hint = _CONTINUE_SYSTEM_HINT.format(sentinel=sentinel)
    first_sys = f"{append_system_prompt}\n\n{hint}".strip() if append_system_prompt else hint

    started = time.monotonic()
    sess = session_id
    thread_out: Optional[str] = None
    final_text = ""

    for pass_idx in range(max_passes + 1):
        # /stop 可能恰好落在两个 codex pass 之间，此时上一进程已经 rc=0，
        # 不能只靠负 returncode 推断取消；必须在产生任何下一轮内容前读显式状态。
        if _stop_requested():
            break
        if pass_idx == 0:
            prompt, sysp = message, first_sys
        else:
            prompt, sysp = _CONTINUE_NUDGE.format(sentinel=sentinel), None
            # 轮次之间打一条带编号的分界符，让用户在卡片上一眼看出：这是【本轮内的
            # 自动续跑循环】把同一任务推进到第 N 轮（同一 session resume），而不是
            # 外层重复触发的新任务。pass_idx=0 是首轮，pass_idx≥1 即第 (pass_idx+1) 轮。
            # （区别于 dispatcher 的「🔄 上游响应中断」——那是流被打断的错误恢复。）
            await _fire_callback(
                on_text_chunk,
                f"\n\n━━━━━━━ 🔁 自动续跑 · 第 {pass_idx + 1} 轮 ━━━━━━━\n\n",
            )
            # 上面的异步回调会让出事件循环；若 /stop 正好在此期间到达，
            # 不得继续 spawn 新进程。
            if _stop_requested():
                break

        if _stop_requested():
            break
        final_text, tid, done = await _run_codex_once(
            prompt, session_id=sess, append_system_prompt=sysp, sentinel=sentinel, **common,
        )
        if tid:
            thread_out = thread_out or tid
            sess = sess or tid  # 首轮拿到 session 后，后续 resume 同一条

        if done or _stop_requested() or time.monotonic() - started >= budget:
            break

    return final_text, thread_out, False
