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
    # 最近一次流式渲染的正文（不含计时 footer）。/stop 用它把「已停止」标记
    # 追加在已展示内容之后，而不是整卡覆盖，从而保留停止前的进度。
    last_body: str = ""
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


# ── 全局并发闸门 ─────────────────────────────────────────────────────────
# per-chat lock 只保证「同一话题串行」，整机层面没有任何刹车：定时任务批量到点、
# 一波 dispatch_task 派 7 个子会话、多个群/多个 profile 同时来人，都能瞬间拉起十
# 几个 agent 进程，把 CPU / 内存 / API 额度一起打满（实测 14 并发全卡死）。
# 这里给所有 run 加一道跨 profile、跨群的全局上限：超额的 run 在闸门前 **FIFO 排队**
# —— 不丢消息、不拒绝，卡片上明示"排队中"，排队期间照样能 /stop。
# CC_LARK_MAX_CONCURRENT_RUNS=0（或负数）= 不限，回到无闸门的老行为。
# ⚠️ 别设成 1：编排 agent 若在同一轮里派子会话又等子会话结果，1 个额度会真锁死
#    （≥2 时父占一个、子仍有额度跑，只是慢）。
def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "")).strip() or default)
    except ValueError:
        return default


MAX_CONCURRENT_RUNS = _env_int("CC_LARK_MAX_CONCURRENT_RUNS", 4)
# 排队最长等待秒数，超时就放弃本次 run 并在卡片上说明；0 = 一直等（默认，保证不丢活）。
QUEUE_MAX_WAIT_SECONDS = _env_int("CC_LARK_QUEUE_MAX_WAIT_SEC", 0)

_ABORT_POLL_INTERVAL_SECONDS = 0.5


class RunGate:
    """全局 run 并发闸门（进程内、跨所有 profile）。

    用 asyncio.Semaphore 拿 FIFO 语义 = 先排队的先跑。等待可被 abort 谓词打断
    （/stop、/restart），打断时把排队位撤掉；撤销与"名额刚好到手"的竞态会把名额
    还回去，绝不漏额度。limit <= 0 时整个闸门退化成无操作。
    """

    def __init__(self, limit: int, max_wait: float = 0):
        self.limit = limit
        self.max_wait = max_wait      # 排队上限秒数，0 = 一直等
        self._sem: Optional[asyncio.Semaphore] = None
        self.running = 0
        self.waiting = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def _semaphore(self) -> asyncio.Semaphore:
        # 惰性创建：import 时还没有 event loop。
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.limit)
        return self._sem

    def full(self) -> bool:
        """当前是否已无空位（= 现在 acquire 会排队）。"""
        return self.enabled and self._semaphore().locked()

    def describe(self) -> str:
        if not self.enabled:
            return "并发不限"
        s = f"{self.running}/{self.limit} 在跑"
        if self.waiting:
            s += f"，{self.waiting} 排队"
        return s

    async def acquire(
        self,
        *,
        timeout: Optional[float] = None,
        abort: Optional[Callable[[], bool]] = None,
    ) -> str:
        """占一个名额。返回 "ok" / "timeout" / "aborted"。只有 "ok" 才需要 release()。

        timeout 不传时用 self.max_wait（0 = 一直等）。
        """
        if not self.enabled:
            self.running += 1
            return "ok"
        if timeout is None:
            timeout = self.max_wait

        sem = self._semaphore()
        if not sem.locked():          # 有空位：直接拿，不必起等待 task
            await sem.acquire()
            self.running += 1
            return "ok"

        acq = asyncio.ensure_future(sem.acquire())
        watcher = (
            asyncio.ensure_future(self._watch_abort(abort))
            if abort is not None else None
        )
        waiters = [acq] + ([watcher] if watcher else [])
        self.waiting += 1
        try:
            done, _pending = await asyncio.wait(
                waiters,
                timeout=timeout if timeout and timeout > 0 else None,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self.waiting -= 1
            if watcher is not None:
                watcher.cancel()

        if acq in done:
            exc = acq.exception()
            if exc is not None:
                raise exc
            self.running += 1
            return "ok"

        # 没等到名额：撤掉排队位。cancel 与"名额刚到手"存在窗口 —— 真到手了就立刻
        # 还回去，否则这个额度会永久漏掉（跑久了并发上限自己越缩越小）。
        acq.cancel()
        await asyncio.gather(acq, return_exceptions=True)
        if acq.done() and not acq.cancelled() and acq.exception() is None:
            sem.release()
        return "aborted" if (watcher is not None and watcher in done) else "timeout"

    def release(self) -> None:
        self.running = max(0, self.running - 1)
        if self.enabled and self._sem is not None:
            self._sem.release()

    async def _watch_abort(self, abort: Callable[[], bool]) -> None:
        """轮询 abort 谓词；返回即代表"别等了"。用轮询而不是 Event，是为了不给
        ActiveRun 加状态、也让 mock 出来的 run 照样能走这条路径。"""
        while True:
            try:
                if abort():
                    return
            except Exception:
                return
            await asyncio.sleep(_ABORT_POLL_INTERVAL_SECONDS)


RUN_GATE = RunGate(MAX_CONCURRENT_RUNS, max_wait=QUEUE_MAX_WAIT_SECONDS)
