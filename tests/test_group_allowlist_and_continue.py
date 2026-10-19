"""群白名单共享写入口（/group add 与外部 API 共用）+ dispatcher.continue_task 的单测。"""

import asyncio
import os
import stat

import pytest

import commands
import dispatcher
import group_allowlist
import task_routes


class _Profile:
    def __init__(self, name="main", groups=()):
        self.name = name
        self.runner = "claude"
        self.allowed_group_chat_ids = set(groups)
        self.chat_default_cwd = {}


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("MAIN_APP_ID=x\n# comment\nMAIN_ALLOWED_GROUP_CHAT_IDS=oc_a\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


# ── group_allowlist ───────────────────────────────────────────

def test_persist_appends_allowlist_and_cwd_atomically(env_file, tmp_path):
    changed, info = group_allowlist.persist_chat_to_env("main", "oc_b", "/w/x", str(env_file))
    assert changed and info == str(env_file)
    text = env_file.read_text(encoding="utf-8")
    assert "MAIN_ALLOWED_GROUP_CHAT_IDS=oc_a,oc_b\n" in text
    assert text.endswith("MAIN_CHAT_CWD_oc_b=/w/x\n")
    assert "# comment" in text
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
    assert not [f for f in os.listdir(tmp_path) if f.startswith(".env.tmp.")]
    # 原权限照旧保留（不是 mkstemp 默认的 0600）
    os.chmod(env_file, 0o640)
    assert group_allowlist.persist_chat_to_env("main", "oc_c", None, str(env_file))[0] is True
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o640
    os.chmod(env_file, 0o600)
    # 幂等；cwd 改了就覆盖那一行
    assert group_allowlist.persist_chat_to_env("main", "oc_b", "/w/x", str(env_file))[0] is False
    assert group_allowlist.persist_chat_to_env("main", "oc_b", "/w/y", str(env_file))[0] is True
    text = env_file.read_text(encoding="utf-8")
    assert "MAIN_CHAT_CWD_oc_b=/w/y" in text and "/w/x" not in text


def test_persist_rejects_injection(env_file):
    before = env_file.read_text(encoding="utf-8")
    for bad in ("oc_x\nMAIN_DEFAULT_CWD=/", "oc_x=y", "oc_", "*"):
        changed, info = group_allowlist.persist_chat_to_env("main", bad, "/w", str(env_file))
        assert not changed and info.startswith("❌"), bad
    changed, info = group_allowlist.persist_chat_to_env("main", "oc_ok", "/w\nEVIL=1", str(env_file))
    assert not changed and info.startswith("❌")
    assert env_file.read_text(encoding="utf-8") == before


def test_add_chat_updates_memory_even_without_env(tmp_path):
    profile = _Profile(groups=("oc_a",))
    changed, info = group_allowlist.add_chat_to_profile(profile, "oc_b", "/w", str(tmp_path / "none"))
    assert not changed and info.startswith("❌ .env 不存在")
    assert "oc_b" in profile.allowed_group_chat_ids and profile.chat_default_cwd["oc_b"] == "/w"


def test_group_add_command_still_works(env_file, tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "REPO_ROOT", str(tmp_path))
    workdir = tmp_path / "ws"
    workdir.mkdir()
    bot = type("Bot", (), {"profile": _Profile()})()
    out = asyncio.run(commands._handle_group_command(f"add oc_new {workdir}", bot))
    assert out.startswith("✅") and ".env 已更新" in out
    assert "oc_new" in bot.profile.allowed_group_chat_ids
    assert bot.profile.chat_default_cwd["oc_new"] == str(workdir)
    text = env_file.read_text(encoding="utf-8")
    assert "MAIN_ALLOWED_GROUP_CHAT_IDS=oc_a,oc_new" in text
    assert f"MAIN_CHAT_CWD_oc_new={workdir}" in text
    out = asyncio.run(commands._handle_group_command("add oc_bad=1", bot))
    assert out.startswith("⚠️")
    assert asyncio.run(commands._handle_group_command("add nope", bot)).startswith("⚠️")


def test_live_group_cwd_reaches_session_store_even_when_mapping_started_empty():
    """profile 起步没有任何 CHAT_CWD 映射时，实时加的群 workspace 也要被新话题用上。"""
    from session_store import SessionStore
    mapping = {}
    store = SessionStore(profile="t-live-cwd", default_cwd="/home/default", chat_default_cwd=mapping)
    profile = _Profile()
    profile.chat_default_cwd = mapping
    group_allowlist.add_chat_to_profile(profile, "oc_live", "/w/partner", "/nonexistent/.env")
    assert store._default_current("oc_live:omt_1")["cwd"] == "/w/partner"
    assert store._default_current("oc_other:omt_1")["cwd"] == "/home/default"


# ── dispatcher.continue_task ──────────────────────────────────

class _Feishu:
    def __init__(self):
        self.replies = []

    async def reply_post(self, message_id, title, body_text):
        self.replies.append((message_id, title, body_text))
        return "om_reply"

    async def get_message_thread_id(self, _mid):
        return "omt_real"


class _Store:
    def __init__(self):
        self.cwd = []
        self.model = []

    async def set_cwd(self, user, chat, cwd, ws=None):
        self.cwd.append((user, chat, cwd, ws))

    async def set_model(self, user, chat, model):
        self.model.append((user, chat, model))

    async def set_effort(self, user, chat, effort):
        pass


class _CBot:
    def __init__(self, name="main"):
        self.profile = _Profile(name)
        self.feishu = _Feishu()
        self.store = _Store()
        self._locks = {}

    def _ensure_chat_lock(self, key):
        return self._locks.setdefault(key, asyncio.Lock())


@pytest.fixture
def followups(monkeypatch):
    calls = []

    async def _fake(bot, **kwargs):
        calls.append((bot, kwargs))

    monkeypatch.setattr(dispatcher, "_deliver_followup", _fake)
    monkeypatch.setattr(dispatcher, "_bots", {})
    return calls


async def _run(coro):
    res = await coro
    for _ in range(3):
        await asyncio.sleep(0)
    return res


def test_continue_task_posts_into_topic_and_resumes_owner_session(followups):
    bot = _CBot()
    dispatcher._bots["main"] = bot
    task_routes.bind(chat_id="oc_p", thread_id="omt_1", profile="main",
                     user_id="ou_owner", anchor="om_root")
    res = asyncio.run(_run(dispatcher.continue_task(
        bot, group_chat_id="oc_p", thread_id="omt_1", title="合作方进件",
        prompt="round 2 please", cwd="/w/partner", workspace="partner-onboarding",
        body_header="hdr")))
    assert res["ok"] and res["thread_id"] == "omt_1" and res["anchor_message_id"] == "om_reply"
    # 完整提示词回帖进原话题根消息下
    mid, title, body = bot.feishu.replies[-1]
    assert mid == "om_root" and "合作方进件" in title and "round 2 please" in body and "hdr" in body
    # cwd 按 route 钉死，会话归属人取派单时记的 owner（不是调用方）
    assert bot.store.cwd == [("ou_owner", "oc_p:omt_1", "/w/partner", "partner-onboarding")]
    fbot, kwargs = followups[-1]
    assert fbot is bot and kwargs["user_id"] == "ou_owner"
    assert kwargs["chat_id"] == "oc_p:omt_1" and kwargs["anchor_message_id"] == "om_reply"
    assert kwargs["instruction"] == "round 2 please" and kwargs["stop_first"] is False


def test_continue_task_refuses_unowned_thread(followups):
    bot = _CBot()
    res = asyncio.run(_run(dispatcher.continue_task(
        bot, group_chat_id="oc_p", thread_id="omt_none", title="t", prompt="x")))
    assert res["ok"] is False and "归属" in res["error"]
    assert bot.feishu.replies == [] and followups == []


def test_continue_task_resolves_om_anchor_record(followups):
    bot = _CBot()
    dispatcher._bots["main"] = bot
    # 派单时 thread_id 没解析出来，记录里存的是 om_ 根消息
    task_routes.bind(chat_id="oc_p", thread_id="om_root", profile="main",
                     user_id="ou_owner", anchor="om_root")
    res = asyncio.run(_run(dispatcher.continue_task(
        bot, group_chat_id="oc_p", thread_id="om_root", title="t", prompt="x")))
    assert res["ok"] and res["thread_id"] == "omt_real"
    assert followups[-1][1]["chat_id"] == "oc_p:omt_real"
