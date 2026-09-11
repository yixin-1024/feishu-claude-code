"""外部群监听器的持久化状态。

为什么必须落盘：
  · seen（已处理消息 id）只放内存的话，bot 每次重启都得重新"初始化基线"——
    重启窗口里进来的 @ 全部丢失；而不初始化基线又会把历史全量重放一遍。
  · session_id 不落盘，重启后同一话题就会开新会话，上下文断掉，
    这跟"话题群 + bot"的体验不一致（主 bot 的 session 是存在 sessions.json 里的）。
  · last_seen 决定「话题新增 · N 条」从哪条开始截，丢了就会把整条话题重灌一遍。

文件放在 session_dir 下，跟浏览器登录态同目录，迁到服务器打包一份即可。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from log_util import log

TAG = "ext-web"

# 每个群保留多少条已处理 message_id。轮询窗口只有几十条，留 500 足够覆盖，
# 又不会让状态文件无限膨胀。
SEEN_LIMIT = 500
# 自己（监听器代发）的消息 id 保留条数，纯粹防自触发用，不需要留太多。
SELF_SENT_LIMIT = 200


@dataclass
class ThreadState:
    """一个话题（thread）的会话状态。"""
    session_id: str = ""
    last_seen: str = ""       # 已注入过上下文的最后一条消息 id
    # 建会话时用的 system prompt 指纹。改了 prompt 必须换新会话——
    # append_system_prompt 只作用于**本轮**，已经写进对话历史的旧指令不会被撤销，
    # resume 老会话等于让模型继续照旧规则办事（实测：哨兵指令删掉了，
    # 但 resume 出来的会话还在吐 `-`）。
    prompt_sig: str = ""
    updated_at: float = 0.0


class WatcherState:
    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        self.seen: dict[str, list[str]] = {}          # chat_id -> message_id 列表（旧→新）
        self.threads: dict[str, ThreadState] = {}     # "chat_id:thread_id" -> ThreadState
        self.self_sent: list[str] = []                # 本监听器代发出去的 message_id
        self._seen_index: dict[str, set[str]] = {}    # chat_id -> set（查重用，不落盘）
        self._self_sent_index: set[str] = set()
        self.load()

    # ── 持久化 ───────────────────────────────────────────────
    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception as e:
            log(TAG, "state", "warn", f"状态文件损坏，按空状态启动: {e}")
            return

        self.seen = {
            str(k): [str(x) for x in (v or [])]
            for k, v in (raw.get("seen") or {}).items()
        }
        self._seen_index = {k: set(v) for k, v in self.seen.items()}
        self.self_sent = [str(x) for x in (raw.get("self_sent") or [])]
        self._self_sent_index = set(self.self_sent)
        self.threads = {}
        for key, val in (raw.get("threads") or {}).items():
            if not isinstance(val, dict):
                continue
            self.threads[str(key)] = ThreadState(
                session_id=str(val.get("session_id") or ""),
                last_seen=str(val.get("last_seen") or ""),
                prompt_sig=str(val.get("prompt_sig") or ""),
                updated_at=float(val.get("updated_at") or 0.0),
            )

    def save(self) -> None:
        """原子写：先写同目录临时文件再 rename，避免进程被杀时留半截 JSON。"""
        payload = {
            "version": 1,
            "seen": self.seen,
            "self_sent": self.self_sent,
            "threads": {k: asdict(v) for k, v in self.threads.items()},
        }
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:
            log(TAG, "state", "warn", f"状态落盘失败: {e}")

    # ── seen ─────────────────────────────────────────────────
    def is_initialized(self, chat_id: str) -> bool:
        """该群是否已经建立过基线。没建立过就不能触发历史消息。"""
        return chat_id in self.seen

    def has_seen(self, chat_id: str, message_id: str) -> bool:
        return message_id in self._seen_index.get(chat_id, set())

    def mark_seen(self, chat_id: str, message_ids: list[str]) -> None:
        lst = self.seen.setdefault(chat_id, [])
        idx = self._seen_index.setdefault(chat_id, set(lst))
        for mid in message_ids:
            if not mid or mid in idx:
                continue
            lst.append(mid)
            idx.add(mid)
        if len(lst) > SEEN_LIMIT:
            dropped = lst[:-SEEN_LIMIT]
            del lst[:-SEEN_LIMIT]
            for mid in dropped:
                idx.discard(mid)

    # ── 自己发的消息（防自触发）────────────────────────────
    def is_self_sent(self, message_id: str) -> bool:
        return message_id in self._self_sent_index

    def mark_self_sent(self, message_ids: list[str]) -> None:
        for mid in message_ids:
            if not mid or mid in self._self_sent_index:
                continue
            self.self_sent.append(mid)
            self._self_sent_index.add(mid)
        if len(self.self_sent) > SELF_SENT_LIMIT:
            dropped = self.self_sent[:-SELF_SENT_LIMIT]
            del self.self_sent[:-SELF_SENT_LIMIT]
            for mid in dropped:
                self._self_sent_index.discard(mid)

    # ── 话题会话 ─────────────────────────────────────────────
    @staticmethod
    def thread_key(chat_id: str, thread_id: str) -> str:
        return f"{chat_id}:{thread_id or '-'}"

    def get_thread(self, chat_id: str, thread_id: str) -> ThreadState:
        key = self.thread_key(chat_id, thread_id)
        st = self.threads.get(key)
        if st is None:
            st = ThreadState()
            self.threads[key] = st
        return st

    def update_thread(
        self,
        chat_id: str,
        thread_id: str,
        session_id: Optional[str] = None,
        last_seen: Optional[str] = None,
        prompt_sig: Optional[str] = None,
    ) -> None:
        st = self.get_thread(chat_id, thread_id)
        if session_id is not None:
            st.session_id = session_id
        if last_seen is not None:
            st.last_seen = last_seen
        if prompt_sig is not None:
            st.prompt_sig = prompt_sig
        st.updated_at = time.time()
