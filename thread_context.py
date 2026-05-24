"""
话题群上下文构建：从飞书 thread 消息列表生成给 Claude 的上下文块。

用于 PM 在话题评论里 @bot 时，把话题正文和所有前置评论（含附件）作为上下文
喂给 Claude，使 Claude 能读取整个话题脉络。
"""

import asyncio
import json
from datetime import datetime
from typing import Optional

from feishu_client import FeishuClient
from feishu_post import parse_post_content, extract_post_image_keys


def _fmt_time(create_time: Optional[str]) -> str:
    """飞书 create_time 是毫秒字符串"""
    if not create_time:
        return ""
    try:
        ts = int(create_time) / 1000
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _replace_mentions(text: str, mentions) -> str:
    """把 @xx_y 占位符换成 @姓名（保留可读性）"""
    if not text:
        return ""
    for m in (mentions or []):
        key = getattr(m, "key", "") or ""
        name = getattr(m, "name", "") or ""
        if key:
            text = text.replace(key, f"@{name}" if name else "")
    return text.strip()


def _extract(msg, feishu: Optional["FeishuClient"] = None) -> tuple[str, list[dict]]:
    """
    从消息体提取纯文本和附件描述。
    attachments: [{"kind": "image"|"file"|"audio"|"media", "key": "...", "name": "..."}]

    feishu: 传入则在 interactive 卡片解析为空 / 仅 loading 时，fallback 到 feishu
    自己维护的卡片文本 cache（update_card 之后的真实内容）。
    """
    msg_type = msg.msg_type or ""
    body = msg.body
    content = body.content if body else ""
    if not content:
        return "", []
    try:
        obj = json.loads(content)
    except Exception:
        return "", []

    if msg_type == "text":
        text = _replace_mentions(obj.get("text", ""), msg.mentions)
        return text, []

    if msg_type == "post":
        text = _replace_mentions(parse_post_content(content), msg.mentions)
        image_keys = extract_post_image_keys(content)
        atts = [{"kind": "image", "key": k, "name": ""} for k in image_keys]
        return text, atts

    if msg_type == "image":
        key = obj.get("image_key", "")
        return "", ([{"kind": "image", "key": key, "name": ""}] if key else [])

    if msg_type == "file":
        key = obj.get("file_key", "")
        name = obj.get("file_name", "") or "file"
        return f"[文件: {name}]", (
            [{"kind": "file", "key": key, "name": name}] if key else []
        )

    if msg_type == "audio":
        key = obj.get("file_key", "")
        return "[语音]", ([{"kind": "audio", "key": key, "name": ""}] if key else [])

    if msg_type == "media":
        key = obj.get("file_key", "")
        name = obj.get("file_name", "") or "video"
        return f"[视频: {name}]", (
            [{"kind": "media", "key": key, "name": name}] if key else []
        )

    if msg_type == "sticker":
        return "[表情]", []

    if msg_type == "interactive":
        # bot 自己发的卡片：解析 card JSON 提取 markdown 正文
        try:
            elements = (obj.get("body") or {}).get("elements") or obj.get("elements") or []
        except Exception:
            elements = []

        def _walk(els):
            parts = []
            for el in els or []:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag", "")
                if tag == "markdown":
                    c = (el.get("content") or "").strip()
                    if c:
                        parts.append(c)
                elif tag == "div":
                    inner = (el.get("text") or {}).get("content")
                    if inner:
                        parts.append(str(inner).strip())
                elif tag in ("column_set", "column"):
                    # 递归列布局；按钮文本无视，靠 markdown 拿到主要内容即可
                    parts.extend(_walk(el.get("columns") or el.get("elements") or []))
            return parts

        chunks = _walk(elements)
        text = "\n".join(chunks).strip()
        # 飞书 im.v1.message.list 返回的 interactive content 是初始快照（多半是
        # loading 占位），update_card 之后的内容拿不到 → fallback 到 bot 自己维护
        # 的卡片文本 cache。两边都空才认为这条没内容。
        if not text or text in ("⏳ 思考中...", "⏳ 思考中"):
            if feishu is not None:
                cached = feishu.get_card_text(msg.message_id or "")
                if cached:
                    return cached, []
            return "", []
        return text, []

    return f"[{msg_type}]", []


def _sender_label(msg) -> str:
    """给消息发送者取一个可读 label：bot 自己 → "bot"；其它优先 mention 同 id 的 name，否则 open_id 末 6 位"""
    sender = msg.sender
    if not sender:
        return "unknown"
    if (sender.sender_type or "") == "app":
        return "bot"
    sid = sender.id or ""
    for m in (msg.mentions or []):
        mid = getattr(m, "id", "") or ""
        mname = getattr(m, "name", "") or ""
        if mid and mid == sid and mname:
            return mname
    if sid:
        return f"user_{sid[-6:]}"
    return "unknown"


async def build_thread_context(
    feishu: FeishuClient,
    thread_id: str,
    last_seen_message_id: str,
    current_message_id: str,
) -> tuple[str, list[str]]:
    """
    构建话题上下文文本块，并下载历史消息里的附件。

    Returns:
        (context_text, downloaded_paths)
        context_text: 空字符串表示没有新增未见过的消息
        downloaded_paths: 历史消息里附件下载到本地的路径列表
    """
    try:
        msgs = await feishu.list_thread_messages(thread_id)
    except Exception as e:
        print(f"[thread] 拉取话题消息失败 thread={thread_id[:12]}...: {e}", flush=True)
        return "", []

    if not msgs:
        return "", []

    # 筛选未处理过的消息：跳过当前消息、bot 自己的消息、last_seen 及之前的消息
    unseen = []
    hit_last_seen = not bool(last_seen_message_id)  # 空 last_seen → 全量从头
    for m in msgs:
        mid = m.message_id or ""
        if mid == current_message_id:
            continue
        if not hit_last_seen:
            if mid == last_seen_message_id:
                hit_last_seen = True
            continue
        # 注意：保留 bot (sender_type=="app") 自己的卡片消息，便于 /new 后让新 session
        # 能看到 bot 之前的回复脉络。loading 占位卡和无正文的卡片会在 _extract 里被过滤。
        unseen.append(m)

    if not unseen:
        return "", []

    # 并发下载所有附件
    download_tasks = []
    download_meta = []  # [(kind, display_name), ...]
    per_msg_indices: list[list[int]] = []  # 每条消息对应 download_tasks 的下标列表

    for m in unseen:
        att_indices = []
        _text, atts = _extract(m, feishu)
        for att in atts:
            if not att["key"]:
                continue
            task = feishu.download_file(
                m.message_id,
                att["key"],
                msg_type=att["kind"],
                file_name=att["name"],
            )
            att_indices.append(len(download_tasks))
            download_tasks.append(task)
            download_meta.append((att["kind"], att["name"] or att["key"][:8]))
        per_msg_indices.append(att_indices)

    if download_tasks:
        results = await asyncio.gather(*download_tasks, return_exceptions=True)
    else:
        results = []

    # 组装上下文文本
    lines = []
    all_paths = []
    for seq, m in enumerate(unseen, 1):
        text, _atts = _extract(m, feishu)
        sender = _sender_label(m)
        time_str = _fmt_time(m.create_time)
        header = f"[{seq}] {sender}"
        if time_str:
            header += f" ({time_str})"

        if text:
            lines.append(f"{header}: {text}")
        else:
            lines.append(f"{header}:")

        for ai in per_msg_indices[seq - 1]:
            kind, display_name = download_meta[ai]
            result = results[ai]
            if isinstance(result, Exception):
                lines.append(f"    · [{kind} 下载失败: {display_name}]")
            else:
                lines.append(f"    · 附件({kind}): {result}")
                all_paths.append(result)

    prefix = (
        f"【话题历史 · {len(unseen)} 条（按时间顺序）】"
        if not last_seen_message_id
        else f"【话题新增 · {len(unseen)} 条（距上次处理后）】"
    )
    context = prefix + "\n" + "\n".join(lines)
    return context, all_paths
