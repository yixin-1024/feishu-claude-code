"""Conversation-level reasoning effort persistence semantics."""

import json

import pytest

from session_store import SessionStore


def _store(profile: str, *, runner: str = "codex", shared: bool = False) -> SessionStore:
    return SessionStore(
        profile=profile,
        default_cwd="/tmp",
        default_runner=runner,
        default_model="gpt-test" if runner == "codex" else "claude-test",
        shared_thread_sessions=shared,
    )


def _cache_summary(store: SessionStore, user_id: str, session_id: str) -> None:
    """Avoid spawning background summary tasks in session-transition tests."""
    store._data.setdefault(user_id, {}).setdefault("summaries", {})[session_id] = "title"


@pytest.mark.asyncio
async def test_effort_override_persists_and_clear_does_not_reset_session():
    user_id = "ou_effort"
    chat_id = "oc_persist"
    store = _store("effort-persist")
    current = await store.get_current_raw(user_id, chat_id)
    current.update({
        "session_id": "sid-keep",
        "preview": "keep preview",
        "started_at": "2026-07-22T20:00:00",
        "last_usage": {"input_tokens": 42},
    })
    await store._save_async()

    await store.set_effort(user_id, chat_id, " Ultra ")

    current = await store.get_current_raw(user_id, chat_id)
    assert (await store.get_current(user_id, chat_id)).effort == "ultra"
    assert current["effort_override"] == "ultra"
    assert current["session_id"] == "sid-keep"
    assert current["preview"] == "keep preview"
    assert current["started_at"] == "2026-07-22T20:00:00"
    assert current["last_usage"] == {"input_tokens": 42}
    assert (await store.list_sessions(user_id, chat_id)) == []

    reloaded = _store("effort-persist")
    session = await reloaded.get_current(user_id, chat_id)
    assert session.effort == "ultra"
    assert session.session_id == "sid-keep"

    await reloaded.set_effort(user_id, chat_id, "")
    session = await reloaded.get_current(user_id, chat_id)
    assert session.effort is None
    assert session.session_id == "sid-keep"
    assert (await reloaded.list_sessions(user_id, chat_id)) == []


@pytest.mark.asyncio
async def test_legacy_current_lazily_migrates_effort_without_losing_session():
    user_id = "ou_legacy"
    chat_id = "oc_legacy"
    store = _store("effort-migration")
    current = await store.get_current_raw(user_id, chat_id)
    current.pop("effort_override")
    current["session_id"] = "sid-legacy"
    await store._save_async()

    reloaded = _store("effort-migration")
    session = await reloaded.get_current(user_id, chat_id)

    assert session.effort is None
    assert session.session_id == "sid-legacy"
    assert (await reloaded.get_current_raw(user_id, chat_id))["effort_override"] is None
    with open(reloaded._sessions_file, encoding="utf-8") as f:
        persisted = json.load(f)
    assert persisted[user_id][chat_id]["current"]["effort_override"] is None


@pytest.mark.asyncio
async def test_effort_is_isolated_by_conversation_and_shared_within_topic():
    store = _store("effort-isolation", shared=True)
    thread_a = "oc_group:omt_a"
    thread_b = "oc_group:omt_b"

    await store.set_effort("ou_alice", thread_a, "high")

    assert (await store.get_current("ou_bob", thread_a)).effort == "high"
    assert (await store.get_current("ou_alice", thread_b)).effort is None

    await store.set_effort("ou_bob", thread_b, "low")
    assert (await store.get_current("ou_alice", thread_a)).effort == "high"
    assert (await store.get_current("ou_alice", thread_b)).effort == "low"

    # Plain groups remain user-scoped even when shared-thread mode is enabled.
    await store.set_effort("ou_alice", "oc_plain", "medium")
    assert (await store.get_current("ou_alice", "oc_plain")).effort == "medium"
    assert (await store.get_current("ou_bob", "oc_plain")).effort is None


@pytest.mark.asyncio
async def test_new_session_preserves_effort_but_history_does_not_own_it():
    user_id = "ou_new"
    chat_id = "oc_new"
    store = _store("effort-new")
    await store.set_effort(user_id, chat_id, "xhigh")
    current = await store.get_current_raw(user_id, chat_id)
    current["session_id"] = "sid-old"
    current["preview"] = "old preview"
    _cache_summary(store, user_id, "sid-old")
    await store._save_async()

    await store.new_session(user_id, chat_id)

    session = await store.get_current(user_id, chat_id)
    history = await store.list_sessions(user_id, chat_id)
    assert session.session_id is None
    assert session.effort == "xhigh"
    assert history[0]["session_id"] == "sid-old"
    assert "effort_override" not in history[0]


@pytest.mark.asyncio
async def test_reset_to_defaults_clears_effort_override():
    user_id = "ou_reset"
    chat_id = "oc_reset"
    store = _store("effort-reset")
    await store.set_effort(user_id, chat_id, "max")
    current = await store.get_current_raw(user_id, chat_id)
    current["session_id"] = "sid-reset"
    _cache_summary(store, user_id, "sid-reset")
    await store._save_async()

    await store.reset_current_to_defaults(user_id, chat_id)

    session = await store.get_current(user_id, chat_id)
    history = await store.list_sessions(user_id, chat_id)
    assert session.session_id is None
    assert session.effort is None
    assert history[0]["session_id"] == "sid-reset"
    assert "effort_override" not in history[0]


@pytest.mark.asyncio
async def test_resume_uses_current_conversation_effort_not_history_metadata():
    user_id = "ou_resume"
    chat_id = "oc_resume"
    store = _store("effort-resume")
    await store.set_effort(user_id, chat_id, "low")
    current = await store.get_current_raw(user_id, chat_id)
    current["session_id"] = "sid-old"
    current["preview"] = "old preview"
    _cache_summary(store, user_id, "sid-old")
    await store._save_async()
    await store.new_session(user_id, chat_id)

    await store.set_effort(user_id, chat_id, "ultra")
    current = await store.get_current_raw(user_id, chat_id)
    current["session_id"] = "sid-current"
    current["preview"] = "current preview"
    _cache_summary(store, user_id, "sid-current")
    await store._save_async()

    resumed_id, _ = await store.resume_session(user_id, chat_id, "sid-old")

    session = await store.get_current(user_id, chat_id)
    assert resumed_id == "sid-old"
    assert session.session_id == "sid-old"
    assert session.effort == "ultra"
    assert all("effort_override" not in item for item in await store.list_sessions(user_id, chat_id))


@pytest.mark.asyncio
async def test_explicit_runner_switch_clears_incompatible_effort():
    user_id = "ou_runner"
    chat_id = "oc_runner"
    store = _store("effort-runner-explicit", runner="codex")
    await store.set_effort(user_id, chat_id, "ultra")
    current = await store.get_current_raw(user_id, chat_id)
    current["session_id"] = "sid-codex"
    _cache_summary(store, user_id, "sid-codex")
    await store._save_async()

    await store.set_runner(user_id, chat_id, "claude", model="claude-test")

    # Inspect the immediate persisted switch. A later get_current intentionally
    # reconciles a per-chat runner back to this store's profile default.
    current = store._data[user_id][chat_id]["current"]
    assert current["runner"] == "claude"
    assert current["effort_override"] is None
    assert current["session_id"] is None


@pytest.mark.asyncio
async def test_profile_runner_change_clears_persisted_effort_and_session():
    user_id = "ou_profile_runner"
    chat_id = "oc_profile_runner"
    old_store = _store("effort-runner-profile", runner="codex")
    await old_store.set_effort(user_id, chat_id, "ultra")
    current = await old_store.get_current_raw(user_id, chat_id)
    current["session_id"] = "sid-codex"
    await old_store._save_async()

    new_store = _store("effort-runner-profile", runner="claude")
    session = await new_store.get_current(user_id, chat_id)

    assert session.runner == "claude"
    assert session.effort is None
    assert session.session_id is None
