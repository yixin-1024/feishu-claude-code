"""本地调用 opencode CLI 的统一入口。

使用 `opencode run --format json`，接口对齐 claude_runner.run_claude /
codex_runner.run_codex，方便 dispatcher 按 profile 在 Claude/Codex/opencode 间分发。

opencode 的 JSON 事件（`--format json`，每行一个对象，均带 sessionID）：
  - step_start  : 一个 step 开始
  - text        : part.id / part.text（整段，可能随更新重发）→ 助手正文
  - tool_use    : part.tool / part.state.{status,input,output,metadata} → 工具调用
  - step_finish : part.tokens{input,output,reasoning,cache{read,write}} / part.cost → 用量

凭证：opencode 的 google provider 读环境变量 GOOGLE_GENERATIVE_AI_API_KEY；
本 runner 支持把 profile 里配置的 key 注入到子进程 env。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from typing import Any, Callable, Optional

IDLE_TIMEOUT = 3600
DEFAULT_API_KEY_ENV = "GOOGLE_GENERATIVE_AI_API_KEY"
DEFAULT_PROVIDER = "google"


def resolve_opencode_bin(configured: Optional[str] = None) -> str:
    if configured:
        return configured
    found = shutil.which("opencode")
    if found:
        return found
    return "opencode"


def _normalize_model(model: Optional[str], provider: Optional[str]) -> Optional[str]:
    """opencode 需要 provider/model 形式；裸 model 名按配置 provider 补前缀。"""
    m = (model or "").strip()
    if not m:
        return None
    if "/" in m:
        return m
    prov = (provider or DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER
    return f"{prov}/{m}"


def _context_window_for_model(model: Optional[str]) -> int:
    """opencode 模型的上下文窗口（用于 footer 占比）。dispatcher 默认值对
    gemini 会误判成 200k，所以这里显式给出。"""
    m = (model or "").lower()
    if "gemini" in m:
        return 1_048_576
    if "gpt-5" in m or "codex" in m:
        return 258_400
    if "claude" in m:
        return 200_000
    return 0


async def _fire_callback(cb, *args):
    if cb is None:
        return
    if asyncio.iscoroutinefunction(cb):
        await cb(*args)
    else:
        cb(*args)


def _normalize_usage(tokens: dict[str, Any], model: Optional[str]) -> dict[str, int]:
    """把 opencode step_finish 的 tokens 映射到 footer 字段名。"""
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    usage = {
        "input_tokens": int(tokens.get("input", 0) or 0),
        "output_tokens": int(tokens.get("output", 0) or 0),
    }
    cache_read = int(cache.get("read", 0) or 0)
    cache_write = int(cache.get("write", 0) or 0)
    if cache_read:
        usage["cache_read_input_tokens"] = cache_read
    if cache_write:
        usage["cache_creation_input_tokens"] = cache_write
    reasoning = int(tokens.get("reasoning", 0) or 0)
    if reasoning:
        usage["reasoning_output_tokens"] = reasoning
    window = _context_window_for_model(model)
    if window:
        usage["_context_window"] = window
    return usage


def _tool_from_part(part: dict[str, Any]) -> Optional[tuple[str, dict]]:
    name = str(part.get("tool") or "tool").strip() or "tool"
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    inp = state.get("input") if isinstance(state.get("input"), dict) else {}
    meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    command = inp.get("command")
    if not command:
        # 非 bash 工具：把 input 压成简短描述
        command = inp.get("description") or json.dumps(inp, ensure_ascii=False)[:500]
    payload = {
        "command": command,
        "output": state.get("output") or meta.get("output") or "",
        "exit_code": meta.get("exit"),
        "status": state.get("status") or "in_progress",
    }
    return name, payload


def _compose_text(text_parts: "dict[str, str]") -> str:
    parts = [t.strip() for t in text_parts.values() if isinstance(t, str) and t.strip()]
    return "\n\n".join(parts).strip()


async def run_opencode(
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
    opencode_bin: Optional[str] = None,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    api_key_env: Optional[str] = None,
    dangerously_skip_permissions: bool = True,
    idle_timeout_sec: int = IDLE_TIMEOUT,
) -> tuple[str, Optional[str], bool]:
    del permission_mode, on_status

    prompt = message
    if append_system_prompt:
        prompt = f"{append_system_prompt}\n\n{message}"

    resolved_model = _normalize_model(model, provider)

    cmd = [resolve_opencode_bin(opencode_bin), "run", "--format", "json"]
    # 把 opencode 的工作目录钉死在 cwd —— 否则它按 .git 向上探测 project root，
    # 相对路径的文件读写会跑到 sandbox 外（实测会落到 cc-lark 项目目录）。
    if cwd:
        cmd.extend(["--dir", cwd])
    if session_id:
        cmd.extend(["-s", session_id])
    if resolved_model:
        cmd.extend(["-m", resolved_model])
    if dangerously_skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd.append(prompt)

    child_env = dict(os.environ)
    if api_key:
        child_env[(api_key_env or DEFAULT_API_KEY_ENV)] = api_key

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or os.path.expanduser("~"),
        env=child_env,
        start_new_session=True,
        limit=10 * 1024 * 1024,
    )
    await _fire_callback(on_process_start, proc)

    stderr_task = asyncio.create_task(
        proc.stderr.read() if proc.stderr else asyncio.sleep(0, result=b"")
    )
    thread_id: Optional[str] = None
    text_parts: dict[str, str] = {}
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
                raise RuntimeError(
                    f"opencode 长时间无输出（>{idle_timeout_sec}s），进程已终止。"
                )

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

            sid = str(evt.get("sessionID") or "").strip()
            if sid and not thread_id:
                thread_id = sid

            etype = str(evt.get("type") or "").strip()
            part = evt.get("part") if isinstance(evt.get("part"), dict) else {}

            if etype == "text":
                pid = str(part.get("id") or "")
                text = part.get("text")
                if pid and isinstance(text, str):
                    text_parts[pid] = text
                    live_text = _compose_text(text_parts)
                    if live_text and live_text != last_emitted:
                        delta = (
                            live_text[len(last_emitted):]
                            if live_text.startswith(last_emitted)
                            else live_text
                        )
                        await _fire_callback(on_text_chunk, delta)
                        last_emitted = live_text
            elif etype == "tool_use":
                tool = _tool_from_part(part)
                if tool:
                    await _fire_callback(on_tool_use, tool[0], tool[1])
            elif etype == "step_finish":
                tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
                if tokens:
                    await _fire_callback(on_usage, _normalize_usage(tokens, resolved_model))
            elif "error" in etype.lower():
                # opencode 把错误也走 JSON 事件；记下原始行兜底
                pass
    finally:
        if proc.returncode is None:
            await proc.wait()

    stderr_raw = await stderr_task
    stderr_text = (
        stderr_raw.decode("utf-8", errors="replace").strip()
        if isinstance(stderr_raw, bytes)
        else ""
    )
    final_text = _compose_text(text_parts)
    if not final_text:
        merged = ("\n".join(stdout_lines) + "\n" + stderr_text).strip()
        final_text = merged[-3500:] if merged else "opencode 没有返回可展示内容。"
    if proc.returncode not in (0, None) and not _compose_text(text_parts):
        raise RuntimeError(stderr_text or final_text or f"opencode run exited with {proc.returncode}")
    return final_text, thread_id, False
