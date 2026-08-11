import asyncio
import json
import os
import sys

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")
# 这些用例覆盖 `claude --print` 后端（_run_claude_print）的解析逻辑——
# 通过 mock asyncio.create_subprocess_exec 注入伪 stream-json 行。
# 生产默认走 PTY 后端，PTY 路径的测试见 test_claude_pty.py。
os.environ["CLAUDE_RUNNER"] = "print"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_runner import run_claude


class FakeStdin:
    def __init__(self):
        self.buffer = b""
        self.closed = False

    def write(self, data: bytes):
        self.buffer += data

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class FakeStdout:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)
        self._index = 0

    async def readline(self):
        if self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        self._index += 1
        return line


class FakeStderr:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self):
        return self._data


class FakeProc:
    def __init__(self, stdout_lines: list[bytes], stderr: bytes = b"", returncode: int = 0):
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(stdout_lines)
        self.stderr = FakeStderr(stderr)
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


def test_run_claude_prefers_final_result_over_partial_deltas(monkeypatch):
    proc = FakeProc([
        b'{"type":"system","session_id":"sid_123"}\n',
        b'{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}}\n',
        b'{"type":"result","session_id":"sid_123","result":"Hello world"}\n',
    ])

    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    text, session_id, used_fallback = asyncio.run(run_claude("hi"))

    assert text == "Hello world"
    assert session_id == "sid_123"
    assert used_fallback is False
    assert proc.stdin.buffer.endswith(b"hi\n")
    assert proc.stdin.closed is True
    assert captured["kwargs"]["start_new_session"] is True


def test_extra_env_selects_print_backend_over_parent_pty(monkeypatch):
    monkeypatch.setenv("CLAUDE_RUNNER", "pty")
    proc = FakeProc([
        b'{"type":"result","session_id":"sid_print","result":"ok"}\n',
    ])
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    text, session_id, used_fallback = asyncio.run(
        run_claude("hi", extra_env={"CLAUDE_RUNNER": "print"})
    )

    assert text == "ok"
    assert session_id == "sid_print"
    assert used_fallback is False
    assert "--print" in captured["cmd"]


def test_print_backend_injects_cc_lark_mcp(monkeypatch):
    proc = FakeProc([
        b'{"type":"result","session_id":"sid_1","result":"ok"}\n',
    ])
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    asyncio.run(run_claude(
        "hi",
        extra_env={
            "CLAUDE_RUNNER": "print",
            "CC_LARK_THREAD_ID": "omt_1",
            "CC_LARK_MESSAGE_ID": "om_1",
            "CC_LARK_CLI_PROFILE": "work",
            "CC_LARK_CONTROL_TOKEN": "secret-token",
        },
    ))

    cmd = captured["cmd"]
    assert "--disallowedTools" in cmd
    assert "--mcp-config" in cmd
    cfg = json.loads(cmd[cmd.index("--mcp-config") + 1])
    server = cfg["mcpServers"]["cc-lark"]
    assert server["args"][0].endswith("cc_mcp_server.py")
    assert server["env"]["CC_LARK_THREAD_ID"] == "omt_1"
    assert server["env"]["CC_LARK_CLI_PROFILE"] == "work"
    assert "CC_LARK_CONTROL_TOKEN" not in server["env"]


def test_run_claude_returns_partial_output_on_nonzero_exit_with_stderr(monkeypatch):
    """When there's partial output + stderr + nonzero exit, return partial text (don't raise)"""
    proc = FakeProc([
        b'{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"partial"}}}\n',
    ], stderr=b"boom", returncode=1)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    text, session_id, used_fallback = asyncio.run(run_claude("hi"))
    assert text == "partial"
    assert used_fallback is False


def test_run_claude_raises_on_nonzero_exit_without_output(monkeypatch):
    """When there's NO output and nonzero exit, raise RuntimeError"""
    proc = FakeProc([], stderr=b"fatal error", returncode=1)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match=r"fatal error"):
        asyncio.run(run_claude("hi"))


def test_run_claude_retries_without_resume_on_empty_stderr_failure(monkeypatch):
    first = FakeProc([], stderr=b"", returncode=1)
    second = FakeProc([
        b'{"type":"system","session_id":"sid_new"}\n',
        b'{"type":"result","session_id":"sid_new","result":"fresh answer"}\n',
    ])
    procs = iter([first, second])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return next(procs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    text, session_id, used_fallback = asyncio.run(run_claude("hi", session_id="sid_old"))

    assert text == "fresh answer"
    assert session_id == "sid_new"
    assert used_fallback is True
    assert first.stdin.closed is True
    assert second.stdin.closed is True


def test_run_claude_streams_text_chunks_via_callback(monkeypatch):
    """Test that on_text_chunk callback fires for text deltas"""
    proc = FakeProc([
        b'{"type":"system","session_id":"sid_1"}\n',
        b'{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello "}}}\n',
        b'{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"world"}}}\n',
        b'{"type":"result","session_id":"sid_1","result":"Hello world"}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    chunks = []

    async def collect_chunk(chunk):
        chunks.append(chunk)

    text, session_id, _ = asyncio.run(
        run_claude("hi", on_text_chunk=collect_chunk)
    )

    assert chunks == ["Hello ", "world"]
    assert text == "Hello world"


def test_run_claude_fires_tool_use_callback(monkeypatch):
    """Test that on_tool_use callback fires for tool calls"""
    proc = FakeProc([
        b'{"type":"system","session_id":"sid_1"}\n',
        b'{"type":"stream_event","event":{"type":"content_block_start","content_block":{"type":"tool_use","name":"Bash"}}}\n',
        b'{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"{\\"command\\": \\"ls\\"}"}}}\n',
        b'{"type":"stream_event","event":{"type":"content_block_stop"}}\n',
        b'{"type":"result","session_id":"sid_1","result":"done"}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    tool_calls = []

    async def collect_tool(name, inp):
        tool_calls.append((name, inp))

    text, _, _ = asyncio.run(
        run_claude("hi", on_tool_use=collect_tool)
    )

    # Should fire twice: once on block_start (empty input), once on block_stop (full input)
    assert len(tool_calls) == 2
    assert tool_calls[0] == ("Bash", {})
    assert tool_calls[1] == ("Bash", {"command": "ls"})


def test_stalled_result_raises_transient_resumable(monkeypatch):
    """上游流中断的 result 事件（is_error + stalled 文本）应 raise，而不是把错误
    字符串当正常回复返回；异常要带上可 resume 的 session id 且标记为瞬时可恢复。"""
    proc = FakeProc([
        b'{"type":"system","session_id":"sid_stall"}\n',
        b'{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"partial answer"}}}\n',
        b'{"type":"result","subtype":"error_during_execution","is_error":true,'
        b'"api_error_status":null,"session_id":"sid_stall",'
        b'"result":"API Error: Response stalled mid-stream. The response above may be incomplete."}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(run_claude("hi"))

    exc = ei.value
    assert "stalled mid-stream" in str(exc)
    assert getattr(exc, "cc_session_id", None) == "sid_stall"
    assert getattr(exc, "cc_retryable_resume", None) is True


def test_rate_limit_result_raises_non_transient(monkeypatch):
    """用量墙的 result 事件应 raise 但标记为不可瞬时恢复（重试无益，得等配额）。"""
    proc = FakeProc([
        b'{"type":"result","subtype":"error_during_execution","is_error":true,'
        b'"api_error_status":429,"session_id":"sid_rl",'
        b'"result":"rate_limit: You have hit your usage limit."}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(run_claude("hi"))

    exc = ei.value
    assert getattr(exc, "cc_session_id", None) == "sid_rl"
    assert getattr(exc, "cc_retryable_resume", None) is False


def test_connection_closed_result_is_transient_even_when_subtype_success(monkeypatch):
    """线上实拍的另一种中断文案：subtype 仍是 'success'、只有 is_error=true，
    result 里是 "Connection closed mid-response"。必须同样判为可 resume 续跑
    （旧的白名单只认 "stalled mid-stream"，这条会被误判成不可恢复 → 直接中断）。"""
    proc = FakeProc([
        b'{"type":"system","session_id":"sid_cc"}\n',
        b'{"type":"result","subtype":"success","is_error":true,'
        b'"api_error_status":null,"session_id":"sid_cc",'
        b'"result":"API Error: Connection closed mid-response. '
        b'The response above may be incomplete."}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(run_claude("hi"))

    exc = ei.value
    assert getattr(exc, "cc_retryable_resume", None) is True
    assert getattr(exc, "cc_session_id", None) == "sid_cc"
    # 错误卡上不该出现误导性的 "success"
    assert "success" not in str(exc)


def test_auth_error_result_is_not_transient(monkeypatch):
    """认证/请求侧错误（4xx）重试无益，不能标记为可续跑。"""
    proc = FakeProc([
        b'{"type":"result","subtype":"error_during_execution","is_error":true,'
        b'"api_error_status":401,"session_id":"sid_auth",'
        b'"result":"authentication_error: invalid api key"}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(run_claude("hi"))

    assert getattr(ei.value, "cc_retryable_resume", None) is False


def test_process_crash_marked_resumable(monkeypatch):
    """CLI 进程级崩溃（rc>0、无输出、stderr 是网络错）也要带上可 resume 标记，
    否则 dispatcher 只能报错中断。"""
    proc = FakeProc(
        [b'{"type":"system","session_id":"sid_crash"}\n'],
        stderr=b"Error: fetch failed (ECONNRESET)",
        returncode=1,
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(run_claude("hi"))

    exc = ei.value
    assert getattr(exc, "cc_session_id", None) == "sid_crash"
    assert getattr(exc, "cc_retryable_resume", None) is True


def test_process_crash_with_usage_limit_not_resumable(monkeypatch):
    """rc>0 但 stderr 是用量墙：续跑无意义，不标可 resume。"""
    proc = FakeProc(
        [b'{"type":"system","session_id":"sid_rl2"}\n'],
        stderr=b"Error: usage limit reached, resets 9pm",
        returncode=1,
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError) as ei:
        asyncio.run(run_claude("hi"))

    assert getattr(ei.value, "cc_retryable_resume", None) is None


def test_normal_result_with_error_word_not_flagged(monkeypatch):
    """正常成功回复即便文本里含 'server_error' 字样也不能被误判为错误结果。"""
    proc = FakeProc([
        b'{"type":"result","subtype":"success","is_error":false,'
        b'"session_id":"sid_ok","result":"the server_error happens when overloaded"}\n',
    ])

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    text, session_id, _ = asyncio.run(run_claude("hi"))
    assert text == "the server_error happens when overloaded"
    assert session_id == "sid_ok"
