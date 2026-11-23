"""操作者白名单（user_acl）：按群配人、私聊开关、管理员、/acl 命令。

钉住的规矩：
  1) 什么都不配 = 老行为：谁都能 @、谁都能私聊、谁都能 /restart /group；
  2) 本群名单替代全局名单；空名单 = 只有管理员；`*` = 不单独限制；
  3) 管理员永远放行（不会把自己锁在门外）；
  4) 没权限的人静默忽略，不回话；
  5) /acl 只有管理员能用，改动实时生效并落 .env（clear 写 `*` 不删行）。
"""

import asyncio
import os
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import bot_config
import card_security
import dispatcher
import group_allowlist
import user_acl
from bot_config import Profile
from bot_instance import BotInstance

ADMIN = "ou_admin"
ALICE = "ou_alice"
BOB = "ou_bob"
G1 = "oc_g1"
G2 = "oc_g2"


def _profile(**kw) -> Profile:
    base = dict(name="test", app_id="cli_test", app_secret="secret",
                platform="lark", domain="open.larksuite.com", default_cwd="/tmp",
                allowed_group_chat_ids={"*"})
    base.update(kw)
    return Profile(**base)


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("TEST_APP_ID=x\n# keep me\n", encoding="utf-8")
    os.chmod(path, 0o600)
    monkeypatch.setenv("CC_LARK_ENV_FILE", str(path))
    return path


# ── 判定 ─────────────────────────────────────────────────────────

def test_defaults_keep_old_behaviour():
    p = _profile()
    assert user_acl.user_allowed(p, ALICE, is_group=True, raw_chat_id=G1)
    assert user_acl.user_allowed(p, ALICE, is_group=False)
    assert user_acl.is_admin(p, ALICE)  # 没配管理员 = 谁都算
    assert not user_acl.user_allowed(p, "", is_group=False)


def test_chat_list_only_affects_that_chat():
    p = _profile(admin_open_ids={ADMIN}, chat_allowed_open_ids={G1: {ALICE}})
    assert user_acl.user_allowed(p, ALICE, is_group=True, raw_chat_id=G1)
    assert not user_acl.user_allowed(p, BOB, is_group=True, raw_chat_id=G1)
    assert user_acl.user_allowed(p, ADMIN, is_group=True, raw_chat_id=G1)
    # 别的群、私聊都不受影响
    assert user_acl.user_allowed(p, BOB, is_group=True, raw_chat_id=G2)
    assert user_acl.user_allowed(p, BOB, is_group=False)


def test_empty_chat_list_means_admins_only():
    p = _profile(admin_open_ids={ADMIN}, chat_allowed_open_ids={G1: set()})
    assert not user_acl.user_allowed(p, ALICE, is_group=True, raw_chat_id=G1)
    assert user_acl.user_allowed(p, ADMIN, is_group=True, raw_chat_id=G1)


def test_chat_list_replaces_global_list():
    p = _profile(allowed_open_ids={ALICE}, chat_allowed_open_ids={G1: {BOB}})
    assert user_acl.user_allowed(p, BOB, is_group=True, raw_chat_id=G1)
    assert not user_acl.user_allowed(p, ALICE, is_group=True, raw_chat_id=G1)
    assert user_acl.user_allowed(p, ALICE, is_group=True, raw_chat_id=G2)
    assert not user_acl.user_allowed(p, BOB, is_group=True, raw_chat_id=G2)


def test_p2p_switch_and_list():
    p = _profile(admin_open_ids={ADMIN}, allow_p2p=False)
    assert not user_acl.user_allowed(p, ALICE, is_group=False)
    assert user_acl.user_allowed(p, ADMIN, is_group=False)
    assert user_acl.user_allowed(p, ALICE, is_group=True, raw_chat_id=G1)  # 群不受影响

    p = _profile(p2p_allowed_open_ids={ALICE})
    assert user_acl.user_allowed(p, ALICE, is_group=False)
    assert not user_acl.user_allowed(p, BOB, is_group=False)


def test_admin_gate():
    p = _profile(admin_open_ids={ADMIN})
    assert user_acl.is_admin(p, ADMIN)
    assert not user_acl.is_admin(p, ALICE)
    assert not user_acl.is_admin(p, "")


def test_profile_without_new_fields_still_works():
    """测试里一堆假 profile 只有老字段 —— 不能因为缺属性就炸。"""
    p = SimpleNamespace(allowed_open_ids={ALICE})
    assert user_acl.user_allowed(p, ALICE, is_group=True, raw_chat_id=G1)
    assert not user_acl.user_allowed(p, BOB, is_group=False)
    assert user_acl.is_admin(p, BOB)


def test_parse_chat_users():
    assert user_acl.parse_chat_users("*") is None
    assert user_acl.parse_chat_users("") == set()
    assert user_acl.parse_chat_users(" ou_a , ou_b ,") == {"ou_a", "ou_b"}


def test_profile_loader_reads_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("ACLT_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("ACLT_APP_ID", "cli_x")
    monkeypatch.setenv("ACLT_APP_SECRET", "s")
    monkeypatch.setenv("ACLT_ADMIN_OPEN_IDS", "ou_admin")
    monkeypatch.setenv("ACLT_CHAT_USERS_oc_a", "ou_1,ou_2")
    monkeypatch.setenv("ACLT_CHAT_USERS_oc_b", "")
    monkeypatch.setenv("ACLT_CHAT_USERS_oc_c", "*")
    monkeypatch.setenv("ACLT_ALLOW_P2P", "0")
    monkeypatch.setenv("ACLT_P2P_USERS", "ou_1")
    p = bot_config._load_profile("aclt")
    assert p.admin_open_ids == {"ou_admin"}
    assert p.chat_allowed_open_ids == {"oc_a": {"ou_1", "ou_2"}, "oc_b": set()}
    assert p.allow_p2p is False
    assert p.p2p_allowed_open_ids == {"ou_1"}

    monkeypatch.delenv("ACLT_ALLOW_P2P")
    assert bot_config._load_profile("aclt").allow_p2p is True  # 默认开


def test_card_clicks_use_same_rules():
    p = _profile(admin_open_ids={ADMIN}, chat_allowed_open_ids={G1: {ALICE}},
                 allow_p2p=False)
    assert card_security.card_action_allowed(p, ALICE, f"{G1}:omt_x")
    assert not card_security.card_action_allowed(p, BOB, G1)
    assert card_security.card_action_allowed(p, BOB, G2)
    assert not card_security.card_action_allowed(p, BOB, BOB)  # 私聊关了
    assert card_security.card_action_allowed(p, ADMIN, ADMIN)


# ── .env 落盘 ────────────────────────────────────────────────────

def test_set_env_line_replace_append_dedupe(env_file):
    changed, info = group_allowlist.set_env_line("TEST_ALLOW_P2P", "0", str(env_file))
    assert changed and info == str(env_file)
    assert env_file.read_text(encoding="utf-8").endswith("TEST_ALLOW_P2P=0\n")
    assert group_allowlist.set_env_line("TEST_ALLOW_P2P", "0", str(env_file))[0] is False
    with open(env_file, "a", encoding="utf-8") as f:
        f.write("TEST_ALLOW_P2P=0\n")
    assert group_allowlist.set_env_line("TEST_ALLOW_P2P", "1", str(env_file))[0] is True
    text = env_file.read_text(encoding="utf-8")
    assert text.count("TEST_ALLOW_P2P=") == 1 and "TEST_ALLOW_P2P=1" in text
    assert "# keep me" in text
    assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600


def test_set_env_line_rejects_injection(env_file):
    before = env_file.read_text(encoding="utf-8")
    for key, val in (("A\nB", "1"), ("A=B", "1"), ("", "1"), ("OK", "1\nEVIL=1")):
        changed, info = group_allowlist.set_env_line(key, val, str(env_file))
        assert not changed and info.startswith("❌")
    assert env_file.read_text(encoding="utf-8") == before


# ── /acl 命令 ────────────────────────────────────────────────────

def _bot(profile):
    feishu = AsyncMock()
    feishu.batch_resolve_names = AsyncMock(return_value={})
    return SimpleNamespace(profile=profile, feishu=feishu)


def _run(coro):
    return asyncio.run(coro)


def test_acl_requires_admin(env_file):
    bot = _bot(_profile(admin_open_ids={ADMIN}))
    out = _run(user_acl.handle_acl_command(bot, ALICE, G1, "add", [(BOB, "Bob")]))
    assert out.startswith("⛔")
    assert bot.profile.chat_allowed_open_ids == {}
    assert "CHAT_USERS" not in env_file.read_text(encoding="utf-8")


def test_acl_add_remove_clear_roundtrip(env_file):
    bot = _bot(_profile(admin_open_ids={ADMIN}))
    out = _run(user_acl.handle_acl_command(
        bot, ADMIN, f"{G1}:omt_t", "add", [(ALICE, "Alice"), (BOB, "Bob")]))
    assert out.startswith("✅") and "Alice" in out and "第一份名单" in out
    assert bot.profile.chat_allowed_open_ids[G1] == {ALICE, BOB}
    assert f"TEST_CHAT_USERS_{G1}=ou_alice,ou_bob" in env_file.read_text(encoding="utf-8")

    out = _run(user_acl.handle_acl_command(bot, ADMIN, G1, "remove", [(BOB, "Bob")]))
    assert out.startswith("✅")
    assert bot.profile.chat_allowed_open_ids[G1] == {ALICE}

    out = _run(user_acl.handle_acl_command(bot, ADMIN, G1, f"remove {ALICE}"))
    assert "只有管理员" in out
    assert bot.profile.chat_allowed_open_ids[G1] == set()
    assert f"TEST_CHAT_USERS_{G1}=\n" in env_file.read_text(encoding="utf-8")
    assert not user_acl.user_allowed(bot.profile, ALICE, is_group=True, raw_chat_id=G1)

    out = _run(user_acl.handle_acl_command(bot, ADMIN, G1, "clear"))
    assert out.startswith("✅")
    assert G1 not in bot.profile.chat_allowed_open_ids
    assert f"TEST_CHAT_USERS_{G1}=*\n" in env_file.read_text(encoding="utf-8")
    assert user_acl.user_allowed(bot.profile, ALICE, is_group=True, raw_chat_id=G1)


def test_acl_add_validates_targets(env_file):
    bot = _bot(_profile(admin_open_ids={ADMIN}))
    assert "具体的人" in _run(user_acl.handle_acl_command(bot, ADMIN, G1, "add"))
    assert "不对" in _run(user_acl.handle_acl_command(bot, ADMIN, G1, "add ou_x=1"))
    assert "那个群里" in _run(user_acl.handle_acl_command(bot, ADMIN, ADMIN, "add", [(BOB, "")]))
    assert "没设名单" in _run(user_acl.handle_acl_command(bot, ADMIN, G1, "remove", [(BOB, "")]))
    assert bot.profile.chat_allowed_open_ids == {}


def test_acl_dm_toggle(env_file):
    bot = _bot(_profile(admin_open_ids={ADMIN}))
    assert _run(user_acl.handle_acl_command(bot, ADMIN, ADMIN, "dm off")).startswith("✅")
    assert bot.profile.allow_p2p is False
    assert "TEST_ALLOW_P2P=0" in env_file.read_text(encoding="utf-8")
    assert _run(user_acl.handle_acl_command(bot, ADMIN, G1, "dm on")).startswith("✅")
    assert bot.profile.allow_p2p is True
    assert "用法" in _run(user_acl.handle_acl_command(bot, ADMIN, G1, "dm maybe"))


def test_acl_show(env_file):
    bot = _bot(_profile(admin_open_ids={ADMIN}, chat_allowed_open_ids={G1: {ALICE}}))
    bot.feishu.batch_resolve_names = AsyncMock(return_value={ALICE: "Alice", ADMIN: "Boss"})
    out = _run(user_acl.handle_acl_command(bot, ADMIN, G1, ""))
    assert "Alice" in out and "Boss" in out and "私聊：开" in out
    out = _run(user_acl.handle_acl_command(bot, ADMIN, G2, ""))
    assert "沿用默认" in out


def test_mention_targets_skip_bot_and_all():
    def m(oid, name=""):
        return SimpleNamespace(id=SimpleNamespace(open_id=oid), name=name, key="@_x")
    got = user_acl.mention_targets([m("ou_bot"), m(ALICE, "Alice"), m("", "所有人")], "ou_bot")
    assert got == [(ALICE, "Alice")]


# ── 端到端：dispatcher 入口 ──────────────────────────────────────

def _make_bot(profile) -> BotInstance:
    bot = BotInstance.__new__(BotInstance)
    bot.profile = profile
    bot.chat_locks = {}
    bot.active_runs = MagicMock()
    bot.store = MagicMock()
    bot.feishu = AsyncMock()
    bot.feishu.get_bot_open_id = AsyncMock(return_value="ou_bot")
    bot.feishu.batch_resolve_names = AsyncMock(return_value={})
    return bot


def _event(text, sender, *, chat_id=G1, chat_type="group", extra_mentions=(), msg_id="om_1"):
    ev = Mock()
    ev.event.sender.sender_id.open_id = sender
    ev.event.message.chat_id = chat_id
    ev.event.message.chat_type = chat_type
    ev.event.message.message_type = "text"
    ev.event.message.message_id = msg_id
    ev.event.message.thread_id = ""
    mentions = []
    keys = []
    for i, (oid, name) in enumerate([("ou_bot", "bot"), *extra_mentions], start=1):
        mm = Mock()
        mm.key = f"@_user_{i}"
        mm.id = Mock()
        mm.id.open_id = oid
        mm.name = name
        mentions.append(mm)
        keys.append(mm.key)
    if chat_type == "group":
        head = keys[0]
        tail = " ".join(keys[1:])
        ev.event.message.content = '{"text": "%s %s %s"}' % (head, text, tail)
        ev.event.message.mentions = mentions
    else:
        ev.event.message.content = '{"text": "%s"}' % text
        ev.event.message.mentions = []
    return ev


@pytest.fixture(autouse=True)
def _reset_seen():
    dispatcher._seen_messages.clear()
    yield
    dispatcher._seen_messages.clear()


async def test_unlisted_user_is_silently_ignored():
    bot = _make_bot(_profile(admin_open_ids={ADMIN}, chat_allowed_open_ids={G1: {ALICE}}))
    with patch("dispatcher._process_message", new_callable=AsyncMock) as proc, \
         patch("dispatcher.handle_command", new_callable=AsyncMock) as hc:
        await dispatcher.handle_message_async(bot, _event("帮我查一下", BOB))
        await dispatcher.handle_message_async(bot, _event("/usage", BOB, msg_id="om_2"))
    proc.assert_not_awaited()
    hc.assert_not_awaited()
    bot.feishu.reply_card.assert_not_awaited()
    bot.feishu.reply_text.assert_not_awaited()


async def test_dm_off_ignores_non_admin():
    bot = _make_bot(_profile(admin_open_ids={ADMIN}, allow_p2p=False))
    with patch("dispatcher.handle_command", new_callable=AsyncMock) as hc:
        hc.return_value = "📊"
        await dispatcher.handle_message_async(bot, _event("/usage", BOB, chat_id="oc_dm", chat_type="p2p"))
        hc.assert_not_awaited()
        await dispatcher.handle_message_async(
            bot, _event("/usage", ADMIN, chat_id="oc_dm", chat_type="p2p", msg_id="om_2"))
        hc.assert_awaited_once()


async def test_acl_via_message_uses_mentions(env_file):
    bot = _make_bot(_profile(admin_open_ids={ADMIN}))
    await dispatcher.handle_message_async(
        bot, _event("/acl add", ADMIN, extra_mentions=[(ALICE, "Alice")]))
    assert bot.profile.chat_allowed_open_ids == {G1: {ALICE}}  # bot 自己没被加进去
    bot.feishu.reply_card.assert_awaited_once()
    assert "Alice" in bot.feishu.reply_card.await_args.kwargs["content"]


async def test_restart_requires_admin():
    bot = _make_bot(_profile(admin_open_ids={ADMIN}))
    with patch("commands._trigger_restart") as trig:
        await dispatcher.handle_message_async(bot, _event("/restart", ALICE))
    trig.assert_not_called()
    assert not dispatcher._restart_in_progress
    bot.feishu.reply_text.assert_awaited_once()
    assert "管理员" in bot.feishu.reply_text.await_args.args[1]


async def test_group_command_requires_admin():
    import commands
    bot = SimpleNamespace(profile=_profile(admin_open_ids={ADMIN}))
    out = await commands.handle_command("group", "add oc_new", ALICE, G1, MagicMock(), bot=bot)
    assert out.startswith("⛔")
    assert "oc_new" not in bot.profile.allowed_group_chat_ids


# ── 名单热重读：改 .env 不用重启 ─────────────────────────────────

def _armed_profile(path, monkeypatch, base_env=None) -> Profile:
    """像 bot_config 启动时那样：从 .env 读名单并记下 .env 的样子（参与热重读）。"""
    monkeypatch.setattr(bot_config, "_BASE_ENVIRON", dict(base_env or {}))
    # 别的测试会重新 import bot_config，注册进 user_acl 的可能是另一份模块的函数；
    # 钉成本文件这份，monkeypatch 的 _BASE_ENVIRON 才作用得上。
    monkeypatch.setattr(user_acl, "_ENV_RELOADER", bot_config.reload_acl_from_env)
    from dotenv import dotenv_values
    vals = {k: v for k, v in dotenv_values(str(path)).items() if v is not None}
    acl = bot_config._acl_from_env("test", "lark", {**(base_env or {}), **vals})
    p = _profile(**acl)
    p.acl_env_file = str(path)
    p.acl_env_sig = bot_config._env_file_sig(str(path))
    return p


def _rewrite(path, text):
    """改写 .env 并把 mtime 往后拨，避免同一时钟刻度内改两次被当成没变。"""
    st = os.stat(path)
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_reloader_registered_by_bot_config():
    fn = user_acl._ENV_RELOADER
    assert fn is not None and fn.__name__ == "reload_acl_from_env"
    assert fn.__module__ == "bot_config"


def test_global_list_edit_takes_effect_without_restart(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(f"TEST_ADMIN_OPEN_IDS={ADMIN}\nTEST_ALLOWED_OPEN_IDS={ADMIN}\n",
                   encoding="utf-8")
    p = _armed_profile(env, monkeypatch)
    glob_ref = p.allowed_open_ids
    assert not user_acl.user_allowed(p, ALICE, is_group=False)

    _rewrite(env, f"TEST_ADMIN_OPEN_IDS={ADMIN}\nTEST_ALLOWED_OPEN_IDS={ADMIN},{ALICE}\n")
    assert user_acl.user_allowed(p, ALICE, is_group=False)       # 下一条消息就放行
    assert user_acl.user_allowed(p, ALICE, is_group=True, raw_chat_id=G1)
    assert not user_acl.is_admin(p, ALICE)
    assert p.allowed_open_ids is glob_ref                         # 原地改，别处的引用跟着变

    _rewrite(env, f"TEST_ADMIN_OPEN_IDS={ADMIN}\nTEST_ALLOWED_OPEN_IDS={ADMIN}\n")
    assert not user_acl.user_allowed(p, ALICE, is_group=False)   # 删人也立刻生效


def test_reload_covers_admin_chat_list_p2p_and_groups(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TEST_ALLOWED_GROUP_CHAT_IDS=oc_g1\n", encoding="utf-8")
    p = _armed_profile(env, monkeypatch)
    assert p.allowed_group_chat_ids == {G1}
    assert user_acl.is_admin(p, ALICE)  # 没配管理员 = 老行为

    _rewrite(env, "\n".join([
        f"TEST_ADMIN_OPEN_IDS={ADMIN}",
        f"TEST_ALLOWED_GROUP_CHAT_IDS={G1},{G2}",
        f"TEST_CHAT_USERS_{G2}={BOB}",
        "TEST_ALLOW_P2P=0",
    ]) + "\n")
    user_acl.refresh(p)
    assert p.allowed_group_chat_ids == {G1, G2}
    assert not user_acl.is_admin(p, ALICE)
    assert user_acl.user_allowed(p, BOB, is_group=True, raw_chat_id=G2)
    assert not user_acl.user_allowed(p, ALICE, is_group=True, raw_chat_id=G2)
    assert not user_acl.user_allowed(p, ALICE, is_group=False)   # 私聊关了
    assert user_acl.user_allowed(p, ADMIN, is_group=False)


def test_acl_command_write_then_reload_keeps_list(env_file, monkeypatch):
    """/acl add 自己写的 .env 被热重读回来，名单不变（不会自己把自己冲掉）。"""
    p = _armed_profile(env_file, monkeypatch)
    user_acl.set_chat_users(p, G1, {ALICE, BOB})
    assert bot_config.reload_acl_from_env(p)                      # .env 变了，重读一次
    assert p.chat_allowed_open_ids == {G1: {ALICE, BOB}}
    assert not bot_config.reload_acl_from_env(p)                  # 没再变就不重读


def test_deleted_line_falls_back_to_base_env_not_sticky(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(f"TEST_ALLOWED_OPEN_IDS={ADMIN}\n", encoding="utf-8")
    p = _armed_profile(env, monkeypatch, base_env={})
    assert not user_acl.user_allowed(p, ALICE, is_group=False)
    _rewrite(env, "# 名单那行删了\n")
    assert user_acl.user_allowed(p, ALICE, is_group=False)       # 回到"不限人"，不粘旧值


def test_unarmed_profile_is_never_reloaded(tmp_path, monkeypatch):
    """测试里手搓的 / 旧版单实例 profile 没记 .env，不参与热重读。"""
    p = _profile(allowed_open_ids={ADMIN})
    assert not bot_config.reload_acl_from_env(p)
    user_acl.refresh(p)
    assert p.allowed_open_ids == {ADMIN}


def test_missing_env_file_keeps_old_lists(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(f"TEST_ALLOWED_OPEN_IDS={ADMIN}\n", encoding="utf-8")
    p = _armed_profile(env, monkeypatch)
    env.unlink()
    assert not bot_config.reload_acl_from_env(p)
    assert p.allowed_open_ids == {ADMIN}


def test_card_click_sees_reloaded_group_list(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TEST_ALLOWED_GROUP_CHAT_IDS=oc_g1\n", encoding="utf-8")
    p = _armed_profile(env, monkeypatch)
    assert not card_security.card_action_allowed(p, ALICE, f"{G2}:omt_x")
    _rewrite(env, f"TEST_ALLOWED_GROUP_CHAT_IDS={G1},{G2}\n")
    assert card_security.card_action_allowed(p, ALICE, f"{G2}:omt_x")
