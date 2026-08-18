"""卡片推送（_run_and_display 里的 push）的抗挂死单测。

复现的线上症状（2026-08-17 12:14，spx 群多个并行任务）：
网络通路被掐断（同一时刻 6 条 WS 全 keepalive ping timeout 重连），已发出的
`PATCH /im/v1/messages` 收不到 RST。lark_oapi 的 Config.timeout 默认 None
（永不超时），于是这次 patch 永久挂在 in-flight —— 它持着本 run 的
card_update_lock，心跳和流式 push 全堵在锁上，卡片就此定格，任务跑完连收尾的
✅ 都写不进去。lsof 实测：3 条到 open.larksuite.com:443 的连接在多次采样中
本地端口纹丝不动，其余连接每秒都在换。

另一半是老 push 的 `push_failures >= 3` 永久开关：抖 10 秒就把这一 run 剩下
40 分钟的卡片刷新全关掉，再不恢复。
"""

import asyncio
import os
import sys
import time
from unittest import mock

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher
from bot_config import Profile
from bot_instance import BotInstance


@pytest.fixture(autouse=True)
def _fast_push(monkeypatch):
    """把看门狗的秒数压小，别让单测真等 20s / 30s。"""
    monkeypatch.setattr(dispatcher, "_PUSH_TIMEOUT", 0.2)
    monkeypatch.setattr(dispatcher, "_PUSH_MUTE_SECONDS", 0.3)


def _bot() -> BotInstance:
    bot = BotInstance.__new__(BotInstance)
    bot.profile = Profile(
        name="test", app_id="cli_x", app_secret="s",
        platform="lark", domain="open.larksuite.com", default_cwd="/tmp",
    )
    bot.feishu = mock.AsyncMock()
    bot.active_runs = mock.MagicMock()
    active_run = mock.Mock(stop_requested=False)
    active_run.card_update_lock = asyncio.Lock()
    bot.active_runs.start_run.return_value = active_run
    bot.store = mock.MagicMock()
    bot.store.on_agent_response = mock.AsyncMock()
    return bot, active_run


def _session():
    return mock.Mock(
        session_id=None, model="claude-opus-5[1m]", effort=None, cwd="/tmp",
        permission_mode="bypassPermissions", runner="claude",
    )


async def _run(bot, fake_run_agent):
    with mock.patch.object(dispatcher, "run_agent", fake_run_agent):
        await dispatcher._run_and_display(
            bot,
            user_id="u1", chat_id="c1", is_group=True,
            text="跑个活", card_msg_id="card_1", session=_session(),
            notify_msg_id="msg_orig",
        )


async def test_hung_patch_does_not_wedge_the_run():
    """一帧永久挂住，不能把后续帧和读流循环一起堵死。"""
    bot, active_run = _bot()
    started = []

    async def update_card(message_id, content):
        started.append(content)
        if len(started) == 1:
            await asyncio.sleep(3600)  # 模拟死连接：永不返回、永不报错

    bot.feishu.update_card = update_card
    timings = []

    async def fake_run_agent(**kwargs):
        on_tool = kwargs["on_tool_use"]
        t0 = time.monotonic()
        await on_tool("Bash", {"command": "echo hi"})   # 撞上死连接的那一帧
        timings.append(time.monotonic() - t0)
        t1 = time.monotonic()
        await on_tool("Read", {"file_path": "/tmp/a"})  # 锁必须已经还回来了
        timings.append(time.monotonic() - t1)
        return "跑完了", "sid_1", False

    await _run(bot, fake_run_agent)

    # 挂住的那一帧被看门狗掐断，读流循环最多被拖 _PUSH_TIMEOUT
    assert timings[0] < 1.0, f"挂死的 push 把读流循环堵了 {timings[0]:.1f}s"
    # 后续帧仍然推得出去 —— 证明锁没被永久占住（老代码这里会永久 hang）
    assert len(started) >= 2, "第一帧挂死后卡片再也不更新了"
    assert not active_run.card_update_lock.locked(), "看门狗超时后没把锁还回来"


async def test_stop_card_writes_even_when_lock_is_wedged():
    """卡片锁被一次挂死的请求占着时，/stop、/restart 仍要能写出中断卡。

    否则 stop_run → on_stopped → `async with card_update_lock` 会永久阻塞，
    /restart 停在「中断任务」阶段出不来，整个服务救不回来。
    """
    bot, active_run = _bot()
    active_run.card_msg_id = "card_1"
    active_run.last_body = "已经跑了一半"
    await active_run.card_update_lock.acquire()  # 模拟被挂死的 push 占住

    await asyncio.wait_for(
        dispatcher._announce_stopped_run(bot, active_run), timeout=5.0)

    bot.feishu.update_card.assert_awaited()
    content = bot.feishu.update_card.await_args.args[1]
    assert "任务已被停止" in content
    assert "已经跑了一半" in content, "停止卡应保留停止前的进度"


async def test_push_failures_mute_then_recover():
    """连续失败只静音一段时间，不能像老代码那样永久关掉本 run 的卡片。"""
    bot, _ = _bot()
    calls = []
    fail = {"on": True}

    async def update_card(message_id, content):
        calls.append(content)
        if fail["on"]:
            raise RuntimeError("boom")

    bot.feishu.update_card = update_card

    # ⚠️ 断言必须写在 _run 之外：_run_and_display 用 `except Exception` 包住了
    # run_agent（重试/降级逻辑），在 fake_run_agent 里 assert 会被它吞掉，测试
    # 会假绿。这里只记录观测值，跑完再断言。
    seen = {}

    async def fake_run_agent(**kwargs):
        on_tool = kwargs["on_tool_use"]
        for i in range(dispatcher._PUSH_FAILURE_LIMIT):
            await on_tool("Bash", {"command": f"c{i}"})
        seen["at_limit"] = len(calls)
        await on_tool("Bash", {"command": "muted"})
        seen["while_muted"] = len(calls)

        fail["on"] = False
        await asyncio.sleep(dispatcher._PUSH_MUTE_SECONDS + 0.05)
        await on_tool("Bash", {"command": "after-mute"})
        seen["after_mute"] = len(calls)
        return "跑完了", "sid_1", False

    await _run(bot, fake_run_agent)

    assert seen["at_limit"] == dispatcher._PUSH_FAILURE_LIMIT, \
        f"前 {dispatcher._PUSH_FAILURE_LIMIT} 帧都该真的发出去过：{seen}"
    assert seen["while_muted"] == seen["at_limit"], "达到失败上限后应当静音，别继续空烧请求"
    assert seen["after_mute"] == seen["at_limit"] + 1, \
        f"静音窗口过后必须恢复推送（老代码 push_failures>=3 是永久开关）：{seen}"
