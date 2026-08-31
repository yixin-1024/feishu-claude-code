#!/usr/bin/env python3
"""agent 定时任务的「删 / 停 / 改」——纯文本外科手术 + 复用已有 /reload，**免重启**。

为什么整套逻辑放在这一侧、而不是像 schedule_cron 那样加 bot 端点：
scheduler.py / http_server.py 活在常驻 bot 进程里，往那儿加函数或路由必须 /restart
才生效；而 cc_mcp_server 是每个 turn 由 agent 后端重新 spawn 的短命进程，改完**下一轮
就是新代码**。所以这里直接改 scheduled_tasks.yaml，再打一发**早就存在的**
GET /reload（http_server._handle_reload → scheduler.reload_tasks）让常驻 scheduler
全量 remove+add 重建 job —— 删/停/改立刻上线，bot 一秒都不用停。

为什么不 yaml.safe_load + safe_dump 整份重写（scheduler.schedule_cron 也刻意避开了）：
  1) _load_tasks 对整份文本做 os.path.expandvars，yaml 里存在 ${SPX_DISPATCH_CHAT_ID}
     这类引用；一次 load+dump 会把它们烤成字面值，把 chat_id 泄漏进代码库；
  2) 注释会被 dump 全部抹掉（比如「APScheduler dow=0 是周一不是周日」那条血泪注解）。
所以这里只按顶层 list item 切块、只替换目标条目里涉及的那一行，其余字节原样保留。

同理不 import yaml：跟 cc_mcp_server 一样保持 stdlib-only。写值用 json.dumps ——
JSON 字符串字面量本身就是合法的 YAML 双引号标量。

安全边界：所有操作都按 chat_id 严格限定在**调用方所在的那个群**（与 list_crons 的
可见性同口径），跨群既看不见也改不动；chat_id 写成未展开 ${VAR} 的条目一律判为
「不属于本群」拒绝操作（fail-safe，宁可管不了也不误删）。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Optional

TASKS_FILENAME = "scheduled_tasks.yaml"

# 顶层 list item 的起始行；块内字段行（本文件只处理扁平标量条目）。
_ITEM_RE = re.compile(r"^-\s")
_FIELD_RE = re.compile(r"^(?:-\s+|\s{1,4})([A-Za-z_]\w*):(?=\s|$)(.*)$")


# ── 路径 ──────────────────────────────────────────────────────

def repo_dir() -> str:
    """本文件所在目录 = 仓库根 = scheduled_tasks.yaml 与 data/ 的父目录。

    与 main.py 用 __file__ 拼 sched_path 是同一口径，不依赖进程 cwd
    （MCP server 的 cwd 是用户的工作目录，不是本仓库）。"""
    return os.path.dirname(os.path.abspath(__file__))


def tasks_path() -> str:
    return (os.environ.get("CC_LARK_TASKS_YAML") or "").strip() or os.path.join(
        repo_dir(), TASKS_FILENAME
    )


def _paused_dir() -> str:
    return os.path.join(repo_dir(), "data", "agent_crons", "paused")


def _removed_dir() -> str:
    return os.path.join(repo_dir(), "data", "agent_crons", "removed")


# ── YAML 标量的最小读写（只够本文件用，不追求通用） ─────────────

def _parse_scalar(raw: str) -> str:
    """还原 `key:` 右边的单行标量。支持双引号 / 单引号 / 裸值（含行尾注释）。"""
    s = raw.strip()
    if not s:
        return ""
    if s[0] == '"':
        try:
            value, _ = json.JSONDecoder().raw_decode(s)
            if isinstance(value, str):
                return value
        except ValueError:
            pass
        return s.strip('"')
    if s[0] == "'":
        # YAML 单引号里 '' 表示一个字面单引号
        out, i = [], 1
        while i < len(s):
            if s[i] == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                break
            out.append(s[i])
            i += 1
        return "".join(out)
    # 裸标量：只有「空格 + #」才开注释
    cut = s.find(" #")
    if cut >= 0:
        s = s[:cut]
    return s.strip()


def _emit_scalar(value: str) -> str:
    """总是输出 JSON 双引号形式 —— 合法 YAML，且把转义问题一次性交给 json。"""
    return json.dumps(value, ensure_ascii=False)


# ── 条目切块 ──────────────────────────────────────────────────

def split_entries(text: str) -> tuple[list[str], list[list[str]]]:
    """把 yaml 文本切成 (前言行, [条目块行, ...])。

    条目块 = 一个顶层 `- ` 行 + 其后所有非顶层行（含块内注释），直到下一个 `- ` 或 EOF。
    前言 = 第一个 `- ` 之前的所有行（文件头注释等），原样保留。"""
    lines = text.split("\n")
    first = next((i for i, ln in enumerate(lines) if _ITEM_RE.match(ln)), None)
    if first is None:
        return lines, []
    preamble = lines[:first]
    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in lines[first:]:
        if _ITEM_RE.match(ln) and cur:
            blocks.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        blocks.append(cur)
    return preamble, blocks


def join_entries(preamble: list[str], blocks: list[list[str]]) -> str:
    out = list(preamble)
    for b in blocks:
        out.extend(b)
    text = "\n".join(out)
    return text if text.endswith("\n") else text + "\n"


def field_index(block: list[str], key: str) -> Optional[int]:
    for i, ln in enumerate(block):
        m = _FIELD_RE.match(ln)
        if m and m.group(1) == key:
            return i
    return None


def field_value(block: list[str], key: str) -> Optional[str]:
    i = field_index(block, key)
    if i is None:
        return None
    return _parse_scalar(_FIELD_RE.match(block[i]).group(2))


def set_field(block: list[str], key: str, value: str) -> None:
    """就地改一行；字段不存在则插在 prompt_file/prompt 之前（保持 prompt 收尾的排版）。"""
    i = field_index(block, key)
    if i is not None:
        m = _FIELD_RE.match(block[i])
        indent = block[i][: m.start(1)]
        block[i] = f"{indent}{key}: {_emit_scalar(value)}"
        return
    anchor = field_index(block, "prompt_file")
    if anchor is None:
        anchor = field_index(block, "prompt")
    line = f"  {key}: {_emit_scalar(value)}"
    if anchor is None:
        # 追加到块尾（跳过尾部空行）
        end = len(block)
        while end > 0 and not block[end - 1].strip():
            end -= 1
        block.insert(end, line)
    else:
        block.insert(anchor, line)


def append_block(blocks: list[list[str]], block: list[str]) -> None:
    """把条目追加到末尾，并把原末块尾部的空行搬到新末块尾部。

    切块时文件末尾的换行会以一个空行的形式落在最后一个块里；直接 append 会把它
    夹在两个条目中间，于是每来回 pause/resume 一次就多攒一个空行。"""
    tail: list[str] = []
    if blocks:
        last = blocks[-1]
        while last and not last[-1].strip():
            tail.insert(0, last.pop())
    blocks.append(list(block) + tail)


def drop_field(block: list[str], key: str) -> None:
    i = field_index(block, key)
    if i is not None:
        del block[i]


# ── 归属校验 ──────────────────────────────────────────────────

def block_in_chat(block: list[str], chat_id: str) -> bool:
    """条目是否属于 chat_id。未展开的 ${VAR} 一律判否（fail-safe）。"""
    if not chat_id:
        return False
    raw = field_value(block, "chat_id")
    if not raw:
        return False
    expanded = os.path.expandvars(raw)
    if "$" in expanded:  # env 里没有这个变量 → 归属存疑，不给碰
        return False
    return expanded == chat_id


def _find(blocks: list[list[str]], name: str) -> Optional[int]:
    for i, b in enumerate(blocks):
        if field_value(b, "name") == name:
            return i
    return None


# ── 文件读写 ──────────────────────────────────────────────────

def _read(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write(path: str, text: str) -> None:
    """同目录 tmp + os.replace：常驻 bot 随时可能在读，避免读到写了一半的 yaml。"""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _apply_and_reload(path: str, original: str, new_text: str, name: str,
                      reload_fn: Callable[[], dict]) -> dict:
    """落盘 → reload → 若常驻 scheduler 报本任务有错就整份回滚再 reload。

    这一步顺带把「cron 合法性」的校验外包给了 bot 自己的解析器（reload_tasks 会把
    `cron 非法` 之类塞进 errors），比在这边复刻一份 crontab 语法检查更可靠。"""
    _write(path, new_text)
    try:
        res = reload_fn()
    except Exception as e:  # noqa: BLE001 —— reload 打不通就当作失败回滚
        _write(path, original)
        return {"ok": False, "error": f"reload 失败已回滚: {type(e).__name__}: {e}"}
    if not res.get("ok", True):
        _write(path, original)
        return {"ok": False, "error": f"reload 失败已回滚: {res.get('error')}"}
    bad = [e for e in (res.get("errors") or []) if str(e).startswith(f"{name}:")]
    if bad:
        _write(path, original)
        reload_fn()
        return {"ok": False, "error": f"改动被 scheduler 拒绝，已回滚: {bad[0]}"}
    return {"ok": True, "reload": res}


# ── 对外操作 ──────────────────────────────────────────────────

def cancel(name: str, *, chat_id: str, reload_fn: Callable[[], dict]) -> dict:
    """永久删除一条任务。原始条目文本留档到 data/agent_crons/removed/ 以便找回。"""
    path = tasks_path()
    original = _read(path)
    preamble, blocks = split_entries(original)
    idx = _find(blocks, name)
    if idx is None:
        return {"ok": False, "error": f"没有名为 {name!r} 的任务"}
    if not block_in_chat(blocks[idx], chat_id):
        return {"ok": False, "error": f"{name!r} 不属于本群，拒绝操作"}

    block = blocks.pop(idx)
    os.makedirs(_removed_dir(), exist_ok=True)
    stash = os.path.join(_removed_dir(), f"{name}.{int(time.time())}.yaml")
    with open(stash, "w", encoding="utf-8") as f:
        f.write("\n".join(block).rstrip("\n") + "\n")

    res = _apply_and_reload(path, original, join_entries(preamble, blocks), name, reload_fn)
    if not res.get("ok"):
        return res
    return {"ok": True, "name": name, "stash": os.path.relpath(stash, repo_dir())}


def pause(name: str, *, chat_id: str, reload_fn: Callable[[], dict]) -> dict:
    """暂停：把条目原样搬到 data/agent_crons/paused/<name>.yaml，从主 yaml 摘掉。

    不用「在条目里加 paused: true」是因为 ScheduledTask.from_dict 不认识这个字段、
    会直接忽略它照常注册 —— 那要让 bot 认得就得改 scheduler.py，就又要重启了。
    搬文件则完全在本侧闭环，且照样落盘、重启后仍是暂停态。"""
    path = tasks_path()
    original = _read(path)
    preamble, blocks = split_entries(original)
    idx = _find(blocks, name)
    if idx is None:
        return {"ok": False, "error": f"没有名为 {name!r} 的任务（已暂停的用 resume_cron 恢复）"}
    if not block_in_chat(blocks[idx], chat_id):
        return {"ok": False, "error": f"{name!r} 不属于本群，拒绝操作"}

    block = blocks.pop(idx)
    os.makedirs(_paused_dir(), exist_ok=True)
    sidecar = os.path.join(_paused_dir(), f"{name}.yaml")
    with open(sidecar, "w", encoding="utf-8") as f:
        f.write("\n".join(block).rstrip("\n") + "\n")

    res = _apply_and_reload(path, original, join_entries(preamble, blocks), name, reload_fn)
    if not res.get("ok"):
        os.remove(sidecar)
        return res
    return {"ok": True, "name": name}


def resume(name: str, *, chat_id: str, reload_fn: Callable[[], dict]) -> dict:
    """恢复：把 paused 的条目追加回主 yaml（append-only，不动既有内容）。"""
    sidecar = os.path.join(_paused_dir(), f"{name}.yaml")
    if not os.path.exists(sidecar):
        return {"ok": False, "error": f"{name!r} 不在暂停列表里"}
    block = _read(sidecar).rstrip("\n").split("\n")
    if not block or not _ITEM_RE.match(block[0]):
        return {"ok": False, "error": f"暂停存档 {name}.yaml 格式已损坏（首行不是 YAML 列表项）"}
    if not block_in_chat(block, chat_id):
        return {"ok": False, "error": f"{name!r} 不属于本群，拒绝操作"}

    path = tasks_path()
    original = _read(path)
    preamble, blocks = split_entries(original)
    if _find(blocks, name) is not None:
        return {"ok": False, "error": f"{name!r} 已经在运行中的任务里了"}
    append_block(blocks, block)
    res = _apply_and_reload(path, original, join_entries(preamble, blocks), name, reload_fn)
    if not res.get("ok"):
        return res
    os.remove(sidecar)
    return {"ok": True, "name": name}


# 可改字段 → yaml 里的 key。prompt 单独走文件，不在这张表里。
_EDITABLE = {"cron": "cron", "title": "topic_title", "model": "model", "effort": "effort"}


def update(name: str, *, chat_id: str, reload_fn: Callable[[], dict],
           cron: Optional[str] = None, prompt: Optional[str] = None,
           title: Optional[str] = None, model: Optional[str] = None,
           effort: Optional[str] = None) -> dict:
    """改一条任务的 cron / prompt / 标题 / model / effort。只动传进来的字段。

    model / effort 传空串 = 清掉该覆盖（回落到 profile 默认）；不传 = 不动。
    暂停中的任务也能改（改 sidecar），恢复时才会被 scheduler 校验。"""
    changes = {k: v for k, v in
               (("cron", cron), ("title", title), ("model", model), ("effort", effort))
               if v is not None}
    if not changes and prompt is None:
        return {"ok": False, "error": "至少要给一个要改的字段（cron / prompt / title / model / effort）"}
    if any("\n" in v for v in changes.values()):
        return {"ok": False, "error": "cron / title / model / effort 不能含换行"}
    for key in ("cron", "title"):
        if key in changes and not changes[key].strip():
            return {"ok": False, "error": f"{key} 不能改成空（清空只对 model / effort 有意义）"}

    path = tasks_path()
    original = _read(path)
    preamble, blocks = split_entries(original)
    idx = _find(blocks, name)
    paused_sidecar = os.path.join(_paused_dir(), f"{name}.yaml")
    is_paused = idx is None and os.path.exists(paused_sidecar)
    if idx is None and not is_paused:
        return {"ok": False, "error": f"没有名为 {name!r} 的任务"}

    block = _read(paused_sidecar).rstrip("\n").split("\n") if is_paused else blocks[idx]
    if not block_in_chat(block, chat_id):
        return {"ok": False, "error": f"{name!r} 不属于本群，拒绝操作"}

    # prompt 先改文件（内容不进 yaml，从而绕开多行转义），失败就整体不落盘
    prompt_backup: Optional[tuple[str, str]] = None
    if prompt is not None:
        if not prompt.strip():
            return {"ok": False, "error": "prompt 不能为空"}
        rel = field_value(block, "prompt_file")
        if not rel:
            return {"ok": False, "error":
                    f"{name!r} 的 prompt 是内联写在 yaml 里的（非 prompt_file），本工具不改它"}
        abs_prompt = rel if os.path.isabs(rel) else os.path.join(repo_dir(), rel)
        prompt_backup = (abs_prompt, _read(abs_prompt))
        try:
            os.makedirs(os.path.dirname(abs_prompt), exist_ok=True)
            _write(abs_prompt, prompt.strip() + "\n")
        except OSError as e:
            return {"ok": False, "error": f"写 prompt 文件失败: {type(e).__name__}: {e}"}

    for key, value in changes.items():
        yaml_key = _EDITABLE[key]
        if value == "" and key in ("model", "effort"):
            drop_field(block, yaml_key)
        else:
            set_field(block, yaml_key, value)

    if is_paused:
        # 暂停态不在 scheduler 里，没什么可 reload 的，直接落 sidecar
        _write(paused_sidecar, "\n".join(block).rstrip("\n") + "\n")
        return {"ok": True, "name": name, "paused": True, "changed": sorted(changes) +
                (["prompt"] if prompt is not None else [])}

    blocks[idx] = block
    res = _apply_and_reload(path, original, join_entries(preamble, blocks), name, reload_fn)
    if not res.get("ok"):
        if prompt_backup:
            _write(*prompt_backup)
        return res
    return {"ok": True, "name": name,
            "changed": sorted(changes) + (["prompt"] if prompt is not None else [])}


def list_paused(chat_id: str) -> list[dict]:
    """本群暂停中的任务（scheduler 里没有它们，list_crons 看不见，得从这边补）。"""
    out = []
    d = _paused_dir()
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".yaml"):
            continue
        block = _read(os.path.join(d, fn)).rstrip("\n").split("\n")
        if not block_in_chat(block, chat_id):
            continue
        out.append({
            "name": field_value(block, "name") or fn[:-5],
            "cron": field_value(block, "cron") or "?",
            "title": field_value(block, "topic_title") or "",
        })
    return out
