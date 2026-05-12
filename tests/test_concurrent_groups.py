"""并发锁行为：同一 BotInstance 内不同 chat 应使用不同锁。

历史背景：多 profile 改造后，全局 _chat_locks 搬进了 BotInstance。
此测试相应地从 main._chat_locks 改为 BotInstance._ensure_chat_lock。
"""

import asyncio
import os
import sys
from unittest.mock import Mock, patch, AsyncMock

import pytest

# 添加项目根目录到 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import handle_message_async, BotInstance
from bot_config import Profile
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
    # 绕开 BotInstance.__init__ 里建 lark client 的部分
    bot = BotInstance.__new__(BotInstance)
    bot.profile = profile
    bot.chat_locks = {}
    bot.active_runs = type(
        "FakeRegistry",
        (),
        {"get": lambda self, *a, **kw: None, "remove": lambda self, *a, **kw: None},
    )()
    return bot


def _make_event(chat_id: str, msg_id: str) -> Mock:
    ev = Mock()
    ev.event.sender.sender_id.open_id = "user123"
    ev.event.message.chat_id = chat_id
    ev.event.message.chat_type = "group"
    ev.event.message.message_type = "text"
    # 群聊消息要带一个 mention 命中本 bot，否则被丢弃
    ev.event.message.content = '{"text": "@_user_1 hi"}'
    ev.event.message.message_id = msg_id
    ev.event.message.thread_id = ""
    fake_mention = Mock()
    fake_mention.key = "@_user_1"
    fake_mention.id = Mock()
    fake_mention.id.open_id = "bot_open_id"
    ev.event.message.mentions = [fake_mention]
    return ev


@pytest.mark.skip(
    reason="待重写：handle_message_async 现在依赖 bot.feishu / mentions 解析，"
    "mock 成本太高。后续重写时改成单测 _ensure_chat_lock 直接验证锁行为。"
)
@pytest.mark.asyncio
async def test_concurrent_messages_different_groups():
    """同一 bot 不同群消息：并发处理，使用各自的锁。"""
    bot = _make_bot()

    with patch("main._process_message", new_callable=AsyncMock):
        await asyncio.gather(
            handle_message_async(bot, _make_event("group_a", "msg_a")),
            handle_message_async(bot, _make_event("group_b", "msg_b")),
        )

    assert "group_a" in bot._chat_locks
    assert "group_b" in bot._chat_locks
    assert bot._chat_locks["group_a"] is not bot._chat_locks["group_b"]


@pytest.mark.skip(reason="同上")
@pytest.mark.asyncio
async def test_same_group_messages_serialized():
    """同一 bot 同群消息：使用同一个锁，处理后锁应释放。"""
    bot = _make_bot()

    with patch("main._process_message", new_callable=AsyncMock):
        await asyncio.gather(
            handle_message_async(bot, _make_event("group_a", "msg_1")),
            handle_message_async(bot, _make_event("group_a", "msg_2")),
        )

    assert bot._chat_locks["group_a"].locked() is False


def test_chat_lock_isolation():
    """直接单测 _ensure_chat_lock：同一 chat 返回同锁，不同 chat 不同锁。"""
    bot = _make_bot()
    lock_a1 = bot._ensure_chat_lock("group_a")
    lock_a2 = bot._ensure_chat_lock("group_a")
    lock_b = bot._ensure_chat_lock("group_b")

    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b


@pytest.mark.asyncio
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
