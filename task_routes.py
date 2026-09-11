"""Persistent dispatch ownership, independent of runner session IDs and run lifetime."""

from __future__ import annotations

import json
import os
import tempfile
import threading

_LOCK = threading.RLock()


def _path() -> str:
    return os.getenv("CC_LARK_TASK_ROUTES", "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "task_routes.json"
    )


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("invalid task route store")
    return data  # Corrupt ownership must fail closed, never become an empty registry.


def get(chat_id: str, thread_id: str) -> dict | None:
    with _LOCK:
        record = _load().get(f"{chat_id}:{thread_id}")
        if record is None:
            return None
        if not isinstance(record, dict) or any(
            not isinstance(record.get(k), str) or not record[k]
            for k in ("chat_id", "thread_id", "profile", "user_id", "anchor")
        ) or record["chat_id"] != chat_id or thread_id not in (record["thread_id"], record["anchor"]):
            raise ValueError("invalid task ownership record")
        return dict(record)


def bind(*, chat_id: str, thread_id: str, profile: str, user_id: str, anchor: str) -> dict:
    record = dict(chat_id=chat_id, thread_id=thread_id, profile=profile,
                  user_id=user_id, anchor=anchor)
    if any(not isinstance(v, str) or not v for v in record.values()):
        raise ValueError("incomplete task ownership")
    with _LOCK:
        data = _load()
        # The om_ anchor is also a valid lookup key when thread resolution was delayed.
        keys = {f"{chat_id}:{thread_id}", f"{chat_id}:{anchor}"}
        for key in keys:
            existing = data.get(key)
            canonicalizing = (isinstance(existing, dict)
                              and existing.get("thread_id") == anchor
                              and thread_id.startswith("omt_")
                              and {**existing, "thread_id": thread_id} == record)
            if existing is not None and existing != record and not canonicalizing:
                raise ValueError("conflicting task ownership")
        for key in keys:
            data[key] = record
        directory = os.path.dirname(_path()) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".task-routes-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, _path())
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return record
