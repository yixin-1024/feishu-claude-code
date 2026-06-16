import json
import types

from feishu_post import parse_post_content, strip_lark_mentions
from thread_context import _extract


def _mention(key="@_user_1", name="Alice"):
    return types.SimpleNamespace(key=key, name=name)


def _thread_msg(msg_type, content, mentions=None):
    return types.SimpleNamespace(
        msg_type=msg_type,
        body=types.SimpleNamespace(content=json.dumps(content, ensure_ascii=False)),
        mentions=mentions or [],
        message_id="om_1",
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
