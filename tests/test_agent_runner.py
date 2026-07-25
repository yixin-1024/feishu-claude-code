import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runner import run_agent
from bot_config import Profile


def _profile(runner="codex"):
    return Profile(
        name="p",
        app_id="a",
        app_secret="b",
        platform="lark",
        domain="https://open.larksuite.com",
        default_cwd="/tmp",
        runner=runner,
        default_model=(
            "gpt-5.1-codex-max"
            if runner == "codex"
            else "google/gemini-3.1-pro-preview" if runner == "opencode"
            else "claude-sonnet-4-6"
        ),
    )


def test_run_agent_dispatches_to_codex(monkeypatch):
    captured = {}
    should_stop = lambda: False

    async def fake_codex(**kwargs):
        captured.update(kwargs)
        return "ok", "thread_1", False

    async def fake_claude(**kwargs):
        raise AssertionError("claude runner should not be called")

    monkeypatch.setattr("agent_runner.run_codex", fake_codex)
    monkeypatch.setattr("agent_runner.run_claude", fake_claude)

    text, sid, fallback = asyncio.run(run_agent(
        profile=_profile("codex"),
        runner="codex",
        message="hi",
        model="gpt-5.1-codex-max",
        cwd="/tmp",
        should_stop=should_stop,
        wake_context={"CC_LARK_THREAD_ID": "omt_1"},
    ))

    assert text == "ok"
    assert sid == "thread_1"
    assert fallback is False
    assert captured["message"] == "hi"
    assert captured["model"] == "gpt-5.1-codex-max"
    assert captured["should_stop"] is should_stop
    assert captured["extra_env"]["CC_LARK_THREAD_ID"] == "omt_1"


def test_run_agent_dispatches_to_opencode(monkeypatch):
    captured = {}

    async def fake_opencode(**kwargs):
        captured.update(kwargs)
        return "ok", "ses_1", False

    async def fake_claude(**kwargs):
        raise AssertionError("claude runner should not be called")

    async def fake_codex(**kwargs):
        raise AssertionError("codex runner should not be called")

    monkeypatch.setattr("agent_runner.run_opencode", fake_opencode)
    monkeypatch.setattr("agent_runner.run_claude", fake_claude)
    monkeypatch.setattr("agent_runner.run_codex", fake_codex)

    profile = _profile("opencode")
    text, sid, fallback = asyncio.run(run_agent(
        profile=profile,
        runner="opencode",
        message="hi",
        model="google/gemini-3.1-pro-preview",
        cwd="/tmp",
    ))

    assert text == "ok"
    assert sid == "ses_1"
    assert fallback is False
    assert captured["message"] == "hi"
    assert captured["model"] == "google/gemini-3.1-pro-preview"
    assert captured["provider"] == profile.opencode_provider


def test_run_agent_dispatches_to_mimo(monkeypatch):
    captured = {}

    async def fake_mimo(**kwargs):
        captured.update(kwargs)
        return "ok", "ses_mimo_1", False

    async def fake_claude(**kwargs):
        raise AssertionError("claude runner should not be called")

    async def fake_codex(**kwargs):
        raise AssertionError("codex runner should not be called")

    monkeypatch.setattr("agent_runner.run_mimo", fake_mimo)
    monkeypatch.setattr("agent_runner.run_claude", fake_claude)
    monkeypatch.setattr("agent_runner.run_codex", fake_codex)

    profile = _profile("mimo")
    profile.mimo_provider = "quotio"
    profile.mimo_base_url = "http://127.0.0.1:8317/v1"
    profile.mimo_api_key = "k"
    text, sid, fallback = asyncio.run(run_agent(
        profile=profile,
        runner="mimo",
        message="hi",
        model="quotio/claude-opus-4-8",
        cwd="/tmp",
    ))

    assert text == "ok"
    assert sid == "ses_mimo_1"
    assert fallback is False
    assert captured["message"] == "hi"
    assert captured["model"] == "quotio/claude-opus-4-8"
    assert captured["provider"] == "quotio"
    assert captured["base_url"] == "http://127.0.0.1:8317/v1"


def test_run_agent_merges_wake_context_into_claude_env(monkeypatch):
    captured = {}

    async def fake_claude(**kwargs):
        captured.update(kwargs)
        return "ok", "sid_1", False

    monkeypatch.setattr("agent_runner.run_claude", fake_claude)
    monkeypatch.setattr("agent_runner.load_claude_extra_env", lambda _profile: {
        "ANTHROPIC_MODEL": "claude-sonnet-4-6",
        "EXISTING": "1",
    })

    text, sid, fallback = asyncio.run(run_agent(
        profile=_profile("claude"),
        runner="claude",
        message="hi",
        model="claude-opus-4-8",
        cwd="/tmp",
        wake_context={
            "CC_LARK_CLI_PROFILE": "work",
            "CC_LARK_MESSAGE_ID": "om_1",
            "CC_LARK_CONTROL_PORT": "9982",
            "CC_LARK_CONTROL_TOKEN": "secret-token",
        },
    ))

    assert text == "ok"
    assert sid == "sid_1"
    assert fallback is False
    assert captured["extra_env"]["EXISTING"] == "1"
    assert captured["extra_env"]["ANTHROPIC_MODEL"] == "claude-sonnet-4-6"
    assert captured["extra_env"]["CC_LARK_CLI_PROFILE"] == "work"
    assert captured["extra_env"]["CC_LARK_MESSAGE_ID"] == "om_1"
    assert captured["extra_env"]["CC_LARK_CONTROL_PORT"] == "9982"
    assert captured["extra_env"]["CC_LARK_CONTROL_TOKEN"] == "secret-token"


def test_run_agent_applies_profile_claude_runner(monkeypatch):
    captured = {}

    async def fake_claude(**kwargs):
        captured.update(kwargs)
        return "ok", "sid_1", False

    monkeypatch.setattr("agent_runner.run_claude", fake_claude)
    monkeypatch.setattr("agent_runner.load_claude_extra_env", lambda _profile: {
        "CLAUDE_RUNNER": "pty",
    })

    profile = _profile("claude")
    profile.claude_runner = "print"

    text, sid, fallback = asyncio.run(run_agent(
        profile=profile,
        runner="claude",
        message="hi",
        model="claude-opus-4-8",
        cwd="/tmp",
    ))

    assert text == "ok"
    assert sid == "sid_1"
    assert fallback is False
    assert captured["extra_env"]["CLAUDE_RUNNER"] == "print"
