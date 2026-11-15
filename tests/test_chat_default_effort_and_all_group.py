"""本群默认模型 / 推理强度（用户 2026-11-15 要的）。

- 私聊里 /all 卡上那两排改的是全局（见 test_all_command.py）；
- 群里同一组按钮只改本群（含所有话题），落 <PREFIX>_CHAT_MODEL_<chat_id> /
  <PREFIX>_CHAT_EFFORT_<chat_id>，「跟随全局」删掉那行；
- 优先级：话题里 /model、/effort 覆盖 > 本群 > 全局。
"""

import asyncio
import os

import pytest

import commands
import group_allowlist
import server_stats
from bot_config import _load_profile
from session_store import SessionStore

GROUP = "oc_cclark"
TOPIC = f"{GROUP}:omt_topic"
OTHER = "oc_other"
ADMIN = "ou_admin"


class _Profile:
    def __init__(self, name="main", runner="claude", model="opus[1m]"):
        self.name = name
        self.runner = runner
        self.default_model = model
        self.admin_open_ids = {ADMIN}
        self.chat_default_model: dict = {}
        self.chat_default_effort: dict = {}


class _Bot:
    """profile 和 store 共用同一份群级 dict —— 和 bot_instance 里一样。"""

    def __init__(self, name="main"):
        self.profile = _Profile(name)
        self.store = SessionStore(
            profile=f"t-{name}", default_cwd="/tmp", default_runner="claude",
            default_model=self.profile.default_model,
            chat_default_model=self.profile.chat_default_model,
            chat_default_effort=self.profile.chat_default_effort,
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("MAIN_DEFAULT_MODEL=opus[1m]\nDEFAULT_MODEL=opus[1m]\nCLAUDE_EFFORT=xhigh\n")
    monkeypatch.setenv("CC_LARK_ENV_FILE", str(path))
    monkeypatch.setenv("PROFILES", "main")
    monkeypatch.setenv("CLAUDE_EFFORT", "xhigh")
    for key in (f"MAIN_CHAT_MODEL_{GROUP}", f"MAIN_CHAT_EFFORT_{GROUP}"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(commands, "_get_usage", lambda chat_id, compact=False: {
        "text": "📊 **Claude 用量**\n\n剩余 **43%**", "buttons": []})
    monkeypatch.setattr(server_stats, "get_report", lambda compact=False: "server")
    return path


def _all(bot, args, chat=TOPIC, user=ADMIN):
    return asyncio.run(commands.handle_command("all", args, user, chat, bot.store, bot=bot))


def _rows(out):
    model = [b for b in out["buttons"] if b["value"]["cmd"].startswith(commands.ALL_MODEL_PREFIX)]
    effort = [b for b in out["buttons"] if b["value"]["cmd"].startswith(commands.ALL_EFFORT_PREFIX)]
    return model, effort


# ── 卡片 ─────────────────────────────────────────────────────

def test_group_card_shows_group_rows_following_global(env):
    bot = _Bot()
    model, effort = _rows(_all(bot, ""))
    assert [b["text"] for b in model] == ["● 跟随全局", "Fable", "Opus", "Sonnet", "Haiku"]
    assert model[0]["group"] == "**本群默认模型**（全局 Opus）"
    assert [b["text"] for b in effort] == ["● 跟随全局", "Low", "Medium", "High", "XHigh", "Max"]
    assert effort[0]["group"] == "**本群默认推理强度**（全局 XHigh）"
    assert model[0]["value"]["cmd"] == "/all model global"
    # 群里不再出全局那两排
    assert all("全局默认" not in b["group"] for b in model + effort)


def test_dm_card_still_shows_global_rows(env):
    bot = _Bot()
    model, effort = _rows(_all(bot, "", chat=ADMIN))
    assert {b["group"] for b in model} == {commands.GLOBAL_MODEL_LABEL}
    assert {b["group"] for b in effort} == {commands.GLOBAL_EFFORT_LABEL}
    assert "跟随全局" not in [b["text"] for b in model + effort]


def test_group_rows_hidden_for_non_admin(env):
    bot = _Bot()
    model, effort = _rows(_all(bot, "", user="ou_someone"))
    assert model == [] and effort == []


# ── 改本群 ───────────────────────────────────────────────────

def test_group_model_only_changes_this_group(env):
    bot = _Bot()
    out = _all(bot, "model sonnet")
    assert out["text"].split("\n")[2] == "剩余 **43%**"  # 成功不写
    assert bot.profile.chat_default_model == {GROUP: "sonnet[1m]"}  # 话题后缀已剥掉
    assert f"MAIN_CHAT_MODEL_{GROUP}=sonnet[1m]" in env.read_text().splitlines()
    assert os.environ[f"MAIN_CHAT_MODEL_{GROUP}"] == "sonnet[1m]"
    # 全局没动
    assert bot.profile.default_model == "opus[1m]" and bot.store.default_model == "opus[1m]"
    assert "MAIN_DEFAULT_MODEL=opus[1m]" in env.read_text().splitlines()
    # 本群新老话题都跟本群，别的群、私聊跟全局
    get = lambda chat: asyncio.run(bot.store.get_current(ADMIN, chat)).model
    assert get(TOPIC) == "sonnet[1m]" and get(f"{GROUP}:omt_new") == "sonnet[1m]"
    assert get(OTHER) == "opus[1m]" and get(ADMIN) == "opus[1m]"
    model, _ = _rows(out)
    assert [b["text"] for b in model] == ["跟随全局", "Fable", "Opus", "● Sonnet", "Haiku"]


def test_group_effort_only_changes_this_group(env):
    bot = _Bot()
    out = _all(bot, "effort max")
    assert bot.profile.chat_default_effort == {GROUP: "max"}
    assert f"MAIN_CHAT_EFFORT_{GROUP}=max" in env.read_text().splitlines()
    assert os.environ["CLAUDE_EFFORT"] == "xhigh"  # 全局没动
    get = lambda chat: asyncio.run(bot.store.get_current(ADMIN, chat)).effort
    assert get(TOPIC) == "max" and get(f"{GROUP}:omt_new") == "max"
    assert get(OTHER) is None and get(ADMIN) is None  # None = runner 落到全局 CLAUDE_EFFORT
    _, effort = _rows(out)
    assert [b["text"] for b in effort] == ["跟随全局", "Low", "Medium", "High", "XHigh", "● Max"]


def test_topic_override_beats_group_default(env):
    bot = _Bot()
    _all(bot, "model haiku")
    _all(bot, "effort low")
    asyncio.run(bot.store.set_model_override(ADMIN, TOPIC, "opus[1m]"))
    asyncio.run(bot.store.set_effort(ADMIN, TOPIC, "high"))
    cur = asyncio.run(bot.store.get_current(ADMIN, TOPIC))
    assert cur.model == "opus[1m]" and cur.effort == "high"
    # 清掉话题覆盖，回到本群默认
    asyncio.run(bot.store.set_effort(ADMIN, TOPIC, ""))
    assert asyncio.run(bot.store.get_current(ADMIN, TOPIC)).effort == "low"


def test_follow_global_removes_group_setting(env):
    bot = _Bot()
    _all(bot, "model fable")
    _all(bot, "effort high")
    out = _all(bot, "model global")
    _all(bot, "effort reset")
    assert bot.profile.chat_default_model == {} and bot.profile.chat_default_effort == {}
    text = env.read_text()
    assert f"MAIN_CHAT_MODEL_{GROUP}" not in text and f"MAIN_CHAT_EFFORT_{GROUP}" not in text
    assert f"MAIN_CHAT_MODEL_{GROUP}" not in os.environ
    cur = asyncio.run(bot.store.get_current(ADMIN, TOPIC))
    assert cur.model == "opus[1m]" and cur.effort is None
    assert [b["text"] for b in _rows(out)[0]][0] == "● 跟随全局"


def test_group_change_refused_for_non_admin(env):
    bot = _Bot()
    before = env.read_text()
    out = _all(bot, "model haiku", user="ou_someone")
    assert out["text"].split("\n")[2] == "⛔ 只有管理员能改本群默认模型"
    out = _all(bot, "effort low", user="ou_someone")
    assert out["text"].split("\n")[2] == "⛔ 只有管理员能改本群默认推理强度"
    assert bot.profile.chat_default_model == {} and env.read_text() == before


def test_group_unknown_value(env):
    bot = _Bot()
    out = _all(bot, "effort ultra")
    assert out["text"].split("\n")[2].startswith("❌ 不认识的强度 `ultra`")
    assert bot.profile.chat_default_effort == {}


def test_global_change_does_not_touch_group_setting(env, monkeypatch):
    bot = _Bot()
    monkeypatch.setattr(commands, "_registered_bots", lambda b: [bot])
    _all(bot, "model sonnet")                 # 本群
    _all(bot, "model fable", chat=ADMIN)      # 私聊 → 全局
    assert bot.store.default_model == "fable[1m]"
    get = lambda chat: asyncio.run(bot.store.get_current(ADMIN, chat)).model
    assert get(TOPIC) == "sonnet[1m]" and get(OTHER) == "fable[1m]"
    model, _ = _rows(_all(bot, ""))
    assert model[0]["group"] == "**本群默认模型**（全局 Fable）"


def test_toasts_for_follow_global():
    assert commands.run_cmd_toast("/all model global") == "正在把默认模型改成跟随全局…"
    assert commands.run_cmd_toast("/all effort global") == "正在把默认推理强度改成跟随全局…"
    assert commands.run_cmd_toast("/all effort max") == "正在把默认推理强度改成 Max…"


# ── 配置 / 存储 ──────────────────────────────────────────────

def test_profile_scans_chat_effort_env(monkeypatch):
    monkeypatch.setenv("CE1_APP_ID", "a")
    monkeypatch.setenv("CE1_APP_SECRET", "s")
    monkeypatch.setenv(f"CE1_CHAT_EFFORT_{GROUP}", " High ")
    monkeypatch.setenv("CE1_CHAT_EFFORT_oc_blank", "  ")
    assert _load_profile("ce1").chat_default_effort == {GROUP: "high"}


def test_group_effort_only_for_claude_runner():
    store = SessionStore(profile="t-codex", default_cwd="/tmp", default_runner="codex",
                         default_model="gpt-5.5", chat_default_effort={GROUP: "max"})
    assert store.default_effort_for(GROUP, "codex") is None
    assert store.default_effort_for(GROUP, "claude") == "max"
    assert store.default_effort_for(OTHER, "claude") is None
    bad = SessionStore(profile="t-bad", default_cwd="/tmp", chat_default_effort={GROUP: "ultra"})
    assert bad.default_effort_for(GROUP, "claude") is None


def test_effort_and_status_cards_say_group_default(env):
    bot = _Bot()
    _all(bot, "effort medium")
    text = asyncio.run(commands.handle_command("effort", "", ADMIN, TOPIC, bot.store, bot=bot))["text"]
    assert "当前推理强度：**medium**（跟随默认）" in text
    assert "本群默认：**medium**" in text
    status = asyncio.run(commands.handle_command("status", "", ADMIN, TOPIC, bot.store, bot=bot))
    status = status["text"] if isinstance(status, dict) else status
    assert "推理强度: `medium`（跟随本群默认）" in status


def test_remove_env_line(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\nMAIN_CHAT_MODEL_oc_x=opus\nB=2\nMAIN_CHAT_MODEL_oc_x=dup\n")
    changed, _ = group_allowlist.remove_env_line("MAIN_CHAT_MODEL_oc_x", str(path))
    assert changed and path.read_text().splitlines() == ["A=1", "B=2"]
    changed, _ = group_allowlist.remove_env_line("MAIN_CHAT_MODEL_oc_x", str(path))
    assert not changed
    assert group_allowlist.remove_env_line("bad key", str(path))[1].startswith("❌")
