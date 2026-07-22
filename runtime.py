"""后台基础设施：事件循环、看门狗、WS 启动、摘要后台、定时任务。

不含业务逻辑。所有需要"业务回调"的地方（如 WS 收到消息后调谁、scheduler 派单调谁）
都通过 `configure(...)` 注入，避免循环 import + 让 runtime 可以单独测试。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import lark_oapi as lark
import lark_oapi.ws.client as _lark_ws_mod

from bot_instance import BotInstance
from log_util import log
from scheduler import start_scheduler
from session_store import generate_summary, _write_custom_title


# ── lark_oapi 的模块级 loop 替换为 per-thread 代理 ────────────
#
# lark_oapi.ws.client 有一个模块级全局 `loop`，所有 ws.Client 实例共用它，
# 原生不支持「同进程跑多个 bot」。把它替换成代理对象，按调用线程的默认事件
# 循环动态分发——每个 WS 线程先 set_event_loop(new_loop) 拿到自己的事件循环，
# 然后 lark_oapi 内部所有 `loop.*` 调用都会落到正确的那个 loop。

class _PerThreadLoopProxy:
    def __getattr__(self, attr):
        return getattr(asyncio.get_event_loop(), attr)


def install_loop_proxy() -> None:
    """**必须在创建任何 ws.Client 之前调用。**"""
    _lark_ws_mod.loop = _PerThreadLoopProxy()


# ── 配置注入 ──────────────────────────────────────────────────

@dataclass
class RuntimeBindings:
    """业务回调集合。configure() 注入。"""
    # WS 收到消息时调，签名 (bot, event) -> Awaitable[None]
    on_lark_message: Callable[..., Awaitable[None]]
    # 用户点了卡片按钮（SDK 长连接路径）时调
    on_card_action: Callable
    # /spawn 派单 handler（给 scheduler 用）
    spawn_fn: Callable[..., Awaitable[None]]


_bindings: Optional[RuntimeBindings] = None
_bots: dict[str, BotInstance] = {}
_bot_loop: Optional[asyncio.AbstractEventLoop] = None


def configure(
    *,
    bot_loop: asyncio.AbstractEventLoop,
    bots: dict[str, BotInstance],
    bindings: RuntimeBindings,
) -> None:
    global _bot_loop, _bots, _bindings
    _bot_loop = bot_loop
    _bots = bots
    _bindings = bindings


# ── bot 事件循环（跨 profile 共享） ───────────────────────────

def create_bot_loop() -> asyncio.AbstractEventLoop:
    """创建并启动 bot_loop（一个独立的 asyncio loop 跑在后台线程）。"""
    loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=_run, daemon=True, name="bot-loop").start()
    return loop


# ── 活跃时间戳（供调用方追踪 WS 是否还在收事件） ──────────

last_event_ts = time.time()

# 按 profile 的最近活跃时间戳。全局 last_event_ts 会被「任意一个 bot 收到事件」
# 刷新，所以无法判断「某一条 WS 单独死了」——曾经 hermes 的 WS 静默挂掉，但
# 其它 profile 照常收事件把全局时间戳喂饱，于是没有任何监控发现 hermes 变哑。
# 这里按 profile 名分开记，供 WS 看门狗 / 状态查询用。
_profile_last_event: dict[str, float] = {}
_profile_last_event_lock = threading.Lock()


def touch_event() -> None:
    """每收到一次 Lark event 调一次，刷新最近活跃时间。"""
    global last_event_ts
    last_event_ts = time.time()


def _touch_profile_event(name: str) -> None:
    """刷新某个 profile 的最近活跃时间（每收到一次该 bot 的事件调一次）。"""
    with _profile_last_event_lock:
        _profile_last_event[name] = time.time()


def profile_idle_seconds() -> dict[str, float]:
    """返回每个 profile 距上次收到事件的秒数（用于状态查询 / 调试）。"""
    now = time.time()
    with _profile_last_event_lock:
        return {name: now - ts for name, ts in _profile_last_event.items()}


# ── 后台定时摘要生成 ────────────────────────────────────────

def _bg_summary_loop():
    """扫描所有 profile 下未摘要的会话，逐个生成摘要。"""
    time.sleep(60)
    while True:
        try:
            for bot in _bots.values():
                unsummarized = bot.store.get_all_unsummarized()
                if not unsummarized:
                    continue
                log(bot.profile.name, "summary", "info",
                    f"发现 {len(unsummarized)} 个未摘要会话")
                count = 0
                for user_id, sid in unsummarized[:5]:
                    try:
                        summary = generate_summary(sid)
                        if summary:
                            (bot.store._data
                                .setdefault(user_id, {})
                                .setdefault("summaries", {}))[sid] = summary
                            _write_custom_title(sid, summary)
                            count += 1
                            log(bot.profile.name, "summary", "info",
                                f"#{sid[:8]} → {summary}")
                    except Exception as e:
                        log(bot.profile.name, "summary", "warn",
                            f"#{sid[:8]} 失败: {e}")
                    time.sleep(5)
                if count:
                    bot.store._save()
        except Exception as e:
            log("global", "summary", "error", f"定时任务异常: {e}")
        time.sleep(600)


def start_summary_thread() -> None:
    threading.Thread(target=_bg_summary_loop, daemon=True, name="bg-summary").start()


# ── Claude Max 用量监控（5h/7d 跨阈值 + 重置通报）─────────────

def start_quota_watcher(
    notify_profile: str,
    notify_open_id: str,
    interval_sec: int = 600,
    *,
    enable_account_switcher: bool = False,
    switcher_cooldown_sec: int = 1800,
) -> None:
    """启动 quota_watcher 后台线程。

    通报通过 `notify_profile` 这个 bot 的 send_text_to_user 发给 `notify_open_id`。
    watcher 跑在独立线程，所以 send_fn 用 run_coroutine_threadsafe 把异步发送
    投回 bot_loop。

    enable_account_switcher：True 时同时启用多账户智能切换。每次 poll 之后
    探测所有 ~/.claude/accounts/*.json、按 5h+7d headroom 打分、必要时调
    account_switcher.use_account(name) 切到更优账户（keychain + ~/.claude.json
    identity 一起搬）。冷却 switcher_cooldown_sec 内不连切。正在跑 claude
    子进程时推迟到下一轮。
    """
    if _bot_loop is None or not _bots:
        raise RuntimeError("runtime not configured; call configure() first")
    bot = _bots.get(notify_profile)
    if bot is None:
        log("global", "quota", "warn",
            f"quota_watcher: profile {notify_profile!r} 未加载，跳过")
        return
    if not notify_open_id:
        log("global", "quota", "warn",
            f"quota_watcher: notify_open_id 为空，跳过启动")
        return

    from quota_watcher import start_watcher_thread

    def _send(text: str) -> None:
        # watcher 线程里被调，必须投回 bot_loop 跑 async send
        try:
            asyncio.run_coroutine_threadsafe(
                bot.feishu.send_text_to_user(notify_open_id, text),
                _bot_loop,
            )
        except Exception as e:
            log(notify_profile, "quota", "error",
                f"投递 quota 通报失败: {e}")

    switcher = None
    if enable_account_switcher:
        try:
            from account_switcher import AccountSwitcher, set_default_switcher

            # 注：不再传 has_active_children_fn——Claude 支持 keychain 热切换，正在跑的
            # claude 子进程不受影响，没必要因"有活跃子进程"而推迟切换。
            switcher = AccountSwitcher(
                send_fn=_send,
                cooldown_sec=switcher_cooldown_sec,
                enabled=True,
            )
            # 注册成模块级默认实例：spawn claude 前（claude_runner.run_claude）按需
            # 触发 maybe_switch_before_spawn，不必等 quota_watcher 10min 轮询。
            set_default_switcher(switcher)
            log("global", "switcher", "info",
                f"account_switcher 启用，冷却 {switcher_cooldown_sec}s")
        except Exception as e:
            log("global", "switcher", "error", f"account_switcher 启动失败: {e}")
            switcher = None

    # 启动期 keychain 自愈：cc-lark /restart 周期里观察到 Claude CLI / MCP 子系统
    # 偶发把 keychain blob 覆写为只剩 mcpOAuth，丢掉 claudeAiOauth。这里先自检
    # 一次，缺失就从 saved 账户文件恢复（同时 patch ~/.claude.json identity），
    # 并 Lark 通报 owner。
    try:
        from account_switcher import ensure_keychain_intact, auto_stash_identity_for_current
        status, name = ensure_keychain_intact()
        if status == "restored":
            log("global", "switcher", "warn",
                f"keychain claudeAiOauth 缺失，已从 {name!r} 自动恢复")
            _send(
                f"⚠️ **keychain 自愈**\n\n"
                f"启动时发现 keychain 缺 `claudeAiOauth`，已从 saved 账户 `{name}` 写回。\n"
                f"根因怀疑是 Claude CLI / MCP 在 cc-lark 重启周期里只写 mcpOAuth 覆盖了 blob。"
            )
        elif status == "no_active":
            log("global", "switcher", "error",
                "keychain claudeAiOauth 缺失且无可用 saved 账户")
            _send("⚠️ keychain 缺 `claudeAiOauth` 且 `~/.claude/accounts/` 没有可用 saved 账户——请手动 `claude` 登录或 `claude-switch use <name>`")
        elif status == "error":
            log("global", "switcher", "error", f"keychain 自愈失败: {name}")

        # 顺便：当前账户的 saved file 如果没存 identity，把 ~/.claude.json 抠一份补上。
        # 用户手动 login 切到 reg 干了一阵子，cc-lark 启动时就能自动把当时的 reg
        # identity stash 回 reg.json，下次切换才丝滑。
        try:
            st, nm = auto_stash_identity_for_current()
            if st == "stashed":
                log("global", "switcher", "info",
                    f"identity 已从 ~/.claude.json 自动 stash 回 {nm!r}")
            elif st == "error":
                log("global", "switcher", "warn", f"identity auto-stash 失败: {nm}")
        except Exception as e:
            log("global", "switcher", "warn", f"identity auto-stash 异常: {e}")
    except Exception as e:
        log("global", "switcher", "error", f"keychain 自愈异常: {e}")

    start_watcher_thread(_send, interval=interval_sec, switcher=switcher)
    log(notify_profile, "quota", "info",
        f"quota watcher 启动 → 通报到 {notify_open_id[:14]}... 每 {interval_sec}s"
        + ("，账户智能切换：开" if switcher is not None else ""))


def start_codex_quota_watcher(
    notify_profile: str,
    notify_open_id: str,
    interval_sec: int = 1800,
) -> None:
    """启动 Codex 额度监控后台线程：窗口重置时给 owner 发 Lark 私信。

    通报通过 `notify_profile` 这个 bot 的 send_text_to_user 发给 `notify_open_id`。
    watcher 跑在独立线程，send_fn 用 run_coroutine_threadsafe 投回 bot_loop。
    """
    if _bot_loop is None or not _bots:
        raise RuntimeError("runtime not configured; call configure() first")
    bot = _bots.get(notify_profile)
    if bot is None:
        log("global", "codex_quota", "warn",
            f"codex_quota_watcher: profile {notify_profile!r} 未加载，跳过")
        return
    if not notify_open_id:
        log("global", "codex_quota", "warn",
            "codex_quota_watcher: notify_open_id 为空，跳过启动")
        return

    from codex_quota_watcher import start_watcher_thread as start_codex_watcher

    def _send(text: str) -> None:
        try:
            fut = asyncio.run_coroutine_threadsafe(
                bot.feishu.send_text_to_user(notify_open_id, text),
                _bot_loop,
            )
            # 阻塞等发送真正落地：否则 Future 被丢弃，协程内抛的异常（如对方从未
            # 开通与该 bot 的私聊 → 发不出去）会被静默吞掉，日志里查不到，看起来
            # 就像"重置了却没通报"。watcher 跑在独立线程，这里 block 一会儿无妨。
            fut.result(timeout=30)
        except Exception as e:
            log(notify_profile, "codex_quota", "error",
                f"投递 codex 额度通报失败（收件人 {notify_open_id[:14]}...）: {e}")

    start_codex_watcher(_send, interval=interval_sec)
    log(notify_profile, "codex_quota", "info",
        f"codex 额度监控启动 → 通报到 {notify_open_id[:14]}... 每 {interval_sec}s")


# ── 为 profile 启动 WebSocket 客户端 ─────────────────────────

def start_profile_ws(bot: BotInstance) -> None:
    """为一个 profile 启动独立 WS 客户端（跑在单独线程，监督式自愈）。

    历史坑：lark_oapi 的 ws.Client.start() 内部虽有无限 _reconnect()，但若重连
    期间 _try_connect 抛出 ClientException（凭证错误 / 服务端某些返回码），异常会
    一路冒泡冲出 start() 把线程结束掉。旧实现只调一次 start()、不监督，于是这条
    WS 永久变哑（进程不崩、其它 profile 照常、launchd 不重启、lark_oapi 自身的
    日志又不进 cc-lark 日志 → 完全静默）。这里用监督循环包住：start() 一旦返回
    或抛异常，就退避后**重建 client 重连**，让单条 WS 死亡可自愈而无需整进程重启。
    """
    if _bindings is None or _bot_loop is None:
        raise RuntimeError("runtime not configured; call configure() first")
    name = bot.profile.name

    def _on_message(data) -> None:
        touch_event()
        _touch_profile_event(name)
        asyncio.run_coroutine_threadsafe(
            _bindings.on_lark_message(bot, data),
            _bot_loop,
        )

    def _on_card(data):
        touch_event()
        _touch_profile_event(name)
        # The WS connection is authenticated for this exact app/profile.  Pass the
        # bound bot through; action.value.profile must never be allowed to reroute it.
        return _bindings.on_card_action(bot, data)

    def _build_client():
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message)
            .register_p2_card_action_trigger(_on_card)
            .register_p2_im_message_message_read_v1(lambda _e: None)
            .build()
        )
        return lark.ws.Client(
            bot.profile.app_id,
            bot.profile.app_secret,
            event_handler=handler,
            domain=bot.profile.domain,
            log_level=lark.LogLevel.INFO,
        )

    def _run():
        backoff = 5
        while True:
            started = time.time()
            try:
                # 每次都换一个全新的事件循环 + 全新 client：重连后旧 client 的内部
                # 状态（_connected/_ws）已脏，复用会卡死。lark_oapi 模块级 loop 代理
                # 按当前线程默认 loop 分发，不同 profile 的 WS 互不打架。
                asyncio.set_event_loop(asyncio.new_event_loop())
                _build_client().start()
                reason = "start() 正常返回（SDK 已放弃内部重连）"
            except Exception as e:  # noqa: BLE001 — 任何异常都不能让监督线程退出
                reason = f"{type(e).__name__}: {e}"
            lived = int(time.time() - started)
            # 连得久（>2min）说明是新一次断线，退避重置；短时间反复失败才指数退避。
            if lived > 120:
                backoff = 5
            log(name, "ws", "warn",
                f"WS 断开（存活 {lived}s）：{reason}；{backoff}s 后重连")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    _touch_profile_event(name)  # 启动即视为「刚活跃」，避免看门狗误判
    threading.Thread(
        target=_run, daemon=True, name=f"ws-{name}",
    ).start()
    log(name, "ws", "info",
        f"WS 客户端已启动 ({bot.profile.brand_label} · {bot.profile.domain})")


# ── 定时任务调度 ─────────────────────────────────────────────

_scheduler = None  # 持有引用避免被 GC


def start_scheduler_bg(config_path: str) -> None:
    """主入口调一次。BackgroundScheduler 自己跑独立线程，不依赖 bot_loop 的 timer。

    job 触发时同步 wrapper 用 run_coroutine_threadsafe 把 async 业务投回 bot_loop —
    跨睡眠周期的 monotonic drift 不再影响调度准确性（详见 scheduler.py docstring）。
    """
    global _scheduler
    if _bot_loop is None or _bindings is None:
        raise RuntimeError("runtime not configured; call configure() first")
    try:
        _scheduler = start_scheduler(
            config_path=config_path,
            bots=_bots,
            bot_loop=_bot_loop,
            spawn_fn=_bindings.spawn_fn,
        )
    except Exception as e:
        log("global", "scheduler", "error",
            f"启动失败: {type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stdout)
