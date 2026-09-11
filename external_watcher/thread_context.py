"""外部群话题上下文构建。

跟主 bot 的 thread_context.py 是同一套输出格式（【话题新增 · N 条（距上次处理后）】
+ `[i] 姓名 (时间): 正文` + `    · 附件(kind): 路径`），差别只在数据来源：
主 bot 用 bot token 走 FeishuClient，这里用机主的 user token 走 lark-cli。

顺带一个白捡的好处：user 身份能直接读到卡片正文（`<card>…</card>`），
不用像主 bot 那样再回头借 user 身份补捞一次。
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Optional

from log_util import log
from external_watcher.lark_user_api import (
    LarkUserApi,
    extract_file_keys,
    extract_image_keys,
    strip_resource_markup,
    unwrap_card,
)

TAG = "ext-web"

# last_seen 不在本次拉取窗口内时（话题太长 / 状态文件过期），退回最近这么多条，
# 免得整条话题重灌一遍，也免得静默什么都不给。
FALLBACK_TAIL = 15
# 单次注入最多带多少条历史，防止一条超长话题把 prompt 撑爆。
MAX_CONTEXT_MESSAGES = 40
# 单条正文截断长度，跟主 bot 的卡片补捞上限对齐。
MAX_TEXT_CHARS = 4000


def _fmt_time(create_time: str) -> str:
    """lark-cli 给的是 'YYYY-MM-DD HH:MM'，转成主 bot 一致的 'MM-DD HH:MM'。"""
    raw = (create_time or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).strftime("%m-%d %H:%M")
        except ValueError:
            continue
    return raw


def sender_label(msg: dict, owner_open_id: str) -> str:
    """发送人显示名。机主本人标 (自己)，跟主 bot 里 bot 标 (自己) 的位置一致。"""
    sender = msg.get("sender") or {}
    name = str(sender.get("name") or "").strip()
    sid = str(sender.get("id") or "")
    if not name:
        name = f"user_{sid[-6:]}" if sid else "未知"
    if owner_open_id and sid == owner_open_id:
        return f"{name}(自己)"
    return name


def extract_text(msg: dict) -> str:
    """把一条消息压成一行正文。图片 / 附件的 markdown 占位在这里去掉，改走附件行。"""
    msg_type = str(msg.get("msg_type") or "")
    content = str(msg.get("content") or "")

    if msg_type == "interactive":
        content = unwrap_card(content)
    if msg_type in ("post", "image", "file", "media", "audio", "interactive", "text"):
        text = strip_resource_markup(content)
    else:
        text = content.strip()

    if not text and msg_type not in ("text", "post", "interactive"):
        text = f"[{msg_type} 消息]"
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + " …（已截断）"
    return text


def extract_attachments(msg: dict) -> list[dict]:
    """返回 [{'kind': 'image'|'file', 'key': ..., 'name': ...}]。"""
    content = str(msg.get("content") or "")
    if str(msg.get("msg_type") or "") == "interactive":
        # 卡片里的图片是 bot 自己发的，没有可下载的 message resource，跳过。
        return []
    atts = [{"kind": "image", "key": k, "name": k[:12]} for k in extract_image_keys(content)]
    atts += [{"kind": "file", "key": k, "name": n} for n, k in extract_file_keys(content)]
    return atts


def select_unseen(
    msgs: list[dict], last_seen: str, current_message_id: str,
) -> tuple[list[dict], bool]:
    """挑出"上次处理之后"的消息。

    返回 (unseen, truncated)；truncated=True 表示 last_seen 不在窗口内，
    退回了最近 FALLBACK_TAIL 条（调用方据此在文案上说明是"话题最近"而非"新增"）。
    """
    pool = [m for m in msgs if str(m.get("message_id") or "") != current_message_id]
    if not pool:
        return [], False

    if not last_seen:
        return pool[-MAX_CONTEXT_MESSAGES:], False

    ids = [str(m.get("message_id") or "") for m in pool]
    if last_seen not in ids:
        # 也可能 last_seen 就是 current_message_id（上一轮刚推进过），那不算截断
        return pool[-FALLBACK_TAIL:], True

    idx = ids.index(last_seen)
    return pool[idx + 1:][:MAX_CONTEXT_MESSAGES], False


async def build_thread_context(
    api: LarkUserApi,
    chat_id: str,
    thread_id: str,
    last_seen: str,
    current_message_id: str,
    owner_open_id: str,
    download_dir: str,
) -> tuple[str, list[str], Optional[str]]:
    """构建话题上下文块并下载历史附件。

    Returns:
        (context_text, downloaded_paths, error)
        context_text 为空 = 没有新增可注入的消息（不是错误）。
        error 非空 = 拉话题历史失败，调用方据此**不要推进 last_seen**，
        下一轮还能补回这段 backlog。
    """
    if not thread_id:
        return "", [], None

    try:
        msgs = await api.list_thread_messages(thread_id)
    except Exception as e:
        log(TAG, "thread", "warn", f"拉取话题消息失败 thread={thread_id[:14]}…: {e}")
        return "", [], str(e)

    if not msgs:
        return "", [], None

    unseen, truncated = select_unseen(msgs, last_seen, current_message_id)
    if not unseen:
        return "", [], None

    # 并发下载所有附件
    tasks: list[asyncio.Future] = []
    meta: list[tuple[str, str]] = []
    per_msg: list[list[int]] = []
    for m in unseen:
        idxs: list[int] = []
        mid = str(m.get("message_id") or "")
        for n, att in enumerate(extract_attachments(m)):
            out = os.path.join(download_dir, f"{mid}_{n}")
            tasks.append(api.download_resource(mid, att["key"], att["kind"], out))
            idxs.append(len(tasks) - 1)
            meta.append((att["kind"], att["name"]))
        per_msg.append(idxs)

    results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []

    lines: list[str] = []
    paths: list[str] = []
    for seq, m in enumerate(unseen, 1):
        text = extract_text(m)
        header = f"[{seq}] {sender_label(m, owner_open_id)}"
        time_str = _fmt_time(str(m.get("create_time") or ""))
        if time_str:
            header += f" ({time_str})"
        lines.append(f"{header}: {text}" if text else f"{header}:")

        for ai in per_msg[seq - 1]:
            kind, name = meta[ai]
            res = results[ai]
            if isinstance(res, BaseException):
                lines.append(f"    · [{kind} 下载失败: {name}]")
            else:
                lines.append(f"    · 附件({kind}): {res}")
                paths.append(str(res))

    if truncated:
        prefix = f"【话题最近 · {len(unseen)} 条（更早的没取到，可能已超出拉取窗口）】"
    elif not last_seen:
        prefix = f"【话题历史 · {len(unseen)} 条（按时间顺序）】"
    else:
        prefix = f"【话题新增 · {len(unseen)} 条（距上次处理后）】"

    return prefix + "\n" + "\n".join(lines), paths, None
