"""pi_runner：JSON 事件流解析、argv、会话续轮、MCP 扩展注入、出错回显。"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pi_runner
from pi_runner import run_pi


class FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


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
        self._final = returncode
        self.pid = 4242

    async def wait(self):
        self.returncode = self._final
        return self.returncode

    def kill(self):
        self.returncode = -9


SID = "01a1f7e9-308c-74f5-ba57-8ef20daba43f"


def _j(obj):
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


def _usage(inp, out, cache_read=0, cost=0.001):
    return {"input": inp, "output": out, "cacheRead": cache_read, "cacheWrite": 0,
            "totalTokens": inp + out + cache_read, "cost": {"total": cost}}


def _delta(text):
    return _j({"type": "message_update", "usage": {},
               "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": text}})


# 照 pi 1.5.2 `--mode json` 实测输出裁剪：先说一句、调 bash、调 cc-lark MCP，最后收尾
STREAM = [
    _j({"type": "session", "version": 3, "id": SID, "timestamp": "2026-11-20T07:43:37.101Z", "cwd": "/tmp"}),
    _j({"type": "agent_start"}),
    _j({"type": "message_start", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}),
    _j({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}}),
    _delta("先看看"),
    _j({"type": "message_update", "usage": {},
        "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0, "delta": "内心戏"}}),
    _j({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "toolUse", "usage": _usage(6000, 20, cost=0.0046),
        "content": [{"type": "text", "text": "先看看"},
                    {"type": "toolCall", "id": "call_1", "name": "bash", "arguments": {"command": "ls"}}]}}),
    _j({"type": "tool_execution_start", "toolCallId": "call_1", "toolName": "bash", "args": {"command": "ls"}}),
    _j({"type": "tool_execution_end", "toolCallId": "call_1", "toolName": "bash", "isError": False,
        "result": {"content": [{"type": "text", "text": "a\n"}],
                   "structuredContent": {"output": "a\n", "exit_code": 0}}}),
    _j({"type": "tool_execution_start", "toolCallId": "call_2", "toolName": "mcp__cc_lark__list_crons", "args": {}}),
    _j({"type": "tool_execution_end", "toolCallId": "call_2", "toolName": "mcp__cc_lark__list_crons",
        "isError": False, "result": {"content": [{"type": "text", "text": "none"}]}}),
    _j({"type": "tool_execution_start", "toolCallId": "call_3", "toolName": "find", "args": {"pattern": "*.py"}}),
    _j({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}}),
    _delta("搞定"),
    _delta("了"),
    _j({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "stop", "usage": _usage(2000, 10, cache_read=4000, cost=0.0019),
        "content": [{"type": "text", "text": "搞定了"}]}}),
    _j({"type": "agent_end", "messages": [], "willRetry": False}),
    _j({"type": "agent_settled", "aborted": False}),
]


def _patch_exec(monkeypatch, procs, captured):
    procs = list(procs)
    captured.setdefault("calls", [])

    async def fake_exec(*args, **kwargs):
        captured["calls"].append({"cmd": list(args), "cwd": kwargs.get("cwd"), "env": kwargs.get("env")})
        proc = procs.pop(0)
        captured.setdefault("procs", []).append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    # 上下文窗口别真去跑 `pi --list-models`（会吃掉一个假进程）
    monkeypatch.setenv("PI_CONTEXT_WINDOW", "1000000")
    monkeypatch.delenv("PI_MODEL", raising=False)
    monkeypatch.delenv("PI_EFFORT", raising=False)
    d = tmp_path / "pi-sessions"
    d.mkdir()
    return str(d)


def _run(sessions, **kw):
    kw.setdefault("message", "hello")
    kw.setdefault("cwd", "/tmp")
    return asyncio.run(run_pi(sessions_dir=sessions, **kw))


def test_streams_text_tools_and_usage(monkeypatch, sessions):
    captured, chunks, tools, usages = {}, [], [], []
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)
    text, sid, fallback = _run(
        sessions,
        on_text_chunk=chunks.append, on_tool_use=lambda n, i: tools.append((n, i)), on_usage=usages.append,
    )
    assert (text, sid, fallback) == ("搞定了", SID, False)
    # 两条 assistant 消息之间补空行；thinking 不进正文
    assert "".join(chunks) == "先看看\n\n搞定了"
    assert tools == [
        ("bash", {"command": "ls"}),
        ("bash", {"command": "ls", "status": "completed", "exit_code": 0}),
        ("mcp__cc_lark__list_crons", {}),
        ("mcp__cc_lark__list_crons", {"_agy_state": "DONE"}),
        ("glob", {"pattern": "*.py"}),
    ]
    u = usages[-1]
    # 上下文 = 最后一次请求；本轮总消耗 / 花费 = 全部请求求和
    assert u["input_tokens"] == 2000 and u["cache_read_input_tokens"] == 4000 and u["output_tokens"] == 10
    assert u["_context_window"] == 1_000_000
    assert u["_turn_tokens"] == 6020 + 6010
    assert u["_turn_cost_usd"] == pytest.approx(0.0065)
    proc = captured["procs"][0]
    assert proc.stdin.data == b"hello" and proc.stdin.closed


def test_builds_expected_argv_and_env(monkeypatch, sessions):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)
    monkeypatch.setenv("AGY_API_KEY", "gem-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _run(
        sessions, model="pi-neon", effort="max", pi_bin="/opt/pi", append_system_prompt="SYS",
        extra_env={"CC_LARK_THREAD_ID": "omt_p", "CC_LARK_PROFILE": "main", "CC_LARK_CONTROL_TOKEN": "x"},
    )
    call = captured["calls"][0]
    cmd, env = call["cmd"], call["env"]
    assert cmd[:3] == ["/opt/pi", "--mode", "json"]
    assert cmd[cmd.index("--session-dir") + 1] == sessions
    assert "--session" not in cmd  # 新会话
    assert cmd[cmd.index("--model") + 1] == "google/gemini-4-neon"  # 别名已解析
    assert cmd[cmd.index("--thinking") + 1] == "xhigh"  # Neon 最高档是 xhigh，max 落到它
    assert "--tools" not in cmd  # 默认工具全开
    assert cmd[cmd.index("-e") + 1] == pi_runner.EXTENSION_PATH
    # 系统提示走临时文件（跑完删掉）
    assert not os.path.exists(cmd[cmd.index("--append-system-prompt") + 1])
    # cc-lark MCP 配置进 env；控制口令不进 MCP server 的 env
    cfg = json.loads(env["CC_LARK_PI_MCP_CONFIG"])
    server_env = cfg["mcpServers"]["cc-lark"]["env"]
    assert server_env["CC_LARK_THREAD_ID"] == "omt_p"
    assert "CC_LARK_CONTROL_TOKEN" not in server_env
    assert env["GEMINI_API_KEY"] == "gem-key"  # 借 agy 的 Gemini key
    assert env["PI_SKIP_VERSION_CHECK"] == "1"
    # 模型表：Google 只留那 5 款，Neon / Hydrogen 标成要改写 xhigh
    provider = json.loads(env["CC_LARK_PI_PROVIDER"])
    assert provider["provider"] == "google"
    assert provider["config"]["apiKey"] == "$GEMINI_API_KEY"
    assert sorted(m["id"] for m in provider["config"]["models"]) == sorted(pi_runner.PI_GEMINI_MODELS)
    assert sorted(provider["xhighModels"]) == ["gemini-4-hydrogen", "gemini-4-neon"]


def test_no_thread_context_means_no_mcp_but_still_model_table(monkeypatch, sessions):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)
    monkeypatch.setenv("CC_LARK_PI_MCP_CONFIG", "stale")
    _run(sessions, extra_env={"CC_LARK_PROFILE": "main"})
    call = captured["calls"][0]
    # 私聊没有话题上下文：不挂 cc-lark MCP，但扩展照样加载（模型表要换）
    assert call["cmd"][call["cmd"].index("-e") + 1] == pi_runner.EXTENSION_PATH
    assert "CC_LARK_PI_MCP_CONFIG" not in call["env"]
    assert "CC_LARK_PI_PROVIDER" in call["env"]


def test_resumes_by_session_file(monkeypatch, sessions):
    path = os.path.join(sessions, f"2026-11-20T07-43-37-101Z_{SID}.jsonl")
    open(path, "w").close()
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)
    _, sid, fallback = _run(sessions, session_id=SID)
    cmd = captured["calls"][0]["cmd"]
    assert cmd[cmd.index("--session") + 1] == path
    assert (sid, fallback) == (SID, False)


def test_missing_session_file_starts_fresh(monkeypatch, sessions):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)
    _, sid, fallback = _run(sessions, session_id="0000dead-beef-7000-8000-000000000000")
    assert "--session" not in captured["calls"][0]["cmd"]
    assert (sid, fallback) == (SID, True)


def test_resume_rejected_by_pi_retries_fresh(monkeypatch, sessions):
    path = os.path.join(sessions, f"x_{SID}.jsonl")
    open(path, "w").close()
    captured = {}
    gone = FakeProc([], returncode=1, stderr=b"No session found matching 'x'")
    _patch_exec(monkeypatch, [gone, FakeProc(STREAM)], captured)
    text, _, fallback = _run(sessions, session_id=SID)
    assert text == "搞定了" and fallback is True
    assert "--session" not in captured["calls"][1]["cmd"]


def test_parallel_tools_only_mark_latest_done(monkeypatch, sessions):
    stream = [
        STREAM[0],
        _j({"type": "tool_execution_start", "toolCallId": "a", "toolName": "bash", "args": {"command": "echo 1"}}),
        _j({"type": "tool_execution_start", "toolCallId": "b", "toolName": "read", "args": {"path": "/x"}}),
        _j({"type": "tool_execution_end", "toolCallId": "a", "toolName": "bash", "isError": False,
            "result": {"structuredContent": {"exit_code": 0}}}),
        _j({"type": "tool_execution_end", "toolCallId": "b", "toolName": "read", "isError": True, "result": {}}),
        STREAM[-3], STREAM[-2], STREAM[-1],
    ]
    tools = []
    _patch_exec(monkeypatch, [FakeProc(stream)], {})
    _run(sessions, on_tool_use=lambda n, i: tools.append((n, i)))
    assert tools == [
        ("bash", {"command": "echo 1"}),
        ("read", {"path": "/x"}),
        ("read", {"path": "/x", "_agy_state": "ERROR"}),
    ]


def test_model_error_is_raised_with_session(monkeypatch, sessions):
    stream = [
        STREAM[0],
        _j({"type": "message_start", "message": {"role": "assistant", "content": []}}),
        _j({"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "content": [],
                                               "errorMessage": "503 overloaded"}}),
        _j({"type": "agent_settled", "aborted": False}),
    ]
    _patch_exec(monkeypatch, [FakeProc(stream)], {})
    with pytest.raises(RuntimeError) as ei:
        _run(sessions)
    assert "503 overloaded" in str(ei.value)
    assert ei.value.cc_session_id == SID
    assert ei.value.cc_retryable_resume is True


def test_error_then_successful_retry_is_not_an_error(monkeypatch, sessions):
    stream = [
        STREAM[0],
        _j({"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "content": [],
                                               "errorMessage": "529"}}),
        _j({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "errorMessage": "529"}),
        _j({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
                                               "content": [{"type": "text", "text": "ok"}]}}),
    ]
    _patch_exec(monkeypatch, [FakeProc(stream)], {})
    text, _, _ = _run(sessions)
    assert text == "ok"


def test_read_only_tools_when_permissions_not_bypassed(monkeypatch, sessions):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)
    _run(sessions, dangerously_skip_permissions=False, permission_mode="default")
    cmd = captured["calls"][0]["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "read,grep,find,ls"


def test_unknown_effort_is_ignored_and_profile_default_used(monkeypatch, sessions):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)
    monkeypatch.setenv("MAIN_PI_EFFORT", "high")
    _run(sessions, effort="ultra", extra_env={"CC_LARK_PROFILE": "main"})
    cmd = captured["calls"][0]["cmd"]
    assert cmd[cmd.index("--thinking") + 1] == "high"


def test_non_pi_model_falls_back_to_pi_default(monkeypatch, sessions):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)
    monkeypatch.setenv("PI_MODEL", "google/gemini-4-argon")
    _run(sessions, model="opus[1m]")
    cmd = captured["calls"][0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "google/gemini-4-argon"


def test_session_cost_sums_assistant_messages(sessions):
    path = os.path.join(sessions, f"t_{SID}.jsonl")
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "session", "id": SID}) + "\n")
        for c in (0.01, 0.02):
            fh.write(json.dumps({"type": "message", "message": {"role": "assistant",
                                                                "usage": {"cost": {"total": c}}}}) + "\n")
        fh.write(json.dumps({"type": "message", "message": {"role": "user", "content": "assistant"}}) + "\n")
    assert pi_runner.session_cost(SID, sessions) == pytest.approx(0.03)
    assert pi_runner.session_cost("missing-session-id", sessions) is None


def test_model_list_matches_bot_config():
    import bot_config
    assert set(pi_runner.PI_GEMINI_MODELS) == set(bot_config.PI_MODELS)
    assert pi_runner.DEFAULT_MODEL == "google/gemini-3.8-flash"


def test_provider_config_maps_xhigh_to_high_for_pi_validation():
    models = {m["id"]: m for m in pi_runner.provider_config()["config"]["models"]}
    assert models["gemini-4-neon"]["thinkingLevelMap"]["xhigh"] == "high"  # 扩展再改写成 XHIGH
    assert models["gemini-4-carbon"]["thinkingLevelMap"]["xhigh"] is None
    assert all(m["contextWindow"] == 1_048_576 for m in models.values())
    assert models["gemini-4-neon"]["cost"]["input"] == 3.0


def test_normalize_thinking_per_model():
    nt = pi_runner.normalize_thinking
    assert nt("") is None and nt("auto") is None
    assert nt("none", "google/gemini-4-neon") == "low"
    assert nt("minimal", "google/gemini-3.8-flash") == "low"
    assert nt("MEDIUM", "google/gemini-4-carbon") == "medium"
    assert nt("max", "google/gemini-4-neon") == "xhigh"
    assert nt("xhigh", "gemini-4-hydrogen") == "xhigh"
    assert nt("xhigh", "google/gemini-4-carbon") == "high"
    assert nt("max", "google/gemini-3.8-flash") == "high"
    with pytest.raises(ValueError):
        nt("ultra", "google/gemini-4-neon")
    assert pi_runner.effort_levels_for_model("google/gemini-4-neon") == ("low", "medium", "high", "xhigh")
    assert pi_runner.effort_levels_for_model("google/gemini-4-argon") == ("low", "medium", "high")


def test_default_model_ignores_non_gemini(monkeypatch):
    monkeypatch.setenv("PI_MODEL", "openai-codex/gpt-5.5")
    assert pi_runner.default_model("main") == "google/gemini-3.8-flash"
    monkeypatch.setenv("MAIN_PI_MODEL", "gemini-4-neon")
    assert pi_runner.default_model("main") == "google/gemini-4-neon"


def test_extension_file_registers_models_and_servers():
    src = open(pi_runner.EXTENSION_PATH, encoding="utf-8").read()
    assert "CC_LARK_PI_PROVIDER" in src and "registerProvider" in src
    assert "before_provider_request" in src and '"XHIGH"' in src
    assert "CC_LARK_PI_MCP_CONFIG" in src
    assert "registerMcpServer" in src
    assert 'exposure: "direct"' in src
