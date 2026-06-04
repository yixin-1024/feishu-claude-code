import asyncio
import os
import signal
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from bot_config import THREAD_SHARED_SESSION


def _kill_pgroup(proc, sig: int) -> bool:
    """杀 proc 所在进程组（spawn 时 start_new_session=True，proc 是 pgid leader）。
    只杀 leader 会留下 claude 起的 MCP server / hook 子进程当孤儿。"""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
        return True
    except (ProcessLookupError, PermissionError, AttributeError):
        return False


@dataclass
class ActiveRun:
    user_id: str
    chat_id: str
    card_msg_id: str
    proc: object | None = None
    stop_requested: bool = False
    stop_announced: bool = False


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
            await proc.wait()

    if on_stopped is not None and not active_run.stop_announced:
        await _maybe_await(on_stopped(active_run))
        active_run.stop_announced = True

    return True
