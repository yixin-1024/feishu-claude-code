import asyncio
import json
import pytest

from bot_config import is_model_compatible_with_runner, normalize_model_for_runner
import session_store
from session_store import SessionStore
import commands


def test_is_model_compatible_with_runner():
    # Claude runner
    assert is_model_compatible_with_runner("opus", "claude") is True
    assert is_model_compatible_with_runner("opus[1m]", "claude") is True
    assert is_model_compatible_with_runner("sonnet", "claude") is True
    assert is_model_compatible_with_runner("fable", "claude") is True
    assert is_model_compatible_with_runner("claude-opus-5[1m]", "claude") is True
    assert is_model_compatible_with_runner("gemini-3.8-flash", "claude") is False
    assert is_model_compatible_with_runner("gpt-5.5", "claude") is False

    # Agy runner (only accepts gemini models)
    assert is_model_compatible_with_runner("gemini-3.8-flash", "agy") is True
    assert is_model_compatible_with_runner("gemini-3.8-flash-high", "agy") is True
    assert is_model_compatible_with_runner("gemini-3.1-pro", "agy") is True
    assert is_model_compatible_with_runner("agy", "agy") is True
    assert is_model_compatible_with_runner("agy-38", "agy") is True
    assert is_model_compatible_with_runner("google/gemini-3.8-flash", "agy") is True
    assert is_model_compatible_with_runner("opus", "agy") is False
    assert is_model_compatible_with_runner("opus[1m]", "agy") is False
    assert is_model_compatible_with_runner("fable", "agy") is False
    assert is_model_compatible_with_runner("sonnet[1m]", "agy") is False
    assert is_model_compatible_with_runner("gpt-5.5", "agy") is False

    # Codex runner
    assert is_model_compatible_with_runner("gpt-5.5", "codex") is True
    assert is_model_compatible_with_runner("codex", "codex") is True
    assert is_model_compatible_with_runner("gpt-5.1-codex-max", "codex") is True
    assert is_model_compatible_with_runner("opus[1m]", "codex") is False
    assert is_model_compatible_with_runner("gemini-3.8-flash", "codex") is False


def test_normalize_model_for_runner():
    # Normal cases
    assert normalize_model_for_runner("opus", "claude") == "opus[1m]"
    assert normalize_model_for_runner("agy-38", "agy") == "gemini-3.8-flash"
    assert normalize_model_for_runner("google/gemini-3.8-flash", "agy") == "gemini-3.8-flash"

    # Incompatible model fallback
    assert normalize_model_for_runner("opus", "agy", fallback="gemini-3.8-flash") == "gemini-3.8-flash"
    assert normalize_model_for_runner("opus[1m]", "agy", fallback="gemini-3.8-flash") == "gemini-3.8-flash"
    assert normalize_model_for_runner("gemini-3.8-flash", "claude", fallback="opus") == "opus[1m]"


@pytest.mark.asyncio
async def test_session_store_self_heals_incompatible_model_override(monkeypatch, tmp_path):
    monkeypatch.setattr(session_store, "SESSIONS_DIR", str(tmp_path))
    # Prepare a sessions file with invalid model_override for an agy runner
    file_path = tmp_path / "sessions-agy.json"
    dirty_data = {
        "ou_test": {
            "oc_chat:omt_thread": {
                "current": {
                    "runner": "agy",
                    "model_override": "opus[1m]",
                    "effort_override": "high",
                    "session_id": "conv-123",
                }
            }
        }
    }
    file_path.write_text(json.dumps(dirty_data), encoding="utf-8")

    store = SessionStore(
        profile="agy",
        default_model="gemini-3.8-flash",
        default_cwd="/tmp",
        default_runner="agy",
    )

    session = await store.get_current("ou_test", "oc_chat:omt_thread")
    # model_override should be cleaned because opus[1m] is incompatible with agy
    assert session.model == "gemini-3.8-flash"

    # Check raw store data
    cur = await store.get_current_raw("ou_test", "oc_chat:omt_thread")
    assert cur["model_override"] is None


@pytest.mark.asyncio
async def test_session_store_blocks_incompatible_set_model(monkeypatch, tmp_path):
    monkeypatch.setattr(session_store, "SESSIONS_DIR", str(tmp_path))
    store = SessionStore(
        profile="agy",
        default_model="gemini-3.8-flash",
        default_cwd="/tmp",
        default_runner="agy",
    )

    # Setting an incompatible model should reset model_override to None
    await store.set_model("ou_test", "oc_chat:omt_thread", "opus[1m]")
    session = await store.get_current("ou_test", "oc_chat:omt_thread")
    assert session.model == "gemini-3.8-flash"

    cur = await store.get_current_raw("ou_test", "oc_chat:omt_thread")
    assert cur["model_override"] is None


@pytest.mark.asyncio
async def test_model_command_rejects_incompatible_runner_model(monkeypatch, tmp_path):
    monkeypatch.setattr(session_store, "SESSIONS_DIR", str(tmp_path))
    store = SessionStore(
        profile="agy",
        default_model="gemini-3.8-flash",
        default_cwd="/tmp",
        default_runner="agy",
    )

    # In agy session, /model opus should be rejected
    res = await commands.handle_command(
        cmd="model",
        args="opus",
        user_id="ou_test",
        chat_id="oc_chat:omt_thread",
        store=store,
    )
    assert isinstance(res, str)
    assert "与当前 runner `agy` 不兼容" in res

    # Store should remain clean
    session = await store.get_current("ou_test", "oc_chat:omt_thread")
    assert session.model == "gemini-3.8-flash"
