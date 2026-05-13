"""并发锁行为：同一 BotInstance 内不同 chat 用不同锁，同 chat 用同锁。

历史背景：multi-profile 重构后，全局 _chat_locks 搬进了 BotInstance。
test_integration.py 已覆盖端到端"并发不同群应使用不同锁"的场景；本文件聚焦
锁本身的契约（_ensure_chat_lock）+ SessionStore 并发写入。
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

# 添加项目根目录到 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_config import Profile
from bot_instance import BotInstance
from dispatcher import handle_message_async
from session_store import SessionStore


def _make_bot() -> BotInstance:
    """构造一个不真正连 Lark 的 BotInstance 测试桩。"""
    profile = Profile(
        name="test",
        app_id="cli_test",
        app_secret="secret",
        platform="lark",
        domain="open.larksuite.com",
        default_cwd="/tmp",
        allowed_group_chat_ids={"group_a", "group_b"},
    )
    bot = BotInstance.__new__(BotInstance)
    bot.profile = profile
    bot.chat_locks = {}
    bot.active_runs = MagicMock()
    bot.feishu = AsyncMock()
    bot.feishu.get_bot_open_id = AsyncMock(return_value="bot_open_id")
    return bot


def _make_event(chat_id: str, msg_id: str) -> Mock:
    ev = Mock()
    ev.event.sender.sender_id.open_id = "user123"
    ev.event.message.chat_id = chat_id
    ev.event.message.chat_type = "group"
    ev.event.message.message_type = "text"
    ev.event.message.content = '{"text": "@_user_1 hi"}'
    ev.event.message.message_id = msg_id
    ev.event.message.thread_id = ""
    fake_mention = Mock()
    fake_mention.key = "@_user_1"
    fake_mention.id = Mock()
    fake_mention.id.open_id = "bot_open_id"
    ev.event.message.mentions = [fake_mention]
    return ev


async def test_concurrent_messages_different_groups():
    """端到端：不同群消息并发跑，各自挂在不同锁上。"""
    bot = _make_bot()
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await asyncio.gather(
            handle_message_async(bot, _make_event("group_a", "msg_a")),
            handle_message_async(bot, _make_event("group_b", "msg_b")),
        )
    assert proc.await_count == 2
    assert "group_a" in bot.chat_locks
    assert "group_b" in bot.chat_locks
    assert bot.chat_locks["group_a"] is not bot.chat_locks["group_b"]


async def test_same_group_messages_serialized():
    """端到端：同群两条消息共享同锁，处理完释放。"""
    bot = _make_bot()
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await asyncio.gather(
            handle_message_async(bot, _make_event("group_a", "msg_1")),
            handle_message_async(bot, _make_event("group_a", "msg_2")),
        )
    assert proc.await_count == 2
    assert bot.chat_locks["group_a"].locked() is False


def test_chat_lock_isolation_unit():
    """直接单测 _ensure_chat_lock 契约。"""
    bot = _make_bot()
    lock_a1 = bot._ensure_chat_lock("group_a")
    lock_a2 = bot._ensure_chat_lock("group_a")
    lock_b = bot._ensure_chat_lock("group_b")

    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b


def test_chat_lock_eviction_under_pressure():
    """超过 _MAX_CHAT_LOCKS 时清理空闲锁，活跃锁不动。"""
    bot = _make_bot()
    BotInstance._MAX_CHAT_LOCKS = 4  # 临时调小

    # 先建 3 个 idle 锁 + 1 个 held 锁
    held = bot._ensure_chat_lock("held")
    asyncio.get_event_loop().run_until_complete(held.acquire()) if False else None
    # 用同步方式 held：用一个新 lock 直接 acquire
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    try:
        loop.run_until_complete(held.acquire())

        bot._ensure_chat_lock("idle1")
        bot._ensure_chat_lock("idle2")
        bot._ensure_chat_lock("idle3")
        assert len(bot.chat_locks) == 4

        # 触发第 5 个 → 应该清理一半 idle
        bot._ensure_chat_lock("new")
        assert "held" in bot.chat_locks  # 持有的锁不被清
        assert "new" in bot.chat_locks
    finally:
        held.release()
        loop.close()
        BotInstance._MAX_CHAT_LOCKS = 200


async def test_session_store_concurrent_writes(tmp_path, monkeypatch):
    """SessionStore 并发写入仍能 round-trip。"""
    import session_store as _ss
    monkeypatch.setattr(_ss, "SESSIONS_DIR", str(tmp_path))

    store = SessionStore(profile="test")

    async def update():
        await store._save_async()

    await asyncio.gather(update(), update(), update())

    store2 = SessionStore(profile="test")
    assert store2._data is not None
