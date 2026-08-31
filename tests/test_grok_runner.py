import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grok_runner
from grok_runner import run_grok


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
    def __init__(self, blob=b""):
        self._blob = blob

    async def read(self):
        return self._blob


class FakeProc:
    def __init__(self, lines, returncode=0, stderr=b""):
        self.stdout = FakeStdout(lines)
        self.stderr = FakeStderr(stderr)
        self.returncode = None
        self._final_returncode = returncode
        self.pid = 4242

    async def wait(self):
        self.returncode = self._final_returncode
        return self.returncode

    def kill(self):
        self.returncode = -9


SID = "01a01f72-e413-77c1-b4fa-9bd4f61ba9f7"

# 真实 grok --output-format streaming-messages-json --include-partial-messages 的行
STREAM_LINES = [
    b'{"type":"system","subtype":"init","session_id":"' + SID.encode() + b'","model":"wow-gpt"}\n',
    b'{"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"c1","name":"read_file","input":{}}}}\n',
    b'{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"target_file\\":\\"probe.txt\\"}"}}}\n',
    b'{"type":"stream_event","event":{"type":"content_block_stop","index":0}}\n',
    b'{"type":"stream_event","event":{"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}}\n',
    b'{"type":"stream_event","event":{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"bana"}}}\n',
    b'{"type":"stream_event","event":{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"na-7788"}}}\n',
    b'{"type":"result","subtype":"success","is_error":false,"result":"banana-7788","session_id":"'
    + SID.encode()
    + b'","usage":{"input_tokens":18313,"output_tokens":28,"cache_read_input_tokens":18048,"server_tool_use":{"web_search_requests":0}},'
    b'"modelUsage":{"gpt-5.4":{"inputTokens":18313,"contextWindow":200000}}}\n',
]


def _patch_exec(monkeypatch, proc, captured):
    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def test_run_grok_streams_text_tools_and_usage(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, FakeProc(STREAM_LINES), captured)

    chunks, tools, usages = [], [], []
    text, sid, fallback = asyncio.run(
        run_grok(
            message="读 probe.txt",
            model="wow-gpt",
            cwd="/tmp",
            on_text_chunk=chunks.append,
            on_tool_use=lambda name, inp: tools.append((name, inp)),
            on_usage=usages.append,
            api_key="sk-test",
            api_key_env="WOWAPI_API_KEY",
        )
    )

    assert text == "banana-7788"
    assert sid == SID
    assert fallback is False
    assert "".join(chunks) == "banana-7788"
    # 工具事件：content_block_start 先报名字，content_block_stop 补齐入参
    assert tools[0] == ("read_file", {})
    assert tools[-1] == ("read_file", {"target_file": "probe.txt"})
    # server_tool_use 这类嵌套字段会被过滤掉，contextWindow 补进 _context_window
    assert usages[-1]["input_tokens"] == 18313
    assert usages[-1]["cache_read_input_tokens"] == 18048
    assert usages[-1]["_context_window"] == 200000
    assert "server_tool_use" not in usages[-1]


def test_run_grok_builds_expected_argv(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, FakeProc(STREAM_LINES), captured)

    asyncio.run(
        run_grok(
            message="hi",
            model="wow-glm",
            cwd="/tmp",
            effort="high",
            append_system_prompt="LARK RULES",
            max_turns=6,
            api_key="sk-test",
            api_key_env="WOWAPI_API_KEY",
        )
    )

    cmd = captured["cmd"]
    assert cmd[1:5] == ["--prompt-file", cmd[2], "--output-format", "streaming-messages-json"]
    assert "--include-partial-messages" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert cmd[cmd.index("-m") + 1] == "wow-glm"
    assert cmd[cmd.index("--reasoning-effort") + 1] == "high"
    assert cmd[cmd.index("--rules") + 1] == "LARK RULES"
    assert cmd[cmd.index("--max-turns") + 1] == "6"
    # 首轮：自己钉一个新 UUID，不带 --resume
    assert "--session-id" in cmd and "--resume" not in cmd
    # 凭证注入到 config.toml 的 env_key 指定的变量上
    assert captured["env"]["WOWAPI_API_KEY"] == "sk-test"
    assert captured["cwd"] == "/tmp"
    # prompt 落文件后应被清理
    assert not os.path.exists(cmd[2])


def test_run_grok_resume_uses_same_session(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, FakeProc(STREAM_LINES), captured)

    asyncio.run(run_grok(message="hi", session_id=SID, cwd="/tmp"))

    cmd = captured["cmd"]
    assert cmd[cmd.index("--resume") + 1] == SID
    assert "--session-id" not in cmd


def test_run_grok_result_error_raises_retryable(monkeypatch):
    lines = [
        b'{"type":"system","subtype":"init","session_id":"' + SID.encode() + b'"}\n',
        b'{"type":"result","subtype":"error_during_execution","is_error":true,'
        b'"result":"Response stalled mid-stream","session_id":"' + SID.encode() + b'"}\n',
    ]
    _patch_exec(monkeypatch, FakeProc(lines), {})

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(run_grok(message="hi", cwd="/tmp"))
    assert getattr(exc.value, "cc_session_id", None) == SID
    assert getattr(exc.value, "cc_retryable_resume", None) is True


def test_run_grok_resume_dumb_failure_falls_back_to_fresh(monkeypatch):
    """resume 哑失败（code>0 + 无 stderr + 无输出）→ 退回新 session 重试一次。"""
    procs = [
        FakeProc([], returncode=1),  # resume 这一趟什么都没吐
        FakeProc(STREAM_LINES),      # fresh 重试成功
    ]
    seen = []

    async def fake_exec(*args, **kwargs):
        seen.append(list(args))
        return procs[len(seen) - 1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    text, sid, fallback = asyncio.run(
        run_grok(message="hi", session_id="stale-sid", cwd="/tmp")
    )
    assert text == "banana-7788"
    assert sid == SID
    assert fallback is True
    assert "--resume" in seen[0] and "--session-id" in seen[1]


def test_run_grok_rejects_unknown_effort():
    with pytest.raises(ValueError):
        asyncio.run(run_grok(message="hi", cwd="/tmp", effort="turbo"))


def test_permission_mode_passthrough_and_fallback():
    assert grok_runner._normalize_permission_mode("plan", True) == "plan"
    assert grok_runner._normalize_permission_mode("nonsense", True) == "bypassPermissions"
    assert grok_runner._normalize_permission_mode("", True) == "bypassPermissions"
