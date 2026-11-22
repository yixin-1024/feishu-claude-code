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
    # 窗口按模型（Efficient 200K），已用 = 占比 × 窗口
    assert usages[-1] == {"_context_ratio": 0.11064, "_context_window": 200_000,
                          "_context_tokens": 22128, "_session_credits": 0.0672}
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
    }, "Performance")
    assert usage == {"input_tokens": 1200, "output_tokens": 30,
                     "_context_ratio": 0.2, "_context_window": 272_000,
                     "_context_tokens": 54400, "_session_credits": 0.5}


def test_turn_credits_sum_billable_messages_only(monkeypatch):
    # result.total_credits 是会话累计（实测 6.87 → 28.09 → 67.11 → 69.78 一路涨）；
    # 本轮要按消息加，限时免费模型的消息 billable=false 不算。
    def asst(mid, credits, billable=True, **extra):
        return (json.dumps({"type": "assistant", "session_id": SID, **extra, "message": {
            "id": mid, "content": [], "usage": {"credits": credits, "billable": billable}}}) + "\n").encode()

    lines = [
        b'{"type":"system","subtype":"init","session_id":"' + SID.encode() + b'"}\n',
        asst("m1", 0.0),          # 同一条消息先到 thinking 块（还没记 credits）
        asst("m1", 1.25),         # 再到带 credits 的那次——按 id 取最大，只算一次
        asst("m2", 0.75),
        asst("m3", 2.0, billable=False),
        asst("m4", 0.5, parent_tool_use_id="call_sub"),  # 子 agent 也是真扣钱
        b'{"type":"result","subtype":"success","is_error":false,"result":"ok","total_credits":69.78,'
        b'"session_id":"' + SID.encode() + b'","usage":{"context_usage_ratio":0.694}}\n',
    ]
    _patch_exec(monkeypatch, [FakeProc(lines)], {})
    usages = []
    asyncio.run(run_qoder(message="x", cwd="/tmp", on_usage=usages.append))

    u = usages[-1]
    assert u["_turn_credits"] == 2.5
    assert u["_turn_free_credits"] == 2.0
    assert u["_session_credits"] == 69.78
    assert qoder_runner.format_credits_suffix(u) == "本轮 2.50 credits · 会话累计 69.78 credits"


def test_turn_credits_from_message_delta_in_partial_mode(monkeypatch):
    # 实测 --include-partial-messages 下：assistant 事件的 usage 没有 credits，
    # credits 在 message_delta.usage 里（Qwen3.8-Flash：0.257、billable=false）
    lines = [
        b'{"type":"system","subtype":"init","session_id":"' + SID.encode() + b'"}\n',
        _ev({"type": "message_start", "message": {"id": "m1", "usage": {}}}),
        _ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "2"}}),
        (json.dumps({"type": "assistant", "session_id": SID,
                     "message": {"id": "m1", "content": [{"type": "text", "text": "2"}], "usage": {}}}) + "\n").encode(),
        _ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"credits": 0.25724, "billable": False}}),
        _ev({"type": "message_start", "message": {"id": "m2", "usage": {}}}),
        _ev({"type": "message_delta", "usage": {"credits": 0.4, "billable": True}}),
        # 同一条消息若 assistant 事件里也带了 credits，不能算两遍
        (json.dumps({"type": "assistant", "session_id": SID,
                     "message": {"id": "m2", "content": [], "usage": {"credits": 0.4, "billable": True}}}) + "\n").encode(),
        b'{"type":"result","subtype":"success","is_error":false,"result":"2","total_credits":0,'
        b'"session_id":"' + SID.encode() + b'","usage":{"context_usage_ratio":0.16}}\n',
    ]
    _patch_exec(monkeypatch, [FakeProc(lines)], {})
    usages = []
    asyncio.run(run_qoder(message="x", cwd="/tmp", on_usage=usages.append))

    u = usages[-1]
    assert u["_turn_credits"] == 0.4
    assert u["_turn_free_credits"] == 0.2572
    assert "_session_credits" not in u
    assert qoder_runner.format_credits_suffix(u) == "本轮 0.40 credits"
    free_only = {**u, "_turn_credits": 0.0}
    assert qoder_runner.format_credits_suffix(free_only) == "本轮免费"


def test_credits_suffix_variants():
    f = qoder_runner.format_credits_suffix
    assert f({"_turn_credits": 0.0, "_turn_free_credits": 1.25, "_session_credits": 69.78}) == \
        "本轮免费 · 会话累计 69.78 credits"
    assert f({"_turn_credits": 0.39, "_session_credits": 0.39}) == "本轮 0.39 credits"
    assert f({"_session_credits": 12.0}) == "会话累计 12.00 credits"
    assert f({"_turn_credits": 0.0672}) == "本轮 0.07 credits"   # 升级前落盘的老数据
    assert f({}) == ""


def test_context_window_per_model_and_override(monkeypatch):
    monkeypatch.delenv("QODER_CONTEXT_WINDOW", raising=False)
    assert qoder_runner.context_window_for("Qwen3.8-Flash") == 200_000
    assert qoder_runner.context_window_for("performance") == 272_000
    assert qoder_runner.context_window_for(None) == 200_000
    monkeypatch.setenv("QODER_CONTEXT_WINDOW", "1000000")
    assert qoder_runner.context_window_for("Auto") == 1_000_000


def test_resolve_qoder_bin_prefers_configured(monkeypatch):
    assert qoder_runner.resolve_qoder_bin("~/bin/qodercli") == os.path.expanduser("~/bin/qodercli")
    monkeypatch.setattr(qoder_runner.shutil, "which", lambda name: "/usr/local/bin/qodercli")
    assert qoder_runner.resolve_qoder_bin("") == "/usr/local/bin/qodercli"


# ── /usage 套餐额度 ─────────────────────────────────────────────

# 交互式 qodercli 敲 /usage 后实测的面板（光标右移被当成空格前的样子，词粘在一起）
USAGE_SCREEN_SQUASHED = """
Qoder CLI · Usage  Status
APIquotaandsessiontokenusageforthecurrentconversation.

QoderPlan:ProTrial
PlanExpiresAt:Oct19,2026at10:36:35GMT+8
PlanCreditsUsed:30/300
Add-onCreditsUsed:0/100
OrgResourcePackage:N/A
TotalDuration(API):0.0s
"""


def test_parse_usage_screen_with_squashed_spaces():
    u = qoder_runner.parse_qoder_usage_screen(USAGE_SCREEN_SQUASHED)
    assert u == {
        "plan": "Pro Trial",
        "expires": "2026-10-19 10:36",
        "plan_credits": (30.0, 300.0),
        "addon_credits": (0.0, 100.0),
        "org_package": "N/A",
    }


def test_screen_text_turns_cursor_moves_into_spaces_and_keeps_last_redraw():
    raw = (b"\x1b[2J\x1b[1;1HPlan\x1b[1CCredits\x1b[1CUsed:\x1b[1C10/300\r\n"
           b"\x1b[38;5;245mPlan Credits Used: 12.5/300\x1b[0m\r\n")
    text = qoder_runner._screen_text(raw)
    assert "Plan Credits Used: 10/300" in text
    assert qoder_runner.parse_qoder_usage_screen(text)["plan_credits"] == (12.5, 300.0)


def test_usage_lines_render_remaining_credits(monkeypatch):
    import commands
    monkeypatch.setattr(qoder_runner, "fetch_qoder_plan_usage", lambda *a, **k: {
        "plan": "Pro Trial", "expires": "2026-10-19 10:36",
        "plan_credits": (30.0, 300.0), "addon_credits": (0.0, 100.0), "org_package": "N/A",
    })
    lines = commands._qoder_plan_lines(None)
    assert lines[0] == "**订阅额度** — Pro Trial（2026-10-19 10:36 到期）"
    assert any(l.startswith("套餐 credits 剩余") and "90.0%" in l for l in lines)
    assert "已用 30 / 300，剩 270" in lines
    assert "已用 0 / 100，剩 100" in lines
    assert not any("组织资源包" in l for l in lines)


def test_usage_lines_report_failure(monkeypatch):
    import commands

    def boom(*a, **k):
        raise RuntimeError("没读到 /usage 面板")

    monkeypatch.setattr(qoder_runner, "fetch_qoder_plan_usage", boom)
    assert "读取失败" in commands._qoder_plan_lines(None)[0]


def test_session_file_lookup_includes_qoder_projects(tmp_path, monkeypatch):
    import session_store
    claude = tmp_path / "claude_projects"
    qoder = tmp_path / "qoder_projects" / "-Users-me-spx"
    claude.mkdir()
    qoder.mkdir(parents=True)
    (qoder / f"{SID}.jsonl").write_text('{"type":"user","message":{"content":"hi"}}\n')
    monkeypatch.setattr(session_store, "CLAUDE_PROJECTS_DIR", str(claude))
    monkeypatch.setattr(session_store, "QODER_PROJECTS_DIR", str(tmp_path / "qoder_projects"))
    assert session_store._find_session_file(SID) == str(qoder / f"{SID}.jsonl")
    assert session_store._find_session_file("nope") is None


# ── 模型表 ──────────────────────────────────────────────────────

LIST_MODELS_OUT = """MODEL
Auto
Ultimate
Efficient
Qwen4-Max
Kimi-K3.1-Pro
\x1b[2mMiniMax-M3.1-Flash\x1b[0m

"""


def test_parse_model_list_keeps_cli_order():
    assert qoder_runner.parse_qoder_model_list(LIST_MODELS_OUT) == [
        "Auto", "Ultimate", "Efficient", "Qwen4-Max", "Kimi-K3.1-Pro", "MiniMax-M3.1-Flash",
    ]
    # 表头之前的杂行不算；多出来的列只取第一列
    noisy = "Loading models...\nMODEL   RATE\nAuto    0.3x\nSonus   8x\n"
    assert qoder_runner.parse_qoder_model_list(noisy) == ["Auto", "Sonus"]
    assert qoder_runner.parse_qoder_model_list("") == []


def test_model_label_shows_rate_and_off_peak():
    assert qoder_runner.qoder_model_label("Auto") == "Auto 0.3x"
    assert qoder_runner.qoder_model_label("Ultimate") == "Ultimate 2x ◑"
    assert qoder_runner.qoder_model_label("Brand-New-9") == "Brand-New-9"


@pytest.fixture
def live(monkeypatch):
    calls = []

    def fake_fetch(qoder_bin, config_dir, api_key, timeout):
        calls.append((qoder_bin, config_dir, api_key))
        return ("Auto", "Qwen4-Max", "Kimi-K3.1-Pro")

    class SyncThread:  # 后台刷新当场跑完，测试好断言
        def __init__(self, target, args=(), **kwargs):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setenv("CC_LARK_QODER_LIVE_MODELS", "1")
    monkeypatch.setattr(qoder_runner, "_fetch_qoder_models", fake_fetch)
    monkeypatch.setattr(qoder_runner.threading, "Thread", SyncThread)
    monkeypatch.setattr(qoder_runner, "_models_cache", {})
    monkeypatch.setattr(qoder_runner, "_models_refreshing", set())
    monkeypatch.setattr(qoder_runner, "_last_models", ())
    return calls


def test_qoder_models_cache_and_background_refresh(live):
    # 跑任务的路径不等：没表就回空、后台去拉
    assert qoder_runner.qoder_models("/q", wait=False) == ()
    assert len(live) == 1
    assert qoder_runner.known_qoder_models() == ("Auto", "Qwen4-Max", "Kimi-K3.1-Pro")
    # 缓存期内不再跑 CLI
    assert qoder_runner.qoder_models("/q") == ("Auto", "Qwen4-Max", "Kimi-K3.1-Pro")
    assert len(live) == 1
    # 过期：先回旧表，后台重拉
    qoder_runner._expire_models()
    assert qoder_runner.qoder_models("/q") == ("Auto", "Qwen4-Max", "Kimi-K3.1-Pro")
    assert len(live) == 2
    # 换了 qodercli（另一个 profile）各拉各的
    qoder_runner.qoder_models("/other")
    assert live[-1][0] == "/other"


def test_qoder_models_off_switch(monkeypatch):
    monkeypatch.setenv("CC_LARK_QODER_LIVE_MODELS", "0")
    monkeypatch.setattr(qoder_runner, "_fetch_qoder_models", lambda *a: pytest.fail("不该跑 CLI"))
    assert qoder_runner.qoder_models() == ()
    assert qoder_runner.known_qoder_models() == ()


def test_fetch_qoder_models_failure_is_empty(monkeypatch):
    monkeypatch.setattr(qoder_runner, "QODER_USAGE_CWD", "/tmp")
    assert qoder_runner._fetch_qoder_models("/nonexistent/qodercli", None, None, 5) == ()


def test_run_qoder_uses_cli_spelling_of_model(monkeypatch, live):
    qoder_runner.qoder_models()
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM_LINES)], captured)
    asyncio.run(run_qoder(message="hi", model="qwen4-max", cwd="/tmp"))
    assert _flag(captured["cmd"], "-m") == "Qwen4-Max"


def test_run_qoder_flags_retired_model_fallback(monkeypatch):
    stderr = (b'Model "Kimi-K3" is not available right now; using "auto" instead. '
              b'Use /model to choose another model.\n')
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM_LINES, stderr=stderr)], captured)
    text, _, _ = asyncio.run(run_qoder(message="hi", model="Kimi-K3", cwd="/tmp"))
    assert text.startswith("hello\n\n⚠️ Qoder 已经没有模型 `Kimi-K3` 了，这一轮实际用的是 `auto`")
    assert "/model" in text


def test_run_qoder_without_fallback_has_no_notice(monkeypatch):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM_LINES, stderr=b"some other warning\n")], captured)
    text, _, _ = asyncio.run(run_qoder(message="hi", model="Auto", cwd="/tmp"))
    assert text == "hello"
