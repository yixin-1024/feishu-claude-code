"""把模型输出的 markdown 渲染成 Telegram 能显示的 HTML，并按 4096 上限切段。

为什么不用 Telegram 自己的 MarkdownV2：它要求 `_ * [ ] ( ) ~ > # + - = | { } . !`
全部转义，模型输出里这些字符满天飞，漏一个整条消息就 400（`can't parse entities`）
直接发不出去。HTML 模式只需要转义 `& < >`，其余交给我们自己的转换，可控得多。

Telegram HTML 只认这几个标签（其余一律非法）：
    <b> <i> <u> <s> <tg-spoiler> <a href> <code> <pre> <blockquote>
所以 markdown 的标题 / 表格 / 列表都得降级：标题 → 加粗、表格 → <pre> 等宽块、
列表 → `• `。降级而不是丢弃，信息不会消失。

调用方（telegram_client）拿到 HTML 后仍要准备一条**纯文本兜底**：真出现解析失败
就退回 parse_mode=None 重发，宁可丑也不能发不出去。
"""

from __future__ import annotations

import html
import re
from typing import Optional

# Telegram 单条消息 4096 字符上限。留出余量给 footer / 分页标记。
TG_TEXT_LIMIT = 4096
SPLIT_LIMIT = 3800

_PLACEHOLDER = "\x00%d\x01"
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x01")
# 正文里如果真出现 \x00<数字>\x01（模型贴二进制/转义串时会），就会撞上内部占位符
# 让还原越界。控制字符 Telegram 本来也显示不了，入口直接剥掉最省事。
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_TAG_STRIP_RE = re.compile(r"<[^>]*>")

_FENCE_RE = re.compile(r"^[ \t]*(```+|~~~+)[ \t]*([^\n`]*)$")
_HEADER_RE = re.compile(r"^[ \t]*(#{1,6})[ \t]+(.*)$")
_HR_RE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+")
_QUOTE_RE = re.compile(r"^[ \t]*>[ \t]?(.*)$")
_TABLE_RE = re.compile(r"^[ \t]*\|.*$")

_CODE_SPAN_RE = re.compile(r"(`+)([^\n]*?)\1")
_LINK_RE = re.compile(r"!?\[([^\]\n]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# ***x*** 必须在 ** 之前处理：否则 `**` 会先吃掉前两个星号、把第三个留在里面，
# 生成 <b>*e</b>* 这种半成品，接着斜体规则又跨过 </b> 配对，出来 <b><i>e</b></i>
# 这种交叉嵌套 —— Telegram 直接 400（实测 "Unmatched end tag"）。
_BOLD_ITALIC_RE = re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", re.S)
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.S)
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
# 斜体的前后边界只排除 **ASCII** 单词字符（`a*b*c` / 乘法 / 通配符不算强调），
# 中文标点和汉字要放行 —— `这是*重点*内容` 在 CommonMark 的 flanking 规则下不算
# 强调，但模型和用户都当它是强调，按 \w 排除会让中文里的斜体永远失效。
_ITALIC_STAR_RE = re.compile(
    r"(?<![A-Za-z0-9_*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![A-Za-z0-9_*])")
# 故意**不支持** __粗__ / _斜_ 两种下划线写法：这是个写代码的 bot，正文里
# `__init__` / `_private` 这类标识符远比下划线强调常见，认了它反而天天误加粗。
# 模型输出的强调 99% 是星号形式，够用。

_TAG_RE = re.compile(r"<(/?)([a-z-]+)[^>]*>")
_VOID_OK = frozenset({"b", "i", "u", "s", "code", "pre", "a", "tg-spoiler", "blockquote"})


def _tags_balanced(html: str) -> bool:
    """标签是否严格配对且不交叉。不合法就整段退回纯文本，别赌 Telegram 的宽容度。"""
    stack: list[str] = []
    for closing, name in _TAG_RE.findall(html):
        if name not in _VOID_OK:
            return False
        if not closing:
            stack.append(name)
        elif not stack or stack.pop() != name:
            return False
    return not stack


def visible_len(html_text: str) -> int:
    """渲染结果的**可见文字**长度（标签不算、实体按 1 个字符算）。

    Telegram 的 4096 上限卡的是 "text after entities parsing"，所以预算必须按这个量
    算：`---` 渲染成 `──────────`（+7），一段 3800 源码里 40 多条分隔线就能顶过 4096，
    而 Telegram 那时回的是 `message is too long`（不是解析错误，纯文本兜底接不住）。
    """
    return len(html.unescape(_TAG_STRIP_RE.sub("", html_text)))


def _sanitize_lang(info: str) -> str:
    """围栏 info 串 → `class="language-xx"` 里那个 xx。收尾/重开时也用同一套规则。"""
    return re.sub(r"[^A-Za-z0-9_+.#-]", "", (info or "").strip())[:20]


def _fence_open(line: str) -> Optional[tuple[str, str]]:
    m = _FENCE_RE.match(line)
    return (m.group(1), m.group(2)) if m else None


def _fence_closes(line: str, fence: Optional[tuple[str, str]]) -> bool:
    """CommonMark：收尾行的 marker 必须同字符、不短于开头、且没有 info 串。"""
    if fence is None:
        return False
    m = _FENCE_RE.match(line)
    if not m:
        return False
    return (
        m.group(1)[0] == fence[0][0]
        and len(m.group(1)) >= len(fence[0])
        and not m.group(2).strip()
    )


def _fence_step(line: str, fence: Optional[tuple[str, str]]) -> Optional[tuple[str, str]]:
    """逐行推进围栏状态。**render_html / split_md / tail_md 必须共用这一套**，
    否则三者对"现在在不在代码块里"的判断会岔开：段落收在围栏里（后文整块被 <pre>
    吞）、或者段尾凭空多出一个空代码块。"""
    if fence is None:
        return _fence_open(line)
    return None if _fence_closes(line, fence) else fence


def _esc(text: str) -> str:
    """只转义 HTML 的三个元字符（quote=False：属性值另行处理）。"""
    return html.escape(text, quote=False)


def _render_inline(text: str) -> str:
    """行内 markdown → HTML。

    顺序很关键：先把行内代码和链接抠成占位符，再转义、再套强调标签，最后把占位符
    换回去。否则代码里的 `*` 会被当强调、链接 URL 里的 `_` 会被当斜体。
    """
    stash: list[str] = []

    def _keep(rendered: str) -> str:
        stash.append(rendered)
        return _PLACEHOLDER % (len(stash) - 1)

    def _code(m: re.Match) -> str:
        return _keep(f"<code>{_esc(m.group(2))}</code>")

    text = _CODE_SPAN_RE.sub(_code, text)

    def _link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        if not re.match(r"^(https?|tg)://", url):
            # 相对路径 / mailto / 本地文件：Telegram 认不了，退成 `label (url)` 文本
            body = f"{label} ({url})" if label else url
            return _keep(_esc(body))
        safe_url = html.escape(url, quote=True)
        return _keep(f'<a href="{safe_url}">{_esc(label or url)}</a>')

    text = _LINK_RE.sub(_link, text)
    escaped = _esc(text)
    marked = _BOLD_ITALIC_RE.sub(r"<b><i>\1</i></b>", escaped)
    marked = _BOLD_RE.sub(r"<b>\1</b>", marked)
    marked = _STRIKE_RE.sub(r"<s>\1</s>", marked)
    marked = _ITALIC_STAR_RE.sub(r"<i>\1</i>", marked)
    # 兜底：强调规则再怎么小心也可能被畸形输入拼出交叉标签，那会让整条消息发不出去。
    # 宁可这一行不加粗，也不能让用户什么都收不到。
    if not _tags_balanced(marked):
        marked = escaped

    def _restore(m: re.Match) -> str:
        # 越界只可能来自"正文里本来就有 \x00N\x01"，原样还回去，别抛
        try:
            return stash[int(m.group(1))]
        except (IndexError, ValueError):
            return m.group(0)

    # 占位符会嵌套：链接文字里含行内代码时，先抠走的 code 占位符会被一起收进
    # <a> 的 stash 里。re.sub 不二次扫描替换结果，只还原一轮会把内层占位符（控制
    # 字符）原样发出去、文字丢失，所以要展开到不动点。stash 长度是嵌套深度上界；
    # 越界占位符还回自身，靠"这一轮没变化就停"兜住，不会死循环。
    for _ in range(len(stash) + 1):
        expanded = _PLACEHOLDER_RE.sub(_restore, marked)
        if expanded == marked:
            break
        marked = expanded
    return marked


def _code_block(lang: str, body: str) -> str:
    body = _esc(body.rstrip("\n"))
    lang = _sanitize_lang(lang)
    if lang:
        return f'<pre><code class="language-{lang}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def render_html(md: str) -> str:
    """markdown → Telegram HTML（未闭合的围栏按"到结尾都是代码"处理）。"""
    if not md:
        return ""
    md = _CTRL_RE.sub("", md)
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        opened = _fence_open(line)
        if opened:
            body: list[str] = []
            i += 1
            while i < n:
                if _fence_closes(lines[i], opened):
                    i += 1
                    break
                body.append(lines[i])
                i += 1
            out.append(_code_block(opened[1], "\n".join(body)))
            continue

        if _TABLE_RE.match(line):
            table: list[str] = []
            while i < n and _TABLE_RE.match(lines[i]):
                table.append(lines[i].strip())
                i += 1
            if len(table) >= 2:
                out.append(f"<pre>{_esc(chr(10).join(table))}</pre>")
            else:
                out.append(_render_inline(table[0]))
            continue

        quote = _QUOTE_RE.match(line)
        if quote:
            quoted: list[str] = []
            while i < n:
                q = _QUOTE_RE.match(lines[i])
                if not q:
                    break
                quoted.append(_render_inline(q.group(1)))
                i += 1
            out.append("<blockquote>" + "\n".join(quoted) + "</blockquote>")
            continue

        i += 1

        if _HR_RE.match(line):
            out.append("──────────")
            continue

        header = _HEADER_RE.match(line)
        if header:
            # closed ATX 的收尾 `#` 串**前面必须有空白**才算标记（CommonMark）。
            # 无条件 rstrip("#") 会把 `# C#` 吃成 `C`——这是个写代码的 bot，
            # `C#` / `折扣 50%#` 这种标题不算稀有。
            # `#` 串在 group(2) 的开头也算"前面有空白"——那个空白被 `#{1,6}[ \t]+`
            # 吃掉了。所以 `# ##` 是**空标题**（CommonMark），而 `# C#` 保留那个 #。
            body = re.sub(r"(?:(?<=\s)|^)#+$", "", header.group(2).strip()).strip()
            out.append(f"<b>{_render_inline(body)}</b>" if body else "")
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            indent = bullet.group(1).replace("\t", "  ")
            rest = line[bullet.end():]
            out.append(f"{indent}• {_render_inline(rest)}")
            continue

        out.append(_render_inline(line))

    return "\n".join(out)


# ── 分段 ────────────────────────────────────────────────────────

def _split_source(text: str, limit: int) -> list[str]:
    """按 markdown 源码切段：尽量落在行边界，且**每段自身的围栏是闭合的**。

    三条容易翻车的账，这里都算进预算：
      · 收尾要补 `\n<marker>`（比如 `\n~~~`）——切在围栏里时必须补，长度得预留；
      · 重开要写 `<marker><info>`——info 串按 `_code_block` 的规则 sanitize + 截断，
        不然一个 300 字符的 info 串能把段落顶爆；
      · 单行超长只能硬切，此时**不插换行**（各片各自成段），否则前半片可能凭空
        变成一个围栏开头，把后文整块吞进 <pre>。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    fence: Optional[tuple[str, str]] = None

    def opener() -> str:
        marker, info = fence
        lang = _sanitize_lang(info)
        return f"{marker}{lang}"

    def closer() -> str:
        """预算里给"收尾围栏"留的位置（下界：硬切片段自己可能开出一个围栏）。"""
        return f"\n{fence[0]}" if fence is not None else "\n```"

    def seal(body: str) -> str:
        """按段落**自身**的内容判断要不要补收尾围栏。

        不能只看外层循环的状态：硬切出来的片段自己可能就是一个围栏开头
        （"``` 说明…" 这种行本来因为行尾有反引号才不算围栏，切开之后就算了），
        那种段落也必须自己闭合，否则整段被渲染成代码块。
        """
        state: Optional[tuple[str, str]] = None
        for line in body.split("\n"):
            state = _fence_step(line, state)
        return body if state is None else f"{body}\n{state[0]}"

    def seed() -> None:
        """新段开头：如果正处在围栏里，先把围栏重新打开。"""
        nonlocal cur, cur_len
        if fence is not None and not cur:
            text_open = opener()
            cur.append(text_open)
            cur_len = len(text_open) + 1

    def flush() -> None:
        nonlocal cur, cur_len
        if not cur:
            return
        chunks.append(seal("\n".join(cur)))
        cur, cur_len = [], 0

    for raw in lines:
        budget = limit - len(closer())
        room = budget - (len(opener()) + 1 if fence is not None else 0)
        if len(raw) + 1 > max(16, room):
            # 超长单行（压平的 JSON / base64）：先收掉前面，再把它硬切成独立段
            flush()
            step = max(16, room - 1)
            for k in range(0, len(raw), step):
                piece = raw[k:k + step]
                seed()
                cur.append(piece)
                cur_len += len(piece) + 1
                flush()
        else:
            if cur and cur_len + len(raw) + 1 > budget:
                flush()
            seed()
            cur.append(raw)
            cur_len += len(raw) + 1
        fence = _fence_step(raw, fence)

    flush()
    kept = [c for c in chunks if c.strip()]
    # 纯空白正文也得留一段：调用方是 `split_md(text) or [text]`，返回 [] 会让
    # **未切分的原文**被原样发出去（超长时 Telegram 直接拒收）。
    return kept or chunks[:1]


def split_md(text: str, limit: int = SPLIT_LIMIT) -> list[str]:
    """切段，并保证每段**渲染后的可见长度**不超过 Telegram 的 4096。

    源码长度只是近似：`---` → `──────────` 这类降级会让可见文字变长。所以切完再按
    渲染结果复核一遍，超了就用膨胀比缩小预算重切（最多三轮，防病态输入死循环）。
    """
    if not text:
        return []
    if len(text) <= limit and visible_len(render_html(text)) <= TG_TEXT_LIMIT:
        return [text]

    chunks = _split_source(text, limit)
    for _ in range(3):
        over = [
            (i, c) for i, c in enumerate(chunks)
            if visible_len(render_html(c)) > TG_TEXT_LIMIT
        ]
        if not over:
            break
        rebuilt: list[str] = []
        for i, c in enumerate(chunks):
            if all(i != j for j, _ in over):
                rebuilt.append(c)
                continue
            grown = visible_len(render_html(c))
            tighter = max(120, int(len(c) * (TG_TEXT_LIMIT / max(1, grown)) * 0.85))
            parts = _split_source(c, tighter)
            rebuilt.extend(parts if len(parts) > 1 else [c])
        if rebuilt == chunks:
            break
        chunks = rebuilt
    return chunks


def tail_md(text: str, limit: int = SPLIT_LIMIT) -> str:
    """流式帧用：只保留尾部 limit 字符（前面加省略标记）。

    切点可能落在围栏内部，此时要用**原 marker + info** 把围栏重新打开，否则代码块
    的尾巴会被当成正文渲染（`~~~` 尤其容易踩：只补 ``` 是补不上的）。
    """
    if len(text) <= limit:
        return text
    cut_at = len(text) - limit
    cut = text[cut_at:]
    nl = cut.find("\n")
    if 0 <= nl < 200:
        cut_at += nl + 1
        cut = text[cut_at:]

    fence: Optional[tuple[str, str]] = None
    for line in text[:cut_at].replace("\r\n", "\n").split("\n"):
        fence = _fence_step(line, fence)
    if fence is not None:
        cut = f"{fence[0]}{_sanitize_lang(fence[1])}\n" + cut
    return "…（前文略）\n" + cut
