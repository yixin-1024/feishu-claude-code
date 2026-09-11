"""外部群监听器主循环。

bot 账号进不了外部群，所以整条链路都借机主本人的 user token（lark-cli）走：
轮询白名单群 → 命中 @ → 跑本地 agent → 以机主身份回到话题里。

⚠️ 话题群的坑（这个模块存在的主要理由）：
`im +chat-messages-list` 在话题群里返回的是**话题根消息**，话题内的回复被套在
根消息的 `thread_replies` 数组里。只遍历顶层 messages 的话，"别人在已有话题里
@ 你"这个最常见的场景**永远扫不到**（根消息 id 没变，早在已读集合里）。
所以这里先把 root + thread_replies 拍平成一条时间线再判定。
"""

from __future__ import annotations

import asyncio
import os
from typing import Iterable, Optional

from log_util import log

from external_watcher.config import ExternalWatcherConfig, GroupConfig
from external_watcher.dispatcher_bridge import ExternalSendBlocked, handle_external_message
from external_watcher.lark_user_api import LarkCliError, LarkUserApi
from external_watcher.session_manager import SessionManager
from external_watcher.state import WatcherState

TAG = "ext-web"

# 只有这两类消息会触发 agent。图片/文件走 post，纯文本走 text；
# 系统消息（改群名、拉人）、卡片、表情回复都不该唤醒。
TRIGGER_MSG_TYPES = {"text", "post"}
# 拉群消息失败后的退避，避免网络抖动时把 lark-cli 打爆。
ERROR_BACKOFF_SEC = 15.0


def flatten_thread_messages(roots: Iterable[dict]) -> list[dict]:
    """把 [根消息{thread_replies:[...]}] 拍平成一条按时间排好的消息列表。"""
    flat: list[dict] = []
    seen: set[str] = set()

    def _add(m: dict, parent: Optional[dict] = None) -> None:
        mid = str((m or {}).get("message_id") or "")
        if not mid or mid in seen:
            return
        seen.add(mid)
        # 回复里 chat_id / thread_id 一般都带了；带不全时从根消息补，
        # 否则后面回消息时会挂不到正确的话题上。
        if parent:
            m.setdefault("chat_id", parent.get("chat_id"))
            if not m.get("thread_id"):
                m["thread_id"] = parent.get("thread_id")
        flat.append(m)

    for root in roots or []:
        if not root:
            continue
        _add(root)
        for reply in root.get("thread_replies") or []:
            _add(reply, parent=root)

    def _sort_key(m: dict):
        pos = m.get("message_position")
        try:
            pos_i = int(pos)
        except (TypeError, ValueError):
            pos_i = 0
        return (str(m.get("create_time") or ""), pos_i)

    flat.sort(key=_sort_key)
    return flat


def mentions_owner(msg: dict, owner_open_id: str, owner_name: str) -> bool:
    """只认**结构化 mentions**，不做正文里的 `@名字` 文本匹配。

    文本匹配是个死循环陷阱：agent 代发的回复里只要出现"@机主"这几个字
    （引用别人原话时极容易发生），下一轮就会把自己的回复当成新的 @ 再跑一遍。
    真正的 @ 一定在 mentions 数组里，认它就够了。
    """
    for men in msg.get("mentions") or []:
        men = men or {}
        mid = str(men.get("id") or "")
        if owner_open_id and mid == owner_open_id:
            return True
        # 没解析到 open_id 时（极少数场景）退回按名字比，但绝不看正文
        if not mid and owner_name and str(men.get("name") or "") == owner_name:
            return True
    return False


class ExternalWebWatcher:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = ExternalWatcherConfig.load(config_path)
        self.session_mgr = SessionManager(self.config.session_dir, self.config.lark_domain)
        self.state = WatcherState(self.config.state_path)
        self.api = LarkUserApi(self.config.user_profile)
        self.owner_open_id = self.config.owner_open_id
        self.owner_name = self.config.owner_name
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self._browser_task: Optional[asyncio.Task] = None
        self._inflight: set[asyncio.Task] = set()
        # 发送被 Lark 永久拒绝（230027）的群。进程内熔断，不落盘——
        # 后台开了外部共享 / 换了方案之后，重启一次就恢复。
        self._send_blocked: set[str] = set()
        self._thread_locks: dict[str, asyncio.Lock] = {}
        # 在 start() 里建：__init__ 是在主线程跑的，而循环活在 bot_loop 上，
        # 同步原语一律留到进了目标事件循环再创建。
        self._sem: Optional[asyncio.Semaphore] = None

    # ── 生命周期 ─────────────────────────────────────────────
    async def start(self) -> None:
        if not self.config.enabled:
            log(TAG, "watcher", "info", "外部群监听器未启用 (enabled=false)")
            return
        if not self.config.groups:
            log(TAG, "watcher", "warn", "未配置任何外部群监听白名单")
            return

        self.running = True
        self._sem = asyncio.Semaphore(max(1, self.config.max_concurrent))
        log(TAG, "watcher", "info",
            f"启动外部群监听器：{len(self.config.groups)} 个群，"
            f"轮询 {self.config.poll_interval_sec}s，profile={self.config.user_profile}")
        self._task = asyncio.create_task(self._run_loop())
        if self.config.browser_listener:
            self._browser_task = asyncio.create_task(self._run_browser_listener())

    async def stop(self) -> None:
        self.running = False
        for t in (self._task, self._browser_task):
            if t and not t.done():
                t.cancel()
        for t in list(self._inflight):
            if not t.done():
                t.cancel()
        self.state.save()

    # ── 主循环 ───────────────────────────────────────────────
    async def _resolve_owner(self) -> bool:
        """确认 user 身份可用并拿到 open_id。拿不到就没法判 @，直接不启动。"""
        try:
            open_id, name = await self.api.whoami()
        except Exception as e:
            log(TAG, "watcher", "error",
                f"profile {self.config.user_profile} 的 user 身份不可用，监听器不启动: {e}")
            return False
        if not self.owner_open_id:
            self.owner_open_id = open_id
        if name and not self.config.owner_open_id:
            self.owner_name = name or self.owner_name
        log(TAG, "watcher", "info",
            f"机主身份: {self.owner_name} ({self.owner_open_id[:16]}…)")
        return True

    async def _run_loop(self) -> None:
        if not await self._resolve_owner():
            self.running = False
            return

        while self.running:
            try:
                await self._poll_once()
                await asyncio.sleep(self.config.poll_interval_sec)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(TAG, "watcher", "error", f"轮询循环异常: {e}")
                await asyncio.sleep(ERROR_BACKOFF_SEC)

    async def _poll_once(self) -> None:
        for group in self.config.groups:
            if not self.running:
                return
            try:
                await self._check_group(group)
            except LarkCliError as e:
                log(TAG, "poll", "warn", f"读群 {group.chat_id[:16]}… 失败: {e}")
            except Exception as e:
                log(TAG, "poll", "warn", f"检查群 {group.chat_id[:16]}… 异常: {e}")

    async def _check_group(self, group: GroupConfig) -> None:
        roots = await self.api.list_chat_messages(
            group.chat_id, page_size=self.config.page_size, order="desc",
        )
        msgs = flatten_thread_messages(roots)
        if not msgs:
            return

        # 首次见到这个群：只建基线，不回放历史。基线落盘，重启后不再重建，
        # 所以重启窗口里进来的消息下一轮就能补上（而不是被当成历史吞掉）。
        if not self.state.is_initialized(group.chat_id):
            self.state.mark_seen(group.chat_id, [str(m.get("message_id") or "") for m in msgs])
            self.state.save()
            log(TAG, "init", "info",
                f"群 {group.name or group.chat_id} 建立基线，记录 {len(msgs)} 条历史")
            return

        fresh = [m for m in msgs if not self.state.has_seen(group.chat_id, str(m.get("message_id") or ""))]
        if not fresh:
            return

        # 先整体标记已读再逐条判定：即使下面的分发抛异常，也不会在下一轮重复触发。
        self.state.mark_seen(group.chat_id, [str(m.get("message_id") or "") for m in fresh])
        self.state.save()

        # 熔断中的群：继续跟进已读水位线（免得恢复后回放一大堆），但不再起 agent。
        if group.chat_id in self._send_blocked:
            return

        for msg in fresh:
            if self._should_trigger(msg, group):
                self._spawn(group, msg)

    # ── 判定 ─────────────────────────────────────────────────
    def _should_trigger(self, msg: dict, group: GroupConfig) -> bool:
        mid = str(msg.get("message_id") or "")
        if not mid or msg.get("deleted"):
            return False
        if self.state.is_self_sent(mid):
            return False
        if str(msg.get("msg_type") or "") not in TRIGGER_MSG_TYPES:
            return False

        sender = msg.get("sender") or {}
        sender_id = str(sender.get("id") or "")
        sender_name = str(sender.get("name") or "")
        is_owner = bool(
            (self.owner_open_id and sender_id == self.owner_open_id)
            or (not sender_id and sender_name == self.owner_name)
        )
        mentioned = mentions_owner(msg, self.owner_open_id, self.owner_name)

        if is_owner:
            # 机主自己发的：只有**显式 @ 自己**才算触发指令。
            if not group.allow_self_mention or not mentioned:
                return False
        elif group.require_mention and not mentioned:
            return False

        log(TAG, "trigger", "info",
            f"命中 群={group.name or group.chat_id} 发送人={sender_name}"
            f"{'(自己)' if is_owner else ''} thread={str(msg.get('thread_id') or '-')[:14]} "
            f"msg={mid[:16]}…")
        return True

    # ── 分发 ─────────────────────────────────────────────────
    def _lock_for(self, msg: dict) -> asyncio.Lock:
        """同一话题内串行：连着 @ 两条时，第二条要能看到第一条的回复和 session。"""
        key = f"{msg.get('chat_id')}:{msg.get('thread_id') or msg.get('message_id')}"
        lock = self._thread_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._thread_locks[key] = lock
        return lock

    def _spawn(self, group: GroupConfig, msg: dict) -> None:
        task = asyncio.create_task(self._run_one(group, msg))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _run_one(self, group: GroupConfig, msg: dict) -> None:
        try:
            assert self._sem is not None  # start() 里已建
            async with self._sem:
                async with self._lock_for(msg):
                    await handle_external_message(
                        cfg=self.config,
                        group=group,
                        api=self.api,
                        state=self.state,
                        msg=msg,
                        owner_open_id=self.owner_open_id,
                        owner_name=self.owner_name,
                        download_dir=os.path.expanduser(self.config.download_dir),
                    )
        except asyncio.CancelledError:
            raise
        except ExternalSendBlocked as e:
            chat_id = str(msg.get("chat_id") or group.chat_id)
            self._send_blocked.add(chat_id)
            log(TAG, "bridge", "error",
                f"⛔ 群 {group.name or chat_id} 的发送被 Lark 拒绝（230027），"
                f"已熔断该群的自动回复（重启恢复）: {e}")
        except Exception as e:
            log(TAG, "bridge", "error", f"处理消息异常 msg={str(msg.get('message_id'))[:16]}…: {e}")

    # ── 可选：Playwright 常驻页面 ────────────────────────────
    async def _run_browser_listener(self) -> None:
        """把 Web IM 页面开着，为后续解 WebSocket 协议帧留的钩子。

        ⚠️ 现在它对触发**没有任何贡献**：帧还没解码，触发完全靠上面的轮询。
        默认关闭（config.browser_listener），别为了它常驻一个 headless Chromium。
        """
        from playwright.async_api import async_playwright

        if not self.session_mgr.has_session():
            log(TAG, "browser", "warn", "没有浏览器登录态，跳过常驻页面（不影响轮询触发）")
            return
        try:
            async with async_playwright() as p:
                context = await self.session_mgr.create_authenticated_context(p, headless=True)
                page = await context.new_page()
                url = f"https://c8ytzah4el.sg.{self.config.lark_domain}/messenger"
                log(TAG, "browser", "info", f"打开 Web IM: {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                while self.running:
                    await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(TAG, "browser", "warn", f"常驻页面退出（不影响轮询触发）: {e}")
