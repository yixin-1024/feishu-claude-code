"""kiro 后端在 cc-lark 各处的接线：分发、配置、会话、斜杠命令、提示词、footer。"""

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


def _profile(runner="kiro"):
    return Profile(
        name="kiro",
        app_id="a",
        app_secret="b",
        platform="lark",
        domain="https://open.larksuite.com",
        default_cwd="/tmp",
        runner=runner,
        default_model="auto",
        lark_cli_profile="kiro",
    )


def test_run_agent_dispatches_to_kiro(monkeypatch):
    captured = {}

    async def fake_kiro(**kwargs):
        captured.update(kwargs)
        return "ok", "sid-k", False

    async def fake_claude(**kwargs):
        raise AssertionError("claude runner should not be called")

    monkeypatch.setattr("agent_runner.run_kiro", fake_kiro)
    monkeypatch.setattr("agent_runner.run_claude", fake_claude)

    profile = _profile()
    profile.kiro_bin = "/opt/homebrew/bin/kiro-cli"
    profile.kiro_idle_timeout_sec = 900
    result = asyncio.run(run_agent(
        profile=profile,
        runner="kiro",
        message="hi",
        model="claude-opus-5.5",
        effort="high",
        cwd="/tmp",
        wake_context={"CC_LARK_THREAD_ID": "omt_k"},
    ))

    assert result == ("ok", "sid-k", False)
    assert captured["model"] == "claude-opus-5.5"
    assert captured["effort"] == "high"
    assert captured["kiro_bin"] == "/opt/homebrew/bin/kiro-cli"
    assert captured["idle_timeout_sec"] == 900
    assert captured["dangerously_skip_permissions"] is True
    assert captured["extra_env"]["CC_LARK_THREAD_ID"] == "omt_k"
    assert captured["extra_env"]["CC_LARK_PROFILE"] == "kiro"


def test_load_profile_accepts_kiro_runner(monkeypatch):
    monkeypatch.setenv("KX_APP_ID", "app")
    monkeypatch.setenv("KX_APP_SECRET", "secret")
    monkeypatch.setenv("KX_PLATFORM", "lark")
    monkeypatch.setenv("KX_DEFAULT_CWD", "/tmp")
    monkeypatch.setenv("KX_RUNNER", "kiro")
    monkeypatch.setenv("KX_KIRO_BIN", "/x/kiro-cli")
    monkeypatch.setenv("KX_KIRO_IDLE_TIMEOUT_SEC", "120")
    monkeypatch.setenv("KX_KIRO_DANGEROUS_SKIP", "0")

    profile = bot_config._load_profile("kx")

    assert profile.runner == "kiro"
    assert profile.kiro_bin == "/x/kiro-cli"
    assert profile.kiro_idle_timeout_sec == 120
    assert profile.kiro_dangerous_skip == 0


@pytest.mark.parametrize("model,ok", [
    ("auto", True), ("claude-opus-5.5", True), ("claude-sonnet-5.5", True),
    ("claude-haiku-4.5", True), ("claude-sonnet-4", True), ("gpt-5.6-sol", True),
    ("deepseek-3.2", True), ("glm-5", True), ("minimax-m2.5", True),
    ("qwen3-coder-next", True), ("kiro-opus", True),
    ("opus", False), ("claude-sonnet-4-6", False), ("claude-fable-5-1", False),
    ("gemini-3.8-flash", False), ("Qwen3.8-Flash", False),
])
def test_model_compat_for_kiro(model, ok):
    assert is_model_compatible_with_runner(model, "kiro") is ok


def test_kiro_models_do_not_leak_into_claude():
    assert is_model_compatible_with_runner("claude-opus-5.5", "claude") is False
    assert is_model_compatible_with_runner("claude-opus-5-5", "claude") is True
    assert normalize_model_for_runner("opus", "kiro", "auto") == "auto"
    assert normalize_model_for_runner("kiro-sonnet", "kiro", "auto") == "claude-sonnet-5.5"


def test_agent_alias_knows_kiro():
    assert AGENT_RUNNER_ALIASES["kiro"] == "kiro"
    assert AGENT_RUNNER_ALIASES["kiro-cli"] == "kiro"
    assert '"kiro"' in alias_doc()


@pytest.fixture
def store(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "state"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_store_module, "SESSIONS_DIR", str(sessions_dir))
    return SessionStore(
        profile="kiro",
        default_cwd=str(tmp_path),
        default_runner="kiro",
        default_model="auto",
    )


def _bot():
    return SimpleNamespace(profile=SimpleNamespace(name="kiro", runner="kiro", default_model="auto"))


def test_store_keeps_kiro_as_default_runner(store):
    assert store._default_runner == "kiro"


@pytest.mark.asyncio
async def test_set_runner_normalizes_kiro_cli(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "state2"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_store_module, "SESSIONS_DIR", str(sessions_dir))
    s = SessionStore(profile="p", default_cwd=str(tmp_path), default_runner="claude",
                     default_model="sonnet")
    await s.set_runner("u", "oc_1", "kiro-cli", model="auto")
    stored = [c["current"]["runner"] for u in s._data.values() if isinstance(u, dict)
              for c in u.values() if isinstance(c, dict) and "current" in c]
    assert "kiro" in stored


@pytest.mark.asyncio
async def test_runner_command_switches_to_kiro(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "state3"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_store_module, "SESSIONS_DIR", str(sessions_dir))
    s = SessionStore(profile="p", default_cwd=str(tmp_path), default_runner="claude",
                     default_model="sonnet")
    bot = SimpleNamespace(profile=SimpleNamespace(name="p", runner="claude", default_model="sonnet"))

    picker = await handle_command("runner", "", "u", "oc_1", s, bot=bot)
    assert "Kiro" in [b["text"] for b in picker["buttons"]]

    reply = await handle_command("runner", "kiro", "u", "oc_1", s, bot=bot)
    assert reply.startswith("✅ 已切换 runner 为 `kiro`，模型 `auto`")


@pytest.mark.asyncio
async def test_model_picker_and_alias_for_kiro(store):
    picker = await handle_command("model", "", "u", "oc_1", store, bot=_bot())
    labels = [b["text"] for b in picker["buttons"]]
    assert labels[0].startswith("🧭 Auto")
    assert any("Opus 5.5" in label for label in labels)

    reply = await handle_command("model", "kiro-opus", "u", "oc_1", store, bot=_bot())
    cur = await store.get_current("u", "oc_1")
    assert "claude-opus-5.5" in reply
    assert cur.model == "claude-opus-5.5"

    bad = await handle_command("model", "opus", "u", "oc_1", store, bot=_bot())
    assert "不兼容" in bad


@pytest.mark.asyncio
async def test_effort_levels_for_kiro(store):
    picker = await handle_command("effort", "", "u", "oc_1", store, bot=_bot())
    labels = [b["text"].lower() for b in picker["buttons"]]
    assert "max" in labels and "low" in labels

    await handle_command("effort", "xhigh", "u", "oc_1", store, bot=_bot())
    raw = await store.get_current_raw("u", "oc_1")
    assert raw["effort_override"] == "xhigh"


def test_footer_shows_tokens_window_and_credits():
    usage = {"_context_ratio": 0.0888, "_context_window": 1_000_000, "_context_tokens": 88800,
             "_turn_credits": 0.0387}
    footer = _format_usage_footer(usage, "auto")
    assert footer.startswith("— 📊 上下文 88.8k / 1M") or footer.startswith("— 📊 上下文 88.8k / 1000k")
    assert "本轮 0.04 credits" in footer


def test_kiro_prompt_uses_claude_runtime_mcp_section_with_adapter():
    out = render_lark_prompt(
        profile=_profile(),
        raw_chat_id="oc_1",
        thread_id="omt_1",
        user_message_id="om_1",
        is_group=True,
        asker_open_id="ou_1",
        runner="kiro",
    )
    assert "本后端（Kiro CLI）怎么调下面这些工具" in out
    assert "mcp__cc-lark__wake_me_in" in out
    assert "本后端无运行时 MCP 工具" not in out


@pytest.mark.asyncio
async def test_usage_command_for_kiro(store):
    reply = await handle_command("usage", "", "u", "oc_1", store, bot=_bot())
    text = reply["text"] if isinstance(reply, dict) else reply
    assert "Kiro CLI 用量" in text
    assert "Runner: `kiro`" in text
