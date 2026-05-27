"""
本地调用 Claude Code CLI 的统一入口。

两种后端：
  - "pty"   （默认）→ claude_pty.run_claude：在 PTY 里跑交互式 claude，结构化
                       事件靠 tail ~/.claude/projects/<cwd>/<sid>.jsonl 拿到。
                       不依赖 --print，可以保留 slash 命令 / 权限 UX / plan
                       mode 等交互式 Claude Code 的完整行为。
  - "print" → 本文件 _run_claude_print：经典 `claude --print --output-format
              stream-json` 子进程模式，按 token 级 delta 解析。

通过 CLAUDE_RUNNER 环境变量切换，默认 pty。

两种模式共享：
  - 复用 ~/.claude/ 中已有的 Max 订阅登录凭证
  - 相同的对外签名：run_claude(message, session_id, ...) →
        (full_text, new_session_id, used_fresh_session_fallback)
  - 相同的回调协议：on_text_chunk / on_tool_use / on_process_start / on_usage
"""

import asyncio
import json
import os
import subprocess as sp
from typing import Callable, Optional

from bot_config import PERMISSION_MODE, CLAUDE_CLI

# ── 后端选择 ──────────────────────────────────────────────────
# 默认走 PTY 后端；线上要回退到 -p 时设置 CLAUDE_RUNNER=print
_RUNNER_BACKEND = os.getenv("CLAUDE_RUNNER", "pty").strip().lower()

IDLE_TIMEOUT = 300  # 5 分钟无输出且无子进程 → 视为挂死
_CHECK_INTERVAL = 30  # 静默时每 30 秒检查一次子进程
# 有子进程但 Claude 端持续无新输出的"卡死"上限。
# 专治 tail -f / watch / npm run dev：子进程一直在但 Claude 端再也不产出。
# 留得比 IDLE_TIMEOUT 宽，避免误杀正常长编译/长安装。
STUCK_CHILD_TIMEOUT = 900  # 15 分钟
# 单轮 wall-clock 最终保险：无论是否还在产出，都会强杀，防 runaway loop。
WALL_CLOCK_LIMIT = 3600  # 60 分钟


def _has_children(pid: int) -> bool:
    """进程是否有活跃子进程（说明在跑 bash 命令、编译等）。"""
    try:
        result = sp.run(["pgrep", "-P", str(pid)], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _extract_text_content(value) -> str:
    """Extract final assistant text from Claude CLI result payload."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


async def _fire_callback(cb, *args):
    if cb is None:
        return
    if asyncio.iscoroutinefunction(cb):
        await cb(*args)
    else:
        cb(*args)


async def run_claude(
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
) -> tuple[str, Optional[str], bool]:
    """
    调用 Claude Code CLI 并流式解析输出。

    后端由 CLAUDE_RUNNER 环境变量决定：
        pty   (默认) → PTY + JSONL tail，保留交互式 Claude Code 完整体验
        print        → 经典 `claude --print` 子进程，按 token delta 解析

    Returns:
        (full_response_text, new_session_id, used_fresh_session_fallback)
    """
    # spawn 前按需切账户：当前账户烧穿时在这一拍就切到好账户，不必等 quota_watcher
    # 10min 轮询。内部带探测节流（默认 45s 一次）+ 冷却防抖，健康路径几乎零开销；
    # probe 走网络，丢到 executor 跑别堵事件循环。失败/未启用时静默 no-op。
    try:
        from account_switcher import maybe_switch_before_spawn
        await asyncio.get_event_loop().run_in_executor(None, maybe_switch_before_spawn)
    except Exception:
        pass

    if _RUNNER_BACKEND == "print":
        return await _run_claude_print(
            message=message,
            session_id=session_id,
            model=model,
            cwd=cwd,
            permission_mode=permission_mode,
            on_text_chunk=on_text_chunk,
            on_tool_use=on_tool_use,
            on_process_start=on_process_start,
            on_usage=on_usage,
            append_system_prompt=append_system_prompt,
        )
    # 默认 PTY 后端：延迟 import 避免 print-only 部署没装 termios 的奇怪环境出错
    from claude_pty import run_claude as _run_claude_pty
    return await _run_claude_pty(
        message=message,
        session_id=session_id,
        model=model,
        cwd=cwd,
        permission_mode=permission_mode,
        on_text_chunk=on_text_chunk,
        on_tool_use=on_tool_use,
        on_process_start=on_process_start,
        on_usage=on_usage,
        on_status=on_status,
        append_system_prompt=append_system_prompt,
    )


async def _run_claude_print(
    message: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    permission_mode: Optional[str] = None,
    on_text_chunk: Optional[Callable[[str], None]] = None,
    on_tool_use: Optional[Callable[[str, dict], None]] = None,
    on_process_start: Optional[Callable[[asyncio.subprocess.Process], None]] = None,
    on_usage: Optional[Callable[[dict], None]] = None,
    append_system_prompt: Optional[str] = None,
) -> tuple[str, Optional[str], bool]:
    """`claude --print --output-format stream-json` 模式（兼容/兜底后端）。"""

    async def _run_once(active_session_id: Optional[str]) -> tuple[str, Optional[str], int, str]:
        cmd = [
            CLAUDE_CLI,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", permission_mode or PERMISSION_MODE,
        ]
        if active_session_id:
            cmd += ["--resume", active_session_id]
        if model:
            cmd += ["--model", model]
        if append_system_prompt:
            cmd += ["--append-system-prompt", append_system_prompt]

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.path.expanduser("~"),
            env=env,
            limit=10 * 1024 * 1024,
        )

        await _fire_callback(on_process_start, proc)

        proc.stdin.write((message + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()

        full_text = ""
        new_session_id = None
        pending_tool_name = ""
        pending_tool_input_json = ""

        idle_seconds = 0
        loop = asyncio.get_event_loop()
        start_time = loop.time()

        try:
            while True:
                # 60 分钟 wall-clock 最终保险，防 runaway loop
                if loop.time() - start_time >= WALL_CLOCK_LIMIT:
                    proc.kill()
                    await proc.wait()
                    raise RuntimeError(
                        f"Claude 单轮执行超过 wall-clock 最终上限（{WALL_CLOCK_LIMIT}秒），已终止进程。"
                    )

                try:
                    raw_line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=_CHECK_INTERVAL
                    )
                    idle_seconds = 0  # 收到输出，重置计时
                except asyncio.TimeoutError:
                    idle_seconds += _CHECK_INTERVAL
                    has_kids = _has_children(proc.pid)
                    threshold = STUCK_CHILD_TIMEOUT if has_kids else IDLE_TIMEOUT
                    if idle_seconds >= threshold:
                        proc.kill()
                        await proc.wait()
                        if has_kids:
                            raise RuntimeError(
                                f"Claude 执行超时（{threshold}秒有子进程但 Claude 端无任何新输出），已终止进程。"
                                f"常见原因：tail -f / watch / npm run dev 等永不退出的阻塞命令。"
                            )
                        raise RuntimeError(
                            f"Claude 执行超时（{threshold}秒无输出且无活跃子进程），已终止进程"
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
                    sid = data.get("session_id")
                    if sid:
                        new_session_id = sid

                elif event_type == "stream_event":
                    evt = data.get("event", {})
                    evt_type = evt.get("type")

                    if evt_type == "content_block_delta":
                        delta = evt.get("delta", {})
                        delta_type = delta.get("type")

                        if delta_type == "text_delta":
                            chunk = delta.get("text", "")
                            if chunk:
                                full_text += chunk
                                await _fire_callback(on_text_chunk, chunk)

                        elif delta_type == "input_json_delta":
                            pending_tool_input_json += delta.get("partial_json", "")

                    elif evt_type == "content_block_start":
                        block = evt.get("content_block", {})
                        if block.get("type") == "tool_use":
                            pending_tool_name = block.get("name", "")
                            pending_tool_input_json = ""
                            await _fire_callback(on_tool_use, pending_tool_name, {})

                    elif evt_type == "content_block_stop":
                        if pending_tool_name and pending_tool_input_json:
                            try:
                                inp = json.loads(pending_tool_input_json)
                            except json.JSONDecodeError:
                                inp = {}
                            await _fire_callback(on_tool_use, pending_tool_name, inp)
                        pending_tool_name = ""
                        pending_tool_input_json = ""

                elif event_type == "result":
                    sid = data.get("session_id")
                    if sid:
                        new_session_id = sid
                    final_text = _extract_text_content(data.get("result", ""))
                    if final_text:
                        full_text = final_text
                    # usage 顶层是跨内部迭代的累加值，不反映上下文实际占用。
                    # iterations[-1] 才是最后一次内部调用的真实 prompt 大小，≈ 当前 context fill。
                    raw_usage = dict(data.get("usage") or {})
                    iterations = raw_usage.get("iterations") or []
                    if isinstance(iterations, list) and iterations:
                        last = iterations[-1]
                        if isinstance(last, dict):
                            usage = dict(last)
                        else:
                            usage = raw_usage
                    else:
                        usage = raw_usage
                    usage.pop("iterations", None)
                    # modelUsage 里带 contextWindow，优先用准确值
                    model_usage = data.get("modelUsage") or {}
                    if isinstance(model_usage, dict) and model_usage:
                        first = next(iter(model_usage.values()), {})
                        if isinstance(first, dict) and first.get("contextWindow"):
                            usage["_context_window"] = first["contextWindow"]
                    if usage:
                        await _fire_callback(on_usage, usage)

        except RuntimeError:
            raise

        stderr_output = await proc.stderr.read()
        await proc.wait()
        stderr_text = stderr_output.decode("utf-8", errors="replace").strip()
        return full_text.strip(), new_session_id, proc.returncode, stderr_text

    final_text, new_session_id, returncode, stderr_text = await _run_once(session_id)
    used_fresh_session_fallback = False

    # Claude 的 session 与 cwd 不兼容时，CLI 有时直接 code=1 且 stderr 为空。
    # 这种场景自动退回新 session，避免用户必须手动 /new。
    # returncode 为负数 = 被信号杀（如 /stop 的 SIGTERM/SIGKILL），不能 fallback——
    # 否则用户 /stop 后会立刻在 lock 内拉起新进程，造成"队列说在跑、/stop 杀不死"的死循环。
    if session_id and returncode is not None and returncode > 0 and not stderr_text and not final_text:
        print("[run_claude] resume failed without stderr, retrying with fresh session", flush=True)
        final_text, new_session_id, returncode, stderr_text = await _run_once(None)
        used_fresh_session_fallback = True

    if returncode != 0:
        detail = stderr_text or "no stderr"
        if final_text:
            detail += f" (partial output length={len(final_text)})"
        # 如果有部分输出，返回给用户看而不是抛异常
        if final_text:
            return final_text, new_session_id, used_fresh_session_fallback
        raise RuntimeError(f"claude exited with code {returncode}: {detail}")

    return final_text, new_session_id, used_fresh_session_fallback
