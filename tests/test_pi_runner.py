"""pi_runner：JSON 事件流解析、argv、会话续轮、MCP 扩展注入、出错回显。"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pi_runner
from pi_runner import run_pi


@pytest.fixture(autouse=True)
def _no_real_local_providers(monkeypatch):
    """导入 bot_config 时会把本机 .env 灌进环境（服务器上配了 Lebrel + Venice），每个用例先清掉。"""
    for k in list(os.environ):
        if k.startswith("PI_LOCAL_") or k == "PI_GEMINI_BUTTONS":
            monkeypatch.delenv(k, raising=False)


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
    monkeypatch.delenv("PI_LOCAL_MODELS", raising=False)
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
    providers = json.loads(env["CC_LARK_PI_PROVIDER"])
    assert [p["provider"] for p in providers] == ["google"]  # 没配本地模型就只有 Google
    provider = providers[0]
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
    assert "Array.isArray(raw)" in src  # 多个 provider（Google + 本地）
    assert "before_provider_request" in src and '"XHIGH"' in src
    assert "CC_LARK_PI_MCP_CONFIG" in src
    assert "registerMcpServer" in src
    assert 'exposure: "direct"' in src


# ── 本地模型（PI_LOCAL_MODELS，mini2 上的 Ollama）────────────────────────────


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setenv("PI_LOCAL_MODELS", "qwen36-uncensored, Other:7b")
    for k in ("PI_LOCAL_PROVIDER", "PI_LOCAL_BASE_URL", "PI_LOCAL_API_KEY_ENV", "PI_LOCAL_CONTEXT_WINDOW",
              "PI_LOCAL_MAX_TOKENS", "PI_LOCAL_VISION", "PI_LOCAL_COST", "PI_LOCAL_EXTRA_BODY",
              "PI_CONTEXT_WINDOW", "PI_MODEL", "MAIN_PI_MODEL"):
        monkeypatch.delenv(k, raising=False)


def test_local_provider_absent_without_config(monkeypatch):
    monkeypatch.delenv("PI_LOCAL_MODELS", raising=False)
    assert pi_runner.local_provider_config() is None
    assert [p["provider"] for p in pi_runner.provider_configs()] == ["google"]
    assert not pi_runner.is_local_model("ollama/qwen36-uncensored")


def test_local_provider_config(local, monkeypatch):
    monkeypatch.setenv("PI_LOCAL_CONTEXT_WINDOW", "32768")
    monkeypatch.setenv("PI_LOCAL_MAX_TOKENS", "999999")
    cfg = pi_runner.local_provider_config()
    assert cfg["provider"] == "ollama"
    c = cfg["config"]
    assert c["api"] == "openai-completions" and c["baseUrl"] == "http://127.0.0.1:11434/v1"
    assert c["apiKey"] == "local"  # Ollama 不鉴权，但 pi 要非空 key 才认模型
    assert [m["id"] for m in c["models"]] == ["qwen36-uncensored", "Other:7b"]
    m = c["models"][0]
    assert m["contextWindow"] == 32768 and m["maxTokens"] == 32768  # 回复上限不超过窗口
    assert m["thinkingLevelMap"]["off"] == "none" and m["thinkingLevelMap"]["xhigh"] is None
    assert m["compat"]["thinkingFormat"] == "openai"
    assert m["cost"]["input"] == 0 and m["input"] == ["text", "image"]
    assert [p["provider"] for p in pi_runner.provider_configs()] == ["google", "ollama"]


def test_local_provider_key_env_and_custom_name(local, monkeypatch):
    monkeypatch.setenv("PI_LOCAL_PROVIDER", "LMStudio")
    monkeypatch.setenv("PI_LOCAL_API_KEY_ENV", "LMS_KEY")
    monkeypatch.setenv("PI_LOCAL_BASE_URL", "http://10.0.0.2:1234/v1/")
    monkeypatch.setenv("PI_LOCAL_VISION", "0")
    cfg = pi_runner.local_provider_config()
    assert cfg["provider"] == "lmstudio"
    assert cfg["config"]["apiKey"] == "$LMS_KEY"
    assert cfg["config"]["baseUrl"] == "http://10.0.0.2:1234/v1"
    assert cfg["config"]["models"][0]["input"] == ["text"]
    assert pi_runner.is_local_model("lmstudio/qwen36-uncensored")
    assert not pi_runner.is_local_model("ollama/qwen36-uncensored")


def test_local_provider_cost_and_extra_body(local, monkeypatch):
    # 收费的 OpenAI 兼容服务（Venice）：价目进模型表，extraBody 交给扩展并进请求体
    monkeypatch.setenv("PI_LOCAL_PROVIDER", "venice")
    monkeypatch.setenv("PI_LOCAL_COST", "0.33, 1.32,0.00825")
    monkeypatch.setenv("PI_LOCAL_EXTRA_BODY", '{"venice_parameters":{"include_venice_system_prompt":false}}')
    cfg = pi_runner.local_provider_config()
    assert cfg["config"]["models"][0]["cost"] == {"input": 0.33, "output": 1.32, "cacheRead": 0.00825, "cacheWrite": 0.0}
    # 每个模型一份，别共用同一个 dict
    assert cfg["config"]["models"][0]["cost"] is not cfg["config"]["models"][1]["cost"]
    assert cfg["extraBody"] == {"venice_parameters": {"include_venice_system_prompt": False}}


@pytest.mark.parametrize("cost", ["abc", "1,2,3,4,5", "-1,2"])
def test_local_provider_bad_cost_is_free(local, monkeypatch, cost):
    monkeypatch.setenv("PI_LOCAL_COST", cost)
    cfg = pi_runner.local_provider_config()
    assert cfg["config"]["models"][0]["cost"] == {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


@pytest.mark.parametrize("body", ["not json", "[1]", "{}"])
def test_local_provider_bad_extra_body_ignored(local, monkeypatch, body):
    monkeypatch.setenv("PI_LOCAL_EXTRA_BODY", body)
    assert "extraBody" not in pi_runner.local_provider_config()


def test_local_model_resolution(local, monkeypatch):
    import bot_config
    # 裸 id、带前缀、大小写都认，统一成 ollama/<配置里的原样 id>
    assert bot_config.normalize_model_for_runner("qwen36-uncensored", "pi") == "ollama/qwen36-uncensored"
    assert bot_config.normalize_model_for_runner("ollama/other:7B", "pi") == "ollama/Other:7b"
    assert bot_config.is_model_compatible_with_runner("ollama/qwen36-uncensored", "pi")
    assert not bot_config.is_model_compatible_with_runner("ollama/llama3", "pi")
    # Gemini 照旧
    assert bot_config.normalize_model_for_runner("pi-neon", "pi") == "google/gemini-4-neon"
    # 默认模型可以是本地的
    assert pi_runner.default_model("main") == "google/gemini-3.8-flash"
    monkeypatch.setenv("PI_MODEL", "ollama/qwen36-uncensored")
    assert pi_runner.default_model("main") == "ollama/qwen36-uncensored"
    assert pi_runner.resolve_model("opus[1m]", "main") == "ollama/qwen36-uncensored"
    assert pi_runner.resolve_model("qwen36-uncensored", "main") == "ollama/qwen36-uncensored"


def test_local_thinking_levels(local):
    nt = pi_runner.normalize_thinking
    m = "ollama/qwen36-uncensored"
    assert pi_runner.effort_levels_for_model(m) == ("none", "low", "medium", "high")
    assert nt("none", m) == "off" and nt("off", m) == "off"
    assert nt("minimal", m) == "low" and nt("high", m) == "high"
    assert nt("max", m) == "high" and nt("xhigh", m) == "high"
    with pytest.raises(ValueError):
        nt("ultra", m)
    # Gemini 的 none 还是抬到 low
    assert nt("none", "google/gemini-4-neon") == "low"


def test_local_context_window(local, monkeypatch):
    assert pi_runner.context_window_for("ollama/qwen36-uncensored") == 65_536
    monkeypatch.setenv("PI_LOCAL_CONTEXT_WINDOW", "131072")
    assert pi_runner.context_window_for("ollama/qwen36-uncensored") == 131_072
    assert pi_runner.context_window_for("google/gemini-4-neon") == 1_048_576


def test_model_buttons_local_first_and_gemini_only_with_key(local, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("AGY_API_KEY", raising=False)
    monkeypatch.delenv("PI_GEMINI_KEY_FROM", raising=False)
    assert pi_runner.model_buttons() == [
        ("ollama/qwen36-uncensored", "🖥️ qwen36-uncensored"), ("ollama/Other:7b", "🖥️ Other:7b"),
    ]
    monkeypatch.setenv("AGY_API_KEY", "k")
    btns = pi_runner.model_buttons()
    assert btns[0][0] == "ollama/qwen36-uncensored" and ("pi-neon", "✴️ Gemini 4 Neon") in btns
    monkeypatch.delenv("PI_LOCAL_MODELS")
    assert pi_runner.model_buttons() == pi_runner.PI_MODEL_BUTTONS


def test_run_with_local_model(monkeypatch, sessions, local):
    captured, usages = {}, []
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)
    monkeypatch.delenv("PI_CONTEXT_WINDOW", raising=False)
    monkeypatch.setenv("PI_MODEL", "ollama/qwen36-uncensored")
    monkeypatch.setenv("PI_EFFORT", "none")
    _run(sessions, model="opus[1m]", on_usage=usages.append, extra_env={"CC_LARK_PROFILE": "main"})
    cmd, env = captured["calls"][0]["cmd"], captured["calls"][0]["env"]
    assert cmd[cmd.index("--model") + 1] == "ollama/qwen36-uncensored"
    assert cmd[cmd.index("--thinking") + 1] == "off"  # PI_EFFORT=none → 关掉思考
    assert [p["provider"] for p in json.loads(env["CC_LARK_PI_PROVIDER"])] == ["google", "ollama"]
    assert usages[-1]["_context_window"] == 65_536


# ── 同时挂几家收费 provider（11-22：Lebrel 为主 + Venice，/model 一家一个按钮）──────────


@pytest.fixture
def two_providers(monkeypatch):
    monkeypatch.setenv("PI_LOCAL_PROVIDER", "lebrel")
    monkeypatch.setenv("PI_LOCAL_MODELS", "lebrel/deepseek-v4.1-flash-uncensored")
    monkeypatch.setenv("PI_LOCAL_BASE_URL", "https://attested.lebrel.ai/v1")
    monkeypatch.setenv("PI_LOCAL_API_KEY_ENV", "LEBREL_API_KEY")
    monkeypatch.setenv("PI_LOCAL_CONTEXT_WINDOW", "262144")
    monkeypatch.setenv("PI_LOCAL_COST", "0.33,1.32,0.01")
    monkeypatch.setenv("PI_LOCAL_LABEL", "🐕 Lebrel")
    monkeypatch.setenv("PI_LOCAL_MORE_PROVIDERS", "Venice, nobody")  # nobody 没配模型，忽略
    monkeypatch.setenv("PI_LOCAL_VENICE_MODELS", "deepseek-v4-1-flash-1017-uncensored")
    monkeypatch.setenv("PI_LOCAL_VENICE_BASE_URL", "https://api.venice.ai/api/v1")
    monkeypatch.setenv("PI_LOCAL_VENICE_API_KEY_ENV", "VENICE_API_KEY")
    monkeypatch.setenv("PI_LOCAL_VENICE_CONTEXT_WINDOW", "131072")
    monkeypatch.setenv("PI_LOCAL_VENICE_COST", "0.33,1.32,0.00825")
    monkeypatch.setenv("PI_LOCAL_VENICE_EXTRA_BODY", '{"venice_parameters":{"include_venice_system_prompt":false}}')
    monkeypatch.setenv("PI_LOCAL_VENICE_LABEL", "🐋 Venice")
    for k in ("PI_CONTEXT_WINDOW", "PI_MODEL", "GEMINI_API_KEY", "AGY_API_KEY", "PI_GEMINI_KEY_FROM"):
        monkeypatch.delenv(k, raising=False)


def test_two_providers_registered_with_own_settings(two_providers):
    from bot_config import pi_local_providers
    assert pi_local_providers() == ("lebrel", "venice")
    cfgs = {c["provider"]: c for c in pi_runner.local_provider_configs()}
    assert list(cfgs) == ["lebrel", "venice"]
    assert cfgs["lebrel"]["config"]["apiKey"] == "$LEBREL_API_KEY"
    assert cfgs["venice"]["config"]["apiKey"] == "$VENICE_API_KEY"
    assert cfgs["venice"]["config"]["models"][0]["cost"]["cacheRead"] == 0.00825
    assert cfgs["lebrel"]["config"]["models"][0]["cost"]["cacheRead"] == 0.01
    assert "extraBody" not in cfgs["lebrel"]
    assert cfgs["venice"]["extraBody"] == {"venice_parameters": {"include_venice_system_prompt": False}}
    assert [p["provider"] for p in pi_runner.provider_configs()] == ["google", "lebrel", "venice"]


def test_two_providers_model_ids_and_buttons(two_providers):
    from bot_config import normalize_model_for_runner, pi_local_ref
    # Lebrel 的 id 自己带斜杠：provider/id 和裸 id 都认
    assert pi_local_ref("lebrel/lebrel/deepseek-v4.1-flash-uncensored") == ("lebrel", "lebrel/deepseek-v4.1-flash-uncensored")
    assert pi_local_ref("lebrel/deepseek-v4.1-flash-uncensored") == ("lebrel", "lebrel/deepseek-v4.1-flash-uncensored")
    assert pi_local_ref("venice/deepseek-v4-1-flash-1017-uncensored") == ("venice", "deepseek-v4-1-flash-1017-uncensored")
    assert pi_local_ref("deepseek-v4-1-flash-1017-uncensored") == ("venice", "deepseek-v4-1-flash-1017-uncensored")
    assert pi_local_ref("venice/lebrel/deepseek-v4.1-flash-uncensored") is None
    assert normalize_model_for_runner("venice/deepseek-v4-1-flash-1017-uncensored", "pi") == \
        "venice/deepseek-v4-1-flash-1017-uncensored"
    assert normalize_model_for_runner("lebrel/deepseek-v4.1-flash-uncensored", "pi") == \
        "lebrel/lebrel/deepseek-v4.1-flash-uncensored"
    assert pi_runner.model_buttons() == [
        ("lebrel/lebrel/deepseek-v4.1-flash-uncensored", "🐕 Lebrel"),
        ("venice/deepseek-v4-1-flash-1017-uncensored", "🐋 Venice"),
    ]
    assert pi_runner.context_window_for("venice/deepseek-v4-1-flash-1017-uncensored") == 131_072
    assert pi_runner.context_window_for("lebrel/lebrel/deepseek-v4.1-flash-uncensored") == 262_144
    assert pi_runner.normalize_thinking("none", "venice/deepseek-v4-1-flash-1017-uncensored") == "off"


def test_two_providers_default_label_names_provider(two_providers, monkeypatch):
    monkeypatch.delenv("PI_LOCAL_LABEL")
    assert pi_runner.model_buttons()[0] == (
        "lebrel/lebrel/deepseek-v4.1-flash-uncensored", "🖥️ Lebrel · lebrel/deepseek-v4.1-flash-uncensored",
    )


def test_balance_lines(two_providers, monkeypatch):
    monkeypatch.setenv("VENICE_API_KEY", "vk")
    monkeypatch.setenv("LEBREL_API_KEY", "lk")
    monkeypatch.setenv("PI_LOCAL_BALANCE_URL", "https://lebrel.ai/dashboard")
    calls = []

    def fake_http(url, key="", timeout=5.0):
        calls.append((url, key))
        if url == "https://api.venice.ai/api/v1/api_keys/rate_limits":
            return {"data": {"balances": {"USD": 9.10381, "DIEM": 0}}}
        if url.endswith("/api/drop/policy"):
            return {"phase": {"free": False}, "price": {"percent": 40}}
        return None  # 推理 key 查 /api/overview 回 403

    monkeypatch.setattr(pi_runner, "_http_json", fake_http)
    assert pi_runner.local_balance_lines() == [
        "💰 Lebrel 余额 [后台查看](https://lebrel.ai/dashboard) · 现在原价 4 折",
        "💰 Venice 余额 $9.10",
    ]
    assert ("https://lebrel.ai/api/overview", "lk") in calls
    # 配了只读 key 就查得到余额；免费时段报到几点（北京时间）
    monkeypatch.setenv("PI_LOCAL_BALANCE_KEY_ENV", "LEBREL_ACCOUNT_KEY")
    monkeypatch.setenv("LEBREL_ACCOUNT_KEY", "ro")

    def fake_http2(url, key="", timeout=5.0):
        if url == "https://lebrel.ai/api/overview":
            return {"balanceMicrousd": 12_345_678} if key == "ro" else None
        if url.endswith("/api/drop/policy"):
            return {"phase": {"free": True, "freeUntil": 1795374000000}}
        return None

    monkeypatch.setattr(pi_runner, "_http_json", fake_http2)
    assert pi_runner.local_balance_lines() == [
        "💰 Lebrel 余额 $12.35 · 现在免费（到 03:00）",
        "💰 Venice 余额 [后台查看](https://venice.ai/settings/api)"
        if os.getenv("PI_LOCAL_VENICE_BALANCE_URL") else "💰 Venice 余额 查不到",
    ]


def test_balance_skips_free_local_server(local):
    # Ollama 不收费：认不出是哪家就不报余额
    assert pi_runner.local_balance_lines() == []


def test_gemini_buttons_can_be_hidden_when_local_models_exist(two_providers, monkeypatch):
    monkeypatch.setenv("AGY_API_KEY", "k")
    assert ("pi-neon", "✴️ Gemini 4 Neon") in pi_runner.model_buttons()
    monkeypatch.setenv("PI_GEMINI_BUTTONS", "0")
    assert [a for a, _ in pi_runner.model_buttons()] == [
        "lebrel/lebrel/deepseek-v4.1-flash-uncensored", "venice/deepseek-v4-1-flash-1017-uncensored",
    ]
    # 一个本地模型都没有时照样摆 Gemini，免得面板空了
    for k in ("PI_LOCAL_MODELS", "PI_LOCAL_VENICE_MODELS"):
        monkeypatch.delenv(k)
    assert pi_runner.model_buttons() == pi_runner.PI_MODEL_BUTTONS


def _thinking_end(text):
    return _j({"type": "message_update", "usage": {},
               "assistantMessageEvent": {"type": "thinking_end", "contentIndex": 0, "content": text}})


# DeepSeek 式：叙述全在思考里、工具之间没有正文，正文只在最后一条消息
THINKING_STREAM = [
    STREAM[0],
    _j({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}}),
    _thinking_end("Let me look at\nthe workspace first."),
    _j({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "toolUse", "usage": _usage(100, 10),
        "content": [{"type": "thinking", "thinking": "Let me look at\nthe workspace first."},
                    {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}}]}}),
    _j({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {"command": "ls"}}),
    _j({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash", "isError": False,
        "result": {"structuredContent": {"exit_code": 0}}}),
    _j({"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}}),
    _thinking_end("Found it, now answer."),
    _delta("搞定"),
    _j({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "stop", "usage": _usage(100, 10),
        "content": [{"type": "thinking", "thinking": "Found it, now answer."},
                    {"type": "text", "text": "搞定"}]}}),
    STREAM[-2], STREAM[-1],
]


def test_thinking_shown_on_card_but_not_in_result(monkeypatch, sessions):
    chunks = []
    _patch_exec(monkeypatch, [FakeProc(THINKING_STREAM)], {})
    text, _, _ = _run(sessions, on_text_chunk=chunks.append)
    assert text == "搞定"
    shown = "".join(chunks)
    assert shown == "💭 Let me look at the workspace first.\n\n💭 Found it, now answer.\n\n搞定"
    # 收尾卡片能拆出「过程」= 两行思考、「结论」= 正文
    from dispatcher import _split_process_and_result
    assert _split_process_and_result(shown, text) == (
        "💭 Let me look at the workspace first.\n\n💭 Found it, now answer.", "搞定")


def test_thinking_after_text_gets_blank_line(monkeypatch, sessions):
    stream = [STREAM[0], STREAM[3], _delta("先看看"), _thinking_end("hmm"), _delta("好"), STREAM[-2], STREAM[-1]]
    chunks = []
    _patch_exec(monkeypatch, [FakeProc(stream)], {})
    _run(sessions, on_text_chunk=chunks.append)
    assert "".join(chunks) == "先看看\n\n💭 hmm\n\n好"


def test_thinking_hidden_when_disabled(monkeypatch, sessions):
    monkeypatch.setenv("CC_LARK_PI_SHOW_THINKING", "0")
    chunks = []
    _patch_exec(monkeypatch, [FakeProc(THINKING_STREAM)], {})
    _run(sessions, on_text_chunk=chunks.append)
    assert "".join(chunks) == "搞定"


def test_thinking_line_truncates_and_balances_markdown():
    from pi_runner import THINKING_PREVIEW_CHARS, _thinking_line
    assert _thinking_line("") == "" and _thinking_line("  \n ") == ""
    long = "x" * 10 + " `lark-cli docs +fetch " + "y" * 400 + "`"
    line = _thinking_line(long)
    assert line.startswith("💭 ") and line.count("`") % 2 == 0
    assert len(line) <= len("💭 ") + THINKING_PREVIEW_CHARS + 2
    assert _thinking_line("a **bold") == "💭 a **bold**"
    assert _thinking_line("2 * 3") == "💭 2  3"
