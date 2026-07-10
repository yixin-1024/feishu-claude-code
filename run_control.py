import asyncio
import os
import signal
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from bot_config import THREAD_SHARED_SESSION


_KILL_WAIT_TIMEOUT_SECONDS = 1.0


def _kill_pgroup(proc, sig: int) -> bool:
    """仅终止 proc 自己领导的独立进程组。

    runner 正常应以 start_new_session=True 启动，满足 pid == pgid。这里仍做
    fail-closed 校验：共享 bot 进程组或不是组长时绝不 killpg，交给调用方退回
    单 PID terminate/kill，避免一个 runner 配置遗漏把 main.py / wrapper 一起杀掉。
    """
    try:
        pid = int(proc.pid)
        pgid = os.getpgid(pid)
        if pgid != pid or pgid == os.getpgrp():
            return False
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, AttributeError, TypeError, ValueError):
        return False


@dataclass
class ActiveRun:
    user_id: str
    chat_id: str
    card_msg_id: str
    proc: object | None = None
    stop_requested: bool = False
    stop_announced: bool = False
    # 流式 push、最终落卡与 /stop|/restart 中断提示共用这把锁。
    # 中断提示会等待已在途的旧写入结束后最后落卡，后续写入看到 stop flag 跳过。
    card_update_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


def _key(user_id: str, chat_id: str) -> str:
    # 话题群共享模式：thread 复合 chat_id（"oc_xxx:omt_yyy"）按话题聚合，
    # 与 SessionStore 的共享 session 语义一致 —— 同话题里任何人都能 /stop
    # 正在跑的任务（不管是谁发起的）。
    if THREAD_SHARED_SESSION and ":" in chat_id and chat_id != user_id:
        return f"__thread__::{chat_id}"
    return f"{user_id}::{chat_id}"


class ActiveRunRegistry:
    """按 (user_id, chat_id) 索引 — 同一用户在不同 chat/话题里的任务互不干扰。
    话题群共享模式下话题维度聚合（见 _key）。"""

    def __init__(self):
        self._runs: dict[str, ActiveRun] = {}

    def start_run(self, user_id: str, chat_id: str, card_msg_id: str) -> ActiveRun:
        active_run = ActiveRun(user_id=user_id, chat_id=chat_id, card_msg_id=card_msg_id)
        self._runs[_key(user_id, chat_id)] = active_run
        return active_run

    def get_run(self, user_id: str, chat_id: str) -> Optional[ActiveRun]:
        return self._runs.get(_key(user_id, chat_id))

    def attach_process(self, user_id: str, chat_id: str, proc) -> Optional[ActiveRun]:
        active_run = self._runs.get(_key(user_id, chat_id))
        if active_run is None:
            return None
        active_run.proc = proc
        if active_run.stop_requested and getattr(proc, "returncode", None) is None:
            if not _kill_pgroup(proc, signal.SIGTERM):
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
        return active_run

    def clear_run(self, user_id: str, chat_id: str, active_run: Optional[ActiveRun] = None):
        k = _key(user_id, chat_id)
        current = self._runs.get(k)
        if current is None:
            return
        if active_run is not None and current is not active_run:
            return
        self._runs.pop(k, None)


async def _maybe_await(result):
    if asyncio.iscoroutine(result):
        await result


async def stop_run(
    registry: ActiveRunRegistry,
    user_id: str,
    chat_id: str,
    on_stopped: Optional[Callable[[ActiveRun], Awaitable[None] | None]] = None,
    grace_seconds: float = 2.0,
) -> bool:
    active_run = registry.get_run(user_id, chat_id)
    if active_run is None:
        return False

    active_run.stop_requested = True
    proc = active_run.proc
    if proc is not None and getattr(proc, "returncode", None) is None:
        if not _kill_pgroup(proc, signal.SIGTERM):
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            if not _kill_pgroup(proc, signal.SIGKILL):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            # SIGKILL 后理论上会立即退出，但磁盘/内核异常时 wait 仍可能卡死。
            # /restart 不能因此永远停在“中断任务”阶段。
            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=max(_KILL_WAIT_TIMEOUT_SECONDS, grace_seconds),
                )
            except asyncio.TimeoutError:
                pass

    if on_stopped is not None and not active_run.stop_announced:
        await _maybe_await(on_stopped(active_run))
        active_run.stop_announced = True

    return True
