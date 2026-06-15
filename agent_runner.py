"""Claude/Codex 后端分发入口。"""

from __future__ import annotations

from typing import Callable, Optional

from bot_config import Profile, load_claude_extra_env
from claude_runner import run_claude
from codex_runner import run_codex
from opencode_runner import run_opencode


async def run_agent(
    *,
    profile: Profile,
    runner: str,
    message: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    permission_mode: Optional[str] = None,
    on_text_chunk: Optional[Callable[[str], None]] = None,
    on_tool_use: Optional[Callable[[str, dict], None]] = None,
    on_process_start: Optional[Callable[[object], None]] = None,
    on_usage: Optional[Callable[[dict], None]] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
    append_system_prompt: Optional[str] = None,
) -> tuple[str, Optional[str], bool]:
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

    if backend == "codex":
        return await run_codex(
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
            codex_bin=profile.codex_bin,
            sandbox_mode=profile.codex_sandbox_mode,
            approval_policy=profile.codex_approval_policy,
            dangerous_bypass_level=profile.codex_dangerous_bypass,
            idle_timeout_sec=profile.codex_idle_timeout_sec,
        )

    return await run_claude(
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
        extra_env=load_claude_extra_env(profile) or None,
    )
