"""dispatch_task 的 model / effort 透传单测。

子会话是全新 thread + 全新 session，不继承派发方 thread 的 /model /effort —— 要让
一路跑 Opus、另一路跑 Fable 交叉验证，只能靠 dispatch_task(model=..., effort=...)。
这里验证：别名解析、非法 effort 早拒（不留空话题）、一路透传到 handle_spawn、结果回显。
"""

import asyncio

import pytest

import dispatcher
from bot_config import normalize_effort, normalize_model


class _FakeFeishu:
    def __init__(self):
        self.posts = []

    async def send_post_to_chat(self, **kwargs):
        self.posts.append(kwargs)
        return "om_anchor"

    async def get_message_thread_id(self, _msg_id):
        return "omt_child"


class _FakeStore:
    def find_primary_user(self):
        return "ou_user"


class _FakeProfile:
    def __init__(self, dispatch_model=""):
        self.name = "spx"
        self.runner = "claude"
        self.dispatch_model = dispatch_model


class _FakeBot:
    def __init__(self, dispatch_model=""):
        self.profile = _FakeProfile(dispatch_model)
        self.feishu = _FakeFeishu()
        self.store = _FakeStore()


@pytest.fixture
def spawns(monkeypatch):
    """替掉 handle_spawn，记录每次派发拿到的 kwargs。"""
    calls = []

    async def _fake_spawn(_bot, **kwargs):
        calls.append(kwargs)
        return (True, "done")

    monkeypatch.setattr(dispatcher, "handle_spawn", _fake_spawn)
    dispatcher._DISPATCH_CHILDREN.clear()
    yield calls
    dispatcher._DISPATCH_CHILDREN.clear()


async def _drain():
    """等 fire-and-forget 的子会话 task 跑完，避免 pending task 泄漏到别的用例。"""
    for _ in range(5):
        await asyncio.sleep(0)


async def _dispatch(bot, **kwargs):
    res = await dispatcher.dispatch_task(
        bot, user_id="ou_user", group_chat_id="oc_group",
        title="t", prompt="do something", **kwargs,
    )
    await _drain()
    return res


# ── 归一化本身 ────────────────────────────────────────────────

def test_normalize_model_resolves_alias():
    assert normalize_model("fable") == "fable[1m]"
    assert normalize_model("OPUS") == "opus[1m]"


def test_normalize_model_passes_through_full_id():
    assert normalize_model("claude-opus-5[1m]") == "claude-opus-5[1m]"
    assert normalize_model("") == ""


def test_normalize_effort_rejects_unknown_level():
    with pytest.raises(ValueError):
        normalize_effort("turbo", "dispatch_task")


def test_normalize_effort_default_means_no_override():
    assert normalize_effort("default", "dispatch_task") == ""
    assert normalize_effort("HIGH", "dispatch_task") == "high"


# ── dispatch_task 透传 ────────────────────────────────────────

async def test_model_alias_forwarded_to_spawn(spawns):
    bot = _FakeBot()
    res = await _dispatch(bot, model="fable", effort="high")
    assert res["ok"] is True
    assert spawns[0]["model"] == "fable[1m]"
    assert spawns[0]["effort"] == "high"


async def test_result_echoes_model_and_effort(spawns):
    bot = _FakeBot()
    res = await _dispatch(bot, model="haiku")
    assert res["model"] == "haiku"
    assert res["effort"] == ""


async def test_omitted_model_effort_keeps_profile_default(spawns):
    """不传 = 跟随目标 bot 的 profile 默认（handle_spawn 收到空串才不会 set_model）。"""
    bot = _FakeBot()
    await _dispatch(bot)
    assert spawns[0]["model"] == ""
    assert spawns[0]["effort"] == ""


async def test_profile_dispatch_model_used_when_omitted(spawns):
    """DISPATCH_MODEL=opus + DEFAULT_MODEL=fable：不传 model 的派活也该落到 opus。"""
    bot = _FakeBot(dispatch_model="opus")
    res = await _dispatch(bot)
    assert spawns[0]["model"] == "opus[1m]"
    assert res["model"] == "opus[1m]"


async def test_explicit_model_beats_profile_dispatch_model(spawns):
    bot = _FakeBot(dispatch_model="opus")
    await _dispatch(bot, model="haiku")
    assert spawns[0]["model"] == "haiku"


async def test_cross_agent_uses_target_profile_dispatch_model(spawns):
    """跨 agent：model 属于跑它的后端，取 target_bot 的 DISPATCH_MODEL，不是派发方的。"""
    parent = _FakeBot(dispatch_model="opus")
    child = _FakeBot(dispatch_model="codex-max")
    child.profile.name = "regtank"
    child.profile.runner = "codex"
    await _dispatch(parent, target_bot=child)
    assert spawns[0]["model"] == "gpt-5.1-codex-max"


async def test_bad_effort_rejected_before_creating_topic(spawns):
    """非法 effort 必须在建话题前拒掉，否则群里会留一条没人跑的空话题。"""
    bot = _FakeBot()
    res = await _dispatch(bot, effort="turbo")
    assert res["ok"] is False
    assert "effort" in res["error"]
    assert bot.feishu.posts == []
    assert spawns == []
