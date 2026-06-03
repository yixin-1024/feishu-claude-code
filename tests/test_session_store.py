import pytest
import json
import tempfile
import os
import sys

# Set test environment variables before importing
os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")
os.environ.setdefault("DEFAULT_MODEL", "claude-opus-4-6")
os.environ.setdefault("PERMISSION_MODE", "bypassPermissions")

# Add parent directory to path to import session_store
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_config import DEFAULT_MODEL, PERMISSION_MODE
from session_store import SessionStore


@pytest.fixture
def temp_store():
    """Create a temporary session store for testing"""
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)

    store = SessionStore()
    store.SESSIONS_FILE = path
    store._data = {}
    store._save()

    yield store

    if os.path.exists(path):
        os.unlink(path)


@pytest.mark.asyncio
async def test_get_current_with_chat_id_private(temp_store):
    """Test getting current session for private chat"""
    user_id = "user_123"
    chat_id = "user_123"

    session = await temp_store.get_current(user_id, chat_id)
    assert session.model == DEFAULT_MODEL
    assert session.permission_mode == PERMISSION_MODE


@pytest.mark.asyncio
async def test_get_current_with_chat_id_group(temp_store):
    """Test getting current session for group chat"""
    user_id = "user_123"
    chat_id = "group_456"

    session = await temp_store.get_current(user_id, chat_id)
    assert session.model == DEFAULT_MODEL
    assert session.permission_mode == PERMISSION_MODE


@pytest.mark.asyncio
async def test_profile_default_runner_and_model(tmp_path, monkeypatch):
    monkeypatch.setattr("session_store.SESSIONS_DIR", str(tmp_path))
    store = SessionStore(
        profile="codexbot",
        default_cwd="/tmp",
        default_runner="codex",
        default_model="gpt-5.1-codex-max",
    )

    session = await store.get_current("user_1", "oc_codex")

    assert session.runner == "codex"
    assert session.model == "gpt-5.1-codex-max"


@pytest.mark.asyncio
async def test_reset_current_to_profile_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("session_store.SESSIONS_DIR", str(tmp_path))
    store = SessionStore(
        profile="codexbot",
        default_cwd="/tmp/default",
        default_runner="codex",
        default_model="gpt-5.5",
    )
    user_id = "user_1"
    chat_id = "oc_codex"

    await store.set_model(user_id, chat_id, "claude-sonnet-4-6")
    await store.set_cwd(user_id, chat_id, "/tmp/other")
    await store.set_permission_mode(user_id, chat_id, "default")
    cur = await store.get_current_raw(user_id, chat_id)
    cur["runner"] = "claude"
    cur["session_id"] = "old_sid"

    await store.reset_current_to_defaults(user_id, chat_id)
    session = await store.get_current(user_id, chat_id)

    assert session.session_id is None
    assert session.runner == "codex"
    assert session.model == "gpt-5.5"
    assert session.cwd == "/tmp/default"
    assert session.permission_mode == PERMISSION_MODE


@pytest.mark.asyncio
async def test_codex_profile_clears_stale_claude_model(tmp_path, monkeypatch):
    monkeypatch.setattr("session_store.SESSIONS_DIR", str(tmp_path))
    store = SessionStore(
        profile="codexbot",
        default_cwd="/tmp/default",
        default_runner="codex",
        default_model="gpt-5.5",
    )
    user_id = "user_1"
    chat_id = "oc_codex"
    cur = await store.get_current_raw(user_id, chat_id)
    cur["runner"] = "codex"
    cur["model"] = "claude-opus-4-8[1m]"
    cur["session_id"] = "old_claude_sid"
    await store._save_async()

    session = await store.get_current(user_id, chat_id)

    assert session.runner == "codex"
    assert session.model == "gpt-5.5"
    assert session.session_id is None


@pytest.mark.asyncio
async def test_session_isolation_between_chats(temp_store):
    """Test that private and group sessions are isolated"""
    user_id = "user_123"
    private_chat_id = "user_123"
    group_chat_id = "group_456"

    await temp_store.set_model(user_id, private_chat_id, "claude-sonnet-4-6")
    await temp_store.set_model(user_id, group_chat_id, "claude-haiku-4-5-20251001")

    private_session = await temp_store.get_current(user_id, private_chat_id)
    group_session = await temp_store.get_current(user_id, group_chat_id)

    assert private_session.model == "claude-sonnet-4-6"
    assert group_session.model == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_set_model_with_chat_id(temp_store):
    """Test setting model for specific chat"""
    user_id = "user_123"
    chat_id = "group_456"

    await temp_store.set_model(user_id, chat_id, "claude-sonnet-4-6")

    session = await temp_store.get_current(user_id, chat_id)
    assert session.model == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_set_permission_mode_with_chat_id(temp_store):
    user_id = "user_123"
    chat_id = "group_456"

    await temp_store.set_permission_mode(user_id, chat_id, "plan")
    session = await temp_store.get_current(user_id, chat_id)
    assert session.permission_mode == "plan"


@pytest.mark.asyncio
async def test_set_cwd_with_chat_id(temp_store):
    user_id = "user_123"
    chat_id = "group_456"

    await temp_store.set_cwd(user_id, chat_id, "/tmp")
    session = await temp_store.get_current(user_id, chat_id)
    assert session.cwd == "/tmp"


@pytest.mark.asyncio
async def test_new_session_with_chat_id(temp_store):
    user_id = "user_123"
    chat_id = "group_456"

    await temp_store.set_model(user_id, chat_id, "claude-sonnet-4-6")
    await temp_store.new_session(user_id, chat_id)

    session = await temp_store.get_current(user_id, chat_id)
    assert session.session_id is None


@pytest.mark.asyncio
async def test_chat_default_cwd_used_for_new_group():
    """Per-chat default cwd should be applied to a freshly-seen group."""
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    try:
        store = SessionStore(
            default_cwd="/fallback",
            chat_default_cwd={
                "oc_spx": "/work/spx",
                "oc_feishu": "/work/feishu-claude-code",
            },
        )
        # Drop on-disk legacy state and any data the constructor loaded so we
        # exercise the "new chat" path with our injected map only.
        store._sessions_file = path
        store._data = {}
        store._save()

        spx = await store.get_current("user_1", "oc_spx")
        feishu = await store.get_current("user_1", "oc_feishu")
        unknown = await store.get_current("user_1", "oc_other")

        assert spx.cwd == "/work/spx"
        assert feishu.cwd == "/work/feishu-claude-code"
        assert unknown.cwd == "/fallback"
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_chat_default_cwd_does_not_override_persisted():
    """Once a chat has a persisted cwd, the chat_default_cwd map must NOT touch it."""
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    try:
        # First pass: no map. User manually sets cwd to /custom.
        store1 = SessionStore(default_cwd="/fallback")
        store1._sessions_file = path
        store1._data = {}
        store1._save()
        await store1.set_cwd("user_1", "oc_spx", "/custom")

        # Second pass: now map says oc_spx -> /work/spx. Persisted /custom wins.
        store2 = SessionStore(
            default_cwd="/fallback",
            chat_default_cwd={"oc_spx": "/work/spx"},
        )
        store2._sessions_file = path
        store2._data = json.load(open(path))

        session = await store2.get_current("user_1", "oc_spx")
        assert session.cwd == "/custom"
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_chat_default_cwd_private_uses_global_default():
    """Private chats normalize to 'private', so a map keyed by user_id should NOT match."""
    fd, path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    try:
        store = SessionStore(
            default_cwd="/fallback",
            chat_default_cwd={"user_1": "/should-not-apply", "private": "/private-default"},
        )
        store._sessions_file = path
        store._data = {}
        store._save()

        # chat_id == user_id → key normalizes to "private"
        session = await store.get_current("user_1", "user_1")
        assert session.cwd == "/private-default"
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_list_sessions_with_chat_id(temp_store):
    user_id = "user_123"
    chat_id = "group_456"

    sessions = await temp_store.list_sessions(user_id, chat_id)
    assert len(sessions) == 0

    await temp_store.set_model(user_id, chat_id, "claude-sonnet-4-6")
    raw = await temp_store.get_current_raw(user_id, chat_id)
    raw["session_id"] = "test_session_123"
    temp_store._save()

    await temp_store.new_session(user_id, chat_id)

    sessions = await temp_store.list_sessions(user_id, chat_id)
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "test_session_123"
