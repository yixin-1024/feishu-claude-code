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
