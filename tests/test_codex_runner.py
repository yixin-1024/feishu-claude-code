import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codex_runner import _clean_stderr, run_codex


class FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)
        self._i = 0

    async def readline(self):
        if self._i >= len(self._lines):
            return b""
        line = self._lines[self._i]
        self._i += 1
        return line


class FakeStderr:
    async def read(self):
        return b""


class FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = FakeStdout(lines)
        self.stderr = FakeStderr()
        self.returncode = None
        self._final_returncode = returncode
        self.pid = 12345

    async def wait(self):
        self.returncode = self._final_returncode
        return self.returncode

    def terminate(self):
        self.returncode = -15


def test_run_codex_parses_thread_and_streams(monkeypatch):
    captured = {}
    proc = FakeProc([
        b'{"type":"thread.started","thread_id":"thread_123"}\n',
        b'{"type":"item.started","item":{"type":"command_execution","command":"/bin/zsh -lc pwd","status":"in_progress"}}\n',
        b'{"type":"item.completed","item":{"type":"command_execution","command":"/bin/zsh -lc pwd","aggregated_output":"/tmp\\n","exit_code":0,"status":"completed"}}\n',
        b'{"type":"item.delta","delta":"hello"}\n',
        b'{"type":"item.completed","item":{"type":"agent_message","text":"hello world"}}\n',
        b'{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":5,"reasoning_output_tokens":0}}\n',
    ])

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        captured["cwd"] = kwargs.get("cwd")
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    # 单轮解析用例：关掉自动续跑，否则续跑会重复调用已耗尽的 FakeProc。
    monkeypatch.setenv("CODEX_AUTO_CONTINUE", "0")

    chunks = []
    tools = []
    usages = []

    async def on_chunk(chunk):
        chunks.append(chunk)

    async def on_tool(name, inp):
        tools.append((name, inp))

    def on_usage(usage):
        usages.append(usage)

    text, sid, fallback = asyncio.run(run_codex(
        "hi",
        session_id="old_thread",
        model="gpt-5.1-codex-max",
        cwd="/tmp",
        codex_bin="/bin/codex",
        on_text_chunk=on_chunk,
        on_tool_use=on_tool,
        on_usage=on_usage,
    ))

    assert text == "hello world"
    assert sid == "thread_123"
    assert fallback is False
    assert captured["cwd"] == "/tmp"
    cmd = captured["cmd"]
    assert cmd[:8] == [
        "/bin/codex", "-s", "danger-full-access", "-a", "never", "exec", "resume", "--json",
    ]
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-5.1-codex-max"
    assert "--json" in cmd
    assert "old_thread" in cmd
    assert cmd[-1] == "hi"
    assert chunks
    assert tools[-1][0] == "bash"
    assert tools[-1][1]["command"] == "/bin/zsh -lc pwd"
    assert tools[-1][1]["exit_code"] == 0
    assert usages[-1] == {
        "input_tokens": 100,
        "_cached_input_tokens": 40,
        "output_tokens": 5,
    }


def test_run_codex_prepends_system_prompt(monkeypatch):
    captured = {}
    proc = FakeProc([
        b'{"type":"thread.started","thread_id":"thread_1"}\n',
        b'{"type":"turn.completed","text":"ok"}\n',
    ])

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("CODEX_AUTO_CONTINUE", "0")

    text, sid, _ = asyncio.run(run_codex(
        "user message",
        append_system_prompt="system block",
        codex_bin="/bin/codex",
    ))

    assert text == "ok"
    assert sid == "thread_1"
    assert captured["cmd"][-1] == "system block\n\nuser message"


def test_run_codex_injects_cc_lark_mcp(monkeypatch):
    captured = {}
    proc = FakeProc([
        b'{"type":"thread.started","thread_id":"thread_1"}\n',
        b'{"type":"turn.completed","text":"ok"}\n',
    ])

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("CC_LARK_ALLOW_WAKE", "0")
    monkeypatch.setenv("CODEX_AUTO_CONTINUE", "0")

    text, sid, _ = asyncio.run(run_codex(
        "hi",
        codex_bin="/bin/codex",
        extra_env={
            "CC_LARK_CHAT_ID": "oc_1",
            "CC_LARK_THREAD_ID": "omt_1",
            "CC_LARK_MESSAGE_ID": "om_1",
            "CC_LARK_CONTROL_PORT": "9982",
            "CC_LARK_CONTROL_TOKEN": "secret-token",
            "NOT_CC_LARK": "ignored",
        },
    ))

    assert text == "ok"
    assert sid == "thread_1"
    cmd = captured["cmd"]
    config = {
        cmd[i + 1].split("=", 1)[0]: cmd[i + 1].split("=", 1)[1]
        for i, value in enumerate(cmd)
        if value == "-c"
    }
    assert config["mcp_servers.cc-lark.command"].startswith('"')
    assert config["mcp_servers.cc-lark.args"].endswith('cc_mcp_server.py"]')
    assert config["mcp_servers.cc-lark.env.CC_LARK_CHAT_ID"] == '"oc_1"'
    assert config["mcp_servers.cc-lark.env.CC_LARK_THREAD_ID"] == '"omt_1"'
    assert config["mcp_servers.cc-lark.env.CC_LARK_MESSAGE_ID"] == '"om_1"'
    assert config["mcp_servers.cc-lark.env.CC_LARK_CONTROL_PORT"] == '"9982"'
    # Secret remains in the inherited process environment; never serialize it into
    # Codex `-c` flags where it would be visible in `ps` output.
    assert "mcp_servers.cc-lark.env.CC_LARK_CONTROL_TOKEN" not in config
    assert config["mcp_servers.cc-lark.env.CC_LARK_ALLOW_WAKE"] == '"0"'
    assert not any("NOT_CC_LARK" in item for item in cmd)


def test_run_codex_skips_cc_lark_mcp_without_thread(monkeypatch):
    captured = {}
    proc = FakeProc([
        b'{"type":"thread.started","thread_id":"thread_1"}\n',
        b'{"type":"turn.completed","text":"ok"}\n',
    ])

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    asyncio.run(run_codex(
        "hi",
        codex_bin="/bin/codex",
        extra_env={"CC_LARK_CHAT_ID": "oc_1"},
    ))

    assert not any("mcp_servers.cc-lark" in item for item in captured["cmd"])


def test_run_codex_surfaces_usage_limit_error(monkeypatch):
    """codex 命中用量限制时以 stdout JSON error 事件报出、且 returncode=0；
    runner 必须把这条真因抛出来，而不是让无害的 stdin 横幅冒充报错。"""
    proc = FakeProc([
        b'{"type":"thread.started","thread_id":"thread_x"}\n',
        b'{"type":"turn.started"}\n',
        b'{"type":"error","message":"You\'ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at Jul 26th, 2026 11:01 PM."}\n',
        b'{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit."}}\n',
    ], returncode=0)

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(run_codex("hi", codex_bin="/bin/codex"))

    msg = str(excinfo.value)
    assert "usage limit" in msg
    assert "Jul 26th, 2026" in msg
    # 绝不能把无害横幅当报错抛出来。
    assert "Reading additional input from stdin" not in msg


def test_clean_stderr_drops_benign_noise():
    raw = (
        "Reading additional input from stdin...\n"
        "2026-07-21T01:41:42Z ERROR codex_models_manager::manager: failed to renew cache TTL\n"
        "Shell cwd was reset to /some/path\n"
        "panic: real fatal error"
    )
    assert _clean_stderr(raw) == "panic: real fatal error"
    assert _clean_stderr("Reading additional input from stdin...\n") == ""
