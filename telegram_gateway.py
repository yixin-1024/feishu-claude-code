"""Telegram 长轮询网关：把 getUpdates 的 update 翻译成 Lark 事件的形状。

设计原则和 telegram_client 一样——**不动业务层**。dispatcher.handle_message_async
只读事件对象上这几个字段：

    event.event.sender.sender_id.open_id
    event.event.message.{message_id, chat_id, chat_type, thread_id,
                         message_type, content, mentions, parent_id, create_time}

所以这里造一组同名属性的轻对象，dispatcher 完全分不出这条消息来自 Lark 还是
Telegram，斜杠命令 / 队列 / 图片 / 上下文注入 / 按钮全都照旧生效。

几个 Telegram 特有的坑：

1. **没有话题群**。合成 thread id 代替：一个群默认一条 thread（`c<chat_id>`），
   论坛群的 topic 用 `f<topic_id>`，dispatch_task 派出去的子会话用 `t<锚点消息 id>`
   （回复到子会话消息上就自动落回它自己的 thread）。有了 thread id，
   `<chat>:<thread>` 这个复合 session key、last_seen 未读水位线、共享话题 session
   就全部照 Lark 那套跑，"自动读上下文"不用另写一套。

2. **entity 的 offset 是 UTF-16 单位**，正文里有 emoji 时按 Python 下标切会错位。
   所以判 @ 一律用正则找 `@username`，不碰 offset。

3. **privacy mode**。默认开启时 Telegram 只推「@ 到 bot / 回复 bot / 斜杠命令」，
   别人的闲聊 bot 收不到，上下文缓冲就只有这些。要完整上下文得去 BotFather
   `/setprivacy → Disable`（或把 bot 设成群管理员）。启动时会检查并告警。

4. **bot 收不到自己发的消息**，所以 bot 的回复由 telegram_client 自己写进缓冲。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Callable, Optional

from bot_instance import BotInstance
from log_util import log
from telegram_client import TelegramApiError, make_key, new_thread_id, split_key

# 启动时 Telegram 可能还压着一堆离线期间的 update。比这个时间更老的一律丢掉
# （只推进 offset），否则 /restart 之后会把几小时前的老消息当新任务重跑一遍。
_START_GRACE_SEC = 180
_POLL_TIMEOUT = 25
# 相册（media_group）：用户一次发多张图，Telegram 推的是**多条独立 message**，只有
# 第一条带说明文字。不聚合的话同一份相册会把 agent 跑 N 遍，后面几遍还看不到说明。
# 同组消息几乎同时到，攒这么久足够。
_ALBUM_WINDOW = float(os.getenv("CC_TG_ALBUM_WINDOW_SEC", "1.5") or "1.5")
# 连续多少次 409 才认定"真有第二个实例在抢"（之前都当成连接错位，快速重试）
_CONFLICT_ESCALATE = int(os.getenv("CC_TG_CONFLICT_ESCALATE", "8") or "8")
# 两次长轮询之间的小间隔。上一轮的长轮询在 Telegram 服务端注销要一小会儿，紧挨着发
# 下一轮就会被判成"两个 getUpdates 同时在跑"→ 每轮都白吃一个 409（实测 5 分钟 15 次，
# 消息不丢但请求翻倍，还会盖住"真有第二个实例"这种需要看见的信号）。
_POLL_GAP = float(os.getenv("CC_TG_POLL_GAP_SEC", "0.4") or "0.4")
# 撞 409 时退避的**上限**。这里刻意远小于普通错误的 60s：409 最坏也就是"有人跟我抢"，
# 而抢的规则是**最新的请求赢**，所以多试才有用、少试只会让自己一直没份。
# 真机踩过：退避涨到 60s 之后连续 40 次 409 = 入站整整停了 30 分钟（Lark 侧毫无异常）。
_CONFLICT_MAX_BACKOFF = float(os.getenv("CC_TG_CONFLICT_MAX_BACKOFF", "10") or "10")
# 连续几次 409 之后改用「零超时轮询」抢回槽位（Telegram 把 getUpdates 的槽位交给
# **最后到达**的那个请求，长轮询会一直占着，短轮询才抢得回来）
_RECLAIM_AFTER = int(os.getenv("CC_TG_RECLAIM_AFTER", "3") or "3")


# ── Lark 事件形状的轻替身 ────────────────────────────────────

class _MentionId:
    __slots__ = ("open_id", "user_id", "union_id")

    def __init__(self, open_id: str):
        self.open_id = open_id
        self.user_id = open_id
        self.union_id = open_id


class _Mention:
    """WS 事件口径的 mention：`.id` 是对象（`.open_id`），`.key` 是正文里的占位符。"""

    __slots__ = ("id", "key", "name", "tenant_key")

    def __init__(self, open_id: str, key: str, name: str = ""):
        self.id = _MentionId(open_id)
        self.key = key
        self.name = name
        self.tenant_key = ""


class _SenderId:
    __slots__ = ("open_id", "user_id", "union_id")

    def __init__(self, open_id: str):
        self.open_id = open_id
        self.user_id = open_id
        self.union_id = open_id


class _Sender:
    __slots__ = ("sender_id", "sender_type", "tenant_key")

    def __init__(self, open_id: str):
        self.sender_id = _SenderId(open_id)
        self.sender_type = "user"
        self.tenant_key = ""


class TgEventMessage:
    __slots__ = (
        "message_id", "root_id", "parent_id", "chat_id", "chat_type", "thread_id",
        "message_type", "content", "mentions", "create_time", "update_time",
        "sender_name", "tg_chat_id", "tg_message_id",
    )

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot, ""))
        self.mentions = kw.get("mentions") or []


class _EventBody:
    __slots__ = ("message", "sender")

    def __init__(self, message: TgEventMessage, sender: _Sender):
        self.message = message
        self.sender = sender


class TgEvent:
    """`P2ImMessageReceiveV1` 的替身。"""

    __slots__ = ("event", "header", "schema")

    def __init__(self, message: TgEventMessage, sender: _Sender):
        self.event = _EventBody(message, sender)
        self.header = None
        self.schema = "2.0"


# ── update → 事件 ────────────────────────────────────────────

def _display_name(user: dict) -> str:
    first = (user.get("first_name") or "").strip()
    last = (user.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    return name or (user.get("username") or "") or str(user.get("id") or "")


def _photo_file_id(photos: list) -> str:
    """photo 是同一张图的多个尺寸，取最大的那个。

    `file_size` 在 Bot API 里是 Optional：缺失时所有比较都是 0>0=False，会停在
    Telegram 给的第一个元素（90px 缩略图），agent 拿糊图去读图分析。所以补一个
    像素面积的次级排序键。
    """
    best = None
    best_key = (-1, -1)
    for p in photos or []:
        key = (int(p.get("file_size") or 0),
               int(p.get("width") or 0) * int(p.get("height") or 0))
        if key > best_key:
            best, best_key = p, key
    return (best or {}).get("file_id", "")


def _post_content(caption: str, image_keys: list[str]) -> str:
    """拼一段 Lark post JSON，让 dispatcher 的 post 分支同时吃到文字和图片。"""
    line: list[dict] = []
    if caption:
        line.append({"tag": "text", "text": caption})
    for key in image_keys:
        line.append({"tag": "img", "image_key": key})
    return json.dumps({"zh_cn": {"title": "", "content": [line]}}, ensure_ascii=False)


def _mentions_for(text: str, msg: dict, bot_username: str, bot_id: int,
                  commands_imply_mention: bool) -> list[_Mention]:
    """判定这条消息有没有 @ 到本 bot，并给出正文里要剥掉的占位符。"""
    mentions: list[_Mention] = []
    open_id = str(bot_id)

    if bot_username:
        # 边界只排除 **ASCII** 单词字符：
        #   左 (?<![A-Za-z0-9_@])：邮箱 `ops@spx_bot.com` 里的片段不算 @ 到我；
        #   右 (?![A-Za-z0-9_@])：`@spx_bot2` / `@spx_bot_dev` 是别的 bot，不算；
        # 但 `\b` 不行 —— 中文是 \w，`@spx_bot帮我看看`（手机上很常见，Telegram 的
        # 自动补全并不总是补空格）在 `\b` 下会**整条漏掉**，bot 一声不响。
        pattern = rf"(?<![A-Za-z0-9_@])@{re.escape(bot_username)}(?![A-Za-z0-9_@])"
        for m in re.finditer(pattern, text or "", re.IGNORECASE):
            mentions.append(_Mention(open_id, m.group(0), bot_username))

    entities = (msg.get("entities") or []) + (msg.get("caption_entities") or [])
    # bot_id==0 表示 getMe 还没成功。这时候一律不认 @ —— 否则 `int(None or 0) == 0`
    # 会把"根本不是回复/不是 @"的普通闲聊也判成 @ 到我，群里逢消息就抢答。
    if bot_id:
        for ent in entities:
            if ent.get("type") == "text_mention":
                user = ent.get("user") or {}
                if int(user.get("id") or 0) == bot_id:
                    mentions.append(_Mention(open_id, "", bot_username))

        # 回复 bot 自己的消息 = 在跟 bot 说话（Telegram 里最自然的接续方式）
        reply_from = ((msg.get("reply_to_message") or {}).get("from") or {})
        if int(reply_from.get("id") or 0) == bot_id:
            mentions.append(_Mention(open_id, "", bot_username))

    # `/cmd` 或 `/cmd@bot`：Telegram 客户端点命令菜单发出来的就是裸 `/cmd`，
    # 群里不认它就等于斜杠命令在群里全废。
    if commands_imply_mention and not mentions:
        for ent in entities:
            if ent.get("type") == "bot_command" and int(ent.get("offset") or 0) == 0:
                head = (text or "").split()[0] if text else ""
                at = head.split("@", 1)
                if len(at) == 1 or (bot_username and at[1].lower() == bot_username.lower()):
                    mentions.append(_Mention(open_id, "", bot_username))
                break

    # 去重（同一个 key 只留一份，空 key 只留一个）
    unique: list[_Mention] = []
    seen: set[str] = set()
    for m in mentions:
        tag = m.key or "\x00"
        if tag in seen:
            continue
        seen.add(tag)
        unique.append(m)
    return unique


def _merge_photo_album(parts: list[dict]) -> dict:
    """把同一相册的多条 photo 消息合成一条：所有图 + 第一条非空说明。

    以第一条（message_id 最小）为骨架，这样回复锚点、@ 判定、时间戳都跟用户眼里的
    "这组图" 一致。
    """
    ordered = sorted(parts, key=lambda m: int(m.get("message_id") or 0))
    base = dict(ordered[0])
    caption = ""
    entities: list[dict] = []
    for part in ordered:
        cap = (part.get("caption") or "").strip()
        if cap and not caption:
            caption = cap
            entities = part.get("caption_entities") or []
    photos = [pid for pid in (_photo_file_id(p.get("photo") or []) for p in ordered) if pid]
    base["caption"] = caption
    base["caption_entities"] = entities
    base["photo"] = ordered[0].get("photo") or []
    base["_album_photos"] = photos
    base.pop("media_group_id", None)
    return base


def _resolve_thread(bot: BotInstance, msg: dict, chat_id: str, is_group: bool) -> str:
    """合成 thread id。私聊返回空串（跟 Lark 私聊一致，不注入话题上下文）。

    thread id 一律带 chat 作用域（`c<chat>` / `f<chat>#<topic>` / `t<chat>#<mid>`）：
    topic id 和锚点 mid 都是 chat 内局部 id，不带 chat 的话两个群撞号就会互相串上下文。
    """
    if not is_group:
        return ""
    buffer = bot.feishu.buffer
    reply = msg.get("reply_to_message") or {}
    reply_mid = reply.get("message_id")
    if reply_mid:
        inherited = buffer.thread_of(make_key(chat_id, reply_mid))
        # 只继承 dispatch 子会话（t*）与论坛话题（f*），普通群主线回复不设子会话
        if inherited.startswith("t") or inherited.startswith(f"f{chat_id}#"):
            return inherited
        # 锚点那条记录可能已被 MAX_PER_THREAD 裁掉 / 回放窗口截掉。桶本身只要还有
        # 消息就在，所以直接问"有没有这条子会话"，别只依赖单条锚点记录 ——
        # 否则子会话会静默塌回群主线，和群里的主对话共享 session（串上下文且无报错）。
        candidate = new_thread_id(chat_id, reply_mid)
        if buffer.has_thread(candidate):
            return candidate

    chat = msg.get("chat") or {}
    is_forum = bool(chat.get("is_forum"))
    topic_id = msg.get("message_thread_id")

    # 论坛话题：群开启了论坛功能且带 topic_id，或者消息显式带有 is_topic_message 标记
    if topic_id and (is_forum or msg.get("is_topic_message")):
        return f"f{chat_id}#{topic_id}"
    return f"c{chat_id}"


def build_event(bot: BotInstance, msg: dict) -> Optional[TgEvent]:
    """把一条 Telegram message 翻成 Lark 形状的事件；记进上下文缓冲。

    返回 None = 这条消息不进业务流程（频道贴 / 纯服务消息 / 贴纸等）。
    不管返回什么，能记的都会记进缓冲——上下文要的就是"别人说过什么"。
    """
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    chat_type_raw = chat.get("type", "")
    if not sender or chat_type_raw == "channel":
        # 匿名管理员发言只有 sender_chat 没有 from：没有可鉴权的自然人，只能丢，
        # 但要留下痕迹 —— 否则"我明明 @ 了它却一声不响"完全查不出原因。
        if not sender and (msg.get("sender_chat") or {}).get("id"):
            log(bot.profile.name, "tg", "info",
                f"忽略匿名发言（只有 sender_chat，无法鉴权）chat={chat.get('id')} "
                f"mid={msg.get('message_id')}")
        return None

    chat_id = str(chat.get("id") or "")
    user_id = str(sender.get("id") or "")
    if not chat_id or not user_id:
        return None

    is_group = chat_type_raw in ("group", "supergroup")
    key = make_key(chat_id, msg.get("message_id"))
    thread_id = _resolve_thread(bot, msg, chat_id, is_group)
    name = _display_name(sender)
    bot.feishu.buffer.remember_name(user_id, name)

    profile = bot.profile
    mentions = _mentions_for(
        msg.get("text") or msg.get("caption") or "",
        msg,
        bot.feishu.bot_username,
        bot.feishu.bot_id,
        commands_imply_mention=bool(
            getattr(profile, "tg_commands_imply_mention", 1)),
    )

    text = msg.get("text")
    caption = (msg.get("caption") or "").strip()
    msg_type = ""
    content = ""

    if text is not None:
        msg_type, content = "text", json.dumps({"text": text}, ensure_ascii=False)
    elif msg.get("photo"):
        keys = list(msg.get("_album_photos") or []) or [_photo_file_id(msg["photo"])]
        keys = [k for k in keys if k]
        if caption or len(keys) > 1:
            # 有说明文字、或者是一组图 → 走 post 分支（它同时吃文字和多张图）
            msg_type, content = "post", _post_content(caption, keys)
        else:
            msg_type = "image"
            content = json.dumps({"image_key": keys[0] if keys else ""},
                                 ensure_ascii=False)
    elif msg.get("voice") or (msg.get("audio") and not (msg.get("audio") or {}).get("file_name")):
        # 只有"语音消息"（voice）和没有文件名的 audio 才送去转写；带 file_name 的
        # audio 是用户在传音乐/录音文件，几分钟长度送 ASR 没意义，走文件分支。
        media = msg.get("voice") or msg.get("audio")
        payload = {
            "file_key": media.get("file_id", ""),
            "duration": int(float(media.get("duration") or 0) * 1000),
        }
        if caption:
            payload["caption"] = caption
        msg_type = "audio"
        content = json.dumps(payload, ensure_ascii=False)
    elif (msg.get("document") or msg.get("video") or msg.get("animation")
          or msg.get("video_note") or msg.get("audio")):
        media = (msg.get("document") or msg.get("video") or msg.get("animation")
                 or msg.get("video_note") or msg.get("audio"))
        fallback = "video.mp4" if not msg.get("document") else "file"
        msg_type = "file"
        content = json.dumps({
            "file_key": media.get("file_id", ""),
            "file_name": media.get("file_name") or fallback,
        }, ensure_ascii=False)
        if caption:
            # 文件带说明：把说明也塞进正文，dispatcher 的 file 分支只读 file_name，
            # 所以顺手把 caption 拼进文件名后面会破坏后缀 —— 改成额外记一条文本。
            content = json.dumps({
                "file_key": media.get("file_id", ""),
                "file_name": media.get("file_name") or fallback,
                "caption": caption,
            }, ensure_ascii=False)
    elif msg.get("sticker"):
        emoji = (msg["sticker"] or {}).get("emoji") or ""
        bot.feishu.buffer.record(
            message_id=key, chat_id=chat_id, thread_id=thread_id or f"c{chat_id}",
            user_id=user_id, name=name, msg_type="text",
            content=json.dumps({"text": f"[贴纸 {emoji}]"}, ensure_ascii=False),
            ts_ms=int(msg.get("date") or 0) * 1000,
        )
        return None
    else:
        # 入群/退群/置顶等服务消息：不进业务流程，也不值得记
        return None

    bot.feishu.buffer.record(
        message_id=key,
        chat_id=chat_id,
        thread_id=thread_id or f"c{chat_id}",
        user_id=user_id,
        name=name,
        msg_type=msg_type,
        content=content,
        ts_ms=int(msg.get("date") or 0) * 1000,
        reply_to=(
            make_key(chat_id, (msg.get("reply_to_message") or {}).get("message_id"))
            if (msg.get("reply_to_message") or {}).get("message_id") else ""
        ),
        mentions=[{"id": m.id.open_id, "name": m.name} for m in mentions],
    )

    event_msg = TgEventMessage(
        message_id=key,
        parent_id=(
            make_key(chat_id, (msg.get("reply_to_message") or {}).get("message_id"))
            if (msg.get("reply_to_message") or {}).get("message_id") else ""
        ),
        chat_id=chat_id,
        chat_type="group" if is_group else "p2p",
        thread_id=thread_id,
        message_type=msg_type,
        content=content,
        mentions=mentions,
        create_time=str(int(msg.get("date") or 0) * 1000),
        sender_name=name,
        tg_chat_id=chat_id,
        tg_message_id=str(msg.get("message_id") or ""),
    )
    return TgEvent(event_msg, _Sender(user_id))


# ── 访问控制 ─────────────────────────────────────────────────

def group_reject(profile, chat_id: str) -> str:
    """这个群是否被允许（空串=放行）。决定"这个房间里的话能不能进上下文缓冲"。"""
    if not set(getattr(profile, "allowed_open_ids", set()) or set()):
        return "白名单为空（Telegram 不允许默认放开）"
    groups = set(getattr(profile, "allowed_group_chat_ids", set()) or set())
    if "*" not in groups and str(chat_id) not in groups:
        return "群不在白名单"
    return ""


def user_reject(profile, user_id: str) -> str:
    """这个人能不能触发 agent（空串=放行）。"""
    allowed_users = set(getattr(profile, "allowed_open_ids", set()) or set())
    if not allowed_users:
        return "白名单为空（Telegram 不允许默认放开）"
    if str(user_id) not in allowed_users:
        return "用户不在白名单"
    return ""


def acl_reject(profile, user_id: str, chat_id: str, is_group: bool) -> str:
    """能不能**触发** agent：群白名单 + 用户白名单都要过（空串=放行）。

    和 Lark 的差别：Telegram bot 的用户名是公开的，任何人都能私聊它，所以
    **白名单为空时一律拒绝**（Lark 那边为空是"所有人可用"）。宁可自己配一次，
    也不能默认对全网开放一台能跑 shell 的 agent。
    """
    if is_group:
        reason = group_reject(profile, chat_id)
        if reason:
            return reason
    return user_reject(profile, user_id)


# ── 按钮回调 ─────────────────────────────────────────────────

def handle_callback(bot: BotInstance, cq: dict, submit: Callable) -> None:
    """callback_query → 走 dispatcher 里那几个跟 Lark 卡片共用的 handler。"""
    import dispatcher

    tag = bot.profile.name
    cq_id = str(cq.get("id") or "")
    sender = cq.get("from") or {}
    user_id = str(sender.get("id") or "")
    message = cq.get("message") or {}
    chat = message.get("chat") or {}
    tg_chat_id = str(chat.get("id") or "")
    card_key = make_key(tg_chat_id, message.get("message_id"))
    is_group = (chat.get("type") in ("group", "supergroup"))

    def ack(text: str = "", alert: bool = False):
        submit(bot.feishu.answer_callback(cq_id, text, alert))

    entry = bot.feishu.resolve_callback(str(cq.get("data") or ""))
    if not entry:
        ack("按钮已过期，请重新发一次命令", alert=True)
        return

    value = entry.get("value") or {}
    if value.get("profile") not in (None, "", bot.profile.name):
        ack("按钮不属于这个 bot", alert=True)
        return
    bound_uid = str(value.get("_cc_uid") or "")
    if bound_uid and bound_uid != user_id:
        ack("这个按钮是别人的", alert=True)
        return

    chat_id = value.get("cid") or (tg_chat_id if is_group else user_id)
    reason = acl_reject(bot.profile, user_id, tg_chat_id if is_group else user_id, is_group)
    if reason:
        log(tag, "tgcard", "warn", f"拒绝按钮 user={user_id} chat={tg_chat_id}：{reason}")
        ack("无权限", alert=True)
        return

    action = value.get("action", "")
    if action == "set_mode":
        mode = value.get("mode", "")
        if mode:
            submit(dispatcher.handle_set_mode(bot, user_id, chat_id, mode, card_key))
        ack(f"已切换: {mode}")
        return
    if action == "run_cmd":
        cmd_text = value.get("cmd", "")
        if cmd_text:
            submit(dispatcher.handle_menu_command(bot, user_id, chat_id, cmd_text, card_key))
        ack(cmd_text)
        return
    if action == "switch_usage":
        name = value.get("name", "")
        if name:
            submit(dispatcher.handle_switch_usage(bot, user_id, chat_id, name, card_key))
        ack(f"正在切换到 {name}…")
        return
    if action == "resume_session":
        sid = value.get("sid", "")
        if sid:
            submit(dispatcher.handle_resume_session(bot, user_id, chat_id, sid, card_key))
        ack("正在恢复…")
        return

    reply = value.get("reply", "")
    if reply:
        submit(dispatcher.handle_button_reply(bot, user_id, chat_id, reply, card_key))
        ack(f"已发送: {reply}")
        return
    ack()


# ── 长轮询 ───────────────────────────────────────────────────

# 已经收到过"你未授权"提示的 (profile, user id)（每进程一次，别对刷子无限回复）
_revealed: set[tuple[str, str]] = set()
_revealed_lock = threading.Lock()
_REVEALED_MAX = 2000


class TgPoller:
    """一次 getUpdates 的全部逻辑（可单测，不牵扯线程）。

    Telegram 的 update 只有"确认过 offset 才会删"的语义，所以重启后会把离线期间
    积压的消息重投一遍。`_START_GRACE_SEC` 之前的消息只推进 offset 不处理，否则
    每次 /restart 都会把几小时前的老话当新任务再跑一遍。
    """

    def __init__(self, bot: BotInstance, *, submit: Callable, on_message: Callable,
                 touch: Callable[[], None], started_at: Optional[float] = None):
        self.bot = bot
        self.submit = submit
        self.on_message = on_message
        self.touch = touch
        self.started_at = time.time() if started_at is None else started_at
        self.offset: Optional[int] = None
        # 生产里轮询线程跟进程同生共死，但"能停"是可测性的前提：多个 poller 抢同一个
        # bot token 会互相吃 update（真机上是 409，测试里是静默丢消息）。
        self._stopped = threading.Event()
        # media_group_id → {"parts": [...], "first": ts}
        self._albums: dict[str, dict] = {}

    def stop(self) -> None:
        self._stopped.set()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    # ── 单条消息 ──
    def deliver(self, msg: dict) -> bool:
        """过白名单 → 记上下文 → 投给 dispatcher。返回是否真的派发了。

        **顺序很重要**：白名单在前。bot 的用户名是公开的，任何人都能私聊它，
        先 build_event 的话陌生人一条消息就能让我们往磁盘写一份正文 + 在内存里
        常驻一个会话桶（无鉴权写入）。
        """
        tag = self.bot.profile.name
        if self.stopped:
            return False

        # 相册先攒起来，等同组的其它张到齐再合成一条（见 flush_albums）
        gid = str(msg.get("media_group_id") or "")
        if gid and not msg.get("_album_merged") and msg.get("photo"):
            self._albums.setdefault(gid, {"parts": [], "first": time.time()})[
                "parts"].append(msg)
            return False

        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        chat_id = str(chat.get("id") or "")
        user_id = str(sender.get("id") or "")
        is_group = chat.get("type") in ("group", "supergroup")

        # 两级闸门，分工不同：
        #   群闸门 → 决定"这个房间的话能不能进上下文缓冲"（白名单群里所有人的发言
        #            都要记，否则被 @ 时读不到别人说了什么，"自动读上下文"就废了）
        #   人闸门 → 决定"这条消息能不能触发 agent"
        # 私聊没有房间可信任，所以人不过闸就什么都不记（bot 用户名是公开的，
        # 不能让陌生人一条私信就往磁盘写正文）。
        if chat_id and user_id:
            gate = (group_reject(self.bot.profile, chat_id) if is_group
                    else user_reject(self.bot.profile, user_id))
            if gate:
                # 自助授权：私聊里整条消息就是口令 → 当场入白名单（落盘），
                # 机主不用知道自己的 user id、不用改 .env、不用再重启一次。
                if not is_group and self._try_claim(chat_id, user_id, sender, msg):
                    return False
                log(tag, "acl", "info",
                    f"忽略 tg 消息 user={user_id} chat={chat_id} "
                    f"name={_display_name(sender)!r}：{gate}")
                self._reveal_unauthorized(chat_id, user_id, is_group)
                return False

        try:
            event = build_event(self.bot, msg)
        except Exception as e:  # noqa: BLE001 — 一条消息解析失败不能掀翻轮询
            log(tag, "tg", "warn", f"事件解析失败（已忽略）: {type(e).__name__}: {e}")
            return False
        if event is None:
            return False

        if is_group:
            speaker = user_reject(self.bot.profile, user_id)
            if speaker:
                log(tag, "acl", "info",
                    f"只记上下文不触发 user={user_id} chat={chat_id}：{speaker}")
                return False
        self.submit(self.on_message(self.bot, event))
        return True

    def _try_claim(self, chat_id: str, user_id: str, sender: dict, msg: dict) -> bool:
        """私聊里的自助授权口令。返回 True = 这条消息已被当作口令消费掉。

        只认**私聊** + **整条消息就是口令**：群里发口令一律无效（免得口令留在群历史
        里还长期有效），也不做包含匹配（避免复述/引用误触发）。
        """
        code = (getattr(self.bot.profile, "claim_code", "") or "").strip()
        if not code:
            return False
        text = (msg.get("text") or "").strip()
        if text != code:
            return False

        import tg_allowlist

        tag = self.bot.profile.name
        fresh = tg_allowlist.add(tag, user_id)
        self.bot.profile.allowed_open_ids.add(str(user_id))
        log(tag, "acl", "warn",
            f"自助授权{'成功' if fresh else '（此前已授权）'}：user={user_id} "
            f"name={_display_name(sender)!r} —— 已写入 {tg_allowlist.path_for(tag)}")
        self.submit(self.bot.feishu.send_text_to_user(chat_id, (
            "✅ 已授权，你可以直接说话了。\n\n"
            f"你的 user id `{user_id}` 已记进白名单并落盘，重启后依然有效"
            "（不需要改 .env）。\n"
            "群里用：把 bot 拉进群，然后 @ 它 / 回复它 / 发斜杠命令。"
        )))
        return True

    def _reveal_unauthorized(self, chat_id: str, user_id: str, is_group: bool) -> None:
        """私聊里给未授权的人回一次"你的 id 是 X"，方便机主把他加进白名单。

        只在私聊、每个 (profile, id) 每进程一次。群里永远不回（不给陌生群暴露
        bot 在干什么）。`<PROFILE>_REVEAL_UNAUTHORIZED=0` 可关。
        """
        if is_group:
            return
        if not getattr(self.bot.profile, "tg_reveal_unauthorized", 1):
            return
        # 按 profile 分开记：多个 TG bot 各自的白名单不同，第二个 bot 也该提示一次
        mark = (self.bot.profile.name, user_id)
        with _revealed_lock:
            if mark in _revealed:
                return
            if len(_revealed) >= _REVEALED_MAX:
                _revealed.clear()   # 朴素上限：刷子换号也不至于无限驻留
            _revealed.add(mark)
        text = (
            "⛔ 未授权。\n\n"
            f"你的 Telegram user id：`{user_id}`\n"
            f"当前 chat id：`{chat_id}`\n\n"
            f"把 id 加到 bot 的 `{self.bot.profile.name.upper()}_ALLOWED_OPEN_IDS` "
            "后重启即可使用。"
        )
        self.submit(self.bot.feishu.send_text_to_user(chat_id, text))

    def flush_albums(self, force: bool = False) -> int:
        """把攒够时间的相册合成一条消息投出去。返回投出的相册数。"""
        now = time.time()
        ready = [
            gid for gid, album in list(self._albums.items())
            if force or now - album["first"] >= _ALBUM_WINDOW
        ]
        for gid in ready:
            album = self._albums.pop(gid, None)
            if not album or not album["parts"]:
                continue
            merged = _merge_photo_album(album["parts"])
            merged["_album_merged"] = True
            try:
                self.deliver(merged)
            except Exception as e:  # noqa: BLE001
                log(self.bot.profile.name, "tg", "warn",
                    f"相册合并投递失败（已忽略）: {type(e).__name__}: {e}")
        return len(ready)

    def albums_pending(self) -> bool:
        return bool(self._albums)

    # ── 一轮轮询 ──
    def poll_once(self, reclaim: bool = False) -> int:
        """拉一批 update 并处理。返回处理条数（异常原样抛给监督循环）。

        reclaim=True 用零超时轮询：Telegram 把 getUpdates 的槽位交给**最后到达**的
        请求，所以撞 409 时短轮询才抢得回来 —— 长轮询会被对方一直占着。
        """
        tag = self.bot.profile.name
        payload = {
            "timeout": 0 if reclaim else _POLL_TIMEOUT,
            "allowed_updates": ["message", "callback_query"],
        }
        if self.offset is not None:
            # 首轮没有 offset 就别放这个键。Telegram 目前容忍 "offset": null，
            # 但那是运气不是契约（poll_once 直接调 _post_sync 拿自己的长轮询超时，
            # 绕过了 call() 的 None 过滤）。
            payload["offset"] = self.offset
        updates = self.bot.feishu._post_sync(  # noqa: SLF001 — 长轮询要自己的超时
            "getUpdates", payload, _POLL_TIMEOUT + 15, fresh=True,
        ) or []
        handled = 0
        for update in updates:
            try:
                nxt = int(update["update_id"]) + 1
            except (KeyError, TypeError, ValueError):
                # 畸形 payload：跳过这一条，别让 int() 抛出去中断整批，也别把
                # offset 拉回 1（那会让 Telegram 把之后的 update 全部反复重投）
                log(tag, "tg", "warn", f"update 缺少可用的 update_id，跳过: {update!r:.120}")
                continue
            self.offset = max(self.offset or 0, nxt)
            try:
                if "message" in update:
                    msg = update["message"]
                    sent_at = int(msg.get("date") or 0)
                    if self.started_at - sent_at > _START_GRACE_SEC:
                        log(tag, "tg", "info",
                            f"丢弃离线期堆积消息（{int(time.time() - sent_at)}s 前）"
                            f"mid={msg.get('message_id')}")
                        continue
                    self.touch()
                    self.deliver(msg)
                    handled += 1
                elif "callback_query" in update:
                    self.touch()
                    handle_callback(self.bot, update["callback_query"], self.submit)
                    handled += 1
            except Exception as e:  # noqa: BLE001
                log(tag, "tg", "error", f"处理 update 失败: {type(e).__name__}: {e}")
        # 相册收口：同组消息就在这一批或紧接着的下一批里，等满窗口再合并投递。
        # 只有真的攒着东西时才等，正常消息完全不受影响。
        if self.albums_pending():
            deadline = time.time() + _ALBUM_WINDOW
            while self.albums_pending() and time.time() < deadline and not self.stopped:
                time.sleep(0.1)
            handled += self.flush_albums(force=True)
        return handled


def start_polling(
    bot: BotInstance,
    *,
    submit: Callable,
    on_message: Callable,
    touch: Callable[[], None],
) -> TgPoller:
    """起一个后台线程长轮询 getUpdates。异常自愈，永不退出（对齐 start_profile_ws）。

    submit(coro)：把协程投到 bot_loop 执行。
    on_message(bot, event)：dispatcher.handle_message_async。
    touch()：刷新该 profile 的"最近活跃"时间戳，给看门狗用。
    """
    tag = bot.profile.name
    poller = TgPoller(bot, submit=submit, on_message=on_message, touch=touch)

    def _run():
        backoff = 3
        conflicts = 0            # 连续 409 次数（成功一次就清零）
        conflicts_total = 0      # 本轮日志窗口内累计
        last_info_log = 0.0
        last_error_log = 0.0
        while not poller.stopped:
            try:
                # 连着撞 409 就改用零超时轮询抢槽位（见 poll_once 的 reclaim）
                poller.poll_once(reclaim=conflicts >= _RECLAIM_AFTER)
                backoff = 3
                conflicts = 0
                if _POLL_GAP > 0:
                    time.sleep(_POLL_GAP)
            except TelegramApiError as e:
                if e.code == 409:
                    # 两种成因，处理方式一样：① 真有第二个实例/webhook 在抢；
                    # ② **自己的连接被复用**，读到的是上一个 getUpdates 的 409
                    # （Telegram 收到新请求就用 409 终止旧的）。②在走代理时很常见，
                    # 一旦按指数退避就会"409 → 退避 → 又读到 409"自锁，退避爬到 60s
                    # 入站彻底停（真机踩过）。所以：先丢掉连接池，再**快速重试**；
                    # 只有连续很多次都没成功才认为是①，转指数退避并喊出来。
                    conflicts += 1
                    conflicts_total += 1
                    poller.bot.feishu.reset_session()
                    now = time.time()
                    if conflicts >= _CONFLICT_ESCALATE:
                        # 第一次升级一定要喊（别被前面那条 info 的节流窗口盖住）
                        if conflicts == _CONFLICT_ESCALATE or now - last_error_log > 60:
                            last_error_log = now
                            log(tag, "tg", "error",
                                f"getUpdates 连续 {conflicts} 次 409（{e.description}）"
                                "——大概真有第二个进程/webhook 在抢这个 token，"
                                "先 deleteWebhook 或关掉另一个实例")
                        # 409 的退避上限单独收窄：多试才有机会抢回槽位
                        time.sleep(min(backoff, _CONFLICT_MAX_BACKOFF))
                        backoff = min(backoff * 2, _CONFLICT_MAX_BACKOFF)
                        continue
                    if now - last_info_log > 300:
                        # 偶发的连接错位不值得刷屏，5 分钟报一次累计量就够诊断
                        last_info_log = now
                        log(tag, "tg", "info",
                            f"getUpdates 409 已发生 {conflicts_total} 次"
                            f"（{e.description}）——已换新连接快速重试；"
                            "若消息仍能收到就只是连接错位，不是有人抢 token")
                        conflicts_total = 0
                    time.sleep(1)
                    continue
                elif e.code == 401:
                    log(tag, "tg", "error", "getUpdates 401：bot token 无效，停止轮询")
                    return
                else:
                    log(tag, "tg", "warn", f"getUpdates 失败: {e}；{backoff}s 后重试")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:  # noqa: BLE001 — 监督线程绝不能退出
                log(tag, "tg", "warn",
                    f"轮询异常: {type(e).__name__}: {e}；{backoff}s 后重试")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    threading.Thread(target=_run, daemon=True, name=f"tg-{tag}").start()
    return poller
