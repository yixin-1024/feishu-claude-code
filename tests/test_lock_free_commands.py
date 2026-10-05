"""只读斜杠命令免排队：/usage /status 这类命令不该跟在 per-chat 锁后面等。

背景（2026-09-16 实测）：前面一条消息正在跑 agent 时，用户发 `/usage`，卡片只回
「📬 前面还有任务在跑，排队中」，要等那个任务整个跑完才出用量。可这类命令跟正在
跑的任务毫无关联，也不动 session —— 该并行。

铁律（下面每条都有用例钉住）：
  1) 只读命令在锁被占着时也能立刻出结果，且**不进** _process_message；
  2) 会写 session 的命令（/new、/model opus …）仍老老实实排队，绝不能并发改写
     store —— 正在跑的 run 结束时会把 session_id 写回去，并发写必串台；
  3) 群里仍要求 @ 到本 bot 才响应。
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher
from bot_config import Profile
from bot_instance import BotInstance
from commands import is_lock_free_command


def _make_bot() -> BotInstance:
    bot = BotInstance.__new__(BotInstance)
    bot.profile = Profile(
        name="test", app_id="cli_test", app_secret="secret",
        platform="lark", domain="open.larksuite.com", default_cwd="/tmp",
        allowed_group_chat_ids={"group_a"},
    )
    bot.chat_locks = {}
    bot.active_runs = MagicMock()
    bot.store = MagicMock()
    bot.feishu = AsyncMock()
    bot.feishu.get_bot_open_id = AsyncMock(return_value="bot_open_id")
    return bot


def _make_event(text: str, msg_id: str, chat_id: str = "group_a") -> Mock:
    ev = Mock()
    ev.event.sender.sender_id.open_id = "user123"
    ev.event.message.chat_id = chat_id
    ev.event.message.chat_type = "group"
    ev.event.message.message_type = "text"
    ev.event.message.content = '{"text": "@_user_1 %s"}' % text
    ev.event.message.message_id = msg_id
    ev.event.message.thread_id = ""
    mention = Mock()
    mention.key = "@_user_1"
    mention.id = Mock()
    mention.id.open_id = "bot_open_id"
    ev.event.message.mentions = [mention]
    return ev


# ── 判定函数本身 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,args", [
    ("usage", ""), ("status", ""), ("help", ""), ("h", ""),
    ("skills", ""), ("mcp", ""), ("accounts", ""), ("ls", "src"),
    ("model", ""), ("mode", ""), ("effort", ""), ("runner", ""),
    ("resume", ""), ("ws", ""), ("workspace", ""),
])
def test_readonly_commands_are_lock_free(cmd, args):
    assert is_lock_free_command(cmd, args) is True


@pytest.mark.parametrize("cmd,args", [
    # 带参数 = 会写 session，必须排队
    ("model", "opus"), ("mode", "plan"), ("effort", "high"),
    ("runner", "codex"), ("resume", "3"), ("workspace", "spx"),
    # 本来就会改状态 / 跑外部命令的
    ("new", ""), ("clear", ""), ("defaults", ""), ("cd", "/tmp"),
    ("exec", "git status"), ("switch", "info"), ("group", "add x"),
    ("opus", ""), ("commit", ""),
])
def test_mutating_commands_still_queue(cmd, args):
    assert is_lock_free_command(cmd, args) is False


# ── 端到端：锁被占着时的真实行为 ────────────────────────────────────────

async def test_usage_answers_while_a_run_holds_the_lock():
    """锁被前一个任务占着，/usage 仍立刻出卡片，且不走 _process_message。"""
    bot = _make_bot()
    lock = bot._ensure_chat_lock("group_a")

    with patch("dispatcher.handle_command", new_callable=AsyncMock) as hc, \
         patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        hc.return_value = "📊 5h 12%"
        async with lock:                       # 模拟前面那个任务正在跑
            await asyncio.wait_for(
                dispatcher.handle_message_async(bot, _make_event("/usage", "m1")),
                timeout=2,
            )

    hc.assert_awaited_once()
    assert hc.await_args.args[0] == "usage"
    proc.assert_not_awaited()                  # 没进队列路径
    bot.feishu.reply_card.assert_awaited_once()
    assert bot.feishu.reply_card.await_args.kwargs["content"] == "📊 5h 12%"
    # 也不该丢一条"排队中"噪音
    assert bot.feishu.reply_text.await_count == 0


async def test_mutating_command_still_waits_for_the_lock():
    """/new 会写 session，锁被占着就必须等 —— 不能跟着只读命令一起放行。"""
    bot = _make_bot()
    lock = bot._ensure_chat_lock("group_a")

    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc:
        await lock.acquire()
        task = asyncio.create_task(
            dispatcher.handle_message_async(bot, _make_event("/new", "m2"))
        )
        await asyncio.sleep(0.05)
        proc.assert_not_awaited()              # 还卡在锁上
        assert any("排队中" in str(c) for c in bot.feishu.reply_text.await_args_list)
        lock.release()
        await asyncio.wait_for(task, timeout=2)

    proc.assert_awaited_once()


async def test_usage_runs_in_parallel_with_a_slow_run():
    """并行是真的并行：/usage 不必等占锁的任务结束。"""
    bot = _make_bot()
    lock = bot._ensure_chat_lock("group_a")
    finished_order: list[str] = []

    async def slow_hold():
        async with lock:
            await asyncio.sleep(0.3)
        finished_order.append("run")

    async def fake_cmd(*a, **kw):
        finished_order.append("usage")
        return "📊 ok"

    with patch("dispatcher.handle_command", new=fake_cmd):
        holder = asyncio.create_task(slow_hold())
        await asyncio.sleep(0.05)
        await asyncio.wait_for(
            dispatcher.handle_message_async(bot, _make_event("/status", "m3")),
            timeout=1,
        )
        await holder

    assert finished_order == ["usage", "run"], "只读命令还是排在任务后面了"


async def test_group_still_requires_mention():
    """群里没 @ 到本 bot 的 /usage 不响应（和 /stop /restart 一致）。"""
    bot = _make_bot()
    ev = _make_event("/usage", "m4")
    ev.event.message.mentions[0].id.open_id = "some_other_bot"

    with patch("dispatcher.handle_command", new_callable=AsyncMock) as hc:
        await dispatcher.handle_message_async(bot, ev)

    hc.assert_not_awaited()
    bot.feishu.reply_card.assert_not_awaited()


async def test_lock_free_command_error_is_reported_not_swallowed():
    """只读命令炸了要把错误回给用户，不能静默（它已经不在外层 try 里了）。"""
    bot = _make_bot()

    with patch("dispatcher.handle_command", new_callable=AsyncMock) as hc:
        hc.side_effect = RuntimeError("keychain 读不到")
        await dispatcher.handle_message_async(bot, _make_event("/usage", "m5"))

    bot.feishu.reply_card.assert_awaited_once()
    content = bot.feishu.reply_card.await_args.kwargs["content"]
    assert "❌" in content and "keychain 读不到" in content
