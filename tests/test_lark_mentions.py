import json
import types

import pytest

from feishu_post import parse_post_content, strip_lark_mentions
from thread_context import _extract, _fetch_card_texts_as_user, _needs_user_fetch, _unwrap_card


def _mention(key="@_user_1", name="Alice"):
    return types.SimpleNamespace(key=key, name=name)


def _thread_msg(msg_type, content, mentions=None, sender_type="user"):
    return types.SimpleNamespace(
        msg_type=msg_type,
        body=types.SimpleNamespace(content=json.dumps(content, ensure_ascii=False)),
        mentions=mentions or [],
        message_id="om_1",
        sender=types.SimpleNamespace(sender_type=sender_type, id="ou_1"),
    )


def test_strip_lark_mentions_removes_placeholder_without_name():
    text = strip_lark_mentions("@_user_1 你去看看 @_user_2", [
        _mention("@_user_1", "Alice"),
        _mention("@_user_2", "Bob"),
    ])

    assert text == "你去看看"


def test_thread_text_context_removes_mentions():
    msg = _thread_msg(
        "text",
        {"text": "@_user_1 这个给下层 agent"},
        mentions=[_mention("@_user_1", "Alice")],
    )

    text, atts = _extract(msg)

    assert text == "这个给下层 agent"
    assert atts == []


_DEGRADED_CARD = {
    "title": None,
    "elements": [[
        {"tag": "img", "image_key": "img_v3_0214g_ae30cb6e"},
        {"tag": "text", "text": " "},
        {"tag": "text", "text": ""},
    ]],
}


def test_forwarded_card_leaves_a_trace_instead_of_vanishing():
    """自然人转发进来的卡片：Lark 只给占位图不给正文，至少要留一行痕迹。"""
    msg = _thread_msg("interactive", _DEGRADED_CARD, sender_type="user")

    text, atts = _extract(msg)

    assert "转发进来的卡片" in text
    # 那张占位图是通用插图（火箭），零信息且 bot 身份下不动，不能当附件抓
    assert atts == []


def test_own_card_prefers_cache_over_degraded_placeholder():
    """bot 自己发的卡片同样退化成占位图形态，必须优先走卡片文本缓存还原。"""
    msg = _thread_msg("interactive", _DEGRADED_CARD, sender_type="app")
    feishu = types.SimpleNamespace(get_card_text=lambda mid: "部署完成 ✅")

    text, atts = _extract(msg, feishu)

    assert text == "部署完成 ✅"
    assert atts == []


def test_bot_card_without_cache_is_not_labelled_as_forwarded():
    """缓存丢了（bot 重启过）的自家旧卡片不该被冤枉成转发。"""
    msg = _thread_msg("interactive", _DEGRADED_CARD, sender_type="app")
    feishu = types.SimpleNamespace(get_card_text=lambda mid: "")

    text, atts = _extract(msg, feishu)

    assert "转发进来的卡片" not in text
    assert "没捞回来" in text
    assert atts == []


def test_user_fetched_card_text_wins_over_placeholder():
    """user 身份捞回真身后，上下文里就该是卡片正文，而不是占位提示。"""
    msg = _thread_msg("interactive", _DEGRADED_CARD, sender_type="user")
    feishu = types.SimpleNamespace(get_card_text=lambda mid: "")

    text, atts = _extract(msg, feishu, {"om_1": "公司注册号 67441336"})

    assert text == "公司注册号 67441336"
    assert atts == []


def test_needs_user_fetch_skips_cards_already_in_cache():
    """缓存里有真身的（bot 自己刚发的卡）不必再花一次 subprocess 去捞。"""
    msg = _thread_msg("interactive", _DEGRADED_CARD, sender_type="app")

    assert _needs_user_fetch(msg, types.SimpleNamespace(get_card_text=lambda mid: "")) is True
    assert _needs_user_fetch(msg, types.SimpleNamespace(get_card_text=lambda mid: "真身")) is False
    assert _needs_user_fetch(_thread_msg("text", {"text": "hi"})) is False


def test_unwrap_card_strips_lark_cli_wrapper():
    assert _unwrap_card("<card>\n正文\n</card>") == "正文"
    assert _unwrap_card("裸文本") == "裸文本"
    assert _unwrap_card("") == ""


@pytest.mark.asyncio
async def test_fetch_card_texts_parses_lark_cli_output(monkeypatch):
    """lark-cli 的 mget 输出 → message_id → 正文。"""
    payload = json.dumps({"data": {"messages": [
        {"message_id": "om_a", "content": "<card>\nA 的正文\n</card>"},
        {"message_id": "om_b", "content": "B 的正文"},
        {"message_id": "om_c", "content": ""},
    ]}}).encode()
    seen = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return payload, b""

    async def fake_exec(*cmd, **kw):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    out = await _fetch_card_texts_as_user(["om_a", "om_b", "om_c"], "spx")

    assert out == {"om_a": "A 的正文", "om_b": "B 的正文"}
    assert "--as" in seen["cmd"] and "user" in seen["cmd"]
    assert "om_a,om_b,om_c" in seen["cmd"]
    assert "spx" in seen["cmd"]


@pytest.mark.asyncio
async def test_fetch_card_texts_degrades_quietly_when_lark_cli_fails(monkeypatch):
    """lark-cli 挂了 / user 授权过期 → 返回空，caller 退回占位提示，不能抛。"""
    class _Proc:
        returncode = 1

        async def communicate(self):
            return b"", b"not authorized"

    async def fake_exec(*cmd, **kw):
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    assert await _fetch_card_texts_as_user(["om_a"], "spx") == {}
    # 没有 profile 就别 spawn
    assert await _fetch_card_texts_as_user(["om_a"], "") == {}


def test_normal_bot_card_still_parses_markdown():
    """转发形态的判别不能误伤 bot 自己发的卡片（elements 是 list[dict]）。"""
    msg = _thread_msg("interactive", {
        "elements": [{"tag": "markdown", "content": "部署完成 ✅"}],
    })

    text, atts = _extract(msg)

    assert text == "部署完成 ✅"
    assert atts == []


def test_post_at_nodes_are_not_rendered():
    raw = json.dumps({
        "zh_cn": {
            "content": [[
                {"tag": "at", "user_id": "ou_1", "user_name": "Alice"},
                {"tag": "text", "text": " 你去看看"},
            ]]
        }
    }, ensure_ascii=False)

    assert parse_post_content(raw) == "你去看看"
