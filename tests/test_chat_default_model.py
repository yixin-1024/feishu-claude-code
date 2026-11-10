"""群级默认模型：<PREFIX>_CHAT_MODEL_<chat_id> 让个别群不跟 profile 的 DEFAULT_MODEL 走。"""

import pytest

from bot_config import _load_profile
from session_store import SessionStore

OPUS_GROUP = "oc_keep_opus"
OTHER_GROUP = "oc_other"
USER = "ou_user"


def _store(mapping, *, runner: str = "claude", default_model: str = "fable[1m]") -> SessionStore:
    return SessionStore(
        profile=f"chat-model-{runner}",
        default_cwd="/tmp",
        default_runner=runner,
        default_model=default_model,
        chat_default_model=mapping,
    )


def test_profile_scans_chat_model_env(monkeypatch):
    monkeypatch.setenv("CM1_APP_ID", "a")
    monkeypatch.setenv("CM1_APP_SECRET", "s")
    monkeypatch.setenv("CM1_DEFAULT_MODEL", "fable[1m]")
    monkeypatch.setenv(f"CM1_CHAT_MODEL_{OPUS_GROUP}", " opus[1m] ")
    monkeypatch.setenv("CM1_CHAT_MODEL_oc_blank", "  ")
    p = _load_profile("cm1")
    assert p.default_model == "fable[1m]"
    assert p.chat_default_model == {OPUS_GROUP: "opus[1m]"}


@pytest.mark.asyncio
async def test_group_and_its_topics_follow_the_group_default():
    store = _store({OPUS_GROUP: "opus[1m]"})
    assert (await store.get_current(USER, OPUS_GROUP)).model == "opus[1m]"
    # 话题群：chat_id 是 "oc_xxx:omt_yyy"，配置里只写裸 chat_id
    assert (await store.get_current(USER, f"{OPUS_GROUP}:omt_topic")).model == "opus[1m]"
    # 别的群、私聊照旧跟 profile 默认
    assert (await store.get_current(USER, OTHER_GROUP)).model == "fable[1m]"
    assert (await store.get_current(USER, f"{OTHER_GROUP}:omt_topic")).model == "fable[1m]"
    assert (await store.get_current(USER, USER)).model == "fable[1m]"


@pytest.mark.asyncio
async def test_group_default_is_not_persisted_and_follows_config_changes():
    mapping = {OPUS_GROUP: "opus[1m]"}
    store = _store(mapping)
    chat = f"{OPUS_GROUP}:omt_old"
    assert (await store.get_current(USER, chat)).model == "opus[1m]"
    assert (await store.get_current_raw(USER, chat))["model_override"] is None
    # 配置拿掉后，同一个老话题回到 profile 默认
    mapping.clear()
    assert (await store.get_current(USER, chat)).model == "fable[1m]"


@pytest.mark.asyncio
async def test_explicit_model_override_beats_group_default():
    store = _store({OPUS_GROUP: "opus[1m]"})
    chat = f"{OPUS_GROUP}:omt_pinned"
    await store.set_model_override(USER, chat, "haiku")
    assert (await store.get_current(USER, chat)).model == "haiku"
    await store.set_model_override(USER, chat, "")
    assert (await store.get_current(USER, chat)).model == "opus[1m]"


@pytest.mark.asyncio
async def test_alias_is_resolved_like_slash_model():
    store = _store({OPUS_GROUP: "opus"})
    assert (await store.get_current(USER, OPUS_GROUP)).model == "opus[1m]"


@pytest.mark.asyncio
async def test_group_default_is_ignored_when_it_does_not_fit_the_runner():
    """话题跑在 codex 上时，不能把群里配的 opus 塞给它。"""
    store = _store({OPUS_GROUP: "opus[1m]"}, runner="codex", default_model="gpt-5.5")
    assert (await store.get_current(USER, OPUS_GROUP)).model == "gpt-5.5"
    assert store.default_model_for(OPUS_GROUP, "claude") == "opus[1m]"


@pytest.mark.asyncio
async def test_session_model_helper_resolves_override_group_then_profile():
    from types import SimpleNamespace

    import commands

    store = _store({OPUS_GROUP: "opus[1m]"})
    assert commands._session_model(store, {}, f"{OPUS_GROUP}:omt_a", "claude") == "opus[1m]"
    assert commands._session_model(store, {"model_override": "haiku"}, OPUS_GROUP, "claude") == "haiku"
    assert commands._session_model(store, {}, OTHER_GROUP, "claude") == "fable[1m]"
    # 没有 default_model_for 的旧 store 替身照常工作
    legacy = SimpleNamespace(default_model="fable[1m]")
    assert commands._session_model(legacy, {}, OPUS_GROUP, "claude") == "fable[1m]"
