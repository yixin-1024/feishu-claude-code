"""事件驱动的"自动派单扫描器" — 把源群对话喂给 Claude，命中即在派单群发顶楼。

工作流：
    1. observe(bot, event)       从 dispatcher.handle_message_async 入口同步调用，
                                  把源群消息塞到 bot_loop 跑 _observe_event_async
    2. _ingest(message)           按 reply_to / thread_id 聚成 Cluster
    3. _should_short_circuit      @owner + 动作词 → 立即触发 _schedule_judge
                                  否则按上下文挑 debounce 秒数，重置 deadline
    4. _tick_loop                 每 5s 扫所有 cluster.deadline 过期 → _schedule_judge
    5. _judge_and_dispatch        调 claude -p 跑 prompts/spx_inbox_judge.md，
                                  decode JSON → dispatch=true 时 bot.send_post 派单
    6. DM polling                 bot 物理上看不到 owner ↔ Boss 私聊；
                                  另起 _dm_poll_loop 用 user 身份 lark-cli 拉

长期记忆（~/.feishu-claude/spx_inbox/）：
    heuristics.md                 用户手写经验，prompt 每次读
    dispatched.jsonl              每次派单一行
    decisions_skipped.jsonl       judge 决定不派也记下来
    state.json                    cluster runtime + DM cursor（重启恢复）

设计原则：
    - **非破坏性 hook**：observe() 同步返回不阻塞 dispatcher 主路径
    - **失败容忍**：claude exec 挂了 / lark-cli 挂了 / json 解析失败 → 全部静默 log，不影响 cc-lark
    - **配置驱动**：所有 chat_id / open_id / 阈值 / 关键词 从 inbox_config.yaml 读
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import yaml

from bot_config import DEFAULT_MODEL
from bot_instance import BotInstance
from feishu_post import extract_post_image_keys, parse_post_content
from log_util import log


# ── 配置 dataclass ────────────────────────────────────────────

@dataclass
class SourceConfig:
    kind: str                   # "group" | "dm"
    chat_id: str = ""           # group 时填
    user_id: str = ""           # dm 时填（对方 open_id）
    name: str = ""


@dataclass
class InboxConfig:
    enabled: bool = False
    profile: str = "spx"
    claude_model: str = field(default_factory=lambda: DEFAULT_MODEL)
    dispatch_chat_id: str = ""
    owner_open_id: str = ""
    owner_name: str = ""
    workspace: str = ""

    sources: list[SourceConfig] = field(default_factory=list)

    require_mention_owner: bool = True
    action_keywords: list[str] = field(default_factory=list)

    debounce_dm_seconds: int = 60
    debounce_discussion_seconds: int = 180
    debounce_default_seconds: int = 90

    cooldown_minutes: int = 30
    dm_poll_interval_seconds: int = 30

    memory_dir: str = "~/.feishu-claude/spx_inbox"
    heuristics_max_chars: int = 8000
    dispatched_recent: int = 10
    feedback_recent: int = 20

    judge_timeout_seconds: int = 180

    # ─── auto_execute（机器人自己接手干）─────────────────────
    auto_execute_enabled: bool = True
    auto_execute_kinds: list[str] = field(default_factory=lambda: ["readonly"])
    auto_execute_min_confidence: float = 0.8
    auto_execute_quota_per_hour: int = 5
    auto_execute_quota_per_day: int = 30

    # ─── case session 跟踪 ──────────────────────────────────
    case_session_enabled: bool = True
    case_thread_ttl_days: int = 30
    case_thread_max_entries: int = 200

    # ─── P1: feedback 闭环 ──────────────────────────────────
    feedback_enabled: bool = True
    feedback_scan_interval_seconds: int = 3600
    feedback_min_age_hours: int = 4
    feedback_max_age_days: int = 7

    # ─── P2: 源群就地处理 ─────────────────────────────────────
    source_inline_enabled: bool = False        # 默认关，跑稳前手动开
    source_inline_whitelist: list[str] = field(default_factory=list)
    source_inline_min_confidence: float = 0.85

    @classmethod
    def load(cls, path: str) -> "InboxConfig":
        with open(path, "r", encoding="utf-8") as f:
            raw_text = f.read()
        raw_text = os.path.expandvars(raw_text)
        raw = yaml.safe_load(raw_text) or {}

        srcs = []
        for s in raw.get("sources", []) or []:
            srcs.append(SourceConfig(
                kind=str(s.get("kind", "")).strip(),
                chat_id=str(s.get("chat_id", "")).strip(),
                user_id=str(s.get("user_id", "")).strip(),
                name=str(s.get("name", "")).strip(),
            ))

        sc = raw.get("short_circuit", {}) or {}
        db = raw.get("debounce", {}) or {}
        mem = raw.get("memory", {}) or {}
        ae = raw.get("auto_execute", {}) or {}
        cs = raw.get("case_session", {}) or {}
        fb = raw.get("feedback", {}) or {}
        si = raw.get("source_inline", {}) or {}

        owner_id = str(raw.get("owner_open_id", "")).strip()
        # 兼容 .env 里 SPX_ALLOWED_OPEN_IDS 是逗号分隔 — 取第一个
        if "," in owner_id:
            owner_id = owner_id.split(",", 1)[0].strip()

        return cls(
            enabled=bool(raw.get("enabled", False)),
            profile=str(raw.get("profile", "spx")).strip(),
            claude_model=str(raw.get("claude_model", DEFAULT_MODEL)).strip(),
            dispatch_chat_id=str(raw.get("dispatch_chat_id", "")).strip(),
            owner_open_id=owner_id,
            owner_name=str(raw.get("owner_name", "")).strip(),
            workspace=str(raw.get("workspace", "")).strip(),
            sources=srcs,
            require_mention_owner=bool(sc.get("require_mention_owner", True)),
            action_keywords=[str(k).strip() for k in sc.get("action_keywords", []) if str(k).strip()],
            debounce_dm_seconds=int(db.get("dm_seconds", 60)),
            debounce_discussion_seconds=int(db.get("discussion_seconds", 180)),
            debounce_default_seconds=int(db.get("default_seconds", 90)),
            cooldown_minutes=int(raw.get("cooldown_minutes", 30)),
            dm_poll_interval_seconds=int(raw.get("dm_poll_interval_seconds", 30)),
            memory_dir=str(raw.get("memory_dir", "~/.feishu-claude/spx_inbox")),
            heuristics_max_chars=int(mem.get("heuristics_max_chars", 8000)),
            dispatched_recent=int(mem.get("dispatched_recent", 10)),
            feedback_recent=int(mem.get("feedback_recent", 20)),
            judge_timeout_seconds=int(raw.get("judge_timeout_seconds", 180)),
            auto_execute_enabled=bool(ae.get("enabled", True)),
            auto_execute_kinds=[str(k).strip() for k in (ae.get("kinds") or ["readonly"]) if str(k).strip()],
            auto_execute_min_confidence=float(ae.get("min_confidence", 0.8)),
            auto_execute_quota_per_hour=int(ae.get("quota_per_hour", 5)),
            auto_execute_quota_per_day=int(ae.get("quota_per_day", 30)),
            case_session_enabled=bool(cs.get("enabled", True)),
            case_thread_ttl_days=int(cs.get("ttl_days", 30)),
            case_thread_max_entries=int(cs.get("max_entries", 200)),
            feedback_enabled=bool(fb.get("enabled", True)),
            feedback_scan_interval_seconds=int(fb.get("scan_interval_seconds", 3600)),
            feedback_min_age_hours=int(fb.get("min_age_hours", 4)),
            feedback_max_age_days=int(fb.get("max_age_days", 7)),
            source_inline_enabled=bool(si.get("enabled", False)),
            source_inline_whitelist=[str(x).strip() for x in (si.get("whitelist") or []) if str(x).strip()],
            source_inline_min_confidence=float(si.get("min_confidence", 0.85)),
        )


# ── 数据类型 ─────────────────────────────────────────────────

@dataclass
class Attachment:
    """从消息里下载到本地的附件（图片/文件/post 内嵌图）。"""
    kind: str          # image | file | image_post
    path: str          # 本地绝对路径（下载成功）或空（下载失败）
    name: str = ""     # 原始文件名（image 没有）
    error: str = ""    # 下载失败原因


@dataclass
class Message:
    message_id: str
    sender_open_id: str
    sender_name: str
    text: str
    create_time: float
    reply_to: str = ""
    raw_chat_id: str = ""
    thread_id: str = ""
    mentions: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class Cluster:
    key: str
    messages: list[Message] = field(default_factory=list)
    deadline: float = 0.0           # debounce 到点 timestamp（0 = 不调度 / 已调度）
    last_dispatched_at: float = 0.0
    dispatched_msg_id: str = ""
    case_key: str = ""              # judge 抽出的业务键（邮箱 sha1 / 订单号 等）
    inflight: bool = False          # 防止同 cluster 并发 judge


@dataclass
class CaseThread:
    """case_key → dispatch 群里这个 case 的首条顶楼 msg_id（thread anchor）。

    target_chat_id 区分中央派单群 vs 源群就地（P2）—— 同一 case 一旦定了 target
    就一直在那里 follow-up，避免跨群跳。
    """
    case_key: str
    anchor_msg_id: str
    first_dispatched_at: float
    last_touched_at: float
    target_chat_id: str = ""    # 派单去向（dispatch_chat_id 或源群 chat_id）
    inline: bool = False        # True = 派到源群（P2），False = 派到中央派单群
    history: list[dict] = field(default_factory=list)  # [{ts, title, body, action_prompt?}]


# ── 全局状态 ─────────────────────────────────────────────────

_config: Optional[InboxConfig] = None
_bot: Optional[BotInstance] = None
_bot_loop: Optional[asyncio.AbstractEventLoop] = None
_state_lock: Optional[asyncio.Lock] = None
_clusters: dict[str, Cluster] = {}
_dm_cursor: dict[str, float] = {}
_case_threads: dict[str, CaseThread] = {}     # case_key → CaseThread
_auto_exec_log: list[float] = []              # 自动执行的 timestamp 列表（quota 用）
_feedback_done: set[str] = set()              # 已标注的 dispatched_msg_id
_tick_task: Optional[asyncio.Task] = None
_dm_poll_task: Optional[asyncio.Task] = None
_feedback_task: Optional[asyncio.Task] = None


def _memory_path(filename: str) -> Path:
    base = Path(os.path.expanduser(_config.memory_dir))
    base.mkdir(parents=True, exist_ok=True)
    return base / filename


# ── 启动 ─────────────────────────────────────────────────────

def start(
    config_path: str,
    bots: dict[str, BotInstance],
    bot_loop: asyncio.AbstractEventLoop,
) -> None:
    """main.py 启动时调一次。

    不存在配置文件 / enabled=false / profile 未加载 → log + return（不抛）。
    成功启动后由 dispatcher.handle_message_async 调 observe() 喂消息。
    """
    global _config, _bot, _bot_loop

    if not os.path.exists(config_path):
        log("global", "inbox", "info",
            f"未找到 {os.path.basename(config_path)}，inbox_watcher 跳过")
        return

    try:
        _config = InboxConfig.load(config_path)
    except Exception as e:
        log("global", "inbox", "error",
            f"配置加载失败 {config_path}: {type(e).__name__}: {e}")
        return

    if not _config.enabled:
        log("global", "inbox", "info", "inbox_watcher 关闭 (enabled=false)")
        return

    if not _config.dispatch_chat_id:
        log("global", "inbox", "warn", "dispatch_chat_id 为空，禁用 inbox")
        _config = None
        return

    if not _config.owner_open_id:
        log("global", "inbox", "warn", "owner_open_id 为空，禁用 inbox（短路/过滤都依赖它）")
        _config = None
        return

    bot = bots.get(_config.profile)
    if bot is None:
        log("global", "inbox", "warn",
            f"profile={_config.profile!r} 未加载，inbox_watcher 跳过")
        _config = None
        return

    _bot = bot
    _bot_loop = bot_loop

    _load_state()

    # 把 _state_lock + 后台 task 投到 bot_loop
    asyncio.run_coroutine_threadsafe(_bootstrap_background(), bot_loop)

    log("global", "inbox", "info",
        f"✅ 已启动 profile={_config.profile} dispatch={_config.dispatch_chat_id[:14]}... "
        f"sources={len(_config.sources)} model={_config.claude_model}")


async def _bootstrap_background():
    global _state_lock, _tick_task, _dm_poll_task, _feedback_task
    _state_lock = asyncio.Lock()
    _tick_task = asyncio.create_task(_tick_loop())
    if any(s.kind == "dm" for s in _config.sources):
        _dm_poll_task = asyncio.create_task(_dm_poll_loop())
    if _config.feedback_enabled:
        _feedback_task = asyncio.create_task(_feedback_loop())


# ── 公开 hook：dispatcher.handle_message_async 同步调 ─────────

def observe(bot: BotInstance, event) -> None:
    """从 dispatcher 入口被同步调用 — 投到 bot_loop，不阻塞主路径。

    安全保证：
        - _config / _bot 未初始化 → 静默 return
        - profile 不匹配 → 静默 return
        - 任何异常都不抛回 dispatcher
    """
    if _config is None or _bot is None or _bot_loop is None:
        return
    if bot.profile.name != _config.profile:
        return
    try:
        asyncio.run_coroutine_threadsafe(_observe_event_async(event), _bot_loop)
    except Exception as e:
        log("global", "inbox", "warn", f"observe submit 失败: {e}")


async def _observe_event_async(event) -> None:
    try:
        msg = event.event.message
        sender = event.event.sender

        raw_chat_id = getattr(msg, "chat_id", "") or ""
        sender_open_id = getattr(getattr(sender, "sender_id", None), "open_id", "") or ""
        chat_type = getattr(msg, "chat_type", "") or ""

        # ─── 过滤：必须命中某个 source ─────────────────────
        matched_src: Optional[SourceConfig] = None
        for src in _config.sources:
            if src.kind == "group" and src.chat_id == raw_chat_id:
                matched_src = src
                break
            if src.kind == "dm" and chat_type == "p2p" and src.user_id == sender_open_id:
                matched_src = src
                break
        if matched_src is None:
            return

        # 跳过 owner 自己发的（避免自循环）+ 跳过派单群（dispatch_chat_id 自己）
        if sender_open_id == _config.owner_open_id:
            return
        if raw_chat_id == _config.dispatch_chat_id:
            return

        text, attachments = await _extract_text_and_attachments(msg)
        if not text and not attachments:
            return

        message_id = getattr(msg, "message_id", "") or ""
        thread_id = getattr(msg, "thread_id", "") or ""
        reply_to = getattr(msg, "parent_id", "") or ""
        mentions = []
        for m in (getattr(msg, "mentions", None) or []):
            mid = getattr(getattr(m, "id", None), "open_id", "") or ""
            if mid:
                mentions.append(mid)

        wrapped = Message(
            message_id=message_id,
            sender_open_id=sender_open_id,
            sender_name=getattr(sender, "name", "") or "",
            text=text[:4000],
            create_time=time.time(),
            reply_to=reply_to,
            raw_chat_id=raw_chat_id,
            thread_id=thread_id,
            mentions=mentions,
            attachments=attachments,
        )
        await _ingest(wrapped, matched_src)
    except Exception as e:
        log("global", "inbox", "error",
            f"observe_async 异常: {type(e).__name__}: {e}")
        traceback.print_exc()


async def _extract_text_and_attachments(msg) -> tuple[str, list[Attachment]]:
    """从 lark message 提取可读文本 + 下载附件到本地。

    text → 直接返回
    image → 下载，返回 "[图片: /tmp/xxx]" + Attachment(kind=image, path=...)
    file → 下载，返回 "[文件 name: /tmp/xxx]" + Attachment(kind=file, ...)
    post → 解析正文，递归下载内嵌图，文末附 "[内嵌图片: <paths>]"

    下载失败：用 "[image: download failed: <err>]" 占位，**不阻断 ingest**——
    judge prompt 看不到图片就照文本判定，比直接静默丢消息好。
    """
    mt = getattr(msg, "message_type", "")
    content_raw = getattr(msg, "content", "") or ""
    if not content_raw:
        return "", []
    try:
        c = json.loads(content_raw)
    except Exception:
        return "", []

    atts: list[Attachment] = []

    if mt == "text":
        return (c.get("text") or "").strip(), []

    if mt == "image":
        image_key = (c.get("image_key") or "").strip()
        if not image_key:
            return "[image]", []
        try:
            path = await _bot.feishu.download_image(msg.message_id, image_key)
            atts.append(Attachment(kind="image", path=path))
            return f"[图片: {path}]", atts
        except Exception as e:
            atts.append(Attachment(kind="image", path="", error=str(e)))
            return f"[image: download failed: {type(e).__name__}]", atts

    if mt == "file":
        file_key = (c.get("file_key") or "").strip()
        file_name = (c.get("file_name") or "file").strip()
        if not file_key:
            return f"[file: {file_name}]", []
        try:
            path = await _bot.feishu.download_file(
                msg.message_id, file_key, msg_type="file", file_name=file_name,
            )
            atts.append(Attachment(kind="file", path=path, name=file_name))
            return f"[文件 {file_name}: {path}]", atts
        except Exception as e:
            atts.append(Attachment(kind="file", path="", name=file_name, error=str(e)))
            return f"[file {file_name}: download failed: {type(e).__name__}]", atts

    if mt == "post":
        text = parse_post_content(content_raw).strip()
        img_keys = extract_post_image_keys(content_raw)
        img_paths: list[str] = []
        for k in img_keys:
            try:
                path = await _bot.feishu.download_image(msg.message_id, k)
                atts.append(Attachment(kind="image_post", path=path))
                img_paths.append(path)
            except Exception as e:
                atts.append(Attachment(kind="image_post", path="", error=str(e)))
        if img_paths:
            text = f"{text}\n[内嵌图片: {', '.join(img_paths)}]"
        return text, atts

    return "", []


# ── Case Key 抽取（硬规则层）──────────────────────────────
# 思路：从一段对话里抽出"稳定业务键"。同一个 case 的所有后续 follow-up
# 都应该映射到同一个 key，让派单 reply 到老 thread 而不是开新 thread。
#
# 硬规则覆盖 80% 场景（邮箱列表、工单号、订单号、卡号末四位、UID）；剩下的
# judge prompt 里让 claude 自己抽一个语义键（写进 case_key_hint，硬规则未
# 命中时回落到它）。

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_TICKET_RE = re.compile(r"\b(?:KYC|KYB|TKT|TICKET|工单)[0O]*\d{3,}\b", re.I)
_USER_ID_RE = re.compile(r"\buser[_-]?id[\s=:]*?(\d{5,})", re.I)
_CARD_LAST4_RE = re.compile(r"卡[尾末后]四位[\s:：]*?(\d{4})|末四位[\s:：]*?(\d{4})|卡号[尾末后]?[\s:：]*?\*+(\d{4})")


def _case_key_from_messages(messages: list["Message"]) -> str:
    """硬规则抽取 case_key（未命中返回 ""，留给 judge 自己抽）。

    优先级：邮箱集合 > 工单号 > user_id > 卡号末四位
    """
    blob = "\n".join(m.text for m in messages[-20:])

    emails = sorted({e.lower() for e in _EMAIL_RE.findall(blob)})
    if emails:
        h = hashlib.sha1(",".join(emails).encode()).hexdigest()[:10]
        return f"emails:{h}"

    tickets = sorted({t.upper() for t in _TICKET_RE.findall(blob)})
    if tickets:
        return f"ticket:{tickets[0]}"

    for m in _USER_ID_RE.finditer(blob):
        uid = m.group(1)
        return f"uid:{uid}"

    for m in _CARD_LAST4_RE.finditer(blob):
        last4 = next((g for g in m.groups() if g), None)
        if last4:
            return f"card:{last4}"

    return ""


def _normalize_case_key(raw: str) -> str:
    """把 claude 抽的 case_key 归一化：去掉空格 / 全角符号 / 截断 / 转小写前缀。

    judge 返回的 case_key 可能形如 "ticket KYC00123" / "用户邮箱组" / "" 等，
    需要清洗一下避免重复 key。
    """
    if not raw:
        return ""
    s = raw.strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^a-z0-9_:.\-]", "", s)
    return s[:80]


# ── Case Thread 表（LRU + TTL）────────────────────────────

def _case_thread_get(case_key: str) -> Optional[CaseThread]:
    if not case_key or not _config.case_session_enabled:
        return None
    ct = _case_threads.get(case_key)
    if ct is None:
        return None
    ttl = _config.case_thread_ttl_days * 86400
    if time.time() - ct.last_touched_at > ttl:
        # 过期 → 删除并返回 None（caller 会重新派一条新 anchor）
        _case_threads.pop(case_key, None)
        return None
    return ct


def _case_thread_upsert(
    case_key: str,
    anchor_msg_id: str,
    title: str,
    body: str,
    action_prompt: str = "",
    target_chat_id: str = "",
    inline: bool = False,
) -> None:
    if not case_key:
        return
    now = time.time()
    ct = _case_threads.get(case_key)
    if ct is None:
        ct = CaseThread(
            case_key=case_key,
            anchor_msg_id=anchor_msg_id,
            first_dispatched_at=now,
            last_touched_at=now,
            target_chat_id=target_chat_id,
            inline=inline,
        )
        _case_threads[case_key] = ct
    else:
        ct.last_touched_at = now
        # 不覆盖 anchor_msg_id 和 inline —— follow-up 必须跟首发路由保持一致
    ct.history.append({
        "ts": now,
        "title": title,
        "body": body[:300],
        "action_prompt": action_prompt[:300] if action_prompt else "",
    })
    ct.history = ct.history[-20:]
    _case_thread_evict()


def _case_thread_evict() -> None:
    """LRU 淘汰 + TTL 清扫。要求持 _state_lock。"""
    if not _config or not _config.case_session_enabled:
        return
    ttl = _config.case_thread_ttl_days * 86400
    now = time.time()
    # TTL
    expired = [k for k, ct in _case_threads.items() if now - ct.last_touched_at > ttl]
    for k in expired:
        _case_threads.pop(k, None)
    # LRU
    if len(_case_threads) > _config.case_thread_max_entries:
        sorted_keys = sorted(_case_threads.items(), key=lambda kv: kv[1].last_touched_at)
        n_remove = len(_case_threads) - _config.case_thread_max_entries
        for k, _ in sorted_keys[:n_remove]:
            _case_threads.pop(k, None)


def _case_history_text(case_key: str) -> str:
    """给 judge prompt 看的"该 case 历史派单"。"""
    ct = _case_threads.get(case_key)
    if not ct:
        return ""
    lines = []
    for h in ct.history[-8:]:
        ts = time.strftime("%m-%d %H:%M", time.localtime(h.get("ts", 0)))
        title = h.get("title", "")
        body = (h.get("body", "") or "").replace("\n", " ")[:200]
        ap = h.get("action_prompt", "")
        line = f"- [{ts}] {title}\n    body: {body}"
        if ap:
            line += f"\n    action: {ap[:150]}"
        lines.append(line)
    return "\n".join(lines)


# ── Auto Execute：用 owner 身份发触发消息 ────────────────
# bot 自己用 _bot.feishu.reply_text 发的 @bot 消息会被 dispatcher ACL（owner
# allowlist）拦截 —— sender=app 不在 allowed_open_ids。要触发 dispatcher 正常
# 执行路径，必须让消息看起来是 owner 发的。
# 方案：subprocess 调 `lark-cli --as user im +messages-reply`，sender 自然是 owner。

async def _send_trigger_as_owner(
    target_msg_id: str, action_prompt: str, in_thread: bool, bot_open_id: str,
) -> None:
    """用 owner 身份在 target_msg_id 下 reply 一条 @bot <action_prompt>。

    抛异常由 caller 兜底（caller 把错误写进 auto_exec_reject）。
    """
    cli_profile = _bot.profile.lark_cli_profile or _config.profile
    mention = f"<at user_id=\"{bot_open_id}\"></at> " if bot_open_id else ""
    cmd = [
        "lark-cli", "--profile", cli_profile,
        "im", "+messages-reply",
        "--as", "user",
        "--message-id", target_msg_id,
        "--text", f"{mention}{action_prompt}",
    ]
    if in_thread:
        cmd.append("--reply-in-thread")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("lark-cli messages-reply 超时")
    if proc.returncode != 0:
        raise RuntimeError(
            f"lark-cli rc={proc.returncode} err={stderr.decode('utf-8', errors='replace')[:300]}"
        )


# ── Auto Execute Quota ────────────────────────────────────

def _auto_exec_quota_ok() -> tuple[bool, str]:
    """返回 (允许执行?, 拒绝原因)。"""
    now = time.time()
    # 清掉 24h 之前的旧记录
    cutoff_day = now - 86400
    global _auto_exec_log
    _auto_exec_log = [t for t in _auto_exec_log if t > cutoff_day]

    last_day = len(_auto_exec_log)
    last_hour = sum(1 for t in _auto_exec_log if t > now - 3600)

    if last_hour >= _config.auto_execute_quota_per_hour:
        return False, f"小时配额 {_config.auto_execute_quota_per_hour} 已满（最近 1h 已执行 {last_hour} 次）"
    if last_day >= _config.auto_execute_quota_per_day:
        return False, f"日配额 {_config.auto_execute_quota_per_day} 已满（最近 24h 已执行 {last_day} 次）"
    return True, ""


def _auto_exec_record() -> None:
    _auto_exec_log.append(time.time())


# ── Cluster 聚类 ─────────────────────────────────────────────

def _cluster_key(m: Message) -> str:
    """thread_id 优先 → reply_to → message_id 自己作根。

    精确追溯 reply_to 链需要查每条消息的 parent，open API 不便宜。简化为
    "reply_to 即根 key"——同一根的所有 reply 自然聚到同一 key，足够好用。
    """
    if m.thread_id:
        return f"{m.raw_chat_id}:{m.thread_id}"
    if m.reply_to:
        return f"{m.raw_chat_id}:{m.reply_to}"
    return f"{m.raw_chat_id}:{m.message_id}"


def _should_short_circuit(m: Message) -> bool:
    if _config.require_mention_owner and _config.owner_open_id not in m.mentions:
        return False
    if not _config.action_keywords:
        return True
    return any(kw in m.text for kw in _config.action_keywords)


def _debounce_seconds_for(c: Cluster, src: SourceConfig) -> int:
    if src.kind == "dm":
        return _config.debounce_dm_seconds
    now = time.time()
    recent_senders = {x.sender_open_id for x in c.messages if now - x.create_time < 60}
    if len(recent_senders) >= 2:
        return _config.debounce_discussion_seconds
    return _config.debounce_default_seconds


async def _ingest(m: Message, src: SourceConfig) -> None:
    async with _state_lock:
        key = _cluster_key(m)
        c = _clusters.get(key)
        if c is None:
            c = Cluster(key=key)
            _clusters[key] = c
        c.messages.append(m)
        if len(c.messages) > 50:
            c.messages = c.messages[-50:]

        # 冷却期：派过单了，新消息只归簇不重派
        if c.last_dispatched_at:
            cooldown_until = c.last_dispatched_at + _config.cooldown_minutes * 60
            if time.time() < cooldown_until:
                _persist_state_unlocked()
                return

        if _should_short_circuit(m):
            log("global", "inbox", "info",
                f"短路触发 cluster={key[:32]} (@owner + 动作词)")
            c.deadline = 0
            _persist_state_unlocked()
            schedule = True
        else:
            c.deadline = time.time() + _debounce_seconds_for(c, src)
            _persist_state_unlocked()
            schedule = False

    if schedule:
        await _schedule_judge(c, reason="immediate")


# ── Tick：扫到期 cluster ─────────────────────────────────────

async def _tick_loop():
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()
            ripe: list[Cluster] = []
            async with _state_lock:
                for c in list(_clusters.values()):
                    if c.inflight or c.last_dispatched_at:
                        continue
                    if c.deadline and now >= c.deadline:
                        ripe.append(c)
                        c.deadline = 0
            for c in ripe:
                await _schedule_judge(c, reason="debounce")
        except asyncio.CancelledError:
            return
        except Exception as e:
            log("global", "inbox", "error", f"tick 异常: {type(e).__name__}: {e}")
            traceback.print_exc()


# ── DM 轮询：owner ↔ Boss 私聊（bot 看不到的部分）─────────

async def _dm_poll_loop():
    while True:
        try:
            for src in _config.sources:
                if src.kind != "dm":
                    continue
                try:
                    await _poll_dm_once(src)
                except Exception as e:
                    log("global", "inbox", "warn",
                        f"poll dm {src.user_id[:14]}... 失败: {type(e).__name__}: {e}")
            await asyncio.sleep(_config.dm_poll_interval_seconds)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log("global", "inbox", "error", f"dm-poll-loop 异常: {type(e).__name__}: {e}")
            traceback.print_exc()
            await asyncio.sleep(_config.dm_poll_interval_seconds)


async def _poll_dm_once(src: SourceConfig):
    """用 user 身份 lark-cli 拉 owner ↔ Boss 私聊。

    首次启动（cursor 未持久化过）→ 把 cursor 设为 "now"，不拉历史。
    历史消息已经过去了，再灌进 inbox 只会触发批量误判 + 烧 token。
    """
    cli_profile = _bot.profile.lark_cli_profile or _config.profile

    if src.user_id not in _dm_cursor:
        _dm_cursor[src.user_id] = time.time()
        async with _state_lock:
            _persist_state_unlocked()
        log("global", "inbox", "info",
            f"dm-poll 首次启动 user={src.user_id[:14]}... cursor 设为现在，不拉历史")
        return

    last_seen = _dm_cursor[src.user_id]

    cmd = [
        "lark-cli", "--profile", cli_profile,
        "im", "+chat-messages-list",
        "--as", "user",
        "--user-id", src.user_id,
        "--sort", "asc",
        "--page-size", "30",
        "--format", "json",
        "--start", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_seen)),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        raise

    if proc.returncode != 0:
        raise RuntimeError(f"lark-cli rc={proc.returncode} err={stderr.decode()[:200]}")

    try:
        data = json.loads(stdout)
    except Exception:
        return
    msgs = ((data.get("data") or {}).get("messages") or [])

    # 业务关心的 msg_type；其余（video_chat / sticker / share_user / system 等）跳过
    _DM_OK_TYPES = {"text", "post", "image", "file"}

    max_ts = last_seen
    new_msgs: list[tuple[float, dict]] = []
    for raw in msgs:
        try:
            ct = time.mktime(time.strptime(raw.get("create_time", ""), "%Y-%m-%d %H:%M"))
        except Exception:
            ct = 0
        if ct <= last_seen:
            continue
        sender_id = (raw.get("sender") or {}).get("id", "")
        if sender_id != src.user_id:
            # 只关心对方发的；自己发的（owner 自己）不算源
            continue
        if raw.get("msg_type") not in _DM_OK_TYPES:
            continue
        new_msgs.append((ct, raw))
        if ct > max_ts:
            max_ts = ct

    for ct, raw in new_msgs:
        wrapped = Message(
            message_id=raw.get("message_id", ""),
            sender_open_id=src.user_id,
            sender_name=(raw.get("sender") or {}).get("name", "") or src.name,
            text=str(raw.get("content", ""))[:2000],
            create_time=ct,
            reply_to=raw.get("reply_to", "") or "",
            raw_chat_id=f"p2p:{src.user_id}",
            thread_id="",
            mentions=[],   # DM 文本里没 mentions 解析；动作词靠 short_circuit 关键词命中
        )
        # DM 没有 @owner，所以短路只看动作词
        await _ingest_dm(wrapped, src)

    if max_ts > last_seen:
        _dm_cursor[src.user_id] = max_ts
        async with _state_lock:
            _persist_state_unlocked()


async def _ingest_dm(m: Message, src: SourceConfig) -> None:
    """DM 入口：跟 _ingest 同样路径，但短路判定不要求 @owner（私聊里默认就是说给你听）。"""
    async with _state_lock:
        key = _cluster_key(m)
        c = _clusters.get(key)
        if c is None:
            c = Cluster(key=key)
            _clusters[key] = c
        c.messages.append(m)
        if len(c.messages) > 50:
            c.messages = c.messages[-50:]

        if c.last_dispatched_at:
            cooldown_until = c.last_dispatched_at + _config.cooldown_minutes * 60
            if time.time() < cooldown_until:
                _persist_state_unlocked()
                return

        # DM 短路：含动作词即触发（不要求 @owner）
        hit = bool(_config.action_keywords) and any(kw in m.text for kw in _config.action_keywords)
        if hit:
            c.deadline = 0
            _persist_state_unlocked()
            schedule = True
        else:
            c.deadline = time.time() + _config.debounce_dm_seconds
            _persist_state_unlocked()
            schedule = False

    if schedule:
        await _schedule_judge(c, reason="immediate_dm")


# ── Judge + Dispatch ────────────────────────────────────────

async def _schedule_judge(c: Cluster, reason: str):
    async with _state_lock:
        if c.inflight or c.last_dispatched_at:
            return
        c.inflight = True
    asyncio.create_task(_judge_and_dispatch(c, reason))


async def _judge_and_dispatch(c: Cluster, reason: str):
    try:
        result = await _claude_judge(c, reason)
        await _record_decision(c, result, dispatched=False)   # 总是记一条 decision

        if not result.get("dispatch"):
            log("global", "inbox", "info",
                f"⏭  不派单 cluster={c.key[:32]} reason={str(result.get('reasoning',''))[:100]}")
            return

        title = (result.get("title") or "📥 待办").strip()[:80]
        body = (result.get("body") or "").strip()
        if not body:
            log("global", "inbox", "warn",
                f"judge 返回 dispatch=true 但 body 空 cluster={c.key[:32]}，跳过")
            return

        # ── case_key：硬规则优先 → 否则 judge 自抽 ───────────
        case_key = _case_key_from_messages(c.messages)
        if not case_key:
            case_key = _normalize_case_key(result.get("case_key", "") or "")

        # ── 决策：派到中央 dispatch_chat_id 还是源群就地 reply？─────
        # 规则（保守，且 case_thread 已锁死了的就跟它走，不跨群跳）：
        #   1. case_thread 命中 → 沿用它原本的 target_chat_id（中央 or 源群）
        #   2. 否则看 source_inline_enabled + 源群在白名单 + judge 请求 prefer_inline
        #      + readonly + confidence ≥ source_inline_min_confidence → 源群就地
        #   3. 不满足 → 中央派单群
        existing = _case_thread_get(case_key) if case_key else None
        source_chat_id = c.messages[-1].raw_chat_id if c.messages else ""
        source_anchor_msg_id = c.messages[-1].message_id if c.messages else ""

        inline_route = False
        target_chat_id = _config.dispatch_chat_id

        if existing:
            inline_route = existing.inline
            target_chat_id = existing.target_chat_id or _config.dispatch_chat_id
        else:
            prefer_inline = bool(result.get("prefer_inline"))
            kind = (result.get("execute_kind") or "").strip().lower()
            ec = float(result.get("execute_confidence") or 0)
            if (
                _config.source_inline_enabled
                and prefer_inline
                and source_chat_id in _config.source_inline_whitelist
                and kind == "readonly"
                and ec >= _config.source_inline_min_confidence
                and source_anchor_msg_id
            ):
                inline_route = True
                target_chat_id = source_chat_id

        # 派单：case_thread 命中老 anchor → reply；否则：
        #   inline=True → reply_post 到源消息（源群 inline）
        #   inline=False → send_post_to_chat 到中央派单群
        if existing:
            msg_id = await _bot.feishu.reply_post(
                message_id=existing.anchor_msg_id,
                title=title,
                body_text=body,
            )
            dispatch_mode = f"case_reply→{existing.anchor_msg_id[:14]}...(inline={inline_route})"
        elif inline_route:
            msg_id = await _bot.feishu.reply_post(
                message_id=source_anchor_msg_id,
                title=title,
                body_text=body,
            )
            dispatch_mode = f"source_inline→{source_chat_id[:14]}..."
        else:
            msg_id = await _bot.feishu.send_post_to_chat(
                chat_id=_config.dispatch_chat_id,
                title=title,
                body_text=body,
                mention_open_id="",   # 不 @ 任何人
            )
            dispatch_mode = "new_thread"

        # ── auto_execute 判定 ───────────────────────────────
        auto_exec_kicked = False
        auto_exec_reject = ""
        action_prompt = (result.get("action_prompt") or "").strip()
        if _config.auto_execute_enabled and action_prompt:
            kind = (result.get("execute_kind") or "").strip().lower()
            conf = float(result.get("execute_confidence") or 0)
            want_auto = bool(result.get("auto_execute"))

            if not want_auto:
                auto_exec_reject = "judge 未请求自动执行"
            elif kind not in _config.auto_execute_kinds:
                auto_exec_reject = f"kind={kind!r} 不在 auto_execute_kinds={_config.auto_execute_kinds}"
            elif conf < _config.auto_execute_min_confidence:
                auto_exec_reject = f"confidence={conf:.2f} < min={_config.auto_execute_min_confidence}"
            else:
                ok, why = _auto_exec_quota_ok()
                if not ok:
                    auto_exec_reject = why
                else:
                    try:
                        bot_open_id = await _bot.feishu.get_bot_open_id() or ""
                        # 关键：必须用 owner 身份发，bot 自己发的会被 dispatcher ACL 拦
                        # in_thread=True：话题群里 reply-in-thread 才不会破坏 thread 结构
                        await _send_trigger_as_owner(
                            target_msg_id=msg_id,
                            action_prompt=action_prompt,
                            in_thread=True,
                            bot_open_id=bot_open_id,
                        )
                        _auto_exec_record()
                        auto_exec_kicked = True
                    except Exception as e:
                        auto_exec_reject = f"trigger 失败: {type(e).__name__}: {e}"

        async with _state_lock:
            c.last_dispatched_at = time.time()
            c.dispatched_msg_id = msg_id
            c.case_key = case_key
            if case_key:
                anchor = existing.anchor_msg_id if existing else msg_id
                _case_thread_upsert(
                    case_key,
                    anchor,
                    title,
                    body,
                    action_prompt,
                    target_chat_id=target_chat_id,
                    inline=inline_route,
                )
            _persist_state_unlocked()

        log("global", "inbox", "info",
            f"✅ 派单 cluster={c.key[:32]} → {msg_id[:14]}... mode={dispatch_mode} "
            f"target={target_chat_id[:14]}... case_key={case_key or '(无)'} title={title[:40]} "
            f"auto_exec={'YES' if auto_exec_kicked else f'NO({auto_exec_reject})' if auto_exec_reject else 'N/A'}")

        # 在 decision 里记下 auto_exec 结果，方便复盘
        result["_auto_exec_kicked"] = auto_exec_kicked
        result["_auto_exec_reject"] = auto_exec_reject
        result["_case_key_used"] = case_key
        result["_dispatch_mode"] = dispatch_mode
        await _record_decision(c, result, dispatched=True)
    except asyncio.TimeoutError:
        log("global", "inbox", "warn", f"judge 超时 cluster={c.key[:32]}")
    except Exception as e:
        log("global", "inbox", "error",
            f"judge/dispatch 异常 cluster={c.key[:32]}: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        async with _state_lock:
            c.inflight = False


async def _claude_judge(c: Cluster, reason: str) -> dict:
    """调 claude --print 跑 prompts/spx_inbox_judge.md，解析 JSON 输出。"""
    prompt_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "prompts", "spx_inbox_judge.md",
    )
    if not os.path.exists(prompt_path):
        return {"dispatch": False, "reasoning": f"prompt 文件不存在: {prompt_path}"}
    with open(prompt_path, "r", encoding="utf-8") as f:
        tpl = f.read()

    # 输入：对话清单（时间序）
    msgs_lines = []
    for m in c.messages:
        ts = time.strftime("%H:%M", time.localtime(m.create_time))
        line = f"- [{ts}] {m.sender_name}: {m.text}"
        if m.reply_to:
            line += f"\n  (↩ reply to {m.reply_to[:14]}...)"
        if m.mentions:
            line += f"\n  (@mentions: {', '.join(x[:14]+'...' for x in m.mentions)})"
        if m.attachments:
            atts_summary = []
            for a in m.attachments:
                if a.path:
                    atts_summary.append(f"{a.kind}: {a.path}{(' ('+a.name+')') if a.name else ''}")
                else:
                    atts_summary.append(f"{a.kind}: ⚠️ 下载失败 ({a.error[:60]})")
            line += "\n  附件: " + "; ".join(atts_summary)
        msgs_lines.append(line)
    messages_text = "\n".join(msgs_lines)

    heuristics = _read_optional(_memory_path("heuristics.md"))[: _config.heuristics_max_chars]
    dispatched_tail = _read_jsonl_tail(_memory_path("dispatched.jsonl"), _config.dispatched_recent)
    feedback_tail = _read_jsonl_tail(_memory_path("feedback.jsonl"), _config.feedback_recent)

    # case_key 硬规则提示 + 历史
    hard_case_key = _case_key_from_messages(c.messages)
    case_history = _case_history_text(hard_case_key) if hard_case_key else ""

    # 源群列表 + lark-cli profile（让 judge 在上下文不够时自己拉历史）
    src_lines = []
    for s in _config.sources:
        if s.kind == "group":
            src_lines.append(f"  - group {s.chat_id} ({s.name})")
        elif s.kind == "dm":
            src_lines.append(f"  - dm user {s.user_id} ({s.name})")
    sources_text = "\n".join(src_lines) or "(无)"
    lark_profile = _bot.profile.lark_cli_profile or _config.profile

    # 简单 placeholder 替换（不用 str.format 避免 {} 冲突）
    full_prompt = (
        tpl.replace("{{CLUSTER_KEY}}", c.key)
           .replace("{{TRIGGER_REASON}}", reason)
           .replace("{{OWNER_OPEN_ID}}", _config.owner_open_id)
           .replace("{{OWNER_NAME}}", _config.owner_name)
           .replace("{{DISPATCH_CHAT_ID}}", _config.dispatch_chat_id)
           .replace("{{MESSAGES}}", messages_text)
           .replace("{{HEURISTICS}}", heuristics or "(空 — 用户尚未写经验)")
           .replace("{{DISPATCHED_RECENT}}", dispatched_tail or "(无历史派单)")
           .replace("{{FEEDBACK_RECENT}}", feedback_tail or "(无反馈)")
           .replace("{{HARD_CASE_KEY}}", hard_case_key or "(硬规则未命中)")
           .replace("{{CASE_HISTORY}}", case_history or "(该 case 无历史派单)")
           .replace("{{SOURCES}}", sources_text)
           .replace("{{LARK_PROFILE}}", lark_profile)
           .replace("{{AUTO_EXECUTE_KINDS}}", ",".join(_config.auto_execute_kinds))
           .replace("{{AUTO_EXECUTE_MIN_CONFIDENCE}}", str(_config.auto_execute_min_confidence))
           .replace("{{SOURCE_INLINE_ENABLED}}", "true" if _config.source_inline_enabled else "false")
           .replace("{{SOURCE_INLINE_WHITELIST}}", ", ".join(_config.source_inline_whitelist) or "(空)")
           .replace("{{SOURCE_INLINE_MIN_CONFIDENCE}}", str(_config.source_inline_min_confidence))
    )

    cmd = [
        "claude", "-p", full_prompt,
        "--model", _config.claude_model,
        "--output-format", "stream-json",
        "--verbose",
    ]
    cwd = os.path.expanduser(_config.workspace) if _config.workspace else _bot.profile.default_cwd

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_config.judge_timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return {"dispatch": False, "reasoning": "claude judge 超时"}

    if proc.returncode != 0:
        return {
            "dispatch": False,
            "reasoning": f"claude rc={proc.returncode} err={stderr.decode()[:200]}",
        }

    # 解析 stream-json：取最后一条 type=result
    last_text = ""
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "result":
            last_text = obj.get("result", "") or ""

    if not last_text:
        return {"dispatch": False, "reasoning": "claude 无 result 输出"}

    return _extract_json(last_text)


def _extract_json(text: str) -> dict:
    """从 claude 文本输出里挖出 JSON dict。"""
    # 1) ```json``` 包
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 2) 第一个含 "dispatch" 的 {...}
    for m in re.finditer(r"\{[^{}]*\"dispatch\"[^{}]*\}", text, re.DOTALL):
        try:
            return json.loads(m.group(0))
        except Exception:
            continue
    # 3) 整体当 JSON 试一次
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    return {
        "dispatch": False,
        "reasoning": "claude 输出无法解析为 JSON",
        "_raw_preview": text[:300],
    }


# ── 持久化 / 记忆 ────────────────────────────────────────────

def _load_state():
    if _config is None:
        return
    p = _memory_path("state.json")
    if not p.exists():
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in (data.get("dm_cursor") or {}).items():
            try:
                _dm_cursor[k] = float(v)
            except Exception:
                pass
        for ck, cd in (data.get("clusters") or {}).items():
            c = Cluster(
                key=ck,
                deadline=float(cd.get("deadline") or 0),
                last_dispatched_at=float(cd.get("last_dispatched_at") or 0),
                dispatched_msg_id=str(cd.get("dispatched_msg_id") or ""),
                case_key=str(cd.get("case_key") or ""),
            )
            for mm in (cd.get("messages") or [])[-20:]:
                try:
                    atts = []
                    for ad in (mm.get("attachments") or []):
                        atts.append(Attachment(
                            kind=str(ad.get("kind", "")),
                            path=str(ad.get("path", "")),
                            name=str(ad.get("name", "")),
                            error=str(ad.get("error", "")),
                        ))
                    c.messages.append(Message(
                        message_id=str(mm.get("message_id", "")),
                        sender_open_id=str(mm.get("sender_open_id", "")),
                        sender_name=str(mm.get("sender_name", "")),
                        text=str(mm.get("text", "")),
                        create_time=float(mm.get("create_time") or 0),
                        reply_to=str(mm.get("reply_to", "")),
                        raw_chat_id=str(mm.get("raw_chat_id", "")),
                        thread_id=str(mm.get("thread_id", "")),
                        mentions=list(mm.get("mentions") or []),
                        attachments=atts,
                    ))
                except Exception:
                    pass
            _clusters[ck] = c
        for k, cd in (data.get("case_threads") or {}).items():
            try:
                _case_threads[k] = CaseThread(
                    case_key=k,
                    anchor_msg_id=str(cd.get("anchor_msg_id", "")),
                    first_dispatched_at=float(cd.get("first_dispatched_at") or 0),
                    last_touched_at=float(cd.get("last_touched_at") or 0),
                    target_chat_id=str(cd.get("target_chat_id", "")),
                    inline=bool(cd.get("inline", False)),
                    history=list(cd.get("history") or []),
                )
            except Exception:
                pass
        for t in (data.get("auto_exec_log") or []):
            try:
                _auto_exec_log.append(float(t))
            except Exception:
                pass
        for x in (data.get("feedback_done") or []):
            if isinstance(x, str) and x:
                _feedback_done.add(x)
        log("global", "inbox", "info",
            f"state.json 恢复 clusters={len(_clusters)} dm_cursor={len(_dm_cursor)} "
            f"case_threads={len(_case_threads)} auto_exec_log={len(_auto_exec_log)} "
            f"feedback_done={len(_feedback_done)}")
    except Exception as e:
        log("global", "inbox", "warn", f"state.json 恢复失败: {e}")


def _persist_state_unlocked():
    """要求调用方已持 _state_lock。"""
    if _config is None:
        return
    try:
        data = {
            "dm_cursor": dict(_dm_cursor),
            "clusters": {},
            "case_threads": {},
            "auto_exec_log": list(_auto_exec_log),
            "feedback_done": list(_feedback_done)[-2000:],   # cap 2000
        }
        for ck, c in _clusters.items():
            data["clusters"][ck] = {
                "deadline": c.deadline,
                "last_dispatched_at": c.last_dispatched_at,
                "dispatched_msg_id": c.dispatched_msg_id,
                "case_key": c.case_key,
                "messages": [asdict(m) for m in c.messages[-20:]],
            }
        for k, ct in _case_threads.items():
            data["case_threads"][k] = {
                "anchor_msg_id": ct.anchor_msg_id,
                "first_dispatched_at": ct.first_dispatched_at,
                "last_touched_at": ct.last_touched_at,
                "target_chat_id": ct.target_chat_id,
                "inline": ct.inline,
                "history": ct.history[-20:],
            }
        p = _memory_path("state.json")
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        log("global", "inbox", "warn", f"persist_state 失败: {e}")


# ── P1: feedback 闭环 ─────────────────────────────────────
# 每 1h 扫一遍 dispatched.jsonl 里 4h-7d 龄、还没标注过的派单，拉 thread
# 历史判定 engaged / missed / auto_executed_* → 写 feedback.jsonl。
# judge prompt 的 FEEDBACK_RECENT 自动消费这份反馈，让 inbox 误派/漏派
# 随时间自我校准。

async def _feedback_loop():
    """长跑 task：每 feedback_scan_interval_seconds 跑一次。"""
    # 启动延迟 5 分钟，让 bot 完全起来再扫
    await asyncio.sleep(300)
    while True:
        try:
            await _run_feedback_scan()
        except asyncio.CancelledError:
            return
        except Exception as e:
            log("global", "inbox", "error",
                f"feedback loop 异常: {type(e).__name__}: {e}")
            traceback.print_exc()
        await asyncio.sleep(_config.feedback_scan_interval_seconds)


async def _run_feedback_scan() -> None:
    if not _config.feedback_enabled:
        return
    p = _memory_path("dispatched.jsonl")
    if not p.exists():
        return

    now = time.time()
    min_age = _config.feedback_min_age_hours * 3600
    max_age = _config.feedback_max_age_days * 86400

    # 只看最近 500 条派单（更老的肯定已经被标或者过期）
    try:
        all_lines = p.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        log("global", "inbox", "warn", f"读 dispatched.jsonl 失败: {e}")
        return

    candidates: list[dict] = []
    for line in all_lines[-500:]:
        try:
            entry = json.loads(line)
        except Exception:
            continue
        msg_id = entry.get("dispatched_msg_id", "")
        ts = float(entry.get("ts", 0) or 0)
        if not msg_id or not ts:
            continue
        if msg_id in _feedback_done:
            continue
        age = now - ts
        if age < min_age:
            continue
        if age > max_age:
            _feedback_done.add(msg_id)
            continue
        candidates.append(entry)

    if not candidates:
        return

    log("global", "inbox", "info", f"feedback 扫描 候选={len(candidates)}")

    for entry in candidates:
        msg_id = entry.get("dispatched_msg_id", "")
        try:
            label = await _classify_feedback(entry)
            _feedback_done.add(msg_id)
            log("global", "inbox", "info",
                f"📝 feedback msg={msg_id[:14]}... label={label}")
        except Exception as e:
            log("global", "inbox", "warn",
                f"classify msg={msg_id[:14]}... 失败: {type(e).__name__}: {e}")
            # 不加入 _feedback_done，下次再试

    async with _state_lock:
        _persist_state_unlocked()


async def _classify_feedback(entry: dict) -> str:
    """判定一条派单的 feedback label，并写一行到 feedback.jsonl。

    label：
      auto_executed      — 自动执行通道 kick 了 + bot 在 thread 里有回复
      auto_then_engaged  — 自动执行 + owner 之后又接了话（最理想：bot 干完 owner 在监督）
      auto_then_ignored  — 自动执行 kick 了但 bot 没回复（claude 失败 / ACL 没过 / 异常）
      engaged            — 没 auto_execute 但 owner 进 thread 接手了
      missed             — 没 auto_execute 且 owner 完全没理（最可疑：误派）
    """
    msg_id = entry.get("dispatched_msg_id", "")
    decision = entry.get("decision") or {}
    auto_exec_kicked = bool(decision.get("_auto_exec_kicked"))
    action_prompt = (decision.get("action_prompt") or "").strip()

    thread_id = await _bot.feishu.get_message_thread_id(msg_id)
    if not thread_id:
        label = "no_thread"
        _write_feedback_line(entry, label, 0, 0, 0)
        return label

    msgs = await _bot.feishu.list_thread_messages(thread_id, limit=100)
    bot_open_id = await _bot.feishu.get_bot_open_id() or ""

    owner_real_msgs = 0      # owner 真人进来的消息
    auto_trigger_msgs = 0    # owner 身份发的"@bot <action_prompt>"（auto_execute 触发器）
    bot_replies = 0          # bot 真正回复的消息（非 loading 占位）

    # 比对触发器：取 action_prompt 前 80 字，content 里能搜到就认为是触发器
    ap_signature = action_prompt[:80] if action_prompt else ""

    for m in msgs:
        mid = m.message_id or ""
        if mid == msg_id:
            continue
        sender = m.sender
        if not sender:
            continue
        sender_id = sender.id or ""
        sender_type = sender.sender_type or ""

        if sender_type == "app":
            bot_replies += 1
            continue

        if sender_id == _config.owner_open_id:
            # 拿消息纯文本（content 是 JSON，里面中文会被 escape，先解析出 text 字段比对）
            plain = ""
            try:
                content_raw = (m.body.content if m.body else "") or ""
                if content_raw:
                    try:
                        parsed = json.loads(content_raw)
                        if isinstance(parsed, dict):
                            plain = parsed.get("text", "") or content_raw
                        else:
                            plain = content_raw
                    except Exception:
                        plain = content_raw
            except Exception:
                plain = ""
            if ap_signature and ap_signature in plain:
                auto_trigger_msgs += 1
            else:
                owner_real_msgs += 1

    if auto_exec_kicked:
        if bot_replies > 0:
            label = "auto_then_engaged" if owner_real_msgs > 0 else "auto_executed"
        else:
            label = "auto_then_ignored"
    else:
        label = "engaged" if owner_real_msgs > 0 else "missed"

    _write_feedback_line(entry, label, owner_real_msgs, bot_replies, auto_trigger_msgs)
    return label


def _write_feedback_line(
    entry: dict, label: str, owner_msgs: int, bot_msgs: int, auto_triggers: int,
) -> None:
    decision = entry.get("decision") or {}
    fb = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "dispatched_msg_id": entry.get("dispatched_msg_id", ""),
        "age_hours": round((time.time() - float(entry.get("ts", 0) or 0)) / 3600, 1),
        "label": label,
        "owner_real_messages": owner_msgs,
        "bot_replies": bot_msgs,
        "auto_exec_triggers": auto_triggers,
        "auto_exec_kicked": bool(decision.get("_auto_exec_kicked")),
        "title": (decision.get("title") or "")[:80],
        "case_key": decision.get("_case_key_used") or decision.get("case_key", ""),
        "dispatch_mode": decision.get("_dispatch_mode", ""),
        "src_summary": " | ".join(
            (sm.get("text") or "")[:80].replace("\n", " ")
            for sm in (entry.get("src_messages") or [])[:3]
        ),
    }
    p = _memory_path("feedback.jsonl")
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(fb, ensure_ascii=False) + "\n")
    except Exception as e:
        log("global", "inbox", "warn", f"feedback.jsonl 写入失败: {e}")


async def _record_decision(c: Cluster, result: dict, dispatched: bool):
    """派单和未派单都各自落一行。dispatched.jsonl / decisions_skipped.jsonl。"""
    entry = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "cluster_key": c.key,
        "dispatched": dispatched,
        "dispatched_msg_id": c.dispatched_msg_id if dispatched else "",
        "decision": result,
        "src_messages": [
            {"id": m.message_id, "sender": m.sender_name, "text": m.text[:300]}
            for m in c.messages[-8:]
        ],
    }
    path = _memory_path("dispatched.jsonl" if dispatched else "decisions_skipped.jsonl")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log("global", "inbox", "warn", f"record_decision 写入失败: {e}")


def _read_optional(p: Path) -> str:
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _read_jsonl_tail(p: Path, n: int) -> str:
    if not p.exists() or n <= 0:
        return ""
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""
