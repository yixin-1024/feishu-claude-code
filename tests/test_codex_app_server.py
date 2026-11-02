"""codex app-server 传输：用假 app-server 子进程回放真机录下来的 JSON-RPC 流。

假进程见 tests/fake_codex_app_server.py；fixture 是 codex 0.164 真机录制的 turn 帧。
走的是完整的 run_codex → 子进程 → 管道 → 解析路径，不 mock asyncio 子进程。
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import codex_runner
import codex_quota_watcher
from auto_continue import DONE_SENTINEL, IncompleteTaskError
from codex_runner import run_codex

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_codex_app_server.py")
FINAL_ANSWER = "The secret word is MAGENTA-42, and the uname output is Darwin.\nPINEAPPLE"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """返回 setup(scenario) → (codex_bin, read_log)。"""
    monkeypatch.setenv("CODEX_TRANSPORT", "app-server")
    monkeypatch.setenv("CODEX_AUTO_CONTINUE", "0")
    monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)
    pushed = []
    monkeypatch.setattr(codex_runner, "_on_rate_limits_pushed", lambda snap: pushed.append(snap))
    log = tmp_path / "client.jsonl"

    def setup(scenario):
        path = tmp_path / "scenario.json"
        path.write_text(json.dumps(scenario))
        wrapper = tmp_path / "codex"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE}" "$@"\n')
        wrapper.chmod(0o755)
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", str(path))
        monkeypatch.setenv("FAKE_CODEX_LOG", str(log))

        def read_log():
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

        return str(wrapper), read_log

    setup.pushed = pushed
    return setup


def _requests(entries, method):
    return [e["msg"] for e in entries if e["msg"].get("method") == method]


def _run(**kwargs):
    return asyncio.run(run_codex(**kwargs))


def test_first_turn_streams_tools_usage_and_final(fake):
    codex_bin, read_log = fake({"turns": [{"fixture": "turn_read_and_run.json"}]})
    chunks, tools, usages, procs = [], [], [], []

    text, thread_id, fresh = _run(
        message="read secret.txt and run uname",
        cwd="/tmp",
        model="gpt-6-astra",
        reasoning_effort="xhigh",
        append_system_prompt="SYSTEM PROMPT",
        codex_bin=codex_bin,
        on_text_chunk=chunks.append,
        on_tool_use=lambda name, inp: tools.append((name, dict(inp))),
        on_usage=usages.append,
        on_process_start=procs.append,
    )

    assert text == FINAL_ANSWER
    assert thread_id == "019a0000-0000-7000-8000-00000000cafe"
    assert fresh is False
    streamed = "".join(chunks)
    assert streamed.startswith("I’ll read `secret.txt` and run `uname -s`.")
    assert streamed.endswith(FINAL_ANSWER)
    # 两条命令各有 开始 / 完成，完成态带退出码和输出
    cmds = [(n, t["command"], t["status"]) for n, t in tools if n == "bash"]
    assert ("bash", "/bin/zsh -lc 'cat secret.txt'", "in_progress") in cmds
    assert ("bash", "/bin/zsh -lc 'cat secret.txt'", "completed") in cmds
    done = next(t for n, t in tools if n == "bash" and "secret" in t["command"] and t["status"] == "completed")
    assert done["exit_code"] == 0 and "MAGENTA-42" in done["output"]
    # 上下文占用取 last，带模型窗口
    assert usages[-1]["_context_window"] == 258400
    assert usages[-1]["input_tokens"] > 0
    assert fake.pushed and fake.pushed[-1]["limitId"] == "codex"
    assert procs and procs[0].returncode is not None  # 进程已收掉

    log = read_log()
    assert log[0]["argv"] == ["app-server"]  # 私聊无 thread：不注入 cc-lark MCP
    start = _requests(log, "thread/start")[0]["params"]
    assert start["sandbox"] == "danger-full-access"
    assert start["approvalPolicy"] == "never"
    assert start["developerInstructions"] == "SYSTEM PROMPT"
    assert start["model"] == "gpt-6-astra" and start["cwd"] == "/tmp"
    turn = _requests(log, "turn/start")[0]["params"]
    # 系统提示走 developerInstructions，不再拼进用户消息
    assert turn["input"][0]["text"] == "read secret.txt and run uname"
    assert turn["effort"] == "xhigh" and turn["model"] == "gpt-6-astra"
    assert not _requests(log, "thread/resume")


def test_resume_uses_native_thread_and_excludes_turns(fake):
    codex_bin, read_log = fake({"turns": [{"fixture": "turn_resume_recall.json"}]})
    text, thread_id, fresh = _run(
        message="what was the secret word?", session_id="exec-made-thread-id", codex_bin=codex_bin,
    )
    assert text == "The secret word was MAGENTA-42.\nPINEAPPLE"
    assert thread_id == "exec-made-thread-id" and fresh is False
    log = read_log()
    resume = _requests(log, "thread/resume")[0]["params"]
    assert resume["threadId"] == "exec-made-thread-id" and resume["excludeTurns"] is True
    assert not _requests(log, "thread/start")


def test_resume_of_missing_thread_starts_fresh_and_says_so(fake):
    codex_bin, read_log = fake({
        "resume_error": "no rollout found for thread id gone",
        "turns": [{"fixture": "turn_resume_recall.json"}],
    })
    _, thread_id, fresh = _run(message="hi", session_id="gone", codex_bin=codex_bin)
    assert fresh is True
    assert thread_id == "019a0000-0000-7000-8000-00000000cafe"
    assert len(_requests(read_log(), "thread/start")) == 1


def test_stop_interrupts_turn_and_process_exits(fake):
    frames = json.load(open(os.path.join(os.path.dirname(FAKE), "fixtures", "codex_app_server",
                                         "turn_long_command.json")))
    # long_command 停在命令开始；只有 interrupt 能结束这个 turn
    codex_bin, read_log = fake({"turns": [{"frames": frames + [{"__wait__": "interrupt"}]}]})
    inputs, procs = [], []

    stoppers = []

    async def on_tool(name, inp):
        # /stop 从另一个任务进来（dispatcher 的 stop_run），不在 runner 的回调里
        if name == "bash" and inp["status"] == "in_progress":
            stoppers.append(asyncio.create_task(inputs[0].stop()))

    text, _, _ = _run(
        message="sleep 60", codex_bin=codex_bin,
        on_input_ready=inputs.append, on_tool_use=on_tool, on_process_start=procs.append,
    )
    assert "I’ll run the command" in text or text == ""
    assert inputs[0].stopped and not inputs[0].open
    assert len(_requests(read_log(), "turn/interrupt")) == 1
    assert procs[0].returncode is not None


def test_inject_mid_turn_uses_turn_steer(fake):
    frames = json.load(open(os.path.join(os.path.dirname(FAKE), "fixtures", "codex_app_server",
                                         "turn_long_command.json")))
    finish = [
        {"method": "item/completed", "params": {"threadId": "$THREAD", "turnId": "$TURN", "item": {
            "type": "agentMessage", "id": "m-final", "text": "done. BANANA", "phase": "final_answer"}}},
        {"method": "turn/completed", "params": {"threadId": "$THREAD", "turn": {
            "id": "$TURN", "items": [], "status": "completed"}}},
    ]
    codex_bin, read_log = fake({"turns": [{"frames": frames + [{"__wait__": "steer"}] + finish}]})
    inputs, uids, results = [], [], []

    async def ready(run_input):
        inputs.append(run_input)
        run_input.on_turn_result = results.append

    async def on_tool(name, inp):
        if name == "bash" and inp["status"] == "in_progress" and not uids:
            uids.append(await inputs[0].send("also say BANANA", "next"))

    text, _, _ = _run(message="sleep", codex_bin=codex_bin, on_input_ready=ready, on_tool_use=on_tool)
    assert text == "done. BANANA"
    steer = _requests(read_log(), "turn/steer")[0]["params"]
    assert steer["expectedTurnId"] == "019a0000-0000-7000-8000-000000000000"
    assert steer["clientUserMessageId"] == uids[0]
    assert steer["input"][0]["text"] == "also say BANANA"
    assert inputs[0].undelivered() == []
    assert results == ["done. BANANA"]
    assert len(_requests(read_log(), "turn/start")) == 1


def test_inject_after_turn_ended_runs_follow_up_turn(fake):
    """steer 被拒（turn 刚好收尾）：同一进程里接着开一个 turn 处理这条插话。"""
    frames = json.load(open(os.path.join(os.path.dirname(FAKE), "fixtures", "codex_app_server",
                                         "turn_read_and_run.json")))
    follow = [
        {"method": "item/completed", "params": {"threadId": "$THREAD", "turnId": "$TURN", "item": {
            "type": "agentMessage", "id": "m2", "text": "follow-up handled", "phase": "final_answer"}}},
        {"method": "turn/completed", "params": {"threadId": "$THREAD", "turn": {
            "id": "$TURN", "items": [], "status": "completed"}}},
    ]
    codex_bin, read_log = fake({"reject_steer": True, "turns": [{"frames": frames}, {"frames": follow}]})
    inputs, results, sent = [], [], []

    async def ready(run_input):
        inputs.append(run_input)
        run_input.on_turn_result = results.append

    async def on_tool(name, inp):
        if name == "bash" and not sent:
            sent.append(await inputs[0].send("one more thing", "next"))

    text, _, _ = _run(message="go", codex_bin=codex_bin, on_input_ready=ready, on_tool_use=on_tool)
    assert sent[0]  # 已受理（排到下一个 turn）
    assert results == [FINAL_ANSWER, "follow-up handled"]
    assert text == "follow-up handled"
    starts = _requests(read_log(), "turn/start")
    assert len(starts) == 2 and starts[1]["params"]["input"][0]["text"] == "one more thing"


def test_bang_steer_interrupts_and_restarts_with_new_instruction(fake):
    frames = json.load(open(os.path.join(os.path.dirname(FAKE), "fixtures", "codex_app_server",
                                         "turn_long_command.json")))
    second = [
        {"method": "item/completed", "params": {"threadId": "$THREAD", "turnId": "$TURN", "item": {
            "type": "agentMessage", "id": "m3", "text": "switched", "phase": "final_answer"}}},
        {"method": "turn/completed", "params": {"threadId": "$THREAD", "turn": {
            "id": "$TURN", "items": [], "status": "completed"}}},
    ]
    codex_bin, read_log = fake({"turns": [{"frames": frames + [{"__wait__": "interrupt"}]},
                                          {"frames": second}]})
    inputs = []

    async def on_tool(name, inp):
        if name == "bash" and inp["status"] == "in_progress":
            await inputs[0].steer("do this instead")

    chunks = []
    text, _, _ = _run(message="sleep", codex_bin=codex_bin, on_input_ready=inputs.append, on_tool_use=on_tool,
                      on_text_chunk=chunks.append)
    assert text == "switched"
    # 新 turn 的正文另起一段（真机上见过 "...output.KIWI" 粘连）
    assert "".join(chunks).endswith("PINEAPPLE\n\nswitched")
    log = read_log()
    assert len(_requests(log, "turn/interrupt")) == 1
    starts = _requests(log, "turn/start")
    assert [s["params"]["input"][0]["text"] for s in starts] == ["sleep", "do this instead"]


def test_failed_turn_without_text_raises_codex_error(fake):
    err = {"message": "You've hit your usage limit. Try again at 3:00 PM.",
           "codexErrorInfo": "usageLimitExceeded"}
    codex_bin, _ = fake({"turns": [{"frames": [
        {"method": "error", "params": {"threadId": "$THREAD", "turnId": "$TURN", "willRetry": False, "error": err}},
        {"method": "turn/completed", "params": {"threadId": "$THREAD", "turn": {
            "id": "$TURN", "items": [], "status": "failed", "error": err}}},
    ]}]})
    with pytest.raises(RuntimeError, match="usage limit"):
        _run(message="hi", codex_bin=codex_bin)


def test_failed_turn_with_partial_text_is_incomplete_and_keeps_session(fake):
    codex_bin, _ = fake({"turns": [{"frames": [
        {"method": "item/agentMessage/delta", "params": {"threadId": "$THREAD", "turnId": "$TURN",
                                                          "itemId": "m1", "delta": "working on it"}},
        {"method": "turn/completed", "params": {"threadId": "$THREAD", "turn": {
            "id": "$TURN", "items": [], "status": "failed",
            "error": {"message": "stream disconnected", "codexErrorInfo": {"responseStreamDisconnected": {}}}}}},
    ]}]})
    with pytest.raises(IncompleteTaskError) as exc:
        _run(message="hi", codex_bin=codex_bin)
    assert "stream disconnected" in str(exc.value)
    assert exc.value.cc_session_id == "019a0000-0000-7000-8000-00000000cafe"


def test_retrying_error_shows_status_then_clears(fake):
    codex_bin, _ = fake({"turns": [{"frames": [
        {"method": "error", "params": {"threadId": "$THREAD", "turnId": "$TURN", "willRetry": True,
                                       "error": {"message": "Reconnecting... 1/5"}}},
        {"method": "turn/completed", "params": {"threadId": "$THREAD", "turn": {
            "id": "$TURN", "items": [{"type": "agentMessage", "id": "m", "text": "ok", "phase": "final_answer"}],
            "status": "completed"}}},
    ]}]})
    statuses = []
    text, _, _ = _run(message="hi", codex_bin=codex_bin, on_status=lambda lvl, label: statuses.append((lvl, label)))
    assert text == "ok"  # 流里漏掉的最终消息从 turn/completed 的 items 兜回来
    assert statuses[0][0] == "warn" and "Reconnecting" in statuses[0][1]
    assert statuses[-1] == ("clear", "")


def test_approval_requests_are_declined(fake):
    codex_bin, read_log = fake({"turns": [{"frames": [
        {"__server_request__": {"method": "item/commandExecution/requestApproval",
                                "params": {"threadId": "$THREAD", "turnId": "$TURN", "itemId": "c1",
                                           "command": "rm -rf build"}}},
        {"method": "turn/completed", "params": {"threadId": "$THREAD", "turn": {
            "id": "$TURN", "items": [{"type": "agentMessage", "id": "m", "text": "skipped", "phase": "final_answer"}],
            "status": "completed"}}},
    ]}]})
    tools = []
    _run(message="hi", codex_bin=codex_bin, on_tool_use=lambda n, i: tools.append((n, i)))
    reply = next(e["msg"] for e in read_log() if e["msg"].get("id") == "srv-1")
    assert reply["result"] == {"decision": "decline"}
    assert tools and tools[0][0] == "codex_approval" and "rm -rf build" in tools[0][1]["command"]


def test_process_crash_mid_turn_raises(fake):
    codex_bin, _ = fake({"turns": [{"frames": [
        {"method": "item/agentMessage/delta", "params": {"threadId": "$THREAD", "turnId": "$TURN",
                                                          "itemId": "m1", "delta": "partial"}},
        {"__exit__": 3},
    ]}]})
    with pytest.raises(RuntimeError, match="意外退出"):
        _run(message="hi", codex_bin=codex_bin)


def test_auto_continue_reuses_the_same_process(fake, monkeypatch):
    def turn(text):
        return {"frames": [
            {"method": "item/agentMessage/delta", "params": {"threadId": "$THREAD", "turnId": "$TURN",
                                                              "itemId": "m", "delta": text}},
            {"method": "turn/completed", "params": {"threadId": "$THREAD", "turn": {
                "id": "$TURN", "items": [], "status": "completed"}}},
        ]}

    codex_bin, read_log = fake({"turns": [turn("step 1 done, next step 2"),
                                          turn(f"all done\n{DONE_SENTINEL}")]})
    monkeypatch.setenv("CODEX_AUTO_CONTINUE", "1")
    chunks, procs = [], []
    text, _, _ = _run(message="long task", codex_bin=codex_bin,
                      on_text_chunk=chunks.append, on_process_start=procs.append)
    assert text == "all done"
    assert DONE_SENTINEL not in "".join(chunks)
    assert "自动续跑 · 第 2 轮" in "".join(chunks)
    assert len(procs) == 1  # 续跑不再重拉进程
    log = read_log()
    assert len(_requests(log, "turn/start")) == 2
    assert DONE_SENTINEL in _requests(log, "thread/start")[0]["params"]["developerInstructions"]


def test_mcp_flags_are_passed_to_app_server(fake, monkeypatch):
    codex_bin, read_log = fake({"turns": [{"fixture": "turn_resume_recall.json"}]})
    monkeypatch.setenv("CC_LARK_WAKE_MCP", "1")
    _run(message="hi", codex_bin=codex_bin, extra_env={"CC_LARK_THREAD_ID": "omt_x", "CC_LARK_PROFILE": "p"})
    argv = read_log()[0]["argv"]
    assert argv[0] == "app-server"
    assert any(a.startswith("mcp_servers.cc-lark.command=") for a in argv)
    assert "mcp_servers.cc-lark.env.CC_LARK_THREAD_ID=\"omt_x\"" in argv


def test_dangerous_bypass_maps_to_full_access_and_never(fake):
    codex_bin, read_log = fake({"turns": [{"fixture": "turn_resume_recall.json"}]})
    _run(message="hi", codex_bin=codex_bin, sandbox_mode="read-only", approval_policy="on-request",
         dangerous_bypass_level=2)
    p = _requests(read_log(), "thread/start")[0]["params"]
    assert (p["sandbox"], p["approvalPolicy"]) == ("danger-full-access", "never")


def test_profile_sandbox_and_approval_without_bypass(fake):
    codex_bin, read_log = fake({"turns": [{"fixture": "turn_resume_recall.json"}]})
    _run(message="hi", codex_bin=codex_bin, sandbox_mode="workspace-write", approval_policy="on-request",
         dangerous_bypass_level=0)
    p = _requests(read_log(), "thread/start")[0]["params"]
    assert (p["sandbox"], p["approvalPolicy"]) == ("workspace-write", "on-request")


# ── 纯逻辑 ────────────────────────────────────────────────────────────────


def test_transport_selection(monkeypatch):
    monkeypatch.delenv("CODEX_TRANSPORT", raising=False)
    monkeypatch.delenv("P_CODEX_TRANSPORT", raising=False)
    assert codex_runner.codex_transport({}) == codex_runner.DEFAULT_TRANSPORT
    monkeypatch.setenv("CODEX_TRANSPORT", "exec")
    assert codex_runner.codex_transport({"CC_LARK_PROFILE": "p"}) == "exec"
    monkeypatch.setenv("P_CODEX_TRANSPORT", "app_server")
    assert codex_runner.codex_transport({"CC_LARK_PROFILE": "p"}) == "app-server"
    monkeypatch.setenv("P_CODEX_TRANSPORT", "bogus")
    assert codex_runner.codex_transport({"CC_LARK_PROFILE": "p"}) == codex_runner.DEFAULT_TRANSPORT


def test_resolve_codex_bin_finds_chatgpt_bundle(monkeypatch):
    monkeypatch.setattr(codex_runner.shutil, "which", lambda _: None)
    bundled = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
    monkeypatch.setattr(codex_runner.os.path, "exists", lambda p: p == bundled)
    assert codex_runner.resolve_codex_bin() == bundled


def test_sentinel_prefix_is_held_back_while_streaming():
    from codex_app_server import _TurnText
    tt = _TurnText(DONE_SENTINEL)
    tt.delta("m", "done\n" + DONE_SENTINEL[:3])
    assert tt.next_chunk() == "done\n"
    tt.delta("m", DONE_SENTINEL[3:])
    assert tt.next_chunk() == ""
    assert tt.saw_sentinel and tt.final() == "done"


def test_observe_rate_limits_filters_throttles_and_detects_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_quota_watcher, "_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(codex_quota_watcher, "_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(codex_quota_watcher, "_STATE_LOCK_FILE", str(tmp_path / "state.lock"))
    monkeypatch.setattr(codex_quota_watcher, "_PUSHED_FILE", str(tmp_path / "pushed.json"))
    monkeypatch.setattr(codex_quota_watcher, "_last_observed", 0.0)
    sent = []
    monkeypatch.setattr(codex_quota_watcher, "_send_fn", sent.append)
    (tmp_path / "state.json").write_text(json.dumps({"primary": {"used": 97.0, "resets": 1000}}))

    assert codex_quota_watcher.observe_rate_limits({"limitId": "gpt-x-mini", "primary": {"usedPercent": 1}}) is False
    snap = {"limitId": "codex", "primary": {"usedPercent": 3, "windowDurationMins": 10080, "resetsAt": 999999}}
    assert codex_quota_watcher.observe_rate_limits(snap) is True
    assert sent and "额度已重置" in sent[0]
    assert codex_quota_watcher.read_pushed_rate_limits()["primary"]["usedPercent"] == 3
    # 60s 内的下一份推送被节流
    assert codex_quota_watcher.observe_rate_limits(snap) is False


def test_trace_records_frames_when_enabled(fake, tmp_path, monkeypatch):
    codex_bin, _ = fake({"turns": [{"fixture": "turn_resume_recall.json"}]})
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("CODEX_APP_SERVER_TRACE", str(trace))
    _run(message="hi", session_id="t1", codex_bin=codex_bin)
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    methods = [(r["dir"], r["msg"].get("method")) for r in rows]
    assert ("out", "thread/resume") in methods and ("in", "turn/completed") in methods
    assert ("in", "item/agentMessage/delta") not in methods  # 正文增量不记
