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
import http_server
import runtime
from bot_instance import BotInstance
from log_util import log
from scheduler import fire_task_now, list_tasks, reload_tasks


# profile_name → BotInstance（跨进程共享，dispatcher / http_server / runtime 都依赖）
_bots: dict[str, BotInstance] = {}


def main():
    print("🚀 飞书/Lark Claude Bot 启动中...")
    print(f"   已加载 {len(config.PROFILES)} 个 profile")
    for p in config.PROFILES:
        allow_desc = f"{len(p.allowed_open_ids)} 人" if p.allowed_open_ids else "⚠️ 所有人"
        group_desc = (
            f"{len(p.allowed_group_chat_ids)} 群" if p.allowed_group_chat_ids else "禁用"
        )
        role_desc = f"role={p.role}" if p.role else "—"
        print(
            f"   · {p.name:<10} {p.brand_label}  "
            f"app={p.app_id}  cwd={p.default_cwd}  "
            f"allow={allow_desc}  groups={group_desc}  "
            f"lark-cli profile={p.lark_cli_profile}  {role_desc}"
        )
    print(f"   默认模型    : {config.DEFAULT_MODEL}")
    print(f"   权限模式    : {config.PERMISSION_MODE}")
    if config.TRINITY_ENABLED:
        print(f"   Trinity     : 已启用（roles: {sorted(config.PROFILES_BY_ROLE.keys())}）")

    # 1) 启动跨 profile 的 bot 事件循环
    bot_loop = runtime.create_bot_loop()

    # 2) 实例化所有 BotInstance
    for p in config.PROFILES:
        _bots[p.name] = BotInstance(p)

    # 3) 三个层都通过 configure() 注入依赖（避免循环 import）
    dispatcher.configure(bot_loop=bot_loop, bots=_bots)

    http_server.configure(
        bot_loop=bot_loop,
        bots=_bots,
        handlers=http_server.HttpHandlers(
            handle_spawn=dispatcher.handle_spawn,
            handle_handover=dispatcher.handle_handover,
            handle_set_mode=dispatcher.handle_set_mode,
            handle_menu_command=dispatcher.handle_menu_command,
            handle_resume_session=dispatcher.handle_resume_session,
            handle_button_reply=dispatcher.handle_button_reply,
            fire_task=fire_task_now,
            list_tasks=list_tasks,
            reload_tasks=reload_tasks,
        ),
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

    # 4) HTTP server + ngrok（卡片按钮回调走这里）
    cb_port = config.CALLBACK_PORT
    http_server.start_callback_server(cb_port)
    ngrok_url = http_server.start_ngrok(cb_port)
    if ngrok_url:
        print(f"   卡片回调    : {ngrok_url}/callback")
    else:
        print(f"   卡片回调    : http://localhost:{cb_port}/callback (需启动 ngrok)")

    # 5) 后台基础设施
    runtime.start_watchdog()
    runtime.start_summary_thread()

    # 6) 每个 profile 起一个 WS 客户端
    print("✅ 连接 WebSocket 长连接（自动重连）...")
    for bot in _bots.values():
        runtime.start_profile_ws(bot)

    # 7) 定时任务调度器
    sched_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduled_tasks.yaml")
    runtime.start_scheduler_async(sched_path)

    # 主线程保持运行
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n⛔ 退出")


if __name__ == "__main__":
    main()
