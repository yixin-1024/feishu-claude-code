"""Telegram 会话缓冲：把群里的消息记下来，供"自动读上下文"复用 Lark 那套逻辑。

为什么必须自己存：
    Lark 有话题群，`im.v1.message.list(container=thread)` 能把整条话题拉回来，所以
    bot 被 @ 时可以现拉历史当上下文。**Telegram Bot API 没有任何"读历史"接口** ——
    bot 只能看到推给它的 update，看不到自己加群之前、以及 turn 之间的消息。想有
    上下文只能在收到 update 的那一刻自己记下来。

存下来的记录刻意长得跟 Lark 的 `ListMessage` 一样（`.message_id` / `.msg_type` /
`.body.content` / `.sender.id` / `.create_time` / `.mentions`），这样 thread_context.py
里那套「筛未读 → 解析正文 → 下附件 → 拼上下文块」的成熟逻辑可以原封不动复用，
dispatcher 也不用为 Telegram 分叉。

⚠️ 前提：群里的非 @ 消息想被记下来，bot 的 privacy mode 必须关（BotFather →
/setprivacy → Disable），否则 Telegram 只推「@ 到 bot / 回复 bot / 斜杠命令」这几类，
上下文就只有这些。启动时会检查 getMe 的 can_read_all_group_messages 并告警。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Optional

_DEFAULT_DIR = os.path.expanduser("~/.feishu-claude/tg")

# 单 thread 保留多少条（超出丢最早的）。上下文注入只看 last_seen 之后那几条，
# 留 200 足够覆盖"bot 离线一阵子"的补读，又不至于让文件无限长。
MAX_PER_THREAD = 200
# 文件超过这个大小就在下次启动时压实（只写活着的记录）
COMPACT_BYTES = 4 * 1024 * 1024


def buffer_dir() -> str:
    return os.environ.get("CC_TG_BUFFER_DIR", "").strip() or _DEFAULT_DIR


class _Body:
    __slots__ = ("content",)

    def __init__(self, content: str):
        self.content = content


class _Sender:
    __slots__ = ("id", "sender_type", "tenant_key")

    def __init__(self, sender_id: str, sender_type: str):
        self.id = sender_id
        self.sender_type = sender_type
        self.tenant_key = ""


class _Mention:
    __slots__ = ("id", "name", "key")

    def __init__(self, mention_id: str, name: str):
        self.id = mention_id
        self.name = name
        self.key = ""


class TgMessage:
    """Lark `ListMessage` 的鸭子类型替身（thread_context / read_thread 直接吃）。"""

    __slots__ = (
        "message_id", "msg_type", "body", "mentions", "sender",
        "create_time", "update_time", "thread_id", "parent_id", "chat_id",
        "sender_name", "upsert_key",
    )

    def __init__(self, rec: dict):
        self.message_id = rec.get("mid", "")
        self.msg_type = rec.get("type", "text")
        self.body = _Body(rec.get("content", ""))
        self.sender = _Sender(rec.get("uid", ""), "app" if rec.get("bot") else "user")
        self.sender_name = rec.get("name", "")
        self.mentions = [
            _Mention(m.get("id", ""), m.get("name", ""))
            for m in (rec.get("mentions") or [])
        ]
        self.create_time = str(rec.get("ts", "") or "")
        self.update_time = self.create_time
        self.thread_id = rec.get("thread", "")
        self.parent_id = rec.get("reply_to", "") or ""
        self.chat_id = rec.get("chat", "")
        self.upsert_key = rec.get("mid", "")


class TgBuffer:
    """一个 profile 的全部 Telegram 消息缓冲（内存 + jsonl 落盘）。

    落盘是为了跨重启保住上下文：/restart 很频繁，纯内存的话每次重启后第一条消息
    都会因为 last_seen 指向一条"已经不在缓冲里"的消息而拿不到任何历史。
    """

    def __init__(self, profile: str, path: Optional[str] = None):
        self.profile = profile
        self.path = path or os.path.join(buffer_dir(), f"{profile}.jsonl")
        self._lock = threading.RLock()
        self._threads: dict[str, list[dict]] = {}
        self._index: dict[str, dict] = {}       # mid → record
        # 只记 thread 归属、不记正文的消息（bot 发的 ✅ / 📬 这类带外提示）。
        # 用户回复它们时要认得出属于哪条 thread，但它们不该进 prompt 上下文。
        self._thread_only: dict[str, str] = {}
        self._THREAD_ONLY_MAX = 2000
        self._names: dict[str, str] = {}        # uid → 显示名
        self._load()

    # ── 落盘 ────────────────────────────────────────────────
    def _append_line(self, obj: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"[tg] 缓冲写盘失败（忽略）: {e}", flush=True)

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            # errors="replace"：进程被 kill -9 / 磁盘写满时尾行可能撕成半个多字节字符，
            # 严格解码会抛 UnicodeDecodeError —— 而这个构造函数在 BotInstance.__init__
            # 里、main 的 profile 循环没有 try，等于**整个 cc-lark 起不来**（Lark 的
            # profile 一起陪葬），而且每次启动都炸、永不自愈。
            with open(self.path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            print(f"[tg] 缓冲读盘失败（忽略）: {e}", flush=True)
            return
        oversized = False
        try:
            oversized = os.path.getsize(self.path) > COMPACT_BYTES
        except OSError:
            pass
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001 — 坏行只能跳过，不能让 bot 开不了机
                continue
            if not isinstance(obj, dict):
                # 合法 JSON 但不是 object（`[]` / `null` / `123` / `"str"`）：
                # 下面 obj.get 会 AttributeError，同样属于"开不了机"级别
                continue
            op = obj.get("op", "msg")
            if op == "msg":
                self._insert(obj, persist=False)
            elif op == "text":
                self._patch_text(obj.get("mid", ""), obj.get("content", ""), persist=False)
        if oversized:
            self._compact()

    # 注：这里刻意**全量回放**而不是只读尾部 N 行。曾经的"尾部窗口"会让安静的群
    # 被活跃群的消息挤出窗口，接着一次压实就把它们**永久删掉** —— 上下文静默清零，
    # 没有任何报错。内存有 MAX_PER_THREAD 兜着（每个 thread 最多 200 条），
    # 文件有 COMPACT_BYTES 兜着（超了就压实），全量扫一遍的代价可控。

    def _compact(self) -> None:
        """把内存里活着的记录重写成新文件（丢掉被裁掉的历史和 patch 流水）。"""
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for records in self._threads.values():
                    for rec in records:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
            print(f"[tg] 缓冲已压实: {self.path}", flush=True)
        except OSError as e:
            print(f"[tg] 缓冲压实失败（忽略）: {e}", flush=True)

    # ── 写入 ────────────────────────────────────────────────
    def _insert(self, rec: dict, persist: bool = True) -> None:
        mid = rec.get("mid", "")
        if not mid:
            return
        thread = rec.get("thread", "") or rec.get("chat", "")
        rec["thread"] = thread
        with self._lock:
            if mid in self._index:
                self._index[mid].update(rec)
            else:
                bucket = self._threads.setdefault(thread, [])
                bucket.append(rec)
                self._index[mid] = rec
                if len(bucket) > MAX_PER_THREAD:
                    for dropped in bucket[:-MAX_PER_THREAD]:
                        self._index.pop(dropped.get("mid", ""), None)
                    del bucket[:-MAX_PER_THREAD]
            name = rec.get("name", "")
            if rec.get("uid") and name:
                self._names[str(rec["uid"])] = name
            # 写盘放在锁内：msg 行和它后续的 text patch 行必须保持先后顺序，
            # 否则重启回放时 patch 找不到对应记录，终稿被回滚成"⏳ 思考中..."
            if persist:
                self._append_line({"op": "msg", **rec})

    def record(
        self,
        *,
        message_id: str,
        chat_id: str,
        thread_id: str,
        user_id: str,
        name: str = "",
        is_bot: bool = False,
        msg_type: str = "text",
        content: str = "",
        ts_ms: int = 0,
        reply_to: str = "",
        mentions: Optional[list[dict]] = None,
    ) -> None:
        """记一条消息。content 是 **Lark 口径的 JSON 串**（如 {"text": "..."}）。"""
        self._insert({
            "mid": message_id,
            "chat": str(chat_id),
            "thread": thread_id,
            "uid": str(user_id),
            "name": name,
            "bot": bool(is_bot),
            "type": msg_type,
            "content": content,
            "ts": int(ts_ms or 0),
            "reply_to": reply_to or "",
            "mentions": mentions or [],
        })

    def _patch_text(self, mid: str, text: str, persist: bool = True) -> None:
        if not mid:
            return
        with self._lock:
            rec = self._index.get(mid)
            if rec is None:
                return
            rec["content"] = json.dumps({"text": text}, ensure_ascii=False)
            rec["type"] = "text"
            if persist:
                self._append_line({"op": "text", "mid": mid, "content": text})

    def update_text(self, message_id: str, text: str) -> None:
        """bot 自己那条消息被 edit 后，把最终正文覆盖进缓冲（流式中间帧不必调）。"""
        self._patch_text(message_id, text)

    # ── 读取 ────────────────────────────────────────────────
    def thread_messages(self, thread_id: str, limit: int = 200) -> list[TgMessage]:
        with self._lock:
            records = list(self._threads.get(thread_id, []))
        if limit and len(records) > limit:
            records = records[-limit:]
        return [TgMessage(r) for r in records]

    def remember_thread(self, message_id: str, thread_id: str) -> None:
        """只登记"这条消息属于哪条 thread"，不进上下文。"""
        if not (message_id and thread_id):
            return
        with self._lock:
            if len(self._thread_only) >= self._THREAD_ONLY_MAX:
                for key in list(self._thread_only.keys())[: self._THREAD_ONLY_MAX // 4]:
                    self._thread_only.pop(key, None)
            self._thread_only[message_id] = thread_id

    def thread_of(self, message_id: str) -> str:
        with self._lock:
            rec = self._index.get(message_id or "")
            thread = (rec or {}).get("thread", "") or ""
            return thread or self._thread_only.get(message_id or "", "")

    def chat_of(self, message_id: str) -> str:
        with self._lock:
            rec = self._index.get(message_id or "")
            return (rec or {}).get("chat", "") or ""

    def has(self, message_id: str) -> bool:
        with self._lock:
            return (message_id or "") in self._index

    def has_thread(self, thread_id: str) -> bool:
        """这条 thread 里有没有记录。

        用来在"锚点那条记录已被裁掉"之后仍然认得出子会话：桶本身只要还有一条消息
        就在，比依赖单条锚点记录健壮得多（MAX_PER_THREAD 裁的是桶内最老的几条）。
        """
        with self._lock:
            return bool(self._threads.get(thread_id or ""))

    def names(self, user_ids: list[str]) -> dict[str, str]:
        with self._lock:
            return {
                str(uid): self._names.get(str(uid), "")
                for uid in user_ids
            }

    def name_of(self, user_id: str) -> str:
        with self._lock:
            return self._names.get(str(user_id), "")

    def remember_name(self, user_id: str, name: str) -> None:
        if user_id and name:
            with self._lock:
                self._names[str(user_id)] = name
