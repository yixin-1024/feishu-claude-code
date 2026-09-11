"""把模型输出的 markdown 归一成 Lark 卡片真能渲染的样子。

实测会在卡片上"漏字面量"的两类写法：

1. LaTeX 数学。卡片没有公式渲染器，`$\\rightarrow$` / `\\(x^2\\)` / `$$...$$`
   会原样显示成一串反斜杠。这里把常见符号降级成 Unicode（→ × ≤ α …）。
2. `**加粗**` 紧贴中文标点，例如 `她觉得你**"狡辩"**；`。CommonMark 的
   flanking 规则下，前面是汉字、后面是标点的 `**` 不算合法开界符，于是星号
   原样显示、加粗失效。这里把标点挪到 `**` 外面 → `她觉得你"**狡辩**"；`。

代码块（围栏和行内）一律原样保留——里面的 `$` 和 `*` 本来就该是字面量。
"""

import re
import unicodedata

# 围栏代码块 / 行内代码。第二个分支收尾未闭合的围栏（流式中途会出现）。
_CODE_SPLIT = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~|```[\s\S]*$|`[^`\n]*`)")

# ---------------------------------------------------------------- LaTeX

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}

_SYMBOLS = {
    # 箭头
    "rightarrow": "→", "to": "→", "longrightarrow": "⟶", "Rightarrow": "⇒",
    "implies": "⇒", "leftarrow": "←", "gets": "←", "longleftarrow": "⟵",
    "Leftarrow": "⇐", "leftrightarrow": "↔", "Leftrightarrow": "⇔",
    "iff": "⇔", "mapsto": "↦", "uparrow": "↑", "downarrow": "↓",
    "rightleftharpoons": "⇌", "hookrightarrow": "↪",
    # 关系 / 运算
    "times": "×", "div": "÷", "cdot": "·", "cdots": "⋯", "ldots": "…", "dots": "…",
    "pm": "±", "mp": "∓", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "sim": "∼", "simeq": "≃",
    "equiv": "≡", "propto": "∝", "ll": "≪", "gg": "≫", "ast": "∗", "star": "⋆",
    "oplus": "⊕", "otimes": "⊗", "circ": "∘", "bullet": "•",
    # 集合 / 逻辑
    "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩", "setminus": "\\",
    "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
    "nexists": "∄", "neg": "¬", "lnot": "¬", "land": "∧", "wedge": "∧",
    "lor": "∨", "vee": "∨", "therefore": "∴", "because": "∵",
    # 微积分 / 大符号
    "sum": "∑", "prod": "∏", "int": "∫", "iint": "∬", "oint": "∮",
    "partial": "∂", "nabla": "∇", "infty": "∞", "sqrt": "√",
    "lim": "lim", "log": "log", "ln": "ln", "exp": "exp", "max": "max",
    "min": "min", "sin": "sin", "cos": "cos", "tan": "tan",
    # 几何 / 其它
    "angle": "∠", "perp": "⊥", "parallel": "∥", "degree": "°", "prime": "′",
    "checkmark": "✓", "dagger": "†", "aleph": "ℵ", "hbar": "ℏ",
    "%": "%", "$": "$", "&": "&", "#": "#", "_": "_", "{": "{", "}": "}",
    # 间距 / 分隔：一律吃掉
    "left": "", "right": "", "big": "", "Big": "", "bigg": "", "Bigg": "",
    "quad": " ", "qquad": "  ", ",": " ", ";": " ", ":": " ", "!": "", " ": " ",
    "displaystyle": "", "limits": "", "nolimits": "",
}
_SYMBOLS.update(_GREEK)

# 只保留内容的"包装类"命令
_UNWRAP = re.compile(
    r"\\(?:text|textbf|textit|mathrm|mathbf|mathit|mathsf|mathtt|mathcal|mathbb"
    r"|operatorname|bm|boldsymbol|hbox|mbox)\s*\{([^{}]*)\}"
)

_SUP = str.maketrans(
    "0123456789+-=()aeiouxyzhijklmnprstw",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵉⁱᵒᵘˣʸᶻʰⁱʲᵏˡᵐⁿᵖʳˢᵗʷ",
)
_SUB = str.maketrans("0123456789+-=()aeioxhklmnpst", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑᵢₒₓₕₖₗₘₙₚₛₜ")

_MATH_CMD = re.compile(r"\\([A-Za-z]+|[%$&#_{},;:! ])")
_FRAC = re.compile(r"\\(?:d|t)?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")


def _atom(part: str) -> str:
    """分式的分子/分母：单个词不加括号，带运算符的才括起来。"""
    part = part.strip()
    return part if re.fullmatch(r"[\w.]+", part) else f"({part})"


def _script(body: str, table: dict, fallback: str) -> str:
    """上下标：能整段映射成 Unicode 就映射，否则退回 `^(...)` 形式。"""
    mapped = body.translate(table)
    if all(ord(ch) > 127 or ch == " " for ch in mapped):
        return mapped
    return f"{fallback}({body})" if len(body) > 1 else f"{fallback}{body}"


def _math_to_text(src: str) -> str:
    """把一段 LaTeX 公式降级成纯文本 / Unicode 符号。"""
    out = src
    for _ in range(4):  # 嵌套 \text{\mathrm{..}} 这类，跑几轮直到不动
        new = _UNWRAP.sub(lambda m: m.group(1), out)
        new = _FRAC.sub(lambda m: f"{_atom(m.group(1))}/{_atom(m.group(2))}", new)
        if new == out:
            break
        out = new
    out = re.sub(r"\^\s*\{([^{}]*)\}", lambda m: _script(m.group(1), _SUP, "^"), out)
    out = re.sub(r"_\s*\{([^{}]*)\}", lambda m: _script(m.group(1), _SUB, "_"), out)
    out = re.sub(r"\^\s*([A-Za-z0-9])", lambda m: _script(m.group(1), _SUP, "^"), out)
    out = re.sub(r"_\s*([A-Za-z0-9])", lambda m: _script(m.group(1), _SUB, "_"), out)
    # 未收录的命令去掉反斜杠留下词本身，总比 `\foo` 好看
    out = _MATH_CMD.sub(lambda m: _SYMBOLS.get(m.group(1), m.group(1)), out)
    out = out.replace("\\\\", " ").replace("{", "").replace("}", "")
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _looks_like_math(inner: str) -> bool:
    """`$...$` 里到底是公式还是"$5 涨到 $8"这种价钱，判错了会吃掉正文。"""
    if not inner.strip():
        return False
    # "$5 涨到 $8"：以数字开头又夹着空格的，一律判为钱，哪怕中间蹭到反斜杠
    if re.match(r"\d", inner) and re.search(r"\s", inner):
        return False
    if re.search(r"\\[A-Za-z]+|\\[%$&#_{}]", inner):
        return True
    # 没有命令时只认「纯 ASCII、不带空格、有上下标」的写法，
    # 否则 "$A_B and $C_D" 这种 shell 变量会被当成公式吃掉
    return (
        bool(re.search(r"[\^_]", inner))
        and inner.isascii()
        and not re.search(r"\s", inner)
    )


def _strip_latex(text: str) -> str:
    if "$" not in text and "\\" not in text:
        return text
    text = re.sub(r"\$\$([\s\S]+?)\$\$", lambda m: _math_to_text(m.group(1)), text)
    text = re.sub(r"\\\[([\s\S]+?)\\\]", lambda m: _math_to_text(m.group(1)), text)
    text = re.sub(r"\\\(([\s\S]+?)\\\)", lambda m: _math_to_text(m.group(1)), text)
    return re.sub(
        r"(?<!\\)\$([^$\n]{1,400}?)(?<!\\)\$",
        lambda m: _math_to_text(m.group(1)) if _looks_like_math(m.group(1)) else m.group(0),
        text,
    )


# ------------------------------------------------------------- 加粗/删除线

def _is_ws(ch: str) -> bool:
    # 行首行尾按空白算（CommonMark 的定义）
    return ch == "" or ch.isspace()


def _is_punct(ch: str) -> bool:
    return ch != "" and unicodedata.category(ch)[0] in ("P", "S")


def _left_flanking(prev_ch: str, next_ch: str) -> bool:
    if _is_ws(next_ch):
        return False
    if not _is_punct(next_ch):
        return True
    return _is_ws(prev_ch) or _is_punct(prev_ch)


def _right_flanking(prev_ch: str, next_ch: str) -> bool:
    if _is_ws(prev_ch):
        return False
    if not _is_punct(prev_ch):
        return True
    return _is_ws(next_ch) or _is_punct(next_ch)


def _fix_marker(text: str, marker: str) -> str:
    """把 `**"X"**` 这种渲染不出来的写法改写成 `"**X**"`。"""
    esc = re.escape(marker)
    pattern = re.compile(rf"{esc}(?!\s)([^\n]+?)(?<!\s){esc}")

    def repl(m: re.Match) -> str:
        body = m.group(1)
        if body.startswith(marker[0]) or body.endswith(marker[0]):
            return m.group(0)  # ***粗斜体*** 之类，别碰
        prev_ch = text[m.start() - 1] if m.start() else ""
        next_ch = text[m.end()] if m.end() < len(text) else ""
        if _left_flanking(prev_ch, body[0]) and _right_flanking(body[-1], next_ch):
            return m.group(0)  # 本来就渲染得出来
        # 把首尾的标点挪到 `**` 外面，让开界符两侧都是实字
        head = 0
        while head < len(body) and _is_punct(body[head]):
            head += 1
        tail = len(body)
        while tail > head and _is_punct(body[tail - 1]):
            tail -= 1
        lead, core, trail = body[:head], body[head:tail], body[tail:]
        if not core:
            return m.group(0)
        after = trail[0] if trail else next_ch
        if not (_left_flanking(lead[-1] if lead else prev_ch, core[0])
                and _right_flanking(core[-1], after)):
            return m.group(0)
        return f"{lead}{marker}{core}{marker}{trail}"

    return pattern.sub(repl, text)


def _fix_emphasis(text: str) -> str:
    for marker in ("**", "__", "~~"):
        if marker in text:
            text = _fix_marker(text, marker)
    return text


# ---------------------------------------------------------------- 入口

def normalize_lark_md(text: str) -> str:
    """卡片渲染前的最后一道整形。非字符串 / 空串原样返回。"""
    if not text or not isinstance(text, str):
        return text
    parts = _CODE_SPLIT.split(text)
    for i in range(0, len(parts), 2):  # 偶数下标是代码块之外的正文
        parts[i] = _fix_emphasis(_strip_latex(parts[i]))
    return "".join(parts)
