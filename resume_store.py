"""被中断的 run 落盘 → 服务起来后自动续跑。

痛点（用户实际踩的）：`/restart` 会把正在跑的 run 直接打断，卡片停在
"♻️ 本次任务被中断，~5s 后再发一遍"——人必须回来手动把指令重发一遍或者说
"继续"，而且中断前跑到哪了只能靠模型自己回忆。进程崩溃 / 机器重启同理。

这里给每个**真正开跑**的 run 落一条磁盘记录，进程重启后由
`scheduler.restore_pending_resumes()` 把它投回原来那条话题自动续跑。

设计要点：
- 记录跟着 run 的生命周期建立 / 删除（跑完就删），所以磁盘上**残留的记录 ==
  上一个进程没跑完就没了**，语义单一，不需要额外的心跳或状态机。
- `/restart` 走 `mark_interrupted()` 显式打标（带上中断前卡片上的进度），并让
  run 的 finally 别把记录删掉（`ActiveRun.keep_resume`）。
- attempt 计数跨进程累加：同一条 (profile, chat) 连续被打断 N 次就不再自动续跑，
  避免"续跑 → 又崩 → 再续跑"的死循环把额度烧光。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional

_LOCK = threading.RLock()

# 同一 key 已经自动续跑过几次（key → 次数）。启动时由 load_all() 从落盘记录里恢复，
# run 正常跑完（drop）时清零。只在内存里，重启后靠记录里的 attempt 字段接续。
_ATTEMPTS: dict[str, int] = {}

_PROMPT_LIMIT = 4000
_PROGRESS_LIMIT = 1500

# 续跑指令的开头标记。续跑起来的 run 再被打断时，落盘要存**原始指令**而不是上一版
# 续跑指令——否则第 2、3 次续跑的 prompt 会把上一版整个包进去，层层套娃：原指令被
# 埋在三层模板最里面，还白烧 token。key → 原始指令，由 bump_attempt 在投递时登记。
_RESUME_MARKER = "[♻️ 重启续跑]"
_ORIGINALS: dict[str, str] = {}


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def enabled() -> bool:
    """自动续跑总开关（CC_LARK_RESUME_AFTER_RESTART=0 关掉）。"""
    return _env_flag("CC_LARK_RESUME_AFTER_RESTART", True)


def delay_seconds() -> int:
    """启动后隔多久才投第一条续跑——留出 WS / channel 连上的时间。"""
    return max(1, _env_int("CC_LARK_RESUME_DELAY_SEC", 15))


def stagger_seconds() -> int:
    """多条续跑之间的间隔，避免一开机就把并发闸门顶满。"""
    return max(0, _env_int("CC_LARK_RESUME_STAGGER_SEC", 5))


def max_attempts() -> int:
    """同一条任务最多自动续跑几次（超过就只提示、不再自动跑）。"""
    return max(1, _env_int("CC_LARK_RESUME_MAX_ATTEMPTS", 3))


def max_age_minutes() -> int:
    """中断多久之后就不再自动续跑（机器关了一整天再开机，续跑没意义反而危险）。"""
    return max(1, _env_int("CC_LARK_RESUME_MAX_AGE_MIN", 120))


def store_path() -> str:
    override = (os.getenv("CC_LARK_RESUME_STORE") or "").strip()
    if override:
        return override
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "pending_resumes.json"
    )


def make_key(profile: str, user_id: str, chat_id: str) -> str:
    return f"{profile}::{user_id}::{chat_id}"


def _load() -> dict[str, dict]:
    try:
        with open(store_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 — 坏文件不能把 run 拖死
        print(f"[resume] ⚠️ 读取 {store_path()} 失败: {type(e).__name__}: {e}", flush=True)
        return {}


def _save(data: dict[str, dict]) -> None:
    path = store_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _write(mutate) -> None:
    """读-改-写一次落盘。任何异常只 log —— 续跑是增强，绝不能反过来弄挂 run。"""
    with _LOCK:
        data = _load()
        try:
            if mutate(data) is False:
                return
            _save(data)
        except Exception as e:  # noqa: BLE001
            print(f"[resume] ⚠️ 写 {store_path()} 失败: {type(e).__name__}: {e}", flush=True)


def record(
    *,
    profile: str,
    user_id: str,
    chat_id: str,
    is_group: bool,
    thread_id: str,
    anchor: str,
    card_msg_id: str,
    prompt: str,
    preview: str = "",
    reason: str = "",
) -> str:
    """run 开跑时落一条记录；返回 key（run 结束时用它 drop）。"""
    if not enabled():
        return ""
    key = make_key(profile, user_id, chat_id)
    # 这一轮的输入本身就是续跑指令 → 存投递时登记的原始指令，避免套娃。
    if _RESUME_MARKER in (prompt or "")[:200]:
        prompt = _ORIGINALS.get(key) or prompt
    rec = {
        "key": key,
        "profile": profile,
        "user_id": user_id,
        "chat_id": chat_id,
        "is_group": bool(is_group),
        "thread_id": thread_id or "",
        "anchor": anchor or "",
        "card_msg_id": card_msg_id or "",
        "prompt": (prompt or "")[:_PROMPT_LIMIT],
        "preview": (preview or "")[:200],
        "progress": "",
        "reason": reason,
        "started_at": time.time(),
        "interrupted_at": 0.0,
        "attempt": _ATTEMPTS.get(key, 0),
    }
    _write(lambda data: data.__setitem__(key, rec))
    return key


def mark_interrupted(key: str, *, reason: str, progress: str = "") -> None:
    """把一条记录标成"被中断"，并存下中断前卡片上的进度（供续跑 prompt 用）。"""
    if not (key and enabled()):
        return

    def _mutate(data: dict[str, dict]):
        rec = data.get(key)
        if rec is None:
            return False
        rec["reason"] = reason
        rec["interrupted_at"] = time.time()
        if progress:
            rec["progress"] = progress[-_PROGRESS_LIMIT:]
        return True

    _write(_mutate)


def drop(key: str) -> None:
    """run 正常结束（成功 / 失败 / 被用户 /stop）→ 删记录 + 清 attempt 计数。"""
    if not key:
        return
    _ATTEMPTS.pop(key, None)
    _ORIGINALS.pop(key, None)

    def _mutate(data: dict[str, dict]):
        return data.pop(key, None) is not None

    _write(_mutate)


def load_all() -> list[dict]:
    """读出所有残留记录（= 上个进程没跑完的 run），并把 attempt 计数装回内存。"""
    with _LOCK:
        data = _load()
    recs = [r for r in data.values() if isinstance(r, dict) and r.get("chat_id")]
    for rec in recs:
        try:
            _ATTEMPTS[rec["key"]] = int(rec.get("attempt") or 0)
        except (KeyError, TypeError, ValueError):
            continue
    recs.sort(key=lambda r: r.get("interrupted_at") or r.get("started_at") or 0)
    return recs


def bump_attempt(key: str, original_prompt: str = "") -> int:
    """投出一次续跑 → attempt+1（新 run 的 record() 会带上这个值）。

    同时写回磁盘：续跑投出去之后如果连 run 都没起来（权限被拒 / 直投异常）就又
    重启一次，只加内存计数的话次数永远归零，"续跑→挂→再续跑"能无限循环。

    original_prompt = 这条任务最初的那句指令。登记下来，让续跑起来的 run 再被打断
    时落盘存的还是它，而不是包了一层的续跑指令（防套娃）。
    """
    if not key:
        return 0
    if original_prompt:
        _ORIGINALS[key] = original_prompt
    _ATTEMPTS[key] = _ATTEMPTS.get(key, 0) + 1
    nth = _ATTEMPTS[key]

    def _mutate(data: dict[str, dict]):
        rec = data.get(key)
        if rec is None:
            return False
        rec["attempt"] = nth
        return True

    _write(_mutate)
    return nth


def clear_all() -> None:
    """整表清空（测试 / 手工救火用）。"""
    _ATTEMPTS.clear()
    _ORIGINALS.clear()
    _write(lambda data: (data.clear(), True)[1])


def interrupted_at(rec: dict) -> float:
    for field in ("interrupted_at", "started_at"):
        try:
            ts = float(rec.get(field) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts > 0:
            return ts
    return 0.0


def age_minutes(rec: dict, now: Optional[float] = None) -> float:
    ts = interrupted_at(rec)
    if ts <= 0:
        return 0.0
    return max(0.0, ((now or time.time()) - ts) / 60.0)


_REASON_TEXT = {
    "restart": "cc-lark 服务被 /restart 重启",
    "queued": "cc-lark 服务重启",
    "crash": "cc-lark 进程意外退出（崩溃 / 被杀 / 机器重启）",
}


def build_resume_prompt(rec: dict[str, Any]) -> str:
    """把一条中断记录渲染成续跑指令。

    两种形态：
    - `reason == "queued"`：任务当时还在排队，一行都没跑过 —— 直接把原指令重放，
      别让模型去"核实上一步"（根本没有上一步，凭空核实只会瞎猜）。
    - 其它：跑到一半被打断 —— 带上原指令 + 中断前的可见进度，并**硬性要求先核实
      写操作是否已生效再继续**（重复开户 / 重复发消息 / 重复提交都是事故）。
    """
    prompt = (rec.get("prompt") or "").strip()
    # reason 为空 = 记录没被 /restart 打过标，说明上个进程是硬死的（崩溃 / 被杀 /
    # 机器重启），没机会走中断编排 —— 按 crash 措辞。
    reason = _REASON_TEXT.get(rec.get("reason") or "crash", _REASON_TEXT["crash"])
    attempt = int(rec.get("attempt") or 0)
    again = f"（⚠️ 这已经是这条任务第 {attempt + 1} 次被中断后续跑）" if attempt else ""

    if rec.get("reason") == "queued":
        return (
            f"[♻️ 重启续跑] 下面这条指令在{reason}时还排在队列里、一行都没开始跑，"
            f"现在服务已恢复，请正常从头执行它。{again}\n"
            f"（这是环境自动重投的，不用问用户要不要跑，也不用核实"
            f"「上一步做到哪」——没有上一步。）\n\n"
            f"原指令：\n---\n{prompt}\n---"
        )

    progress = (rec.get("progress") or "").strip()
    progress_block = (
        f"\n中断前卡片上最后可见的进度（可能不完整、也可能只是中间态）：\n---\n{progress}\n---\n"
        if progress
        else "\n（中断前卡片上还没有可见进度。）\n"
    )
    return (
        f"[♻️ 重启续跑] 你上一轮跑到一半时{reason}，那一轮被强制中断——"
        f"这是环境行为，不是你出错、也不是用户叫停。现在服务已恢复，请接着把它做完。{again}\n\n"
        f"被中断的那条指令原文：\n---\n{prompt}\n---\n"
        f"{progress_block}\n"
        f"续跑要求：\n"
        f"1. **先核实断点**：用最小代价确认上一步到底做没做成，尤其是写操作 / 外部接口调用 / "
        f"文件改动 / git 操作——已经生效的绝不要重复执行（重复开户、重复发消息、重复提交都算事故）。\n"
        f"2. 核实完从断点继续往下做，直到整件事真正完成；本话题的 session 上下文通常还在，"
        f"缺什么就读本话题历史或相关文件补齐。\n"
        f"3. 如果核实下来这件事其实已经做完了，直接给结论，不要重跑一遍。"
    )
