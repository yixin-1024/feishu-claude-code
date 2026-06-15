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
async def test_opencode_profile_default_runner_and_model(tmp_path, monkeypatch):
    monkeypatch.setattr("session_store.SESSIONS_DIR", str(tmp_path))
    store = SessionStore(
        profile="hermes",
        default_cwd="/tmp",
        default_runner="opencode",
        default_model="google/gemini-3.1-pro-preview",
    )

    session = await store.get_current("user_1", "oc_hermes")

    assert session.runner == "opencode"
    assert session.model == "google/gemini-3.1-pro-preview"


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
async def test_stale_pinned_model_ignored_follows_default_codex(tmp_path, monkeypatch):
    """旧 session 里残留的 current['model'] 已不再权威：实际模型一律跟随 profile
    默认（model_override 缺省时）。哪怕残留的是个 claude 模型也不会泄漏出去。"""
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
    cur["model"] = "claude-opus-4-8[1m]"   # 旧版残留的钉死字段
    await store._save_async()

    session = await store.get_current(user_id, chat_id)

    assert session.runner == "codex"
    assert session.model == "gpt-5.5"      # 残留字段被忽略，跟随 profile 默认


@pytest.mark.asyncio
async def test_stale_pinned_model_ignored_follows_default_opencode(tmp_path, monkeypatch):
    monkeypatch.setattr("session_store.SESSIONS_DIR", str(tmp_path))
    store = SessionStore(
        profile="hermes",
        default_cwd="/tmp/default",
        default_runner="opencode",
        default_model="google/gemini-3.1-pro-preview",
    )
    user_id = "user_1"
    chat_id = "oc_hermes"
    cur = await store.get_current_raw(user_id, chat_id)
    cur["runner"] = "opencode"
    cur["model"] = "claude-opus-4-8[1m]"   # 旧版残留的钉死字段
    await store._save_async()

    session = await store.get_current(user_id, chat_id)

    assert session.runner == "opencode"
    assert session.model == "google/gemini-3.1-pro-preview"


@pytest.mark.asyncio
async def test_default_model_change_propagates_to_old_session(tmp_path, monkeypatch):
    """核心诉求：改 config 默认模型 + 重启，旧 session（无显式 override）立即跟随。"""
    monkeypatch.setattr("session_store.SESSIONS_DIR", str(tmp_path))
    # 旧进程：默认 opus，建了个 session
    store1 = SessionStore(profile="spx", default_cwd="/tmp", default_runner="claude",
                          default_model="claude-opus-4-8[1m]")
    cur = await store1.get_current_raw("u1", "oc_x")
    cur["model"] = "claude-opus-4-8[1m]"      # 模拟旧版残留
    cur["session_id"] = "sid_keep"
    await store1._save_async()

    # 新进程：config 默认换成 opus-4-9，同一份持久化文件
    store2 = SessionStore(profile="spx", default_cwd="/tmp", default_runner="claude",
                          default_model="claude-opus-4-9[1m]")
    session = await store2.get_current("u1", "oc_x")
    assert session.model == "claude-opus-4-9[1m]"   # 旧 session 跟随新默认
    assert session.session_id == "sid_keep"          # 仅换模型，不打断会话


@pytest.mark.asyncio
async def test_model_override_set_and_clear(tmp_path, monkeypatch):
    """/model 显式覆盖生效；/model default（set_model 空串）清回默认。"""
    monkeypatch.setattr("session_store.SESSIONS_DIR", str(tmp_path))
    store = SessionStore(profile="spx", default_cwd="/tmp", default_runner="claude",
                         default_model="claude-opus-4-8[1m]")
    await store.set_model("u1", "oc_x", "claude-sonnet-4-6")
    assert (await store.get_current("u1", "oc_x")).model == "claude-sonnet-4-6"
    await store.set_model("u1", "oc_x", "")          # 清除 override
    assert (await store.get_current("u1", "oc_x")).model == "claude-opus-4-8[1m]"


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
