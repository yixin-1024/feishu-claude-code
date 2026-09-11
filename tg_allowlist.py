"""Telegram 的「自助授权」白名单：一次性口令换永久白名单，落盘。

为什么需要：Telegram bot 的白名单填的是**数字 user id**，而机主一开始并不知道
自己的 id —— 得先给 bot 发一条消息、有人去日志里捞出来、写进 .env、再重启一次。
中间那个"有人"如果不在（比如机主半夜自己装），整个流程就卡住。

于是给一条自助通道：`<PROFILE>_CLAIM_CODE` 配一串随机口令，机主**私聊** bot 把这串
口令原样发过去，bot 当场把他的 user id 记进白名单并落盘（`~/.feishu-claude/`），
下一条消息就能正常干活 —— 不用改 .env、不用再重启。

安全边界：
  · 只在**私聊**里认（群里发口令一律无效，免得口令在群历史里泄露还生效）；
  · 必须**整条消息就是口令**（不做包含匹配，避免复述/引用误触发）；
  · 口令用完不失效（机主可能有第二台设备、或要给同事开通），但每次授权都打日志；
  · 落盘文件 0600，只存 id 列表。
"""

from __future__ import annotations

import json
import os
import threading

_DIR = os.path.expanduser("~/.feishu-claude")
_lock = threading.Lock()


def path_for(profile: str) -> str:
    base = os.environ.get("CC_TG_ALLOWLIST_DIR", "").strip() or _DIR
    return os.path.join(base, f"tg-allowed-{profile}.json")


def load(profile: str) -> set[str]:
    """读已落盘的自助授权 id。文件坏了/没有都返回空集合，绝不抛。"""
    try:
        with open(path_for(profile), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    if isinstance(data, dict):
        data = data.get("user_ids", [])
    if not isinstance(data, list):
        return set()
    return {str(x).strip() for x in data if str(x).strip()}


def add(profile: str, user_id: str) -> bool:
    """把一个 id 加进落盘白名单。返回是否是新增（已存在返回 False）。"""
    uid = str(user_id).strip()
    if not uid:
        return False
    with _lock:
        current = load(profile)
        if uid in current:
            return False
        current.add(uid)
        target = path_for(profile)
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = f"{target}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"user_ids": sorted(current)}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
            os.chmod(target, 0o600)
        except OSError as e:
            print(f"[tg] 自助授权落盘失败（本进程内仍生效）: {e}", flush=True)
        return True
