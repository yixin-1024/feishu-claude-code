"""上游中断自动续跑（_run_and_display 的 is_stall 分支）单测。

复现的线上症状（2026-07-31 spx 群 SGB 话题）：
    ❌ Agent 执行出错：Claude API 错误（success, HTTP None）：
       API Error: Connection closed mid-response.
上游把流掐断 → 整轮任务中断，要人手发「继续」。修复后应当自动 resume 同一 session
接着跑完，卡片只显示恢复出来的完整回复。
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
from bot_config import Profile
from bot_instance import BotInstance


@pytest.fixture(autouse=True)
def _no_cooldown(monkeypatch):
    """把递增冷却压成 0，别让单测真睡 10/30/60 秒。"""
    monkeypatch.setattr(dispatcher, "_STALL_COOLDOWNS", (0, 0, 0))


def _bot() -> BotInstance:
    bot = BotInstance.__new__(BotInstance)
    bot.profile = Profile(
        name="test", app_id="cli_x", app_secret="s",
        platform="lark", domain="open.larksuite.com", default_cwd="/tmp",
    )
    bot.feishu = mock.AsyncMock()
    bot.feishu.reply_card = mock.AsyncMock(return_value="card_id_1")
    bot.active_runs = mock.MagicMock()
    active_run = mock.Mock(stop_requested=False)
    active_run.card_update_lock = asyncio.Lock()
    bot.active_runs.start_run.return_value = active_run
    bot.store = mock.MagicMock()
    bot.store.on_agent_response = mock.AsyncMock()
    return bot


def _session(session_id=None):
    return mock.Mock(
        session_id=session_id, model="claude-opus-5[1m]", effort=None, cwd="/tmp",
        permission_mode="bypassPermissions", runner="claude",
    )


def _stall_exc(session_id="sid_stall"):
    exc = RuntimeError(
        "Claude API 错误（stream_error）：API Error: Connection closed mid-response. "
        "The response above may be incomplete."
    )
    exc.cc_session_id = session_id
    exc.cc_retryable_resume = True
    return exc


async def _run(bot, session, fake_run_agent, text="帮我查一下余额"):
    with mock.patch.object(dispatcher, "run_agent", fake_run_agent):
        await dispatcher._run_and_display(
            bot,
            user_id="u1", chat_id="c1", is_group=True,
            text=text, card_msg_id="card_id_1", session=session,
            notify_msg_id="msg_orig",
        )


async def test_stall_auto_resumes_same_session_and_recovers():
    bot = _bot()
    session = _session()
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append((kwargs["message"], kwargs["session_id"]))
        if len(calls) == 1:
            raise _stall_exc("sid_stall")
        return "余额是 100 USD", "sid_stall", False

    await _run(bot, session, fake_run_agent)

    assert len(calls) == 2, "上游中断后没有自动续跑"
    # 第二轮必须 resume 崩溃前那个 session，并带「继续」提示
    assert calls[1][1] == "sid_stall"
    assert calls[1][0] == dispatcher._STALL_RESUME_NUDGE
    # 最终卡片是恢复出来的完整回复，不是 ❌
    final = bot.feishu.update_card_final.await_args
    assert final is not None
    body = final.args[1] if len(final.args) > 1 else final.kwargs.get("content", "")
    assert "余额是 100 USD" in body
    assert "❌" not in body


async def test_stall_retries_up_to_budget_then_reports():
    """一直中断也不能无限续跑：跑满预算后报错，并保留『继续』提示。"""
    bot = _bot()
    session = _session()
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append(kwargs["message"])
        raise _stall_exc("sid_stall")

    with mock.patch.dict(os.environ, {"CC_LARK_STALL_RETRY_MAX": "2"}):
        await _run(bot, session, fake_run_agent)

    assert len(calls) == 3, f"预算 2 次续跑 → 共 3 次调用，实际 {len(calls)}"
    final = bot.feishu.update_card_final.await_args
    body = final.args[1] if len(final.args) > 1 else final.kwargs.get("content", "")
    assert "自动重试 2 次后仍失败" in body
    assert "上下文已保留" in body


async def test_stall_in_write_op_context_resumes_with_verify_nudge(monkeypatch):
    """写操作场景（开户等）也续跑，但提示换成「先核实上一步是否已生效」。"""
    monkeypatch.setenv("CC_LARK_WRITE_OP_MARKERS", "spxpay-sgb-va-onboarding")
    bot = _bot()
    session = _session()
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append(kwargs["message"])
        if len(calls) == 1:
            raise _stall_exc("sid_sgb")
        return "已开户", "sid_sgb", False

    await _run(bot, session, fake_run_agent, text="用 spxpay-sgb-va-onboarding 给客户开户")

    assert len(calls) == 2
    assert calls[1] == dispatcher._STALL_RESUME_NUDGE_WRITE


async def test_stall_without_session_in_write_op_context_does_not_retry(monkeypatch):
    """无 session 可 resume + 写操作：只能原样重发，有 double-write 风险，不自动重试。"""
    monkeypatch.setenv("CC_LARK_WRITE_OP_MARKERS", "spxpay-sgb-va-onboarding")
    bot = _bot()
    session = _session()
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append(kwargs["message"])
        exc = RuntimeError("claude exited with code 1: fetch failed")
        exc.cc_retryable_resume = True  # 瞬时错误但拿不到 session id
        raise exc

    await _run(bot, session, fake_run_agent, text="用 spxpay-sgb-va-onboarding 给客户开户")

    assert len(calls) == 1


async def test_stall_without_session_read_only_retries_original_text():
    """无 session 但只读场景：原样重发用户诉求（fresh 重试）。"""
    bot = _bot()
    session = _session()
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append(kwargs["message"])
        if len(calls) == 1:
            exc = RuntimeError("claude exited with code 1: fetch failed")
            exc.cc_retryable_resume = True
            raise exc
        return "查完了", "sid_new", False

    await _run(bot, session, fake_run_agent, text="帮我查一下余额")

    assert len(calls) == 2
    assert calls[1] == "帮我查一下余额"


async def test_stall_budget_zero_disables_auto_resume(monkeypatch):
    monkeypatch.setenv("CC_LARK_STALL_RETRY_MAX", "0")
    bot = _bot()
    session = _session()
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append(kwargs["message"])
        raise _stall_exc("sid_stall")

    await _run(bot, session, fake_run_agent)

    assert len(calls) == 1
