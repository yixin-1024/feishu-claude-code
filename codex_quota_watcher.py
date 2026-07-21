"""
Codex 用量监控：后台每 30 min 拉一次 Codex 额度快照，检测到窗口重置就主动通报。

背景：Codex Pro 现在只有一个 7 天固定窗口（没有 5h 窗口了），到 `resets_at` 时刻
整段回血。用户常年顶到 100% 被限流，最关心的就是"什么时候能接着用"。所以这个
watcher 只做一件事：检测到某个窗口重置（used_percent 从高位显著回落，或 resets_at
向后跳一整段）时，给 owner 发一条 Lark 私信提醒可以继续用了。

reset 判据（二选一即触发）：
- resets_at 向后跳 > _RESET_ADVANCE_SEC（固定窗口滚到下一段 = 真重置，最可靠）；
- used_percent 从 ≥ _RESET_MIN_PREV 显著回落 ≥ _RESET_DROP（RPC 没给 resets_at 时的兜底）。

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
from typing import Callable, Optional

# 默认 poll 间隔：30 分钟
DEFAULT_INTERVAL_SEC = 1800

# used_percent 回落这么多（百分点）视为窗口重置的兜底信号
_RESET_DROP = 20.0
# 且回落前至少到过这个水位，避免小波动误报
_RESET_MIN_PREV = 50.0
# resets_at 向后跳超过这么多秒 = 窗口滚到下一段（忽略 1~2s 的微漂移）
_RESET_ADVANCE_SEC = 3600

_STATE_DIR = os.path.expanduser("~/.feishu-claude")
_STATE_FILE = os.path.join(_STATE_DIR, "codex_quota_state.json")


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
    return advanced or dropped


def poll_once(send_fn: Callable[[str], None]) -> None:
    """跑一次：拉 codex 额度 → 比对 state → 检测到重置就 send_fn。"""
    # 延迟 import 避免与 commands 循环依赖
    from commands import (
        _get_codex_rate_limits,
        _codex_window_label,
        _fmt_codex_reset_ts,
    )

    rate_limits = _get_codex_rate_limits()
    if not rate_limits:
        # 拉取失败（codex 未登录 / app-server 起不来）——跳过这轮，不改 state
        print("[codex_quota_watcher] 额度获取失败，跳过本轮", flush=True)
        return

    state = _load_state()
    reset_blocks: list[str] = []

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


def start_watcher_thread(
    send_fn: Callable[[str], None],
    interval: int = DEFAULT_INTERVAL_SEC,
) -> threading.Thread:
    """起后台线程定期 poll。send_fn 同步签名，内部自己 schedule 到 bot_loop。"""

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
