import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dispatcher import _format_dispatch_body, _format_usage_footer


def test_usage_footer_uses_codex_context_window():
    footer = _format_usage_footer(
        {
            "input_tokens": 53_277,
            "_cached_input_tokens": 52_608,
            "output_tokens": 61,
            "_context_window": 258_400,
        },
        "gpt-5.5",
    )

    assert "53.3k / 258.4k" in footer


def test_dispatch_body_includes_full_prompt():
    prompt = "第一行任务\n第二行要求\n不要再派发子 agent"

    body = _format_dispatch_body(prompt)

    assert "cc-lark 子任务已派发" in body
    assert "【完整任务提示词】" in body
    assert prompt in body


def test_strip_fake_usage_footer_removes_model_imitated_line():
    from dispatcher import _strip_fake_usage_footer

    # 11-10 prod 告警分诊：模型照抄历史卡片尾巴编了一行假的，bot 再追加真的 → 两行
    text = "结论已经回在原告警下面。\n\n这类告警一周只出了两次。\n\n— 📊 上下文 48k / 1M (4.8%)"
    assert _strip_fake_usage_footer(text) == "结论已经回在原告警下面。\n\n这类告警一周只出了两次。"

    # 中间位置、qoder 占比样式、短横线写法也认
    mid = "第一段\n\n— 📊 上下文 12.3%\n\n第二段\n- 📊 上下文 1k / 200k (0.5%) · 本轮消耗 3k"
    assert _strip_fake_usage_footer(mid) == "第一段\n\n第二段"


def test_strip_fake_usage_footer_keeps_quoted_or_unrelated_text():
    from dispatcher import _strip_fake_usage_footer

    quoted = "卡片尾巴长这样：`— 📊 上下文 48k / 1M (4.8%)`"
    assert _strip_fake_usage_footer(quoted) == quoted
    other = "📊 状态\n- 列表项\n— 普通破折号"
    assert _strip_fake_usage_footer(other) == other
    assert _strip_fake_usage_footer("") == ""
    assert _strip_fake_usage_footer(None) is None
