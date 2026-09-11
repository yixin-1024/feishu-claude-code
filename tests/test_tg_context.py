"""Telegram 会话缓冲：落盘 / 回放 / 裁剪 / 姓名表。

Telegram Bot API 没有"读历史"的接口，上下文全靠这份缓冲，所以它的持久化必须
经得起 /restart —— 否则重启后 last_seen 指向一条已经不存在的消息，上下文直接
变空（比报错更难发现）。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tg_context
from tg_context import TgBuffer


def _rec(buf, mid, text, thread="c-100", uid="777", name="Yixin", bot=False, ts=0):
    buf.record(
        message_id=mid, chat_id="-100", thread_id=thread, user_id=uid, name=name,
        is_bot=bot, content=json.dumps({"text": text}, ensure_ascii=False),
        ts_ms=ts or 1700000000000,
    )


def _texts(msgs):
    return [json.loads(m.body.content)["text"] for m in msgs]


def test_records_are_readable_as_lark_shaped_messages():
    buf = TgBuffer("p1")
    _rec(buf, "-100:1", "第一句")
    msg = buf.thread_messages("c-100")[0]
    # thread_context._extract / _sender_label 就吃这几个字段
    assert msg.message_id == "-100:1"
    assert msg.msg_type == "text"
    assert json.loads(msg.body.content)["text"] == "第一句"
    assert msg.sender.id == "777" and msg.sender.sender_type == "user"
    assert msg.create_time == "1700000000000"


def test_bot_messages_are_marked_as_app_sender():
    buf = TgBuffer("p2")
    _rec(buf, "-100:2", "我的回答", uid="42", name="spx_bot", bot=True)
    assert buf.thread_messages("c-100")[0].sender.sender_type == "app"


def test_threads_are_isolated():
    buf = TgBuffer("p3")
    _rec(buf, "-100:1", "群主线", thread="c-100")
    _rec(buf, "-100:2", "子会话", thread="t80")
    assert _texts(buf.thread_messages("c-100")) == ["群主线"]
    assert _texts(buf.thread_messages("t80")) == ["子会话"]
    assert buf.thread_of("-100:2") == "t80"
    assert buf.thread_of("不存在") == ""


def test_update_text_overwrites_streamed_card():
    buf = TgBuffer("p4")
    _rec(buf, "-100:3", "⏳ 思考中...", uid="42", bot=True)
    buf.update_text("-100:3", "最终结论")
    assert _texts(buf.thread_messages("c-100")) == ["最终结论"]


def test_survives_reload_from_disk():
    buf = TgBuffer("p5")
    _rec(buf, "-100:1", "重启前说的")
    _rec(buf, "-100:2", "bot 的回答", uid="42", bot=True)
    buf.update_text("-100:2", "bot 的最终回答")

    again = TgBuffer("p5")   # 模拟 /restart 之后重新加载
    assert _texts(again.thread_messages("c-100")) == ["重启前说的", "bot 的最终回答"]
    assert again.names(["777"]) == {"777": "Yixin"}


def test_per_thread_cap_drops_oldest(monkeypatch):
    monkeypatch.setattr(tg_context, "MAX_PER_THREAD", 3)
    buf = TgBuffer("p6")
    for i in range(6):
        _rec(buf, f"-100:{i}", f"第 {i} 句")
    assert _texts(buf.thread_messages("c-100")) == ["第 3 句", "第 4 句", "第 5 句"]
    # 被裁掉的消息也从索引里摘掉，不残留悬空引用
    assert buf.thread_of("-100:0") == ""


def test_same_message_id_is_upserted_not_duplicated():
    buf = TgBuffer("p7")
    _rec(buf, "-100:1", "a")
    _rec(buf, "-100:1", "b")
    assert _texts(buf.thread_messages("c-100")) == ["b"]


def test_limit_returns_the_tail():
    buf = TgBuffer("p8")
    for i in range(10):
        _rec(buf, f"-100:{i}", f"m{i}")
    assert _texts(buf.thread_messages("c-100", limit=2)) == ["m8", "m9"]


def test_corrupt_lines_do_not_break_loading(tmp_path):
    buf = TgBuffer("p9")
    _rec(buf, "-100:1", "好行")
    with open(buf.path, "a", encoding="utf-8") as f:
        f.write("{不是 json\n\n")
    assert _texts(TgBuffer("p9").thread_messages("c-100")) == ["好行"]


def test_unwritable_path_is_not_fatal(monkeypatch, capsys):
    """缓冲写不进去只该丢上下文，不该让消息处理挂掉。"""
    monkeypatch.setenv("CC_TG_BUFFER_DIR", "/proc/nonexistent-dir")
    buf = TgBuffer("p10")
    _rec(buf, "-100:1", "内存里还是有的")
    assert _texts(buf.thread_messages("c-100")) == ["内存里还是有的"]
