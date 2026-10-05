"""qoder 后端在 cc-lark 各处的接线：分发、配置、会话、斜杠命令、提示词、footer。"""

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
from commands import _format_context_line, handle_command
from dispatcher import _format_usage_footer
from lark_prompts import render_lark_prompt
from session_store import SessionStore


def _profile(runner="qoder"):
    return Profile(
        name="mac",
        app_id="a",
        app_secret="b",
        platform="lark",
        domain="https://open.larksuite.com",
        default_cwd="/tmp",
        runner=runner,
        default_model="Auto",
        lark_cli_profile="mac",
    )


def test_run_agent_dispatches_to_qoder(monkeypatch):
    captured = {}

    async def fake_qoder(**kwargs):
        captured.update(kwargs)
        return "ok", "sid-q", False

    async def fake_claude(**kwargs):
        raise AssertionError("claude runner should not be called")

    monkeypatch.setattr("agent_runner.run_qoder", fake_qoder)
    monkeypatch.setattr("agent_runner.run_claude", fake_claude)

    profile = _profile()
    profile.qoder_bin = "/opt/qodercli"
    profile.qoder_api_key = "pat"
    profile.qoder_idle_timeout_sec = 900
    result = asyncio.run(run_agent(
        profile=profile,
        runner="qoder",
        message="hi",
        model="Qwen3.8-Max",
        effort="high",
        cwd="/tmp",
        wake_context={"CC_LARK_THREAD_ID": "omt_q"},
    ))

    assert result == ("ok", "sid-q", False)
    assert captured["model"] == "Qwen3.8-Max"
    assert captured["effort"] == "high"
    assert captured["qoder_bin"] == "/opt/qodercli"
    assert captured["api_key"] == "pat"
    assert captured["idle_timeout_sec"] == 900
    assert captured["dangerously_skip_permissions"] is True
    # wake_context 要带到 runner，cc-lark MCP 的 env 从这里来
    assert captured["extra_env"]["CC_LARK_THREAD_ID"] == "omt_q"
    assert captured["extra_env"]["CC_LARK_PROFILE"] == "mac"


def test_load_profile_accepts_qoder_runner(monkeypatch):
    monkeypatch.setenv("QX_APP_ID", "app")
    monkeypatch.setenv("QX_APP_SECRET", "secret")
    monkeypatch.setenv("QX_PLATFORM", "lark")
    monkeypatch.setenv("QX_DEFAULT_CWD", "/tmp")
    monkeypatch.setenv("QX_RUNNER", "qoder")
    monkeypatch.setenv("QX_QODER_BIN", "/x/qodercli")
    monkeypatch.setenv("QX_QODER_IDLE_TIMEOUT_SEC", "120")
    monkeypatch.setenv("QX_QODER_DANGEROUS_SKIP", "0")

    profile = bot_config._load_profile("qx")

    assert profile.runner == "qoder"
    assert profile.qoder_bin == "/x/qodercli"
    assert profile.qoder_idle_timeout_sec == 120
    assert profile.qoder_dangerous_skip == 0


@pytest.mark.parametrize("model,ok", [
    ("Auto", True), ("auto", True), ("Ultimate", True), ("Efficient", True),
    ("Qwen3.8-Max", True), ("Kimi-K3", True), ("GLM-5.3", True),
    ("DeepSeek-V4-Pro", True), ("MiniMax-M3", True), ("qoder-qwen", True),
    ("opus", False), ("claude-sonnet-4-6", False), ("gpt-5.5", False),
    ("gemini-3.8-flash", False),
])
def test_model_compat_for_qoder(model, ok):
    assert is_model_compatible_with_runner(model, "qoder") is ok


def test_qoder_models_do_not_leak_into_claude():
    assert is_model_compatible_with_runner("Auto", "claude") is False
    assert is_model_compatible_with_runner("Qwen3.8-Max", "claude") is False
    # Claude 模型派给 qoder bot 时回落到 qoder 的默认
    assert normalize_model_for_runner("opus", "qoder", "Auto") == "Auto"
    assert normalize_model_for_runner("qoder-kimi", "qoder", "Auto") == "Kimi-K3"


def test_agent_alias_knows_qoder():
    assert AGENT_RUNNER_ALIASES["qoder"] == "qoder"
    assert AGENT_RUNNER_ALIASES["qodercli"] == "qoder"
    assert '"qoder"' in alias_doc()


@pytest.fixture
def store(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "state"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_store_module, "SESSIONS_DIR", str(sessions_dir))
    return SessionStore(
        profile="mac",
        default_cwd=str(tmp_path),
        default_runner="qoder",
        default_model="Auto",
    )


def _bot():
    return SimpleNamespace(profile=SimpleNamespace(name="mac", runner="qoder", default_model="Auto"))


def test_store_keeps_qoder_as_default_runner(store):
    # 校验集漏了新后端时 default_runner 会被静默打回 claude（踩过的坑）
    assert store._default_runner == "qoder"


@pytest.mark.asyncio
async def test_set_runner_normalizes_qodercli(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "state2"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_store_module, "SESSIONS_DIR", str(sessions_dir))
    s = SessionStore(profile="p", default_cwd=str(tmp_path), default_runner="claude",
                     default_model="sonnet")
    # 别名归一 + 校验集放行（之前只认 7 个后端，传 qoder 会直接 ValueError）。
    # 注意 runner 由 profile 钉死，下次 get_current 会拉回 profile 默认，所以看落盘那一刻的值。
    await s.set_runner("u", "oc_1", "qodercli", model="Auto")
    stored = [c["current"]["runner"] for u in s._data.values() if isinstance(u, dict)
              for c in u.values() if isinstance(c, dict) and "current" in c]
    assert "qoder" in stored


@pytest.mark.asyncio
async def test_runner_command_switches_to_qoder(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "state3"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_store_module, "SESSIONS_DIR", str(sessions_dir))
    s = SessionStore(profile="p", default_cwd=str(tmp_path), default_runner="claude",
                     default_model="sonnet")
    bot = SimpleNamespace(profile=SimpleNamespace(name="p", runner="claude", default_model="sonnet"))

    picker = await handle_command("runner", "", "u", "oc_1", s, bot=bot)
    assert "Qoder" in [b["text"] for b in picker["buttons"]]

    reply = await handle_command("runner", "qoder", "u", "oc_1", s, bot=bot)
    assert reply.startswith("✅ 已切换 runner 为 `qoder`，模型 `Qwen3.8-Flash`")


@pytest.mark.asyncio
async def test_model_picker_and_alias_for_qoder(store):
    picker = await handle_command("model", "", "u", "oc_1", store, bot=_bot())
    labels = [b["text"] for b in picker["buttons"]]
    assert labels[0] == "🆓 Qwen3.8 Flash"
    assert "🧭 Auto" in labels and "🇨🇳 Qwen3.8 Max" in labels
    assert all("Opus" not in label and "Sonnet" not in label for label in labels)

    reply = await handle_command("model", "qoder-kimi", "u", "oc_1", store, bot=_bot())
    cur = await store.get_current("u", "oc_1")
    assert "Kimi-K3" in reply
    assert cur.model == "Kimi-K3"

    bad = await handle_command("model", "opus", "u", "oc_1", store, bot=_bot())
    assert "不兼容" in bad


@pytest.mark.asyncio
async def test_effort_levels_for_qoder(store):
    picker = await handle_command("effort", "", "u", "oc_1", store, bot=_bot())
    labels = [b["text"].lower() for b in picker["buttons"]]
    assert "ultracode" in labels and "auto" in labels

    await handle_command("effort", "max", "u", "oc_1", store, bot=_bot())
    raw = await store.get_current_raw("u", "oc_1")
    assert raw["effort_override"] == "max"


def test_footer_and_status_show_estimated_tokens_and_window():
    # 用户截图里那一轮：69.4%、69.78 credits，Auto 窗口 200K
    usage = {"_context_ratio": 0.694, "_context_window": 200_000,
             "_context_tokens": 138800, "_turn_credits": 69.78}
    assert _format_usage_footer(usage, "Auto") == "— 📊 上下文 138.8k / 200k (69.4%) · 本轮 69.78 credits"
    assert _format_context_line(None, "Auto", runner="qoder", current_usage=usage) == \
        "上下文: `138.8k / 200k (69.4%)`"


def test_footer_falls_back_to_ratio_only_for_old_usage():
    # 升级前落盘的 last_usage 只有占比
    usage = {"_context_ratio": 0.11064, "_turn_credits": 0.0672}
    assert _format_usage_footer(usage, "Auto") == "— 📊 上下文 11.1% · 本轮 0.07 credits"
    assert _format_context_line(None, "Auto", runner="qoder", current_usage=usage) == "上下文: `11.1%`"
    # 别的后端照旧
    assert _format_usage_footer({}, "Auto") == ""


def test_qoder_prompt_uses_claude_runtime_mcp_section():
    out = render_lark_prompt(
        profile=_profile(),
        raw_chat_id="oc_1",
        thread_id="omt_1",
        user_message_id="om_1",
        is_group=True,
        asker_open_id="ou_1",
        runner="qoder",
    )
    assert "cc-lark 运行时 MCP 工具" in out
    assert "mcp__cc-lark__wake_me_in" in out
    assert "本后端无运行时 MCP 工具" not in out


@pytest.mark.asyncio
async def test_usage_command_shows_qoder_plan_credits(store, monkeypatch):
    import qoder_runner
    monkeypatch.setattr(qoder_runner, "fetch_qoder_plan_usage", lambda *a, **k: {
        "plan": "Pro Trial", "expires": "2026-10-19 10:36",
        "plan_credits": (37.0, 300.0), "addon_credits": (0.0, 100.0),
    })
    reply = await handle_command("usage", "", "u", "oc_1", store, bot=_bot())
    text = reply["text"] if isinstance(reply, dict) else reply
    assert "Qoder CLI 用量" in text
    assert "Pro Trial（2026-10-19 10:36 到期）" in text
    assert "已用 37 / 300，剩 263" in text
