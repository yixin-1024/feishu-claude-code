"""Private, atomic dispatch completion mailbox for local consumers (e.g. cc voice).

Only the dispatcher writes terminal states. Model messages are never parsed as
completion signals. Caller identity remains attached across cross-agent routing.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


def _directory() -> Path:
    return Path(os.getenv("CC_LARK_TASK_RESULTS_DIR") or
                Path(__file__).resolve().parent / "data" / "task_results")


def _path(thread_id: str) -> Path:
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("thread_id is required")
    return _directory() / (hashlib.sha256(thread_id.encode()).hexdigest() + ".json")


def _write(record: dict) -> None:
    path = _path(record["thread_id"])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".result-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def begin(*, thread_id: str, profile: str, user_id: str, title: str,
          chat_id: str) -> None:
    if not profile or not user_id:
        raise ValueError("dispatch result requires caller identity")
    _write(dict(thread_id=thread_id, profile=profile, user_id=user_id,
                title=title, chat_id=chat_id, status="running", result="",
                updated_at=time.time()))


def finish(thread_id: str, *, ok: bool, text: str, cancelled: bool = False) -> None:
    path = _path(thread_id)
    record = json.loads(path.read_text())
    # A duplicate callback must not rewrite a terminal result.
    if record["status"] != "running":
        return
    record.update(status="cancelled" if cancelled else ("completed" if ok else "failed"),
                  result=str(text)[:12000], updated_at=time.time())
    _write(record)


def get(thread_id: str, *, profile: str, user_id: str) -> dict:
    try:
        record = json.loads(_path(thread_id).read_text())
    except FileNotFoundError:
        return {"thread_id": thread_id, "status": "unavailable",
                "detail": "No completion record. The dispatcher may need /restart."}
    if not profile or not user_id or (record.get("profile"), record.get("user_id")) != (profile, user_id):
        raise PermissionError("task result belongs to another caller")
    return {k: v for k, v in record.items() if k not in ("profile", "user_id")}
