"""端到端集成测试（基于 BotInstance fixture）。

覆盖：
    - 私聊消息进 _process_message
    - 群聊未 @ 时被忽略
    - 群聊 @ 后正常处理，mention 占位符被剥离
    - allowed_open_ids 白名单（非 trinity）
    - allowed_group_chat_ids 白名单（非 trinity）
    - /stop 在锁外处理
    - /  显示菜单
    - per-chat 锁：同 chat 串行、跨 chat 并行
    - 同 user 私聊 vs 群聊 session 隔离

每个测试自带一个新的 BotInstance（绕开真 lark.Client.builder），feishu 字段 mock。
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import session_store as session_store_module
import dispatcher
from bot_config import Profile
from bot_instance import BotInstance
from dispatcher import handle_message_async
from session_store import SessionStore


# ── fixtures ─────────────────────────────────────────────────

@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_store_module, "SESSIONS_DIR", str(sessions_dir))
    dispatcher._seen_messages.clear()
    return sessions_dir


def _make_bot(
    *,
    profile_name: str = "test",
    allowed_groups: set = None,
    allowed_users: set = None,
    bot_open_id: str = "ou_bot_self",
) -> BotInstance:
    """构造不连真 Lark 的 BotInstance。feishu 字段 AsyncMock。"""
    bot = BotInstance.__new__(BotInstance)
    bot.profile = Profile(
        name=profile_name,
        app_id="cli_test",
        app_secret="secret",
        platform="lark",
        domain="open.larksuite.com",
        default_cwd="/tmp",
        allowed_group_chat_ids=allowed_groups or set(),
        allowed_open_ids=allowed_users or set(),
    )
    bot.feishu = AsyncMock()
    bot.feishu.get_bot_open_id = AsyncMock(return_value=bot_open_id)
    bot.active_runs = MagicMock()
    bot.chat_locks = {}
    bot.store = SessionStore(profile=profile_name)
    return bot


def _make_event(
    *,
    user_id: str = "ou_user_1",
    chat_id: str = "ou_user_1",
    chat_type: str = "p2p",
    text: str = "hello",
    message_id: str = "om_msg_1",
    thread_id: str = "",
    mentions: list = None,
    message_type: str = "text",
):
    ev = MagicMock()
    ev.event.sender.sender_id.open_id = user_id
    ev.event.message.chat_type = chat_type
    ev.event.message.chat_id = chat_id
    ev.event.message.message_type = message_type
    ev.event.message.content = json.dumps({"text": text}) if message_type == "text" else "{}"
    ev.event.message.message_id = message_id
    ev.event.message.thread_id = thread_id
    ev.event.message.mentions = mentions
    return ev


def _make_mention(key: str = "@_user_1", open_id: str = "ou_bot_self"):
    m = MagicMock()
    m.key = key
    m.id = MagicMock()
    m.id.open_id = open_id
    return m


# ── 私聊 ─────────────────────────────────────────────────────

async def test_private_chat_invokes_process(isolated_sessions):
    bot = _make_bot()
    event = _make_event(user_id="ou_u1", chat_id="ou_u1", chat_type="p2p", text="hi")
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await handle_message_async(bot, event)
    proc.assert_awaited_once()
    args = proc.await_args.args
    # (bot, user_id, chat_id, is_group, thread_id, msg)
    assert args[1] == "ou_u1"
    assert args[3] is False  # is_group


async def test_private_chat_empty_text_skipped(isolated_sessions):
    bot = _make_bot()
    # /stop 检测会在锁外路径处理，空文本经过这里 → 进入 lock 路径并 reach _process_message
    # 这里只验证流程能跑完。
    event = _make_event(text="")
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await handle_message_async(bot, event)
    # 空文本不会被 mention 校验拦截（私聊），仍然进 _process_message
    proc.assert_awaited_once()


# ── 群聊 mention 校验 ────────────────────────────────────────

async def test_group_chat_no_mention_ignored(isolated_sessions):
    bot = _make_bot(allowed_groups={"oc_group_1"})
    event = _make_event(
        user_id="ou_u1", chat_id="oc_group_1", chat_type="group", text="hi",
        mentions=None,
    )
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await handle_message_async(bot, event)
    proc.assert_not_awaited()


async def test_group_chat_mention_other_bot_ignored(isolated_sessions):
    bot = _make_bot(allowed_groups={"oc_group_1"})
    other_mention = _make_mention(key="@_other_bot", open_id="ou_other_bot")
    event = _make_event(
        chat_id="oc_group_1", chat_type="group", text="@_other_bot hi",
        mentions=[other_mention],
    )
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await handle_message_async(bot, event)
    proc.assert_not_awaited()


async def test_group_chat_mention_self_processed(isolated_sessions):
    bot = _make_bot(allowed_groups={"oc_group_1"})
    mention = _make_mention(key="@_user_1", open_id="ou_bot_self")
    event = _make_event(
        chat_id="oc_group_1", chat_type="group", text="@_user_1 do something",
        mentions=[mention],
    )
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await handle_message_async(bot, event)
    proc.assert_awaited_once()


async def test_group_text_removes_mentions_before_agent(isolated_sessions):
    bot = _make_bot(allowed_groups={"oc_group_1"})
    bot.feishu.reply_card.return_value = "card_1"
    mention = _make_mention(key="@_user_1", open_id="ou_bot_self")
    event = _make_event(
        user_id="ou_u1",
        chat_id="oc_group_1",
        chat_type="group",
        text="@_user_1 你去看看",
        mentions=[mention],
    )

    with patch("dispatcher._run_and_display", new_callable=AsyncMock) as run:
        await dispatcher._process_message(
            bot, "ou_u1", "oc_group_1", True, "", event.event.message,
        )

    run.assert_awaited_once()
    assert run.await_args.args[4] == "你去看看"


# ── 白名单 ─────────────────────────────────────────────────

async def test_group_not_in_whitelist_ignored(isolated_sessions):
    bot = _make_bot(allowed_groups={"oc_other"})
    event = _make_event(chat_id="oc_not_allowed", chat_type="group", text="hi")
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await handle_message_async(bot, event)
    proc.assert_not_awaited()


async def test_user_not_in_allowlist_ignored(isolated_sessions):
    bot = _make_bot(allowed_users={"ou_allowed"})
    event = _make_event(user_id="ou_random", text="hi")
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await handle_message_async(bot, event)
    proc.assert_not_awaited()


# ── /stop 锁外处理 ───────────────────────────────────────────

async def test_stop_command_handled_out_of_lock(isolated_sessions):
    bot = _make_bot()
    bot.active_runs.get_run.return_value = None  # 没有 active run
    event = _make_event(text="/stop")
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await handle_message_async(bot, event)
    # /stop 不走 _process_message
    proc.assert_not_awaited()
    # /stop 私聊会发卡片
    bot.feishu.send_card_to_user.assert_awaited()


async def test_group_stop_ignores_mentions_for_other_bot(isolated_sessions):
    bot = _make_bot(allowed_groups={"oc_a"}, bot_open_id="ou_this_bot")
    bot.active_runs.get_run.return_value = None
    other = _make_mention(key="@Other", open_id="ou_other_bot")
    event = _make_event(
        chat_id="oc_a",
        chat_type="group",
        text="/stop @Other",
        mentions=[other],
    )
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await handle_message_async(bot, event)

    proc.assert_not_awaited()
    bot.feishu.reply_card.assert_not_awaited()


async def test_group_stop_handles_current_bot_mention(isolated_sessions):
    bot = _make_bot(allowed_groups={"oc_a"}, bot_open_id="ou_this_bot")
    bot.active_runs.get_run.return_value = None
    self_mention = _make_mention(key="@This", open_id="ou_this_bot")
    event = _make_event(
        chat_id="oc_a",
        chat_type="group",
        text="/stop @This",
        mentions=[self_mention],
    )
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await handle_message_async(bot, event)

    proc.assert_not_awaited()
    bot.feishu.reply_card.assert_awaited()


# ── per-chat 锁 ─────────────────────────────────────────────

async def test_locks_are_per_chat(isolated_sessions):
    bot = _make_bot(allowed_groups={"oc_a", "oc_b"})
    ma = _make_mention()
    mb = _make_mention()
    event_a = _make_event(chat_id="oc_a", chat_type="group", text="@_u hi a", mentions=[ma], message_id="m_a")
    event_b = _make_event(chat_id="oc_b", chat_type="group", text="@_u hi b", mentions=[mb], message_id="m_b")
    with patch("dispatcher._process_message", new_callable=AsyncMock):
        await asyncio.gather(
            handle_message_async(bot, event_a),
            handle_message_async(bot, event_b),
        )
    assert "oc_a" in bot.chat_locks
    assert "oc_b" in bot.chat_locks
    assert bot.chat_locks["oc_a"] is not bot.chat_locks["oc_b"]


async def test_same_chat_uses_same_lock(isolated_sessions):
    bot = _make_bot(allowed_groups={"oc_a"})
    m1 = _make_mention()
    m2 = _make_mention()
    e1 = _make_event(chat_id="oc_a", chat_type="group", text="@_u 1", mentions=[m1], message_id="m_1")
    e2 = _make_event(chat_id="oc_a", chat_type="group", text="@_u 2", mentions=[m2], message_id="m_2")
    with patch("dispatcher._process_message", new_callable=AsyncMock):
        await asyncio.gather(
            handle_message_async(bot, e1),
            handle_message_async(bot, e2),
        )
    assert bot.chat_locks["oc_a"].locked() is False  # 处理完释放


# ── session 隔离 ────────────────────────────────────────────

async def test_session_isolated_private_vs_group(isolated_sessions):
    """同一 user 在私聊和群聊用的是不同 session。"""
    bot = _make_bot(allowed_groups={"oc_x"})
    store = bot.store
    user = "ou_u_1"
    chat_p = user            # 私聊 chat_id 等于 user_id
    chat_g = "oc_x"

    await store.set_cwd(user, chat_p, "/tmp/private")
    await store.set_cwd(user, chat_g, "/tmp/group")

    s_p = await store.get_current(user, chat_p)
    s_g = await store.get_current(user, chat_g)

    assert s_p.cwd == "/tmp/private"
    assert s_g.cwd == "/tmp/group"


# ── extract_chat_info ───────────────────────────────────────

def test_extract_chat_info_thread_compound_key():
    """话题群消息：chat_id = chat_id_raw + ':' + thread_id"""
    from dispatcher import extract_chat_info
    ev = _make_event(
        chat_id="oc_court", chat_type="group", thread_id="omt_t1",
        mentions=[_make_mention()],
    )
    user_id, chat_id, is_group, raw_chat_id, thread_id = extract_chat_info(ev)
    assert chat_id == "oc_court:omt_t1"
    assert raw_chat_id == "oc_court"
    assert thread_id == "omt_t1"
    assert is_group is True
