import json
import types

from feishu_post import parse_post_content, strip_lark_mentions
from thread_context import _extract


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
    assert "读不到" in text
    assert atts == []


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
