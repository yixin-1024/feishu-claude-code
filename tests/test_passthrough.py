"""passthrough.is_builtin_passthrough 的语义回归。

重点是话题群陷阱的根因：/compact 被埋在话题上下文中间时必须返回 False，
dispatcher 才能据此"只在消息开头是控制命令时跳过上下文注入"。
"""

from passthrough import is_builtin_passthrough


def test_exact_compact():
    assert is_builtin_passthrough("/compact") is True


def test_compact_with_args():
    assert is_builtin_passthrough("/compact focus on db schema") is True
    assert is_builtin_passthrough("/compact\nfocus") is True


def test_leading_whitespace_ok():
    # 用户消息常带前导空白，lstrip 后仍算命中
    assert is_builtin_passthrough("  /compact") is True


def test_compactfoo_is_not_compact():
    assert is_builtin_passthrough("/compactfoo") is False


def test_buried_compact_is_false():
    # 话题群注入上下文后，/compact 沦为正文中间的普通文字 → 不算透传命令
    buried = "【话题新增 · 2 条】\n[1] bot: ...\n\n【用户刚刚 @ 你并说】\n/compact"
    assert is_builtin_passthrough(buried) is False


def test_plain_text_and_empty():
    assert is_builtin_passthrough("帮我看下 bug") is False
    assert is_builtin_passthrough("") is False
    assert is_builtin_passthrough(None) is False  # type: ignore[arg-type]
