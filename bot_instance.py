"""BotInstance：一个 profile 的运行时状态封装。

每个 profile 启动后构造一个 BotInstance，持有：
    - Lark SDK 长连接 client
    - FeishuClient（业务包装，发消息 / 上传文件等）
    - SessionStore（per-profile 持久化）
    - ActiveRunRegistry（per-chat 正在跑的 Claude run）
    - chat_locks（同一 chat 内串行，跨 chat 并行）

消息处理函数把 BotInstance 作为首参数传递，相当于上下文/this 指针。
"""

from __future__ import annotations

import asyncio

import lark_oapi as lark

from bot_config import Profile
from feishu_client import FeishuClient
from run_control import ActiveRunRegistry
from session_store import SessionStore


class BotInstance:
    _MAX_CHAT_LOCKS = 200

    def __init__(self, profile: Profile):
        self.profile = profile
        self.lark_client = (
            lark.Client.builder()
            .app_id(profile.app_id)
            .app_secret(profile.app_secret)
            .domain(profile.domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        self.feishu = FeishuClient(
            self.lark_client,
            app_id=profile.app_id,
            app_secret=profile.app_secret,
            domain=profile.domain,
            label=profile.name,
        )
        self.store = SessionStore(
            profile=profile.name,
            default_cwd=profile.default_cwd,
            chat_default_cwd=profile.chat_default_cwd,
            default_runner=profile.runner,
            default_model=profile.default_model,
        )
        self.active_runs = ActiveRunRegistry()
        # per-chat 消息队列锁：同一 chat 串行，跨 chat 并行
        self.chat_locks: dict[str, asyncio.Lock] = {}

    def _ensure_chat_lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self.chat_locks:
            if len(self.chat_locks) >= self._MAX_CHAT_LOCKS:
                idle = [k for k, v in self.chat_locks.items() if not v.locked()]
                for k in idle[: len(idle) // 2]:
                    del self.chat_locks[k]
            self.chat_locks[chat_id] = asyncio.Lock()
        return self.chat_locks[chat_id]
