"""全局并发闸门（RunGate）单测。

要治的症状：per-chat lock 只保证「同一话题串行」，整机层面没有刹车 —— 定时任务批量
到点 / 一波 dispatch_task 派 7 个 / 多个群同时来人，都能瞬间拉起十几个 agent 进程把
CPU / 内存 / API 额度打满（实测 14 并发全卡死）。闸门必须满足：

  · 同时真正在跑的 run 不超过上限
  · 超额的 run **排队**而不是被丢弃，且先来先跑（FIFO）
  · 排队期间能被 /stop、/restart 打断；打断绝不能漏额度
    （漏一个额度 = 并发上限跑久了自己越缩越小）
  · 上限设 0 = 回到无闸门的老行为
"""

import asyncio
import os
import sys
from unittest import mock

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher
import run_control
from bot_config import Profile
from bot_instance import BotInstance
from run_control import ActiveRunRegistry, RunGate, stop_run


@pytest.fixture(autouse=True)
def _fast_abort_poll(monkeypatch):
    """abort 谓词的轮询间隔压到 10ms，别让单测真等半秒一轮。"""
    monkeypatch.setattr(run_control, "_ABORT_POLL_INTERVAL_SECONDS", 0.01)


# ── 闸门本身 ─────────────────────────────────────────────────────────────

async def test_never_exceeds_limit():
    gate = RunGate(4)
    live = 0
    peak = 0

    async def one():
        nonlocal live, peak
        assert await gate.acquire() == "ok"
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.02)
        finally:
            live -= 1
            gate.release()

    await asyncio.gather(*(one() for _ in range(12)))

    assert peak == 4, f"同时在跑 {peak} 个，上限 4 没兜住"
    assert gate.running == 0 and gate.waiting == 0
    # 12 个全跑到了（排队不丢活）
    assert live == 0


async def test_queues_fifo():
    gate = RunGate(1)
    assert await gate.acquire() == "ok"      # 占满
    order: list[int] = []

    async def waiter(i: int):
        assert await gate.acquire() == "ok"
        order.append(i)
        gate.release()

    tasks = [asyncio.create_task(waiter(i)) for i in (1, 2, 3)]
    await asyncio.sleep(0.05)
    assert gate.waiting == 3
    assert gate.full()

    gate.release()                          # 放一个额度，链式跑完
    await asyncio.gather(*tasks)

    assert order == [1, 2, 3], f"排队没按先来先跑：{order}"
    assert gate.running == 0 and gate.waiting == 0


async def test_abort_cancels_wait_without_leaking_slot():
    gate = RunGate(1)
    assert await gate.acquire() == "ok"
    aborted = False

    task = asyncio.create_task(gate.acquire(abort=lambda: aborted))
    await asyncio.sleep(0.05)
    assert gate.waiting == 1

    aborted = True                          # 相当于 /stop 落在排队中的 run 上
    assert await task == "aborted"
    assert gate.waiting == 0

    # 关键：额度没被排队者顺手拿走 —— 放掉手里那个后，闸门应当完整恢复到 1 个可用。
    gate.release()
    assert gate.running == 0
    assert not gate.full()
    assert await gate.acquire() == "ok"
    gate.release()
    assert await gate.acquire() == "ok"     # 还能再拿，说明额度没漏
    gate.release()


async def test_abort_losing_race_returns_slot():
    """cancel 与「额度刚好到手」的竞态：晚了一步也必须把额度还回去。"""
    gate = RunGate(1)
    assert await gate.acquire() == "ok"

    task = asyncio.create_task(gate.acquire(abort=lambda: True))
    await asyncio.sleep(0.05)
    gate.release()                          # 额度在 abort 生效的同一轮里放出
    result = await task

    assert result in {"aborted", "ok"}
    if result == "ok":                      # 抢赢了就正常还
        gate.release()
    assert not gate.full(), "额度漏了：闸门还是满的"
    assert await gate.acquire() == "ok"
    gate.release()


async def test_timeout_gives_up_without_leaking_slot():
    gate = RunGate(1, max_wait=0.05)
    assert await gate.acquire() == "ok"

    assert await gate.acquire() == "timeout"
    assert gate.waiting == 0

    gate.release()
    assert await gate.acquire() == "ok"
    gate.release()


async def test_zero_limit_means_unlimited():
    gate = RunGate(0)
    for _ in range(20):
        assert await gate.acquire() == "ok"
    assert not gate.full()
    assert gate.describe() == "并发不限"
    for _ in range(20):
        gate.release()
    assert gate.running == 0


async def test_describe_shows_running_and_waiting():
    gate = RunGate(2)
    await gate.acquire()
    assert gate.describe() == "1/2 在跑"
    await gate.acquire()
    task = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0.05)
    assert gate.describe() == "2/2 在跑，1 排队"
    gate.release()
    await task
    for _ in range(2):
        gate.release()


def test_gate_is_wired_to_env(monkeypatch):
    """闸门参数来自环境变量，缺省 4（给服务器兜底）；本机 .env 钉成 100 = 放开。

    这里不断言"当前就是 4" —— pytest 会把 .env 灌进 os.environ，本机那份写着 100。
    要盯的是"读得对、接得上"。
    """
    monkeypatch.delenv("CC_LARK_MAX_CONCURRENT_RUNS", raising=False)
    assert run_control._env_int("CC_LARK_MAX_CONCURRENT_RUNS", 4) == 4

    monkeypatch.setenv("CC_LARK_MAX_CONCURRENT_RUNS", "100")
    assert run_control._env_int("CC_LARK_MAX_CONCURRENT_RUNS", 4) == 100

    monkeypatch.setenv("CC_LARK_MAX_CONCURRENT_RUNS", "  ")      # 空白 = 用缺省
    assert run_control._env_int("CC_LARK_MAX_CONCURRENT_RUNS", 4) == 4
    monkeypatch.setenv("CC_LARK_MAX_CONCURRENT_RUNS", "abc")     # 垃圾值也不能崩
    assert run_control._env_int("CC_LARK_MAX_CONCURRENT_RUNS", 4) == 4

    # 进程里那个真闸门确实按这两个变量装起来了
    assert run_control.RUN_GATE.limit == run_control.MAX_CONCURRENT_RUNS
    assert run_control.RUN_GATE.max_wait == run_control.QUEUE_MAX_WAIT_SECONDS


# ── dispatcher 集成：排队卡片 / 排队中被 /stop ────────────────────────────

def _bot() -> BotInstance:
    bot = BotInstance.__new__(BotInstance)
    bot.profile = Profile(
        name="test", app_id="cli_x", app_secret="s",
        platform="lark", domain="open.larksuite.com", default_cwd="/tmp",
    )
    bot.feishu = mock.AsyncMock()
    bot.active_runs = ActiveRunRegistry()      # 用真 registry：排队态也要能被 /stop
    bot.store = mock.MagicMock()
    bot.store.on_agent_response = mock.AsyncMock()
    return bot


def _session():
    return mock.Mock(
        session_id=None, model="claude-opus-5[1m]", effort=None, cwd="/tmp",
        permission_mode="bypassPermissions", runner="claude",
    )


async def _run(bot, chat_id: str):
    """注意：run_agent 的 patch 必须由调用方在**所有**并发 run 之外套一层 ——
    每个 run 各自 with 一次时，先结束的那个会把 dispatcher.run_agent 还原成真
    实现，排在后面的 run 就会真的去拉一个 claude 进程。"""
    return await dispatcher._run_and_display(
        bot, user_id="u1", chat_id=chat_id, is_group=True,
        text="干活", card_msg_id=f"card_{chat_id}", session=_session(),
        notify_msg_id="msg_orig",
    )


def _cards(bot, card_msg_id: str) -> list[str]:
    out = []
    for c in bot.feishu.update_card.await_args_list:
        if c.args and c.args[0] == card_msg_id:
            out.append(c.args[1] if len(c.args) > 1 else c.kwargs.get("content", ""))
    return out


async def test_second_run_waits_and_shows_queued_card(monkeypatch):
    """上限 1 时第二条消息不该被丢，而是卡片显示"排队中"、等前面跑完再执行。"""
    monkeypatch.setattr(dispatcher, "RUN_GATE", RunGate(1))
    bot = _bot()
    gate1 = asyncio.Event()
    started: list[str] = []

    async def fake_run_agent(**kwargs):
        # 用户消息前面会挂一行【本轮 · 消息 id…】头，这里只关心正文
        started.append(kwargs["message"].rsplit("】\n\n", 1)[-1])
        if len(started) == 1:
            await gate1.wait()
        return "干完了", "sid", False

    monkeypatch.setattr(dispatcher, "run_agent", fake_run_agent)
    first = asyncio.create_task(_run(bot, "c1"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(_run(bot, "c2"))
    await asyncio.sleep(0.05)

    assert started == ["干活"], "上限 1，第二个 run 居然也起跑了"
    queued = _cards(bot, "card_c2")
    assert any("排队中" in c for c in queued), f"没给排队的任务写排队卡片：{queued}"

    gate1.set()
    assert await first == "干完了"
    assert await second == "干完了", "排队的任务必须在额度腾出来后照跑，不能丢"
    assert len(started) == 2
    assert dispatcher.RUN_GATE.running == 0


async def test_stop_while_queued_cancels_before_it_starts(monkeypatch):
    """排队中的任务能被 /stop 掉：不占额度、不进 agent、卡片说明未执行。"""
    monkeypatch.setattr(dispatcher, "RUN_GATE", RunGate(1))
    bot = _bot()
    gate1 = asyncio.Event()
    started: list[str] = []

    async def fake_run_agent(**kwargs):
        # 用户消息前面会挂一行【本轮 · 消息 id…】头，这里只关心正文
        started.append(kwargs["message"].rsplit("】\n\n", 1)[-1])
        if len(started) == 1:
            await gate1.wait()
        return "干完了", "sid", False

    monkeypatch.setattr(dispatcher, "run_agent", fake_run_agent)
    first = asyncio.create_task(_run(bot, "c1"))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(_run(bot, "c2"))
    await asyncio.sleep(0.05)

    assert await stop_run(bot.active_runs, "u1", "c2") is True
    assert await second is None
    assert started == ["干活"], "被 /stop 的排队任务不该进 agent"
    cards = _cards(bot, "card_c2")
    assert any("已取消" in c for c in cards), f"取消后没收尾卡片：{cards}"
    # 排队者退场不能带走额度
    gate1.set()
    assert await first == "干完了"
    assert dispatcher.RUN_GATE.running == 0
    assert not dispatcher.RUN_GATE.full()
