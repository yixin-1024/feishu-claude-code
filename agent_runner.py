"""Claude / Codex / OpenCode / MiMo / Grok / Maka 后端分发入口。"""

from __future__ import annotations

from typing import Callable, Optional

from bot_config import Profile, load_claude_extra_env, resolve_cc_lark_gates
from claude_runner import run_claude
from codex_runner import run_codex
from opencode_runner import run_opencode
from mimo_runner import run_mimo
from grok_runner import run_grok
from maka_runner import run_maka


async def run_agent(
    *,
    profile: Profile,
    runner: str,
    message: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    cwd: Optional[str] = None,
    permission_mode: Optional[str] = None,
    on_text_chunk: Optional[Callable[[str], None]] = None,
    on_tool_use: Optional[Callable[[str, dict], None]] = None,
    on_process_start: Optional[Callable[[object], None]] = None,
    on_usage: Optional[Callable[[dict], None]] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    append_system_prompt: Optional[str] = None,
    wake_context: Optional[dict] = None,
) -> tuple[str, Optional[str], bool]:
    """wake_context: 本轮 Lark 会话上下文（CC_LARK_* 形态）。

    Claude/Codex 后端会把它透传给 cc_mcp_server，让 wake/dispatch/cron 能定向到本话题。
    """
    backend = (runner or profile.runner or "claude").strip().lower()
    if backend == "opencode":
        return await run_opencode(
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
            opencode_bin=profile.opencode_bin,
            provider=profile.opencode_provider,
            api_key=profile.opencode_api_key,
            api_key_env=profile.opencode_api_key_env,
            dangerously_skip_permissions=bool(profile.opencode_dangerous_skip),
            idle_timeout_sec=profile.opencode_idle_timeout_sec,
        )

    if backend == "mimo":
        return await run_mimo(
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
            mimo_bin=profile.mimo_bin,
            provider=profile.mimo_provider,
            base_url=profile.mimo_base_url,
            api_key=profile.mimo_api_key,
            variant=profile.mimo_variant,
            dangerously_skip_permissions=bool(profile.mimo_dangerous_skip),
            idle_timeout_sec=profile.mimo_idle_timeout_sec,
        )

    if backend == "grok":
        # grok 的 MCP 子进程继承父进程 env，所以 wake_context（CC_LARK_*）直接
        # 塞进 extra_env 即可让 cc_mcp_server 定向到本话题，不用改写配置文件。
        grok_env = dict(wake_context or {})
        grok_env["CC_LARK_PROFILE"] = profile.name
        # 能力闸门（per-profile 覆盖优先）也得进 env——grok 没有 --mcp-config，
        # cc_mcp_server 全靠继承本进程环境拿到它们。
        grok_env.update(resolve_cc_lark_gates(profile.name))
        return await run_grok(
            message=message,
            session_id=session_id,
            model=model,
            effort=effort,
            cwd=cwd,
            permission_mode=permission_mode,
            on_text_chunk=on_text_chunk,
            on_tool_use=on_tool_use,
            on_process_start=on_process_start,
            on_usage=on_usage,
            on_status=on_status,
            append_system_prompt=append_system_prompt,
            grok_bin=profile.grok_bin,
            grok_home=profile.grok_home,
            api_key=profile.grok_api_key,
            api_key_env=profile.grok_api_key_env,
            max_turns=profile.grok_max_turns,
            dangerously_skip_permissions=bool(profile.grok_dangerous_skip),
            idle_timeout_sec=profile.grok_idle_timeout_sec,
            extra_env=grok_env,
        )

    if backend == "maka":
        # maka 给 MCP 子进程的环境是白名单（PATH/HOME/LC_*/XDG_* + mcp.json 里显式
        # 写的 env），CC_LARK_* 传不进去，所以 cc-lark 运行时 MCP 还没接到这个后端上。
        # extra_env 这里只用于 wall-clock 上限解析（resolve_claude_wall_clock_limit）。
        maka_env = dict(wake_context or {})
        maka_env["CC_LARK_PROFILE"] = profile.name
        return await run_maka(
            message=message,
            session_id=session_id,
            model=model,
            effort=effort,
            cwd=cwd,
            permission_mode=permission_mode,
            on_text_chunk=on_text_chunk,
            on_tool_use=on_tool_use,
            on_process_start=on_process_start,
            on_usage=on_usage,
            on_status=on_status,
            append_system_prompt=append_system_prompt,
            maka_bin=profile.maka_bin,
            workspace_root=profile.maka_workspace_root,
            connection=profile.maka_connection,
            api_key=profile.maka_api_key,
            api_key_env=profile.maka_api_key_env,
            base_url=profile.maka_base_url,
            base_url_env=profile.maka_base_url_env,
            max_steps=profile.maka_max_steps,
            dangerously_skip_permissions=bool(profile.maka_dangerous_skip),
            idle_timeout_sec=profile.maka_idle_timeout_sec,
            extra_env=maka_env,
        )

    if backend == "codex":
        # 私聊没有 dispatcher 的 thread wake_context，也必须带 profile，才能解析
        # <PROFILE>_CODEX_REASONING_EFFORT / AUTO_CONTINUE 等 profile 级配置。
        codex_env = dict(wake_context or {})
        codex_env["CC_LARK_PROFILE"] = profile.name
        return await run_codex(
            message=message,
            session_id=session_id,
            model=model,
            reasoning_effort=effort,
            cwd=cwd,
            permission_mode=permission_mode,
            on_text_chunk=on_text_chunk,
            on_tool_use=on_tool_use,
            on_process_start=on_process_start,
            on_usage=on_usage,
            on_status=on_status,
            should_stop=should_stop,
            append_system_prompt=append_system_prompt,
            codex_bin=profile.codex_bin,
            sandbox_mode=profile.codex_sandbox_mode,
            approval_policy=profile.codex_approval_policy,
            dangerous_bypass_level=profile.codex_dangerous_bypass,
            idle_timeout_sec=profile.codex_idle_timeout_sec,
            extra_env=codex_env,
        )

    claude_env = load_claude_extra_env(profile) or {}
    if wake_context:
        claude_env = {**claude_env, **wake_context}
    if profile.claude_runner:
        # profile 级子后端选择优先于 claude env 文件，便于单独切换某个 bot。
        claude_env = {**claude_env, "CLAUDE_RUNNER": profile.claude_runner}
    return await run_claude(
        message=message,
        session_id=session_id,
        model=model,
        effort=effort,
        cwd=cwd,
        permission_mode=permission_mode,
        on_text_chunk=on_text_chunk,
        on_tool_use=on_tool_use,
        on_process_start=on_process_start,
        on_usage=on_usage,
        on_status=on_status,
        append_system_prompt=append_system_prompt,
        extra_env=claude_env or None,
    )
