"""卡片流式推送的节流水位线回归测试。

2026-09-14 线上症状：模型 38 字/秒，卡片只画出 ~2 字/秒。根因是 on_text_chunk 里
`last_push_time = now`（push **发起前**的时间戳）——当单帧耗时超过 _PUSH_INTERVAL
(0.4s) 时节流彻底失效，每个 text_delta 都推一整卡；而 on_text_chunk 是在 runner 读
stdout 的循环里被 await 的，于是退化成「读 1 个 delta → 堵一帧 → 读 1 个 delta」。

本测试用「单帧 0.5s > 阈值 0.4s」的慢 Lark 复现该条件，断言推送次数远小于 delta 数。
"""

import asyncio
import os
import sys
import time
from unittest import mock

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher
from bot_config import Profile
from bot_instance import BotInstance

_SLOW_PATCH = 0.5   # 模拟 Lark 整卡 PATCH 耗时（实测 850ms），必须 > _PUSH_INTERVAL
_DELTAS = 20
_DELTA_GAP = 0.05   # 模型出字节奏，总计约 1s


def _bot(pushes: list) -> BotInstance:
    bot = BotInstance.__new__(BotInstance)
    bot.profile = Profile(
        name="test", app_id="cli_x", app_secret="s",
        platform="lark", domain="open.larksuite.com", default_cwd="/tmp",
    )
    bot.feishu = mock.AsyncMock()
    bot.store = mock.AsyncMock()
    bot.active_runs = mock.MagicMock()
    bot.chat_locks = {}

    async def slow_update_card(msg_id, content):
        await asyncio.sleep(_SLOW_PATCH)
        pushes.append((time.monotonic(), content))

    bot.feishu.update_card.side_effect = slow_update_card
    return bot


async def test_slow_card_patch_does_not_degrade_to_one_delta_per_frame():
    pushes: list = []
    bot = _bot(pushes)
    active_run = mock.Mock(stop_requested=False, last_body="")
    active_run.card_update_lock = asyncio.Lock()
    bot.active_runs.start_run.return_value = active_run

    session = mock.Mock(
        session_id=None, model="opus[1m]", effort=None, cwd="/tmp",
        permission_mode="bypassPermissions", runner="claude",
    )

    async def fake_run_agent(**kwargs):
        for _ in range(_DELTAS):
            await asyncio.sleep(_DELTA_GAP)
            await kwargs["on_text_chunk"]("字字")
        return "done", "sess_1", False

    t0 = time.monotonic()
    with mock.patch.object(dispatcher, "run_agent", fake_run_agent):
        await dispatcher._run_and_display(
            bot, user_id="u1", chat_id="c1", is_group=True, text="hi",
            card_msg_id="card_1", session=session, notify_msg_id="msg_1",
        )
    elapsed = time.monotonic() - t0

    # 出 bug 时：20 个 delta = 20 帧 × 0.5s = 10s+，且每帧只多 2 个字。
    # 正确时：帧间隔 = 阈值 + 单帧耗时 ≈ 0.9s，1s 的输出最多 2~3 帧（含心跳兜底放宽到 5）。
    assert len(pushes) <= 5, f"推了 {len(pushes)} 帧（delta 共 {_DELTAS} 个）——节流水位线又取成 push 发起前了"
    # 读流不该被卡片拖住：总耗时应接近模型出字时间，而不是 帧数 × 单帧耗时
    # 出 bug 时这里是 _DELTAS * _SLOW_PATCH ≈ 10s，留足抖动余量也能清晰区分
    assert elapsed < _DELTAS * _DELTA_GAP + 5 * _SLOW_PATCH, f"读流被卡片堵住，总耗时 {elapsed:.1f}s"
    # 每帧要把积压的 delta 一次性画出，而不是一帧只多一个 delta（2 个字）
    lens = [c.split("\n\n`")[0].count("字") for _, c in pushes]
    avg_gain = lens[-1] / len(lens)
    assert avg_gain >= 10, f"平均每帧只新增 {avg_gain:.1f} 字——退化成一帧一个 delta 了"
    # 流式没画完的尾巴由收尾帧补齐，正文一个字都不能丢
    final = bot.feishu.update_card_final.await_args or bot.feishu.update_card_with_buttons.await_args
    assert final and "字" * (2 * _DELTAS) in final.args[1], "收尾帧漏字"
