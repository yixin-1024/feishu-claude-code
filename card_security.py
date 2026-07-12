"""Interactive-card action signing and authorization helpers.

The callback URL is public, so the JSON body is not an authority by itself.  Every
button value emitted by cc-lark carries an app-secret HMAC bound to its intended
user and message.  Both the HTTP callback path and the authenticated Lark WS path
verify the same envelope before dispatching a side effect.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import threading
import time
from typing import Any


_VERSION = 1
_SIGNATURE_FIELD = "_cc_sig"
_USER_FIELD = "_cc_uid"
_MESSAGE_FIELD = "_cc_mid"
_TIMESTAMP_FIELD = "_cc_ts"
_VERSION_FIELD = "_cc_v"
_CLOCK_SKEW_SEC = 300
_DEFAULT_TTL_SEC = 30 * 24 * 60 * 60
_EVENT_TTL_SEC = 10 * 60
_seen_events: dict[str, float] = {}
_seen_events_lock = threading.Lock()


def _canonical(value: dict[str, Any]) -> bytes:
    unsigned = {k: v for k, v in value.items() if k != _SIGNATURE_FIELD}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _signing_key(secret: str) -> bytes:
    """Domain-separate card signatures from any other use of the app secret."""
    return hmac.new(
        secret.encode("utf-8"), b"cc-lark/card-action/v1", hashlib.sha256,
    ).digest()


def _ttl_sec() -> int:
    try:
        return max(60, int(os.getenv("CC_LARK_CARD_ACTION_TTL_SEC", _DEFAULT_TTL_SEC)))
    except (TypeError, ValueError):
        return _DEFAULT_TTL_SEC


def sign_action_value(
    value: dict[str, Any],
    secret: str,
    *,
    user_id: str,
    message_id: str,
    now: int | None = None,
) -> dict[str, Any]:
    """Return a signed copy of one callback value.

    Missing identity/context is rejected instead of producing a weaker signature;
    this makes future button call sites fail visibly until they supply both fields.
    """
    if not isinstance(value, dict):
        raise ValueError("card action value must be an object")
    if not secret:
        raise ValueError("card action signing secret is missing")
    if not user_id or not message_id:
        raise ValueError("card action requires user_id and message_id binding")

    signed = copy.deepcopy(value)
    signed.pop(_SIGNATURE_FIELD, None)
    signed[_VERSION_FIELD] = _VERSION
    signed[_USER_FIELD] = user_id
    signed[_MESSAGE_FIELD] = message_id
    signed[_TIMESTAMP_FIELD] = int(time.time() if now is None else now)
    signed[_SIGNATURE_FIELD] = hmac.new(
        _signing_key(secret), _canonical(signed), hashlib.sha256,
    ).hexdigest()
    return signed


def verify_action_value(
    value: dict[str, Any],
    secret: str,
    *,
    user_id: str,
    message_id: str,
    now: int | None = None,
) -> tuple[bool, str]:
    """Verify signature, expiry, intended operator, and originating message."""
    if not isinstance(value, dict) or not secret:
        return False, "missing signed action"
    signature = value.get(_SIGNATURE_FIELD)
    if not isinstance(signature, str) or not signature:
        return False, "unsigned action"
    if value.get(_VERSION_FIELD) != _VERSION:
        return False, "unsupported action version"
    if not user_id or value.get(_USER_FIELD) != user_id:
        return False, "operator mismatch"
    if not message_id or value.get(_MESSAGE_FIELD) != message_id:
        return False, "message mismatch"
    timestamp = value.get(_TIMESTAMP_FIELD)
    if not isinstance(timestamp, int):
        return False, "invalid action timestamp"
    current = int(time.time() if now is None else now)
    age = current - timestamp
    if age < -_CLOCK_SKEW_SEC or age > _ttl_sec():
        return False, "expired action"

    expected = hmac.new(
        _signing_key(secret), _canonical(value), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False, "invalid action signature"
    return True, ""


def card_action_allowed(profile, user_id: str, chat_id: str) -> bool:
    """Apply the profile's normal user/group allowlists to a button click."""
    if not user_id or not chat_id:
        return False
    allowed_users = set(getattr(profile, "allowed_open_ids", set()) or set())
    if allowed_users and user_id not in allowed_users:
        return False

    raw_chat_id = chat_id.split(":", 1)[0]
    if raw_chat_id.startswith("oc_"):
        allowed_groups = set(getattr(profile, "allowed_group_chat_ids", set()) or set())
        if "*" not in allowed_groups and raw_chat_id not in allowed_groups:
            return False
    return True


def card_context_matches(user_id: str, chat_id: str, open_chat_id: str) -> bool:
    """Bind group actions to Lark's callback chat; DMs remain bound to the user."""
    raw_chat_id = (chat_id or "").split(":", 1)[0]
    if raw_chat_id.startswith("oc_"):
        return bool(open_chat_id) and hmac.compare_digest(raw_chat_id, open_chat_id)
    return bool(user_id) and hmac.compare_digest(chat_id or "", user_id)


def claim_event(profile_name: str, event_id: str, *, now: float | None = None) -> bool:
    """Atomically claim one callback event id; False means invalid or replayed."""
    if not profile_name or not event_id:
        return False
    current = time.time() if now is None else now
    key = f"{profile_name}:{event_id}"
    with _seen_events_lock:
        stale_before = current - _EVENT_TTL_SEC
        for seen_key, seen_at in list(_seen_events.items()):
            if seen_at < stale_before:
                _seen_events.pop(seen_key, None)
        if key in _seen_events:
            return False
        _seen_events[key] = current
        return True
