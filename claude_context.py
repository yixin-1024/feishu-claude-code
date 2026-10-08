"""把本机 Claude Code 的规则 / 记忆 / skill 接给非 Claude 后端（qoder、kiro）。

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
from typing import Optional

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


def claude_memory_dir(cwd: str) -> str:
    return os.path.join(CLAUDE_HOME, "projects", claude_project_slug(cwd), "memory")


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


def _project_claude_mds(cwd: str) -> list[str]:
    """cwd 往上到 $HOME（不含）路上的 CLAUDE.md，排除旁边 AGENTS.md 已经指向它的。

    qoder 会自动加载项目里的 AGENTS.md；spx 这类仓库已经把 AGENTS.md 软链成
    CLAUDE.md，再提示一遍就重复了。
    """
    home = os.path.realpath(os.path.expanduser("~"))
    out: list[str] = []
    cur = os.path.realpath(os.path.expanduser(cwd))
    while True:
        if cur == home or cur == os.path.dirname(cur):
            break
        cm = os.path.join(cur, "CLAUDE.md")
        if os.path.isfile(cm):
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


def _memory_index(cwd: str) -> tuple[str, str]:
    """(记忆目录, 截断后的 MEMORY.md)；没有就 ("", "")。"""
    mem_dir = claude_memory_dir(cwd)
    index_path = os.path.join(mem_dir, "MEMORY.md")
    if not os.path.isfile(index_path):
        return "", ""
    raw = _read(index_path, MEMORY_INDEX_MAX_CHARS)
    lines = raw.split("\n")
    if len(lines) > MEMORY_INDEX_MAX_LINES:
        raw = "\n".join(lines[:MEMORY_INDEX_MAX_LINES]) + "\n…（索引超过 200 行，后面没贴）"
    return mem_dir, raw


def build_claude_context_brief(cwd: Optional[str], skills_note: str = "") -> str:
    """给非 Claude 后端的提示词段落：Claude Code 的规则 / 记忆 / skill 在哪、怎么读。

    什么都没有（干净机器）时返回空串，不往提示词里塞空壳。
    """
    cwd = cwd or os.path.expanduser("~")
    parts: list[str] = []

    global_rules = _read(os.path.join(CLAUDE_HOME, "CLAUDE.md"), GLOBAL_RULES_MAX_CHARS)
    if global_rules:
        parts.append(
            "■ 全局规则（~/.claude/CLAUDE.md，用户写给 agent 的长期指令，和本提示词一样要遵守）：\n"
            + global_rules
        )

    project_mds = _project_claude_mds(cwd)
    if project_mds:
        parts.append(
            "■ 项目规则：下面这些 CLAUDE.md 你不会自动加载，**动手前先读完**，里面是这个项目的约定、"
            "禁区和常用命令：\n" + "\n".join(f"- {p}" for p in project_mds)
        )

    mem_dir, index = _memory_index(cwd)
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
        "这台机器上主力 agent 是 Claude Code，用户的长期规则、项目记忆和 skill 都存在它那边，"
        "你默认读不到。下面把它们接给你：\n\n"
        + "\n\n".join(parts)
        + "\n"
    )
