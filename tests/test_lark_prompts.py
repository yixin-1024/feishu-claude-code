import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_config import Profile
from lark_prompts import render_lark_prompt


def _profile():
    return Profile(
        name="regtank",
        app_id="app",
        app_secret="secret",
        platform="lark",
        domain="https://open.larksuite.com",
        default_cwd="/tmp",
        runner="codex",
        default_model="gpt-5.1-codex-max",
        lark_cli_profile="regtank",
    )


def test_codex_prompt_includes_runtime_mcp_section():
    out = render_lark_prompt(
        profile=_profile(),
        raw_chat_id="oc_1",
        thread_id="omt_1",
        user_message_id="om_1",
        is_group=True,
        asker_open_id="ou_1",
        runner="codex",
    )

    assert "cc-lark 运行时 MCP 工具" in out
    assert "本后端无运行时 MCP 工具" not in out


def test_non_mcp_backend_prompt_keeps_no_runtime_mcp_warning():
    out = render_lark_prompt(
        profile=_profile(),
        raw_chat_id="oc_1",
        thread_id="omt_1",
        user_message_id="om_1",
        is_group=True,
        asker_open_id="ou_1",
        runner="opencode",
    )

    assert "本后端无运行时 MCP 工具" in out
