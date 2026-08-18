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
import os

import lark_oapi as lark

from bot_config import Profile
from feishu_client import FeishuClient
from run_control import ActiveRunRegistry
from session_store import SessionStore


def _api_timeout() -> float:
    """单次 Lark HTTP 请求的超时（秒）。

    lark_oapi 的 Config.timeout 默认是 None —— 直接透传给 httpx/requests 就是
    「永不超时」。网络通路被掐断（ClashX TUN 切节点 / WiFi 抖动）时，已发出的
    请求收不到 RST，会永久挂住：卡片 patch 卡在 in-flight，持锁的那个 run 心跳
    再也推不动，卡片就此定格（而且连收尾的 ✅ 都写不进去）。给它一个上限，让
    死连接变成一个可重试的异常。httpx/requests 的 timeout 是「两次收字节之间的
    间隔」而非请求总时长，所以大文件上传不受影响。
    """
    try:
        return max(1.0, float(str(os.environ.get("CC_LARK_API_TIMEOUT", "")).strip()))
    except (TypeError, ValueError):
        return 15.0


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
            .timeout(_api_timeout())
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
