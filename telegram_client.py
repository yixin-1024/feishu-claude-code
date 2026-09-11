"""Telegram Bot API 客户端——刻意做成 FeishuClient 的鸭子类型替身。

dispatcher / commands / scheduler 里到处都是 `bot.feishu.reply_card(...)` /
`update_card(...)`，这些调用点（32 + 23 处）没有一个关心底层是 Lark 还是 Telegram。
所以接 Telegram 的正确做法不是在 dispatcher 里加 `if platform == "telegram"` 分支，
而是**换掉 bot.feishu 这个实现**：方法名、参数、返回值全部对齐 FeishuClient，
业务层一行不用改，runner / session / 队列 / 斜杠命令 / 按钮全部原样复用。

两边的概念对照：

    Lark                        Telegram
    ──────────────────────────  ────────────────────────────────────
    interactive 卡片            一条普通消息（HTML parse_mode）
    update_card (patch)         editMessageText
    卡片按钮 + callback value    inline_keyboard + callback_data(token)
    message_id "om_xxx"         "<chat_id>:<message_id>" 复合 key
    话题 thread                 无（合成 thread id，见 telegram_gateway）
    open_id "ou_xxx"            数字 user id 的字符串形式

**message_id 一律用 `"<chat_id>:<message_id>"` 复合 key**：Telegram 的 message_id
只在单个 chat 内唯一（两个群可以都有 message_id=42），而 dispatcher 的去重表
（_is_duplicate_event）和卡片锚点是全局的，不复合就会串台；而且 editMessageText
必须同时给 chat_id，光有 message_id 也发不出去。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from typing import Optional

import requests

import outbox
from tg_context import TgBuffer
from tg_md import SPLIT_LIMIT, render_html, split_md, tail_md

# Bot API 根地址。留出 env 覆盖是为了本机跑"假 Bot API"做端到端联调
# （tests/manual_tg_e2e.py 那个 harness），生产不要设。
API_ROOT = (os.environ.get("CC_TG_API_ROOT", "").strip().rstrip("/")
            or "https://api.telegram.org")

# 同一条消息两次 editMessageText 的最小间隔。dispatcher 的流式 push 是 0.4s 一帧、
# 心跳 1.5s 一帧，全打过去必定撞 Telegram 的 flood control（429）。中间帧丢了没关系
# ——心跳会在 1s 内再推一帧，收尾还有 update_card_final / finalize 强制落地。
_EDIT_MIN_INTERVAL = float(os.getenv("CC_TG_EDIT_MIN_INTERVAL", "1.5") or "1.5")
_HTTP_TIMEOUT = float(os.getenv("CC_TG_HTTP_TIMEOUT", "25") or "25")
# 单条消息的 edit 超时要**小于** dispatcher 的 _PUSH_TIMEOUT(20s)：不然 push 先超时
# 放锁、终态写进去了，那个还在飞的旧帧才落地，卡片被打回流式快照（Lark 侧的老病）。
_EDIT_TIMEOUT = float(os.getenv("CC_TG_EDIT_TIMEOUT", "8") or "8")
# 终态写之后隔一小会儿补一次同内容的确认写：万一有个更早发出的请求后到把正文改回去，
# 这次确认写会把终态盖回来（对应 FeishuClient 的 LARK_CARD_FINAL_CONFIRM_DELAY）。
_FINAL_CONFIRM_DELAY = float(os.getenv("CC_TG_FINAL_CONFIRM_DELAY", "0.8") or "0.8")
_LOADING_TEXT = "⏳ 思考中..."
# 按钮 token 的有效期 / 双击去重窗口。token 只在内存里，重启即失效（点了会提示重发）。
_CALLBACK_TTL_SEC = float(os.getenv("CC_TG_CALLBACK_TTL_SEC", "86400") or "86400")
_CALLBACK_DEDUP_SEC = float(os.getenv("CC_TG_CALLBACK_DEDUP_SEC", "3") or "3")


class TelegramApiError(RuntimeError):
    def __init__(self, action: str, code, description: str, retry_after: float = 0.0):
        self.action = action
        self.code = code
        self.description = description or ""
        self.retry_after = retry_after
        super().__init__(f"{action}: [{code}] {self.description}")


def _is_not_modified(exc: BaseException) -> bool:
    return (
        isinstance(exc, TelegramApiError)
        and "message is not modified" in (exc.description or "").lower()
    )


def _is_parse_error(exc: BaseException) -> bool:
    d = (getattr(exc, "description", "") or "").lower()
    return "can't parse entities" in d or "unsupported start tag" in d


def _safe_html(text: str) -> str:
    """渲染 markdown；渲染器自己炸了就退回纯文本。

    渲染只是"好看一点"，绝不能因为它抛异常就让整条回复发不出去（那是内容丢失）。
    """
    try:
        return render_html(text)
    except Exception as e:  # noqa: BLE001
        print(f"[tg] markdown 渲染异常，退回纯文本: {type(e).__name__}: {e}", flush=True)
        return text


def make_key(chat_id, message_id) -> str:
    return f"{chat_id}:{message_id}"


def new_thread_id(chat_id, message_id) -> str:
    """dispatch 顶楼 / 定时任务顶楼那条消息自成一条 thread 的 id。

    **必须带 chat 作用域**：message_id 只在单个 chat 内唯一，`t80` 这种裸 id 在两个
    群里会撞号，而上下文缓冲只按 thread 分桶（thread_context 拉历史时也不带 chat
    过滤）→ A 群说过的话会被当成历史喂进 B 群的 prompt。session key 是
    `<chat>:<thread>`，所以撞号时 session 不串、**串的是注入的上下文**，更隐蔽。
    """
    return f"t{chat_id}#{message_id}"


def split_key(key: str) -> tuple[str, str]:
    """`"-1001:42"` → `("-1001", "42")`。不是复合 key 就当纯 chat_id（消息 id 为空）。"""
    text = str(key or "")
    if ":" not in text:
        return text, ""
    chat_id, _, mid = text.rpartition(":")
    return chat_id, mid


class TelegramClient:
    """一个 Telegram bot 的运行时客户端。线程安全（HTTP 走 to_thread + per-thread session）。"""

    def __init__(self, token: str, *, label: str = "", buffer: Optional[TgBuffer] = None):
        self._token = token.strip()
        self.label = label or "tg"
        self.buffer = buffer if buffer is not None else TgBuffer(self.label)
        # FeishuClient 的两个下划线字段有外部读者：thread_context 用 _app_id 判"这条
        # 是不是我自己发的"，card_security 用 _app_secret 当 HMAC key。保持同名。
        self._app_id = ""
        self._app_secret = self._token
        self._domain = API_ROOT
        self._bot_id = 0
        self._bot_username = ""
        self._can_read_all_group_messages = False
        # 语音转写代理：Telegram 自己没有 ASR，若本进程里还有 Lark profile，就借它的
        # speech_to_text（同一台机器上的能力复用）。main.py 启动时注入。
        self.asr_client = None

        self._local = threading.local()
        self._lock = threading.RLock()
        # key → 已经渲染在 Telegram 上的 markdown 原文（去重，避免 not-modified 400）
        self._rendered: dict[str, str] = {}
        self._last_edit: dict[str, float] = {}
        # 被节流挡下来的最新一帧 + 负责把它补写回去的延迟任务。
        # 节流必须是"合并"而不是"丢弃"：像 handle_set_mode / handle_menu_command
        # 这种**一次性**更新（点完按钮改文案）背后没有心跳来重推，丢一帧就等于
        # 这条命令在 Telegram 上永远没反应。
        self._pending: dict[str, tuple[int, str]] = {}
        self._flush_tasks: dict[str, asyncio.Task] = {}
        # 每条消息一个单调序号：写请求在**调用入口**领号，真正发出前再比一次。
        # 这样"排在锁后面的旧帧"会被丢掉，而不是后到覆盖新内容。
        self._seq: dict[str, int] = {}
        self._applied: dict[str, int] = {}
        # 同一条消息同时只允许一个 edit 在飞（Telegram 不保证并发请求的应用顺序）
        self._edit_locks: dict[str, asyncio.Lock] = {}
        # 一条卡片超长时，续段消息的 key（重复收尾时不再重复发）
        self._continuations: dict[str, list[str]] = {}
        self._card_text_cache: dict[str, str] = {}
        self._CARD_CACHE_MAX = 500
        # 一条卡片对应 4 个 dict 里各一项。bot 一跑就是几周不重启，不设上限的话
        # 这些"早就发完的消息"的状态会一直堆着（Lark 侧同理，那边靠 _CARD_CACHE_MAX）。
        self._CARD_STATE_MAX = 500
        # callback_data 只有 64 字节，塞不下业务 value dict → 存服务端，只发短 token
        self._callbacks: dict[str, dict] = {}
        self._CALLBACK_MAX = 800

    # ── 底层 HTTP ─────────────────────────────────────────────
    @property
    def _session(self) -> requests.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            self._local.session = sess
        return sess

    def _url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self._token}/{method}"

    def reset_session(self) -> None:
        """丢掉本线程的连接池。

        长轮询撞 409 时必须做这一步：连接被复用时，读到的可能是**上一个** getUpdates
        的 409 响应（Telegram 收到新请求就用 409 终止旧的），于是"409 → 退避 → 又读到
        409"自锁，退避一路爬到 60s，入站彻底停。真机踩过：本机只有一个 bot 进程、
        没有 webhook，我自己 curl 一发就成功 —— 问题在连接不在竞争者。
        """
        sess = getattr(self._local, "session", None)
        if sess is not None:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass
            self._local.session = None

    def _post_sync(self, method: str, payload: dict, timeout: float,
                   fresh: bool = False) -> dict:
        # None 一律剔掉：Telegram 目前容忍 "offset": null，但那是运气不是契约
        # （poll_once 为了自己的长轮询超时会直接调这里，绕过 call() 的过滤）。
        payload = {k: v for k, v in (payload or {}).items() if v is not None}
        headers = None
        if fresh:
            # 长轮询：一次一条新连接、用完即弃。25s 一次的请求本来没有复用价值，
            # 而复用带来的响应错位会把整条入站打死（见 reset_session）。
            self.reset_session()
            headers = {"Connection": "close"}
        resp = self._session.post(self._url(method), json=payload, timeout=timeout,
                                  headers=headers)
        try:
            data = resp.json()
        except ValueError:
            raise TelegramApiError(method, resp.status_code, resp.text[:200])
        if not data.get("ok"):
            params = data.get("parameters") or {}
            raise TelegramApiError(
                method,
                data.get("error_code", resp.status_code),
                data.get("description", ""),
                float(params.get("retry_after") or 0),
            )
        return data.get("result")

    async def call(self, method: str, payload: Optional[dict] = None, *,
                   retries: int = 2, timeout: float = _HTTP_TIMEOUT):
        """调一次 Bot API。429 按 retry_after 等待重试，网络/5xx 指数退避。"""
        body = {k: v for k, v in (payload or {}).items() if v is not None}
        delay = 0.5
        last: Optional[BaseException] = None
        for attempt in range(retries + 1):
            try:
                return await asyncio.to_thread(self._post_sync, method, body, timeout)
            except TelegramApiError as e:
                last = e
                if e.retry_after and attempt < retries:
                    await asyncio.sleep(min(e.retry_after + 0.5, 30))
                    continue
                # 4xx（除 429）是业务错误，重试没意义
                if isinstance(e.code, int) and 400 <= e.code < 500 and e.code != 429:
                    raise
                if attempt >= retries:
                    raise
            except (requests.RequestException, OSError) as e:
                last = e
                if attempt >= retries:
                    raise TelegramApiError(method, "network", str(e)[:200]) from e
            await asyncio.sleep(delay)
            delay *= 2
        raise TelegramApiError(method, "unknown", str(last or "调用失败"))

    # ── 身份 ──────────────────────────────────────────────────
    async def get_me(self) -> dict:
        me = await self.call("getMe", retries=3) or {}
        self._bot_id = int(me.get("id") or 0)
        self._bot_username = me.get("username", "") or ""
        self._app_id = str(self._bot_id)
        self._can_read_all_group_messages = bool(me.get("can_read_all_group_messages"))
        name = me.get("first_name") or self._bot_username or "bot"
        self.buffer.remember_name(self._app_id, name)
        return me

    async def get_bot_open_id(self) -> Optional[str]:
        if not self._bot_id:
            try:
                await self.get_me()
            except Exception as e:
                print(f"[tg] getMe 失败: {e}", flush=True)
                return None
        return self._app_id or None

    @property
    def bot_username(self) -> str:
        return self._bot_username

    @property
    def bot_id(self) -> int:
        return self._bot_id

    @property
    def can_read_all_group_messages(self) -> bool:
        return self._can_read_all_group_messages

    # ── 卡片文本快照（thread_context 复用）────────────────────
    def _remember_card_text(self, key: str, content: str) -> None:
        if not key or not content:
            return
        with self._lock:
            if len(self._card_text_cache) >= self._CARD_CACHE_MAX:
                for k in list(self._card_text_cache.keys())[: self._CARD_CACHE_MAX // 4]:
                    self._card_text_cache.pop(k, None)
            self._card_text_cache[key] = content

    def get_card_text(self, message_id: str) -> str:
        # 形参名必须和 FeishuClient.get_card_text 一致：thread_context 是按关键字调的
        with self._lock:
            return self._card_text_cache.get(message_id, "")

    def save_outbox(self, content: str, *, kind: str = "result", error: str = "",
                    meta: Optional[dict] = None) -> Optional[str]:
        return outbox.record(self.label, content, kind=kind, error=error, meta=meta)

    # ── 按钮 ──────────────────────────────────────────────────
    def _register_callback(self, value: dict, key: str) -> str:
        token = secrets.token_urlsafe(9)
        with self._lock:
            if len(self._callbacks) >= self._CALLBACK_MAX:
                for k in list(self._callbacks.keys())[: self._CALLBACK_MAX // 4]:
                    self._callbacks.pop(k, None)
            self._callbacks[token] = {
                "value": dict(value or {}),
                "card": key,
                "ts": time.time(),
            }
        return token

    def resolve_callback(self, token: str, *, card_key: str = "",
                         dedup_window: float = _CALLBACK_DEDUP_SEC) -> Optional[dict]:
        """按 token 取回业务 value，并做三道校验（Lark 侧对应 HMAC + TTL + claim_event）：

        1. TTL：太老的按钮不认（重启后表是空的，本来就会回"已过期"）；
        2. 卡片绑定：token 只能在它自己那条消息上点，防止把别处的按钮值搬过来；
        3. 双击去重：手滑连点两下不能把 `/new` 这种副作用命令跑两遍。
        """
        now = time.time()
        with self._lock:
            entry = self._callbacks.get(token or "")
            if entry is None:
                return None
            if now - entry.get("ts", 0) > _CALLBACK_TTL_SEC:
                self._callbacks.pop(token, None)
                return None
            bound = entry.get("card", "")
            if card_key and bound and bound != card_key:
                return None
            if now - entry.get("used_at", 0) < dedup_window:
                return None
            entry["used_at"] = now
            return entry

    def _rebind_callbacks(self, keyboard: Optional[dict], card_key: str) -> None:
        """按钮是在消息发出**之前**建的，那时还不知道消息 id，发完回填绑定关系。"""
        if not keyboard or not card_key:
            return
        with self._lock:
            for row in keyboard.get("inline_keyboard") or []:
                for cell in row:
                    entry = self._callbacks.get(cell.get("callback_data", ""))
                    if entry is not None:
                        entry["card"] = card_key

    def _keyboard(self, buttons: Optional[list[dict]], key: str, flow: bool) -> Optional[dict]:
        """把 Lark 口径的 [{"text","value"}] 转成 inline_keyboard。

        flow=True（Lark 语义是"短按钮横排"）→ 每行 3 个；否则每行 1 个。
        """
        rows: list[list[dict]] = []
        per_row = 3 if flow else 1
        for btn in buttons or []:
            text = str(btn.get("text", "") or "·")[:64]
            token = self._register_callback(btn.get("value") or {}, key)
            cell = {"text": text, "callback_data": token}
            if not rows or len(rows[-1]) >= per_row:
                rows.append([cell])
            else:
                rows[-1].append(cell)
        return {"inline_keyboard": rows} if rows else None

    # ── 发送 / 编辑 ───────────────────────────────────────────
    async def _send_chunk(self, chat_id: str, text: str, *, reply_to: str = "",
                          keyboard: Optional[dict] = None,
                          message_thread_id: Optional[int] = None) -> str:
        payload = {
            "chat_id": chat_id,
            "text": _safe_html(text),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": keyboard,
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = int(message_thread_id)
        if reply_to:
            # allow_sending_without_reply：被回复的消息被删了也别整条发不出去
            payload["reply_parameters"] = {
                "message_id": int(reply_to),
                "allow_sending_without_reply": True,
            }
        try:
            result = await self.call("sendMessage", payload)
        except TelegramApiError as e:
            if not _is_parse_error(e):
                raise
            print(f"[tg] HTML 解析失败，退回纯文本: {e.description[:120]}", flush=True)
            payload["text"] = text
            payload.pop("parse_mode", None)
            result = await self.call("sendMessage", payload)
        return make_key(result["chat"]["id"], result["message_id"])

    def _prune_card_state(self) -> None:
        """卡片状态表按插入序裁掉最早的（dict 保序）。只影响老消息的节流/去重。

        不能只跟着 `_rendered` 裁：卡片被用户删掉之后每次 edit 都 400，这个 key
        永远进不了 `_rendered`，却会在 `_last_edit` / `_seq` 里留痕 —— bot 跑几周
        这几张表就只涨不减。所以每张表各自收口。
        """
        with self._lock:
            drop = (list(self._rendered.keys())[: self._CARD_STATE_MAX // 4]
                    if len(self._rendered) >= self._CARD_STATE_MAX else [])
            for key in drop:
                self._rendered.pop(key, None)
                self._last_edit.pop(key, None)
                self._pending.pop(key, None)
                self._continuations.pop(key, None)
                self._seq.pop(key, None)
                self._applied.pop(key, None)
                task = self._flush_tasks.pop(key, None)
                if task is not None and not task.done():
                    task.cancel()
                lock = self._edit_locks.get(key)
                if lock is not None and not lock.locked():
                    self._edit_locks.pop(key, None)
            # 这几张表的 key 可能不在 _rendered 里（只被节流/领号碰过），单独收口
            for table in (self._last_edit, self._pending, self._seq, self._applied):
                # 裁到 MAX-1：这次调用之后马上会再插一条，插完仍然 ≤ MAX
                if len(table) >= self._CARD_STATE_MAX:
                    excess = len(table) - self._CARD_STATE_MAX + 1
                    for key in list(table.keys())[:excess]:
                        table.pop(key, None)
            for key, task in list(self._flush_tasks.items()):
                if task.done():
                    self._flush_tasks.pop(key, None)

    def _issue_and_prune(self, message_id: str) -> int:
        """领号顺手收一次状态表（长度检查是 O(1)，超了才真裁）。"""
        seq = self._issue(message_id)
        self._prune_card_state()
        return seq

    async def _send(self, chat_id: str, content: str, *, reply_to: str = "",
                    buttons: Optional[list[dict]] = None, flow: bool = False,
                    record: bool = True, as_new_thread: bool = False) -> str:
        text = content if (content or "").strip() else _LOADING_TEXT
        chunks = split_md(text) or [text]
        first_key = ""
        extra: list[str] = []
        last_keyboard: Optional[dict] = None

        topic_id: Optional[int] = None
        if reply_to:
            thread = self.buffer.thread_of(make_key(chat_id, reply_to))
            if thread and thread.startswith(f"f{chat_id}#"):
                try:
                    topic_id = int(thread.split("#")[-1])
                except (ValueError, TypeError):
                    pass

        for i, chunk in enumerate(chunks):
            keyboard = None
            if i == len(chunks) - 1 and buttons:
                keyboard = self._keyboard(buttons, first_key, flow)
                last_keyboard = keyboard
            try:
                key = await self._send_chunk(
                    chat_id, chunk,
                    reply_to=reply_to if i == 0 else "",
                    keyboard=keyboard,
                    message_thread_id=topic_id,
                )
            except Exception as e:
                # 第一段就失败 → 上层需要知道（它会走"发送失败"兜底）。
                # 后续段失败 → 第一段已经发出去了，绝不能把异常抛上去让上层整条重发
                # （那会重复刷屏），记一笔日志、把已发的段收好即可。
                if i == 0:
                    raise
                print(f"[tg] 第 {i + 1} 段发送失败（前面的段已发出）: {e}", flush=True)
                break
            if i == 0:
                first_key = key
            else:
                extra.append(key)
        self._rebind_callbacks(last_keyboard, first_key)
        with self._lock:
            # 故意不写 _last_edit：占位卡之后的第一帧要立刻落地（否则用户会先干等
            # 一个节流间隔才看到 Claude 开口），节流只管"连续帧"。
            self._rendered[first_key] = chunks[0]
            if extra:
                self._continuations[first_key] = extra
        self._remember_card_text(first_key, text)
        self._prune_card_state()
        if record:
            self._record_outgoing(
                first_key, text, reply_to=reply_to, as_new_thread=as_new_thread)
        return first_key

    def _record_outgoing(self, key: str, text: str, reply_to: str = "",
                         as_new_thread: bool = False) -> None:
        """把 bot 自己发的消息记进缓冲——Telegram 不会把 bot 自己的消息推回来，
        不自己记的话 /new 之后的新 session 就完全看不到 bot 之前说过什么。

        as_new_thread：这条消息是"顶楼"（dispatch_task / 定时任务的锚点），要自成
        一条 thread。Lark 里往群里发一条新消息天然开一条 thread，Telegram 没有这个
        概念，就靠这里给它一个 `t<message_id>` 的合成 thread —— 子会话的 session、
        read_thread 的 transcript、以及"回复它就落回子会话"全靠这个 id 隔离。
        """
        chat_id, mid = split_key(key)
        thread = new_thread_id(chat_id, mid) if as_new_thread else ""
        if not thread and reply_to:
            thread = self.buffer.thread_of(make_key(chat_id, reply_to))
        self.buffer.record(
            message_id=key,
            chat_id=chat_id,
            thread_id=thread or f"c{chat_id}",
            user_id=self._app_id or str(self._bot_id),
            name=self._bot_username or "bot",
            is_bot=True,
            msg_type="text",
            content=json.dumps({"text": text}, ensure_ascii=False),
            ts_ms=int(time.time() * 1000),
            reply_to=make_key(chat_id, reply_to) if reply_to else "",
        )

    async def send_card_to_user(self, open_id: str, content: str = "",
                                loading: bool = True) -> str:
        return await self._send(str(open_id), content or (_LOADING_TEXT if loading else ""))

    async def reply_card(self, message_id: str, content: str = "",
                         loading: bool = True) -> str:
        chat_id, mid = split_key(message_id)
        return await self._send(
            chat_id, content or (_LOADING_TEXT if loading else ""), reply_to=mid,
        )

    async def _edit(self, key: str, text: str, *, keyboard: Optional[dict] = None,
                    keep_keyboard: bool = False, seq: Optional[int] = None,
                    lock_timeout: Optional[float] = None) -> bool:
        """改写一条消息。返回是否真的发了请求（False = 被更新的写抢先，主动作废）。

        seq：调用入口领的单调号。等锁期间若有更大的号已经落地，这一帧就是旧内容，
        直接丢弃 —— 否则它会把终态卡片打回流式快照。
        """
        chat_id, mid = split_key(key)
        if not mid:
            raise TelegramApiError("editMessageText", "bad_key", f"非法卡片 key: {key!r}")
        if seq is None:
            seq = self._issue(key)
        lock = self._edit_lock(key)
        acquired = False
        try:
            if lock_timeout is None:
                await lock.acquire()
                acquired = True
            else:
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=lock_timeout)
                    acquired = True
                except asyncio.TimeoutError:
                    # 终态写不能被一个卡住的旧请求无限期堵死：硬发出去，
                    # 后面还有确认写兜底顺序。
                    print(f"[tg] 等 edit 锁超时，终态写强行发出 key={key}", flush=True)
            with self._lock:
                if seq < self._applied.get(key, 0):
                    return False
            sent = await self._edit_request(key, text, keyboard, keep_keyboard)
            with self._lock:
                self._applied[key] = max(self._applied.get(key, 0), seq)
            return sent
        finally:
            if acquired:
                lock.release()

    async def _edit_request(self, key: str, text: str, keyboard: Optional[dict],
                            keep_keyboard: bool) -> bool:
        chat_id, mid = split_key(key)
        payload = {
            "chat_id": chat_id,
            "message_id": int(mid),
            "text": _safe_html(text),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if keyboard is not None or not keep_keyboard:
            payload["reply_markup"] = keyboard
        try:
            await self.call("editMessageText", payload,
                            retries=1, timeout=_EDIT_TIMEOUT)
        except TelegramApiError as e:
            if _is_not_modified(e):
                # 正文没变但按钮要挂：editMessageText 会被 Telegram 拒（not modified），
                # 得改走 editMessageReplyMarkup，否则按钮永远出不来（/mode /model
                # /resume 这些命令在 Telegram 上就等于废了）。
                if keyboard is not None:
                    await self._edit_markup(key, keyboard)
                return True
            if not _is_parse_error(e):
                raise
            print(f"[tg] HTML 解析失败，退回纯文本: {e.description[:120]}", flush=True)
            payload["text"] = text
            payload.pop("parse_mode", None)
            try:
                await self.call("editMessageText", payload,
                                retries=1, timeout=_EDIT_TIMEOUT)
            except TelegramApiError as e2:
                if not _is_not_modified(e2):
                    raise
        return True

    async def _edit_markup(self, key: str, keyboard: Optional[dict]) -> None:
        chat_id, mid = split_key(key)
        await self.call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": int(mid),
            "reply_markup": keyboard,
        })

    def _issue(self, message_id: str) -> int:
        with self._lock:
            nxt = self._seq.get(message_id, 0) + 1
            self._seq[message_id] = nxt
            return nxt

    def _edit_lock(self, message_id: str) -> asyncio.Lock:
        with self._lock:
            lock = self._edit_locks.get(message_id)
            if lock is None:
                lock = asyncio.Lock()
                self._edit_locks[message_id] = lock
            return lock

    def _cancel_flush(self, message_id: str) -> None:
        """终态写之前先撤掉延迟补帧，避免旧内容盖掉最终结果。"""
        task = self._flush_tasks.pop(message_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_flush(self, message_id: str, delay: float) -> None:
        task = self._flush_tasks.get(message_id)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有事件循环（同步调用方）→ 交给心跳 / 收尾兜底
        self._flush_tasks[message_id] = loop.create_task(
            self._flush_later(message_id, delay))

    async def _flush_later(self, message_id: str, delay: float) -> None:
        """节流窗口过去之后，把攒着的最新一帧写出去（还在攒就继续等下一窗口）。"""
        try:
            await asyncio.sleep(max(0.0, delay) + 0.05)
            while True:
                with self._lock:
                    item = self._pending.pop(message_id, None)
                    if item is None:
                        return
                    seq, text = item
                    if self._rendered.get(message_id) == text:
                        return
                    self._last_edit[message_id] = time.time()
                try:
                    ok = await self._edit(
                        message_id, text, keep_keyboard=True, seq=seq)
                except Exception as e:  # noqa: BLE001 — 补帧失败不影响主流程
                    print(f"[tg] 补帧失败（忽略） key={message_id}: {e}", flush=True)
                    return
                with self._lock:
                    if ok:
                        self._rendered[message_id] = text
                    if message_id not in self._pending:
                        return
                await asyncio.sleep(_EDIT_MIN_INTERVAL)
        except asyncio.CancelledError:
            raise
        finally:
            # 注意：这里不能用 `task.done()` 当判据 —— 自己还在 finally 里跑，
            # done() 必然是 False，老写法等于永不回收。按身份比对才对。
            me = asyncio.current_task()
            with self._lock:
                if self._flush_tasks.get(message_id) is me:
                    self._flush_tasks.pop(message_id, None)

    async def update_card(self, message_id: str, content: str):
        """一帧更新。间隔不够就攒着、由延迟任务补写（合并，不丢弃）。"""
        text = tail_md(content or "")
        seq = self._issue_and_prune(message_id)
        now = time.time()
        with self._lock:
            if self._rendered.get(message_id) == text:
                return
            last = self._last_edit.get(message_id, 0.0)
            wait = _EDIT_MIN_INTERVAL - (now - last)
            if wait > 0:
                self._pending[message_id] = (seq, text)
                self._schedule_flush(message_id, wait)
                return
            self._last_edit[message_id] = now
            self._pending.pop(message_id, None)
        if await self._edit(message_id, text, keep_keyboard=True, seq=seq):
            with self._lock:
                self._rendered[message_id] = text
            self._remember_card_text(message_id, content or "")

    async def update_card_final(self, message_id: str, content: str):
        """终态：不受节流限制，超长自动续段，重复收尾幂等且**内容感知**。"""
        text = content or ""
        chunks = split_md(text) or [text]
        seq = self._issue_and_prune(message_id)
        self._cancel_flush(message_id)
        with self._lock:
            self._pending.pop(message_id, None)
            self._last_edit[message_id] = time.time()
            same = self._rendered.get(message_id) == chunks[0]
        if not same:
            if await self._edit(message_id, chunks[0], keep_keyboard=True,
                                seq=seq, lock_timeout=_EDIT_TIMEOUT + 2):
                with self._lock:
                    self._rendered[message_id] = chunks[0]
        self._remember_card_text(message_id, text)
        self.buffer.update_text(message_id, text)
        await self._sync_continuations(message_id, chunks[1:])
        await self._confirm_final(message_id, chunks[0], seq)

    async def _confirm_final(self, message_id: str, text: str, seq: int) -> None:
        """终态确认写：隔一小会儿把同样的内容再写一次。

        目的不是"再显示一遍"，而是**当最后一次写**。万一有个更早发出、还在飞的
        请求后到把正文改回流式快照，这次写就把终态盖回来；正常情况下 Telegram 直接
        回 not modified，无副作用。
        """
        if _FINAL_CONFIRM_DELAY <= 0:
            return
        await asyncio.sleep(_FINAL_CONFIRM_DELAY)
        try:
            # 沿用终态那一次的号（不领新号）：这 0.8s 里如果有**更新**的写落地了
            # （比如用户点按钮改了这条卡片），确认写就该自动作废，不能把它盖回去。
            await self._edit(message_id, text, keep_keyboard=True, seq=seq,
                             lock_timeout=_EDIT_TIMEOUT + 2)
        except Exception as e:  # noqa: BLE001
            print(f"[tg] 终态确认写失败（忽略） key={message_id}: {e}", flush=True)

    async def _sync_continuations(self, message_id: str, rest: list[str]) -> None:
        """把超长正文的续段消息同步成 rest。

        幂等必须是**内容感知**的：第二次收尾（错误路径 / 自动续跑改了正文）如果只看
        "已经发过续段就跳过"，用户看到的就是"新的头 + 旧的尾"。所以已存在的续段
        逐条 edit 成新内容，多了就补发、少了就把多余那条改成一句说明。
        """
        chat_id, mid = split_key(message_id)
        with self._lock:
            existing = list(self._continuations.get(message_id, []))
        updated: list[str] = []
        anchor = mid
        for i, chunk in enumerate(rest):
            if i < len(existing):
                key = existing[i]
                try:
                    await self._edit(key, chunk, keep_keyboard=True)
                    with self._lock:
                        self._rendered[key] = chunk
                except Exception as e:  # noqa: BLE001
                    print(f"[tg] 续段更新失败（忽略）: {e}", flush=True)
                updated.append(key)
                anchor = split_key(key)[1]
                continue
            try:
                key = await self._send_chunk(chat_id, chunk, reply_to=anchor)
            except Exception as e:  # noqa: BLE001
                print(f"[tg] 续段发送失败（忽略）: {e}", flush=True)
                break
            updated.append(key)
            anchor = split_key(key)[1]
        for stale in existing[len(rest):]:
            try:
                await self._edit(stale, "（这一段已被更新，见上文）", keep_keyboard=True)
            except Exception:  # noqa: BLE001 — 收尾清理，失败无所谓
                pass
        if updated or existing:
            with self._lock:
                if updated:
                    self._continuations[message_id] = updated
                else:
                    self._continuations.pop(message_id, None)

    async def finalize_streaming_card(self, message_id: str,
                                      buttons: Optional[list[dict]] = None,
                                      flow: bool = False):
        """收尾：把被节流丢掉的最后一帧补上，可选挂按钮。永不抛异常。"""
        try:
            self._cancel_flush(message_id)
            with self._lock:
                item = self._pending.pop(message_id, None)
            if item is not None and item[1] != self._rendered.get(message_id):
                seq, pending = item
                if await self._edit(message_id, pending, keep_keyboard=True, seq=seq):
                    with self._lock:
                        self._rendered[message_id] = pending
                        self._last_edit[message_id] = time.time()
            if buttons:
                keyboard = self._keyboard(buttons, message_id, flow)
                chat_id, mid = split_key(message_id)
                await self.call("editMessageReplyMarkup", {
                    "chat_id": chat_id,
                    "message_id": int(mid),
                    "reply_markup": keyboard,
                })
        except Exception as e:
            print(f"[tg] finalize 失败（忽略） key={message_id}: {e}", flush=True)

    async def update_card_with_buttons(self, message_id: str, content: str,
                                       buttons: list[dict], flow: bool = False):
        text = content or ""
        chunks = split_md(text) or [text]
        keyboard = self._keyboard(buttons, message_id, flow)
        seq = self._issue(message_id)
        self._cancel_flush(message_id)
        with self._lock:
            self._pending.pop(message_id, None)
            self._last_edit[message_id] = time.time()
            unchanged = self._rendered.get(message_id) == chunks[0]
        if unchanged:
            # 典型路径：斜杠命令先 reply 一条带正文的消息，再补按钮。正文一模一样，
            # editMessageText 必然被判 not modified，直接改按钮。
            await self._edit_markup(message_id, keyboard)
        else:
            await self._edit(message_id, chunks[0], keyboard=keyboard, seq=seq,
                             lock_timeout=_EDIT_TIMEOUT + 2)
        with self._lock:
            self._rendered[message_id] = chunks[0]
        self._remember_card_text(message_id, text)
        self.buffer.update_text(message_id, text)
        await self._sync_continuations(message_id, chunks[1:])

    async def update_card_elements(self, message_id: str, elements: list[dict]):
        """Lark 的 elements 混排（markdown + 按钮）→ Telegram 文本 + inline keyboard。"""
        texts: list[str] = []
        buttons: list[dict] = []

        def visit(node):
            if isinstance(node, list):
                for item in node:
                    visit(item)
                return
            if not isinstance(node, dict):
                return
            tag = node.get("tag", "")
            if tag == "markdown":
                c = (node.get("content") or "").strip()
                if c:
                    texts.append(c)
            elif tag == "div":
                c = ((node.get("text") or {}).get("content") or "").strip()
                if c:
                    texts.append(c)
            elif tag == "button":
                label = ((node.get("text") or {}).get("content") or "").strip()
                value = node.get("value")
                if not isinstance(value, dict):
                    for behavior in node.get("behaviors") or []:
                        if behavior.get("type") == "callback" and isinstance(behavior.get("value"), dict):
                            value = behavior["value"]
                            break
                if label:
                    buttons.append({"text": label, "value": value or {}})
            elif tag in ("column_set", "column"):
                visit(node.get("columns") or node.get("elements") or [])
            else:
                visit(node.get("elements") or [])

        visit(elements)
        # 菜单类卡片按钮很多，横排 3 个一行才不至于刷屏
        await self.update_card_with_buttons(
            message_id, "\n".join(texts) or "·", buttons, flow=True,
        )

    # ── 纯文本 / post ─────────────────────────────────────────
    # dispatcher 的带外提示有几条是"裸 emoji"（收尾的 ✅）。Telegram 对**只含 emoji**
    # 的消息会放大到贴纸尺寸，手机上占掉半屏（真机实测），所以给它们配个词 ——
    # 顺带更有信息量：editMessageText 不推通知，这条 ping 才是通知载体。
    _EMOJI_ONLY_LABEL = {
        "✅": "✅ 完成",
        "⚠️": "⚠️ 有异常",
        "⏹": "⏹ 已停止",
        "❌": "❌ 出错了",
    }

    async def _send_plain(self, chat_id: str, text: str, reply_to: str = "") -> str:
        """纯文本走的都是"带外提示"（✅ 收尾 / 📬 排队中 / ❌ 报错 / ♻️ 重启公告）。

        故意 **不记进上下文缓冲**：它们不是对话内容，记了只会在下一轮的
        「话题新增」里堆出一串 ✅ / 排队提示，白占 prompt 又干扰模型。真正的回答走
        reply_card / update_card_final，那些是记的。
        """
        text = self._EMOJI_ONLY_LABEL.get((text or "").strip(), text)
        key = await self._send(chat_id, text, reply_to=reply_to, record=False)
        # 正文不记（不然下一轮上下文里全是 ✅），但 **thread 归属要记** ——
        # 用户回复这条 "✅ 完成" 时，得能认出它属于哪条子会话，否则回复会掉回群主线。
        if reply_to:
            thread = self.buffer.thread_of(make_key(chat_id, reply_to))
            if thread:
                self.buffer.remember_thread(key, thread)
        return key

    async def reply_text(self, message_id: str, text: str) -> str:
        chat_id, mid = split_key(message_id)
        return await self._send_plain(chat_id, text, reply_to=mid)

    async def send_text_to_user(self, open_id: str, text: str) -> str:
        return await self._send_plain(str(open_id), text)

    async def send_post_to_chat(self, chat_id: str, title: str, body_text: str,
                                mention_open_id: str = "") -> str:
        mention = ""
        if mention_open_id:
            name = self.buffer.name_of(mention_open_id) or "you"
            mention = f'[{name}](tg://user?id={mention_open_id}) '
        return await self._send(
            str(chat_id), f"**{title}**\n{mention}{body_text}", as_new_thread=True)

    async def reply_post(self, message_id: str, title: str, body_text: str) -> str:
        chat_id, mid = split_key(message_id)
        return await self._send(chat_id, f"**{title}**\n{body_text}", reply_to=mid)

    # ── 附件 ──────────────────────────────────────────────────
    async def download_image(self, message_id: str, image_key: str) -> str:
        return await asyncio.to_thread(self._download_sync, image_key, "image", "")

    async def download_file(self, message_id: str, file_key: str,
                            msg_type: str = "file", file_name: str = "") -> str:
        return await asyncio.to_thread(self._download_sync, file_key, msg_type, file_name)

    def _download_sync(self, file_id: str, msg_type: str, file_name: str) -> str:
        info = self._post_sync("getFile", {"file_id": file_id}, _HTTP_TIMEOUT)
        remote = (info or {}).get("file_path") or ""
        if not remote:
            raise TelegramApiError("getFile", "no_path", "Telegram 未返回 file_path")
        ext = os.path.splitext(remote)[1] or (".jpg" if msg_type == "image" else ".bin")
        if file_name:
            safe = os.path.basename(file_name).replace("/", "_")[:120]
            local = os.path.join(tempfile.gettempdir(), f"tg-{uuid.uuid4().hex[:6]}-{safe}")
        else:
            local = os.path.join(
                tempfile.gettempdir(), f"tg-{msg_type}-{uuid.uuid4().hex[:8]}{ext}")
        url = f"{API_ROOT}/file/bot{self._token}/{remote}"
        with self._session.get(url, stream=True, timeout=120) as r:
            if r.status_code != 200:
                raise TelegramApiError("download", r.status_code, r.text[:200])
            with open(local, "wb") as f:
                for block in r.iter_content(64 * 1024):
                    if block:
                        f.write(block)
        return local

    async def speech_to_text(self, audio_path: str, file_id: str = "") -> str:
        """Telegram 没有 ASR。若本进程里有 Lark profile，就借它的语音识别接口。"""
        if self.asr_client is None:
            raise RuntimeError(
                "Telegram 语音转写未启用：本进程没有可借用的 Lark ASR 通道"
                "（配 CC_TG_ASR_PROFILE 指向一个 Lark profile，或改用文字）"
            )
        return await self.asr_client.speech_to_text(audio_path, file_id=file_id)

    # ── 话题（合成）───────────────────────────────────────────
    async def get_message_thread_id(self, message_id: str) -> str:
        """Telegram 没有 thread。缓冲里记过就返回它的合成 thread，否则给这条消息
        自成一条 thread（dispatch_task 拿它当子会话的隔离键）。"""
        known = self.buffer.thread_of(message_id)
        if known:
            return known
        chat_id, mid = split_key(message_id)
        return new_thread_id(chat_id, mid) if mid else ""

    async def list_thread_messages(self, thread_id: str, limit: int = 200) -> list:
        return self.buffer.thread_messages(thread_id, limit=limit)

    async def batch_resolve_names(self, open_ids: list[str]) -> dict[str, str]:
        return self.buffer.names(list(open_ids or []))

    # ── 命令菜单 ──────────────────────────────────────────────
    # Telegram 客户端左下角那个 "/" 菜单来自 setMyCommands（BotFather 的
    # /setcommands 是同一个接口）。启动时注册一次，装完就有菜单可点，不用手工贴。
    # 只放"裸命令"——带参数的（如 `/new plan`）Telegram 的命令格式不认。
    MENU_COMMANDS = (
        ("new", "开一条新会话"),
        ("resume", "恢复历史会话"),
        ("stop", "停止当前任务"),
        ("model", "切换模型"),
        ("effort", "切换推理强度"),
        ("mode", "切换权限模式"),
        ("ws", "查看 / 设置本会话的工作目录"),
        ("status", "当前会话状态"),
        ("usage", "Claude 用量"),
        ("skills", "可用 skills"),
        ("mcp", "MCP 服务器"),
        ("ls", "列出工作目录"),
        ("help", "命令帮助"),
        ("restart", "重启 bot 服务"),
    )

    async def set_my_commands(self) -> None:
        """注册斜杠命令菜单。失败只 log —— 菜单没了不影响任何功能。"""
        try:
            await self.call("setMyCommands", {
                "commands": [
                    {"command": name, "description": desc}
                    for name, desc in self.MENU_COMMANDS
                ],
            }, retries=1)
        except Exception as e:
            print(f"[tg] setMyCommands 失败（忽略）: {e}", flush=True)

    # ── callback 回执 ─────────────────────────────────────────
    async def answer_callback(self, callback_id: str, text: str = "",
                              alert: bool = False) -> None:
        try:
            await self.call("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": (text or "")[:200],
                "show_alert": alert,
            }, retries=0)
        except Exception as e:
            print(f"[tg] answerCallbackQuery 失败（忽略）: {e}", flush=True)
