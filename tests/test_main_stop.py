"""_handle_stop_command 单测：BotInstance 携带 active_runs，签名是
(bot, sender_open_id, chat_id) -> str。"""

import os
import sys
from unittest import mock

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher
from bot_config import Profile
from bot_instance import BotInstance


def _bot() -> BotInstance:
    """构造一个不真正连 Lark 的 BotInstance（绕开 __init__）。"""
    bot = BotInstance.__new__(BotInstance)
    bot.profile = Profile(
        name="test", app_id="cli_x", app_secret="s",
        platform="lark", domain="open.larksuite.com", default_cwd="/tmp",
    )
    bot.feishu = mock.AsyncMock()
    bot.active_runs = mock.MagicMock()
    bot.chat_locks = {}
    return bot


async def test_no_active_run():
    bot = _bot()
    bot.active_runs.get_run.return_value = None
    reply = await dispatcher._handle_stop_command(bot, "user-1", "chat-1")
    assert "没有正在运行" in reply


async def test_active_run_already_stopping():
    bot = _bot()
    bot.active_runs.get_run.return_value = mock.Mock(stop_requested=True)
    reply = await dispatcher._handle_stop_command(bot, "user-1", "chat-1")
    assert "正在停止" in reply


async def test_active_run_stopped_successfully():
    bot = _bot()
    bot.active_runs.get_run.return_value = mock.Mock(stop_requested=False)
    with mock.patch.object(dispatcher, "stop_run", mock.AsyncMock(return_value=True)) as stop_run:
        reply = await dispatcher._handle_stop_command(bot, "user-1", "chat-1")
    stop_run.assert_awaited_once()
    assert "已发送停止请求" in reply


async def test_active_run_already_terminated_between_check_and_call():
    """race: get_run 返回 active，但 stop_run 拿不到（已结束）。"""
    bot = _bot()
    bot.active_runs.get_run.return_value = mock.Mock(stop_requested=False)
    with mock.patch.object(dispatcher, "stop_run", mock.AsyncMock(return_value=False)):
        reply = await dispatcher._handle_stop_command(bot, "user-1", "chat-1")
    assert "没有正在运行" in reply


async def test_heartbeat_does_not_overwrite_error_card_on_watchdog_kill():
    """复现 2026-05-17 13:36 卡片显示进行中而不是 ❌ 的 bug：watchdog 抛 RuntimeError
    后，dispatcher 在 except 里 update_card(❌)，**同时**心跳还活着、每 1.5s push
    一次 _build_display() 的进行中画面，下一次心跳 push 会把 ❌ 覆盖回去。

    修复：进 except 后必须**先** await heartbeat_task.cancel() 等心跳真退出，再 patch ❌。
    """
    import asyncio

    card_history: list[str] = []

    async def fake_update_card(_msg_id, content):
        # 心跳 push 用快 API 模拟（生产 ~100ms）；error patch 慢一点（让 race window 打开）。
        # 关键：err patch 的 await 期间，心跳能从 sleep 醒过来再 push 一次。
        card_history.append(content)
        if "❌" in content:
            await asyncio.sleep(2.5)   # error patch 故意慢，让心跳有窗口插队
        else:
            await asyncio.sleep(0.05)  # 心跳 push 快返回

    bot = _bot()
    bot.feishu.update_card = fake_update_card          # 心跳「进行中」push 走这里
    bot.feishu.update_card_final = fake_update_card     # 终态 ❌ 收尾写走这里
    bot.feishu.update_card_with_buttons = mock.AsyncMock()
    bot.feishu.reply_text = mock.AsyncMock()
    bot.feishu.reply_card = mock.AsyncMock(return_value="card_id_1")

    active_run = mock.Mock(stop_requested=False)
    active_run.card_update_lock = asyncio.Lock()
    bot.active_runs.start_run.return_value = active_run

    session = mock.Mock(session_id=None, model="claude-opus-4-7[1m]", cwd="/tmp",
                       permission_mode="bypassPermissions", runner="claude")

    async def fake_run_agent(**kwargs):
        # 让心跳跑几轮再抛 watchdog 异常
        await asyncio.sleep(2.5)
        raise RuntimeError("Claude 客户端疑似 hung：90s 三信号全 0 增长")

    with mock.patch.object(dispatcher, "run_agent", fake_run_agent):
        await dispatcher._run_and_display(
            bot,
            user_id="u1", chat_id="c1", is_group=True,
            text="hi", card_msg_id="card_id_1", session=session,
            notify_msg_id="msg_orig",
        )

    assert card_history, "dispatcher 完全没 patch 卡片"
    # 关键断言：最后一次 update_card 必须是 ❌ 错误消息（auto-retry 走完所有重试后才会出现），
    # 不能被心跳的"进行中"画面覆盖。措辞会因 retry_count 不同而不同
    # （"❌ Claude 执行出错..." 或 "❌ 自动重试 N 次后仍失败..."），都接受。
    last_card = card_history[-1]
    assert last_card.startswith("❌") and "hung" in last_card, (
        f"心跳竞态把错误 patch 覆盖了。最终卡片内容：{last_card!r}"
    )
