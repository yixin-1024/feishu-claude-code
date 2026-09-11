"""TelegramClient：FeishuClient 的鸭子类型替身。

这些测试盯的是"换掉 bot.feishu 就能接新渠道"这个契约本身——方法名 / 返回值 /
幂等性必须和 Lark 那套一致，另外加 Telegram 特有的三道护栏：编辑节流、4096
分段、HTML 解析失败退回纯文本。HTTP 全部在 _post_sync 处打桩，不碰网络。
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_client as tc
from telegram_client import TelegramApiError, TelegramClient, make_key, split_key


class FakeTransport:
    """记录所有调用，按 method 返回可编排的结果。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.next_message_id = 100
        self.errors: dict[str, list[TelegramApiError]] = {}

    def __call__(self, method, payload, timeout, fresh=False):
        self.calls.append((method, payload))
        queued = self.errors.get(method)
        if queued:
            raise queued.pop(0)
        if method == "sendMessage":
            self.next_message_id += 1
            return {
                "message_id": self.next_message_id,
                "chat": {"id": int(payload["chat_id"])},
            }
        if method == "getMe":
            return {
                "id": 42, "username": "spx_bot", "first_name": "spx",
                "can_read_all_group_messages": True,
            }
        return True

    def of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def client(monkeypatch):
    c = TelegramClient("42:secret", label="tgtest")
    monkeypatch.setattr(c, "_post_sync", FakeTransport())
    monkeypatch.setattr(tc, "_EDIT_MIN_INTERVAL", 1.5)
    return c


def transport(client) -> FakeTransport:
    return client._post_sync


# ── key 编解码 ───────────────────────────────────────────────

def test_key_roundtrip():
    assert make_key(-1001234, 55) == "-1001234:55"
    assert split_key("-1001234:55") == ("-1001234", "55")
    # 纯 chat id（没有消息 id）也不能炸
    assert split_key("-1001234") == ("-1001234", "")


# ── 发送 ─────────────────────────────────────────────────────

async def test_send_card_to_user_returns_composite_key(client):
    key = await client.send_card_to_user("777", content="hi")
    assert key == "777:101"
    payload = transport(client).of("sendMessage")[0]
    assert payload["chat_id"] == "777"
    assert payload["text"] == "hi"
    assert payload["parse_mode"] == "HTML"


async def test_reply_card_sets_reply_parameters(client):
    key = await client.reply_card("-100:9", content="")
    assert key.startswith("-100:")
    payload = transport(client).of("sendMessage")[0]
    assert payload["reply_parameters"]["message_id"] == 9
    assert payload["reply_parameters"]["allow_sending_without_reply"] is True
    # loading 占位卡不能是空正文（Telegram 拒收空消息）
    assert payload["text"].strip()


async def test_outgoing_message_lands_in_context_buffer(client):
    """bot 自己的回复必须自己记 —— Telegram 不会把 bot 的消息推回来。"""
    key = await client.reply_card("-100:9", content="结论 A", loading=False)
    await client.update_card_final(key, "结论 B")
    msgs = await client.list_thread_messages("c-100")
    texts = [json.loads(m.body.content)["text"] for m in msgs]
    assert texts == ["结论 B"]
    assert msgs[0].sender.sender_type == "app"


# ── 编辑：节流 / 去重 / 收尾 ─────────────────────────────────

async def test_update_card_throttles_intermediate_frames(client):
    key = await client.send_card_to_user("777", content="a")
    await client.update_card(key, "frame-1")
    await client.update_card(key, "frame-2")   # 1.5s 内 → 丢帧
    edits = transport(client).of("editMessageText")
    assert [e["text"] for e in edits] == ["frame-1"]
    # 丢掉的那帧留在 pending 里，收尾时补上
    await client.finalize_streaming_card(key)
    edits = transport(client).of("editMessageText")
    assert [e["text"] for e in edits] == ["frame-1", "frame-2"]


async def test_update_card_skips_identical_content(client):
    key = await client.send_card_to_user("777", content="same")
    await client.update_card(key, "same")
    assert transport(client).of("editMessageText") == []


async def test_update_card_final_bypasses_throttle(client, monkeypatch):
    monkeypatch.setattr(tc, "_FINAL_CONFIRM_DELAY", 0)
    key = await client.send_card_to_user("777", content="a")
    await client.update_card(key, "frame-1")
    await client.update_card_final(key, "done")
    assert [e["text"] for e in transport(client).of("editMessageText")] == [
        "frame-1", "done",
    ]


async def test_final_write_is_confirmed_once_more(client, monkeypatch):
    """终态之后补一次同内容的确认写：万一有更早发出的请求后到，把终态盖回来。"""
    monkeypatch.setattr(tc, "_FINAL_CONFIRM_DELAY", 0.05)
    key = await client.send_card_to_user("777", content="a")
    await client.update_card_final(key, "结论")
    texts = [e["text"] for e in transport(client).of("editMessageText")]
    assert texts == ["结论", "结论"]


async def test_confirm_write_does_not_clobber_a_newer_update(client, monkeypatch):
    """确认写沿用终态的号：期间用户点按钮改了这条卡片，确认写必须自动作废。"""
    monkeypatch.setattr(tc, "_FINAL_CONFIRM_DELAY", 0.3)
    monkeypatch.setattr(tc, "_EDIT_MIN_INTERVAL", 0)
    key = await client.send_card_to_user("777", content="a")
    final = asyncio.create_task(client.update_card_final(key, "结论"))
    await asyncio.sleep(0.1)
    await client.update_card(key, "✅ 已切换为 plan")
    await final
    assert [e["text"] for e in transport(client).of("editMessageText")] == [
        "结论", "✅ 已切换为 plan",
    ]


async def test_update_card_final_splits_long_content_once(client):
    key = await client.send_card_to_user("777", content="a")
    long_text = "\n".join(f"行 {i} " + "x" * 60 for i in range(200))
    await client.update_card_final(key, long_text)
    sent = len(transport(client).of("sendMessage"))
    assert sent >= 2, "超长正文应该用续段消息补齐"
    # 重复收尾（update_card_final 会被错误路径 / 确认写再调一次）不能重复发续段
    await client.update_card_final(key, long_text)
    assert len(transport(client).of("sendMessage")) == sent


async def test_not_modified_is_swallowed(client):
    key = await client.send_card_to_user("777", content="a")
    transport(client).errors["editMessageText"] = [
        TelegramApiError("editMessageText", 400,
                         "Bad Request: message is not modified")
    ]
    await client.update_card_final(key, "b")  # 不抛就算过


async def test_html_parse_failure_falls_back_to_plain_text(client):
    transport(client).errors["sendMessage"] = [
        TelegramApiError("sendMessage", 400,
                         "Bad Request: can't parse entities: unexpected end tag")
    ]
    key = await client.send_card_to_user("777", content="<b>坏 html")
    assert key == "777:101"
    payloads = transport(client).of("sendMessage")
    assert "parse_mode" in payloads[0]
    assert "parse_mode" not in payloads[1]
    assert payloads[1]["text"] == "<b>坏 html"


async def test_client_error_is_not_retried(client):
    transport(client).errors["sendMessage"] = [
        TelegramApiError("sendMessage", 403, "Forbidden: bot was blocked by the user")
    ]
    with pytest.raises(TelegramApiError):
        await client.send_card_to_user("777", content="hi")
    assert len(transport(client).of("sendMessage")) == 1


async def test_rate_limit_is_retried_after_wait(client, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)
    transport(client).errors["sendMessage"] = [
        TelegramApiError("sendMessage", 429, "Too Many Requests", retry_after=3)
    ]
    key = await client.send_card_to_user("777", content="hi")
    assert key == "777:101"
    assert slept and slept[0] == pytest.approx(3.5)


# ── 按钮 ─────────────────────────────────────────────────────

async def test_buttons_become_inline_keyboard_with_server_side_tokens(client):
    key = await client.send_card_to_user("777", content="a")
    buttons = [
        {"text": "是", "value": {"reply": "y", "_cc_uid": "777"}},
        {"text": "否", "value": {"reply": "n", "_cc_uid": "777"}},
    ]
    await client.update_card_with_buttons(key, "选一个", buttons, flow=True)
    edit = transport(client).of("editMessageText")[-1]
    rows = edit["reply_markup"]["inline_keyboard"]
    assert [c["text"] for c in rows[0]] == ["是", "否"]
    token = rows[0][0]["callback_data"]
    # callback_data 有 64 字节硬上限，业务 value 只能留在服务端
    assert len(token.encode()) <= 64
    assert client.resolve_callback(token)["value"]["reply"] == "y"
    assert client.resolve_callback("nope") is None


async def test_update_card_elements_extracts_text_and_buttons(client):
    key = await client.send_card_to_user("777", content="a")
    elements = [
        {"tag": "markdown", "content": "⚡ 快捷命令"},
        {"tag": "column_set", "columns": [
            {"tag": "column", "elements": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "/new"},
                "value": {"action": "run_cmd", "cmd": "/new"},
            }]},
        ]},
    ]
    await client.update_card_elements(key, elements)
    edit = transport(client).of("editMessageText")[-1]
    assert "快捷命令" in edit["text"]
    token = edit["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert client.resolve_callback(token)["value"]["cmd"] == "/new"


# ── 身份 / 附件 ──────────────────────────────────────────────

async def test_get_bot_open_id_uses_get_me(client):
    assert await client.get_bot_open_id() == "42"
    assert client.bot_username == "spx_bot"
    # thread_context 靠 _app_id 判断"这条是我自己发的"
    assert client._app_id == "42"


async def test_speech_to_text_without_delegate_explains_itself(client):
    with pytest.raises(RuntimeError, match="语音转写未启用"):
        await client.speech_to_text("/tmp/a.oga")


async def test_speech_to_text_delegates_to_lark_client(client):
    class FakeLark:
        async def speech_to_text(self, path, file_id=""):
            return "转写结果"

    client.asr_client = FakeLark()
    assert await client.speech_to_text("/tmp/a.oga", "x") == "转写结果"


async def test_get_message_thread_id_falls_back_to_own_message(client):
    # 缓冲里没见过 → 这条消息自成一条 thread（dispatch 子会话靠它隔离 session）。
    # thread id 必须带 chat 作用域，否则两个群撞同号 message_id 会串上下文。
    assert await client.get_message_thread_id("-100:77") == "t-100#77"
    key = await client.reply_card("-100:9", content="x", loading=False)
    assert await client.get_message_thread_id(key) == "c-100"


# ── 合成 thread（dispatch_task 的子会话隔离）─────────────────

async def test_send_post_to_chat_opens_a_new_synthetic_thread(client):
    """dispatch_task 靠"顶楼消息自成一条 thread"来隔离子会话的 session。

    Lark 里往群里发一条消息天然开一条 thread；Telegram 没有话题，若锚点被记成
    群主线（c<chat>），子会话就会和群里的主对话抢同一个 session。
    """
    anchor = await client.send_post_to_chat("-100", "🤖 子任务", "去查一下余额")
    thread = await client.get_message_thread_id(anchor)
    chat_id, mid = split_key(anchor)
    assert thread == f"t{chat_id}#{mid}"
    assert thread != "c-100"

    # 子会话在这条 thread 里的回复也归到同一条 thread（read_thread 才收得全）
    child = await client.reply_card(anchor, content="查完了", loading=False)
    assert await client.get_message_thread_id(child) == thread
    msgs = await client.list_thread_messages(thread)
    assert [json.loads(m.body.content)["text"] for m in msgs] == [
        "**🤖 子任务**\n去查一下余额", "查完了",
    ]


async def test_reply_post_inherits_the_anchor_thread(client):
    anchor = await client.send_post_to_chat("-100", "case", "首帖")
    follow = await client.reply_post(anchor, "case", "第二帖")
    assert await client.get_message_thread_id(follow) == \
        await client.get_message_thread_id(anchor)


async def test_card_state_tables_are_bounded(client):
    """bot 一跑几周不重启，节流/去重表不能无上限堆积。"""
    client._CARD_STATE_MAX = 8
    for i in range(40):
        await client.send_card_to_user(str(700 + i), content=f"m{i}")
    assert len(client._rendered) <= 8
    assert len(client._last_edit) <= 8
    # 最近发的那条还在（只裁最早的）
    last = f"{700 + 39}:{client._post_sync.next_message_id}"
    assert last in client._rendered


async def test_out_of_band_notices_stay_out_of_the_context_buffer(client):
    """✅ / 📬 排队中 这类提示不该进上下文（否则下一轮 prompt 里全是 ✅）。"""
    answer = await client.reply_card("-100:9", content="真正的回答", loading=False)
    await client.update_card_final(answer, "真正的回答")
    await client.reply_text("-100:9", "✅")
    await client.reply_text("-100:9", "📬 前面还有任务在跑，排队中")
    msgs = await client.list_thread_messages("c-100")
    assert [json.loads(m.body.content)["text"] for m in msgs] == ["真正的回答"]


# ── 两个被真·端到端抓出来的 bug（回归钉住）────────────────────

async def test_buttons_still_attach_when_text_is_unchanged(client):
    """斜杠命令的路径是「先 reply 带正文的消息，再补按钮」，两次正文一模一样。

    editMessageText 此时必被 Telegram 判 `message is not modified`，早期实现把它当
    成"没事发生"直接 return，结果按钮永远出不来 —— /mode /model /resume /usage
    在 Telegram 上全成了死命令。
    """
    key = await client.reply_card("-100:9", content="当前模式：bypass", loading=False)
    transport(client).errors["editMessageText"] = [
        TelegramApiError("editMessageText", 400,
                         "Bad Request: message is not modified")
    ]
    await client.update_card_with_buttons(
        key, "当前模式：bypass",
        [{"text": "📋 规划", "value": {"action": "set_mode", "mode": "plan"}}],
        flow=True,
    )
    markup = transport(client).of("editMessageReplyMarkup")
    assert markup, "正文没变时必须改走 editMessageReplyMarkup 把按钮挂上"
    assert markup[-1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "📋 规划"


async def test_throttled_one_shot_update_is_eventually_written(client, monkeypatch):
    """节流必须是"合并"而不是"丢弃"。

    handle_set_mode / handle_menu_command 这类一次性更新背后**没有心跳**来重推，
    丢一帧就等于"点了按钮没反应"。这里模拟"刚写过一帧、立刻又来一帧"。
    """
    monkeypatch.setattr(tc, "_EDIT_MIN_INTERVAL", 0.2)
    key = await client.send_card_to_user("777", content="a")
    await client.update_card(key, "第一帧")
    await client.update_card(key, "✅ 已切换为 plan")   # 立刻又来一帧 → 被节流挡下
    assert [e["text"] for e in transport(client).of("editMessageText")] == ["第一帧"]

    await asyncio.sleep(0.5)   # 等延迟补帧任务
    assert [e["text"] for e in transport(client).of("editMessageText")] == [
        "第一帧", "✅ 已切换为 plan",
    ]


async def test_final_write_cancels_pending_flush(client, monkeypatch):
    """终态写之后，延迟补帧不能再把旧内容盖回去。"""
    monkeypatch.setattr(tc, "_EDIT_MIN_INTERVAL", 0.3)
    monkeypatch.setattr(tc, "_FINAL_CONFIRM_DELAY", 0)
    key = await client.send_card_to_user("777", content="a")
    await client.update_card(key, "帧 1")
    await client.update_card(key, "帧 2（会被节流）")
    await client.update_card_final(key, "最终结论")
    await asyncio.sleep(0.6)
    assert [e["text"] for e in transport(client).of("editMessageText")] == [
        "帧 1", "最终结论",
    ]


async def test_bare_emoji_notices_get_a_label(client):
    """Telegram 把**只含 emoji** 的消息放大成贴纸尺寸，手机上占掉半屏（真机实测）。

    收尾的 ✅ ping 就是这种，所以配个词让它回到正常字号 —— 顺带更有信息量
    （editMessageText 不推通知，这条 ping 才是通知载体）。
    """
    await client.reply_text("-100:9", "✅")
    assert transport(client).of("sendMessage")[-1]["text"] == "✅ 完成"

    await client.reply_text("-100:9", "⏹")
    assert transport(client).of("sendMessage")[-1]["text"] == "⏹ 已停止"

    # 本来就有正文的提示不动
    await client.reply_text("-100:9", "📬 前面还有任务在跑，排队中")
    assert transport(client).of("sendMessage")[-1]["text"].startswith("📬 前面")


async def test_long_poll_asks_for_a_fresh_connection(client):
    """长轮询要一次一条新连接；reset_session 丢掉本线程连接池。"""
    client._session  # 先建一条
    assert getattr(client._local, "session", None) is not None
    client.reset_session()
    assert getattr(client._local, "session", None) is None
