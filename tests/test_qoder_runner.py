import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qoder_runner
from qoder_runner import run_qoder


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


class FakeStdin:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, lines, returncode=0, stderr=b""):
        self.stdin = FakeStdin()
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


SID = "38a02c48-a41b-43f4-a116-00f6e9a6a8cb"


def _ev(event, parent=None):
    return (json.dumps({
        "type": "stream_event", "event": event,
        "parent_tool_use_id": parent, "session_id": SID,
    }) + "\n").encode()


# 照 qodercli 1.1.65 `-p --output-format stream-json --include-partial-messages` 实测输出裁剪
STREAM_LINES = [
    b'{"type":"system","subtype":"hook_started","hook_name":"Initializing Qoder Security","session_id":"' + SID.encode() + b'"}\n',
    b'{"type":"system","subtype":"init","cwd":"/tmp/qtest","model":"Efficient","session_id":"' + SID.encode() + b'"}\n',
    _ev({"type": "message_start"}),
    _ev({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
    _ev({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "run cat"}}),
    _ev({"type": "content_block_stop", "index": 0}),
    _ev({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {}}}),
    _ev({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"command\": \"cat"}}),
    _ev({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": " a.txt\"}"}}),
    _ev({"type": "content_block_stop", "index": 1}),
    b'{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"call_1","content":"hello"}]},"session_id":"' + SID.encode() + b'"}\n',
    # 子 agent 内部的流不该混进正文
    _ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "SUBAGENT DRAFT"}}, parent="call_9"),
    _ev({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
    _ev({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "hel"}}),
    _ev({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "lo"}}),
    _ev({"type": "content_block_stop", "index": 1}),
    b'{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]},"session_id":"' + SID.encode() + b'"}\n',
    b'{"type":"result","subtype":"success","is_error":false,"result":"hello","total_credits":0.0672,"session_id":"'
    + SID.encode()
    + b'","usage":{"input_tokens":0,"output_tokens":0,"cache_read_input_tokens":0,"server_tool_use":{"web_search_requests":0},"context_usage_ratio":0.11064},'
    b'"modelUsage":{"efficient":{"inputTokens":0,"contextWindow":0,"credits":0.0672}}}\n',
]


def _patch_exec(monkeypatch, procs, captured):
    procs = list(procs)
    captured.setdefault("calls", [])

    async def fake_exec(*args, **kwargs):
        captured["calls"].append({"cmd": list(args), "cwd": kwargs.get("cwd"), "env": kwargs.get("env")})
        captured["cmd"] = list(args)
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        proc = procs.pop(0)
        captured.setdefault("procs", []).append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_run_qoder_streams_text_tools_and_usage(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM_LINES)], captured)

    chunks, tools, usages = [], [], []
    text, sid, fallback = asyncio.run(
        run_qoder(
            message="读 a.txt",
            model="Efficient",
            cwd="/tmp",
            on_text_chunk=chunks.append,
            on_tool_use=lambda name, inp: tools.append((name, inp)),
            on_usage=usages.append,
        )
    )

    assert text == "hello"
    assert sid == SID
    assert fallback is False
    # 正文只来自主 agent 的 text_delta；thinking 和子 agent 的流都不进
    assert "".join(chunks) == "hello"
    # 入参攒齐才报，一个工具只报一次
    assert tools == [("Bash", {"command": "cat a.txt"})]
    # qoder 的 token 数全是 0：只留上下文占比和 credits
    assert usages[-1] == {"_context_ratio": 0.11064, "_turn_credits": 0.0672}
    # prompt 走 stdin，写完就关
    proc = captured["procs"][0]
    assert proc.stdin.data.decode() == "读 a.txt"
    assert proc.stdin.closed


def test_run_qoder_interleaved_parallel_tools_keep_their_own_inputs(monkeypatch):
    # 实测：并行的两个 tool_use 块流是交错的，按「当前一个」攒入参会让 Write 的
    # 入参被 list_crons 冲掉。assistant 整条消息里的同一批工具不能再报一遍。
    lines = [
        b'{"type":"system","subtype":"init","session_id":"' + SID.encode() + b'"}\n',
        _ev({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "w1", "name": "Write", "input": {}}}),
        _ev({"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "m1", "name": "mcp__cc-lark__list_crons", "input": {}}}),
        _ev({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"file_path\": \"n.txt\","}}),
        _ev({"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{}"}}),
        _ev({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": " \"content\": \"ok\"}"}}),
        _ev({"type": "content_block_stop", "index": 2}),
        _ev({"type": "content_block_stop", "index": 1}),
        (json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "n.txt", "content": "ok"}},
            {"type": "tool_use", "id": "m1", "name": "mcp__cc-lark__list_crons", "input": {}},
            # 流里没出现过的工具（没有 partial 事件）从整条消息补报
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "ls"}},
        ]}, "session_id": SID}) + "\n").encode(),
        b'{"type":"result","subtype":"success","is_error":false,"result":"DONE","session_id":"' + SID.encode() + b'"}\n',
    ]
    _patch_exec(monkeypatch, [FakeProc(lines)], {})

    tools = []
    text, _, _ = asyncio.run(run_qoder(message="x", cwd="/tmp", on_tool_use=lambda n, i: tools.append((n, i))))

    assert text == "DONE"
    assert tools == [
        ("mcp__cc-lark__list_crons", {}),
        ("Write", {"file_path": "n.txt", "content": "ok"}),
        ("Bash", {"command": "ls"}),
    ]


def test_run_qoder_builds_expected_argv(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM_LINES)], captured)
    monkeypatch.delenv("CC_LARK_QODER_DISALLOWED_TOOLS", raising=False)

    asyncio.run(
        run_qoder(
            message="hi",
            model="Qwen3.8-Max",
            cwd="/tmp",
            effort="high",
            append_system_prompt="LARK RULES",
            qoder_bin="/opt/qodercli",
            config_dir="/tmp/qoder-home",
            api_key="pat-123",
        )
    )

    cmd = captured["cmd"]
    assert cmd[0] == "/opt/qodercli"
    assert "-p" in cmd
    assert _flag(cmd, "--output-format") == "stream-json"
    assert "--include-partial-messages" in cmd
    assert _flag(cmd, "--permission-mode") == "bypass_permissions"
    assert _flag(cmd, "-m") == "Qwen3.8-Max"
    assert _flag(cmd, "--reasoning-effort") == "high"
    assert _flag(cmd, "--append-system-prompt") == "LARK RULES"
    assert _flag(cmd, "--config-dir") == "/tmp/qoder-home"
    assert "ScheduleWakeup" in _flag(cmd, "--disallowed-tools")
    # 首轮自己钉 session id，不 resume；prompt 不进 argv
    assert "-r" not in cmd
    assert len(_flag(cmd, "--session-id")) == 36
    assert "hi" not in cmd
    # 没有话题上下文就不注入 cc-lark MCP
    assert "--mcp-config" not in cmd
    assert captured["cwd"] == "/tmp"
    assert captured["env"]["QODER_PERSONAL_ACCESS_TOKEN"] == "pat-123"
    assert captured["env"]["CC_LARK_MIRROR_OFF"] == "1"


def test_run_qoder_injects_cc_lark_mcp_with_thread_env(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM_LINES)], captured)
    monkeypatch.delenv("CC_LARK_WAKE_MCP", raising=False)

    asyncio.run(
        run_qoder(
            message="hi",
            cwd="/tmp",
            extra_env={
                "CC_LARK_THREAD_ID": "omt_1",
                "CC_LARK_CHAT_ID": "oc_1",
                "CC_LARK_PROFILE": "mac",
                "CC_LARK_CONTROL_TOKEN": "secret",
            },
        )
    )

    cfg = json.loads(_flag(captured["cmd"], "--mcp-config"))
    server = cfg["mcpServers"]["cc-lark"]
    assert server["args"][0].endswith("cc_mcp_server.py")
    assert server["env"]["CC_LARK_THREAD_ID"] == "omt_1"
    assert server["env"]["CC_LARK_CHAT_ID"] == "oc_1"
    # 控制 token 不往 MCP 配置里写（和 claude 后端一致）
    assert "CC_LARK_CONTROL_TOKEN" not in server["env"]


def test_run_qoder_resume_uses_same_session(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM_LINES)], captured)

    _, sid, fallback = asyncio.run(run_qoder(message="again", session_id=SID, cwd="/tmp"))

    assert _flag(captured["cmd"], "-r") == SID
    assert "--session-id" not in captured["cmd"]
    assert sid == SID
    assert fallback is False


def test_run_qoder_resume_failure_falls_back_to_fresh(monkeypatch):
    captured = {}
    resume_fail = FakeProc(
        [], returncode=42,
        stderr=b'Error resuming session: Invalid session identifier "x".\n  Searched current project',
    )
    _patch_exec(monkeypatch, [resume_fail, FakeProc(STREAM_LINES)], captured)

    text, sid, fallback = asyncio.run(run_qoder(message="again", session_id="stale-id", cwd="/elsewhere"))

    assert fallback is True
    assert text == "hello"
    assert sid == SID
    first, second = captured["calls"]
    assert _flag(first["cmd"], "-r") == "stale-id"
    assert "-r" not in second["cmd"]
    assert "--session-id" in second["cmd"]


def test_run_qoder_killed_by_signal_does_not_fall_back(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc([], returncode=-15)], captured)

    with pytest.raises(RuntimeError, match="exited with code -15"):
        asyncio.run(run_qoder(message="again", session_id=SID, cwd="/tmp"))
    assert len(captured["calls"]) == 1


def test_run_qoder_result_error_raises_retryable(monkeypatch):
    captured = {}
    lines = [
        b'{"type":"system","subtype":"init","session_id":"' + SID.encode() + b'"}\n',
        b'{"type":"result","subtype":"error_during_execution","is_error":true,"result":"upstream timeout","session_id":"' + SID.encode() + b'"}\n',
    ]
    _patch_exec(monkeypatch, [FakeProc(lines)], captured)

    with pytest.raises(RuntimeError, match="upstream timeout") as ei:
        asyncio.run(run_qoder(message="x", cwd="/tmp"))
    assert ei.value.cc_session_id == SID
    assert ei.value.cc_retryable_resume is True


def test_run_qoder_rejects_unknown_effort():
    with pytest.raises(ValueError, match="invalid Qoder effort"):
        asyncio.run(run_qoder(message="x", effort="turbo"))


def test_permission_mode_maps_claude_names():
    norm = qoder_runner._normalize_permission_mode
    assert norm("bypassPermissions", False) == "bypass_permissions"
    assert norm("acceptEdits", False) == "accept_edits"
    assert norm("plan", False) == "plan"
    assert norm("dont_ask", False) == "dont_ask"
    assert norm("", True) == "bypass_permissions"


def test_usage_keeps_real_token_counts_when_present():
    usage = qoder_runner._usage_from_result({
        "usage": {"input_tokens": 1200, "output_tokens": 30, "context_usage_ratio": 0.2},
        "total_credits": 0.5,
    })
    assert usage == {"input_tokens": 1200, "output_tokens": 30,
                     "_context_ratio": 0.2, "_turn_credits": 0.5}


def test_resolve_qoder_bin_prefers_configured(monkeypatch):
    assert qoder_runner.resolve_qoder_bin("~/bin/qodercli") == os.path.expanduser("~/bin/qodercli")
    monkeypatch.setattr(qoder_runner.shutil, "which", lambda name: "/usr/local/bin/qodercli")
    assert qoder_runner.resolve_qoder_bin("") == "/usr/local/bin/qodercli"
