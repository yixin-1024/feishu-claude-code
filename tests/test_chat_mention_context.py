"""普通群（非话题）里只 @ 不写字 / 引用回复只写 @：要带上前面的消息去跑，不能静默丢掉。

2026-10-20 实例：Megan 在「【SGB】Swift UETR 获取」群里先发图、再发「这个银行打过吗」、
最后单独补一条 @spx财务，又引用那句问题再 @ 了一次——两条 @ 去掉 mention 后都是空的，
被 _extract_message_text 直接 return None，bot 一个字都没回。
"""

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher
import thread_context
from bot_config import Profile
from bot_instance import BotInstance
from run_control import ActiveRunRegistry

CHAT = "oc_uetr"


def _lm(mid, text="", *, sender="ou_megan", app=False, mtype="text", image_key=""):
    """list / get 接口返回的 Message 替身（只带 thread_context 会碰的字段）。"""
    if mtype == "image":
        content = json.dumps({"image_key": image_key})
    else:
        content = json.dumps({"text": text})
    return SimpleNamespace(
        message_id=mid, msg_type=mtype, body=SimpleNamespace(content=content),
        sender=SimpleNamespace(id=sender, sender_type="app" if app else "user"),
        create_time="1760935540000", mentions=None,
    )


class StubFeishu:
    def __init__(self, recent, by_id=None, list_error=None):
        self.recent = recent
        self.by_id = by_id or {}
        self.list_error = list_error
        self._app_id = "cli_self"
        self.list_calls: list[tuple] = []
        self.downloads: list[tuple] = []

    async def list_chat_messages(self, chat_id, start_time, limit=15):
        self.list_calls.append((chat_id, start_time, limit))
        if self.list_error:
            raise RuntimeError(self.list_error)
        return list(self.recent)

    async def get_message(self, message_id):
        return self.by_id.get(message_id)

    async def batch_resolve_names(self, open_ids):
        return {"ou_megan": "Megan", "ou_louie": "Lu Yixin"}

    async def download_file(self, message_id, file_key, msg_type="file", file_name=""):
        self.downloads.append((message_id, file_key))
        return f"/tmp/dl/{file_key}"

    def get_card_text(self, message_id):
        return ""


# ── thread_context.build_chat_context ─────────────────────────


async def test_chat_context_takes_recent_messages_and_drops_current():
    fs = StubFeishu([
        _lm("om_img", mtype="image", image_key="img_bank"),
        _lm("om_q", "这个银行打过吗"),
        _lm("om_at", ""),  # 当前这条 @
    ])
    ctx, paths, err = await thread_context.build_chat_context(
        fs, CHAT, "om_at", now=1_760_000_000)
    assert err is None
    assert "这个银行打过吗" in ctx and "Megan" in ctx
    assert "【群里最近的消息 · 2 条" in ctx
    assert paths == ["/tmp/dl/img_bank"]
    chat_id, start, _limit = fs.list_calls[0]
    assert chat_id == CHAT and start == 1_760_000_000 - thread_context.CHAT_CONTEXT_WINDOW_SEC


async def test_chat_context_pulls_quoted_message_outside_window():
    fs = StubFeishu([_lm("om_bot", "✅", sender="cli_self", app=True)],
                    by_id={"om_q": _lm("om_q", "这个银行打过吗")})
    ctx, _paths, err = await thread_context.build_chat_context(
        fs, CHAT, "om_at", parent_id="om_q")
    assert err is None
    first, second = ctx.splitlines()[1:3]
    assert "被引用" in first and "这个银行打过吗" in first
    assert "(自己)" in second


async def test_chat_context_marks_quoted_message_already_in_window():
    fs = StubFeishu([_lm("om_q", "这个银行打过吗"), _lm("om_x", "别的")])
    ctx, _paths, _err = await thread_context.build_chat_context(
        fs, CHAT, "om_at", parent_id="om_q")
    assert ctx.count("这个银行打过吗") == 1
    assert "Megan（被引用）" in ctx


async def test_chat_context_reports_list_error():
    fs = StubFeishu([], list_error="99991672 no permission")
    ctx, paths, err = await thread_context.build_chat_context(fs, CHAT, "om_at")
    assert (ctx, paths) == ("", []) and "99991672" in err


async def test_chat_context_keeps_only_latest_limit():
    msgs = [_lm(f"om_{i}", f"第{i}条") for i in range(thread_context.CHAT_CONTEXT_LIMIT + 1)]
    fs = StubFeishu(msgs)
    ctx, _p, _e = await thread_context.build_chat_context(fs, CHAT, "om_current")
    assert "第0条" not in ctx
    assert f"第{thread_context.CHAT_CONTEXT_LIMIT}条" in ctx


# ── dispatcher：非话题群里的空 @ 不再被丢 ──────────────────────


def _bot(feishu):
    bot = BotInstance.__new__(BotInstance)
    bot.profile = Profile(
        name="test", app_id="cli_self", app_secret="s",
        platform="lark", domain="open.larksuite.com", default_cwd="/tmp",
    )
    bot.feishu = feishu
    bot.active_runs = ActiveRunRegistry()
    bot.store = mock.MagicMock()
    bot.store.get_current = mock.AsyncMock(
        return_value=SimpleNamespace(runner="claude", session_id=None, model="opus"))
    return bot


def _event(text, mid="om_at", parent_id="", mtype="text"):
    content = json.dumps({"text": text}) if mtype == "text" else json.dumps(
        {"title": "", "content": [[{"tag": "at", "user_id": "@_user_1", "user_name": "spx财务"}]]})
    return mock.Mock(
        message_type=mtype, content=content, message_id=mid, chat_id=CHAT,
        parent_id=parent_id, mentions=[mock.Mock(key="@_user_1", id=mock.Mock(open_id="ou_bot"))],
    )


async def _process(bot, msg, monkeypatch):
    started = mock.AsyncMock()
    monkeypatch.setattr(dispatcher, "_start_agent_run", started)
    await dispatcher._process_message(bot, "ou_megan", CHAT, True, "", msg)
    return started


async def test_bare_mention_in_plain_group_runs_with_recent_messages(monkeypatch):
    fs = StubFeishu([_lm("om_q", "这个银行打过吗")])
    bot = _bot(fs)
    started = await _process(bot, _event("@_user_1"), monkeypatch)
    started.assert_awaited_once()
    text = started.await_args.args[6]
    assert "这个银行打过吗" in text and "没有写正文" in text


async def test_bare_post_mention_in_plain_group_is_not_dropped(monkeypatch):
    fs = StubFeishu([_lm("om_q", "这个银行打过吗")])
    bot = _bot(fs)
    started = await _process(bot, _event("", mtype="post"), monkeypatch)
    started.assert_awaited_once()
    assert "这个银行打过吗" in started.await_args.args[6]


async def test_quote_reply_with_text_gets_quoted_message(monkeypatch):
    fs = StubFeishu([], by_id={"om_q": _lm("om_q", "HKD 563,697.86 Sender: Huang Zhenhua")})
    bot = _bot(fs)
    started = await _process(
        bot, _event("@_user_1 再查一下这笔", parent_id="om_q"), monkeypatch)
    text = started.await_args.args[6]
    assert text.startswith("【用户引用的消息】")
    assert "Huang Zhenhua" in text and text.endswith("再查一下这笔")
    assert fs.list_calls == []  # 有正文时只补被引用的那条，不翻群消息


async def test_plain_text_mention_without_quote_is_untouched(monkeypatch):
    fs = StubFeishu([_lm("om_q", "不该被带上")])
    bot = _bot(fs)
    started = await _process(bot, _event("@_user_1 查状态"), monkeypatch)
    assert started.await_args.args[6] == "查状态"
    assert fs.list_calls == []


async def test_bare_mention_when_history_unreadable_still_reaches_run(monkeypatch):
    """拉不到群消息时空正文照样交给 _start_agent_run，由它回「补一句」的提示，不静默。"""
    fs = StubFeishu([], list_error="boom")
    bot = _bot(fs)
    started = await _process(bot, _event("@_user_1"), monkeypatch)
    started.assert_awaited_once()
    assert started.await_args.args[6] == ""


async def test_bare_mention_while_running_is_injected_with_context(monkeypatch):
    """12:45:43 那条：上一笔还在跑时单独补的 @，要带着刚发的问题插进去，而不是被吞。"""
    fs = StubFeishu([_lm("om_img", mtype="image", image_key="img_bank"),
                     _lm("om_q", "这个银行打过吗")])
    fs.reply_text = mock.AsyncMock()
    bot = _bot(fs)

    class FakeInput:
        open = True
        sent: list = []

        async def send(self, text, priority="next"):
            self.sent.append(text)
            return "uid-1"

    run = bot.active_runs.start_run("ou_megan", CHAT, "card_1")
    run.input = FakeInput()
    run.on_inject = mock.AsyncMock()
    handled = await dispatcher._inject_or_steer(
        bot, "ou_megan", CHAT, True, "", _event("@_user_1"), asyncio.Lock())
    assert handled is True
    (body,) = run.input.sent
    assert "这个银行打过吗" in body and "/tmp/dl/img_bank" in body
