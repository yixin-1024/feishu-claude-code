import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kiro_runner
from kiro_runner import run_kiro


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
        self.pid = 4343

    async def wait(self):
        self.returncode = self._final
        return self.returncode

    def kill(self):
        self.returncode = -9


SID = "7b322660-157c-44e3-b754-1ac6484044c4"


def _j(obj):
    return (json.dumps(obj) + "\n").encode()


def _upd(update, sid=SID):
    return _j({"type": "sessionUpdate", "data": {"sessionId": sid, "update": update}})


# 照 kiro-cli 2.28.0 `chat --output-format stream-json` 实测输出裁剪
STREAM = [
    _j({"type": "runStarted", "data": {"payloadSchema": "acp", "engine": "v2"}}),
    _j({"type": "metadata", "data": {"sessionId": SID, "contextUsagePercentage": 5.3}}),
    _upd({"sessionUpdate": "tool_call", "toolCallId": "tooluse_1", "title": "Running: echo hi",
          "kind": "execute", "rawInput": {"command": "echo hi", "__tool_use_purpose": "say hi"},
          "_meta": {"kiro": {"toolName": "shell"}}}),
    _upd({"sessionUpdate": "tool_call_update", "toolCallId": "tooluse_1", "status": "completed",
          "rawInput": {"command": "echo hi"}, "_meta": {"kiro": {"toolName": "shell"}}}),
    _upd({"sessionUpdate": "tool_call", "toolCallId": "tooluse_2", "title": "Running: @cc-lark/list_crons",
          "rawInput": {"__tool_use_purpose": "check crons"},
          "_meta": {"kiro": {"toolName": "list_crons", "mcpServerName": "cc-lark"}}}),
    _upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "DO"}}),
    _upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "NE"}}),
    _upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "子agent"}}, sid="other"),
    _j({"type": "metadata", "data": {"sessionId": SID, "contextUsagePercentage": 8.88,
                                     "meteringUsage": [{"value": 0.026, "unit": "credit"},
                                                       {"value": 0.0127, "unit": "credit"}]}}),
    _j({"type": "runFinished", "data": {"sessionId": SID, "status": "success",
                                        "stopReason": "end_turn", "finalText": "DONE",
                                        "finalTextTruncated": False}}),
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
def agents_dir(tmp_path):
    return str(tmp_path / "agents")


def _run(agents_dir, **kw):
    kw.setdefault("message", "hello")
    kw.setdefault("cwd", "/tmp")
    return asyncio.run(run_kiro(agents_dir=agents_dir, **kw))


def test_streams_text_tools_usage_and_cleans_up_agent(monkeypatch, agents_dir):
    captured, chunks, tools, usages, agent_seen = {}, [], [], [], {}
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)

    def on_start(proc):
        # 进程启动那一刻临时 agent 必须已经在盘上
        files = os.listdir(agents_dir)
        agent_seen["files"] = files
        agent_seen["cfg"] = json.load(open(os.path.join(agents_dir, files[0])))

    text, sid, fallback = _run(
        agents_dir,
        on_text_chunk=chunks.append, on_tool_use=lambda n, i: tools.append((n, i)),
        on_usage=usages.append, on_process_start=on_start,
        append_system_prompt="SYS PROMPT", model="claude-haiku-4.5",
    )
    assert (text, sid, fallback) == ("DONE", SID, False)
    assert "".join(chunks) == "DONE"  # 子 agent（别的 sessionId）的流不进正文
    assert tools == [("shell", {"command": "echo hi"}),
                     ("mcp__cc-lark__list_crons", {"purpose": "check crons"})]
    u = usages[-1]
    assert u["_turn_credits"] == pytest.approx(0.0387)
    assert u["_context_window"] == 200_000
    assert u["_context_tokens"] == int(round(0.0888 * 200_000))
    assert captured["procs"][0].stdin.data == b"hello" and captured["procs"][0].stdin.closed
    assert agent_seen["cfg"]["prompt"] == "SYS PROMPT"
    assert agent_seen["cfg"]["tools"] == ["*"] and agent_seen["cfg"]["allowedTools"] == ["*"]
    assert any(r.startswith("skill://") for r in agent_seen["cfg"]["resources"])
    assert os.listdir(agents_dir) == []  # 跑完删掉


def test_builds_expected_argv(monkeypatch, agents_dir):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)
    _run(agents_dir, session_id=SID, model="claude-opus-5.5", effort="high", kiro_bin="/opt/kiro-cli")
    cmd = captured["calls"][0]["cmd"]
    assert cmd[:2] == ["/opt/kiro-cli", "chat"]
    assert cmd[cmd.index("--agent-engine") + 1] == "v2"
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--agent") + 1].startswith("cc-lark-")
    assert "--trust-all-tools" in cmd
    assert cmd[cmd.index("--resume-id") + 1] == SID
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5.5"
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert captured["calls"][0]["env"]["CC_LARK_MIRROR_OFF"] == "1"


def test_injects_cc_lark_mcp_into_temp_agent(monkeypatch, agents_dir):
    captured, cfg = {}, {}
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)

    def on_start(proc):
        f = os.listdir(agents_dir)[0]
        cfg.update(json.load(open(os.path.join(agents_dir, f))))

    _run(agents_dir, on_process_start=on_start,
         extra_env={"CC_LARK_THREAD_ID": "omt_k", "CC_LARK_PROFILE": "kiro", "CC_LARK_CONTROL_TOKEN": "x"})
    server = cfg["mcpServers"]["cc-lark"]
    assert server["args"][0].endswith("cc_mcp_server.py")
    assert server["env"]["CC_LARK_THREAD_ID"] == "omt_k"
    assert "CC_LARK_CONTROL_TOKEN" not in server["env"]


def test_no_thread_means_no_mcp(monkeypatch, agents_dir):
    captured, cfg = {}, {}
    _patch_exec(monkeypatch, [FakeProc(STREAM)], captured)
    _run(agents_dir, on_process_start=lambda p: cfg.update(
        json.load(open(os.path.join(agents_dir, os.listdir(agents_dir)[0])))))
    assert cfg["mcpServers"] == {}


def test_resume_not_found_falls_back_to_fresh_session(monkeypatch, agents_dir):
    captured = {}
    missing = [
        _j({"type": "runStarted", "data": {"engine": "v2"}}),
        _j({"type": "runError", "data": {"sessionId": None, "stage": "init",
                                         "message": 'Internal error: "Failed to start session: Session not found: x"'}}),
    ]
    _patch_exec(monkeypatch, [FakeProc(missing, stderr=b"error: ACP load_session failed"), FakeProc(STREAM)], captured)
    text, sid, fallback = _run(agents_dir, session_id="11111111-0000-0000-0000-000000000000")
    assert (text, sid, fallback) == ("DONE", SID, True)
    assert "--resume-id" in captured["calls"][0]["cmd"]
    assert "--resume-id" not in captured["calls"][1]["cmd"]
    assert os.listdir(agents_dir) == []


def test_run_error_raises_with_resumable_session(monkeypatch, agents_dir):
    captured = {}
    lines = [
        _j({"type": "metadata", "data": {"sessionId": SID, "contextUsagePercentage": 5.3}}),
        _j({"type": "runError", "data": {"sessionId": SID, "stage": "prompt",
                                         "message": "Encountered an error in the response stream: throttled"}}),
    ]
    _patch_exec(monkeypatch, [FakeProc(lines)], captured)
    with pytest.raises(RuntimeError) as ei:
        _run(agents_dir, session_id=SID)
    assert "throttled" in str(ei.value)
    assert ei.value.cc_session_id == SID
    assert ei.value.cc_retryable_resume is True
    assert os.listdir(agents_dir) == []


def test_unknown_model_error_is_not_retried(monkeypatch, agents_dir):
    captured = {}
    lines = [
        _j({"type": "metadata", "data": {"sessionId": SID}}),
        _j({"type": "runError", "data": {"sessionId": SID, "stage": "prompt",
                                         "message": "The model 'nope' is not available. Please use '/model'"}}),
    ]
    _patch_exec(monkeypatch, [FakeProc(lines)], captured)
    with pytest.raises(RuntimeError) as ei:
        _run(agents_dir, model="nope")
    assert ei.value.cc_retryable_resume is False


def test_v3_events_still_parsed(monkeypatch, agents_dir):
    captured, usages, chunks = {}, [], []
    lines = [
        _upd({"sessionUpdate": "session_info_update",
              "_meta": {"kiro": {"contextUsage": {"usagePercentage": 22.9}, "kind": "context_usage"}}}),
        _upd({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hey"}}),
        _upd({"sessionUpdate": "session_info_update",
              "_meta": {"kiro": {"promptTurnSummaries": [{"unit": "credit", "usage": 0.0587}]}}}),
        _j({"type": "runFinished", "data": {"sessionId": SID, "status": "success", "finalText": "Hey"}}),
    ]
    _patch_exec(monkeypatch, [FakeProc(lines)], captured)
    text, sid, _ = _run(agents_dir, on_usage=usages.append, on_text_chunk=chunks.append)
    assert text == "Hey" and sid == SID and chunks == ["Hey"]
    assert usages[-1]["_turn_credits"] == pytest.approx(0.0587)
    assert usages[-1]["_context_ratio"] == pytest.approx(0.229)


def test_nonzero_exit_without_text_raises(monkeypatch, agents_dir):
    captured = {}
    _patch_exec(monkeypatch, [FakeProc([], returncode=1, stderr=b"error: not logged in")], captured)
    with pytest.raises(RuntimeError) as ei:
        _run(agents_dir)
    assert "not logged in" in str(ei.value)


def test_rejects_unknown_effort():
    with pytest.raises(ValueError):
        kiro_runner._normalize_effort("ultra")
    assert kiro_runner._normalize_effort("auto") is None
    assert kiro_runner._normalize_effort("MAX") == "max"


def test_context_window_per_model_and_override(monkeypatch):
    assert kiro_runner.context_window_for("auto") == 1_000_000
    assert kiro_runner.context_window_for("claude-haiku-4.5") == 200_000
    monkeypatch.setenv("KIRO_CONTEXT_WINDOW", "300000")
    assert kiro_runner.context_window_for("auto") == 300_000


def test_stale_temp_agents_are_cleaned(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    old = d / "cc-lark-deadbeef.json"
    old.write_text("{}")
    keep = d / "my-agent.json"
    keep.write_text("{}")
    os.utime(old, (1, 1))
    name, path = kiro_runner.write_temp_agent("p", None, str(d))
    assert not old.exists() and keep.exists() and os.path.exists(path)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_resolve_kiro_bin_prefers_configured(monkeypatch):
    assert kiro_runner.resolve_kiro_bin("~/bin/kiro-cli") == os.path.expanduser("~/bin/kiro-cli")
    monkeypatch.setattr(kiro_runner.shutil, "which", lambda name: "/usr/local/bin/kiro-cli")
    assert kiro_runner.resolve_kiro_bin(None) == "/usr/local/bin/kiro-cli"


def test_default_effort_from_env_when_not_overridden(monkeypatch, agents_dir):
    monkeypatch.setenv("KIRO_EFFORT", "medium")
    captured = {}
    _patch_exec(monkeypatch, [FakeProc(STREAM), FakeProc(STREAM)], captured)
    _run(agents_dir, extra_env={"CC_LARK_PROFILE": "kiro"})
    cmd = captured["calls"][0]["cmd"]
    assert cmd[cmd.index("--effort") + 1] == "medium"
    _run(agents_dir, effort="max", extra_env={"CC_LARK_PROFILE": "kiro"})
    cmd = captured["calls"][1]["cmd"]
    assert cmd[cmd.index("--effort") + 1] == "max"


def test_default_effort_ignores_bad_value(monkeypatch):
    monkeypatch.setenv("KIRO_EFFORT", "turbo")
    assert kiro_runner.default_effort("kiro") is None
    monkeypatch.setenv("KIRO_KIRO_EFFORT", "high")
    assert kiro_runner.default_effort("kiro") == "high"


USAGE_SCREEN = """
› /usage
────────────────────────────────────────
 Estimated Usage | resets on 2026-11-01 | KIRO PRO
 Credits (0.47 of 1000 covered in plan)
 ████████████████ 0.0%
 Since your account is through your organization, for account management please contact your account administrator.
 esc to close                     Tab to switch to /context
"""


def test_parse_usage_screen():
    u = kiro_runner.parse_kiro_usage_screen(USAGE_SCREEN)
    assert u["plan"] == "KIRO PRO"
    assert u["resets"] == "2026-11-01"
    assert u["plan_credits"] == (0.47, 1000.0)


def test_usage_lines_render_remaining_credits(monkeypatch):
    import commands
    monkeypatch.setattr(kiro_runner, "fetch_kiro_plan_usage", lambda *a, **k: {
        "plan": "KIRO PRO", "resets": "2026-11-01", "plan_credits": (12.5, 1000.0)})
    lines = commands._kiro_plan_lines(None)
    assert lines[0] == "**订阅额度** — KIRO PRO（2026-11-01 重置）"
    assert "已用 12.5 / 1000，剩 987.5" in lines


def test_usage_lines_report_failure(monkeypatch):
    import commands

    def boom(*a, **k):
        raise RuntimeError("no panel")
    monkeypatch.setattr(kiro_runner, "fetch_kiro_plan_usage", boom)
    assert "读取失败" in commands._kiro_plan_lines(None)[0]
