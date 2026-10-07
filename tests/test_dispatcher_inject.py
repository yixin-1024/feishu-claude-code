"""话题里任务在跑时的新消息：插进正在跑的进程 / ! 打断改道 / 插不进就排队。"""

import asyncio
import json
import os
import sys
from unittest import mock

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher
import run_control
from bot_config import Profile
from bot_instance import BotInstance
from run_control import ActiveRun, ActiveRunRegistry

CHAT = "oc_group:omt_thread"


class FakeInput:
    def __init__(self, open_=True, uid="uid-1"):
        self.open = open_
        self.uid = uid
        self.sent: list[tuple[str, str]] = []
        self.steered: list[str] = []

    async def send(self, text, priority="next"):
        self.sent.append((text, priority))
        return self.uid

    async def steer(self, text):
        self.steered.append(text)
        return self.uid


def _bot():
    bot = BotInstance.__new__(BotInstance)
    bot.profile = Profile(
        name="test", app_id="cli_x", app_secret="s",
        platform="lark", domain="open.larksuite.com", default_cwd="/tmp",
    )
    bot.feishu = mock.AsyncMock()
    bot.active_runs = ActiveRunRegistry()
    bot.store = mock.MagicMock()
    return bot


def _msg(text, mid="om_new", mtype="text"):
    content = json.dumps({"text": text}) if mtype == "text" else json.dumps({"image_key": "img_1"})
    return mock.Mock(message_type=mtype, content=content, message_id=mid, mentions=None)


def _running(bot, run_input):
    run = bot.active_runs.start_run("ou_user", CHAT, "card_1")
    run.input = run_input
    run.on_inject = mock.AsyncMock()
    return run


@pytest.fixture
def no_ctx(monkeypatch):
    """话题上下文原样返回（另一个用例单独测带附件的上下文）。"""
    async def fake_ctx(bot, user_id, chat_id, thread_id, msg, text):
        return text
    monkeypatch.setattr(dispatcher, "_attach_thread_context", fake_ctx)


async def _call(bot, msg, lock=None):
    lock = lock or asyncio.Lock()
    return await dispatcher._inject_or_steer(
        bot, "ou_user", CHAT, True, "omt_thread", msg, lock)


async def test_plain_message_is_injected_with_next(no_ctx):
    bot = _bot()
    fi = FakeInput()
    run = _running(bot, fi)
    assert await _call(bot, _msg("顺便把结果发我邮箱")) is True
    (body, prio), = fi.sent
    assert prio == "next"
    assert "顺便把结果发我邮箱" in body and dispatcher._INJECT_FRAME in body
    assert "消息 id: om_new" in body  # 新消息自己的回复锚点
    run.on_inject.assert_awaited_once_with("顺便把结果发我邮箱", False)
    ack = bot.feishu.reply_text.await_args.args
    assert ack[0] == "om_new" and "已插进" in ack[1]


@pytest.mark.parametrize("mark", ["!", "！"])
async def test_exclamation_steers(no_ctx, mark):
    bot = _bot()
    fi = FakeInput()
    run = _running(bot, fi)
    assert await _call(bot, _msg(f"{mark} 别查了，改成导出 CSV")) is True
    assert fi.sent == []
    (body,) = fi.steered
    assert dispatcher._STEER_FRAME in body and body.endswith("别查了，改成导出 CSV")
    assert mark not in body.split(dispatcher._STEER_FRAME, 1)[1]
    run.on_inject.assert_awaited_once_with("别查了，改成导出 CSV", True)
    assert "已打断" in bot.feishu.reply_text.await_args.args[1]


async def test_commands_and_idle_runs_are_left_alone(no_ctx):
    bot = _bot()
    assert await _call(bot, _msg("随便说句")) is False  # 没有在跑的任务
    fi = FakeInput()
    _running(bot, fi)
    assert await _call(bot, _msg("/status")) is False
    assert fi.sent == [] and fi.steered == []


async def test_not_injectable_plain_message_keeps_queueing(no_ctx):
    bot = _bot()
    _running(bot, None)  # 别的后端 / 没开 stream 输入
    assert await _call(bot, _msg("在吗")) is False


async def test_closed_input_falls_back_to_queue(no_ctx, monkeypatch):
    bot = _bot()
    fi = FakeInput(uid=None)  # 进程正好收尾：写不进去
    _running(bot, fi)
    started = mock.AsyncMock()
    monkeypatch.setattr(dispatcher, "_start_agent_run", started)
    lock = asyncio.Lock()
    assert await _call(bot, _msg("补一句"), lock) is True
    started.assert_awaited_once()
    assert started.await_args.args[6] == "补一句"  # 原文照常跑，没丢


async def test_steer_without_input_stops_then_resumes(no_ctx, monkeypatch):
    bot = _bot()
    _running(bot, None)
    stop = mock.AsyncMock(return_value=True)
    started = mock.AsyncMock()
    monkeypatch.setattr(dispatcher, "stop_run", stop)
    monkeypatch.setattr(dispatcher, "_start_agent_run", started)
    assert await _call(bot, _msg("！换个方向")) is True
    stop.assert_awaited_once()
    text = started.await_args.args[6]
    assert text.startswith(dispatcher._STEER_FRAME) and text.endswith("换个方向")


async def test_thread_context_with_attachments_is_injected(monkeypatch):
    """先发几张图再 @：上下文（含附件路径）跟着一起插进去，和正常消息一样。"""
    bot = _bot()
    fi = FakeInput()
    _running(bot, fi)
    seen = {}

    async def fake_ctx(bot_, user_id, chat_id, thread_id, msg, text):
        seen["text"] = text
        return f"【话题新增 · 2 条】\n[1] 图片 /tmp/a.png\n[2] 文件 /tmp/b.pdf\n\n【用户刚刚 @ 你并说】\n{text}"

    monkeypatch.setattr(dispatcher, "_attach_thread_context", fake_ctx)
    assert await _call(bot, _msg("看看这两个")) is True
    body = fi.sent[0][0]
    assert "/tmp/a.png" in body and "/tmp/b.pdf" in body and body.endswith("看看这两个")


async def test_compose_multi_turn_keeps_every_answer():
    acc = "先看下文件。完整的第一轮回答\n\n📩 **插话**：天气呢\n\n今天晴"
    proc, result = dispatcher._compose_multi_turn(acc, ["完整的第一轮回答", "今天晴"])
    assert "完整的第一轮回答" in result and "今天晴" in result
    assert result.index("完整的第一轮回答") < result.index("插话的回复") < result.index("今天晴")
    assert "先看下文件。" in proc and "📩 **插话**：天气呢" in proc
    assert "完整的第一轮回答" not in proc


class _SoftProc:
    pid = 999999

    def __init__(self):
        self.returncode = None

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.005)
        return self.returncode


async def test_stop_run_soft_interrupts_stream_input(monkeypatch):
    reg = ActiveRunRegistry()
    run = reg.start_run("u", "c", "card")
    proc = _SoftProc()
    run.proc = proc
    calls = []

    class Inp:
        open = True

        async def stop(self):
            calls.append("stop")
            proc.returncode = 1  # CLI 收到中断 + EOF 后自己退出（实测退出码 1）

    run.input = Inp()
    killed = []
    monkeypatch.setattr(run_control, "_kill_pgroup", lambda p, s: killed.append(s) or True)
    monkeypatch.setattr(run_control, "_kill_orphaned_group", lambda p: calls.append("reap"))
    monkeypatch.setattr(run_control, "_descendant_pids", lambda pid: [111, 222])
    monkeypatch.setattr(run_control, "_terminate_pids", lambda pids: calls.append(("term", pids)))
    assert await run_control.stop_run(reg, "u", "c") is True
    assert calls == ["stop", "reap", ("term", [111, 222])]
    assert killed == []  # 软停成功就不用 SIGTERM
    assert run.stop_requested
