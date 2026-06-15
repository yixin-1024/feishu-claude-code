from types import SimpleNamespace

import pytest

import commands
import session_store as session_store_module
from commands import handle_command
from session_store import SessionStore


GEMINI_31 = "google/gemini-3.1-pro-preview"


@pytest.fixture
def opencode_store(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "state"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_store_module, "SESSIONS_DIR", str(sessions_dir))
    return SessionStore(
        profile="hermes",
        default_cwd=str(tmp_path),
        default_runner="opencode",
        default_model=GEMINI_31,
    )


def _bot():
    return SimpleNamespace(
        profile=SimpleNamespace(
            runner="opencode",
            default_model=GEMINI_31,
        )
    )


@pytest.mark.asyncio
async def test_model_picker_uses_gemini_buttons_for_opencode(opencode_store):
    reply = await handle_command(
        "model", "", "user_1", "oc_hermes", opencode_store, bot=_bot()
    )

    assert reply["text"].startswith("当前 runner：**opencode**")
    labels = [button["text"] for button in reply["buttons"]]
    assert "💎 Gemini 3.1 Pro" in labels
    assert "Gemini 2.5 Pro" in labels
    assert all("Claude" not in label and "Sonnet" not in label for label in labels)


@pytest.mark.asyncio
async def test_model_alias_sets_gemini_model_for_opencode(opencode_store):
    reply = await handle_command(
        "model", "g31", "user_1", "oc_hermes", opencode_store, bot=_bot()
    )
    current = await opencode_store.get_current("user_1", "oc_hermes")

    assert f"`{GEMINI_31}`" in reply
    assert current.model == GEMINI_31


@pytest.mark.asyncio
async def test_usage_for_opencode_uses_last_usage_not_claude_quota(opencode_store):
    cur = await opencode_store.get_current_raw("user_1", "oc_hermes")
    cur["session_id"] = "ses_opencode"
    cur["last_usage"] = {
        "input_tokens": 1000,
        "cache_read_input_tokens": 500,
        "output_tokens": 250,
        "_context_window": 1_048_576,
    }
    await opencode_store._save_async()

    reply = await handle_command(
        "usage", "", "user_1", "oc_hermes", opencode_store, bot=_bot()
    )

    assert "opencode 用量" in reply
    assert "Claude Max" not in reply
    assert "Runner: `opencode`" in reply
    assert f"模型: `{GEMINI_31}`" in reply
    assert "1.8k / 1M" in reply


@pytest.mark.asyncio
async def test_status_for_opencode_does_not_probe_claude_quota(
    opencode_store, monkeypatch
):
    def fail_quota():
        raise AssertionError("opencode status must not fetch Claude quota")

    monkeypatch.setattr(commands, "_get_quota_compact", fail_quota)
    cur = await opencode_store.get_current_raw("user_1", "oc_hermes")
    cur["session_id"] = "ses_opencode"
    cur["last_usage"] = {"input_tokens": 100, "output_tokens": 50}
    await opencode_store._save_async()

    reply = await handle_command(
        "status", "", "user_1", "oc_hermes", opencode_store, bot=_bot()
    )

    assert "Runner: `opencode`" in reply
    assert f"模型: `{GEMINI_31}`" in reply
    assert "Claude 配额" not in reply


@pytest.mark.asyncio
async def test_resume_lists_and_restores_opencode_sessions(opencode_store, monkeypatch):
    monkeypatch.setattr(commands, "scan_cli_sessions", lambda _limit: [])

    await opencode_store.on_agent_response(
        "user_1", "oc_hermes", "ses_one", "first message"
    )
    await opencode_store.new_session("user_1", "oc_hermes")
    await opencode_store.on_agent_response(
        "user_1", "oc_hermes", "ses_two", "second message"
    )

    listing = await handle_command(
        "resume", "", "user_1", "oc_hermes", opencode_store, bot=_bot()
    )
    assert "共 1 个历史会话" in listing["text"]
    assert listing["buttons"][0]["value"]["sid"] == "ses_one"

    reply = await handle_command(
        "resume", "1", "user_1", "oc_hermes", opencode_store, bot=_bot()
    )
    current = await opencode_store.get_current("user_1", "oc_hermes")

    assert "已恢复会话" in reply
    assert current.session_id == "ses_one"
    assert current.runner == "opencode"
