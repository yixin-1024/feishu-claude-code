"""改 .env 里的模型配置不用重启：默认模型、派活模型、群级模型 / 强度、agy 通道、pi 默认模型。

和名单热重读（test_user_acl）是同一套机制：每次取模型前看一眼 .env 变没变，变了就重读。
"""
from __future__ import annotations

import os

import pytest
from dotenv import dotenv_values

import bot_config
import user_acl
from bot_config import Profile
from session_store import SessionStore

G1 = "oc_g1"


def _rewrite(path, text):
    """改写 .env 并把 mtime 往后拨，避免同一时钟刻度内改两次被当成没变。"""
    st = os.stat(path)
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def _armed(path, monkeypatch, base_env=None, **kw) -> Profile:
    """像启动时那样从 .env 读出 profile，并记下 .env 的样子（参与热重读）。"""
    monkeypatch.setattr(bot_config, "_BASE_ENVIRON", dict(base_env or {}))
    monkeypatch.setattr(user_acl, "_ENV_RELOADER", bot_config.reload_acl_from_env)
    vals = {k: v for k, v in dotenv_values(str(path)).items() if v is not None}
    merged = {**(base_env or {}), **vals}
    base = dict(name="test", app_id="cli_test", app_secret="secret", platform="lark",
                domain="open.larksuite.com", default_cwd="/tmp")
    base.update(bot_config._acl_from_env("test", "lark", merged))
    base.update(bot_config._models_from_env("test", merged))
    base.update(kw)
    p = Profile(**base)
    p.acl_env_file = str(path)
    p.acl_env_sig = bot_config._env_file_sig(str(path))
    return p


def _store(p: Profile, tmp_path, monkeypatch) -> SessionStore:
    """照 BotInstance 的接法：取模型前 refresh，默认模型变了用回调同步进 store。"""
    monkeypatch.setattr("session_store.SESSIONS_DIR", str(tmp_path))
    store = SessionStore(
        profile=p.name, default_cwd="/tmp", default_runner=p.runner,
        default_model=p.default_model, chat_default_model=p.chat_default_model,
        chat_default_effort=p.chat_default_effort,
        env_refresh=lambda: user_acl.refresh(p),
    )
    p.env_reload_hooks.append(lambda prof: store.set_default_model(prof.default_model))
    return store


@pytest.mark.asyncio
async def test_default_model_edit_reaches_old_session_without_restart(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TEST_DEFAULT_MODEL=gemini-4-neon\n", encoding="utf-8")
    p = _armed(env, monkeypatch, runner="agy")
    store = _store(p, tmp_path, monkeypatch)
    assert (await store.get_current("u1", "oc_x")).model == "gemini-4-neon"

    _rewrite(env, "TEST_DEFAULT_MODEL=gemini-4-carbon\n")
    # 同一个老话题、没 /model 覆盖：下一条消息就换成新默认
    assert (await store.get_current("u1", "oc_x")).model == "gemini-4-carbon"
    assert p.default_model == "gemini-4-carbon"
    assert store.default_model == "gemini-4-carbon"


@pytest.mark.asyncio
async def test_model_override_still_wins_after_reload(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TEST_DEFAULT_MODEL=gemini-4-neon\n", encoding="utf-8")
    p = _armed(env, monkeypatch, runner="agy")
    store = _store(p, tmp_path, monkeypatch)
    await store.set_model("u1", "oc_x", "gemini-3.8-flash")
    _rewrite(env, "TEST_DEFAULT_MODEL=gemini-4-carbon\n")
    assert (await store.get_current("u1", "oc_x")).model == "gemini-3.8-flash"


def test_dispatch_model_and_chat_level_settings_reload_in_place(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TEST_DISPATCH_MODEL=opus[1m]\n", encoding="utf-8")
    p = _armed(env, monkeypatch)
    chat_models, chat_efforts = p.chat_default_model, p.chat_default_effort
    store = _store(p, tmp_path, monkeypatch)
    assert store.default_effort_for(G1, "claude") is None

    _rewrite(env, "\n".join([
        "TEST_DISPATCH_MODEL=fable[1m]",
        f"TEST_CHAT_MODEL_{G1}=sonnet",
        f"TEST_CHAT_EFFORT_{G1}=MAX",
    ]) + "\n")
    assert store.default_effort_for(G1, "claude") == "max"
    assert p.dispatch_model == "fable[1m]"
    assert p.chat_default_model == {G1: "sonnet"}
    assert p.chat_default_model is chat_models            # 原地改，store 手里的引用跟着变
    assert p.chat_default_effort is chat_efforts
    assert store.default_model_for(G1, "claude") == bot_config.normalize_model("sonnet")


def test_agy_channel_switch_without_restart(tmp_path, monkeypatch):
    """Antigravity 从订阅切到 API key（换 Carbon 要的那几行）也是下一轮就生效。"""
    env = tmp_path / ".env"
    env.write_text("\n".join([
        "AGY_API_KEY=global-key",
        "TEST_DEFAULT_MODEL=gemini-3.8-flash",
        "TEST_AGY_MODEL_PROVIDER=",
        "TEST_AGY_API_KEY=",
    ]) + "\n", encoding="utf-8")
    p = _armed(env, monkeypatch, runner="agy")
    assert (p.agy_model_provider, p.agy_api_key) == ("", "")

    _rewrite(env, "\n".join([
        "AGY_API_KEY=global-key",
        "TEST_DEFAULT_MODEL=gemini-4-carbon",
        "TEST_AGY_MODEL_PROVIDER=gemini",
    ]) + "\n")
    user_acl.refresh(p)
    assert p.agy_model_provider == "gemini"
    assert p.agy_api_key == "global-key"                   # 删掉本 profile 那行 = 继承全局 key
    assert p.default_model == "gemini-4-carbon"


def test_deleted_line_falls_back_to_global_then_base_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DEFAULT_MODEL=opus[1m]\nTEST_DEFAULT_MODEL=sonnet\n", encoding="utf-8")
    p = _armed(env, monkeypatch)
    assert p.default_model == "sonnet"
    _rewrite(env, "DEFAULT_MODEL=opus[1m]\n")
    user_acl.refresh(p)
    assert p.default_model == "opus[1m]"                   # 本 profile 那行删了 → 全局
    _rewrite(env, "# 都删了\n")
    user_acl.refresh(p)
    assert p.default_model == ""                           # 不粘旧值


def test_unchanged_env_does_not_fire_hooks(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TEST_DEFAULT_MODEL=sonnet\nTEST_ALLOWED_OPEN_IDS=ou_a\n", encoding="utf-8")
    p = _armed(env, monkeypatch)
    calls = []
    p.env_reload_hooks.append(lambda prof: calls.append(prof.default_model))
    user_acl.refresh(p)
    assert calls == []
    # 只改名单、模型没变：也不打扰回调
    _rewrite(env, "TEST_DEFAULT_MODEL=sonnet\nTEST_ALLOWED_OPEN_IDS=ou_a,ou_b\n")
    user_acl.refresh(p)
    assert calls == []
    _rewrite(env, "TEST_DEFAULT_MODEL=opus\nTEST_ALLOWED_OPEN_IDS=ou_a,ou_b\n")
    user_acl.refresh(p)
    assert calls == ["opus"]


def test_pi_default_model_synced_into_process_env(tmp_path, monkeypatch):
    """pi 每次现读 os.environ 的 <PREFIX>_PI_MODEL：热重读时同步进去。"""
    monkeypatch.delenv("TEST_PI_MODEL", raising=False)
    monkeypatch.delenv("PI_MODEL", raising=False)
    env = tmp_path / ".env"
    env.write_text("TEST_PI_MODEL=deepseek/a\n", encoding="utf-8")
    p = _armed(env, monkeypatch, runner="pi")
    _rewrite(env, "TEST_PI_MODEL=deepseek/b\n")
    user_acl.refresh(p)
    assert os.environ["TEST_PI_MODEL"] == "deepseek/b"
    _rewrite(env, "# 删了\n")
    user_acl.refresh(p)
    assert "TEST_PI_MODEL" not in os.environ


def test_load_profile_semantics_unchanged(monkeypatch):
    """启动加载改用 _models_from_env 后，前缀行为空也照样盖住全局（老语义）。"""
    environ = {
        "AGY_MODEL_PROVIDER": "gemini", "AGY_API_KEY": "k", "DEFAULT_MODEL": "opus[1m]",
        "TEST_AGY_MODEL_PROVIDER": "", "TEST_AGY_API_KEY": "",
        f"TEST_CHAT_MODEL_{G1}": " sonnet ", f"TEST_CHAT_EFFORT_{G1}": "High",
        "TEST_CHAT_MODEL_oc_empty": "  ",
    }
    m = bot_config._models_from_env("test", environ)
    assert m["agy_model_provider"] == "" and m["agy_api_key"] == ""
    assert m["default_model"] == "opus[1m]"
    assert m["chat_default_model"] == {G1: "sonnet"}
    assert m["chat_default_effort"] == {G1: "high"}
    assert bot_config._models_from_env("x", {})["agy_model_provider"] == "gemini"
    assert bot_config._models_from_env("x", {"GEMINI_API_KEY": "g"})["agy_api_key"] == "g"


def test_bot_instance_registers_reload_hook(monkeypatch, tmp_path):
    monkeypatch.setattr("session_store.SESSIONS_DIR", str(tmp_path))
    from bot_instance import BotInstance
    p = Profile(name="hooktest", app_id="cli_x", app_secret="s", platform="lark",
                domain=bot_config._DOMAIN_MAP["lark"], default_cwd="/tmp", default_model="sonnet")
    bot = BotInstance(p)
    assert bot._on_env_models_reloaded in p.env_reload_hooks
    p.default_model = "opus"
    bot._on_env_models_reloaded(p)
    assert bot.store._default_model == "opus"
