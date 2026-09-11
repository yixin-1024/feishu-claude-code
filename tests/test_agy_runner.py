import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agy_runner
from agy_runner import (
    _build_permission_args,
    _extract_agy_log_error,
    _usage_from_result,
    _normalize_effort,
    _resolve_model_effort,
    ensure_cc_lark_mcp,
    ensure_model_provider,
    run_agy,
)
from claude_runner import is_fatal_error_text


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


CID = "d6774357-e4ff-4afe-a711-caeb9a790bfb"

# 真实 `agy -p ... --output-format stream-json` 的行（gemini-3.8-flash）
STREAM_LINES = [
    json.dumps({
        "event": "init",
        "conversation_id": CID,
        "init": {"model": "gemini-3.8-flash", "cwd": "/tmp", "tools": ["view_file"],
                 "permission_mode": "always-proceed"},
    }).encode() + b"\n",
    json.dumps({
        "event": "step_update",
        "step_update": {"conversation_id": CID, "step_index": 0, "state": "DONE",
                        "step_type": "user_input"},
    }).encode() + b"\n",
    json.dumps({
        "event": "step_update",
        "step_update": {"conversation_id": CID, "step_index": 1, "state": "ACTIVE",
                        "step_type": "tool", "tool_name": "view_file",
                        "tool_info": {"name": "view_file",
                                      "parameters": {"AbsolutePath": "/tmp/sample.txt"}}},
    }).encode() + b"\n",
    json.dumps({
        "event": "step_update",
        "step_update": {"conversation_id": CID, "step_index": 1, "state": "DONE",
                        "step_type": "tool", "tool_name": "view_file",
                        "tool_info": {"name": "view_file",
                                      "parameters": {"AbsolutePath": "/tmp/sample.txt"},
                                      "output": "1 lines"}},
    }).encode() + b"\n",
    json.dumps({
        "event": "step_update",
        "step_update": {"conversation_id": CID, "step_index": 2, "state": "ACTIVE",
                        "step_type": "agent_response", "text_delta": "hello "},
    }).encode() + b"\n",
    json.dumps({
        "event": "step_update",
        "step_update": {"conversation_id": CID, "step_index": 2, "state": "DONE",
                        "step_type": "agent_response", "text_delta": "world"},
    }).encode() + b"\n",
    json.dumps({
        "event": "result",
        "result": {"conversation_id": CID, "status": "SUCCESS", "response": "hello world",
                   "duration_seconds": 1.5, "num_turns": 1,
                   "usage": {"input_tokens": 9143, "output_tokens": 12, "thinking_tokens": 30,
                             "cache_read_tokens": 8198, "total_tokens": 9185}},
    }).encode() + b"\n",
]

ERROR_LINES = [
    json.dumps({
        "event": "result",
        "result": {"conversation_id": "", "status": "ERROR", "response": "",
                   "error": "Eligibility check failed: Your current account is not eligible.",
                   "usage": {}},
    }).encode() + b"\n",
]


def _patch_exec(monkeypatch, proc, captured):
    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    # settings.json / mcp_config.json 是真实家目录里的文件，测试里别碰
    monkeypatch.setattr(agy_runner, "ensure_model_provider", lambda *a, **k: False)
    monkeypatch.setattr(agy_runner, "ensure_cc_lark_mcp", lambda *a, **k: False)
    monkeypatch.setattr(agy_runner, "ensure_bot_home", lambda *a, **k: False)


def test_run_agy_streams_text_tools_and_usage(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, FakeProc(STREAM_LINES), captured)

    chunks, tools, usages = [], [], []
    text, sid, fallback = asyncio.run(
        run_agy(
            message="读 sample.txt",
            model="gemini-3.8-flash",
            cwd="/tmp",
            on_text_chunk=chunks.append,
            on_tool_use=lambda name, inp: tools.append((name, inp)),
            on_usage=usages.append,
            api_key="AIza-test",
        )
    )

    assert text == "hello world"
    assert sid == CID
    assert fallback is False
    # text_delta 是增量，拼起来就是全文
    assert "".join(chunks) == "hello world"
    # 每个工具报两次：ACTIVE（真实入参）+ DONE（多带 _agy_state 让卡片打 ✅）
    assert tools == [
        ("view_file", {"AbsolutePath": "/tmp/sample.txt"}),
        ("view_file", {"AbsolutePath": "/tmp/sample.txt", "_agy_state": "DONE"}),
    ]
    assert usages[-1]["input_tokens"] == 9143
    assert usages[-1]["cache_read_input_tokens"] == 8198
    assert usages[-1]["_context_window"] == 1_000_000


def test_run_agy_builds_expected_argv_and_env(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, FakeProc(STREAM_LINES), captured)

    asyncio.run(
        run_agy(
            message="hi",
            session_id=CID,
            model="gemini-3.8-flash",
            effort="max",  # 七档里的 max → agy 的 high
            cwd="/tmp",
            append_system_prompt="LARK RULES",
            api_key="AIza-test",
            proxy="http://127.0.0.1:7899",
        )
    )

    cmd = captured["cmd"]
    assert cmd[1:3] == ["--output-format", "stream-json"]
    assert cmd[cmd.index("--print-timeout") + 1] == "24h"
    assert "--dangerously-skip-permissions" in cmd
    # 不给 --add-dir 的话 run_command 会跑在 agy 自己的 scratch 目录里
    assert cmd[cmd.index("--add-dir") + 1] == "/tmp"
    assert cmd[cmd.index("--conversation") + 1] == CID
    assert cmd[cmd.index("--model") + 1] == "gemini-3.8-flash"
    assert cmd[cmd.index("--effort") + 1] == "high"
    # prompt 必须是最后一段（-p 吃紧随其后的 token）
    assert cmd[-2] == "-p"
    assert cmd[-1] == "LARK RULES\n\nhi"

    env = captured["env"]
    assert env["GEMINI_API_KEY"] == "AIza-test"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7899"
    # cc_mcp_server 回连 bot 的 127.0.0.1 control API 不能走代理
    assert "127.0.0.1" in env["NO_PROXY"]
    assert env["CC_LARK_MIRROR_OFF"] == "1"


def test_run_agy_raises_on_error_result(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, FakeProc(ERROR_LINES), captured)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(run_agy(message="hi", model="gemini-3.8-flash", cwd="/tmp"))
    assert "Eligibility check failed" in str(exc.value)


def test_resolve_model_effort(monkeypatch):
    monkeypatch.delenv("AGY_EFFORT", raising=False)
    # 带档位后缀的 id 原样传，不再给 --effort
    assert _resolve_model_effort("gemini-3.1-pro-high", "low") == ("gemini-3.1-pro-high", None)
    # 裸名必须配 effort，没给就 high
    assert _resolve_model_effort("gemini-3.8-flash", None) == ("gemini-3.8-flash", "high")
    assert _resolve_model_effort("gemini-3.8-flash", "low") == ("gemini-3.8-flash", "low")
    assert _resolve_model_effort("", "high") == (None, "high")
    # AGY_EFFORT 环境变量覆盖默认值
    monkeypatch.setenv("AGY_EFFORT", "low")
    assert _resolve_model_effort("gemini-3.8-flash", None) == ("gemini-3.8-flash", "low")
    # 非 agy 模型（如误传 opus / fable）自动安全回退为 gemini-3.8-flash
    assert _resolve_model_effort("opus[1m]", "high") == ("gemini-3.8-flash", "high")
    assert _resolve_model_effort("opus", "high") == ("gemini-3.8-flash", "high")
    assert _resolve_model_effort("fable", None) == ("gemini-3.8-flash", "low")


def test_normalize_effort_collapses_seven_levels():
    assert _normalize_effort("none") == "low"
    assert _normalize_effort("minimal") == "low"
    assert _normalize_effort("xhigh") == "high"
    assert _normalize_effort("max") == "high"
    assert _normalize_effort("") is None
    with pytest.raises(ValueError):
        _normalize_effort("turbo")


def test_permission_args():
    assert _build_permission_args("plan", True) == ["--mode", "plan"]
    assert _build_permission_args("acceptEdits", True) == ["--mode", "accept-edits"]
    assert _build_permission_args("bypassPermissions", False) == ["--dangerously-skip-permissions"]
    # 无人值守：任何"要问一句"的模式都退到自动放行，否则会挂到 print-timeout
    assert _build_permission_args("default", False) == ["--dangerously-skip-permissions"]


def test_ensure_model_provider_is_idempotent(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"telemetry": false}\n', encoding="utf-8")
    assert ensure_model_provider("gemini", str(path)) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"telemetry": False, "modelProvider": "gemini"}
    assert ensure_model_provider("gemini", str(path)) is False


def test_ensure_cc_lark_mcp_keeps_user_entry(tmp_path):
    path = tmp_path / "mcp_config.json"
    assert ensure_cc_lark_mcp(str(path)) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["cc-lark"]["args"][0].endswith("cc_mcp_server.py")
    # 已存在就不动（尊重用户手改）
    data["mcpServers"]["cc-lark"]["command"] = "/custom/python"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert ensure_cc_lark_mcp(str(path)) is False
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["cc-lark"]["command"] == "/custom/python"


# 真实抓到的一轮（7 次工具调用）的 usage 形状：每个 agent_response 步带自己那次
# 请求的 usage，result 里是**全部步骤的累加**。
_REAL_STEP_USAGES = [
    {"input_tokens": 20310, "output_tokens": 57, "cache_read_tokens": 0, "total_tokens": 20367},
    {"input_tokens": 4238, "output_tokens": 42, "cache_read_tokens": 16303, "total_tokens": 4280},
    {"input_tokens": 5526, "output_tokens": 1, "cache_read_tokens": 16263, "total_tokens": 5527},
]
_REAL_RESULT_USAGE = {
    "input_tokens": 54235, "output_tokens": 446,
    "cache_read_tokens": 113975, "total_tokens": 54681,
}


def test_usage_takes_context_from_last_step_not_cumulative_result():
    """回归：result.usage 是本轮所有内部请求的累加，不能当上下文。

    2026-09-03 用户报「上下文 528.7k / 1M (52.9%)」，实际这一轮只读了两个
    package.json —— 数字是 7 次工具调用各自完整请求的 token 之和。
    """
    usage = _usage_from_result({"usage": _REAL_RESULT_USAGE}, _REAL_STEP_USAGES[-1])
    # 上下文 = 最后一步（单步 input 不含 cache_read，要加回来）
    assert usage["input_tokens"] == 5526
    assert usage["cache_read_input_tokens"] == 16263
    assert usage["output_tokens"] == 1
    assert usage["_context_window"] == 1_000_000
    # 累计消耗单独挂着，不参与上下文百分比
    assert usage["_turn_tokens"] == 54235 + 113975 + 446


def test_usage_falls_back_to_result_when_no_step_usage():
    usage = _usage_from_result({"usage": _REAL_RESULT_USAGE}, None)
    assert usage["input_tokens"] == 54235
    assert usage["cache_read_input_tokens"] == 113975


def test_result_usage_really_is_the_sum_of_steps():
    """把「result = 各步之和」这条事实钉在测试里，agy 哪天改语义会立刻挂。"""
    assert sum(u["input_tokens"] for u in _REAL_STEP_USAGES) < _REAL_RESULT_USAGE["input_tokens"]
    # 抓到的完整 8 步之和恰好等于 result（这里只留了 3 步样本，故用 < 断言方向）
    assert _REAL_RESULT_USAGE["total_tokens"] == (
        _REAL_RESULT_USAGE["input_tokens"] + _REAL_RESULT_USAGE["output_tokens"]
    )
    # 单步 total 也是 input + output —— 说明 cache_read 是**额外**计的，不含在 input 里
    for u in _REAL_STEP_USAGES:
        assert u["total_tokens"] == u["input_tokens"] + u["output_tokens"]


def test_extract_agy_log_error_structured_503(tmp_path):
    log_file = tmp_path / "cli.log"
    log_file.write_text(
        "ERROR: logging before google.Init: E0904 20:23:23.782903 555 errorreport.go:224] "
        "agent executor error: calling model: Error 503, "
        "Message: This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later., "
        "Status: UNAVAILABLE, Details: []\n"
    )
    extracted = _extract_agy_log_error(str(log_file))
    assert extracted.startswith("Error 503 (UNAVAILABLE):")
    assert "This model is currently experiencing high demand" in extracted


def test_extract_agy_log_error_429_resource_exhausted(tmp_path):
    log_file = tmp_path / "cli.log"
    log_file.write_text(
        "E0904 12:00:00.123456 1 errorreport.go:224] calling model: Error 429, "
        "Message: Quota exceeded for quota metric 'Generate Content API requests'..., "
        "Status: RESOURCE_EXHAUSTED, Details: []\n"
    )
    extracted = _extract_agy_log_error(str(log_file))
    assert extracted.startswith("Error 429 (RESOURCE_EXHAUSTED):")
    assert "Quota exceeded" in extracted


def test_extract_agy_log_error_529_overloaded(tmp_path):
    log_file = tmp_path / "cli.log"
    log_file.write_text(
        "E0904 12:00:00.123456 1 errorreport.go:224] calling model: Error 529, "
        "Message: Server overloaded, Status: UNAVAILABLE\n"
    )
    extracted = _extract_agy_log_error(str(log_file))
    assert extracted.startswith("Error 529 (UNAVAILABLE):")
    assert "Server overloaded" in extracted


def test_extract_agy_log_error_400_invalid_key(tmp_path):
    log_file = tmp_path / "cli.log"
    log_file.write_text(
        "E0904 21:02:38.710528 9 errorreport.go:224] agent executor error: calling model: Error 400, "
        "Message: API key not valid. Please pass a valid API key., Status: INVALID_ARGUMENT, "
        "Details: [map[@type:type.googleapis.com/google.rpc.ErrorInfo reason:API_KEY_INVALID]]\n"
    )
    extracted = _extract_agy_log_error(str(log_file))
    assert extracted == "Error 400 (INVALID_ARGUMENT): API key not valid. Please pass a valid API key."


def test_extract_agy_log_error_rst_stream():
    extracted = _extract_agy_log_error(
        stderr_text="Received a RST_STREAM with error code 2 (INTERNAL_ERROR)"
    )
    assert "RST_STREAM" in extracted


def test_extract_agy_log_error_stderr_fallback():
    extracted = _extract_agy_log_error(
        stderr_text="warning: conversation fallback\nfatal: failed to initialize local state"
    )
    assert extracted == "fatal: failed to initialize local state"


def test_run_agy_includes_raw_error_from_log(monkeypatch, tmp_path):
    captured = {}
    error_event = json.dumps({
        "event": "result",
        "result": {
            "status": "ERROR",
            "error": "Agent execution terminated due to error.",
            "conversation_id": "test-cid-123",
        },
    }).encode("utf-8") + b"\n"

    _patch_exec(monkeypatch, FakeProc([error_event]), captured)

    # 模拟在 _extract_agy_log_error 命中 503
    monkeypatch.setattr(
        agy_runner,
        "_extract_agy_log_error",
        lambda *args, **kwargs: "Error 503 (UNAVAILABLE): Model high demand",
    )

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(run_agy(message="hi", cwd="/tmp"))
    err_msg = str(exc.value)
    assert "上游原始错误: Error 503 (UNAVAILABLE): Model high demand" in err_msg
    assert exc.value.cc_session_id == "test-cid-123"
    assert exc.value.cc_retryable_resume is True


def test_run_agy_interrupted_stream_diagnostic(monkeypatch):
    captured = {}
    interrupted_event = json.dumps({
        "event": "result",
        "result": {
            "status": "ERROR",
            "error": "The stream was interrupted. Please continue the task you were working on.",
            "conversation_id": "test-cid-456",
        },
    }).encode("utf-8") + b"\n"

    _patch_exec(monkeypatch, FakeProc([interrupted_event]), captured)
    monkeypatch.setattr(
        agy_runner,
        "_extract_agy_log_error",
        lambda *args, **kwargs: "",
    )

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(run_agy(message="hi", cwd="/tmp"))
    err_msg = str(exc.value)
    assert "上游连接流式中断" in err_msg
    assert exc.value.cc_session_id == "test-cid-456"


def test_run_agy_success_with_interrupted_stream_text_in_response_does_not_raise(monkeypatch):
    captured = {}
    explanation = (
        "This error message occurs when the stream was interrupted during execution. "
        "The upstream connection closed unexpectedly because the session context was too large. "
        "To resolve it, please reset the session and continue with fresh context."
    )
    success_event = json.dumps({
        "event": "result",
        "result": {
            "status": "SUCCESS",
            "response": explanation,
            "conversation_id": "test-cid-789",
        },
    }).encode("utf-8") + b"\n"

    _patch_exec(monkeypatch, FakeProc([success_event]), captured)
    monkeypatch.setattr(
        agy_runner,
        "_extract_agy_log_error",
        lambda *args, **kwargs: "",
    )

    text, cid, used_fresh = asyncio.run(run_agy(message="why this error", cwd="/tmp"))
    assert text == explanation
    assert cid == "test-cid-789"
    assert used_fresh is False


def test_is_fatal_error_text_recognizes_resource_exhausted_and_429():
    assert is_fatal_error_text("Error 429 (RESOURCE_EXHAUSTED): Quota exceeded") is True
    assert is_fatal_error_text("Resource has been exhausted") is True
    assert is_fatal_error_text("Error 429: Too Many Requests") is True
    # 503/529 不属于 fatal，应可重试
    assert is_fatal_error_text("Error 503 (UNAVAILABLE): Model high demand") is False
    assert is_fatal_error_text("Error 529 (UNAVAILABLE): Model overloaded") is False


def test_run_agy_stale_history_error_contamination_suppressed(monkeypatch):
    """验证 agy CLI 历史步骤扫描污染时，若本轮 agent_response 已经 DONE 且 exit_code=0，应忽略假错误正常返回"""
    captured = {}
    step_done_event = json.dumps({
        "event": "step_update",
        "step_update": {
            "conversation_id": "test-cid-probe",
            "step_index": 876,
            "state": "DONE",
            "step_type": "agent_response",
            "text_delta": "AGY_PROBE_OK\n",
        },
    }).encode("utf-8") + b"\n"

    stale_error_result_event = json.dumps({
        "event": "result",
        "result": {
            "conversation_id": "test-cid-probe",
            "status": "ERROR",
            "response": "AGY_PROBE_OK\n",
            "error": "The stream was interrupted. Please continue the task you were working on.",
        },
    }).encode("utf-8") + b"\n"

    fake_proc = FakeProc([step_done_event, stale_error_result_event])
    fake_proc.returncode = 0
    _patch_exec(monkeypatch, fake_proc, captured)
    monkeypatch.setattr(
        agy_runner,
        "_extract_agy_log_error",
        lambda *args, **kwargs: "",
    )

    text, cid, used_fresh = asyncio.run(run_agy(message="probe", cwd="/tmp"))
    assert text == "AGY_PROBE_OK"
    assert cid == "test-cid-probe"
    assert used_fresh is False


def test_run_agy_stale_user_location_contamination_suppressed(monkeypatch):
    """验证会话历史中曾出现 400 User location 假错误时，新一轮正常生成完毕不应被历史错误污染拖入重试循环"""
    captured = {}
    step_done_event = json.dumps({
        "event": "step_update",
        "step_update": {
            "conversation_id": "test-cid-loc",
            "step_index": 999,
            "state": "DONE",
            "step_type": "agent_response",
            "text_delta": "FINAL_ANSWER_OK\n",
        },
    }).encode("utf-8") + b"\n"

    stale_error_result_event = json.dumps({
        "event": "result",
        "result": {
            "conversation_id": "test-cid-loc",
            "status": "ERROR",
            "response": "FINAL_ANSWER_OK\n",
            "error": "calling model: FAILED_PRECONDITION (code 400): User location is not supported for the API use.",
        },
    }).encode("utf-8") + b"\n"

    fake_proc = FakeProc([step_done_event, stale_error_result_event])
    fake_proc.returncode = 0
    _patch_exec(monkeypatch, fake_proc, captured)
    monkeypatch.setattr(
        agy_runner,
        "_extract_agy_log_error",
        lambda *args, **kwargs: "",
    )

    text, cid, used_fresh = asyncio.run(run_agy(message="probe", cwd="/tmp"))
    assert text == "FINAL_ANSWER_OK"
    assert cid == "test-cid-loc"
    assert used_fresh is False



def test_extract_agy_log_error_unexpected_eof():
    """验证从日志中提取 unexpected EOF 原始网络错误"""
    sample_log = (
        "2026-09-05T13:05:29.123Z [agent] Run: attempt 1 failed "
        "(doRequest: error sending request: Post \"https://generativelanguage.googleapis.com/.../streamGenerateContent\": unexpected EOF), retrying in 1s\n"
    )
    extracted = _extract_agy_log_error(stderr_text=sample_log)
    assert "unexpected EOF" in extracted


# ── bot 专用 HOME 隔离（2026-09-07）────────────────────────────────────
# agy 只认全局 settings.json 里的 modelProvider（没有 flag/env 能覆盖），
# 而 runner 每轮都要写它。不隔离的话 bot 一跑，用户在终端手动登录的 OAuth
# 会话就被打回 API key 模式。


def test_bot_home_isolates_settings_from_user_gemini():
    user_settings = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")
    if agy_runner.AGY_BOT_HOME:
        assert agy_runner.AGY_SETTINGS_PATH != user_settings
        for path in (
            agy_runner.AGY_HOME,
            agy_runner.AGY_SETTINGS_PATH,
            agy_runner.AGY_MCP_CONFIG_PATH,
            agy_runner.AGY_SKILLS_LINK,
        ):
            assert path.startswith(agy_runner.AGY_BOT_HOME)
    else:
        assert agy_runner.AGY_SETTINGS_PATH == user_settings


def test_run_agy_sets_home_to_bot_home(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, FakeProc(STREAM_LINES), captured)

    asyncio.run(run_agy(message="hi", cwd="/tmp", api_key="AIza-test"))

    env = captured["env"]
    if agy_runner.AGY_BOT_HOME:
        assert env["HOME"] == agy_runner.AGY_BOT_HOME
    else:
        assert env.get("HOME") == os.environ.get("HOME")


def test_ensure_bot_home_creates_dirs_and_skill_link(monkeypatch, tmp_path):
    home = tmp_path / "agy-home"
    groot = home / ".gemini"
    src = tmp_path / "claude-skills"
    (src / "demo").mkdir(parents=True)
    monkeypatch.setattr(agy_runner, "AGY_BOT_HOME", str(home))
    monkeypatch.setattr(agy_runner, "AGY_HOME", str(groot / "antigravity-cli"))
    monkeypatch.setattr(
        agy_runner, "AGY_MCP_CONFIG_PATH", str(groot / "config" / "mcp_config.json")
    )
    monkeypatch.setattr(agy_runner, "AGY_SKILLS_LINK", str(groot / "config" / "skills"))
    monkeypatch.setattr(agy_runner, "AGY_SKILLS_SRC", str(src))

    assert agy_runner.ensure_bot_home() is True
    assert (groot / "antigravity-cli").is_dir()
    assert (groot / "config" / "skills").is_symlink()
    # skill 索引必须真能穿透软链看到内容，否则 agy 后端会丢掉全部 skill
    assert (groot / "config" / "skills" / "demo").is_dir()
    assert agy_runner.ensure_bot_home() is False  # 幂等


def test_ensure_bot_home_noop_when_disabled(monkeypatch):
    # AGY_BOT_HOME= 是逃生开关：回退到与用户共用 ~/.gemini，且不动文件系统
    monkeypatch.setattr(agy_runner, "AGY_BOT_HOME", "")
    assert agy_runner.ensure_bot_home() is False


def test_run_agy_skips_bot_home_when_going_oauth(monkeypatch):
    """provider 为空(走 OAuth) + 启用隔离 = 致命组合：隔离 HOME 里没有 OAuth
    凭证（凭证绑 HOME，keychain 不共享），agy 会卡 60s 等浏览器授权再失败。
    这种组合必须退回真 HOME。"""
    captured = {}
    _patch_exec(monkeypatch, FakeProc(STREAM_LINES), captured)
    monkeypatch.setattr(agy_runner, "AGY_BOT_HOME", "/tmp/fake-agy-home")

    asyncio.run(run_agy(message="hi", cwd="/tmp", model_provider=""))

    assert captured["env"].get("HOME") == os.environ.get("HOME")


def test_run_agy_uses_bot_home_when_going_api_key(monkeypatch):
    """反过来：provider 非空(走 API key) 时隔离必须生效。"""
    captured = {}
    _patch_exec(monkeypatch, FakeProc(STREAM_LINES), captured)
    monkeypatch.setattr(agy_runner, "AGY_BOT_HOME", "/tmp/fake-agy-home")

    asyncio.run(run_agy(message="hi", cwd="/tmp", model_provider="gemini"))

    assert captured["env"]["HOME"] == "/tmp/fake-agy-home"


def test_run_agy_skips_api_key_when_going_oauth(monkeypatch):
    """provider 为空(走 OAuth) 时不该注入 GEMINI_API_KEY。

    与 AGY_BOT_HOME 那支共用同一条闸门：两个开关必须一起走。provider 为空却
    仍注入 key，排查时会误判成「在跑 API key 模式」，而实际走的是 OAuth。
    """
    captured = {}
    _patch_exec(monkeypatch, FakeProc(STREAM_LINES), captured)

    asyncio.run(
        run_agy(message="hi", cwd="/tmp", api_key="AIza-test", model_provider="")
    )

    assert "GEMINI_API_KEY" not in captured["env"]


def test_run_agy_injects_api_key_when_going_api_key(monkeypatch):
    """反过来：provider 非空(走 API key) 时 key 必须注入。"""
    captured = {}
    _patch_exec(monkeypatch, FakeProc(STREAM_LINES), captured)

    asyncio.run(
        run_agy(message="hi", cwd="/tmp", api_key="AIza-test", model_provider="gemini")
    )

    assert captured["env"]["GEMINI_API_KEY"] == "AIza-test"


def test_run_agy_respects_custom_api_key_env_name(monkeypatch):
    """自定义 AGY_API_KEY_ENV 时也要走同一条 provider 闸门。"""
    captured = {}
    _patch_exec(monkeypatch, FakeProc(STREAM_LINES), captured)

    asyncio.run(
        run_agy(
            message="hi",
            cwd="/tmp",
            api_key="AIza-test",
            api_key_env="GOOGLE_API_KEY",
            model_provider="",
        )
    )

    assert "GOOGLE_API_KEY" not in captured["env"]
    assert "GEMINI_API_KEY" not in captured["env"]


# ── 上游随机抽风的就地重试 ──────────────────────────────────────────────
# 见 agy_runner 尾部长注释：daily-cloudcode-pa 会随机对约半数请求回
# 400 "User location is not supported"，与地区/额度/鉴权都无关。

def _fake_once(monkeypatch, outcomes):
    """按 outcomes 顺序返回结果或抛异常；记录每次收到的 kwargs。"""
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        item = outcomes[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(agy_runner, "_run_agy_once", fake)
    monkeypatch.setattr(agy_runner, "_AGY_FLAKY_RETRY_DELAY", 0)
    return calls


def _location_error(session_id=None):
    exc = RuntimeError(
        "agy 执行出错：Agent execution terminated due to error. (上游原始错误: "
        "calling model: FAILED_PRECONDITION (code 400): User location is not "
        "supported for the API use.)"
    )
    exc.cc_session_id = session_id
    return exc


def test_is_flaky_upstream_error_matches_location_400():
    assert agy_runner.is_flaky_upstream_error(str(_location_error()))
    # 真正该交回 dispatcher 的错误不能被误判成抽风
    assert not agy_runner.is_flaky_upstream_error("RESOURCE_EXHAUSTED (429)")
    assert not agy_runner.is_flaky_upstream_error("invalid model selection")
    assert not agy_runner.is_flaky_upstream_error("")


def test_run_agy_retries_flaky_location_error_then_succeeds(monkeypatch):
    calls = _fake_once(
        monkeypatch, [_location_error(), _location_error(), ("pong", CID, False)]
    )

    text, cid, _ = asyncio.run(run_agy(message="hi", cwd="/tmp"))

    assert text == "pong"
    assert len(calls) == 3  # 两次抽风 + 一次成功


def test_run_agy_does_not_retry_non_flaky_error(monkeypatch):
    calls = _fake_once(monkeypatch, [RuntimeError("agy 执行出错：invalid model selection")])

    with pytest.raises(RuntimeError, match="invalid model selection"):
        asyncio.run(run_agy(message="hi", cwd="/tmp"))

    assert len(calls) == 1  # 一次都不该重试


def test_run_agy_gives_up_after_retry_budget(monkeypatch):
    monkeypatch.setenv("AGY_FLAKY_RETRY_MAX", "2")
    calls = _fake_once(monkeypatch, [_location_error() for _ in range(5)])

    with pytest.raises(RuntimeError, match="User location"):
        asyncio.run(run_agy(message="hi", cwd="/tmp"))

    assert len(calls) == 3  # 首次 + 2 次重试后放弃，交回 dispatcher


def test_run_agy_resumes_same_conversation_on_retry(monkeypatch):
    """重试必须续用上一次建好的 conversation，否则本轮已写入的用户消息/上下文
    会被丢掉，等于每次抽风都重开一个空会话。"""
    calls = _fake_once(
        monkeypatch, [_location_error(session_id="conv-abc"), ("pong", "conv-abc", False)]
    )

    asyncio.run(run_agy(message="hi", cwd="/tmp", session_id=None))

    assert calls[0]["session_id"] is None
    assert calls[1]["session_id"] == "conv-abc"


def test_run_agy_flaky_retry_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AGY_FLAKY_RETRY_MAX", "0")
    calls = _fake_once(monkeypatch, [_location_error()])

    with pytest.raises(RuntimeError):
        asyncio.run(run_agy(message="hi", cwd="/tmp"))

    assert len(calls) == 1


def test_extract_agy_log_error_ignores_startup_not_logged_in_noise(tmp_path):
    """agy 启动早期 keyring 没加载完会刷 `You are not logged into Antigravity.`，
    随后 OAuth 就认证成功了。取日志里第一条 errorreport 行会稳定抓到这句假错误，
    把上游的 400 User location 误报成「没登录」——2026-09-10 线上故障就是被这个
    带偏的（用户被引导去重新登录，实际登录一直是好的）。
    下面是那次真实日志的行序。"""
    log = tmp_path / "run.log"
    log.write_text(
        "I0910 00:19:29.7 keyring.go:64] keyringAuth: loading token\n"
        "W0910 00:19:29.7 cache.go:135] Cache(userInfo): Singleflight refresh failed: "
        "failed to get load code assist response: error getting token source: "
        "You are not logged into Antigravity.\n"
        "E0910 00:19:29.7 errorreport.go:224] error getting token source: "
        "You are not logged into Antigravity.\n"
        "I0910 00:19:29.8 server_oauth.go:197] OAuth: authenticated successfully as x@y.com\n"
        "E0910 00:19:09.3 errorreport.go:224] agent executor error: calling model: "
        "FAILED_PRECONDITION (code 400): User location is not supported for the API use.\n",
        encoding="utf-8",
    )

    got = agy_runner._extract_agy_log_error(log_file=str(log))

    assert "User location is not supported" in got
    assert "not logged into Antigravity" not in got
    # 并且这条要能被就地重试识别出来（否则重试逻辑等于没接上）
    assert agy_runner.is_flaky_upstream_error(got)


def test_extract_agy_log_error_still_reports_real_auth_failure(tmp_path):
    """反向保护：真的只有鉴权错误、没有 executor error 时，不能因为过滤噪声
    就把错误吞成空——那会让用户拿到一句没有信息量的失败。"""
    log = tmp_path / "run.log"
    log.write_text(
        "E0910 00:19:29.7 errorreport.go:224] error getting token source: "
        "You are not logged into Antigravity.\n",
        encoding="utf-8",
    )

    got = agy_runner._extract_agy_log_error(
        log_file=str(log), stderr_text="error: not authenticated"
    )

    assert got  # 必须还有东西可报


# ── agy 内置的第三方模型（Claude Opus/Sonnet 4.6、GPT-OSS）────────────────
# `agy models` 实测：它们没有 -high/-medium/-low 档位变体，传 --effort 会报
# `--effort is not supported for model "..."` 且 **exit code 仍是 0**。

def test_third_party_models_get_no_effort():
    """核心：给这些模型拼 --effort 会让整轮静默失败（exit 0 + 无回答）。"""
    for alias, expect in (
        ("agy-opus", "claude-opus-4-6-thinking"),
        ("agy-sonnet", "claude-sonnet-4-6"),
        ("agy-gpt", "gpt-oss-120b-medium"),
    ):
        model, effort = agy_runner._resolve_model_effort(alias, "high")
        assert model == expect, alias
        assert effort is None, f"{alias} 不该带 effort"


def test_gemini_models_still_get_effort():
    """反向保护：Gemini 裸名**必须**带 --effort，否则 invalid model selection。"""
    assert agy_runner._resolve_model_effort("agy-38", "high") == (
        "gemini-3.8-flash",
        "high",
    )
    # 带档位后缀的仍然不重复给 effort
    assert agy_runner._resolve_model_effort("agy-pro", "high") == (
        "gemini-3.1-pro-high",
        None,
    )


def test_model_ignores_effort_accepts_alias_and_prefix():
    assert agy_runner.model_ignores_effort("agy-opus")
    assert agy_runner.model_ignores_effort("claude-opus-4-6-thinking")
    assert agy_runner.model_ignores_effort("CLAUDE-OPUS-4-6-THINKING")
    assert not agy_runner.model_ignores_effort("agy-38")
    assert not agy_runner.model_ignores_effort("gemini-3.8-flash")
    assert not agy_runner.model_ignores_effort("")
    assert not agy_runner.model_ignores_effort(None)


def test_third_party_models_not_downgraded_to_gemini():
    """它们必须通过 agy 的兼容性校验，否则会被 _resolve 静默回退成 gemini。"""
    from bot_config import is_model_compatible_with_runner

    for m in ("claude-opus-4-6-thinking", "claude-sonnet-4-6", "gpt-oss-120b-medium"):
        assert is_model_compatible_with_runner(m, "agy"), m
    # 而 Claude Code 自己的模型仍然不能注入 agy
    assert not is_model_compatible_with_runner("opus", "agy")
    assert not is_model_compatible_with_runner("claude-opus-5", "agy")


def test_agy_third_party_models_rejected_by_other_runners():
    """反向：agy 专属 id 不能被 claude / codex 误放行（前缀匹配的陷阱）。"""
    from bot_config import is_model_compatible_with_runner

    assert not is_model_compatible_with_runner("claude-opus-4-6-thinking", "claude")
    assert not is_model_compatible_with_runner("agy-opus", "claude")
    assert not is_model_compatible_with_runner("gpt-oss-120b-medium", "codex")
    assert not is_model_compatible_with_runner("agy-gpt", "codex")
    # 没被误伤：Claude Code 真正的模型仍然放行
    assert is_model_compatible_with_runner("opus", "claude")
    assert is_model_compatible_with_runner("gpt-5.6-sol", "codex")


def test_context_window_per_model():
    """第三方模型窗口比 Gemini 小得多，用 1M 会让 footer 百分比严重失真。"""
    assert agy_runner._context_window_for("gemini-3.8-flash") == 1_000_000
    assert agy_runner._context_window_for("claude-opus-4-6-thinking") == 200_000
    assert agy_runner._context_window_for("gpt-oss-120b-medium") == 128_000
    assert agy_runner._context_window_for(None) == 1_000_000


def test_usage_carries_model_specific_window():
    usage = _usage_from_result(
        {"usage": _REAL_RESULT_USAGE},
        _REAL_STEP_USAGES[-1],
        "claude-opus-4-6-thinking",
    )
    assert usage["_context_window"] == 200_000
    # 不传 model 时保持原行为（Gemini 1M）
    usage2 = _usage_from_result({"usage": _REAL_RESULT_USAGE}, _REAL_STEP_USAGES[-1])
    assert usage2["_context_window"] == 1_000_000


def test_run_agy_omits_effort_flag_for_third_party_model(monkeypatch):
    """端到端到 argv：--effort 绝不能出现在 claude-opus 的命令行里。"""
    captured = {}
    _patch_exec(monkeypatch, FakeProc(STREAM_LINES), captured)

    asyncio.run(run_agy(message="hi", cwd="/tmp", model="agy-opus", effort="high"))

    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-6-thinking"
    assert "--effort" not in cmd


def test_claude_sonnet_4_6_stays_valid_for_claude_runner():
    """回归锁：`claude-sonnet-4-6` 在 agy 和 Claude Code 里各有一个同名模型，
    两边都合法。把它当成「agy 专属」从 claude runner 排除掉，会让 Claude 会话里
    /model claude-sonnet-4-6 静默回退成默认模型（改 AGY_ONLY_MODELS 时踩过）。"""
    from bot_config import is_model_compatible_with_runner

    assert is_model_compatible_with_runner("claude-sonnet-4-6", "claude")
    assert is_model_compatible_with_runner("claude-sonnet-4-6", "agy")
