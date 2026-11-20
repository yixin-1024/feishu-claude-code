"""pi 后端在 cc-lark 各处的接线：分发、配置、会话、斜杠命令、提示词、footer、派活回退。"""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config
import session_store as session_store_module
from agent_alias import AGENT_RUNNER_ALIASES, alias_doc
from agent_runner import run_agent
from bot_config import Profile, is_model_compatible_with_runner, normalize_model_for_runner
from commands import handle_command
from dispatcher import _format_usage_footer
from lark_prompts import render_lark_prompt
from session_store import SessionStore


def _profile(runner="claude"):
    return Profile(
        name="main",
        app_id="a",
        app_secret="b",
        platform="lark",
        domain="https://open.larksuite.com",
        default_cwd="/tmp",
        runner=runner,
        default_model="opus[1m]",
        lark_cli_profile="main",
    )


def test_run_agent_dispatches_to_pi(monkeypatch):
    captured = {}

    async def fake_pi(**kwargs):
        captured.update(kwargs)
        return "ok", "sid", False

    monkeypatch.setattr("agent_runner.run_pi", fake_pi)
    profile = _profile()
    profile.pi_bin = "/usr/bin/pi"
    profile.pi_idle_timeout_sec = 900
    out = asyncio.run(run_agent(
        profile=profile,
        runner="pi",
        message="hi",
        model="google/gemini-3.8-flash",
        effort="low",
        wake_context={"CC_LARK_THREAD_ID": "omt_1"},
    ))
    assert out == ("ok", "sid", False)
    assert captured["pi_bin"] == "/usr/bin/pi"
    assert captured["idle_timeout_sec"] == 900
    assert captured["effort"] == "low"
    assert captured["extra_env"]["CC_LARK_PROFILE"] == "main"
    assert captured["extra_env"]["CC_LARK_THREAD_ID"] == "omt_1"


def test_load_profile_accepts_pi_runner(monkeypatch):
    monkeypatch.setenv("PX_APP_ID", "cli_x")
    monkeypatch.setenv("PX_APP_SECRET", "s")
    monkeypatch.setenv("PX_RUNNER", "pi")
    monkeypatch.setenv("PX_PI_BIN", "/x/pi")
    monkeypatch.setenv("PX_PI_AGENT_DIR", "/x/agent")
    monkeypatch.setenv("PX_PI_IDLE_TIMEOUT_SEC", "120")
    monkeypatch.setenv("PX_PI_DANGEROUS_SKIP", "0")
    profile = bot_config._load_profile("px")
    assert profile.runner == "pi"
    assert profile.pi_bin == "/x/pi"
    assert profile.pi_agent_dir == "/x/agent"
    assert profile.pi_idle_timeout_sec == 120
    assert profile.pi_dangerous_skip == 0


@pytest.mark.parametrize("model,ok", [
    ("google/gemini-3.8-flash", True), ("google/gemini-4-neon", True), ("pi-hydrogen", True),
    ("pi-carbon", True), ("google/gemini-4-argon", True),
    ("gemini-4-carbon", True), ("agy-neon", True),  # 裸 id / agy 的别名也认，传给 pi 时补 google/
    ("gemini-3.8-flash", True),  # opencode 的别名，解析成 google/gemini-3.8-flash
    # 只接 Gemini 4 全家 + 3.8 Flash（用户 11-20 定）
    ("openai-codex/gpt-5.5", False), ("google/gemini-3.1-pro-preview", False), ("gemini-3.7-flash", False),
    ("google/gemini-4-neon:high", False), ("anthropic/claude-x", False),
    ("opus", False), ("opus[1m]", False), ("claude-opus-5-5", False), ("auto", False),
])
def test_model_compat_for_pi(model, ok):
    assert is_model_compatible_with_runner(model, "pi") is ok


def test_pi_models_do_not_leak_into_claude():
    assert is_model_compatible_with_runner("pi-flash", "claude") is False
    assert normalize_model_for_runner("opus", "pi", "google/gemini-3.8-flash") == "google/gemini-3.8-flash"
    assert normalize_model_for_runner("pi-argon", "pi", "") == "google/gemini-4-argon"
    assert normalize_model_for_runner("agy-neon", "pi", "") == "google/gemini-4-neon"
    assert normalize_model_for_runner("gemini-4-hydrogen", "pi", "") == "google/gemini-4-hydrogen"


def test_agent_alias_knows_pi():
    assert AGENT_RUNNER_ALIASES["pi"] == "pi"
    assert '"pi"' in alias_doc()


def _new_store(tmp_path, monkeypatch, name="state", runner="claude", model="opus[1m]"):
    sessions_dir = tmp_path / name
    sessions_dir.mkdir()
    monkeypatch.setattr(session_store_module, "SESSIONS_DIR", str(sessions_dir))
    return SessionStore(profile="main", default_cwd=str(tmp_path), default_runner=runner, default_model=model)


def _bot(runner="claude"):
    return SimpleNamespace(profile=SimpleNamespace(name="main", runner=runner, default_model="opus[1m]"))


def test_store_keeps_pi_as_default_runner(tmp_path, monkeypatch):
    s = _new_store(tmp_path, monkeypatch, runner="pi", model="google/gemini-3.8-flash")
    assert s._default_runner == "pi"


@pytest.mark.asyncio
async def test_runner_command_switches_to_pi(tmp_path, monkeypatch):
    monkeypatch.delenv("PI_MODEL", raising=False)
    monkeypatch.delenv("MAIN_PI_MODEL", raising=False)
    s = _new_store(tmp_path, monkeypatch)
    picker = await handle_command("runner", "", "u", "oc_1", s, bot=_bot())
    assert "pi" in [b["text"] for b in picker["buttons"]]

    reply = await handle_command("runner", "pi", "u", "oc_1", s, bot=_bot())
    assert reply.startswith("✅ 已切换 runner 为 `pi`，模型 `google/gemini-3.8-flash`")
    cur = await s.get_current("u", "oc_1")
    assert cur.runner == "pi" and cur.model == "google/gemini-3.8-flash"


@pytest.mark.asyncio
async def test_model_picker_alias_and_effort_for_pi(tmp_path, monkeypatch):
    s = _new_store(tmp_path, monkeypatch)
    await s.set_runner("u", "oc_1", "pi", model="google/gemini-3.8-flash")

    picker = await handle_command("model", "", "u", "oc_1", s, bot=_bot())
    labels = [b["text"] for b in picker["buttons"]]
    for name in ("Gemini 4 Neon", "Gemini 4 Hydrogen", "Gemini 4 Carbon", "Gemini 4 Argon", "Gemini 3.8 Flash"):
        assert any(name in label for label in labels)
    assert not any("GPT" in label or "3.1 Pro" in label for label in labels)

    reply = await handle_command("model", "pi-neon", "u", "oc_1", s, bot=_bot())
    cur = await s.get_current("u", "oc_1")
    assert "google/gemini-4-neon" in reply and cur.model == "google/gemini-4-neon"

    bad = await handle_command("model", "opus", "u", "oc_1", s, bot=_bot())
    assert "不兼容" in bad

    # Neon 有 xhigh
    picker = await handle_command("effort", "", "u", "oc_1", s, bot=_bot())
    levels = [b["text"].lower() for b in picker["buttons"]]
    assert "xhigh" in levels and "high" in levels and "max" not in levels and "minimal" not in levels
    await handle_command("effort", "xhigh", "u", "oc_1", s, bot=_bot())
    raw = await s.get_current_raw("u", "oc_1")
    assert raw["effort_override"] == "xhigh"

    # 3.8 Flash 只有三档
    await handle_command("model", "pi-flash", "u", "oc_1", s, bot=_bot())
    picker = await handle_command("effort", "", "u", "oc_1", s, bot=_bot())
    levels = [b["text"].lower() for b in picker["buttons"]]
    assert "xhigh" not in levels and "high" in levels


@pytest.mark.asyncio
async def test_usage_command_for_pi(tmp_path, monkeypatch):
    s = _new_store(tmp_path, monkeypatch)
    await s.set_runner("u", "oc_1", "pi", model="google/gemini-3.8-flash")
    reply = await handle_command("usage", "", "u", "oc_1", s, bot=_bot())
    text = reply if isinstance(reply, str) else reply.get("text", "")
    assert "pi 用量" in text
    assert "Runner: `pi`" in text


def test_footer_shows_turn_cost():
    usage = {"input_tokens": 2000, "cache_read_input_tokens": 28000, "output_tokens": 60,
             "_context_window": 1_000_000, "_turn_tokens": 90_000, "_turn_cost_usd": 0.0304}
    footer = _format_usage_footer(usage, "google/gemini-3.8-flash")
    assert "/ 1M" in footer
    assert "本轮 $0.03" in footer
    small = _format_usage_footer({**usage, "_turn_cost_usd": 0.0046}, "google/gemini-3.8-flash")
    assert "本轮 $0.0046" in small


def test_pi_prompt_uses_claude_runtime_mcp_section_with_adapter():
    out = render_lark_prompt(
        profile=_profile(),
        raw_chat_id="oc_1",
        thread_id="omt_1",
        user_message_id="om_1",
        is_group=True,
        asker_open_id="ou_1",
        runner="pi",
    )
    assert "本后端（pi）怎么调下面这些工具" in out
    assert "mcp__cc_lark__wake_me_in" in out
    assert "本后端无运行时 MCP 工具" not in out


def test_dispatch_falls_back_to_same_bot_for_pi():
    import http_server

    claude_bot = SimpleNamespace(profile=SimpleNamespace(name="main", runner="claude", platform="lark"))
    codex_bot = SimpleNamespace(profile=SimpleNamespace(name="larkgpt", runner="codex", platform="lark"))
    bots = {"main": claude_bot, "larkgpt": codex_bot}
    target, err = http_server.resolve_target_agent(bots, "pi", exclude="main")
    assert target is None and err
    assert http_server.runner_fallback_for(bots, "pi") == "pi"
    assert http_server.runner_fallback_for(bots, "kiro") == ""  # 只放实测过的后端
    assert http_server.runner_fallback_for(bots, "nonsense") == ""
    # 有专门的 pi bot 就该派给它，不走回退
    pi_bot = SimpleNamespace(profile=SimpleNamespace(name="pibot", runner="pi", platform="lark"))
    assert http_server.runner_fallback_for({**bots, "pibot": pi_bot}, "pi") == ""


@pytest.mark.asyncio
async def test_handle_spawn_switches_thread_runner(tmp_path, monkeypatch):
    """dispatch 回退时 handle_spawn 把子话题的 runner 换掉，不兼容的 profile 默认模型不塞给它。"""
    import dispatcher

    s = _new_store(tmp_path, monkeypatch)
    seen = {}

    class FakeFeishu:
        async def get_message_thread_id(self, mid):
            return "omt_child"

        async def reply_card(self, *a, **k):
            return "om_card"

        async def reply_text(self, *a, **k):
            return None

    lock = asyncio.Lock()
    bot = SimpleNamespace(
        profile=SimpleNamespace(name="main", runner="claude", default_model="opus[1m]",
                                dispatch_model="opus[1m]"),
        store=s, feishu=FakeFeishu(), _ensure_chat_lock=lambda chat_id: lock,
    )

    async def fake_run_and_display(*args, **kwargs):
        cur = await s.get_current("u", "oc_1:omt_child")
        seen["runner"], seen["model"] = cur.runner, cur.model
        return (True, "done")

    monkeypatch.setattr(dispatcher, "_run_and_display", fake_run_and_display)
    monkeypatch.setattr(dispatcher, "build_lark_system_prompt", lambda *a, **k: "")
    res = await dispatcher.handle_spawn(
        bot, user_id="u", chat_id_raw="oc_1", thread_id="omt_child", anchor_message_id="om_a",
        prompt="do it", model="opus", runner="pi",
    )
    assert seen.get("runner") == "pi"
    # opus 不是 pi 的模型：不写覆盖，跑的时候 pi_runner 用自己的默认
    assert seen.get("model") == "opus[1m]"
    assert res[0] is True


@pytest.mark.asyncio
async def test_pi_runner_sticks_but_other_runners_snap_back(tmp_path, monkeypatch):
    s = _new_store(tmp_path, monkeypatch)
    await s.set_runner("u", "oc_1", "pi", model="google/gemini-3.8-flash")
    await s.new_session("u", "oc_1")
    cur = await s.get_current("u", "oc_1")
    assert cur.runner == "pi" and cur.model == "google/gemini-3.8-flash"

    # 切回 profile runner：不再钉住
    await s.set_runner("u", "oc_1", "claude")
    raw = await s.get_current_raw("u", "oc_1")
    assert raw["runner"] == "claude" and not raw.get("runner_pinned")

    # 要专门 bot 的后端（kiro 等）保持老行为：下一次读就回到 profile runner
    await s.set_runner("u", "oc_2", "kiro", model="auto")
    cur = await s.get_current("u", "oc_2")
    assert cur.runner == "claude"
