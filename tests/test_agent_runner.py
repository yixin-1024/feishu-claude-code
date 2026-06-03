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
        default_model="gpt-5.1-codex-max" if runner == "codex" else "claude-sonnet-4-6",
    )


def test_run_agent_dispatches_to_codex(monkeypatch):
    captured = {}

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
    ))

    assert text == "ok"
    assert sid == "thread_1"
    assert fallback is False
    assert captured["message"] == "hi"
    assert captured["model"] == "gpt-5.1-codex-max"
