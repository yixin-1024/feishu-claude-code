"""结构化日志辅助。

目的：把散落在各模块里的 `print(f"[{profile}][warn] ...")` 收口成一个函数，
统一前缀和时间戳格式，多 profile 后日志可 grep。

不引入 logging 框架（成本大、配置散）；保持 print 兼容，
只在前缀格式上做约束。

格式：
    [2026-05-13T10:23:45] [profile][module][level] message

用法：
    from log_util import log
    log("yushitai", "dispatcher", "info", "收到 Boss 消息")
    log("yushitai", "spawn", "error", f"失败: {e}")

约定 level：debug / info / warn / error
约定 module：dispatcher / spawn / scheduler / runtime / prompt / trinity / ...
"""

from __future__ import annotations

import sys
from datetime import datetime


def log(profile: str, module: str, level: str, msg: str) -> None:
    """统一日志输出。同步 + flush，保证 launchd / docker 抓得到。"""
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] [{profile}][{module}][{level}] {msg}", flush=True)


def warn(profile: str, module: str, msg: str) -> None:
    log(profile, module, "warn", msg)


def error(profile: str, module: str, msg: str) -> None:
    log(profile, module, "error", msg)


def info(profile: str, module: str, msg: str) -> None:
    log(profile, module, "info", msg)


def debug(profile: str, module: str, msg: str) -> None:
    log(profile, module, "debug", msg)


def exception(profile: str, module: str, msg: str) -> None:
    """带 traceback 的 error。"""
    log(profile, module, "error", msg)
    import traceback
    traceback.print_exc(file=sys.stdout)
    sys.stdout.flush()
