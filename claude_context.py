"""把本机 Claude Code 的规则 / 记忆 / skill 接给非 Claude 后端（qoder、kiro、pi、codex、agy）。

用户的长期规则和记忆都记在 Claude Code 那边，换个后端就全丢了：
  - 全局规则 ~/.claude/CLAUDE.md、项目规则 <dir>/CLAUDE.md —— qoder 只认 AGENTS.md
    （~/.qoder/AGENTS.md、<project>/AGENTS.md），不读 CLAUDE.md；
  - 自动记忆 ~/.claude/projects/<slug>/memory/MEMORY.md —— qoder 有自己的记忆目录，
    看不到这份；
  - skill：qoder 扫 ~/.qoder/skills 和 ~/.agents/skills。lark-* 这批本来就在
    ~/.agents/skills，但 ~/.claude/skills 里 Claude 独有的（spxpay-* / bg-job / …）
    和项目级 <cwd>/.claude/skills 它都看不到。

做法照 agy 的思路（agy 是把 ~/.gemini/config/skills 整个软链到 ~/.claude/skills）：
skill 逐个软链进 qoder 的用户级 skill 目录；规则和记忆拼成一段提示词，跟着
--append-system-prompt 进去，索引按 Claude Code 自己的读取上限截断，条目全文让模型
用到时再读。
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Optional

CLAUDE_HOME = os.path.expanduser("~/.claude")
CLAUDE_SKILLS_DIR = os.path.join(CLAUDE_HOME, "skills")
AGENTS_SKILLS_DIR = os.path.expanduser("~/.agents/skills")

# Claude Code 只把 MEMORY.md 的前 200 行放进上下文，超出的部分它自己也看不到；
# 照抄同一个上限，两边看到的索引一致。字符上限只防有人把索引写成长文——spx 那份
# 200 行约 3 万字，上限要比它宽，不然会在 200 行以内先被字数截掉。
MEMORY_INDEX_MAX_LINES = 200
MEMORY_INDEX_MAX_CHARS = 32_000
GLOBAL_RULES_MAX_CHARS = 12_000


def claude_project_slug(path: str) -> str:
    """Claude Code 的项目目录名：绝对路径里非字母数字一律换成 '-'。"""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(os.path.expanduser(path)))


def claude_memory_dir(cwd: str, claude_home: Optional[str] = None) -> str:
    """claude_home：Claude Code 的配置目录（CLAUDE_CONFIG_DIR），不给就是 ~/.claude。"""
    home = os.path.expanduser(claude_home) if claude_home else CLAUDE_HOME
    return os.path.join(home, "projects", claude_project_slug(cwd), "memory")


def _read(path: str, max_chars: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(max_chars + 1)
    except OSError:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…（超出长度，后面没贴，需要就读原文件）"
    return text.strip()


def _skill_description(skill_md: str) -> str:
    """SKILL.md frontmatter 里的 description（单行）；拿不到就空串。"""
    head = _read(skill_md, 4000)
    m = re.search(r"^description:\s*(.+)$", head, re.MULTILINE)
    if not m:
        return ""
    desc = m.group(1).strip().strip("'\"")
    return desc if len(desc) <= 160 else desc[:160] + "…"


def link_claude_skills(skills_root: str) -> list[str]:
    """把 ~/.claude/skills 里 qoder 看不到的 skill 软链进 skills_root。返回新建的名字。

    幂等：只补缺的。~/.agents/skills 里已有同名的（lark-* 那批）跳过，qoder 本来就能
    扫到；没有 SKILL.md 的目录（_shared 之类的公共目录）不链；skills_root 下已有的
    同名条目不管是不是软链都不动——用户自己放的 qoder skill 优先。
    """
    if not os.path.isdir(CLAUDE_SKILLS_DIR):
        return []
    try:
        names = sorted(os.listdir(CLAUDE_SKILLS_DIR))
    except OSError:
        return []
    created: list[str] = []
    for name in names:
        if name.startswith((".", "_")):
            continue
        src = os.path.join(CLAUDE_SKILLS_DIR, name)
        if not os.path.isfile(os.path.join(src, "SKILL.md")):
            continue
        if os.path.exists(os.path.join(AGENTS_SKILLS_DIR, name, "SKILL.md")):
            continue
        dst = os.path.join(skills_root, name)
        if os.path.lexists(dst):
            continue
        try:
            os.makedirs(skills_root, exist_ok=True)
            os.symlink(os.path.realpath(src), dst)
            created.append(name)
        except OSError:
            continue
    return created


def _project_claude_mds(cwd: str, skip: Iterable[str] = ()) -> list[str]:
    """cwd 往上到 $HOME（不含）路上的 CLAUDE.md，排除旁边 AGENTS.md 已经指向它的。

    qoder 会自动加载项目里的 AGENTS.md；spx 这类仓库已经把 AGENTS.md 软链成
    CLAUDE.md，再提示一遍就重复了。skip：后端自己会加载的 CLAUDE.md（codex 配了
    project_doc_fallback_filenames 时），也不再列。
    """
    skipped = {os.path.realpath(p) for p in skip}
    home = os.path.realpath(os.path.expanduser("~"))
    out: list[str] = []
    cur = os.path.realpath(os.path.expanduser(cwd))
    while True:
        if cur == home or cur == os.path.dirname(cur):
            break
        cm = os.path.join(cur, "CLAUDE.md")
        if os.path.isfile(cm) and os.path.realpath(cm) not in skipped:
            agents = os.path.join(cur, "AGENTS.md")
            loaded = os.path.exists(agents) and os.path.realpath(agents) == os.path.realpath(cm)
            if not loaded:
                out.append(cm)
        cur = os.path.dirname(cur)
    return out


def _project_skills(cwd: str) -> list[tuple[str, str, str]]:
    root = os.path.join(os.path.expanduser(cwd), ".claude", "skills")
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        skill_md = os.path.join(root, name, "SKILL.md")
        if os.path.isfile(skill_md):
            out.append((name, _skill_description(skill_md), skill_md))
    return out


def _memory_index(cwd: str, claude_home: Optional[str] = None) -> tuple[str, str]:
    """(记忆目录, 截断后的 MEMORY.md)；没有就 ("", "")。"""
    mem_dir = claude_memory_dir(cwd, claude_home)
    index_path = os.path.join(mem_dir, "MEMORY.md")
    if not os.path.isfile(index_path):
        return "", ""
    raw = _read(index_path, MEMORY_INDEX_MAX_CHARS)
    lines = raw.split("\n")
    if len(lines) > MEMORY_INDEX_MAX_LINES:
        raw = "\n".join(lines[:MEMORY_INDEX_MAX_LINES]) + "\n…（索引超过 200 行，后面没贴）"
    return mem_dir, raw


# CLAUDE.md 里独占一行的 `@路径` 是 Claude Code 的导入语法（如全局规则末尾的
# `@~/.claude/global-memory/MEMORY.md`）。别的后端不认这个语法，原样贴过去就是一行
# 看不懂的路径，所以展开成文件内容。
_IMPORT_LINE = re.compile(r"^@(\S+)\s*$")
IMPORT_MAX_CHARS = 8_000


def _import_targets(text: str, base_dir: str) -> list[tuple[int, str]]:
    """(行号, 绝对路径)：text 里独占一行、指向已存在文件的 `@路径`。代码块里的不算。"""
    out: list[tuple[int, str]] = []
    fenced = False
    for i, line in enumerate(text.split("\n")):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        m = None if fenced else _IMPORT_LINE.match(line.strip())
        if not m:
            continue
        raw = os.path.expanduser(m.group(1))
        path = raw if os.path.isabs(raw) else os.path.join(base_dir, raw)
        if os.path.isfile(path):
            out.append((i, os.path.normpath(path)))
    return out


def _imported_block(path: str) -> str:
    body = _read(path, IMPORT_MAX_CHARS)
    if not body:
        return ""
    return f"（以下是 {path} 的内容，里面的相对链接都相对 {os.path.dirname(path)}）\n{body}"


def _expand_imports(text: str, base_dir: str) -> str:
    targets = dict(_import_targets(text, base_dir))
    if not targets:
        return text
    lines = text.split("\n")
    for i, path in targets.items():
        block = _imported_block(path)
        if block:
            lines[i] = block
    return "\n".join(lines)


def _global_rules(claude_home: str, mode: str) -> tuple[str, list[str]]:
    """(要贴进提示词的正文, 被导入的文件列表)。

    mode="paste"：整份 CLAUDE.md，导入行展开；"imports"：只贴被导入的文件（全局记忆），
    规则正文不贴——后端自己的全局规则文件已经是改写过的同一套规矩时用；"none"：都不贴。
    """
    path = os.path.join(claude_home, "CLAUDE.md")
    if mode == "none":
        return "", []
    raw = _read(path, GLOBAL_RULES_MAX_CHARS)
    if not raw:
        return "", []
    imports = [p for _, p in _import_targets(raw, claude_home)]
    if mode == "imports":
        blocks = [b for b in (_imported_block(p) for p in imports) if b]
        return "\n\n".join(blocks), imports
    return _expand_imports(raw, claude_home), imports


def build_claude_context_brief(
    cwd: Optional[str], skills_note: str = "", include_project_rules: bool = True,
    *,
    claude_home: Optional[str] = None,
    global_rules: str = "paste",
    skip_rule_files: Iterable[str] = (),
    full: bool = True,
) -> str:
    """给非 Claude 后端的提示词段落：Claude Code 的规则 / 记忆 / skill 在哪、怎么读。

    include_project_rules=False：后端自己会沿目录往上加载 CLAUDE.md（pi），就别再列
    「项目规则你不会自动加载」那段，免得说错、也免得它把同一份文件再读一遍。
    claude_home：读哪套 Claude Code 配置（CLAUDE_CONFIG_DIR）；research 这类 profile
      用的是 ~/.claude-research，记忆和全局规则都在那边。不给就是 ~/.claude。
    global_rules：全局 CLAUDE.md 怎么接，见 _global_rules。
    skip_rule_files：后端自己会加载的项目 CLAUDE.md，不再列。
    full=False：只给路径不贴正文。系统提示每轮拼进用户消息的后端（agy、codex exec）
      在同一会话的后续轮用它——索引第一轮已经在对话里了，每轮再贴一遍会越攒越多。
    什么都没有（干净机器）时返回空串，不往提示词里塞空壳。
    """
    cwd = cwd or os.path.expanduser("~")
    # 路径一律写绝对路径：agy 跑在单独的 HOME 下，提示词里的 ~ 会被它解析到那个目录去。
    home = os.path.expanduser(claude_home) if claude_home else CLAUDE_HOME

    rules_text, imports = _global_rules(home, global_rules)
    project_mds = (
        _project_claude_mds(cwd, skip_rule_files) if include_project_rules else []
    )
    mem_dir, index = _memory_index(cwd, home)

    if not full:
        return _compact_brief(home, global_rules, rules_text, imports, project_mds, mem_dir, index)

    parts: list[str] = []
    if rules_text and global_rules == "paste":
        parts.append(
            f"■ 全局规则（{home}/CLAUDE.md，用户写给 agent 的长期指令，和本提示词一样要遵守）：\n"
            + rules_text
        )
    elif rules_text:
        parts.append(
            "■ 全局记忆（跨项目的长期偏好，Claude Code 每个会话都会带上）：\n" + rules_text
        )

    if project_mds:
        parts.append(
            "■ 项目规则：下面这些 CLAUDE.md 你不会自动加载，**动手前先读完**，里面是这个项目的约定、"
            "禁区和常用命令：\n" + "\n".join(f"- {p}" for p in project_mds)
        )

    if index:
        parts.append(
            f"■ 长期记忆（Claude Code 给这个项目记的，目录 {mem_dir}）。下面是索引 MEMORY.md，"
            "每条链接指向同目录下的一个文件：\n"
            "- 跟当前任务沾边的条目，**先读全文再动手**，别凭索引里的一句话下结论；索引没列出的"
            "文件也可能在目录里，用 grep 按关键词找。\n"
            "- 记忆反映的是写下那天的状态；里面提到的文件、函数、开关、数据，用之前先核实还在。\n"
            "- 这个目录归 Claude Code 管，只读，不要往里写或改。\n\n"
            + index
        )

    skill_lines = []
    if skills_note:
        skill_lines.append(skills_note)
    project_skills = _project_skills(cwd)
    if project_skills:
        skill_lines.append(
            "本项目还有项目级 skill（Claude Code 的 .claude/skills，你不会自动加载）。任务对得上就直接读对应的 "
            "SKILL.md 照着做；和同名的用户级 skill 冲突时以这里的为准：\n"
            + "\n".join(f"- {n}：{d}（{p}）" if d else f"- {n}（{p}）" for n, d, p in project_skills)
        )
    if skill_lines:
        parts.append("■ Skill：" + "\n".join(skill_lines))

    if not parts:
        return ""
    return (
        "【本机 Claude Code 的规则、记忆和 skill】\n"
        "这台机器上主力 agent 是 Claude Code，用户一直用它，长期规则、项目记忆和 skill 都存在它那边，"
        "记得最全，你默认读不到。用户的习惯、项目背景、踩过的坑、东西放在哪，先查这里再动手，"
        "别从零猜。下面把它们接给你：\n\n"
        + "\n\n".join(parts)
        + "\n"
    )


def _compact_brief(
    home: str, global_rules: str, rules_text: str, imports: list[str],
    project_mds: list[str], mem_dir: str, index: str,
) -> str:
    lines: list[str] = []
    if rules_text and global_rules == "paste":
        lines.append(f"- 全局规则：{home}/CLAUDE.md")
    elif imports:
        lines.append("- 全局记忆：" + "、".join(imports))
    if project_mds:
        lines.append("- 项目规则（动手前要读完）：" + "、".join(project_mds))
    if index:
        lines.append(
            f"- 长期记忆：{mem_dir}/MEMORY.md。跟当前任务沾边的条目先读全文再动手，索引没列的用 grep "
            "按关键词找；只读，别往里写。"
        )
    if not lines:
        return ""
    return (
        "【本机 Claude Code 的规则和记忆】本会话开头已经贴过全文，这里只留路径；"
        "上下文里找不到了就重新读文件：\n" + "\n".join(lines) + "\n"
    )
