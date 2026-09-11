"""Telegram 渠道对等性审计：Lark 上能做的，Telegram 上必须一样能做。

设计承诺是「业务层一行不改」——`bot.feishu` 换成 TelegramClient（FeishuClient 的
鸭子替身），入站由 telegram_gateway.build_event 把 update 翻成 Lark 事件形状。
本文件把 Lark 渠道已经有测试钉住的行为在 Telegram 上逐条重跑：/stop、排队、
全局并发闸门、错误路径、附件、按钮全链路、派单 / read_thread / 唤醒、prompt 注入。

只读审计：不改任何产品代码。已确认但尚未修复的差异用 xfail(strict=False) 钉住，
测试名 + 断言就是复现步骤。HTTP 全部打桩（_post_sync），绝不碰真 Telegram API。
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher
import lark_prompts
import run_control
import scheduler
import telegram_client as tgc
import telegram_gateway as gw
from bot_config import Profile
from bot_instance import BotInstance
from run_control import RunGate

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 打桩的 Telegram HTTP 层 ──────────────────────────────────

class FakeTransport:
    """替掉 TelegramClient._post_sync：记录所有调用，可按方法注入异常。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 500
        self.fail: dict[str, BaseException] = {}
        self.files: dict[str, dict] = {}

    def __call__(self, method, payload, timeout, fresh=False):
        self.calls.append((method, payload))
        exc = self.fail.get(method)
        if exc is not None:
            raise exc
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id, "chat": {"id": int(payload["chat_id"])}}
        if method == "getFile":
            return self.files.get(payload.get("file_id"), {"file_path": "voice/f.oga"})
        return True

    def of(self, method):
        return [p for m, p in self.calls if m == method]

    def texts(self, method="editMessageText"):
        return [p.get("text", "") for p in self.of(method)]


def _profile(**kw) -> Profile:
    base = dict(
        name="tg", app_id="42", app_secret="42:secret", platform="telegram",
        domain="https://api.telegram.org", default_cwd="/tmp/default",
        bot_token="42:secret",
        allowed_open_ids={"777"},
        allowed_group_chat_ids={"-100123", "-100999"},
        chat_default_cwd={"-100123": "/tmp/proj-a", "-100999": "/tmp/proj-b"},
    )
    base.update(kw)
    return Profile(**base)


def _make_bot(monkeypatch, profile=None) -> BotInstance:
    b = BotInstance(profile or _profile())
    b.feishu._bot_id = 42
    b.feishu._app_id = "42"
    b.feishu._bot_username = "spx_bot"
    # 节流会让「同一条消息 1.5s 内的第二次 edit」被丢帧，单测里几乎每次都撞上；
    # 需要专门验证节流行为的用例自己把它调回来。
    monkeypatch.setattr(tgc, "_EDIT_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(b.feishu, "_post_sync", FakeTransport())
    return b


@pytest.fixture
def bot(monkeypatch):
    return _make_bot(monkeypatch)


@pytest.fixture(autouse=True)
def _fast_gate(monkeypatch):
    monkeypatch.setattr(run_control, "_ABORT_POLL_INTERVAL_SECONDS", 0.01)


_MID = [1000]


def _msg(text: str, chat=-100123, uid: int = 777, mid: int = 0, **kw) -> dict:
    """chat 传数字 = 群，传 dict = 原样用（私聊传 {"id":..,"type":"private"}）。"""
    if not mid:
        _MID[0] += 1
        mid = _MID[0]
    base = {
        "message_id": mid,
        "date": 1700000000 + mid,
        "chat": chat if isinstance(chat, dict) else {"id": chat, "type": "supergroup"},
        "from": {"id": uid, "first_name": "Yixin"},
        "text": text,
    }
    base.update(kw)
    return base


def _cmd(text: str, **kw) -> dict:
    """带 bot_command entity 的消息（Telegram 客户端点 "/" 菜单发出来的形状）。"""
    n = len(text.split()[0])
    return _msg(text, entities=[{"type": "bot_command", "offset": 0, "length": n}], **kw)


PRIVATE = {"id": 777, "type": "private"}


async def _deliver(bot, msg: dict):
    event = gw.build_event(bot, msg)
    assert event is not None, f"build_event 返回 None: {msg}"
    await dispatcher.handle_message_async(bot, event)


def _agent(recorder: list, reply: str = "结论：没问题", gate: asyncio.Event = None,
           chunk_text: str = ""):
    async def run_agent(**kwargs):
        recorder.append(kwargs)
        cb = kwargs.get("on_text_chunk")
        if cb and chunk_text:
            await cb(chunk_text)
        if gate is not None:
            await gate.wait()
        if cb and not chunk_text:
            await cb(reply)
        return reply, "sess-1", False
    return run_agent


def _last_body(bot) -> str:
    edits = bot.feishu._post_sync.texts("editMessageText")
    return edits[-1] if edits else ""


# ══════════════════════════════════════════════════════════════
# 1. /stop 中断
# ══════════════════════════════════════════════════════════════

async def test_stop_in_group_keeps_progress_and_appends_marker(bot):
    """群里 /stop：卡片保留停止前流式出来的进度，并追加「任务已被停止」。"""
    gate = asyncio.Event()
    calls = []
    with mock.patch.object(dispatcher, "run_agent",
                           _agent(calls, gate=gate, chunk_text="已经查到 SGB 开户失败原因")):
        task = asyncio.create_task(_deliver(bot, _msg("@spx_bot 查一下")))
        await asyncio.sleep(0.05)
        assert calls, "任务没起来"
        await _deliver(bot, _cmd("/stop"))
        gate.set()
        await task

    body = _last_body(bot)
    assert "已经查到 SGB 开户失败原因" in body, f"停止卡把停止前的进度弄丢了：{body!r}"
    assert "任务已被停止" in body, f"没有追加停止标记：{body!r}"
    # /stop 的回执是一条独立消息
    assert any("已发送停止请求" in p.get("text", "")
               for p in bot.feishu._post_sync.of("sendMessage"))


async def test_stop_in_private_chat(bot):
    gate = asyncio.Event()
    calls = []
    with mock.patch.object(dispatcher, "run_agent",
                           _agent(calls, gate=gate, chunk_text="私聊进度 ABC")):
        task = asyncio.create_task(_deliver(bot, _msg("干活", chat=PRIVATE)))
        await asyncio.sleep(0.05)
        assert calls
        await _deliver(bot, _cmd("/stop", chat=PRIVATE))
        gate.set()
        await task

    body = _last_body(bot)
    assert "私聊进度 ABC" in body and "任务已被停止" in body
    assert any("已发送停止请求" in p.get("text", "")
               for p in bot.feishu._post_sync.of("sendMessage"))


async def test_no_dead_zone_between_queue_and_stop(bot):
    """「队列说在跑、/stop 说没在跑」的死区：收尾阶段 /stop 仍要认得这个 run。"""
    gate = asyncio.Event()
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls, gate=gate)):
        task = asyncio.create_task(_deliver(bot, _msg("@spx_bot 长任务")))
        await asyncio.sleep(0.05)
        # 队列视角：锁被占住 → 第二条消息会收到排队回执
        assert bot._ensure_chat_lock("-100123:c-100123").locked()
        # /stop 视角：必须认得这个 run
        reply = await dispatcher._handle_stop_command(bot, "777", "-100123:c-100123")
        assert reply == "已发送停止请求", f"死区：队列说在跑，/stop 说 {reply!r}"
        gate.set()
        await task


async def test_stop_without_running_task_says_so(bot):
    await _deliver(bot, _cmd("/stop"))
    sends = bot.feishu._post_sync.of("sendMessage")
    assert len(sends) == 1
    assert "没有" in sends[0]["text"]


async def test_stop_card_is_not_overwritten_by_late_stream_frames(bot):
    """停止后 runner 还在吐字（真实里必然发生）：停止卡不能被后续帧覆盖回"进行中"。"""
    gate = asyncio.Event()
    calls = []

    async def run_agent(**kwargs):
        calls.append(kwargs)
        cb = kwargs["on_text_chunk"]
        await cb("第一段进度")
        await gate.wait()
        await cb("停止之后才到的迟到内容")
        return "done", "sess-1", False

    with mock.patch.object(dispatcher, "run_agent", run_agent):
        task = asyncio.create_task(_deliver(bot, _msg("@spx_bot go")))
        await asyncio.sleep(0.05)
        await _deliver(bot, _cmd("/stop"))
        gate.set()
        await task

    body = _last_body(bot)
    assert "任务已被停止" in body
    assert "停止之后才到的迟到内容" not in body, "迟到帧覆盖了停止卡"


def _edits_for(bot, key: str) -> list[str]:
    """某条消息（复合 key）上的全部 editMessageText 正文。"""
    chat_id, mid = tgc.split_key(key)
    out = []
    for m, p in bot.feishu._post_sync.calls:
        if m == "editMessageText" and str(p["chat_id"]) == chat_id and str(p["message_id"]) == mid:
            out.append(p.get("text", ""))
    return out


# ══════════════════════════════════════════════════════════════
# 2. 排队（per-chat 串行 / 跨 chat 并行）
# ══════════════════════════════════════════════════════════════

async def test_second_message_same_chat_is_queued_and_serialized(bot):
    gate = asyncio.Event()
    order: list[str] = []

    async def run_agent(**kwargs):
        order.append(kwargs["message"].rsplit("】\n\n", 1)[-1])
        if len(order) == 1:
            await gate.wait()
        return "ok", "sess-1", False

    with mock.patch.object(dispatcher, "run_agent", run_agent):
        t1 = asyncio.create_task(_deliver(bot, _msg("@spx_bot 第一件")))
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(_deliver(bot, _msg("@spx_bot 第二件")))
        await asyncio.sleep(0.05)
        assert order == ["第一件"], f"同 chat 没串行：{order}"
        queue_notices = [p["text"] for p in bot.feishu._post_sync.of("sendMessage")
                         if "排队中" in p.get("text", "")]
        assert queue_notices, "第二条没收到「📬 排队中」回执"
        assert "📬" in queue_notices[0]
        gate.set()
        await asyncio.gather(t1, t2)
    assert len(order) == 2 and order[1].endswith("第二件"), \
        f"排队的消息必须照跑，不能丢：{order}"


async def test_different_chats_do_not_block_each_other(bot):
    gate = asyncio.Event()
    done: list[str] = []

    async def run_agent(**kwargs):
        body = kwargs["message"].rsplit("】\n\n", 1)[-1]
        if body == "慢活":
            await gate.wait()
        done.append(body)
        return "ok", "sess-1", False

    with mock.patch.object(dispatcher, "run_agent", run_agent):
        slow = asyncio.create_task(_deliver(bot, _msg("@spx_bot 慢活", chat=-100123)))
        await asyncio.sleep(0.05)
        await _deliver(bot, _msg("@spx_bot 快活", chat=-100999))
        assert done == ["快活"], f"另一个群被前一个群堵住了：{done}"
        gate.set()
        await slow
    assert set(done) == {"慢活", "快活"}


async def test_private_and_group_are_separate_queues(bot):
    gate = asyncio.Event()
    done: list[str] = []

    async def run_agent(**kwargs):
        body = kwargs["message"].rsplit("】\n\n", 1)[-1]
        if body == "群里的慢活":
            await gate.wait()
        done.append(body)
        return "ok", "s", False

    with mock.patch.object(dispatcher, "run_agent", run_agent):
        slow = asyncio.create_task(_deliver(bot, _msg("@spx_bot 群里的慢活")))
        await asyncio.sleep(0.05)
        await _deliver(bot, _msg("私聊的活", chat=PRIVATE))
        assert done == ["私聊的活"]
        gate.set()
        await slow


# ══════════════════════════════════════════════════════════════
# 3. 全局并发闸门（RUN_GATE）
# ══════════════════════════════════════════════════════════════

async def test_gate_full_shows_queued_card_then_runs(bot, monkeypatch):
    monkeypatch.setattr(dispatcher, "RUN_GATE", RunGate(1))
    gate = asyncio.Event()
    started: list[str] = []

    async def run_agent(**kwargs):
        started.append(kwargs["message"].rsplit("】\n\n", 1)[-1])
        if len(started) == 1:
            await gate.wait()
        return "干完了", "sid", False

    with mock.patch.object(dispatcher, "run_agent", run_agent):
        t1 = asyncio.create_task(_deliver(bot, _msg("@spx_bot 甲", chat=-100123)))
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(_deliver(bot, _msg("@spx_bot 乙", chat=-100999)))
        await asyncio.sleep(0.08)

        assert started == ["甲"], f"上限 1，第二个 run 也起跑了：{started}"
        queued = [t for t in bot.feishu._post_sync.texts("editMessageText")
                  if "排队中" in t]
        assert queued, "闸门满时没给排队的任务写「排队中」卡片"
        assert "全局并发上限" in queued[0]

        gate.set()
        await asyncio.gather(t1, t2)
    assert started == ["甲", "乙"], "排队的任务必须在额度腾出来后照跑"
    assert dispatcher.RUN_GATE.running == 0


async def test_gate_timeout_abandons_with_explanation(bot, monkeypatch):
    monkeypatch.setattr(dispatcher, "RUN_GATE", RunGate(1, max_wait=0.05))
    gate = asyncio.Event()
    started: list[str] = []

    async def run_agent(**kwargs):
        started.append(kwargs["message"].rsplit("】\n\n", 1)[-1])
        if len(started) == 1:
            await gate.wait()
        return "干完了", "sid", False

    with mock.patch.object(dispatcher, "run_agent", run_agent):
        t1 = asyncio.create_task(_deliver(bot, _msg("@spx_bot 甲", chat=-100123)))
        await asyncio.sleep(0.05)
        await _deliver(bot, _msg("@spx_bot 乙", chat=-100999))
        assert started == ["甲"]
        texts = bot.feishu._post_sync.texts("editMessageText")
        assert any("没拿到并发额度" in t for t in texts), f"超时放弃没写卡片：{texts}"
        gate.set()
        await t1
    assert not dispatcher.RUN_GATE.full(), "超时放弃漏了额度"


async def test_stop_while_queued_cancels_before_start(bot, monkeypatch):
    monkeypatch.setattr(dispatcher, "RUN_GATE", RunGate(1))
    gate = asyncio.Event()
    started: list[str] = []

    async def run_agent(**kwargs):
        started.append(kwargs["message"].rsplit("】\n\n", 1)[-1])
        if len(started) == 1:
            await gate.wait()
        return "干完了", "sid", False

    with mock.patch.object(dispatcher, "run_agent", run_agent):
        t1 = asyncio.create_task(_deliver(bot, _msg("@spx_bot 甲", chat=-100123)))
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(_deliver(bot, _msg("@spx_bot 乙", chat=-100999)))
        await asyncio.sleep(0.08)

        # 排队态也要能被 /stop（走真实的 Telegram 入站路径）
        await _deliver(bot, _cmd("/stop", chat=-100999))
        await t2
        assert started == ["甲"], "被 /stop 的排队任务不该进 agent"
        texts = bot.feishu._post_sync.texts("editMessageText")
        # 走真实 /stop 路径时 _announce_stopped_run 先写了停止卡，_abandon_queued_run
        # 就不再覆盖（和 Lark 同一分支）；两者任一落地都算收尾成功。
        assert any(("已取消" in t) or ("已停止" in t) for t in texts), \
            f"取消后没收尾卡片：{texts}"
        gate.set()
        await t1
    assert dispatcher.RUN_GATE.running == 0 and not dispatcher.RUN_GATE.full()


# ══════════════════════════════════════════════════════════════
# 4. 错误路径
# ══════════════════════════════════════════════════════════════

def _api_error(method="editMessageText", desc="Bad Request: boom"):
    return tgc.TelegramApiError(method, 400, desc)


async def test_agent_exception_is_written_into_that_message(bot):
    """run_agent 抛异常 → 错误必须落进那条卡片，且保留出错前的进度。"""
    async def run_agent(**kwargs):
        await kwargs["on_text_chunk"]("已经跑到一半")
        raise RuntimeError("上游炸了")

    with mock.patch.object(dispatcher, "run_agent", run_agent):
        await _deliver(bot, _msg("@spx_bot 干活"))

    body = _last_body(bot)
    assert "已经跑到一半" in body, f"错误卡把出错前的进度抹了：{body!r}"
    assert "RuntimeError" in body and "上游炸了" in body
    assert "任务没有执行完" in body
    # 卡片是 in-place edit，不触发通知 → 额外一条 ❌ 短消息（与 Lark 一致）
    assert any("异常退出" in p.get("text", "")
               for p in bot.feishu._post_sync.of("sendMessage"))


async def test_error_card_is_not_overwritten_by_heartbeat(bot):
    """心跳竞态：错误落卡之后不能再被「进行中」帧覆盖（tests/dispatcher 心跳竞态教训）。"""
    async def run_agent(**kwargs):
        await kwargs["on_text_chunk"]("进度")
        await asyncio.sleep(1.2)          # 让心跳至少推一帧
        raise RuntimeError("炸")

    with mock.patch.object(dispatcher, "run_agent", run_agent):
        await _deliver(bot, _msg("@spx_bot 干活"))

    edits = bot.feishu._post_sync.texts("editMessageText")
    assert "RuntimeError" in edits[-1], f"最后一帧不是错误卡：{edits[-1]!r}"
    assert "⏱" not in edits[-1], "错误卡被带计时 footer 的心跳帧覆盖了"


async def test_card_patch_failure_falls_back_to_text(bot):
    """editMessageText 失败 → 回退发一条文本消息，结果不能丢。"""
    async def run_agent(**kwargs):
        # 占位卡已建好，从这一刻起让所有 edit 失败
        bot.feishu._post_sync.fail["editMessageText"] = _api_error()
        return "最终答案 42", "sess-1", False

    with mock.patch.object(dispatcher, "run_agent", run_agent):
        await _deliver(bot, _msg("@spx_bot 干活"))

    sends = [p["text"] for p in bot.feishu._post_sync.of("sendMessage")]
    assert any("最终答案 42" in t for t in sends), f"卡片失败后没有回退发文本：{sends}"


async def test_text_fallback_failure_lands_in_outbox(bot, tmp_path, monkeypatch):
    """卡片 + 文本都发不出去 → 落 outbox（bot.feishu.save_outbox），结果绝不丢。"""
    import outbox as _outbox
    monkeypatch.setattr(_outbox, "_LOG_DIR", str(tmp_path))

    async def run_agent(**kwargs):
        bot.feishu._post_sync.fail["editMessageText"] = _api_error()
        bot.feishu._post_sync.fail["sendMessage"] = _api_error("sendMessage")
        return "这段结果必须被保住", "sess-1", False

    with mock.patch.object(dispatcher, "run_agent", run_agent):
        await _deliver(bot, _msg("@spx_bot 干活"))

    path = _outbox.outbox_path("tg")
    assert os.path.exists(path), "卡片+文本双失败后结果没落 outbox"
    assert "这段结果必须被保住" in open(path, encoding="utf-8").read()


async def test_placeholder_card_failure_is_reported(bot):
    """连占位卡都发不出去时也要给用户一句话（不能静默）。"""
    bot.feishu._post_sync.fail["sendMessage"] = _api_error("sendMessage")
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _msg("@spx_bot 干活"))
    assert calls == [], "占位卡都失败了还去跑 agent"
    # 报错本身也发不出去（同一个通道），至少不能把整个 handler 掀翻
    assert bot.feishu._post_sync.of("sendMessage"), "根本没尝试发送"


# ══════════════════════════════════════════════════════════════
# 5. 附件（图片 / 文件 / 语音）
# ══════════════════════════════════════════════════════════════

def _photo(caption: str = "", **kw) -> dict:
    m = _msg("", chat=PRIVATE, **kw)
    m.pop("text")
    m["photo"] = [{"file_id": "AgAC-small", "file_size": 100},
                  {"file_id": "AgAC-big", "file_size": 9000}]
    if caption:
        m["caption"] = caption
    return m


def _voice(**kw) -> dict:
    m = _msg("", chat=PRIVATE, **kw)
    m.pop("text")
    m["voice"] = {"file_id": "VOICE1", "duration": 7}
    return m


def _document(name="report.pdf", caption="", **kw) -> dict:
    m = _msg("", chat=PRIVATE, **kw)
    m.pop("text")
    m["document"] = {"file_id": "DOC1", "file_name": name}
    if caption:
        m["caption"] = caption
    return m


async def test_image_download_failure_is_reported_not_silent(bot):
    bot.feishu._post_sync.fail["getFile"] = _api_error("getFile", "Bad Request: file not found")
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _photo())
    assert calls == []
    sends = [p["text"] for p in bot.feishu._post_sync.of("sendMessage")]
    assert any("下载图片失败" in t for t in sends), f"图片下载失败被静默了：{sends}"


async def test_file_download_failure_is_reported_not_silent(bot):
    bot.feishu._post_sync.fail["getFile"] = _api_error("getFile", "Bad Request: file is too big")
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _document())
    assert calls == []
    sends = [p["text"] for p in bot.feishu._post_sync.of("sendMessage")]
    assert any("下载文件失败" in t for t in sends), f"文件下载失败被静默了：{sends}"


async def test_voice_without_asr_proxy_says_why(bot, monkeypatch):
    """没有可借用的 Lark ASR 通道时要报清楚原因，而不是静默丢掉这条语音。"""
    monkeypatch.setattr(bot.feishu, "_download_sync",
                        lambda *a, **k: "/tmp/fake-voice.oga")
    assert bot.feishu.asr_client is None
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _voice())
    assert calls == []
    sends = [p["text"] for p in bot.feishu._post_sync.of("sendMessage")]
    assert any("语音转写失败" in t for t in sends), f"语音失败被静默了：{sends}"
    assert any("ASR" in t or "CC_TG_ASR_PROFILE" in t for t in sends), \
        f"报错没说清怎么修：{sends}"


async def test_voice_with_asr_proxy_goes_through(bot, monkeypatch):
    monkeypatch.setattr(bot.feishu, "_download_sync",
                        lambda *a, **k: "/tmp/fake-voice.oga")
    asr = mock.MagicMock()
    asr.speech_to_text = mock.AsyncMock(return_value="把 SGB 的开户日志拉出来")
    bot.feishu.asr_client = asr
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _voice())
    assert len(calls) == 1, "有 ASR 代理时语音应该照常触发"
    assert "把 SGB 的开户日志拉出来" in calls[0]["message"]
    assert asr.speech_to_text.await_count == 1


async def test_photo_with_caption_carries_both_text_and_image(bot, monkeypatch):
    monkeypatch.setattr(bot.feishu, "_download_sync",
                        lambda *a, **k: "/tmp/fake-img.jpg")
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _photo(caption="看看这张报错截图"))
    assert len(calls) == 1
    prompt = calls[0]["message"]
    assert "看看这张报错截图" in prompt and "/tmp/fake-img.jpg" in prompt


async def test_document_caption_reaches_the_prompt(bot, monkeypatch):
    monkeypatch.setattr(bot.feishu, "_download_sync",
                        lambda *a, **k: "/tmp/fake-doc.pdf")
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _document(caption="重点看第 3 页"))
    assert len(calls) == 1
    prompt = calls[0]["message"]
    assert "report.pdf" in prompt and "重点看第 3 页" in prompt


# ══════════════════════════════════════════════════════════════
# 6. 按钮全链路（渲染 inline keyboard → callback → handler）
# ══════════════════════════════════════════════════════════════

_KB_METHODS = ("editMessageText", "editMessageReplyMarkup", "sendMessage")


def _keyboard_of(bot, methods=_KB_METHODS) -> list[list[dict]]:
    """最后一次挂上去的 inline keyboard。

    键盘可能来自三个地方：随消息一起发（sendMessage）、连正文一起改
    （editMessageText），或者正文没变只补按钮（editMessageReplyMarkup）。
    这里不关心走了哪条，只关心"用户最终看到的按钮是什么"。
    """
    for m, p in reversed(bot.feishu._post_sync.calls):
        if m in methods and (p.get("reply_markup") or {}).get("inline_keyboard"):
            return p["reply_markup"]["inline_keyboard"]
    return []


def _labels(keyboard) -> list[str]:
    return [c["text"] for row in keyboard for c in row]


def _tokens(keyboard) -> list[str]:
    return [c["callback_data"] for row in keyboard for c in row]


def _peek(bot, token: str) -> dict:
    """只看 token 背后的业务 value，**不走 resolve_callback**。

    `resolve_callback` 带双击去重（会写 `used_at`），测试里为了挑按钮先调一次，
    真正点击时就会被判成"连点"而静默丢掉。所以检查用直读内部表。
    """
    return dict(bot.feishu._callbacks[token])


def _cq(bot, token: str, mid: int, chat=-100123, uid=777) -> dict:
    chat_obj = chat if isinstance(chat, dict) else {"id": chat, "type": "supergroup"}
    return {
        "id": "cb-1",
        "from": {"id": uid, "first_name": "Yixin"},
        "message": {"message_id": mid, "chat": chat_obj},
        "data": token,
    }


def _submitter(pending: list):
    def submit(coro):
        pending.append(asyncio.get_running_loop().create_task(coro))
    return submit


def _last_card_key(bot) -> tuple[str, int]:
    """最后一条被编辑（正文或按钮）的消息的复合 key。"""
    for m, p in reversed(bot.feishu._post_sync.calls):
        if m in ("editMessageText", "editMessageReplyMarkup"):
            return tgc.make_key(p["chat_id"], p["message_id"]), int(p["message_id"])
    raise AssertionError("没有任何被编辑的消息")


async def test_mode_command_renders_inline_keyboard(bot):
    await _deliver(bot, _cmd("/mode"))
    kb = _keyboard_of(bot)
    assert kb, "/mode 没渲染出 inline keyboard"
    assert "🚀 全自动" in _labels(kb)
    # callback_data 是短 token（Telegram 上限 64 字节），业务 value 存服务端
    for tok in _tokens(kb):
        assert len(tok.encode()) <= 64
        assert _peek(bot, tok)["value"]["action"] == "set_mode"


async def test_model_command_renders_inline_keyboard(bot):
    await _deliver(bot, _cmd("/model"))
    kb = _keyboard_of(bot)
    assert kb, "/model 没渲染出 inline keyboard"
    vals = [_peek(bot, t)["value"] for t in _tokens(kb)]
    assert all(v["action"] == "run_cmd" for v in vals)
    assert any(v["cmd"].startswith("/model ") for v in vals)


async def test_resume_command_renders_inline_keyboard(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _msg("@spx_bot 先跑一轮建个 session"))
    await _deliver(bot, _cmd("/resume"))
    kb = _keyboard_of(bot)
    assert kb, "/resume 没渲染出历史会话按钮"
    vals = [_peek(bot, t)["value"] for t in _tokens(kb)]
    assert any(v["action"] == "resume_session" and v.get("sid") for v in vals)


async def test_usage_command_renders_inline_keyboard(bot, monkeypatch):
    import commands as _cmds
    monkeypatch.setattr(_cmds, "_get_usage", lambda chat_id: {
        "text": "📊 用量",
        "buttons": [
            {"text": "● acct-a", "value": {"action": "switch_usage", "name": "acct-a",
                                           "cid": chat_id}},
            {"text": "🔄 刷新", "value": {"action": "run_cmd", "cmd": "/usage",
                                          "cid": chat_id}},
        ],
    })
    await _deliver(bot, _cmd("/usage"))
    kb = _keyboard_of(bot)
    assert kb, "/usage 没渲染出账户切换按钮"
    vals = [_peek(bot, t)["value"] for t in _tokens(kb)]
    assert any(v["action"] == "switch_usage" for v in vals)


async def test_bare_slash_shows_the_command_menu(bot):
    """裸 `/` 要出命令菜单。

    曾经的坑：`parse_command("/")` 对空 parts 取 `parts[0]` 直接 IndexError，而
    `handle_message_async`（dispatcher.py:981）在 `_text == "/"` 的菜单分支之前就调了它，
    那段又没有 try —— 异常一路冒出 handler，用户什么都收不到。Telegram 上尤其容易踩：
    `set_my_commands` 注册的 "/" 菜单让手机端输入框里先出现的就是一个裸 `/`。
    """
    await _deliver(bot, _msg("/", chat=PRIVATE))
    assert _keyboard_of(bot), "裸 / 没有渲染出命令菜单"


def test_parse_command_tolerates_a_bare_slash():
    from commands import parse_command
    assert parse_command("/") is None
    assert parse_command("/  ") is None
    assert parse_command("/mode x") == ("mode", "x")


async def test_command_menu_elements_render_as_grouped_inline_keyboard(bot):
    """绕开上面那个崩溃，直接验 elements 混排 → TG inline keyboard 的转换本身是对的。"""
    await dispatcher._show_command_menu(bot, "777", "777", False, "777:1")
    kb = _keyboard_of(bot)
    labels = _labels(kb)
    assert "🆕 新会话" in labels and "📈 用量" in labels, f"命令菜单没渲染全：{labels}"
    # markdown 分组标题降级成正文，不能丢
    text = bot.feishu._post_sync.of("editMessageText")[-1]["text"]
    assert "会话" in text and "配置" in text and "查看" in text
    # 菜单按钮多，横排（flow）→ 每行 3 个
    assert max(len(row) for row in kb) == 3


async def test_callback_set_mode_updates_the_same_message(bot):
    await _deliver(bot, _cmd("/mode"))
    kb = _keyboard_of(bot)
    token = next(t for t in _tokens(kb)
                 if _peek(bot, t)["value"].get("mode") == "plan")
    key, mid = _last_card_key(bot)
    before = len(_edits_for(bot, key))

    pending: list = []
    gw.handle_callback(bot, _cq(bot, token, mid), _submitter(pending))
    await asyncio.gather(*pending)

    assert (await bot.store.get_current("777", "-100123:c-100123")).permission_mode == "plan"
    after = _edits_for(bot, key)[before:]
    assert any("plan" in t for t in after), \
        f"点按钮后没有原地更新那条消息：{_edits_for(bot, key)}"
    assert bot.feishu._post_sync.of("answerCallbackQuery"), "没给 Telegram 回 callback ack"


async def test_callback_run_cmd_reaches_handle_menu_command(bot):
    await dispatcher._show_command_menu(bot, "777", "777", False, "777:1")
    kb = _keyboard_of(bot)
    token = next(t for t in _tokens(kb)
                 if _peek(bot, t)["value"].get("cmd") == "/mode")
    key, mid = _last_card_key(bot)
    before = len(_edits_for(bot, key))

    pending: list = []
    gw.handle_callback(bot, _cq(bot, token, mid, chat=PRIVATE, uid=777), _submitter(pending))
    await asyncio.gather(*pending)

    edits = _edits_for(bot, key)
    assert any("当前模式" in t for t in edits[before:]), \
        f"run_cmd 按钮没原地重渲染：{edits}"


async def test_callback_resume_session_reaches_handler(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _msg("@spx_bot 建个 session"))
    await _deliver(bot, _cmd("/resume"))
    kb = _keyboard_of(bot)
    token = next(t for t in _tokens(kb)
                 if _peek(bot, t)["value"].get("action") == "resume_session")
    key, mid = _last_card_key(bot)

    with mock.patch.object(dispatcher, "handle_resume_session",
                           new_callable=mock.AsyncMock) as h:
        pending: list = []
        gw.handle_callback(bot, _cq(bot, token, mid), _submitter(pending))
        await asyncio.gather(*pending)
    assert h.await_count == 1
    assert h.await_args.args[3] == _peek(bot, token)["value"]["sid"]
    assert h.await_args.args[4] == key


async def test_callback_from_another_user_is_rejected(bot):
    await _deliver(bot, _cmd("/mode"))
    kb = _keyboard_of(bot)
    token = _tokens(kb)[0]
    _key, mid = _last_card_key(bot)

    pending: list = []
    gw.handle_callback(bot, _cq(bot, token, mid, uid=999), _submitter(pending))
    await asyncio.gather(*pending)
    acks = [p["text"] for p in bot.feishu._post_sync.of("answerCallbackQuery")]
    assert any("别人" in a for a in acks), f"别人的按钮没被拦住：{acks}"


async def test_expired_callback_token_tells_the_user(bot):
    pending: list = []
    gw.handle_callback(bot, _cq(bot, "nonexistent-token", 501), _submitter(pending))
    await asyncio.gather(*pending)
    acks = [p["text"] for p in bot.feishu._post_sync.of("answerCallbackQuery")]
    assert any("过期" in a for a in acks)


# ── 按钮：真实节流下的原地更新 ────────────────────────────────
# 背景：TelegramClient.update_card 有 1.5s 丢帧节流（Lark 的 update_card 没有）。
# 而 handle_set_mode / handle_menu_command / handle_switch_usage /
# handle_resume_session 这四个按钮 handler 用的都是裸 update_card，之后没有任何
# finalize —— 如果节流只是"丢帧"，卡片刚渲染完就点按钮（<1.5s）会永远停在旧文案。
# telegram_client.py:428 的 _schedule_flush 把"丢帧"改成了"攒着 + 延迟补写"，
# 下面两条就是钉住这个补写真的会发生（否则按钮在 TG 上看起来就是坏的）。

async def test_fast_click_still_updates_card_under_real_throttle(bot, monkeypatch):
    monkeypatch.setattr(tgc, "_EDIT_MIN_INTERVAL", 0.3)   # 真节流，只是缩短窗口
    await _deliver(bot, _cmd("/mode", chat=PRIVATE))
    kb = _keyboard_of(bot)
    token = next(t for t in _tokens(kb)
                 if _peek(bot, t)["value"].get("mode") == "plan")
    key, mid = _last_card_key(bot)
    before = len(_edits_for(bot, key))

    pending: list = []
    gw.handle_callback(bot, _cq(bot, token, mid, chat=PRIVATE), _submitter(pending))
    await asyncio.gather(*pending)

    assert (await bot.store.get_current("777", "777")).permission_mode == "plan"
    await asyncio.sleep(0.6)      # 等节流窗口过去，延迟补帧应当把它写出去
    after = _edits_for(bot, key)[before:]
    assert any("plan" in t for t in after), (
        "按钮点了、模式切了，但那条消息一个字都没变 —— 节流丢帧且没人补发。"
        f"（handle_set_mode dispatcher.py:2844 是裸 update_card）edits={_edits_for(bot, key)}"
    )


async def test_menu_stop_button_updates_card_under_real_throttle(bot, monkeypatch):
    monkeypatch.setattr(tgc, "_EDIT_MIN_INTERVAL", 0.3)
    await dispatcher._show_command_menu(bot, "777", "777", False, "777:1")
    kb = _keyboard_of(bot)
    token = next(t for t in _tokens(kb)
                 if _peek(bot, t)["value"].get("cmd") == "/stop")
    key, mid = _last_card_key(bot)

    pending: list = []
    gw.handle_callback(bot, _cq(bot, token, mid, chat=PRIVATE), _submitter(pending))
    await asyncio.gather(*pending)
    await asyncio.sleep(0.6)
    assert any("没有正在运行" in t for t in _edits_for(bot, key)), \
        f"点「⏹ 停止任务」后卡片没有任何反馈：{_edits_for(bot, key)}"


# ══════════════════════════════════════════════════════════════
# 7. 派单 / read_thread / 定时唤醒
# ══════════════════════════════════════════════════════════════

async def test_dispatch_task_creates_top_post_with_its_own_thread(bot):
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
    # 顶楼消息进了群，且和群主线 thread 隔离
    sends = bot.feishu._post_sync.of("sendMessage")
    assert sends[0]["chat_id"] == "-100123"
    assert "查余额" in sends[0]["text"]
    assert bot.feishu.buffer.thread_of(result["anchor_message_id"]) == thread


async def test_read_thread_returns_transcript_of_the_child_thread(bot):
    with mock.patch.object(dispatcher, "handle_spawn", new_callable=mock.AsyncMock):
        result = await dispatcher.dispatch_task(
            bot, user_id="777", group_chat_id="-100123",
            title="子任务甲", prompt="去把 A 查清楚",
        )
    thread = result["thread_id"]
    anchor_mid = int(result["anchor_message_id"].split(":")[1])

    # 用户在子会话里追一句 + bot 在子会话里回一句
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls, reply="子会话的结论 XYZ")):
        await _deliver(bot, _msg(
            "补充一点", reply_to_message={"message_id": anchor_mid, "from": {"id": 42}}))

    out = await dispatcher.read_thread(bot, thread_id=thread)
    assert out["ok"] is True, out
    assert out["count"] >= 2
    assert "去把 A 查清楚" in out["transcript"]
    assert "补充一点" in out["transcript"]
    assert "子会话的结论 XYZ" in out["transcript"], \
        f"read_thread 读不回子会话的最终答案：{out['transcript']}"


async def test_read_thread_on_unknown_thread_is_graceful(bot):
    out = await dispatcher.read_thread(bot, thread_id="tnope")
    assert out["ok"] is True and out["count"] == 0
    assert "暂无消息" in out["transcript"]


async def test_schedule_wake_accepts_a_telegram_profile(bot, monkeypatch):
    """schedule_wake 的参数校验 + 落盘在 telegram profile 上不能报错（不真等 fire）。"""
    jobs = []

    class _Sched:
        def add_job(self, fn, **kw):
            jobs.append(kw)

    monkeypatch.setitem(scheduler._STATE, "scheduler", _Sched())
    monkeypatch.setitem(scheduler._STATE, "bots", {"tg": bot})
    monkeypatch.setitem(scheduler._STATE, "bot_loop", asyncio.get_running_loop())
    monkeypatch.setitem(scheduler._STATE, "spawn_fn", mock.AsyncMock())

    res = scheduler.schedule_wake(
        profile="tg", chat_id="-100123", thread_id="c-100123",
        anchor_message_id="-100123:1234", user_id="777",
        minutes=7, note="回来看 CI 结果",
    )
    assert res["ok"] is True, res
    assert res["job_id"].startswith("wake-")
    assert jobs and jobs[0]["id"] == res["job_id"]

    # 落盘（conftest 已把 CC_LARK_WAKE_STORE 指到 tmp）
    store = json.load(open(os.environ["CC_LARK_WAKE_STORE"], encoding="utf-8"))
    rec = store[res["job_id"]]
    assert rec["profile"] == "tg" and rec["thread_id"] == "c-100123"
    assert rec["anchor"] == "-100123:1234"

    # 公告发到本会话（复合 key 能被 reply_text 拆开）
    await asyncio.sleep(0.05)
    sends = [p["text"] for p in bot.feishu._post_sync.of("sendMessage")]
    assert any("已排定自动唤醒" in t for t in sends), f"没发唤醒公告：{sends}"


async def test_schedule_wake_rejects_bad_args_on_telegram(bot, monkeypatch):
    monkeypatch.setitem(scheduler._STATE, "scheduler", mock.MagicMock())
    monkeypatch.setitem(scheduler._STATE, "bots", {"tg": bot})
    monkeypatch.setitem(scheduler._STATE, "bot_loop", asyncio.get_running_loop())
    monkeypatch.setitem(scheduler._STATE, "spawn_fn", mock.AsyncMock())
    common = dict(profile="tg", chat_id="-100123", thread_id="c-100123",
                  anchor_message_id="-100123:1", user_id="777")
    assert scheduler.schedule_wake(**common, minutes=0, note="x")["ok"] is False
    assert scheduler.schedule_wake(**common, minutes=5, note="  ")["ok"] is False
    bad = dict(common, thread_id="")
    assert scheduler.schedule_wake(**bad, minutes=5, note="x")["ok"] is False


# ══════════════════════════════════════════════════════════════
# 8. prompt 注入
# ══════════════════════════════════════════════════════════════

_LARK_PROFILE = Profile(
    name="test", app_id="cli_test", app_secret="s", platform="lark",
    domain="open.larksuite.com", default_cwd="/tmp",
)


def _render(profile, **over):
    kw = dict(raw_chat_id="oc_xxx", thread_id="omt_t", user_message_id="om_y",
              is_group=True, asker_open_id="ou_z", runner="claude")
    kw.update(over)
    return lark_prompts.render_lark_prompt(profile, **kw)


def _section(text: str, head: str) -> str:
    """截出以 head 开头、到下一个空行分隔的大段（用于比对共用段落）。"""
    i = text.index(head)
    return text[i:]


async def test_telegram_prompt_is_the_telegram_flavour(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _msg("@spx_bot hi"))
    sp = calls[0]["append_system_prompt"]
    assert "你正在通过 Telegram 与用户对话" in sp
    assert "lark-cli" not in sp, "Telegram 模板里混进了 lark-cli"
    assert "tg-cli" in sp
    assert "@spx_bot" in sp, "没告诉 agent 怎样算 @ 到它"
    assert "-100123" in sp and "c-100123" in sp


def test_telegram_and_lark_share_the_runtime_env_section():
    tg = _render(_profile(), bot_username="spx_bot")
    lk = _render(_LARK_PROFILE)
    head = "【⚠️ 运行环境约束（通用）】"
    assert head in tg and head in lk
    # 唯一允许的差异是模板文件末尾的换行（telegram.md 有、default.md 没有）
    assert _section(tg, head).rstrip("\n") == _section(lk, head).rstrip("\n"), \
        "运行环境约束段在两个渠道之间漂了（应共用 prompts/_runtime_env.md）"


def test_telegram_and_lark_share_the_runtime_mcp_section_except_ask_cmd():
    tg = _render(_profile(), bot_username="spx_bot")
    lk = _render(_LARK_PROFILE)
    head = "【cc-lark 运行时 MCP 工具"
    tg_sec = _section(tg, head)
    lk_sec = _section(lk, head)
    assert "wake_me_in" in tg_sec and "dispatch_task" in tg_sec
    # 唯一允许的差异就是 ${ask_cmd}（Lark 用 lark-cli，Telegram 用 tg-cli）
    tg_norm = tg_sec.replace(os.path.join(PROJECT_ROOT, "tg-cli") + ' send --text "<问题>"', "<ASK>")
    lk_norm = lk_sec.replace('lark-cli ... im +messages-reply ... --text "<问题>"', "<ASK>")
    # 后半段（运行环境约束）已在上一个测试比过，这里只比 MCP 段
    tg_mcp = tg_norm[:tg_norm.index("【⚠️ 运行环境约束（通用）】")]
    lk_mcp = lk_norm[:lk_norm.index("【⚠️ 运行环境约束（通用）】")]
    assert tg_mcp == lk_mcp, "运行时 MCP 段除 ask_cmd 外还有别的漂移"


@pytest.mark.parametrize("runner", ["claude", "codex", "opencode", "agy"])
@pytest.mark.parametrize("is_group", [True, False])
def test_lark_runtime_env_is_byte_identical_to_pre_refactor(tmp_path, monkeypatch,
                                                          runner, is_group):
    """重构（default.md 尾部抽成 _runtime_env.md）
    不允许改变被抽取的运行时段一个字节。正文中的云文档交付规则有独立的产品变更，
    不属于模板抽取；其契约由 test_lark_document_delivery_policy 单独覆盖。
    用 git show 重建旧模板做对照，工作区不动。"""
    old_dir = tmp_path / "old_prompts"
    old_dir.mkdir()
    for name in ("default", "_runtime_mcp_claude", "_runtime_mcp_other",
                 "_dispatch", "verify"):
        blob = subprocess.run(
            ["git", "show", f"HEAD:prompts/{name}.md"],
            cwd=PROJECT_ROOT, capture_output=True, check=True).stdout
        (old_dir / f"{name}.md").write_bytes(blob)
    # 旧 default.md 不引用 ${runtime_env_section}，但新 render_lark_prompt 无条件渲染它
    # → 旧目录也得有这个 partial 才跑得起来（本身就是下面那个 git 追踪测试要说的事）。
    shutil.copy(os.path.join(PROJECT_ROOT, "prompts", "_runtime_env.md"),
                old_dir / "_runtime_env.md")

    prof = Profile(
        name="test", app_id="cli_test", app_secret="s", platform="lark",
        domain="open.larksuite.com", default_cwd="/tmp",
        dispatch_chat_id="oc_dispatch",
    )
    kw = dict(raw_chat_id="oc_other", thread_id="", user_message_id="om_y",
              is_group=is_group, asker_open_id="ou_z", runner=runner)

    lark_prompts.clear_cache()
    new = lark_prompts.render_lark_prompt(prof, **kw)
    monkeypatch.setattr(lark_prompts, "PROMPTS_DIR", str(old_dir))
    lark_prompts.clear_cache()
    old = lark_prompts.render_lark_prompt(prof, **kw)
    lark_prompts.clear_cache()

    runtime_head = "【⚠️ 运行环境约束（通用）】"
    assert runtime_head in new and runtime_head in old
    new_runtime = new[new.index(runtime_head):]
    old_runtime = old[old.index(runtime_head):]
    assert new_runtime == old_runtime, (
        f"runner={runner} is_group={is_group}：重构改变了 Lark 渲染结果\n"
        f"--- 旧 ---\n{old[-400:]}\n--- 新 ---\n{new[-400:]}"
    )


def test_lark_document_delivery_policy():
    """Explicitly preserve the approved cloud-document rules outside the runtime refactor."""
    prompt = _render(_LARK_PROFILE)
    assert "实施方案/设计文档" in prompt
    assert "严禁输出 `file:///` 本地文件协议链接或本地磁盘路径" in prompt
    assert "若 `--as user` 报未授权（need_user_authorization），立刻换 `--as bot` 创建" in prompt
    assert "drive +member-add --as bot" in prompt
    assert '--member-id "$CC_LARK_USER_ID"' in prompt


@pytest.mark.parametrize("name", ["_runtime_env.md", "telegram.md"])
def test_new_prompt_templates_are_not_gitignored(name):
    """新模板必须能进版本控制。

    `.gitignore` 里 `prompts/*` 是整体忽略 + 白名单，漏加白名单的后果不是"Telegram
    少个功能"而是**整个 bot 在新克隆/服务器上不工作**：`render_lark_prompt` 无条件
    渲染 `_runtime_env`，缺文件 → FileNotFoundError → Lark 渠道每条消息都回
    「❌ 异常退出」。所以这里钉住"没被忽略"。
    （"已提交"不在测试范围：commit 是人的动作，本测试只保证不会被静默漏掉。）
    """
    rc = subprocess.run(["git", "check-ignore", "-q", f"prompts/{name}"],
                        cwd=PROJECT_ROOT, capture_output=True).returncode
    assert rc != 0, f"prompts/{name} 被 .gitignore 吞了（新克隆上 Lark 也会一起废）"
    assert os.path.exists(os.path.join(PROJECT_ROOT, "prompts", name))


def test_missing_runtime_env_partial_breaks_lark_too(tmp_path, monkeypatch):
    """把「只有 git 里那份 prompts」的部署现场还原出来：Lark 渲染直接炸。

    这是上面那条 gitignore 缺失的**后果**，不是 Telegram 的问题——但它会让
    整个服务在一台干净机器上起不来，所以一并钉住。"""
    # 造一个"缺了 _runtime_env.md"的 prompts 目录（别依赖 git 状态：文件一旦被
    # commit，基于 git ls-files 的写法就会反过来失败）
    clean = tmp_path / "without_runtime_env"
    clean.mkdir()
    for name in os.listdir(os.path.join(PROJECT_ROOT, "prompts")):
        if name == "_runtime_env.md" or not name.endswith(".md"):
            continue
        shutil.copy(os.path.join(PROJECT_ROOT, "prompts", name), clean / name)
    assert not (clean / "_runtime_env.md").exists()

    monkeypatch.setattr(lark_prompts, "PROMPTS_DIR", str(clean))
    lark_prompts.clear_cache()
    with pytest.raises(FileNotFoundError):
        _render(_LARK_PROFILE)
    lark_prompts.clear_cache()


# ══════════════════════════════════════════════════════════════
# 9. 其余对等性（去重 / 超长收尾 / 真实节流下的停止卡 / 历史附件）
# ══════════════════════════════════════════════════════════════

async def test_duplicate_update_is_deduped_by_composite_key(bot):
    """Telegram 重投（同一 update 再来一次）不能跑两遍 agent。"""
    calls = []
    m = _msg("@spx_bot 只跑一次")
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, m)
        await _deliver(bot, dict(m))
    assert len(calls) == 1, "同一条消息被处理了两遍"


async def test_same_message_id_in_two_chats_is_not_deduped(bot):
    """复合 key 的意义：两个群各有 message_id=4242，不能互相把对方判成重复。"""
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _msg("@spx_bot 甲群", chat=-100123, mid=4242))
        await _deliver(bot, _msg("@spx_bot 乙群", chat=-100999, mid=4242))
    assert len(calls) == 2, "两个群里同号消息被误判成重复（复合 key 失效）"


async def test_long_answer_is_split_without_losing_content(bot):
    long_text = "\n".join(f"第 {i} 行 内容内容内容内容内容内容" for i in range(400))
    with mock.patch.object(dispatcher, "run_agent",
                           _agent([], reply=long_text)):
        await _deliver(bot, _msg("@spx_bot 出个长报告"))
    tr = bot.feishu._post_sync
    blob = "".join(tr.texts("editMessageText")) + "".join(
        p.get("text", "") for p in tr.of("sendMessage"))
    assert "第 0 行" in blob and "第 399 行" in blob, "超长回答被截掉了"
    assert len(tr.of("sendMessage")) > 1, "超长回答没有续段消息"


async def test_stop_card_lands_even_under_real_throttle(bot, monkeypatch):
    """真实 1.5s 节流下 /stop：停止卡必须靠 finalize 补发落地，不能只停在 _pending。"""
    monkeypatch.setattr(tgc, "_EDIT_MIN_INTERVAL", 1.5)
    gate = asyncio.Event()
    calls = []
    with mock.patch.object(dispatcher, "run_agent",
                           _agent(calls, gate=gate, chunk_text="节流下的进度 QQQ")):
        task = asyncio.create_task(_deliver(bot, _msg("@spx_bot 干活")))
        await asyncio.sleep(0.05)
        await _deliver(bot, _cmd("/stop"))
        gate.set()
        await task
    body = _last_body(bot)
    assert "任务已被停止" in body, f"节流把停止卡吃了：{body!r}"
    assert "节流下的进度 QQQ" in body


async def test_history_image_attachment_is_downloaded_into_context(bot, monkeypatch):
    """群历史里别人发的图片，被 @ 时要跟着上下文一起下载给 agent（Lark 已有行为）。"""
    monkeypatch.setattr(bot.feishu, "_download_sync",
                        lambda *a, **k: "/tmp/history-img.jpg")
    # 别人发了张图（不 @ bot，只进缓冲）
    photo = _photo(uid=888)
    photo["chat"] = {"id": -100123, "type": "supergroup"}
    assert gw.build_event(bot, photo) is not None
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _msg("@spx_bot 看下上面那张图"))
    assert len(calls) == 1
    assert "/tmp/history-img.jpg" in calls[0]["message"], \
        f"历史图片没被带进上下文：{calls[0]['message']!r}"


async def test_group_message_without_mention_still_feeds_context(bot):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
        await _deliver(bot, _msg("SGB 开户又失败了", uid=888))
        await _deliver(bot, _msg("@spx_bot 怎么回事"))
    assert len(calls) == 1
    assert "SGB 开户又失败了" in calls[0]["message"]


async def test_dispatch_batch_completion_wakes_the_parent_on_telegram(bot):
    """子任务批次跑完 → 唤醒父 agent 并把结果内联。Telegram 没有 lark-cli 的 user
    身份，必须落到 wake_thread_internal 兜底上，不能就此断链。"""
    with mock.patch.object(dispatcher, "_resolve_bot_open_id",
                           new_callable=mock.AsyncMock, return_value=""):
        calls = []
        with mock.patch.object(dispatcher, "run_agent", _agent(calls)):
            await dispatcher._dispatch_wake_parent({
                "bot": bot, "thread": "c-100123", "anchor": "-100123:900",
                "chat": "-100123", "user": "777",
                "results": [("子任务甲", "t901", "结论是 A", True),
                            ("子任务乙", "t902", "超时了", False)],
            })
    assert calls, "批次完成后父 agent 没被唤醒"
    prompt = calls[0]["message"]
    assert "结论是 A" in prompt and "子任务乙" in prompt and "未完成" in prompt


async def test_wake_does_not_call_lark_endpoints_on_a_telegram_profile(bot):
    """telegram profile 上 send-as-user 唤醒必须直接放弃，不能去打 Lark 的接口。

    `_resolve_bot_open_id`（dispatcher.py:3118）是按 `profile.domain` 拼
    `/open-apis/auth/v3/tenant_access_token/internal` 的；telegram profile 的 domain 是
    api.telegram.org、app_secret 就是 bot token —— 真打过去等于白等 10s 超时，
    还把 bot token POST 到一个 404 路径。唤醒应当直接落到 wake_thread_internal 兜底。
    """
    dispatcher._BOT_OPEN_ID_CACHE.pop("42", None)
    seen: list[str] = []

    def _fake_urlopen(req, *a, **kw):
        seen.append(getattr(req, "full_url", str(req)))
        raise OSError("blocked by test")

    with mock.patch("urllib.request.urlopen", _fake_urlopen):
        ok = await dispatcher.wake_thread_as_user(bot, "-100123:900", "醒醒")
    assert ok is False
    assert seen == [], f"telegram profile 去打了 Lark 的接口：{seen}"
