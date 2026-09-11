#!/usr/bin/env python3
"""外部群个人号监听器启动入口"""

import os
import sys

# 优先使用项目目录下的 .venv
venv_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python3")
if os.path.exists(venv_python) and sys.executable != venv_python:
    os.execv(venv_python, [venv_python] + sys.argv)

import argparse
import asyncio

from external_watcher.config import ExternalWatcherConfig
from external_watcher.session_manager import SessionManager
from external_watcher.watcher import ExternalWebWatcher
from log_util import log

TAG = "ext-main"


async def main():
    parser = argparse.ArgumentParser(description="Lark 外部群个人号协议监听器")
    parser.add_argument("--config", default="external_groups.yaml", help="配置文件路径")
    parser.add_argument("--login", action="store_true", help="启动可视化浏览器进行首次登录并保存会话")
    parser.add_argument("--headless-login", action="store_true", help="在无头模式下尝试登录")
    args = parser.parse_args()

    cfg = ExternalWatcherConfig.load(args.config)
    session_mgr = SessionManager(cfg.session_dir, cfg.lark_domain)

    if args.login or args.headless_login:
        log(TAG, "login", "info", "开始初始化 Web 登录会话...")
        success = await session_mgr.login_interactive(headless=args.headless_login)
        if success:
            log(TAG, "login", "info", f"会话已成功保存，路径: {session_mgr.state_file}")
            sys.exit(0)
        else:
            log(TAG, "login", "error", "登录未完成或超时")
            sys.exit(1)

    # 正常常驻启动
    watcher = ExternalWebWatcher(args.config)
    log(TAG, "start", "info", f"正在启动外部群监听器 (配置: {args.config})...")
    await watcher.start()

    # 保持主进程运行
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log(TAG, "shutdown", "info", "正在退出外部群监听器...")
        await watcher.stop()


if __name__ == "__main__":
    asyncio.run(main())
