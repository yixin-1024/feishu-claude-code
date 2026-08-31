"""本地调用 xAI Grok CLI（Grok Build）的统一入口。

    grok --prompt-file <f> --output-format streaming-messages-json
         --include-partial-messages --permission-mode bypassPermissions
         [--session-id <uuid> | --resume <uuid>] [-m <model>]
         [--reasoning-effort <effort>] [--rules <system prompt>]

为什么它是最省事的一个后端：`streaming-messages-json` 就是 Anthropic Messages
stream-json 线格式，事件形状与 `claude --print --output-format stream-json`
完全一致（system/init → stream_event/content_block_delta → assistant → result），
所以解析逻辑与 claude_runner._run_claude_print 同构。

会话：grok 的 session 落在 $GROK_HOME/sessions（默认 ~/.grok/sessions），且
session id **稳定**——首轮我们自己生成 UUID 用 `--session-id` 钉死，续轮
`--resume <同一 id>`，不像 Claude CLI 每轮换 id。

模型：grok 只是 harness，模型可换。自定义 OpenAI 兼容端点写在
~/.grok/config.toml 的 `[model.<name>]`（base_url + env_key），本 runner 传的
`model` 就是那个 `<name>`。凭证按 grok_api_key_env 注入子进程环境，由
config.toml 的 `env_key` 取用。

MCP：grok 的 MCP 子进程**继承父进程 env**（已实测），所以 cc_mcp_server 的
per-turn CC_LARK_* 直接放在本进程 env 里即可，不需要像 codex 那样每轮改写配置。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from typing import Callable, Optional

from bot_config import PERMISSION_MODE, resolve_claude_wall_clock_limit
from claude_runner import (
    _extract_text_content,
    _fire_callback,
    _has_children,
    is_fatal_error_text,
)

IDLE_TIMEOUT = 300  # 无输出且无子进程 → 视为挂死
STUCK_CHILD_TIMEOUT = 900  # 有子进程但 grok 端持续无输出（tail -f / npm run dev 类）
_CHECK_INTERVAL = 30

# grok 的 --reasoning-effort 档位（比 Claude 多 none / minimal 两档）
GROK_EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

GROK_CONFIG_PATH = os.path.expanduser("~/.grok/config.toml")

# 默认禁掉 grok 自己的 subagent：它们活在本 turn 的进程组里，turn 一结束就被
# killpg，"派个子 agent 待会收结果"必然落空。跨 turn 的并行派活走 dispatch_task。
DEFAULT_DISALLOWED_TOOLS = "Agent"

# grok 认的权限模式；其余值一律落到 bypassPermissions（bot 场景无人值守）
GROK_PERMISSION_MODES = (
    "default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan",
)


def resolve_grok_bin(configured: Optional[str] = None) -> str:
    if configured:
        return configured
    found = shutil.which("grok")
    if found:
        return found
    home_bin = os.path.expanduser("~/.grok/bin/grok")
    if os.path.exists(home_bin):
        return home_bin
    return "grok"


def ensure_cc_lark_mcp(config_path: str = GROK_CONFIG_PATH) -> bool:
    """幂等地把 cc_mcp_server 注册进 grok 的 config.toml。返回是否发生写入。

    不写 env：grok 的 MCP 子进程继承父进程环境，per-turn 的 CC_LARK_* 由
    run_grok 放进子进程 env 即可（写死在配置里反而会在并发话题间串台）。
    已存在同名 server 就不动（尊重用户手改）。
    """
    cc_server = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cc_mcp_server.py")
    if not os.path.isfile(cc_server):
        return False
    config_path = os.path.expanduser(config_path)
    existing = ""
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                existing = fh.read()
        except OSError:
            return False
    if "[mcp_servers.cc-lark]" in existing:
        return False
    block = (
        "\n# ── cc-lark 运行时 MCP（wake_me_in / dispatch_task / …）──────────\n"
        "# 不配 env：per-turn 的 CC_LARK_* 由 bot 放进 grok 进程环境，MCP 子进程继承。\n"
        "# 在非 bot 的交互式 grok 会话里调用会明确报 \"no thread context\"，无副作用。\n"
        "[mcp_servers.cc-lark]\n"
        f"command = {json.dumps(sys.executable)}\n"
        f"args = [{json.dumps(cc_server)}]\n"
        "startup_timeout_sec = 30\n"
    )
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    tmp = f"{config_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(existing.rstrip() + "\n" + block if existing.strip() else block.lstrip("\n"))
    os.replace(tmp, config_path)
    os.chmod(config_path, 0o600)
    return True


def _normalize_permission_mode(mode: Optional[str], dangerous_skip: bool) -> str:
    m = (mode or "").strip()
    if m in GROK_PERMISSION_MODES:
        return m
    if dangerous_skip:
        return "bypassPermissions"
    fallback = (PERMISSION_MODE or "").strip()
    return fallback if fallback in GROK_PERMISSION_MODES else "default"


def _normalize_effort(effort: Optional[str]) -> Optional[str]:
    e = (effort or "").strip().lower()
    if not e:
        return None
    if e not in GROK_EFFORT_LEVELS:
        raise ValueError(
            f"invalid Grok effort {e!r}; expected one of {list(GROK_EFFORT_LEVELS)}"
        )
    return e


def _usage_from_result(data: dict) -> dict:
    """把 grok result 事件的 usage 映射成 footer 用的字段名。"""
    usage = dict(data.get("usage") or {})
    usage.pop("server_tool_use", None)
    model_usage = data.get("modelUsage") or {}
    if isinstance(model_usage, dict) and model_usage:
        first = next(iter(model_usage.values()), {})
        if isinstance(first, dict) and first.get("contextWindow"):
            usage["_context_window"] = first["contextWindow"]
    return {k: v for k, v in usage.items() if isinstance(v, (int, float))}


async def run_grok(
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
    grok_bin: Optional[str] = None,
    grok_home: Optional[str] = None,
    api_key: Optional[str] = None,
    api_key_env: Optional[str] = None,
    max_turns: int = 0,
    dangerously_skip_permissions: bool = True,
    idle_timeout_sec: int = IDLE_TIMEOUT,
) -> tuple[str, Optional[str], bool]:
    """返回 (full_text, session_id, used_fresh_session_fallback)。"""
    del on_status  # grok 没有独立的状态事件通道，正文/工具事件已够用

    # cc-lark 运行时 MCP（wake_me_in / dispatch_task）注册一次即可；env 靠继承。
    if (extra_env or {}).get("CC_LARK_THREAD_ID") and os.getenv("CC_LARK_WAKE_MCP", "1") != "0":
        try:
            ensure_cc_lark_mcp(
                os.path.join(os.path.expanduser(grok_home), "config.toml")
                if grok_home else GROK_CONFIG_PATH
            )
        except OSError as exc:
            print(f"[run_grok] cc-mcp 注册跳过: {type(exc).__name__}: {exc}", flush=True)

    resolved_effort = _normalize_effort(effort)
    resolved_mode = _normalize_permission_mode(
        permission_mode, dangerously_skip_permissions
    )
    idle_limit = idle_timeout_sec if idle_timeout_sec > 0 else IDLE_TIMEOUT

    async def _run_once(
        active_session_id: Optional[str],
    ) -> tuple[str, Optional[str], Optional[int], str]:
        # 长 prompt 走文件，避开 argv 长度上限
        fd, prompt_path = tempfile.mkstemp(prefix="cc_lark_grok_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(message)

        cmd = [
            resolve_grok_bin(grok_bin),
            "--prompt-file", prompt_path,
            "--output-format", "streaming-messages-json",
            "--include-partial-messages",
            "--permission-mode", resolved_mode,
            "--verbatim",  # prompt 原样发送，别让 CLI 二次加工
        ]
        deny_tools = os.getenv("CC_LARK_GROK_DISALLOWED_TOOLS", DEFAULT_DISALLOWED_TOOLS).strip()
        if deny_tools:
            cmd += ["--disallowed-tools", deny_tools]
        # 首轮自己钉一个 UUID，这样 session id 从一开始就归我们掌握
        planned_session_id = active_session_id or str(uuid.uuid4())
        if active_session_id:
            cmd += ["--resume", active_session_id]
        else:
            cmd += ["--session-id", planned_session_id]
        if model:
            cmd += ["-m", model]
        if resolved_effort:
            cmd += ["--reasoning-effort", resolved_effort]
        if append_system_prompt:
            cmd += ["--rules", append_system_prompt]
        if max_turns > 0:
            cmd += ["--max-turns", str(max_turns)]

        env = os.environ.copy()
        # 别让 bot spawn 出来的 agent 被 session-mirror hook 镜像回 Lark
        env["CC_LARK_MIRROR_OFF"] = "1"
        if grok_home:
            env["GROK_HOME"] = os.path.expanduser(grok_home)
        if api_key:
            env[(api_key_env or "XAI_API_KEY").strip() or "XAI_API_KEY"] = api_key
        if extra_env:
            env.update(extra_env)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.path.expanduser("~"),
            env=env,
            limit=10 * 1024 * 1024,
            # 与其它后端一致：独立进程组，/stop 时 killpg 不会误伤 main.py
            start_new_session=True,
        )
        await _fire_callback(on_process_start, proc)

        full_text = ""
        new_session_id = planned_session_id
        pending_tool_name = ""
        pending_tool_input_json = ""

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
                        f"Grok 单轮执行超过 wall-clock 最终上限（{int(wall_clock_limit)}秒），已终止进程。"
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
                                f"Grok 执行超时（{threshold}秒有子进程但 Grok 端无任何新输出），已终止进程。"
                                f"常见原因：tail -f / watch / npm run dev 等永不退出的阻塞命令。"
                            )
                        raise RuntimeError(
                            f"Grok 执行超时（{threshold}秒无输出且无活跃子进程），已终止进程"
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
                    # 出错时 grok 也走 result（is_error=true / subtype=error_*），
                    # result 里塞的是错误文案。与 claude 后端对齐：识别为错误就 raise，
                    # 把可 resume 的 session id 带出去让 dispatcher 续跑，而不是
                    # 把错误伪装成回答发给用户。
                    if data.get("is_error") or str(data.get("subtype", "")).startswith("error"):
                        detail = final_text or data.get("error") or data.get("subtype") or "unknown error"
                        exc = RuntimeError(f"Grok 执行出错：{detail}")
                        exc.cc_session_id = new_session_id
                        exc.cc_retryable_resume = not is_fatal_error_text(str(detail))
                        raise exc
                    if final_text:
                        full_text = final_text
                    usage = _usage_from_result(data)
                    if usage:
                        await _fire_callback(on_usage, usage)

        finally:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass

        stderr_output = await proc.stderr.read()
        await proc.wait()
        stderr_text = stderr_output.decode("utf-8", errors="replace").strip()
        return full_text.strip(), new_session_id, proc.returncode, stderr_text

    final_text, new_session_id, returncode, stderr_text = await _run_once(session_id)
    used_fresh_session_fallback = False

    # resume 哑失败兜底（session 文件被清 / 上一轮被杀写坏 / 换了 GROK_HOME）：
    # code>0 + 无 stderr + 无输出 → 退回新 session。returncode<0 = 被信号杀
    # （/stop、restart），那是人为中断，绝不能 fallback 再拉一个新进程。
    if session_id and returncode is not None and returncode > 0 and not stderr_text and not final_text:
        print(
            f"[run_grok] resume failed (code={returncode}, empty stderr/output), "
            f"retrying with fresh session; sid={session_id} cwd={cwd}",
            flush=True,
        )
        final_text, new_session_id, returncode, stderr_text = await _run_once(None)
        used_fresh_session_fallback = True

    if returncode != 0:
        detail = stderr_text or "no stderr"
        if final_text:
            return final_text, new_session_id, used_fresh_session_fallback
        exc = RuntimeError(f"grok exited with code {returncode}: {detail}")
        if returncode is not None and returncode > 0 and new_session_id and not is_fatal_error_text(stderr_text):
            exc.cc_session_id = new_session_id
            exc.cc_retryable_resume = True
        raise exc

    return final_text, new_session_id, used_fresh_session_fallback
