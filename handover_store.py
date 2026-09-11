"""会话移交（handover）简报的渲染与落盘。

定位：`dispatch_task` 派的是**子任务**（做完回报父 agent）；`handover` 交的是**整项活的
所有权**——上一个 agent 上下文爆了/不想接着往下压了，就把"目标 / 已完成 / 未完成 /
注意事项"手工压缩成一份简报，整段交给下一个 agent（可以是别的后端），自己收工，
**不要求任何回报**。

这一层只干两件纯函数式的事，不碰 Lark、不碰事件循环：
  · render()  —— 把简报 dict 渲染成给接手方读的 markdown（顺序固定，缺项显式标注）
  · save()    —— 落盘到 data/handovers/，让接手方**自己上下文再爆时还能重读**
                 （简报同时内联在 prompt 里，所以落盘失败不该阻断移交）

落盘目录可用 CC_LARK_HANDOVER_DIR 覆盖（单测用）。
"""

from __future__ import annotations

import os
import re
import time

# 简报是"压缩后的交接件"，不是上下文转储。超过这个长度就该让移交方自己再压一轮：
# 它同时要进 Lark 顶楼 post + 接手方 prompt，无节制会把两边都撑坏。
MAX_BRIEF_CHARS = 40000

# 渲染顺序固定：接手方永远按"目标→已完成→未完成→注意事项→关键文件"读到简报。
SECTIONS: tuple[tuple[str, str], ...] = (
    ("goal", "任务目标（为什么做这件事 / 验收标准）"),
    ("completed", "已完成（可信但需抽查核实）"),
    ("remaining", "未完成 / 下一步（你要接着做的）"),
    ("notes", "注意事项 / 已知坑 / 已定决策"),
    ("files", "关键文件、命令与产物"),
)

# 移交的意义全在这三项上：没有目标接手方不知道要干什么，没有"未完成"就没有可接的活，
# 没有"已完成"接手方会重复劳动。缺一个直接拒，逼移交方把简报写全。
REQUIRED = ("goal", "completed", "remaining")


def _as_text(value) -> str:
    """简报字段允许 str 或 list[str]（模型两种都爱写），统一成文本。"""
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
        return "\n".join(f"- {v}" if not v.startswith(("-", "*", "#")) else v for v in items)
    if value is None:
        return ""
    return str(value).strip()


def normalize(brief: dict) -> dict:
    """取出已知字段并归一成文本。未知字段直接丢弃（别让脏 key 流进简报）。"""
    if not isinstance(brief, dict):
        raise ValueError("brief 必须是对象（goal / completed / remaining / notes / files）")
    return {key: _as_text(brief.get(key)) for key, _ in SECTIONS}


def missing_required(brief: dict) -> list[str]:
    """返回缺失的必填字段名（供调用方拼错误信息）。"""
    return [key for key in REQUIRED if not brief.get(key)]


def render(brief: dict, meta: dict) -> str:
    """渲染成接手方直接可读的 markdown 简报。brief 需先过 normalize()。"""
    lines = [f"# 会话移交简报 · {meta.get('title') or '（未命名任务）'}", ""]
    for label, value in (
        ("移交时间", meta.get("created_at", "")),
        ("移交方", meta.get("from", "")),
        ("原话题", meta.get("from_thread", "")),
        ("接手方", meta.get("to", "")),
        ("所在群", meta.get("chat_id", "")),
    ):
        if value:
            lines.append(f"- **{label}**：{value}")
    for key, title in SECTIONS:
        body = brief.get(key, "")
        lines += ["", f"## {title}", "", body or "（移交方未填写）"]
    return "\n".join(lines).strip() + "\n"


def directory() -> str:
    return os.getenv("CC_LARK_HANDOVER_DIR", "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "handovers"
    )


def _slug(text: str) -> str:
    """文件名里那段人可读的尾巴。非字母数字/中文一律压成 '-'。"""
    cleaned = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", text or "").strip("-")
    return cleaned[:40] or "handover"


def save(markdown: str, *, title: str = "") -> str:
    """把简报写进 data/handovers/，返回绝对路径。任何 OSError 向上抛，调用方降级。

    同名靠 O_EXCL 独占创建来避让：同一秒里两个移交（save 跑在 to_thread 里，真的会
    并发）不能互相覆盖——简报是这次交接唯一的留档。
    """
    target_dir = directory()
    os.makedirs(target_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = _slug(title)
    suffix = 0
    while True:
        name = f"{stamp}-{slug}.md" if not suffix else f"{stamp}-{slug}-{suffix}.md"
        path = os.path.join(target_dir, name)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            suffix += 1
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(markdown)
        return path


def append_target(path: str, thread_id: str) -> None:
    """移交成功后把接手话题补记到简报末尾（best-effort：失败不影响移交本身）。"""
    if not (path and thread_id):
        return
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"\n---\n\n接手话题：`{thread_id}`\n")
    except OSError:
        pass
