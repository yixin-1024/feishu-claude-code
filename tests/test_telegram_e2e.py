"""端到端：一条真实形状的 Telegram update 走完 dispatcher 全程。

这是"接 Telegram 不改业务层"这个设计成立与否的验收测试：真的 BotInstance、
真的 SessionStore、真的 dispatcher，只把两头打桩 —— HTTP（_post_sync）和
agent（run_agent）。跑通说明队列 / 卡片 / session / 上下文注入 / 斜杠命令
在新渠道上和 Lark 走的是同一条代码路径。
"""

import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher
import telegram_gateway as gw
from bot_config import Profile
from bot_instance import BotInstance


class FakeTransport:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 500

    def __call__(self, method, payload, timeout, fresh=False):
        self.calls.append((method, payload))
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id, "chat": {"id": int(payload["chat_id"])}}
        return True

    def of(self, method):
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def bot(monkeypatch):
    profile = Profile(
        name="tg", app_id="42", app_secret="42:secret", platform="telegram",
        domain="https://api.telegram.org", default_cwd="/tmp/default",
        bot_token="42:secret",
        allowed_open_ids={"777"},
        allowed_group_chat_ids={"-100123", "-100999"},
        chat_default_cwd={"-100123": "/tmp/proj-a", "-100999": "/tmp/proj-b"},
    )
    b = BotInstance(profile)
    b.feishu._bot_id = 42
    b.feishu._app_id = "42"
    b.feishu._bot_username = "spx_bot"
    monkeypatch.setattr(b.feishu, "_post_sync", FakeTransport())
    return b


def _msg(mid: int, text: str, chat=-100123, uid: int = 777, **kw) -> dict:
    """chat 传数字 = 群，传 dict = 原样用（私聊传 {"id": .., "type": "private"}）。"""
    base = {
        "message_id": mid,
        "date": 1700000000 + mid,
        "chat": chat if isinstance(chat, dict) else {"id": chat, "type": "supergroup"},
        "from": {"id": uid, "first_name": "Yixin"},
        "text": text,
    }
    base.update(kw)
    return base


def _fake_agent(recorder: list, reply: str = "结论：没问题"):
    async def run_agent(**kwargs):
        recorder.append(kwargs)
        chunk = kwargs.get("on_text_chunk")
        if chunk:
            await chunk(reply)
        return reply, "sess-1", False
    return run_agent


async def _deliver(bot, msg: dict):
    event = gw.build_event(bot, msg)
    assert event is not None
    await dispatcher.handle_message_async(bot, event)


# ── 触发规则 ─────────────────────────────────────────────────

async def test_group_message_without_mention_does_not_run(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        await _deliver(bot, _msg(1, "我们晚点聊支付那块"))
    assert calls == []
    assert bot.feishu._post_sync.of("sendMessage") == []


async def test_mention_runs_and_streams_into_one_message(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        await _deliver(bot, _msg(2, "@spx_bot 现在怎么样"))

    transport = bot.feishu._post_sync
    sends = transport.of("sendMessage")
    # 一条占位消息，reply 到用户那条上
    assert sends[0]["reply_parameters"]["message_id"] == 2
    assert sends[0]["chat_id"] == "-100123"
    # 正文通过 edit 落到同一条消息里（不是每帧新发一条）
    edits = transport.of("editMessageText")
    assert edits, "应该 edit 占位消息而不是刷新消息"
    assert "结论：没问题" in edits[-1]["text"]
    assert edits[-1]["message_id"] == 501

    # @ 占位符被剥掉，prompt 里不留 @spx_bot
    prompt = calls[0]["message"]
    assert "现在怎么样" in prompt
    assert "@spx_bot" not in prompt


async def test_private_message_needs_no_mention(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        await _deliver(bot, _msg(3, "在吗", chat={"id": 777, "type": "private"}))
    assert len(calls) == 1
    assert "在吗" in calls[0]["message"]


async def test_unauthorized_user_in_allowed_group_is_ignored(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        await _deliver(bot, _msg(4, "@spx_bot 帮我删库", uid=888))
    assert calls == []


# ── 上下文（Telegram 没有话题群，靠自己的缓冲）───────────────

async def test_unseen_group_messages_are_injected_as_context(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        # 三条闲聊没 @ bot（不触发，但要进上下文）
        await _deliver(bot, _msg(10, "SGB 那个开户失败了"))
        await _deliver(bot, _msg(11, "报错是 address 缺字段", uid=888))
        await _deliver(bot, _msg(12, "@spx_bot 看下这个问题"))

    prompt = calls[0]["message"]
    assert "SGB 那个开户失败了" in prompt
    assert "address 缺字段" in prompt
    assert "看下这个问题" in prompt
    # 姓名解析走缓冲里记下的 Telegram 显示名
    assert "Yixin" in prompt


async def test_second_turn_only_carries_new_messages(bot):
    """last_seen 水位线：第二轮不该把第一轮已经读过的消息再喂一遍。"""
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        await _deliver(bot, _msg(20, "第一条背景"))
        await _deliver(bot, _msg(21, "@spx_bot 先看这个"))
        await _deliver(bot, _msg(22, "补充一句新的"))
        await _deliver(bot, _msg(23, "@spx_bot 再看"))

    assert "第一条背景" in calls[0]["message"]
    assert "第一条背景" not in calls[1]["message"]
    assert "补充一句新的" in calls[1]["message"]


async def test_bot_own_answer_is_visible_to_later_context(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls, "答案是 42")):
        await _deliver(bot, _msg(30, "@spx_bot 问题一"))
        await _deliver(bot, _msg(31, "还有别的吗", uid=888))
        await _deliver(bot, _msg(32, "@spx_bot 问题二"))
    assert "答案是 42" in calls[1]["message"]


# ── 每个群一个工作目录 ───────────────────────────────────────

async def test_each_group_gets_its_own_workspace_and_session(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        await _deliver(bot, _msg(40, "@spx_bot a", chat=-100123))
        await _deliver(bot, _msg(41, "@spx_bot b", chat=-100999))
    assert calls[0]["cwd"] == "/tmp/proj-a"
    assert calls[1]["cwd"] == "/tmp/proj-b"

    # session 也各自独立（第二个群不该续第一个群的 session）
    assert calls[0]["session_id"] is None
    assert calls[1]["session_id"] is None
    s_a = await bot.store.get_current("777", "-100123:c-100123")
    s_b = await bot.store.get_current("777", "-100999:c-100999")
    assert s_a.cwd == "/tmp/proj-a" and s_b.cwd == "/tmp/proj-b"


async def test_same_group_second_turn_resumes_session(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        await _deliver(bot, _msg(50, "@spx_bot 一"))
        await _deliver(bot, _msg(51, "@spx_bot 二"))
    assert calls[1]["session_id"] == "sess-1"


# ── 斜杠命令 ─────────────────────────────────────────────────

async def test_slash_command_runs_without_calling_agent(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        await _deliver(bot, _msg(60, "/ws", entities=[
            {"type": "bot_command", "offset": 0, "length": 3}]))
    assert calls == []
    sends = bot.feishu._post_sync.of("sendMessage")
    assert len(sends) == 1
    assert "/tmp/proj-a" in sends[0]["text"]


async def test_ws_set_changes_this_group_only(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        await _deliver(bot, _msg(61, "@spx_bot /ws set /tmp"))
        await _deliver(bot, _msg(62, "@spx_bot 干活"))
        await _deliver(bot, _msg(63, "@spx_bot 干活", chat=-100999))
    assert calls[0]["cwd"] == "/tmp"
    assert calls[1]["cwd"] == "/tmp/proj-b"


async def test_stop_command_is_answered_outside_the_queue(bot):
    with mock.patch.object(dispatcher, "run_agent", _fake_agent([])):
        await _deliver(bot, _msg(70, "/stop", entities=[
            {"type": "bot_command", "offset": 0, "length": 5}]))
    sends = bot.feishu._post_sync.of("sendMessage")
    assert len(sends) == 1
    assert "没有" in sends[0]["text"] or "任务" in sends[0]["text"]


# ── 系统提示 ─────────────────────────────────────────────────

async def test_system_prompt_is_the_telegram_flavour(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        await _deliver(bot, _msg(80, "@spx_bot hi"))
    sys_prompt = calls[0]["append_system_prompt"]
    assert "你正在通过 Telegram 与用户对话" in sys_prompt
    assert "tg-cli" in sys_prompt
    assert "lark-cli" not in sys_prompt
    assert "@spx_bot" in sys_prompt
    # 运行时 MCP / 运行环境约束两段与 Lark 共用，不能因为换渠道就丢
    assert "wake_me_in" in sys_prompt
    assert "禁止运行阻塞式长驻命令" in sys_prompt


async def test_wake_context_points_at_this_telegram_chat(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        await _deliver(bot, _msg(90, "@spx_bot hi"))
    ctx = calls[0]["wake_context"]
    assert ctx["CC_LARK_CHAT_ID"] == "-100123"
    assert ctx["CC_LARK_THREAD_ID"] == "c-100123"
    assert ctx["CC_LARK_USER_ID"] == "777"
    # 回复锚点是复合 key，tg-cli 靠它同时拿到 chat 和 message id
    assert ctx["CC_LARK_MESSAGE_ID"] == "-100123:90"


# ── dispatch_task（Telegram 没有话题群，靠合成 thread 隔离子会话）───────

async def test_dispatch_task_child_runs_in_its_own_session(bot):
    """派单：顶楼消息自成一条 thread，子会话不和群里的主对话抢 session。"""
    with mock.patch.object(dispatcher, "handle_spawn",
                           new_callable=mock.AsyncMock) as spawn:
        result = await dispatcher.dispatch_task(
            bot, user_id="777", group_chat_id="-100123",
            title="查余额", prompt="去查一下 uqpay 余额",
        )
    assert result["ok"] is True
    thread = result["thread_id"]
    assert thread.startswith("t") and thread != "c-100123"

    kwargs = spawn.call_args.kwargs
    assert kwargs["chat_id_raw"] == "-100123"
    assert kwargs["thread_id"] == thread
    assert kwargs["anchor_message_id"] == result["anchor_message_id"]

    # 顶楼消息就是发到这个群里的一条普通消息
    sends = bot.feishu._post_sync.of("sendMessage")
    assert sends[0]["chat_id"] == "-100123"
    assert "查余额" in sends[0]["text"]


async def test_reply_to_a_dispatch_child_lands_in_the_child_session(bot):
    """在子会话的消息上回复 = 继续跟子会话说话，不该串回群主线。"""
    with mock.patch.object(dispatcher, "handle_spawn", new_callable=mock.AsyncMock):
        result = await dispatcher.dispatch_task(
            bot, user_id="777", group_chat_id="-100123",
            title="子任务", prompt="干活",
        )
    anchor_mid = int(result["anchor_message_id"].split(":")[1])

    calls = []
    with mock.patch.object(dispatcher, "run_agent", _fake_agent(calls)):
        await _deliver(bot, _msg(
            100, "换个方向",
            reply_to_message={"message_id": anchor_mid, "from": {"id": 42}},
        ))
    assert calls, "回复子会话的消息应该触发（回复 bot 等于 @ 它）"
    ctx = calls[0]["wake_context"]
    assert ctx["CC_LARK_THREAD_ID"] == result["thread_id"]
