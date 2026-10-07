"""print 后端的「运行中插话」（--input-format stream-json + RunInput）单测。

事件形状照 CLI 2.1.289 实测：command_lifecycle 的 completed 在 result 之后才到，
result 带 user_message_uuids / queued_turn_count；被 now / interrupt 打断的那一轮
result 是 terminal_reason=aborted_*（interrupt 的还是 error_during_execution）。
"""

import asyncio
import json
import os
import sys

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")
os.environ["CLAUDE_RUNNER"] = "print"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import claude_runner
from claude_runner import RunInput, run_claude


class FakeStdin:
    def __init__(self):
        self.lines: list[dict] = []
        self.closed = False

    def write(self, data: bytes):
        assert not self.closed, "写入已关闭的 stdin"
        for line in data.decode().splitlines():
            self.lines.append(json.loads(line))

    async def drain(self):
        return None

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True


class QueueStdout:
    """测试按剧本逐行喂 stdout；stdin 关了且剧本放完 = EOF（像真 CLI 一样退出）。"""

    def __init__(self, proc):
        self._proc = proc
        self.q: asyncio.Queue = asyncio.Queue()

    def feed(self, obj: dict):
        self.q.put_nowait((json.dumps(obj) + "\n").encode())

    async def readline(self):
        while True:
            if not self.q.empty():
                return self.q.get_nowait()
            if self._proc.stdin.closed:
                self._proc.returncode = 0
                return b""
            await asyncio.sleep(0.005)


class FakeStderr:
    async def read(self):
        return b""


class FakeProc:
    pid = 999999

    def __init__(self):
        self.stdin = FakeStdin()
        self.stdout = QueueStdout(self)
        self.stderr = FakeStderr()
        self.returncode = None

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _delta(text):
    return {"type": "stream_event", "event": {"type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text}}}


def _result(text, uuids, queued=0, **extra):
    return {"type": "result", "subtype": "success", "is_error": False, "session_id": "sid_1",
            "result": text, "user_message_uuids": uuids, "queued_turn_count": queued,
            "terminal_reason": "completed", **extra}


def _life(uid, state):
    return {"type": "command_lifecycle", "command_uuid": uid, "state": state}


@pytest.fixture
def fake_proc(monkeypatch):
    monkeypatch.setenv("CLAUDE_PRINT_STREAM_INPUT", "1")
    proc = FakeProc()
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    proc.captured = captured
    return proc


async def _wait_for(pred, timeout=2.0):
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.005)


async def test_single_turn_writes_json_and_closes_after_result(fake_proc):
    got = {}

    async def on_ready(run_input):
        got["input"] = run_input

    task = asyncio.create_task(run_claude("你好", on_input_ready=on_ready))
    await _wait_for(lambda: fake_proc.stdin.lines)
    first = fake_proc.stdin.lines[0]
    assert "--input-format" in fake_proc.captured["args"]
    assert first["type"] == "user" and first["message"]["content"] == "你好"
    assert first["priority"] == "next"
    assert isinstance(got["input"], RunInput) and got["input"].open
    assert not fake_proc.stdin.closed  # 跑着的时候 stdin 开着

    fake_proc.stdout.feed({"type": "system", "session_id": "sid_1"})
    fake_proc.stdout.feed(_delta("你好呀"))
    fake_proc.stdout.feed(_result("你好呀", [first["uuid"]]))
    text, sid, _ = await asyncio.wait_for(task, 2)
    assert (text, sid) == ("你好呀", "sid_1")
    assert fake_proc.stdin.closed


async def test_flag_off_keeps_plain_stdin(monkeypatch):
    monkeypatch.setenv("CLAUDE_PRINT_STREAM_INPUT", "0")
    proc = FakeProc()
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc

    # 旧路径直接写原文，FakeStdin 按 JSON 解析会失败——换一个只记字节的
    raw = []
    proc.stdin.write = lambda data: raw.append(data)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    proc.stdout.feed(_result("ok", []))
    called = []
    text, _, _ = await run_claude("hi", on_input_ready=lambda h: called.append(h))
    assert text == "ok" and raw == [b"hi\n"] and not called
    assert "--input-format" not in captured["args"]


async def test_injection_folds_or_runs_as_next_turn(fake_proc):
    got = {"turns": [], "started": []}

    async def on_ready(run_input):
        got["input"] = run_input
        run_input.on_turn_result = lambda t: got["turns"].append(t)
        run_input.on_started = lambda uid: got["started"].append(uid)

    task = asyncio.create_task(run_claude("写一篇长文", on_input_ready=on_ready))
    await _wait_for(lambda: "input" in got)
    first_uid = fake_proc.stdin.lines[0]["uuid"]

    uid = await got["input"].send("顺便说一下天气", "next")
    assert uid and fake_proc.stdin.lines[-1]["priority"] == "next"

    # 第一轮写完：CLI 说还排着一条 → 不能关 stdin
    fake_proc.stdout.feed(_delta("长文……"))
    fake_proc.stdout.feed(_result("长文……", [first_uid], queued=1))
    fake_proc.stdout.feed(_life(first_uid, "completed"))
    await asyncio.sleep(0.05)
    assert not fake_proc.stdin.closed

    fake_proc.stdout.feed(_life(uid, "started"))
    fake_proc.stdout.feed(_delta("今天晴"))
    fake_proc.stdout.feed(_result("今天晴", [uid], queued=0))
    text, _, _ = await asyncio.wait_for(task, 2)
    assert text == "今天晴"
    assert got["turns"] == ["长文……", "今天晴"]
    assert got["started"] == [uid]
    assert fake_proc.stdin.closed


async def test_message_written_after_last_result_keeps_stdin_open(fake_proc):
    """result 刚到、completed 还没到时又插了一条：不能关（它还没被处理）。"""
    got = {}
    task = asyncio.create_task(run_claude("a", on_input_ready=lambda h: got.setdefault("i", h)))
    await _wait_for(lambda: "i" in got)
    first_uid = fake_proc.stdin.lines[0]["uuid"]
    uid = await got["i"].send("b")
    fake_proc.stdout.feed(_result("A", [first_uid], queued=0))
    await asyncio.sleep(0.05)
    assert not fake_proc.stdin.closed
    fake_proc.stdout.feed(_life(uid, "started"))
    fake_proc.stdout.feed(_result("B", [uid], queued=0))
    text, _, _ = await asyncio.wait_for(task, 2)
    assert text == "B" and fake_proc.stdin.closed


async def test_steer_aborted_turn_is_not_an_error(fake_proc):
    got = {}
    task = asyncio.create_task(run_claude("跑三步", on_input_ready=lambda h: got.setdefault("i", h)))
    await _wait_for(lambda: "i" in got)
    first_uid = fake_proc.stdin.lines[0]["uuid"]

    uid = await got["i"].steer("别跑了，回 BANANA")
    kinds = [(l["type"], l.get("request", {}).get("subtype"), l.get("priority")) for l in fake_proc.stdin.lines[1:]]
    assert kinds == [("control_request", "interrupt", None), ("user", None, "now")]

    fake_proc.stdout.feed({"type": "result", "subtype": "error_during_execution", "is_error": True,
                           "result": None, "terminal_reason": "aborted_tools",
                           "user_message_uuids": [first_uid], "queued_turn_count": 0})
    fake_proc.stdout.feed(_life(first_uid, "cancelled"))
    await asyncio.sleep(0.05)
    assert not fake_proc.stdin.closed
    fake_proc.stdout.feed(_life(uid, "started"))
    fake_proc.stdout.feed(_result("BANANA", [uid]))
    text, _, _ = await asyncio.wait_for(task, 2)
    assert text == "BANANA"


async def test_error_result_raises_and_closes_stdin(fake_proc):
    got = {}
    task = asyncio.create_task(run_claude("x", on_input_ready=lambda h: got.setdefault("i", h)))
    await _wait_for(lambda: "i" in got)
    fake_proc.stdout.feed({"type": "result", "subtype": "success", "is_error": True,
                           "session_id": "sid_1",
                           "result": "API Error: Response stalled mid-stream.",
                           "terminal_reason": "completed"})
    with pytest.raises(RuntimeError) as ei:
        await asyncio.wait_for(task, 2)
    assert getattr(ei.value, "cc_retryable_resume", False) is True
    assert fake_proc.stdin.closed  # 不关就是孤儿进程


async def test_idle_after_result_without_signals_closes_stdin(fake_proc, monkeypatch):
    """CLI 没给 user_message_uuids / completed：上一轮结束后静默一段时间就关。"""
    monkeypatch.setattr(claude_runner, "_CHECK_INTERVAL", 0.05)
    got = {}
    task = asyncio.create_task(run_claude("x", on_input_ready=lambda h: got.setdefault("i", h)))
    await _wait_for(lambda: "i" in got)
    fake_proc.stdout.feed({"type": "result", "subtype": "success", "is_error": False,
                           "session_id": "sid_1", "result": "done"})
    text, _, _ = await asyncio.wait_for(task, 2)
    assert text == "done" and fake_proc.stdin.closed


async def test_send_after_close_returns_none_and_undelivered():
    proc = FakeProc()
    ri = RunInput(proc)
    first = await ri.send_first("first")
    later = await ri.send("插话1")
    assert first and later
    assert ri.undelivered() == ["插话1"]  # 还没 started
    await ri._on_lifecycle(_life(later, "started"))
    assert ri.undelivered() == []
    ri.close()
    assert not ri.open
    assert await ri.send("晚了") is None
    assert await ri.steer("晚了") is None


async def test_soft_stop_never_respawns(monkeypatch):
    """/stop 软停后 CLI 退出码 1、没输出：不能撞「resume 哑失败 → 换新 session 重跑」。"""
    monkeypatch.setenv("CLAUDE_PRINT_STREAM_INPUT", "1")
    spawned = []

    async def fake_exec(*args, **kwargs):
        proc = FakeProc()
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    got = {}
    task = asyncio.create_task(run_claude(
        "跑个长任务", session_id="sid_old", on_input_ready=lambda h: got.setdefault("i", h)))
    await _wait_for(lambda: "i" in got)
    first_uid = spawned[0].stdin.lines[0]["uuid"]
    await got["i"].stop()
    spawned[0].stdout.feed({"type": "result", "subtype": "error_during_execution", "is_error": True,
                            "result": None, "terminal_reason": "aborted_tools",
                            "user_message_uuids": [first_uid], "queued_turn_count": 0})
    orig_readline = spawned[0].stdout.readline

    async def readline_exit1():
        line = await orig_readline()
        if line == b"":
            spawned[0].returncode = 1  # 实测软停后退出码 1
        return line

    spawned[0].stdout.readline = readline_exit1
    text, sid, fresh = await asyncio.wait_for(task, 2)
    assert len(spawned) == 1, "软停后又拉了新进程"
    assert fresh is False
