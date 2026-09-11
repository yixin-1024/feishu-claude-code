"""卡片 markdown 归一：LaTeX 降级 + 中文标点旁的加粗救回来。

2026-09-03 现场：agy 的回答里 `$\\rightarrow$` 原样显示成一串反斜杠，
`她觉得你**"狡辩、自私、怕疼"**；` 的星号也裸奔——Lark 卡片没有公式渲染，
且 CommonMark 的 flanking 规则不认「汉字 + `**` + 标点」这种开界符。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lark_md import normalize_lark_md as norm


def test_inline_latex_becomes_unicode():
    src = "你跟她讲医学事实 $\\rightarrow$ 她觉得你狡辩"
    assert norm(src) == "你跟她讲医学事实 → 她觉得你狡辩"


def test_latex_variants():
    assert norm("$\\alpha \\le \\beta$") == "α ≤ β"
    assert norm("\\(x^2 + y_1\\)") == "x² + y₁"
    assert norm("$$E = mc^2$$") == "E = mc²"
    assert norm("$\\frac{a}{b}$ 与 $\\frac{x+1}{2}$") == "a/b 与 (x+1)/2"
    assert norm("$10^{5}$ 分之一") == "10⁵ 分之一"
    # 没收录的命令至少别留反斜杠
    assert norm("$\\foobar x$") == "foobar x"


def test_money_is_not_math():
    assert norm("价格从 $5 涨到 $8，别动我") == "价格从 $5 涨到 $8，别动我"
    assert norm("成本 $100 和 $200") == "成本 $100 和 $200"
    # 钱中间蹭到反斜杠（Windows 路径）也不能被当公式吃掉
    assert norm("花了 $5 存到 C:\\Users 里 $8") == "花了 $5 存到 C:\\Users 里 $8"


def test_shell_vars_are_not_math():
    assert norm("export $A_B and $C_D 完事") == "export $A_B and $C_D 完事"
    assert norm("用 $HOME 和 $PATH_VAR 变量") == "用 $HOME 和 $PATH_VAR 变量"


def test_bold_hugging_cjk_punctuation_is_rescued():
    src = '她觉得你**“狡辩、自私、怕疼”**；'
    assert norm(src) == '她觉得你“**狡辩、自私、怕疼**”；'


def test_bold_with_leading_quote_only():
    src = "要的是一个**“你为我自残”的投名状**。"
    assert norm(src) == "要的是一个“**你为我自残”的投名状**。"


def test_already_renderable_bold_untouched():
    for src in (
        "1. **认知闭环无法用逻辑打破**：这类人有",
        "普通 **加粗** 不动",
        "你**加粗**了 也不动",
        "obj.__init__(x) 不动",
    ):
        assert norm(src) == src


def test_all_punctuation_body_left_alone():
    # 挪完没有实字可加粗，宁可原样，别把内容吃掉
    assert norm('**“”** x') == '**“”** x'


def test_strikethrough_uses_same_rule():
    assert norm("~~你**“删”**~~") == "~~你“**删**”~~"


def test_code_is_never_touched():
    src = "行内 `**not bold**` 和 `$\\rightarrow$`\n```\n$\\rightarrow$ **x**\n```\n"
    assert norm(src) == src


def test_unclosed_fence_protected():
    # 流式打字机中途，围栏还没闭合，里面的内容照样不能改
    src = "看代码：\n```python\nprice = f\"${x}\"  # **x**\n"
    assert norm(src) == src


def test_idempotent_and_empty():
    src = '$\\rightarrow$ 她说你**“狡辩”**；'
    assert norm(norm(src)) == norm(src)
    assert norm("") == ""
    assert norm(None) is None


def test_card_dict_normalizes_content():
    import feishu_client

    card = feishu_client._card_dict('走到 $\\rightarrow$ 你**“狡辩”**；')
    content = card["body"]["elements"][0]["content"]
    assert "\\rightarrow" not in content
    assert content == '走到 → 你“**狡辩**”；'
