"""/rewind 和 /fork：改的是「下一次 resume 这个会话时」claude 的启动参数。

两件事 CLI 都有现成参数（2.1.327 实测，见 project memory）：
- 回退：`--resume S --resume-session-at <assistant uuid>`。只保留到这条 assistant 消息为止，
  会话 id 不变，之后再普通 `--resume S` 也接着回退后的分支（不用每次都带）。
- 分叉：`--resume S --fork-session --session-id NEW`。新会话用我们预先定好的 NEW，原会话不动。

bot 这边只在 /rewind、/fork 时记一条「这个会话下次启动要带的参数」，claude_runner 起进程时
带上，等 CLI 吐出第一个 system 事件（参数被接受、会话已建好）才删掉。落盘，/restart 不丢。
零依赖叶子模块：claude_runner 和 dispatcher 都 import 它。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Optional

# CLI 不接受参数、一个 system 事件都没吐就退出时，最多再试这么多次，免得每轮都带着坏参数挂掉
_MAX_FAILED_STARTS = 2
_lock = threading.Lock()


def _path() -> str:
    override = (os.getenv("CC_LARK_RESUME_OPTS_FILE") or "").strip()
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "resume_options.json")


def _load() -> dict[str, dict]:
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[session_branch] ⚠️ 读 {_path()} 失败: {type(e).__name__}: {e}", flush=True)
        return {}


def _save(data: dict[str, dict]) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def get_option(session_id: str) -> Optional[dict]:
    if not session_id:
        return None
    with _lock:
        opt = _load().get(session_id)
    return dict(opt) if isinstance(opt, dict) else None


def set_option(session_id: str, *, resume_at: str = "", fork_from: str = "") -> None:
    with _lock:
        data = _load()
        opt: dict = {}
        if resume_at:
            opt["resume_at"] = resume_at
        if fork_from:
            opt["fork_from"] = fork_from
        if opt:
            data[session_id] = opt
        else:
            data.pop(session_id, None)
        _save(data)


def consume(session_id: str) -> None:
    """CLI 已经按这组参数建好会话：删掉，之后普通 --resume 就行。"""
    with _lock:
        data = _load()
        if data.pop(session_id, None) is not None:
            _save(data)


def note_failed_start(session_id: str) -> None:
    """带着参数起进程，却一个 system 事件都没等到就退出了：计一次，超过上限就丢掉。"""
    with _lock:
        data = _load()
        opt = data.get(session_id)
        if not isinstance(opt, dict):
            return
        opt["failed_starts"] = int(opt.get("failed_starts") or 0) + 1
        if opt["failed_starts"] >= _MAX_FAILED_STARTS:
            print(f"[session_branch] 会话 {session_id[:8]} 的启动参数连续失败，已丢弃: {opt}", flush=True)
            data.pop(session_id, None)
        _save(data)


def cli_args(session_id: str, opt: Optional[dict]) -> list[str]:
    """claude 的 --resume 那几个参数。opt 为空 = 普通 resume。"""
    if not session_id:
        return []
    opt = opt or {}
    fork_from = opt.get("fork_from") or ""
    resume_at = opt.get("resume_at") or ""
    if fork_from:
        args = ["--resume", fork_from]
        if resume_at:
            args += ["--resume-session-at", resume_at]
        return args + ["--fork-session", "--session-id", session_id]
    args = ["--resume", session_id]
    if resume_at:
        args += ["--resume-session-at", resume_at]
    return args


# ── 从会话记录里找回退点 ────────────────────────────────────────────────


def _is_real_user_turn(rec: dict) -> bool:
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        kinds = {c.get("type") for c in content if isinstance(c, dict)}
        return "tool_result" not in kinds and bool(kinds & {"text", "image", "document"})
    return False


def _preview(rec: dict, limit: int = 40) -> str:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        text = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    else:
        text = str(content or "")
    marker = "【用户刚刚 @ 你并说】"
    if marker in text:
        text = text.split(marker, 1)[1]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("【")]
    text = " ".join(lines) or text.strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def rewind_target(fpath: str, turns: int = 1, leaf_uuid: str = "") -> tuple[str, list[str]]:
    """退回 turns 轮：返回 (要 resume 到的 assistant 消息 uuid, 被撤掉的用户消息预览，从早到晚)。

    沿当前分支（从最后一条消息或 leaf_uuid 顺着 parentUuid 往上）数用户消息，跳过工具结果、
    meta、压缩摘要；第 turns 条之前最近的那条 assistant 消息就是回退点。退不了抛 ValueError。
    """
    records: dict[str, dict] = {}
    last_uuid = ""
    with open(fpath, encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            uid = rec.get("uuid")
            if not uid:
                continue
            records[uid] = rec
            if rec.get("type") in ("user", "assistant"):
                last_uuid = uid
    cur = leaf_uuid or last_uuid
    if not cur or cur not in records:
        raise ValueError("会话记录里找不到消息")
    chain: list[dict] = []
    seen: set[str] = set()
    while cur and cur in records and cur not in seen:
        seen.add(cur)
        rec = records[cur]
        chain.append(rec)
        cur = rec.get("parentUuid") or ""
    chain.reverse()  # 从早到晚
    user_idx = [i for i, r in enumerate(chain) if _is_real_user_turn(r)]
    if len(user_idx) < turns:
        raise ValueError(f"当前对话只有 {len(user_idx)} 轮可退（压缩过的部分退不回去）")
    first = user_idx[-turns]
    target = next((chain[i]["uuid"] for i in range(first - 1, -1, -1) if chain[i].get("type") == "assistant"), "")
    if not target:
        raise ValueError("已经退到第一轮了，想整个重来用 /new")
    return target, [_preview(chain[i]) for i in user_idx[-turns:]]
