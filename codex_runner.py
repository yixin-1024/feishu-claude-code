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
from typing import Any, Callable, Optional

IDLE_TIMEOUT = 3600


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
) -> tuple[Optional[str], list[str], str, bool, Optional[tuple[str, dict]], Optional[dict]]:
    thread_id: Optional[str] = None
    changed = False
    tool: Optional[tuple[str, dict]] = None
    usage: Optional[dict] = None
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

    return thread_id, messages, current_agent_text, changed, tool, usage


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
    append_system_prompt: Optional[str] = None,
    codex_bin: Optional[str] = None,
    sandbox_mode: Optional[str] = None,
    approval_policy: Optional[str] = None,
    dangerous_bypass_level: int = 0,
    idle_timeout_sec: int = IDLE_TIMEOUT,
    extra_env: Optional[dict] = None,
) -> tuple[str, Optional[str], bool]:
    del permission_mode, on_status

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
            evt_thread_id, messages, current_agent_text, changed, tool, usage = _consume_exec_event(
                evt, messages, current_agent_text,
            )
            if evt_thread_id and not thread_id:
                thread_id = evt_thread_id
            if tool:
                await _fire_callback(on_tool_use, tool[0], tool[1])
            if usage:
                await _fire_callback(on_usage, usage)
            if changed:
                live_text = _compose_agent_text(messages, current_agent_text)
                if live_text and live_text != last_emitted:
                    delta = live_text[len(last_emitted):] if live_text.startswith(last_emitted) else live_text
                    await _fire_callback(on_text_chunk, delta)
                    last_emitted = live_text
    finally:
        if proc.returncode is None:
            await proc.wait()

    stderr_raw = await stderr_task
    stderr_text = stderr_raw.decode("utf-8", errors="replace").strip() if isinstance(stderr_raw, bytes) else ""
    final_text = _compose_agent_text(messages, current_agent_text)
    if not final_text:
        merged = ("\n".join(stdout_lines) + "\n" + stderr_text).strip()
        final_text = merged[-3500:] if merged else "Codex 没有返回可展示内容。"
    persisted_usage = _read_last_context_usage(thread_id)
    if persisted_usage:
        await _fire_callback(on_usage, persisted_usage)
    if proc.returncode not in (0, None) and not _compose_agent_text(messages, current_agent_text):
        raise RuntimeError(stderr_text or final_text or f"codex exec exited with {proc.returncode}")
    return final_text, thread_id, False
