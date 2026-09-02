"""飞书/Lark × Claude Code Bot — 启动入口。

各子模块职责：
    bot_config      多 profile 配置加载（含 trinity role 字段）
    bot_instance    BotInstance：一个 profile 的运行时状态封装
    dispatcher      消息分发 + 业务核心（handle_message_async / _process_message / 卡片回调 ...）
    http_server     卡片回调 + /spawn / /trigger / /reload / /handover 等本机控制端点
    runtime         事件循环 / 看门狗 / WS 启动 / 摘要后台 / scheduler
    trinity_dispatch  三省体系 bot↔bot 调度
    lark_prompts    Prompt 模板加载器（prompts/{role}.md）

启动顺序（main 函数里）：
    1. install_loop_proxy（必须在 ws.Client 创建前）
    2. 创建 _bot_loop 并起后台线程
    3. 实例化所有 BotInstance
    4. configure 三个层（dispatcher / http_server / runtime）注入 loop/bots/handlers
    5. 启动 HTTP server + ngrok
    6. 启动看门狗 + 摘要后台
    7. 为每个 profile 起 WS 客户端
    8. 启动 scheduler
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# 确保项目目录在 sys.path 最前面
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# install_loop_proxy 必须在创建任何 ws.Client 之前。runtime.__init__ 不动它，
# 提供一个 install 函数让主入口显式触发。
from runtime import install_loop_proxy

install_loop_proxy()

import bot_config as config
import dispatcher
import external_api
import http_server
import inbox_watcher
import runtime
from bot_instance import BotInstance
from log_util import log
from scheduler import (
    fire_task_now, list_tasks, reload_tasks, schedule_wake, schedule_cron, list_crons,
)


# profile_name → BotInstance（跨进程共享，dispatcher / http_server / runtime 都依赖）
_bots: dict[str, BotInstance] = {}

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _external_api_deps(bot_loop: asyncio.AbstractEventLoop) -> external_api.ExternalApiDeps:
    """给外部触发 API 注入业务能力：派单 / 读 thread / 同步等 bot_loop 的结果。"""

    def _run_coro(coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, bot_loop).result(timeout=timeout)

    async def _dispatch(bot, *, agent: str = "", **kwargs):
        # route 可以指定 agent="gpt" 之类，把这条外部事件交给别的后端 bot 跑
        target_bot = None
        if agent:
            target_bot, err = http_server.resolve_target_agent(
                _bots, agent, exclude=bot.profile.name)
            if target_bot is None:
                return {"ok": False, "error": err}
        return await dispatcher.dispatch_task(bot, target_bot=target_bot, **kwargs)

    return external_api.ExternalApiDeps(
        bots=_bots,
        dispatch=_dispatch,
        read_thread=dispatcher.read_thread,
        run_coro=_run_coro,
    )


def _make_reload_all(ext_cfg_path: str):
    """/reload 一并刷新定时任务和外部触发路由（两份 yaml 都不必重启 bot）。"""
    def _reload_all() -> dict:
        result = reload_tasks()
        if ext_cfg_path:
            result["external_api"] = external_api.reload()
        return result
    return _reload_all


def main():
    print("🚀 飞书/Lark Agent Bot 启动中...")
    print(f"   已加载 {len(config.PROFILES)} 个 profile")
    for p in config.PROFILES:
        allow_desc = f"{len(p.allowed_open_ids)} 人" if p.allowed_open_ids else "⚠️ 所有人"
        group_desc = (
            f"{len(p.allowed_group_chat_ids)} 群" if p.allowed_group_chat_ids else "禁用"
        )
        role_desc = f"role={p.role}" if p.role else "—"
        print(
            f"   · {p.name:<10} {p.brand_label}  "
            f"app={p.app_id}  runner={p.runner} model={p.default_model or config.DEFAULT_MODEL}  "
            f"cwd={p.default_cwd}  "
            f"allow={allow_desc}  groups={group_desc}  "
            f"lark-cli profile={p.lark_cli_profile}  {role_desc}"
        )
    print(f"   默认后端    : {config.DEFAULT_RUNNER}")
    print(f"   默认模型    : {config.DEFAULT_MODEL}")
    print(f"   权限模式    : {config.PERMISSION_MODE}")
    if config.TRINITY_ENABLED:
        print(f"   Trinity     : 已启用（roles: {sorted(config.PROFILES_BY_ROLE.keys())}）")

    # 1) 启动跨 profile 的 bot 事件循环
    bot_loop = runtime.create_bot_loop()

    # 2) 实例化所有 BotInstance
    for p in config.PROFILES:
        _bots[p.name] = BotInstance(p)

    # 3) control plane secret：显式 env 优先，否则生成/复用 0600 token 文件。
    # 先写回 os.environ，后续每个 agent spawn 才能把同一个 token 注入 cc_mcp_server。
    control_token = http_server.load_or_create_control_token()
    os.environ["CC_LARK_CONTROL_TOKEN"] = control_token
    os.environ["CC_LARK_CONTROL_PORT"] = str(config.CONTROL_PORT)

    # 4) 三个层都通过 configure() 注入依赖（避免循环 import）
    dispatcher.configure(bot_loop=bot_loop, bots=_bots)

    # 4.1) 外部事件触发 API：后端服务 → HTTP → 在配置好的群里开话题跑 agent。
    # 配置文件不存在 = 功能关闭（/api/v1/* 一律 503），不影响其余启动流程。
    ext_cfg_path = os.path.join(_BASE_DIR, "external_triggers.yaml")
    try:
        ext_cfg = external_api.load_config(ext_cfg_path)
    except Exception as e:
        print(f"⚠️ external_triggers.yaml 加载失败，外部触发 API 保持关闭: {e}")
        ext_cfg = external_api.ApiConfig()
    external_api.configure(
        config=ext_cfg,
        deps=_external_api_deps(bot_loop),
        config_path=ext_cfg_path,
    )

    http_server.configure(
        bot_loop=bot_loop,
        bots=_bots,
        handlers=http_server.HttpHandlers(
            handle_spawn=dispatcher.handle_spawn,
            handle_handover=dispatcher.handle_handover,
            handle_set_mode=dispatcher.handle_set_mode,
            handle_menu_command=dispatcher.handle_menu_command,
            handle_switch_usage=dispatcher.handle_switch_usage,
            handle_resume_session=dispatcher.handle_resume_session,
            handle_button_reply=dispatcher.handle_button_reply,
            fire_task=fire_task_now,
            list_tasks=list_tasks,
            reload_tasks=_make_reload_all(ext_cfg_path),
            schedule_wake=schedule_wake,
            dispatch_task=dispatcher.dispatch_task,
            read_thread=dispatcher.read_thread,
            steer_thread=dispatcher.steer_or_append_thread,
            schedule_cron=schedule_cron,
            list_crons=list_crons,
        ),
        control_token=control_token,
    )

    runtime.configure(
        bot_loop=bot_loop,
        bots=_bots,
        bindings=runtime.RuntimeBindings(
            on_lark_message=dispatcher.handle_message_async,
            on_card_action=dispatcher.on_card_action,
            spawn_fn=dispatcher.handle_spawn,
        ),
    )

    # 5) 拆分 HTTP listener：ngrok 只暴露 callback；control 只绑 loopback + token。
    cb_port = config.CALLBACK_PORT
    control_port = config.CONTROL_PORT
    if cb_port == control_port:
        raise RuntimeError("CONTROL_PORT must differ from CALLBACK_PORT")
    http_server.start_callback_server(cb_port)
    http_server.start_control_server(control_port)
    ngrok_url = http_server.start_ngrok(cb_port)
    if ngrok_url:
        print(f"   卡片回调    : {ngrok_url}/callback")
    else:
        print(f"   卡片回调    : http://localhost:{cb_port}/callback (需启动 ngrok)")
    print(f"   本机控制面  : http://127.0.0.1:{control_port} (Bearer token)")
    if external_api.is_enabled():
        base = ngrok_url or f"http://localhost:{cb_port}"
        print(f"   外部触发 API: {base}/api/v1/agent-tasks  "
              f"（{len(ext_cfg.routes)} route / {len(ext_cfg.clients)} client）")
        for r in ext_cfg.routes.values():
            print(f"     · {r.name:<20} profile={r.profile} chat={r.chat_id[:14]}... cwd={r.cwd}")
        # 群 ↔ workspace 对不上是最容易犯又最难发现的配置错（消息跑错群），启动就喊出来
        for line in external_api.audit_route_groups():
            print(f"   ⚠️ {line}")
    else:
        print("   外部触发 API: 未启用（缺 external_triggers.yaml 或密钥未配）")

    # 6) 后台基础设施
    runtime.start_summary_thread()
    runtime.start_log_rotation_thread()

    # 7) 每个 profile 起一个 WS 客户端
    print("✅ 连接 WebSocket 长连接（自动重连）...")
    for bot in _bots.values():
        runtime.start_profile_ws(bot)

    # 8) 定时任务调度器（独立后台线程，绕开 asyncio monotonic timer 的 macOS 睡眠坑）
    sched_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduled_tasks.yaml")
    runtime.start_scheduler_bg(sched_path)

    # 8.1) inbox_watcher：事件驱动的"任务派单扫描器"
    inbox_cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbox_config.yaml")
    inbox_watcher.start(inbox_cfg_path, _bots, bot_loop)

    # 9) Claude Max 用量监控：跨阈值 / 窗口重置时主动给 owner 私聊通报
    # 通报通道：QUOTA_NOTIFY_PROFILE / QUOTA_NOTIFY_OPEN_ID 显式指定，
    # 缺省退回到第一个 profile 的第一个 allowed_open_id（默认 owner）。
    notify_profile = os.getenv("QUOTA_NOTIFY_PROFILE") or config.PROFILES[0].name
    explicit_id = os.getenv("QUOTA_NOTIFY_OPEN_ID", "").strip()
    if explicit_id:
        notify_open_id = explicit_id
    else:
        target = config.PROFILES_BY_NAME.get(notify_profile) or config.PROFILES[0]
        # allowed_open_ids 是 set：next(iter(...)) 的顺序受 hash 随机化影响，每次重启
        # 可能落到白名单里不同的人（曾导致额度通报发错人）。sorted 保证确定性，并
        # 告警提醒：真正指向 owner 请在 .env 显式配 QUOTA_NOTIFY_OPEN_ID。
        notify_open_id = next(iter(sorted(target.allowed_open_ids)), "")
        if notify_open_id:
            print(f"⚠️ QUOTA_NOTIFY_OPEN_ID 未配置，回退到 {notify_open_id[:14]}...（建议 .env 钉死收件人）")
    interval = int(os.getenv("QUOTA_WATCH_INTERVAL_SEC", "600"))
    # ACCOUNT_AUTO_SWITCH=1 启用多账户智能切换；冷却默认 30 min
    enable_switch = os.getenv("ACCOUNT_AUTO_SWITCH", "0").strip().lower() in ("1", "true", "yes", "on")
    switch_cooldown = int(os.getenv("ACCOUNT_SWITCH_COOLDOWN_SEC", "1800"))
    try:
        runtime.start_quota_watcher(
            notify_profile, notify_open_id, interval,
            enable_account_switcher=enable_switch,
            switcher_cooldown_sec=switch_cooldown,
        )
    except Exception as e:
        print(f"⚠️ quota_watcher 启动失败: {e}")

    # 9.1) Codex 额度监控：每 30 min 查一次，窗口重置时私信 owner（仅当有 codex profile）
    if any(getattr(p, "runner", "") == "codex" for p in config.PROFILES):
        codex_interval = int(os.getenv("CODEX_QUOTA_WATCH_INTERVAL_SEC", "1800"))
        codex_notify_profile = os.getenv("CODEX_QUOTA_NOTIFY_PROFILE") or notify_profile
        codex_notify_open_id = os.getenv("CODEX_QUOTA_NOTIFY_OPEN_ID", "").strip() or notify_open_id
        try:
            runtime.start_codex_quota_watcher(
                codex_notify_profile, codex_notify_open_id, codex_interval,
            )
        except Exception as e:
            print(f"⚠️ codex_quota_watcher 启动失败: {e}")

    # 主线程保持运行
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n⛔ 退出")


if __name__ == "__main__":
    main()
