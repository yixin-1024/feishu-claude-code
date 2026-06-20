"""本地调用 MiMo Code（小米 `mimo` CLI）的统一入口。

MiMo Code 是 fork 自 OpenCode 的二次开发产物，CLI 与事件格式与 opencode 高度同构：
    mimo run --format json --dir <cwd> [-s <session>] -m <provider/model>
             [--variant <effort>] [--dangerously-skip-permissions] <prompt>

JSON 事件（`--format json`，每行一个对象，均带 sessionID）：
  - step_start  : 一个 step 开始
  - text        : part.id / part.text（整段，可能随更新重发）→ 助手正文
  - tool_use    : part.tool / part.state.{status,input,output,metadata} → 工具调用
  - step_finish : part.tokens{input,output,reasoning,cache{read,write}} / part.cost → 用量

会话：mimo session 落在 sqlite（~/.local/share/mimocode/mimocode.db），通过 `-s <ses_...>`
续接。本 runner 首轮不带 -s，从事件里抓 sessionID 返回给 dispatcher 持久化，下一轮回传。

模型供应商：mimo 沿用 opencode 的配置格式，自定义 OpenAI 兼容 provider 写在
~/.config/mimocode/mimocode.json 的 `provider` 字段。本 runner 在 mimo_base_url +
mimo_api_key 都给定时，会幂等地把该 provider 合并进配置（已存在则不动），方便部署复现。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from typing import Any, Callable, Optional

IDLE_TIMEOUT = 600  # mimo 首轮要做 sqlite 迁移 + 中转延迟，给比 opencode 更宽的无输出窗口
DEFAULT_PROVIDER = "quotio"
CONFIG_PATH = os.path.expanduser("~/.config/mimocode/mimocode.json")


def resolve_mimo_bin(configured: Optional[str] = None) -> str:
    if configured:
        return configured
    found = shutil.which("mimo")
    if found:
        return found
    home_bin = os.path.expanduser("~/.mimocode/bin/mimo")
    if os.path.exists(home_bin):
        return home_bin
    return "mimo"


def _normalize_model(model: Optional[str], provider: Optional[str]) -> Optional[str]:
    """mimo 需要 provider/model 形式；裸 model 名按配置 provider 补前缀。"""
    m = (model or "").strip()
    if not m:
        return None
    if "/" in m:
        return m
    prov = (provider or DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER
    return f"{prov}/{m}"


def _context_window_for_model(model: Optional[str]) -> int:
    """模型上下文窗口（footer 占比用）。"""
    m = (model or "").lower()
    if "opus-4-8" in m or "opus-4-7" in m or "opus-4-6" in m:
        return 1_000_000  # Claude Opus 4.x 1M 上下文
    if "claude" in m:
        return 200_000
    if "gpt-5" in m:
        return 400_000
    if "mimo" in m:
        return 1_048_576  # MiMo 自家模型 1M
    return 0


def ensure_provider_config(
    provider: str,
    base_url: str,
    api_key: str,
    models: Optional[list[str]] = None,
    config_path: str = CONFIG_PATH,
) -> bool:
    """幂等地把一个 OpenAI 兼容 provider 合并进 mimocode.json。

    已存在同名 provider 则不动（不覆盖用户手改的配置）。返回是否发生写入。
    解析失败（例如文件是带注释的 jsonc）时安全跳过，绝不 clobber。
    """
    provider = (provider or "").strip()
    if not provider or not base_url or not api_key:
        return False
    data: dict[str, Any] = {"$schema": "https://opencode.ai/config.json"}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            # 文件存在但解析不了（jsonc / 损坏）→ 不动它，假定用户已自行配置
            return False
    providers = data.setdefault("provider", {})
    if not isinstance(providers, dict):
        return False
    if provider in providers:
        return False  # 已配置，尊重现有
    model_ids = models or []
    providers[provider] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": provider,
        "options": {"baseURL": base_url, "apiKey": api_key},
        "models": {mid: {"name": mid} for mid in model_ids},
    }
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    tmp = f"{config_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path)
    return True


async def _fire_callback(cb, *args):
    if cb is None:
        return
    if asyncio.iscoroutinefunction(cb):
        await cb(*args)
    else:
        cb(*args)


def _normalize_usage(tokens: dict[str, Any], model: Optional[str]) -> dict[str, int]:
    """把 mimo step_finish 的 tokens 映射到 footer 字段名。"""
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


async def run_mimo(
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
    mimo_bin: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    variant: Optional[str] = None,
    dangerously_skip_permissions: bool = True,
    idle_timeout_sec: int = IDLE_TIMEOUT,
) -> tuple[str, Optional[str], bool]:
    del permission_mode, on_status

    prompt = message
    if append_system_prompt:
        prompt = f"{append_system_prompt}\n\n{message}"

    resolved_provider = (provider or DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER
    resolved_model = _normalize_model(model, resolved_provider)

    # 幂等保证 provider 配置存在（仅在 base_url + api_key 都给定时）。
    if base_url and api_key:
        bare = (resolved_model or "").split("/", 1)[-1] if resolved_model else ""
        try:
            ensure_provider_config(
                resolved_provider, base_url, api_key,
                models=[bare] if bare else None,
            )
        except OSError:
            pass

    cmd = [resolve_mimo_bin(mimo_bin), "run", "--format", "json"]
    # 把工作目录钉死在 cwd —— 否则按 .git 向上探测 project root，相对路径文件读写会跑偏。
    if cwd:
        cmd.extend(["--dir", cwd])
    if session_id:
        cmd.extend(["-s", session_id])
    if resolved_model:
        cmd.extend(["-m", resolved_model])
    if variant:
        cmd.extend(["--variant", variant])
    if dangerously_skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd.append(prompt)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or os.path.expanduser("~"),
        env=dict(os.environ),
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
                    f"mimo 长时间无输出（>{idle_timeout_sec}s），进程已终止。"
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
        final_text = merged[-3500:] if merged else "mimo 没有返回可展示内容。"
    if proc.returncode not in (0, None) and not _compose_text(text_parts):
        raise RuntimeError(stderr_text or final_text or f"mimo run exited with {proc.returncode}")
    return final_text, thread_id, False
