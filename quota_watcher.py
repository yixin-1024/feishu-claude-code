"""
Claude Max 用量监控：后台定期拉 quota headers，跨阈值 / 窗口重置时主动通报。

策略：
- 5h 和 7d 各自维护已跨过的阈值集合，避免反复发同一阈值消息。
- reset 时间戳变化 = 窗口重置：清空跨阈值集合 + 发一条"额度已重置"。
  注意 Anthropic 的 r5h/r7d 是滚动窗口结束时间，每次响应都会向后推一点点。
  真正的"窗口重置"语义是：utilization 大幅回落（旧值 > 当前阈值水位但新值低于）。
  → 用 "utilization 显著下降" 作为重置判据，比 reset 时间靠谱。

state 持久化到 ~/.feishu-claude/quota_state.json。

接口：start_watcher_thread(send_fn, interval) 起后台线程；send_fn(text) 是同步可调用
（被 watcher 线程直接调），实现者负责把消息投到 bot_loop 上发出去。

可选挂账户智能切换器：start_watcher_thread(..., switcher=AccountSwitcher(...))。
每次 poll 之后调 switcher.maybe_switch()——切换条件 / 冷却 / 防抖都在 switcher 内部。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Optional

# 默认阈值（utilization 比例，0..1）。跨过即发。
THRESHOLDS: tuple[float, ...] = (0.25, 0.50, 0.75, 0.95)

# 重置判据：utilization 比上次低这么多 = 窗口重置（清阈值缓存）。
# Anthropic 的 reset 时间戳每次都会微调，不能直接拿它判等。
_RESET_DROP = 0.20

# 默认 poll 间隔
DEFAULT_INTERVAL_SEC = 600  # 10 分钟

_STATE_DIR = os.path.expanduser("~/.feishu-claude")
_STATE_FILE = os.path.join(_STATE_DIR, "quota_state.json")


def _load_state() -> dict:
    if not os.path.isfile(_STATE_FILE):
        return {}
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
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
        print(f"[quota_watcher] save state 失败: {e}", flush=True)


def _crossed_new_thresholds(
    prev_util: Optional[float],
    cur_util: float,
    already_crossed: list[float],
) -> list[float]:
    """返回本次新跨过的阈值列表（升序）。"""
    out = []
    for thr in THRESHOLDS:
        if thr in already_crossed:
            continue
        # 跨过：当前已到/超过 thr。prev 不强制要求 < thr——首次启动时 prev=None，
        # 只要当前已过 thr 就算"未通报过"，应当发。
        if cur_util >= thr:
            out.append(thr)
    return out


def _detect_reset(prev_util: Optional[float], cur_util: float) -> bool:
    """utilization 显著下降 → 窗口重置。prev 缺失视为冷启动，不算重置。"""
    if prev_util is None:
        return False
    return prev_util - cur_util >= _RESET_DROP


def _format_alert_lines(
    *,
    window_label: str,
    util: float,
    crossed: list[float],
    is_reset: bool,
    reset_ts: Optional[int],
) -> list[str]:
    """生成本次该发的消息行。空列表 = 无需通报。"""
    if not crossed and not is_reset:
        return []
    lines: list[str] = []
    pct = util * 100
    if is_reset:
        lines.append(f"♻️ {window_label} 额度已重置（当前 {pct:.1f}%）")
    if crossed:
        # 同次 poll 跨过多个阈值时，只播最高那个最有意义
        thr_pct = int(crossed[-1] * 100)
        emoji = "🟢" if thr_pct < 50 else ("🟡" if thr_pct < 75 else ("🟠" if thr_pct < 95 else "🔴"))
        lines.append(
            f"{emoji} {window_label} 用量已超 **{thr_pct}%**（当前 {pct:.1f}%）"
        )
    if reset_ts:
        try:
            from datetime import datetime
            dt = datetime.fromtimestamp(int(reset_ts))
            diff = dt - datetime.now()
            hh = int(diff.total_seconds() // 3600)
            mm = int((diff.total_seconds() % 3600) // 60)
            lines.append(f"  重置：{dt.strftime('%m/%d %H:%M')}（{hh}h{mm}m 后）")
        except Exception:
            pass
    return lines


def poll_once(send_fn: Callable[[str], None], switcher: Optional[Any] = None) -> None:
    """跑一次：拉 quota → 比对 state → 必要时调 send_fn。

    switcher：可选 AccountSwitcher 实例。poll 完后调 switcher.maybe_switch()——
    它内部会探测所有账户、判定是否要切、必要时切换并通报。即使没有发用量告警，
    也会检查（候选明显更优时仍可主动切）。
    """
    from commands import fetch_quota_headers  # 延迟 import 避免循环

    data = fetch_quota_headers()
    if not data.get("ok"):
        # 当前账户 poll 失败本身就是「当前账户可能不健康」的强信号——不能因此
        # 跳过切换判定，否则当前账户限流/挂掉时永远轮不到 maybe_switch()（循环依赖）。
        print(f"[quota_watcher] poll 失败: {data.get('error')}（仍跑账户切换判定）", flush=True)
        if switcher is not None:
            try:
                switcher.maybe_switch()
            except Exception as e:
                print(f"[quota_watcher] switcher.maybe_switch 异常: {e}", flush=True)
        return

    state = _load_state()
    state.setdefault("5h", {"crossed": [], "util": None, "reset": None})
    state.setdefault("7d", {"crossed": [], "util": None, "reset": None})

    alert_lines: list[str] = []

    for key, win_label, util_key, reset_key in [
        ("5h", "5h", "u5h", "r5h"),
        ("7d", "7d", "u7d", "r7d"),
    ]:
        cur_util = data.get(util_key)
        cur_reset = data.get(reset_key)
        if cur_util is None:
            continue
        win_state = state[key]
        prev_util = win_state.get("util")
        crossed_list = list(win_state.get("crossed") or [])
        first_run = prev_util is None

        is_reset = _detect_reset(prev_util, cur_util)
        if is_reset:
            crossed_list = []

        new_crossed = _crossed_new_thresholds(prev_util, cur_util, crossed_list)
        crossed_list.extend(new_crossed)

        # 首次启动：把"已超过的阈值"全部静默落地，避免重启 bot 刷一堆历史水位
        if first_run:
            lines: list[str] = []
        else:
            lines = _format_alert_lines(
                window_label=win_label,
                util=cur_util,
                crossed=new_crossed,
                is_reset=is_reset,
                reset_ts=cur_reset,
            )
        if lines:
            alert_lines.extend(lines)

        win_state["util"] = cur_util
        win_state["reset"] = cur_reset
        win_state["crossed"] = sorted(set(crossed_list))

    _save_state(state)

    if alert_lines:
        msg = "📊 **Claude Max 用量通报**\n\n" + "\n".join(alert_lines)
        try:
            send_fn(msg)
        except Exception as e:
            print(f"[quota_watcher] send_fn 失败: {e}", flush=True)

    # 账户智能切换：watcher 之外没人定期跑探测，借这条线程顺带做。失败不影响 watcher。
    if switcher is not None:
        try:
            switcher.maybe_switch()
        except Exception as e:
            print(f"[quota_watcher] switcher.maybe_switch 异常: {e}", flush=True)


def start_watcher_thread(
    send_fn: Callable[[str], None],
    interval: int = DEFAULT_INTERVAL_SEC,
    switcher: Optional[Any] = None,
) -> threading.Thread:
    """起后台线程定期 poll。send_fn 同步签名，内部自己 schedule 到 bot_loop。

    switcher：可选 AccountSwitcher。每次 poll 顺带跑一次切换判定。
    """

    def _loop():
        # 第一次 poll 延迟一点，让 bot 完全起来再发
        time.sleep(30)
        while True:
            try:
                poll_once(send_fn, switcher=switcher)
            except Exception as e:
                print(f"[quota_watcher] 异常: {e}", flush=True)
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="quota-watcher")
    t.start()
    print(
        f"[quota_watcher] 已启动，每 {interval}s 拉一次 quota（阈值 {THRESHOLDS}）"
        + (f"；账户智能切换：开" if switcher is not None else ""),
        flush=True,
    )
    return t
