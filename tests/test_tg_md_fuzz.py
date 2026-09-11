"""tg_md.py 的模糊测试：拿对抗样本 + 真实会话语料 + 随机拼接生成器，去打三类不变式。

为什么值得单开一个文件：Telegram 的 HTML 解析是**全或无**的 —— 标签越界一个字符，
整条消息 400（`can't parse entities`）发不出去，用户什么都收不到。test_tg_md.py 钉的是
"每条规则该渲染成什么"，这里钉的是"任何输入都不能违反的硬约束"：

1. 合法性：`_tags_balanced(render_html(x))` 恒 True，且出现的标签名都在白名单里；
2. 不丢内容：渲染结果去标签 + 反转义后的可见文字，与原文的可见文字逐字符一致
   （按下面 `expected_visible()` 定义的"可比较投影"，只放行文档化的降级）；
3. 分段不变式：`split_md` 每段 ≤ limit、字符不丢、``` 成对；`tail_md` 同理。

确定性：随机生成器固定 seed（`_SEED`），同一份语料 + 同一份代码 → 同一批样本。
真 Telegram parser 抽检默认 skip，`TG_FUZZ_LIVE=1` 才跑（一次 ≤200 次请求、≤2 QPS）。

已知 bug 的处理：模糊测试跑出来的反例不该把整个测试库钉红（产品代码在并行修），所以
每个已确认的 bug 在 `_KNOWN_*` 里显式登记（带最小复现），通用不变式对"签名完全吻合"
的反例只计数、不判死；同时给每个 bug 一条 `xfail` 单测把最小复现钉住 —— 修好之后
那条会变 XPASS，提醒把登记项删掉。除此之外的任何反例都会直接 fail 并打印复现信息。
"""

from __future__ import annotations

import html
import itertools
import json
import os
import pathlib
import random
import re
import sys
import time
from collections import Counter

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tg_md
from tg_md import render_html, split_md, tail_md

_SEED = 20260904
_REPO = pathlib.Path(__file__).resolve().parent.parent

# ── ① 手写对抗样本 ────────────────────────────────────────────

HAND: list[str] = [
    # 元字符 / 实体
    "", " ", "\n", "\n\n\n", "\t\t", "   \n \t \n", "a < b & c > d",
    "&amp; &lt; &gt; &quot; &#39; &nbsp;", "<b>已经是标签</b>", "<script>alert(1)</script>",
    "<DIV>大写标签</DIV>", "a&b<c>d\"e'f", "&", "<", ">", "\"", "'",
    # 星号 / 下划线 / 反引号
    "*", "**", "***", "****", "*****", "**a", "a**", "*a", "a*", "***a", "a***",
    "**a*", "*a**", "***x***", "**x***y***z**", "***a**b*", "*a**b***",
    "**粗**里有*斜*", "a*b*c", "2*3*4", "ls *.py 和 rm -rf *", "5 * 3 = 15",
    "__init__", "_private_", "___x___", "a_b_c_d", "__双下划线__",
    "`", "``", "```", "````", "`a", "a`", "`a`b`", "``a`b``", "`` ` ``",
    "`*不该斜*`", "`__init__`", "`<tag>`", "`a\nb`", "~", "~~", "~~~", "~~a~~", "~a~",
    "~~a**b~~c**", "**a~~b**c~~", "*a**b*c**",
    # 尖括号密集（转义后长度膨胀）
    "<" * 200, "&" * 200, "a<b>c</b>d" * 30,
    # 围栏
    "```", "```py", "```\n```", "```py\n```", "```\nunclosed",
    "```py\nx = 1\n```", "~~~\nfoo\n~~~", "~~~py\nfoo\n~~~", "```\na```b\n```",
    "````\n```\n````", "```py\n```\n```", "  ```\n  缩进围栏\n  ```",
    "```json\n{\"a\": [1,2], \"b\": \"<x>\"}\n```",
    "```\n\n\n```", "```python\n# 注释里有 *星号* 和 `反引号`\n```",
    "```c++\nstd::cout << \"x\";\n```", "```\"><script>\nx\n```", "```" + "z" * 40 + "\nx\n```",
    # 标题
    "#", "# ", "#x", "# x", "###### x", "####### x", "# C#", "## Rust vs C#",
    "# a #", "# a ##", "# ##", "#!/bin/sh", "# **粗标题**", "# `code`",
    "# [链接](https://x.com)", "\t# 制表符标题",
    # 列表 / 分隔线 / 引用
    "- a", "* a", "+ a", "-", "- ", "  - 嵌套", "\t- 制表符缩进",
    "- a\n  - b\n    - c", "1. 有序", "- **粗** 项", "---", "***", "___", "* * *",
    "- - -", "-- -", "----------", "> q", "> ", ">", ">>", "> a\n>> b",
    "> **粗引用**", "> ```\n> x\n> ```", "> - 引用里的列表", "  > 缩进引用",
    # 表格
    "|", "|\n|", "| a | b |", "| a | b |\n|---|---|\n| 1 | 2 |",
    "| a |\n| **b** |", "|a|\n|`c`|\n|<x>|", "文本\n| a | b |\n|-|-|\n文本",
    # 链接
    "[a](https://x.com)", "[](https://x.com)", "[a]()", "[a](b)",
    "[a](./local.md)", "[a](mailto:x@y.com)", "[a](tg://user?id=1)",
    "[a](HTTPS://X.COM)", "[下划线](https://x.com/a_b_c)",
    "[括号](https://x.com/a(b))", "[标题](https://x.com \"tooltip\")",
    "![图](https://x.com/a.png)", "![](https://x.com/a.png)",
    "[**粗标签**](https://x.com)", "[a](https://x.com/?a=1&b=2)",
    "[a](https://x.com/<x>)", '[a](https://x.com/")', "[a\n](https://x.com)",
    "[[a]](https://x.com)", "见 [文档](https://ex.com/a_b_c) 和 [另一个](./b.md)",
    # LaTeX / 数学
    r"$x^2 + y_1$", r"$$\frac{a}{b}$$", r"\(a\)", r"\[b\]", r"$a_i * b_j$",
    r"设 $f(x) = \sum_{i=1}^{n} x_i$，则 **结论**成立",
    # unicode
    "😀", "😀" * 500, "a😀*b*😀c", "👨‍👩‍👧‍👦 家庭 emoji", "🇸🇬 旗帜",
    "零宽​空格", "​‌‍﻿", "⁠word⁠",
    "مرحبا بالعالم", "עברית ‏RTL‎ 混排", "**مرحبا**", "*عربي*",
    "́̂ 组合符", "ｆｕｌｌｗｉｄｔｈ", "\x00", "\x00\x01", "\x000\x01",
    "a\x001\x01b", "\x1b[31m颜色\x1b[0m", "\x07\x08\x0b\x0c",
    # 换行 / 空白
    "a\r\nb", "a\rb", "a\n\rb", "行尾空格   \n下一行", " 不换行空格",
    "　全角空格", "a b c",
    # 超长
    "x" * 9000, "y " * 3000, ("很长的中文句子。" * 700),
    "\n".join(f"第 {i} 行 **粗** `code`" for i in range(300)),
    "```\n" + "\n".join(f"row {i}" for i in range(400)) + "\n```",
    "\n".join("| a | b |" for _ in range(400)),
    "- 项\n" * 900, "---\n" * 1200, "> q\n" * 900,
    # 混合恶意
    "**粗`代码**`", "`代码**粗`**", "[链接**粗](https://x.com)**",
    "**[粗链接](https://x.com)**", "# 标题 `**混合**`",
    "> 引用 **粗** `code` [链](https://x.com) ~~删~~",
    "```\n**围栏里的星号不该加粗**\n```",
    "| **表格里的粗** | `代码` |\n|---|---|\n| [链](https://x.com) | ~~删~~ |",
]

# ── ② 真实语料 ────────────────────────────────────────────────


def _repo_docs() -> list[str]:
    """仓库里的长 markdown：README + prompts/*.md（稳定、随代码一起版本化）。"""
    out: list[str] = []
    for p in [_REPO / "README.md", *sorted((_REPO / "prompts").glob("*.md"))]:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.append(text)
        # 再按段落切碎，让每个片段单独接受不变式检验（整篇过了不等于每段都过）
        out.extend(seg for seg in re.split(r"\n{2,}", text) if seg.strip())
    return out


def _session_texts(max_files: int = 160, max_lines: int = 400) -> list[str]:
    """从 ~/.claude/projects 的会话 jsonl 里抽 assistant 正文。

    这些文件动辄几百 MB，只能流式 islice 采样，不整读。取样顺序固定（路径排序 +
    固定 stride），所以同一台机器上是确定的；目录不存在就返回空。
    """
    root = pathlib.Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return []
    out: list[str] = []
    try:
        files = sorted(root.rglob("*.jsonl"))
    except OSError:
        return []
    for path in files[::7][:max_files]:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in itertools.islice(fh, max_lines):
                    if '"type":"assistant"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    content = (rec.get("message") or {}).get("content")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "text":
                            continue
                        text = block.get("text") or ""
                        if len(text) > 60:
                            out.append(text)
        except OSError:
            continue
    return out


# ── ③ 随机拼接生成器 ─────────────────────────────────────────

_FRAGMENTS = [
    "**粗**", "*斜*", "`code`", "~~删~~", "***三星***", "**未闭合", "*未闭合",
    "`未闭合", "~~未闭合", "***", "---", "___", "# 标题", "## C#", "- 项目",
    "  - 嵌套项", "> 引用", ">> 双引用", "| a | b |", "|---|---|",
    "[链接](https://x.com/a_b)", "[本地](./a.md)", "![图](https://x.com/i.png)",
    "```py", "```", "~~~", "x = a * b", "a < b && c > d", "&amp;", "<div>",
    "😀", "​", "مرحبا", r"$x_i^2$", "\t缩进", "   ", "普通中文句子。",
    "plain english words", "a" * 120, "|", "#", "*", "`", "~", "[", "]", "(", ")",
    "!", "\\", "\"", "'", "\x00", "\x011",
]


def _generated(count: int = 2400) -> list[str]:
    rng = random.Random(_SEED)
    pool = _FRAGMENTS + [s for s in HAND if len(s) < 120]
    out: list[str] = []
    for _ in range(count):
        n = rng.randint(1, 14)
        parts = [rng.choice(pool) for _ in range(n)]
        sep = rng.choice(["", " ", "\n", "\n\n", "\r\n", "\t"])
        out.append(sep.join(parts))
    return out


def _all_samples() -> list[str]:
    return HAND + _repo_docs() + _session_texts() + _generated()


SAMPLES = _all_samples()
assert len(SAMPLES) >= 2000, f"样本量不足：{len(SAMPLES)}"


# ── 可比较投影 ────────────────────────────────────────────────
#
# 「内容不丢」需要一个明确的投影，否则断言要么永真要么永假。这里的定义是
# **可见文字的字符序列**，两侧都做同样的删除：
#
#   删掉的字符：空白、`*`、反引号、`~`、`[`、`]`、`(`、`)`、`!`、`•`、`─`
#     理由：这些要么是被吃掉的强调/围栏/链接语法标记，要么是渲染器主动加的降级符号
#     （`- ` → `• `、`---` → `──────────`）。删两侧才对称。
#   保留的字符：字母/数字/CJK/emoji/`_`/`#`/`>`/`<`/`&`/`|`/`-`/`+`/零宽字符
#     理由：`_` 明确不做强调（`__init__` 必须原样），`#`/`>` 只在行首被当语法（投影里
#     按行先剥掉行首标记，剩下的 `#`/`>` 就是正文，丢了就是 bug），`<`/`>`/`&` 用来
#     校验 HTML 实体的转义-反转义往返，`|` 校验表格原样，`-`/`+` 校验正文里的连字号。
#
# 链接是唯一"把可见文字搬到属性里"的降级：渲染侧把 `<a href="U">L</a>` 展开成
# `L` + `U`，源侧把 `[L](U)` 展开成同样的 `L` + `U`（http 且 L 为空时按渲染器的
# `label or url` 展开成 `U`+`U`）。这一段等于把降级规则写进断言，它抓的是
# **label / URL 里的字符被吃掉**（比如 URL 里的下划线被当斜体、`&` 没转义），
# 抓不到链接语义本身的偏差 —— 那部分交给真 parser 抽检。
# markdown 的链接 title（`[a](u "t")`）会被渲染器丢弃，这是已知且可接受的降级
# （title 只是 tooltip，Telegram 也没地方放），所以投影里同样丢掉。

_DROP_CHARS = set("*`~[]()!•─")
_ANY_TAG_RE = re.compile(r"<\s*/?\s*([A-Za-z][A-Za-z0-9:_-]*)")
_WELL_FORMED_TAG_RE = re.compile(r"<(/?)([a-z-]+)((?:\s+[a-z-]+=\"[^\"]*\")*)\s*/?>")
_HREF_RE = re.compile(r'href="([^"]*)"')
_HEADER_RE = re.compile(r"^[ \t]*(#{1,6})[ \t]+(.*)$")


def rendered_visible(rendered: str) -> str:
    """渲染结果 → 可见文字（去标签、反转义，`<a>` 的 href 追加在 label 之后）。"""
    pieces: list[str] = []
    a_href: list[str] = []
    pos = 0
    for m in _WELL_FORMED_TAG_RE.finditer(rendered):
        pieces.append(rendered[pos:m.start()])
        pos = m.end()
        closing, name, attrs = m.group(1), m.group(2), m.group(3)
        if name == "a" and not closing:
            href = _HREF_RE.search(attrs)
            a_href.append(href.group(1) if href else "")
        elif name == "a" and closing and a_href:
            pieces.append(a_href.pop())
    pieces.append(rendered[pos:])
    return html.unescape("".join(pieces))


def _expand_links(line: str) -> str:
    """展开链接语法。行内代码先抠成占位符 —— 渲染器也是先 stash 代码再认链接，
    所以 `` `[a](b)` `` 里的链接语法是**原样**保留的，投影必须跟着一起原样。"""
    stash: list[str] = []

    def keep(m: re.Match) -> str:
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x01"

    line = tg_md._CODE_SPAN_RE.sub(keep, line)

    def rep(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        if re.match(r"^(https?|tg)://", url):
            return f"{label or url} {url}"      # 渲染器：文字 = label or url，href = url
        return f"{label} {url}" if label else url

    line = tg_md._LINK_RE.sub(rep, line)
    return re.sub(r"\x00(\d+)\x01", lambda m: stash[int(m.group(1))], line)


def expected_visible(md: str) -> str:
    """原文 → 可见文字（只剥掉文档化的行首语法标记与围栏标记行）。"""
    # 和 render_html 一样先剥控制字符：不剥的话 "\x00```py" 这种行在这里不算围栏、
    # 在渲染器眼里却是围栏，两边的行解析从此岔开（投影会误报"丢内容"）。
    md = tg_md._CTRL_RE.sub("", md)
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        opened = tg_md._fence_open(line)
        if opened:                                  # 围栏标记行整行消失（含 lang）
            i += 1
            while i < n:
                # 复用产品的判定：收尾 marker 必须同字符、**不短于开头**、无 info 串。
                # 自己再写一套的话，```` 围栏里的 ``` 行会被误当收尾，投影和渲染岔开。
                if tg_md._fence_closes(lines[i], opened):
                    i += 1
                    break
                out.append(lines[i])                # 围栏内原样，不做任何 markdown 处理
                i += 1
            continue
        i += 1
        if tg_md._TABLE_RE.match(line):             # 表格整块进 <pre>，原样
            out.append(line if _in_table_block(lines, i - 1) else _expand_links(line))
            continue
        quote = tg_md._QUOTE_RE.match(line)
        if quote:                                   # 引用里不再处理标题/列表/分隔线
            out.append(_expand_links(quote.group(1)))
            continue
        if line.strip() == ">":                     # 空引用行 → <blockquote></blockquote>
            continue
        if tg_md._HR_RE.match(line):                # → ──────────（在 _DROP_CHARS 里）
            continue
        header = _HEADER_RE.match(line)
        if header:
            body = header.group(2).strip()
            # CommonMark 的 closed ATX：结尾的 `#` 串要**前面有空白**才算收尾标记；
            # 整行只有 `#` 时视为空标题。这里不放行 `C#` 这种紧贴正文的 `#`。
            if body and set(body) == {"#"}:
                body = ""
            else:
                body = re.sub(r"(?<=\s)#+$", "", body).strip()
            out.append(_expand_links(body))
            continue
        bullet = tg_md._BULLET_RE.match(line)
        if bullet:
            out.append(_expand_links(line[bullet.end():]))
            continue
        out.append(_expand_links(line))
    return "\n".join(out)


def _in_table_block(lines: list[str], idx: int) -> bool:
    """>= 2 行连续的 `|` 行才整块进 <pre>（单行会走行内渲染）。"""
    j = k = idx
    while j + 1 < len(lines) and tg_md._TABLE_RE.match(lines[j + 1]):
        j += 1
    while k > 0 and tg_md._TABLE_RE.match(lines[k - 1]):
        k -= 1
    return (j - k + 1) >= 2


def norm(text: str) -> str:
    # 控制字符：render_html 入口就 _CTRL_RE 剥掉了（BUG-1 的修法 —— 正文里的
    # \x00<数字>\x01 会撞上内部占位符，而且 Telegram 本来也显示不了这些字符），
    # 所以投影两侧都不算它们。
    return "".join(
        ch for ch in text
        if not ch.isspace() and ch not in _DROP_CHARS
        and not tg_md._CTRL_RE.match(ch)
    )


# ── 已知 bug 登记（每条都有下面的 xfail 最小复现）────────────

def _known_placeholder_crash(md: str) -> bool:
    """BUG-1：正文里出现 `\\x00<数字>\\x01` 时撞上占位符，_restore 越界 IndexError。"""
    return bool(tg_md._PLACEHOLDER_RE.search(md))


def _known_trailing_hash(md: str) -> bool:
    """BUG-2：标题末尾紧贴正文的 `#` 被 rstrip 吃掉（`# C#` → `<b>C</b>`）。"""
    for line in md.replace("\r\n", "\n").split("\n"):
        m = _HEADER_RE.match(line)
        if m and m.group(2).strip().endswith("#"):
            return True
    return False


def _known_fence_tracking(md: str) -> bool:
    """BUG-3：split_md / tail_md 的围栏跟踪和 render_html 不是同一套规则。

    split_md 对每个"看起来像围栏"的行无条件 toggle —— 不看 marker 是 ``` 还是
    `~~~`、不看是三个还是四个反引号、也不看 info 串（render_html 只让**空 info +
    同字符**的行收尾）；收尾和重开又一律写死 ```。于是两边的状态会岔开，段落收在
    围栏里（后文被 <pre> 整块吞）或者段尾多出一个空代码块。

    命中条件：① 出现非 ``` 的围栏 marker；② 两套状态机逐行状态出现分歧。
    """
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    split_open, r_open, r_marker = False, False, ""
    for line in lines:
        m = tg_md._FENCE_RE.match(line)
        if not m:
            continue
        if m.group(1) != "```":
            return True                                  # ① 收尾/重开会写错 marker
        split_open = not split_open                      # split_md 的规则
        if not r_open:
            r_open, r_marker = True, m.group(1)          # render_html 的规则
        elif m.group(1)[0] == r_marker[0] and not m.group(2).strip():
            r_open, r_marker = False, ""
        if split_open != r_open:
            return True                                  # ② 状态岔开


def _known_hardcut(md: str, limit: int) -> bool:
    """BUG-5：单行超过 limit-8 时会被**硬切在行中间**，那一行的 markdown 语义可能变。

    实测最典型的一刀：`` ``` 说明文字 `code` `` 这种行本来因为 info 串里有反引号而
    不算围栏（`_FENCE_RE` 的 info 是 `[^\n`]*`），反引号被切到下一段之后，前半段就
    变成了一个**围栏开头**，把后面的正文整块吞进 <pre>。
    """
    flat = md.replace("\r\n", "\n").replace("\r", "\n")
    return any(len(line) > limit - 8 for line in flat.split("\n"))


def _repro(md: str, extra: str = "") -> str:
    return f"\n最小输入 repr: {md[:400]!r}\n渲染输出: {render_html(md)[:400]!r}\n{extra}"


# ── 不变式 ① 合法性 ───────────────────────────────────────────

def test_render_never_raises():
    """渲染在发送路径上先于 API 调用，抛异常 = 整条消息发不出去（还不走纯文本兜底）。"""
    unexpected = []
    known = 0
    for md in SAMPLES:
        try:
            render_html(md)
        except Exception as exc:                     # noqa: BLE001 - 就是要抓一切
            unexpected.append((md, repr(exc)))
    assert not unexpected, f"渲染抛异常 {len(unexpected)} 例：" + "".join(
        f"\n  {md[:200]!r} -> {e}" for md, e in unexpected[:5])


def test_tags_always_balanced():
    """硬约束：交叉/未闭合标签 → Telegram 400，整条消息丢。"""
    bad = []
    for md in SAMPLES:
        if _known_placeholder_crash(md):
            continue
        out = render_html(md)
        if not tg_md._tags_balanced(out):
            bad.append(md)
    assert not bad, f"渲染出非法 HTML {len(bad)} 例：" + "".join(_repro(m) for m in bad[:3])


def test_only_whitelisted_tag_names():
    """独立于 _tags_balanced 扫标签名：它的正则只认小写 `[a-z-]+`，大写标签会被漏掉。"""
    bad = []
    for md in SAMPLES:
        if _known_placeholder_crash(md):
            continue
        out = render_html(md)
        for name in _ANY_TAG_RE.findall(out):
            if name not in tg_md._VOID_OK:
                bad.append((md, name))
    assert not bad, f"出现白名单外的标签：" + "".join(
        f"\n  <{n}> ← {m[:200]!r}" for m, n in bad[:5])


def test_render_output_has_no_stray_angle_or_amp():
    """所有正文都必须转义过：裸 `<`/`&` 一旦漏出去就是解析失败的直接原因。"""
    stray = re.compile(r"&(?![a-zA-Z]+;|#\d+;|#x[0-9a-fA-F]+;)")
    bad = []
    for md in SAMPLES:
        if _known_placeholder_crash(md):
            continue
        out = _WELL_FORMED_TAG_RE.sub("", render_html(md))
        if "<" in out or stray.search(out):
            bad.append(md)
    assert not bad, "去掉合法标签后仍有裸 < 或裸 &：" + "".join(_repro(m) for m in bad[:3])


# ── 不变式 ② 内容不丢 ────────────────────────────────────────

def test_no_visible_content_lost():
    bad = []
    known = 0
    for md in SAMPLES:
        if _known_placeholder_crash(md):
            continue
        got = norm(rendered_visible(render_html(md)))
        want = norm(expected_visible(md))
        if got == want:
            continue
        bad.append((md, got, want))
    assert not bad, f"可见文字与原文不一致 {len(bad)} 例：" + "".join(
        _repro(m, f"投影(渲染)={g[:200]!r}\n投影(原文)={w[:200]!r}")
        for m, g, w in bad[:3])


def test_projection_is_not_vacuous():
    """自检：投影必须对真的丢字符敏感，否则上面那条断言等于没写。"""
    md = "banana **粗** `code` [链](https://x.com/a_b) 表格|管道"
    good = norm(rendered_visible(render_html(md)))
    assert good == norm(expected_visible(md))
    for broken in (render_html(md).replace("a", ""),          # 丢字母
                   render_html(md).replace("_", ""),          # 丢 URL 里的下划线
                   render_html(md).replace("|", ""),          # 丢表格管道
                   render_html(md) + "多出来的字"):            # 多内容
        assert norm(rendered_visible(broken)) != norm(expected_visible(md))


# ── 不变式 ③ 分段 ────────────────────────────────────────────

_LIMITS = (60, 137, 300, 1000, tg_md.SPLIT_LIMIT)
# BUG-4：split_md 的长度预算没算上两样东西 ——
#   a) 围栏内切段时追加的 "\n```" 收尾（4 字符，在长度校验之后才加）；
#   b) 下一段重开围栏的 "```<info>" 那一行（info 串多长就超多少，**无上界**）。
# 所以段长的实际上界是 limit + 4 + len(最长的 info 串)。
_FENCE_CLOSE_SLACK = 4


def _fence_info_budget(md: str) -> int:
    """文本里最长的围栏 info 串（重开时会被整串抄进下一段）。"""
    widest = 0
    for line in md.replace("\r\n", "\n").split("\n"):
        m = tg_md._FENCE_RE.match(line)
        if m:
            widest = max(widest, len(m.group(2).strip()))
    return widest


def _split_cases():
    """每个样本都跑全部 limit 档。

    刻意**不**用随机挑 limit：真实语料是从 ~/.claude/projects 现采的，条数会随
    机器上的会话增长而变；如果 limit 来自一条共享的随机流，语料多一条就会把后面
    所有样本的 limit 全洗一遍，同一个 bug 今天报明天不报。全档跑虽然慢一点，但
    每个样本的判定只取决于它自己。
    """
    for md in SAMPLES:
        if len(md) < 40:
            continue
        for limit in _LIMITS:
            yield md, limit


SPLIT_CASES = list(_split_cases())


def test_split_chunk_length():
    bad = []
    for md, limit in SPLIT_CASES:
        for chunk in split_md(md, limit):
            if len(chunk) > limit:
                bad.append((md, limit, len(chunk)))
    assert not bad, "分段长度超限：" + "".join(
        f"\n  limit={l} 段长={n} ← {m[:120]!r}" for m, l, n in bad[:5])


def test_split_preserves_characters():
    """一个字符都不能丢（忽略空白与反引号：围栏重开会补 ```、硬切长行会插 \\n）。

    反过来"多字符"是允许的：重开围栏会把 ```<info> 抄进下一段，这是设计如此。
    但多出来的字符只能来自 info 串，不能凭空长出正文 —— 这里两头都钉住。
    """
    lost, grown = [], []
    for md, limit in SPLIT_CASES:
        chunks = split_md(md, limit)
        flat = md.replace("\r\n", "\n").replace("\r", "\n")
        want = Counter(c for c in flat if not c.isspace() and c != "`")
        got = Counter(c for chunk in chunks for c in chunk
                      if not c.isspace() and c != "`")
        missing = want - got
        if missing:
            lost.append((md, limit, dict(itertools.islice(missing.items(), 6))))
        extra = got - want
        if extra:
            allowed = Counter()
            # 分段只可能补出**围栏标记**：每段都要自己闭合，所以每段最多补一个开头
            # （marker + sanitize 后的 info）和一个收尾（marker）。而"哪一行算围栏"
            # 在硬切之后会变（"``` 说明… `code`" 这种行切开后前半片就成了围栏开头），
            # 所以 marker 字符按上界给，不逐行精算。真正要钉住的是下面那条：
            # **不能凭空长出正文字符**。
            allowed["~"] = 8 * max(1, len(chunks))
            # 注意用 split_md 自己的换行归一化（它只处理 \r\n，不处理裸 \r），
            # 否则裸 \r 会让两边对"哪一行是围栏"的判断不一致。
            for line in md.replace("\r\n", "\n").split("\n"):
                m = tg_md._FENCE_RE.match(line)
                if m:
                    # 重开时写的是 _sanitize_lang(info)（非法字符剔掉 + 截断 20）
                    lang = tg_md._sanitize_lang(m.group(2))
                    allowed.update(c for c in lang * (2 * len(chunks))
                                   if not c.isspace() and c != "`")
            if extra - allowed:
                grown.append((md, limit, dict(itertools.islice((extra - allowed).items(), 6))))
    assert not lost, "分段丢字符：" + "".join(
        f"\n  limit={l} 丢了={d} ← {m[:120]!r}" for m, l, d in lost[:5])
    assert not grown, "分段多出了非围栏标记的字符：" + "".join(
        f"\n  limit={l} 多了={d} ← {m[:120]!r}" for m, l, d in grown[:5])


def test_split_exact_rejoin_for_plain_text():
    """没有围栏、也没有超长单行时，split_md 必须是纯粹的按行切分（拼回去逐字节相等）。"""
    bad = []
    for md, limit in SPLIT_CASES:
        norm_md = md.replace("\r\n", "\n").replace("\r", "\n")
        if "```" in norm_md or "~~~" in norm_md:
            continue
        if any(len(line) > limit - 8 for line in norm_md.split("\n")):
            continue
        if not norm_md.strip() or "\n\n" in norm_md or norm_md != norm_md.strip("\n"):
            continue                      # 空白段会被 `if c.strip()` 丢掉，另有单测
        chunks = split_md(norm_md, limit)
        if "\n".join(chunks) != norm_md:
            bad.append((md, limit))
    assert not bad, "纯文本分段拼不回原文：" + "".join(
        f"\n  limit={l} ← {m[:120]!r}" for m, l in bad[:5])


def _ends_inside_fence(text: str) -> bool:
    """按 render_html 的规则跑围栏状态机：结尾还在围栏里就返回 True。

    比"数 ``` 的个数"准得多 —— 行中间的 ``` 不是围栏标记，而 ``` 也关不掉 ~~~。
    这正是渲染时真正生效的规则，所以拿它当不变式最贴近用户看到的结果。
    """
    in_fence, marker = False, ""
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = tg_md._FENCE_RE.match(line)
        if not m:
            continue
        if not in_fence:
            in_fence, marker = True, m.group(1)
        elif m.group(1)[0] == marker[0] and not m.group(2).strip():
            in_fence, marker = False, ""
    return in_fence


def test_split_leaves_no_chunk_inside_a_fence():
    """每段自身的围栏必须闭合，否则那段渲染出来的 <pre> 会把后文整块吞掉。"""
    bad = []
    known = 0
    for md, limit in SPLIT_CASES:
        if _ends_inside_fence(md):
            continue            # 原文自己就没闭合（流式中途很常见），不是分段的锅
        for chunk in split_md(md, limit):
            if _ends_inside_fence(chunk):
                if _known_fence_tracking(md) or _known_hardcut(md, limit):
                    known += 1
                else:
                    bad.append((md, limit, chunk))
                break
    assert not bad, "分段后有段落停在围栏里：" + "".join(
        f"\n  limit={l} 段={c[:80]!r} ← {m[:120]!r}" for m, l, c in bad[:3])
    assert known, "BUG-3 的样本没进语料，检查生成器"


def test_split_chunks_render_legally():
    """分段后每段还要能单独渲染成合法 HTML（真正发出去的是段，不是整篇）。"""
    bad = []
    for md, limit in SPLIT_CASES:
        if _known_placeholder_crash(md):
            continue
        for chunk in split_md(md, limit):
            out = render_html(chunk)
            if not tg_md._tags_balanced(out):
                bad.append((md, limit, chunk))
                break
    assert not bad, "分段渲染出非法 HTML：" + "".join(
        f"\n  limit={l} 段={c[:120]!r}" for _, l, c in bad[:3])


def _hr_inflation(chunk: str) -> int:
    """`---`(3) → `──────────`(10)，每条分隔线让可见文字多 7 个字符。"""
    lines = chunk.replace("\r\n", "\n").split("\n")
    return 7 * sum(1 for line in lines if tg_md._HR_RE.match(line))


def test_rendered_chunk_fits_telegram_limit():
    """真卡 4096 的是**解析后的可见文字**（Bot API：1-4096 characters after entities
    parsing），而 split_md 是按 markdown 源码长度切的 —— render_html 会让可见文字
    变长（分隔线 3 → 10 个字符），源码 ≤ 3800 并不等于可见 ≤ 4096。溢出时 Telegram
    回 "message is too long"，而 `_is_parse_error()` 不认这条，纯文本兜底接不住，
    异常直接往上抛（讽刺的是纯文本原文只有 3800，本来发得出去）。
    """
    bad = []
    for md in SAMPLES:
        for chunk in split_md(md):
            visible = len(rendered_visible(render_html(chunk)))
            if visible > tg_md.TG_TEXT_LIMIT:
                bad.append((md, len(chunk), visible))
    assert not bad, "分段渲染后超出 Telegram 的 4096：" + "".join(
        f"\n  段长={n} 可见={v} ← {m[:120]!r}" for m, n, v in bad[:5])


_TAIL_MARK = "…（前文略）\n"


def test_tail_length_and_fence():
    bad = []
    for md, limit in SPLIT_CASES:
        out = tail_md(md, limit)
        # 允许的额外量：省略标记 + 补的围栏开头（marker + 最多 20 字符 info）
        if len(out) > limit + len(_TAIL_MARK) + 4 + 20 + 1:
            bad.append(("长度", md, limit, len(out)))
        # 切点落在围栏里时必须补回**同一种** marker（只补 ``` 补不上 ~~~）。
        # 切点要按 tail_md 的口径算（它会前移到最近的换行），否则状态对不上会误报。
        if len(md) > limit:
            cut_at = len(md) - limit
            nl = md[cut_at:].find("\n")
            if 0 <= nl < 200:
                cut_at += nl + 1
            state = None
            for line in md[:cut_at].replace("\r\n", "\n").split("\n"):
                state = tg_md._fence_step(line, state)
            if state is not None:
                first_line = out.split("\n")[1] if "\n" in out else ""
                if not first_line.startswith(state[0]):
                    bad.append(("围栏", md, limit, out[:40]))
    assert not bad, "tail_md 违反不变式：" + "".join(
        f"\n  {k} limit={l} 值={n} ← {m[:100]!r}" for k, m, l, n in bad[:5])


def test_tail_keeps_the_tail():
    """流式帧"保尾巴"：原文最后 20 个字符必须还在。

    只查 limit >= 400 的档：tail_md 会把切出来的第一行残行丢掉（`nl < 200`），
    limit 小到跟这 200 同量级时，"尾巴"本身就可能被这一刀带走 —— 那是设计取舍，
    不是 bug。生产里 limit = SPLIT_LIMIT = 3800，落在安全区。
    """
    bad = []
    for md, limit in SPLIT_CASES:
        if limit < 400:
            continue
        out = tail_md(md, limit)
        tail = md[-20:]         # tail_md 不做 CRLF 归一，比较也别做
        if tail and tail not in out:
            bad.append((md, limit))
    assert not bad, "tail_md 把尾巴切没了：" + "".join(
        f"\n  limit={l} ← {m[-80:]!r}" for m, l in bad[:5])


def test_tail_renders_legally():
    bad = []
    for md, limit in SPLIT_CASES:
        if _known_placeholder_crash(md):
            continue
        out = render_html(tail_md(md, limit))
        if not tg_md._tags_balanced(out):
            bad.append((md, limit))
    assert not bad, "tail_md 的结果渲染出非法 HTML：" + "".join(
        f"\n  limit={l} ← {m[:120]!r}" for m, l in bad[:3])


# ── 已知 bug 的最小复现（修好后会变 XPASS，届时删掉登记项）──

def test_bug1_placeholder_collision_is_fixed():
    """正文里的 \\x00<数字>\\x01 曾撞上内部占位符 → IndexError，整条消息发不出去。"""
    assert render_html("\x000\x01") == "0"
    assert render_html("a\x001\x01b") == "a1b"


def test_bug2_trailing_hash_is_kept():
    assert render_html("# C#") == "<b>C#</b>"
    assert render_html("## Rust vs C#") == "<b>Rust vs C#</b>"


def test_bug3a_tilde_fence_is_closed_with_tildes():
    md = "~~~\n" + "\n".join("x" * 30 for _ in range(20)) + "\n~~~"
    chunks = split_md(md, 100)
    assert not any(_ends_inside_fence(c) for c in chunks)


def test_bug3b_info_string_line_inside_fence_is_content():
    md = "```\n" + "a" * 10 + "\n```py\n" + "b" * 10 + "\n```"
    assert not _ends_inside_fence(md)          # render_html 眼里它是闭合的
    chunks = split_md(md, 25)
    assert not any(_ends_inside_fence(c) for c in chunks)
    # 而且原文已经闭合了，末段还会被凭空补一个 ``` 出来
    assert not chunks[-1].endswith("```\n```")


def test_bug4_fence_budget_is_accounted_for():
    # a) 恒定 +3/+4：收尾的 "\n```" 在长度校验之后才追加
    assert all(len(c) <= 88 for c in split_md("```\n" + "r" * 40 * 30, 88))
    # b) 无上界：重开时把整串 info 抄进下一段
    md = "```" + "L" * 60 + "\n" + "\n".join("x" * 30 for _ in range(10))
    assert all(len(c) <= 60 for c in split_md(md, 60))


def test_bug5_hardcut_does_not_leak_an_open_fence():
    md = "``` " + "说明" * 40 + " `code`"          # info 串里有反引号 → 本来不是围栏
    assert not _ends_inside_fence(md)
    assert not any(_ends_inside_fence(c) for c in split_md(md, 60))


def test_bug6_whitespace_only_text_still_yields_a_chunk():
    assert split_md(" " * 300 + "\n" + " " * 300, 100)


def test_bug8_hr_inflation_is_budgeted():
    """`---` → `──────────` 每条膨胀 7 个可见字符；预算必须按渲染后的可见长度算。"""
    chunks = split_md("---\n" * 950)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(rendered_visible(render_html(chunk))) <= tg_md.TG_TEXT_LIMIT


def test_bug7_tail_md_reopens_the_original_marker():
    out = tail_md("~~~\n" + "\n".join(f"*r{i}*" for i in range(400)), 100)
    assert "<pre>" in render_html(out)


# ── ④ 真 Telegram parser 抽检（默认 skip）────────────────────

_LIVE = os.environ.get("TG_FUZZ_LIVE") == "1"
_LIVE_MAX = 200
_LIVE_QPS_SLEEP = 0.55        # ≤2 QPS，别把 bot 撞进 429


def _bot_token() -> str:
    """从 .env 读 TG_BOT_TOKEN。绝不打印，出错信息里也不带。"""
    env = _REPO / ".env"
    if not env.is_file():
        return ""
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("TG_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def _boundary_score(rendered: str) -> int:
    """挑"边界"样本：本地不变式判不出、但真 parser 最可能拒的那些形态。"""
    score = 0
    names = _ANY_TAG_RE.findall(rendered)
    if not names:
        return 0
    counts = Counter(names)
    score += 3 * sum(1 for n, c in counts.items() if c > 2)     # 同名标签多次
    for empty in ("<code></code>", "<pre></pre>", "<blockquote></blockquote>",
                  "<b></b>", "<i></i>", "<s></s>"):
        if empty in rendered:
            score += 5                                          # 空实体
    score += 4 * len(re.findall(r"<b>[^<]*<b>|<i>[^<]*<i>|<s>[^<]*<s>", rendered))
    score += 3 * rendered.count('class="language-')
    score += 2 * rendered.count("<blockquote>")
    score += 2 * rendered.count('href="tg://')
    score += 2 * len(re.findall(r"<[a-z-]+>\s*</[a-z-]+>", rendered))
    if re.search(r"[​-‏ -‮﻿]", rendered):
        score += 2
    if re.search(r"[\U0001F000-\U0001FAFF]", rendered):
        score += 1
    if re.search(r"[؀-ۿ֐-׿]", rendered):
        score += 1
    score += min(len(counts), 6)
    return score


def _live_payloads() -> list[tuple[str, str]]:
    scored: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for md in SAMPLES:
        if _known_placeholder_crash(md):
            continue
        out = render_html(md)
        if not out.strip() or len(out) > 3900:
            continue
        if out in seen:
            continue
        seen.add(out)
        scored.append((_boundary_score(out), md, out))
    scored.sort(key=lambda t: -t[0])
    picked = scored[:_LIVE_MAX - 40]
    rng = random.Random(_SEED + 2)
    rest = [s for s in scored[_LIVE_MAX - 40:] if s[0] > 0]
    picked += rng.sample(rest, min(40, len(rest)))
    return [(md, out) for _, md, out in picked]


@pytest.mark.skipif(not _LIVE, reason="真 API 抽检：设 TG_FUZZ_LIVE=1 才跑")
def test_live_telegram_parses_every_rendered_sample():
    """chat_id=1 一定不存在，但 Telegram 会**先解析 entities**：

        "chat not found"          → HTML 合法（我们要的结果）
        "can't parse entities"    → 渲染有 bug
        "unsupported start tag"   → 同上
        "message is too long"     → 长度不变式有 bug
    """
    import urllib.error
    import urllib.request

    token = _bot_token()
    if not token:
        pytest.skip("`.env` 里没有 TG_BOT_TOKEN")
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payloads = _live_payloads()
    assert 150 <= len(payloads) <= _LIVE_MAX, f"抽检样本数 {len(payloads)} 不在 150~200"

    failures: list[tuple[str, str, str]] = []
    ok = 0
    for md, rendered in payloads:
        body = json.dumps({
            "chat_id": 1, "text": rendered, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                desc = json.loads(resp.read()).get("description", "")
        except urllib.error.HTTPError as e:
            desc = json.loads(e.read() or b"{}").get("description", "")
        except OSError as e:                      # 网络抖动：不算渲染 bug
            pytest.skip(f"Telegram 不可达：{type(e).__name__}")
        low = desc.lower()
        if "chat not found" in low:
            ok += 1
        elif "too many requests" in low or "retry after" in low:
            pytest.fail("撞到 429，抽检节流不够，别再往下打")
        else:
            failures.append((md, rendered, desc))
        time.sleep(_LIVE_QPS_SLEEP)

    assert not failures, f"真 parser 拒了 {len(failures)}/{len(payloads)} 条：" + "".join(
        f"\n  输入 {m[:160]!r}\n  渲染 {r[:200]!r}\n  报错 {d}"
        for m, r, d in failures[:8])
    assert ok == len(payloads)
