"""话题群共享 session（THREAD_SHARED_SESSION）行为测试。

核心语义：thread 复合 chat_id（"oc_xxx:omt_yyy"）在共享模式下归到哨兵桶
SHARED_THREAD_UID —— 同一话题里所有人共享同一个 claude/codex session；
私聊和非话题群聊不受影响。
"""

import os
import sys

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")
os.environ.setdefault("DEFAULT_MODEL", "claude-opus-4-6")
os.environ.setdefault("PERMISSION_MODE", "bypassPermissions")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_control import ActiveRunRegistry, _key  # noqa: E402
from session_store import SHARED_THREAD_UID, SessionStore  # noqa: E402

THREAD_CHAT = "oc_group1:omt_thread1"
OTHER_THREAD = "oc_group1:omt_thread2"


def _make_store(tmp_path, monkeypatch, shared=True) -> SessionStore:
    monkeypatch.setattr("session_store.SESSIONS_DIR", str(tmp_path))
    return SessionStore(profile="sharedtest", shared_thread_sessions=shared)


@pytest.mark.asyncio
async def test_two_users_share_one_thread_session(tmp_path, monkeypatch):
    """A 跑完一轮后，B 在同一话题 get_current 拿到同一个 session_id。"""
    store = _make_store(tmp_path, monkeypatch)

    await store.on_claude_response("ou_alice", THREAD_CHAT, "sid_alice_1", "hello")

    session_b = await store.get_current("ou_bob", THREAD_CHAT)
    assert session_b.session_id == "sid_alice_1"

    # B 跑完一轮，A 也能续上
    await store.on_claude_response("ou_bob", THREAD_CHAT, "sid_shared_2", "world")
    session_a = await store.get_current("ou_alice", THREAD_CHAT)
    assert session_a.session_id == "sid_shared_2"

    # 数据落在哨兵桶下，而不是任何真实用户桶
    assert THREAD_CHAT in store._data.get(SHARED_THREAD_UID, {})
    assert THREAD_CHAT not in store._data.get("ou_alice", {})
    assert THREAD_CHAT not in store._data.get("ou_bob", {})


@pytest.mark.asyncio
async def test_threads_do_not_cross_share(tmp_path, monkeypatch):
    """不同话题各自一个共享 session，互不串。"""
    store = _make_store(tmp_path, monkeypatch)

    await store.on_claude_response("ou_alice", THREAD_CHAT, "sid_t1", "t1")
    await store.on_claude_response("ou_alice", OTHER_THREAD, "sid_t2", "t2")

    assert (await store.get_current("ou_bob", THREAD_CHAT)).session_id == "sid_t1"
    assert (await store.get_current("ou_bob", OTHER_THREAD)).session_id == "sid_t2"


@pytest.mark.asyncio
async def test_private_and_plain_group_not_shared(tmp_path, monkeypatch):
    """私聊（chat_id == user_id）和非话题群聊（无 ":"）保持按人分桶。"""
    store = _make_store(tmp_path, monkeypatch)

    await store.on_claude_response("ou_alice", "ou_alice", "sid_private", "hi")
    await store.on_claude_response("ou_alice", "oc_plain_group", "sid_group_a", "hi")

    assert (await store.get_current("ou_bob", "ou_bob")).session_id is None
    assert (await store.get_current("ou_bob", "oc_plain_group")).session_id is None
    assert (await store.get_current("ou_alice", "oc_plain_group")).session_id == "sid_group_a"


@pytest.mark.asyncio
async def test_shared_flag_off_keeps_legacy_behavior(tmp_path, monkeypatch):
    """关掉开关 → 旧版按 (人, 话题) 分桶。"""
    store = _make_store(tmp_path, monkeypatch, shared=False)

    await store.on_claude_response("ou_alice", THREAD_CHAT, "sid_alice", "hi")

    assert (await store.get_current("ou_bob", THREAD_CHAT)).session_id is None
    assert (await store.get_current("ou_alice", THREAD_CHAT)).session_id == "sid_alice"


@pytest.mark.asyncio
async def test_adopts_latest_legacy_thread_session(tmp_path, monkeypatch):
    """开启共享前已有按用户分桶的话题数据 → 首次访问收养 started_at 最新的，
    并把摘要带进哨兵桶。"""
    monkeypatch.setattr("session_store.SESSIONS_DIR", str(tmp_path))
    legacy = SessionStore(profile="sharedtest", shared_thread_sessions=False)
    await legacy.on_claude_response("ou_alice", THREAD_CHAT, "sid_alice_old", "alice msg")
    await legacy.on_claude_response("ou_bob", THREAD_CHAT, "sid_bob_new", "bob msg")
    # bob 的 started_at 更新（后写入）；手动拉开差距，避免同秒平局
    legacy._data["ou_alice"][THREAD_CHAT]["current"]["started_at"] = "2026-01-01T00:00:00"
    legacy._data["ou_bob"][THREAD_CHAT]["current"]["started_at"] = "2026-06-01T00:00:00"
    legacy._data["ou_bob"].setdefault("summaries", {})["sid_bob_new"] = "bob 的会话"
    legacy._save()

    store = SessionStore(profile="sharedtest", shared_thread_sessions=True)
    session = await store.get_current("ou_carol", THREAD_CHAT)

    assert session.session_id == "sid_bob_new"
    # 摘要复制进哨兵桶，任何人 get_summary 都能查到
    assert store.get_summary("ou_carol", "sid_bob_new") == "bob 的会话"


@pytest.mark.asyncio
async def test_new_session_resets_for_everyone(tmp_path, monkeypatch):
    """任何人 /new，整个话题的 session 一起重置（共享语义）。"""
    store = _make_store(tmp_path, monkeypatch)

    await store.on_claude_response("ou_alice", THREAD_CHAT, "sid_1", "hi")
    await store.new_session("ou_bob", THREAD_CHAT)

    assert (await store.get_current("ou_alice", THREAD_CHAT)).session_id is None
    # 旧 session 进了共享 history，谁都能 /resume
    sessions = await store.list_sessions("ou_carol", THREAD_CHAT)
    assert any(h["session_id"] == "sid_1" for h in sessions)


def test_run_registry_key_aggregates_by_thread():
    """共享模式下 ActiveRunRegistry 按话题聚合：B 能看到/停掉 A 启动的 run。"""
    registry = ActiveRunRegistry()
    run = registry.start_run("ou_alice", THREAD_CHAT, "card_1")

    assert registry.get_run("ou_bob", THREAD_CHAT) is run
    # 非话题 chat 仍按 (人, chat) 隔离
    registry.start_run("ou_alice", "oc_plain", "card_2")
    assert registry.get_run("ou_bob", "oc_plain") is None
    # 私聊（chat_id == user_id，万一未来 id 里带冒号也不该聚合）
    assert _key("x:y", "x:y") == "x:y::x:y"
