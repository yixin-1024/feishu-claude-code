#!/usr/bin/env python3
"""工作域（workspace）路由表 —— 「这活该派到哪个群」的单一真相源。

背景：dispatch_task 以前只会往「调用方所在的那个群」派活（chat_id 直接取
CC_LARK_CHAT_ID）。可**一个群就是一摊活**——群本身在 .env 里就钉着工作目录
（`<PROFILE>_CHAT_CWD_<chat_id>`）——于是在 cc-lark 群里（或打电话时）说
「去 KYT 那边查个东西」，活会被派回 cc-lark 群、在 cc-lark 的目录里跑，全错。

这张表把**人嘴里的项目名**映射到**该项目的群 + 工作目录**：

    spx / 支付 / SPXpay → oc_1875…（spx 开发bot 群）→ ~/…/payment/spx

一个工作目录常对应多个群（payment/spx 就有 10 个），所以每个工作域**只挑一个
群当派活入口**，其余的记进 `other_chats` 备查——这样"派到 SPX"永远是确定的一个群。

为什么是 JSON 不是 yaml：本模块被 cc_mcp_server 依赖，而那个进程刻意保持
stdlib-only（见它的模块 docstring），不能 import PyYAML。

为什么配置文件不进 git：和 scheduled_tasks.yaml / external_groups.yaml 同一口径
——含真实 chat_id 与个人路径。仓库里只留 workspaces.json.example 模板。

谁读它：
  · cc_mcp_server —— 生成 dispatch_task / schedule_cron 工具说明里的工作域清单，
    并把模型传来的 `workspace` 预检一遍（每个 turn 重新 spawn，改表**下一轮就生效**）；
  · http_server  —— 请求到达时**再解析一次**，这一次是权威的：chat_id 与 cwd 都以
    表为准，不信客户端传来的值（客户端只需传名字）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

CONFIG_FILENAME = "workspaces.json"


@dataclass
class Workspace:
    """一个工作域 = 一个项目 = 一个派活入口群 + 一个工作目录。"""
    name: str
    chat_id: str
    cwd: str = ""
    desc: str = ""
    chat_name: str = ""
    aliases: list[str] = field(default_factory=list)
    # 可选：该工作域默认用哪个 agent 跑（"gpt" / "agy" / …）。调用方显式传 agent
    # 时永远以调用方为准；这里只是"没点名时"的默认。空 = 沿用调用方自己的后端。
    agent: str = ""
    # 备查：同一工作目录下的其它群（不是派活入口，仅用于人工核对配置）
    other_chats: list[dict] = field(default_factory=list)

    def label(self) -> str:
        """给模型看的一行：名字（别名）→ 用途 [目录 / 群名]。"""
        alias = f"（也叫 {'/'.join(self.aliases)}）" if self.aliases else ""
        bits = [b for b in (self.desc, f"目录 {self.cwd}" if self.cwd else "",
                            f"群「{self.chat_name}」" if self.chat_name else "") if b]
        tail = f"　[{'；'.join(bits)}]" if bits else ""
        agent = f"　默认 agent={self.agent}" if self.agent else ""
        return f"{self.name}{alias}{tail}{agent}"


# ── 配置加载 ──────────────────────────────────────────────────

def config_path(path: str = "") -> str:
    """配置文件路径：显式参数 > CC_LARK_WORKSPACES_FILE > 本仓库 workspaces.json。"""
    if path:
        return os.path.expanduser(path)
    env = (os.environ.get("CC_LARK_WORKSPACES_FILE") or "").strip()
    if env:
        return os.path.expanduser(env)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILENAME)


# (abspath, mtime, size) -> 解析结果。表很小，但 dispatch 路径每次请求都要读，
# 缓存让"改完文件立刻生效"和"别每次都读盘"两件事同时成立。
_CACHE: dict[str, tuple[tuple, list[Workspace]]] = {}


def load(path: str = "") -> list[Workspace]:
    """读配置。文件不存在 / 坏了 / 格式不对 → 返回空表（功能静默不可用，绝不抛）。

    绝不抛是硬要求：这张表是**可选增强**，它出问题不该让 MCP server 起不来、
    更不该让原本好好的"派回当前群"也跟着挂掉。
    """
    p = config_path(path)
    try:
        st = os.stat(p)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        _CACHE.pop(p, None)
        return []
    cached = _CACHE.get(p)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    items = raw.get("workspaces") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[Workspace] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        chat_id = str(it.get("chat_id") or "").strip()
        if not name or not chat_id or _norm(name) in seen:
            continue  # 没名字 / 没群 / 重名的条目直接跳过，不让半条配置制造歧义
        seen.add(_norm(name))
        aliases = [str(a).strip() for a in (it.get("aliases") or []) if str(a).strip()]
        others = [o for o in (it.get("other_chats") or []) if isinstance(o, dict)]
        out.append(Workspace(
            name=name,
            chat_id=chat_id,
            cwd=os.path.expanduser(str(it.get("cwd") or "").strip()),
            desc=str(it.get("desc") or "").strip(),
            chat_name=str(it.get("chat_name") or "").strip(),
            aliases=aliases,
            agent=str(it.get("agent") or "").strip(),
            other_chats=others,
        ))
    _CACHE[p] = (key, out)
    return out


# ── 解析 ──────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """归一化匹配键：小写 + 去掉连字符/下划线/空格/点。

    用户是**用嘴说**这些名字的（"cc lark" / "CC-Lark" / "cclark" 都是同一个），
    所以匹配必须对这些分隔符不敏感。中文原样保留。
    """
    return re.sub(r"[\s\-_.·]+", "", (s or "").strip().lower())


def resolve(spec: str, path: str = "") -> tuple[Optional[Workspace], str]:
    """把 `workspace` 参数解析成工作域。返回 (ws, err)，两者必有其一。

    匹配顺序：名字 → 别名 → chat_id 原文 → 工作目录（全路径或最后一段）。
    全部大小写不敏感、对 `-_ .` 空格不敏感。
    """
    spec = (spec or "").strip()
    if not spec:
        return None, "工作域名字不能为空"
    table = load(path)
    if not table:
        return None, (
            "本机没有配置工作域路由表（workspaces.json 不存在或为空），"
            "省略 workspace 参数即可把任务派在当前群。"
        )
    key = _norm(spec)
    for ws in table:
        if _norm(ws.name) == key:
            return ws, ""
    for ws in table:
        if any(_norm(a) == key for a in ws.aliases):
            return ws, ""
    for ws in table:
        if ws.chat_id.lower() == spec.strip().lower():
            return ws, ""
    for ws in table:
        if ws.cwd and (_norm(ws.cwd) == key or _norm(os.path.basename(ws.cwd)) == key):
            return ws, ""
    avail = "、".join(ws.name for ws in table)
    return None, (
        f"未知工作域 {spec!r}。可选：{avail}。"
        f"（省略 workspace = 把任务派在当前群）"
    )


def names(path: str = "") -> list[str]:
    return [ws.name for ws in load(path)]


def catalog_doc(path: str = "", *, indent: str = "  ") -> str:
    """生成给模型看的工作域清单（嵌进工具 description）。无配置时返回空串。"""
    table = load(path)
    if not table:
        return ""
    lines = [f"{indent}· {ws.label()}" for ws in table]
    return "\n".join(lines)
