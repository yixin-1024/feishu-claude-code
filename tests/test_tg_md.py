"""markdown → Telegram HTML 渲染 + 4096 分段。

Telegram 只认 <b>/<i>/<code>/<pre>/<a>/<s>/<blockquote> 这几个标签，非法标签或
没闭合的实体会让整条消息 400 发不出去，所以这里逐条钉住降级规则。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tg_md import render_html, split_md, tail_md


def test_escapes_html_metacharacters():
    assert render_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_inline_marks():
    out = render_html("**粗** *斜* `code` ~~删~~")
    assert out == "<b>粗</b> <i>斜</i> <code>code</code> <s>删</s>"


def test_header_becomes_bold():
    assert render_html("## 小标题") == "<b>小标题</b>"
    assert render_html("### 带 **粗** 的标题") == "<b>带 <b>粗</b> 的标题</b>"


def test_bullets_and_rule():
    out = render_html("- a\n  - b\n---")
    assert out == "• a\n  • b\n──────────"


def test_code_block_keeps_literals():
    out = render_html("```python\nx = a * b  # <tag> & 'q'\n```")
    assert out == (
        '<pre><code class="language-python">'
        "x = a * b  # &lt;tag&gt; &amp; 'q'</code></pre>"
    )


def test_unclosed_fence_is_still_a_block():
    """流式中途必然出现只有开头的围栏，不能因此渲染出裸 ```。"""
    out = render_html("看代码：\n```sh\necho hi")
    assert out == '看代码：\n<pre><code class="language-sh">echo hi</code></pre>'


def test_inline_code_content_is_not_emphasised():
    out = render_html("`a * b * c` 和 `__init__`")
    assert out == "<code>a * b * c</code> 和 <code>__init__</code>"


def test_link_url_with_underscores_survives():
    out = render_html("见 [文档](https://ex.com/a_b_c)")
    assert out == '见 <a href="https://ex.com/a_b_c">文档</a>'


def test_nested_placeholders_are_fully_restored():
    """链接文字里嵌行内代码：占位符套占位符，只还原一轮会把控制字符发出去、文字丢失。"""
    out = render_html("[表 `round up` 见此](https://ex.com/t)")
    assert out == '<a href="https://ex.com/t">表 <code>round up</code> 见此</a>'
    assert "\x00" not in out and "\x01" not in out


def test_non_http_link_degrades_to_text():
    out = render_html("[本地](./a.md)")
    assert out == "本地 (./a.md)"


def test_table_becomes_monospace_block():
    out = render_html("| a | b |\n|---|---|\n| 1 | 2 |")
    assert out.startswith("<pre>|") and out.endswith("</pre>")
    assert "&" not in out  # 表格内容原样，没有多余实体


def test_blockquote():
    assert render_html("> 一\n> 二") == "<blockquote>一\n二</blockquote>"


def test_snake_case_not_italicised():
    assert render_html("函数 do_something_now 不该变斜体") == (
        "函数 do_something_now 不该变斜体"
    )


def test_split_md_keeps_chunks_under_limit():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = split_md(text, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)
    # 内容不丢：拼回去每行都还在
    joined = "\n".join(chunks)
    assert joined.count("line 199") == 1


def test_split_md_reopens_code_fence():
    body = "\n".join(f"row {i}" for i in range(120))
    chunks = split_md(f"前言\n```py\n{body}\n```", limit=300)
    assert len(chunks) > 1
    assert chunks[0].rstrip().endswith("```")
    assert chunks[1].startswith("```py")
    # 每段的围栏都是成对的，渲染出来才不会串
    assert all(c.count("```") % 2 == 0 for c in chunks)


def test_split_md_handles_single_huge_line():
    """压成一行的 JSON / base64：没有行边界可切，只能硬切，但不能丢字符。"""
    chunks = split_md("y" * 5000, limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert sum(c.count("y") for c in chunks) == 5000


def test_tail_md_keeps_tail_and_reopens_the_fence():
    """流式帧切在代码块中间：尾巴要用**原 marker + 语言**重新开一个围栏。

    不需要闭合 —— 代码块一直开到消息结尾，渲染出来就是一整块 <pre>，这正是想要的。
    """
    out = tail_md("```py\n" + "\n".join(f"r{i}" for i in range(500)), limit=200)
    assert out.startswith("…（前文略）")
    assert "r499" in out
    assert out.split("\n")[1] == "```py"
    assert render_html(out).startswith("…（前文略）\n<pre><code class=\"language-py\">")


def test_tail_md_reopens_tilde_fence_with_tildes():
    out = tail_md("~~~\n" + "\n".join(f"*r{i}*" for i in range(400)), limit=100)
    assert out.split("\n")[1] == "~~~"
    assert "<pre>" in render_html(out)


def test_tail_md_passthrough_when_short():
    assert tail_md("短文本") == "短文本"


# ── 交叉嵌套（拿真 Telegram parser 校过的回归）─────────────────

def test_triple_star_is_bold_italic_not_crossed_tags():
    """`***x***` 曾渲染成 <b><i>x</b></i>，Telegram 直接 400 整条发不出去。"""
    assert render_html("***e***") == "<b><i>e</i></b>"
    out = render_html("a*b*c **d** ***e*** 5 * 3")
    assert out == "a*b*c <b>d</b> <b><i>e</i></b> 5 * 3"


def test_unbalanced_result_falls_back_to_plain_escaped_line():
    """兜底闸门：拼出交叉/未知标签时整行退回纯文本，宁可不加粗也不能发不出去。"""
    import tg_md

    assert tg_md._tags_balanced("<b><i>x</i></b>")
    assert not tg_md._tags_balanced("<b><i>x</b></i>")
    assert not tg_md._tags_balanced("<b>x")
    assert not tg_md._tags_balanced("<div>x</div>")


def test_underscore_emphasis_is_literal():
    """这是个写代码的 bot：`__init__` / `_private` 必须原样，不能被当强调。"""
    assert render_html("__init__ 和 _private_ 与 ___x___") == (
        "__init__ 和 _private_ 与 ___x___"
    )


def test_italic_works_next_to_cjk():
    """CommonMark 的 flanking 规则会让中文里的 *斜体* 失效，这里放行 CJK 边界。"""
    assert render_html("这是*重点*内容") == "这是<i>重点</i>内容"
    assert render_html("**粗**里有*斜*") == "<b>粗</b>里有<i>斜</i>"


def test_italic_not_applied_to_identifiers_or_globs():
    for raw in ("a*b*c", "2*3*4", "ls *.py 和 rm -rf *"):
        assert render_html(raw) == raw


def test_bold_next_to_chinese_punctuation():
    """Lark 那边要修 flanking，Telegram 这边 ** 没有边界约束，直接就该成立。"""
    assert render_html('她说你**“狡辩”**；') == "她说你<b>“狡辩”</b>；"
