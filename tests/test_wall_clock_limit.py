"""CLAUDE_WALL_CLOCK_LIMIT_SEC：单轮 wall-clock 上限可配 / 可关。

覆盖三件事：
  1. resolve_claude_wall_clock_limit 的优先级与容错（extra_env > per-profile > 全局 > 默认）
  2. print 后端（claude_runner._run_claude_print）真的按解析结果掐表，0 = 不掐
  3. PTY 后端（claude_pty）的常量与解析入口存在且默认值一致
"""

import os
import sys

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_config import (  # noqa: E402
    CLAUDE_WALL_CLOCK_LIMIT_DEFAULT,
    resolve_claude_wall_clock_limit,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.endswith("CLAUDE_WALL_CLOCK_LIMIT_SEC"):
            monkeypatch.delenv(key, raising=False)


def test_default_is_one_hour():
    assert resolve_claude_wall_clock_limit() == CLAUDE_WALL_CLOCK_LIMIT_DEFAULT == 3600.0


def test_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("CLAUDE_WALL_CLOCK_LIMIT_SEC", "0")
    assert resolve_claude_wall_clock_limit() == 0.0


def test_global_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_WALL_CLOCK_LIMIT_SEC", "7200")
    assert resolve_claude_wall_clock_limit() == 7200.0


def test_profile_beats_global(monkeypatch):
    monkeypatch.setenv("CLAUDE_WALL_CLOCK_LIMIT_SEC", "7200")
    monkeypatch.setenv("SPX_CLAUDE_WALL_CLOCK_LIMIT_SEC", "0")
    assert resolve_claude_wall_clock_limit({"CC_LARK_PROFILE": "spx"}) == 0.0
    # 其他 profile 不受影响，仍吃全局值
    assert resolve_claude_wall_clock_limit({"CC_LARK_PROFILE": "work"}) == 7200.0


def test_profile_name_normalized(monkeypatch):
    monkeypatch.setenv("MY_BOT_CLAUDE_WALL_CLOCK_LIMIT_SEC", "1800")
    assert resolve_claude_wall_clock_limit(profile_name="my-bot") == 1800.0


def test_extra_env_beats_everything(monkeypatch):
    monkeypatch.setenv("CLAUDE_WALL_CLOCK_LIMIT_SEC", "7200")
    monkeypatch.setenv("SPX_CLAUDE_WALL_CLOCK_LIMIT_SEC", "600")
    extra = {"CC_LARK_PROFILE": "spx", "CLAUDE_WALL_CLOCK_LIMIT_SEC": "120"}
    assert resolve_claude_wall_clock_limit(extra) == 120.0


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("CLAUDE_WALL_CLOCK_LIMIT_SEC", raw)
    assert resolve_claude_wall_clock_limit() == CLAUDE_WALL_CLOCK_LIMIT_DEFAULT


@pytest.mark.parametrize("raw", ["-1", "never", "abc"])
def test_garbage_means_unlimited(monkeypatch, raw):
    """负数 / 非数字不抛异常——这条兜底不值得让整轮跑不起来。"""
    monkeypatch.setenv("CLAUDE_WALL_CLOCK_LIMIT_SEC", raw)
    assert resolve_claude_wall_clock_limit() == 0.0


# ── 调用点：print 后端 ────────────────────────────────────────


def _fake_proc_that_never_speaks():
    """一个永不产出、也永不退出的假 claude 进程。"""

    class _Stdin:
        def write(self, data):
            pass

        async def drain(self):
            return None

        def close(self):
            pass

    class _Stdout:
        async def readline(self):
            import asyncio

            await asyncio.sleep(3600)  # 永远不返回；由 fake clock 推进时间
            return b""

    class _Stderr:
        async def read(self):
            return b""

    class _Proc:
        pid = 4242
        returncode = None

        def __init__(self):
            self.stdin, self.stdout, self.stderr = _Stdin(), _Stdout(), _Stderr()
            self.killed = False

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    return _Proc()


class _ClockExhausted(Exception):
    """假时钟跑满预算仍没人喊停——用来证明 wall-clock 没有开火。"""


def _run_print_with_clock(monkeypatch, extra_env, clock_step, max_ticks=200):
    """跑 print 后端，用一个每次调用都跳 clock_step 秒的假时钟推进 wall-clock。

    时钟跳满 max_ticks 次还没被掐 → 抛 _ClockExhausted 收场（否则用例会挂死）。
    返回 (raised_error, proc)
    """
    import asyncio

    import claude_runner

    proc = _fake_proc_that_never_speaks()

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(claude_runner, "_has_children", lambda pid: False)
    # 让 idle 判定不抢在 wall-clock 前面开火；_CHECK_INTERVAL=0 → 主循环空转推进
    monkeypatch.setattr(claude_runner, "IDLE_TIMEOUT", 10**9)
    monkeypatch.setattr(claude_runner, "STUCK_CHILD_TIMEOUT", 10**9)
    monkeypatch.setattr(claude_runner, "_CHECK_INTERVAL", 0)

    async def main():
        loop = asyncio.get_event_loop()
        ticks = {"n": 0}
        real_time = loop.time
        base = real_time()

        def fake_time():
            ticks["n"] += 1
            if ticks["n"] > max_ticks:
                # 先把真时钟装回去，否则 asyncio 收尾（取消 pending task）也会踩到
                # 这个异常，留下 "Task was destroyed but it is pending" 噪音
                monkeypatch.setattr(loop, "time", real_time)
                raise _ClockExhausted(f"clock ran to {ticks['n'] * clock_step}s")
            return base + ticks["n"] * clock_step

        monkeypatch.setattr(loop, "time", fake_time)
        return await claude_runner._run_claude_print("hi", extra_env=extra_env)

    try:
        asyncio.run(main())
        return None, proc
    except Exception as exc:  # noqa: BLE001 — 用例要看的就是它抛了什么
        return exc, proc


def test_print_backend_kills_at_limit(monkeypatch):
    err, proc = _run_print_with_clock(
        monkeypatch, {"CLAUDE_WALL_CLOCK_LIMIT_SEC": "600"}, clock_step=100
    )
    assert isinstance(err, RuntimeError), repr(err)
    assert "wall-clock 最终上限（600秒）" in str(err)
    assert proc.killed is True


def test_print_backend_zero_never_kills(monkeypatch):
    """limit=0：假时钟跑到 20 万秒（>55h）也不该因 wall-clock 掐进程。"""
    err, proc = _run_print_with_clock(
        monkeypatch, {"CLAUDE_WALL_CLOCK_LIMIT_SEC": "0"}, clock_step=1000
    )
    assert isinstance(err, _ClockExhausted), repr(err)
    assert proc.killed is False


def test_print_backend_default_limit_still_one_hour(monkeypatch):
    """没配 env → 仍是历史行为的 3600s。"""
    err, proc = _run_print_with_clock(monkeypatch, None, clock_step=100)
    assert isinstance(err, RuntimeError), repr(err)
    assert "wall-clock 最终上限（3600秒）" in str(err)
    assert proc.killed is True


# ── 调用点：PTY 后端 ─────────────────────────────────────────


def test_pty_module_uses_shared_default():
    import claude_pty

    assert claude_pty.WALL_CLOCK_LIMIT == CLAUDE_WALL_CLOCK_LIMIT_DEFAULT
    assert claude_pty.resolve_claude_wall_clock_limit is resolve_claude_wall_clock_limit


def test_pty_loop_guards_on_resolved_limit():
    """PTY 主循环必须用解析出来的 wall_clock_limit（且 >0 才判），不是硬编码常量。"""
    import inspect

    import claude_pty

    src = inspect.getsource(claude_pty.run_claude)
    assert "wall_clock_limit = resolve_claude_wall_clock_limit(extra_env)" in src
    assert "if wall_clock_limit > 0 and now - start_time >= wall_clock_limit:" in src
