"""
Codex 用量监控：后台每 30 min 拉一次 Codex 额度快照，检测到窗口重置就主动通报。

背景：Codex Pro 现在只有一个 7 天固定窗口（没有 5h 窗口了），到 `resets_at` 时刻
整段回血。用户常年顶到 100% 被限流，最关心的就是"什么时候能接着用"。所以这个
watcher 只做一件事：检测到某个窗口的 used_percent 确实从高位回落（并结合
resets_at 跳变辅助判断）时，给 owner 发一条 Lark 私信提醒可以继续用了。

reset 判据必须能看到真实额度恢复：
- used_percent 从高位显著回落；或
- resets_at 向后跳一段，同时 used_percent 已从高位回落到可用区间。

不能只凭 resets_at 向后跳就通报：Codex 有时会调整该时间戳，即使用量仍在上升。
state 的读改写由文件锁保护，避免滚动部署或残留双进程对同一次重置重复通报。

首次启动只静默落地当前水位，不通报（避免重启 bot 就刷一条）。
state 持久化到 ~/.feishu-claude/codex_quota_state.json。

接口：start_watcher_thread(send_fn, interval) 起后台线程；send_fn(text) 同步签名，
实现者负责把消息投到 bot_loop 上发出去（见 runtime.start_codex_quota_watcher）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

import fcntl

# 默认 poll 间隔：30 分钟
DEFAULT_INTERVAL_SEC = 1800

# used_percent 回落这么多（百分点）视为窗口重置的兜底信号
_RESET_DROP = 20.0
# 且回落前至少到过这个水位，避免小波动误报
_RESET_MIN_PREV = 50.0
# resets_at 跳变时，当前用量还必须回到这个可用区间
_RESET_AVAILABLE_MAX = 50.0
# resets_at 向后跳超过这么多秒 = 窗口滚到下一段（忽略 1~2s 的微漂移）
_RESET_ADVANCE_SEC = 3600

_STATE_DIR = os.path.expanduser("~/.feishu-claude")
_STATE_FILE = os.path.join(_STATE_DIR, "codex_quota_state.json")
_STATE_LOCK_FILE = os.path.join(_STATE_DIR, "codex_quota_state.lock")


@contextmanager
def _state_lock():
    """跨线程/进程串行化 state 的读改写，防止同一重置被重复通报。"""
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(_STATE_LOCK_FILE, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_state() -> dict:
    if not os.path.isfile(_STATE_FILE):
        return {}
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, _STATE_FILE)
    except Exception as e:
        print(f"[codex_quota_watcher] save state 失败: {e}", flush=True)


def _detect_reset(
    prev_used: Optional[float],
    cur_used: float,
    prev_resets: Optional[int],
    cur_resets: Optional[int],
) -> bool:
    """判定窗口是否重置。prev 缺失（冷启动）一律不算重置。"""
    if prev_used is None:
        return False
    advanced = bool(
        prev_resets and cur_resets and (cur_resets - prev_resets) > _RESET_ADVANCE_SEC
    )
    dropped = prev_used >= _RESET_MIN_PREV and (prev_used - cur_used) >= _RESET_DROP
    advanced_with_recovery = bool(
        advanced
        and prev_used >= _RESET_MIN_PREV
        and cur_used < prev_used
        and cur_used <= _RESET_AVAILABLE_MAX
    )
    return dropped or advanced_with_recovery


def _process_snapshot(rate_limits: dict, send_fn: Callable[[str], None]) -> None:
    """比对一份额度快照和 state：检测到重置就 send_fn。poll 和运行中推送共用。"""
    from commands import _codex_window_label, _fmt_codex_reset_ts

    reset_blocks: list[str] = []
    # 只串行化本地 state 的读改写，避免慢请求长期占锁。
    with _state_lock():
        state = _load_state()

        for key in ("primary", "secondary"):
            window = rate_limits.get(key) or {}
            used = window.get("usedPercent")
            if used is None:
                continue
            used = float(used)
            resets = window.get("resetsAt")
            resets = int(resets) if resets else None

            win_state = state.get(key) or {}
            prev_used = win_state.get("used")
            prev_resets = win_state.get("resets")

            if _detect_reset(prev_used, used, prev_resets, resets):
                label = _codex_window_label(window.get("windowDurationMins"))
                prev_txt = f"（之前 {prev_used:.0f}%）" if prev_used is not None else ""
                block = f"**{label}窗口**：当前 {used:.1f}%{prev_txt}"
                if resets:
                    block += f"\n  下次重置：{_fmt_codex_reset_ts(resets)}"
                reset_blocks.append(block)

            state[key] = {"used": used, "resets": resets}

        # 必须在释放锁前持久化；下一条并发 poll 才能看到本次已处理的快照。
        _save_state(state)

    if reset_blocks:
        msg = (
            "♻️ **Codex 额度已重置**\n\n"
            + "\n".join(reset_blocks)
            + "\n\n可以继续用 Codex 了 🎉"
        )
        try:
            send_fn(msg)
        except Exception as e:
            print(f"[codex_quota_watcher] send_fn 失败: {e}", flush=True)


def poll_once(send_fn: Callable[[str], None]) -> None:
    """跑一次：拉 codex 额度 → 比对 state → 检测到重置就 send_fn。"""
    # 延迟 import 避免与 commands 循环依赖
    from commands import _get_codex_rate_limits

    rate_limits = _get_codex_rate_limits()
    if not rate_limits:
        # 拉取失败（codex 未登录 / app-server 起不来）——跳过这轮，不改 state
        print("[codex_quota_watcher] 额度获取失败，跳过本轮", flush=True)
        return
    # RPC 在锁外；_process_snapshot 只锁本地 state。
    _process_snapshot(rate_limits, send_fn)


# ── 运行中推送（codex app-server 的 account/rateLimits/updated）──────────────
# 每次模型响应后 app-server 都会推一份额度快照。落一份到磁盘给 /usage、/status 当
# 「最近上报」（比翻 session 文件新），并在 watcher 已启动时立刻做一次重置检测——
# 不用干等 30 分钟一次的 poll。推送很密，按 _OBSERVE_MIN_INTERVAL 节流。
_PUSHED_FILE = os.path.join(_STATE_DIR, "codex_rate_limits_pushed.json")
_OBSERVE_MIN_INTERVAL = 60.0
_send_fn: Optional[Callable[[str], None]] = None
_observe_lock = threading.Lock()
_last_observed = 0.0


def observe_rate_limits(snapshot: dict) -> bool:
    """处理一份推送来的额度快照。返回 True = 这次真的处理了（没被过滤 / 节流）。"""
    global _last_observed
    if not isinstance(snapshot, dict):
        return False
    # 模型专属的额度（limitId 不是 codex）不进主窗口——和 t3code 的口径一致
    limit_id = snapshot.get("limitId")
    if limit_id and limit_id != "codex":
        return False
    windows = {k: snapshot.get(k) for k in ("primary", "secondary") if isinstance(snapshot.get(k), dict)}
    if not windows:
        return False
    with _observe_lock:
        now = time.monotonic()
        if _last_observed and now - _last_observed < _OBSERVE_MIN_INTERVAL:
            return False
        _last_observed = now
    record = {
        **{k: {
            "usedPercent": w.get("usedPercent"),
            "windowDurationMins": w.get("windowDurationMins"),
            "resetsAt": w.get("resetsAt"),
        } for k, w in windows.items()},
        "_source": "pushed",
        "_at": time.time(),
    }
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        tmp = _PUSHED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(tmp, _PUSHED_FILE)
    except Exception as e:
        print(f"[codex_quota_watcher] 写推送快照失败: {e}", flush=True)
    if _send_fn is not None:
        _process_snapshot(record, _send_fn)
    return True


def read_pushed_rate_limits(max_age_sec: float = 6 * 3600) -> dict:
    """最近一次运行中推送的额度快照（太旧就不要）。"""
    try:
        with open(_PUSHED_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    at = float(data.get("_at") or 0)
    if not at or time.time() - at > max_age_sec:
        return {}
    return data


def start_watcher_thread(
    send_fn: Callable[[str], None],
    interval: int = DEFAULT_INTERVAL_SEC,
) -> threading.Thread:
    """起后台线程定期 poll。send_fn 同步签名，内部自己 schedule 到 bot_loop。

    同时登记 send_fn，让 observe_rate_limits（运行中推送）也能当场通报重置。
    """
    global _send_fn
    _send_fn = send_fn

    def _loop():
        # 首轮延迟，等 bot 完全起来 + 错开 Claude quota_watcher
        time.sleep(45)
        while True:
            try:
                poll_once(send_fn)
            except Exception as e:
                print(f"[codex_quota_watcher] 异常: {e}", flush=True)
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="codex-quota-watcher")
    t.start()
    print(
        f"[codex_quota_watcher] 已启动，每 {interval}s 查一次 Codex 额度（重置即通报）",
        flush=True,
    )
    return t
