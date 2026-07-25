"""Focused regression tests for conversation-level effort runner plumbing."""

from __future__ import annotations

import asyncio

import agent_runner
import claude_runner
import codex_runner
from bot_config import Profile


def _profile(runner: str) -> Profile:
    return Profile(
        name="plumbing-bot",
        app_id="app",
        app_secret="secret",
        platform="lark",
        domain="https://open.larksuite.com",
        default_cwd="/tmp",
        runner=runner,
        default_model=(
            "gpt-5.6-sol" if runner == "codex" else "claude-sonnet-4-6"
        ),
    )


def test_agent_runner_propagates_effort_to_claude(monkeypatch):
    captured = {}

    async def fake_claude(**kwargs):
        captured.update(kwargs)
        return "ok", "claude-session", False

    monkeypatch.setattr(agent_runner, "run_claude", fake_claude)
    monkeypatch.setattr(
        agent_runner,
        "load_claude_extra_env",
        lambda _profile: {"CLAUDE_EFFORT": "low", "PROFILE_ONLY": "1"},
    )

    result = asyncio.run(
        agent_runner.run_agent(
            profile=_profile("claude"),
            runner="claude",
            message="hello",
            effort="xhigh",
            wake_context={"CC_LARK_THREAD_ID": "omt_test"},
        )
    )

    assert result == ("ok", "claude-session", False)
    assert captured["effort"] == "xhigh"
    assert captured["extra_env"]["CLAUDE_EFFORT"] == "low"
    assert captured["extra_env"]["PROFILE_ONLY"] == "1"
    assert captured["extra_env"]["CC_LARK_THREAD_ID"] == "omt_test"


def test_agent_runner_propagates_effort_and_profile_to_private_codex(monkeypatch):
    captured = {}

    async def fake_codex(**kwargs):
        captured.update(kwargs)
        return "ok", "codex-thread", False

    monkeypatch.setattr(agent_runner, "run_codex", fake_codex)

    result = asyncio.run(
        agent_runner.run_agent(
            profile=_profile("codex"),
            runner="codex",
            message="hello",
            effort="ultra",
            wake_context=None,
        )
    )

    assert result == ("ok", "codex-thread", False)
    assert captured["reasoning_effort"] == "ultra"
    assert captured["extra_env"] == {"CC_LARK_PROFILE": "plumbing-bot"}


class _BytesReader:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def readline(self):
        return self._chunks.pop(0) if self._chunks else b""

    async def read(self):
        return b"".join(self._chunks)


class _CodexProc:
    def __init__(self):
        self.stdout = _BytesReader(
            [
                b'{"type":"thread.started","thread_id":"existing-thread"}\n',
                b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
                b'{"type":"turn.completed"}\n',
            ]
        )
        self.stderr = _BytesReader([])
        self.returncode = None
        self.pid = 4242

    async def wait(self):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = -15


def _reasoning_config(cmd: list[str]) -> list[str]:
    return [
        cmd[index + 1]
        for index, token in enumerate(cmd[:-1])
        if token == "-c" and cmd[index + 1].startswith("model_reasoning_effort=")
    ]


def test_codex_resume_cli_explicit_effort_wins_over_profile_and_global_env(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return _CodexProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("CODEX_AUTO_CONTINUE", "0")
    monkeypatch.setenv("PLUMBING-BOT_CODEX_REASONING_EFFORT", "high")
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "medium")

    text, thread_id, fallback = asyncio.run(
        codex_runner.run_codex(
            "hello",
            session_id="existing-thread",
            reasoning_effort="ultra",
            codex_bin="/bin/codex",
            extra_env={"CC_LARK_PROFILE": "plumbing-bot"},
        )
    )

    assert (text, thread_id, fallback) == ("ok", "existing-thread", False)
    cmd = captured["cmd"]
    assert "resume" in cmd
    assert "existing-thread" in cmd
    assert cmd.index("resume") < cmd.index("-c") < cmd.index("existing-thread")
    assert _reasoning_config(cmd) == ["model_reasoning_effort=ultra"]


def test_codex_cli_profile_effort_wins_over_global_env(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return _CodexProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("CODEX_AUTO_CONTINUE", "0")
    monkeypatch.setenv("PLUMBING_CODEX_REASONING_EFFORT", "high")
    monkeypatch.setenv("CODEX_REASONING_EFFORT", "low")

    asyncio.run(
        codex_runner.run_codex(
            "hello",
            codex_bin="/bin/codex",
            extra_env={"CC_LARK_PROFILE": "plumbing"},
        )
    )

    assert _reasoning_config(captured["cmd"]) == ["model_reasoning_effort=high"]


class _ClaudeStdin:
    def __init__(self):
        self.data = b""

    def write(self, data: bytes):
        self.data += data

    async def drain(self):
        return None

    def close(self):
        return None


class _ClaudeProc:
    def __init__(self):
        self.stdin = _ClaudeStdin()
        self.stdout = _BytesReader(
            [b'{"type":"result","session_id":"claude-session","result":"ok"}\n']
        )
        self.stderr = _BytesReader([])
        self.returncode = 0
        self.pid = 4343

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_claude_print_cli_explicit_effort_wins_over_profile_and_global_env(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        captured["env"] = kwargs["env"]
        return _ClaudeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("CLAUDE_EFFORT", "medium")

    result = asyncio.run(
        claude_runner._run_claude_print(
            "hello",
            effort="xhigh",
            extra_env={"CLAUDE_EFFORT": "low"},
        )
    )

    assert result == ("ok", "claude-session", False)
    cmd = captured["cmd"]
    assert cmd[cmd.index("--effort") + 1] == "xhigh"
    assert cmd.count("--effort") == 1
    assert captured["env"]["CLAUDE_EFFORT"] == "xhigh"
    assert captured["env"]["CLAUDE_CODE_EFFORT_LEVEL"] == "xhigh"


def test_claude_print_cli_profile_effort_wins_over_global_env(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        captured["env"] = kwargs["env"]
        return _ClaudeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("CLAUDE_EFFORT", "low")

    asyncio.run(
        claude_runner._run_claude_print(
            "hello",
            extra_env={"CLAUDE_EFFORT": "high"},
        )
    )

    cmd = captured["cmd"]
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert captured["env"]["CLAUDE_CODE_EFFORT_LEVEL"] == "high"
