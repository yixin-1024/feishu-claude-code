"""模型 safeguards 拦截自动降级（_run_and_display 的 is_safeguards 分支）单测。

复现的线上症状（2026-08-09 spx 群）：
    ❌ 自动重试 3 次后仍失败：RuntimeError: Claude API 错误（stream_error）：
       API Error: Fable 5's safeguards flagged this message (...).
       Claude Code can't respond to this message with Fable 5.
       Try rephrasing the request in a new session or change your model.
同一模型重试必然再被拦，3 次 stall 续跑全白费，最后要人手 /model opus。
修复后应当：不重试，直接切到降级模型（默认 opus[1m]）resume 同一 session
接着跑，并显式发一条独立消息告知用户「因为这个错误已切换模型」。
"""

import asyncio
import os
import sys
from unittest import mock

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import claude_runner
import dispatcher
from bot_config import Profile
from bot_instance import BotInstance


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
    bot.store.set_model_override = mock.AsyncMock()
    return bot


def _session(session_id=None, model="claude-fable-5"):
    return mock.Mock(
        session_id=session_id, model=model, effort=None, cwd="/tmp",
        permission_mode="bypassPermissions", runner="claude",
    )


def _safeguards_exc(session_id="sid_sg"):
    exc = RuntimeError(
        "Claude API 错误（stream_error）：API Error: Fable 5's safeguards "
        "flagged this message (https://www.anthropic.com/legal/aup). "
        "This sometimes happens with safe, normal conversations. Claude Code "
        "can't respond to this message with Fable 5. Try rephrasing the "
        "request in a new session or change your model."
    )
    exc.cc_session_id = session_id
    # runner 侧已把 safeguards 归入 fatal 黑名单（不走 stall 续跑）
    exc.cc_retryable_resume = False
    return exc


async def _run(bot, session, fake_run_agent, text="帮我查一下余额"):
    with mock.patch.object(dispatcher, "run_agent", fake_run_agent):
        await dispatcher._run_and_display(
            bot,
            user_id="u1", chat_id="c1", is_group=True,
            text=text, card_msg_id="card_id_1", session=session,
            notify_msg_id="msg_orig",
        )


def test_runner_classifies_safeguards_as_fatal_and_detectable():
    blob = "API Error: Fable 5's safeguards flagged this message (...)"
    assert claude_runner.is_safeguards_error_text(blob)
    assert claude_runner.is_fatal_error_text(blob)
    assert not claude_runner.is_safeguards_error_text(
        "API Error: Connection closed mid-response."
    )


async def test_safeguards_switches_model_and_resumes_same_session():
    bot = _bot()
    session = _session()
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append((kwargs["message"], kwargs["session_id"], kwargs["model"]))
        if len(calls) == 1:
            raise _safeguards_exc("sid_sg")
        return "余额是 100 USD", "sid_sg", False

    await _run(bot, session, fake_run_agent)

    assert len(calls) == 2, "safeguards 拦截后没有自动降级续跑"
    # 第二轮必须换成降级模型、resume 崩溃前那个 session、带 safeguards 续跑提示
    assert calls[1][2] == "opus[1m]"
    assert calls[1][1] == "sid_sg"
    assert calls[1][0] == dispatcher._SAFEGUARDS_RESUME_NUDGE
    # override 落盘（不动 session），本对话后续轮次也用降级模型
    bot.store.set_model_override.assert_awaited_once_with("u1", "c1", "opus[1m]")
    # 显式独立消息告知用户切换原因
    notice_calls = [
        c.args[1] for c in bot.feishu.reply_text.await_args_list
        if len(c.args) > 1
    ]
    assert any(
        "safeguards" in t and "opus[1m]" in t for t in notice_calls
    ), f"没有显式告知模型切换：{notice_calls}"
    # 最终卡片是恢复出来的完整回复，不是 ❌
    final = bot.feishu.update_card_final.await_args
    body = final.args[1] if len(final.args) > 1 else final.kwargs.get("content", "")
    assert "余额是 100 USD" in body
    assert "❌" not in body


async def test_safeguards_does_not_stall_retry_same_model():
    """降级后仍被拦（一轮只切一次）→ 不再用同模型反复重试，直接报错交回用户。"""
    bot = _bot()
    session = _session()
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append(kwargs["model"])
        raise _safeguards_exc("sid_sg")

    await _run(bot, session, fake_run_agent)

    assert calls == ["claude-fable-5", "opus[1m]"], (
        f"应当只切换一次、绝不同模型重试，实际调用序列：{calls}"
    )
    final = bot.feishu.update_card_final.await_args
    body = final.args[1] if len(final.args) > 1 else final.kwargs.get("content", "")
    assert "❌" in body
    # 崩溃前 session 已保存，用户发『继续』可接着跑
    assert "上下文已保留" in body


async def test_safeguards_already_on_fallback_reports_immediately():
    """当前模型已是降级模型：切无可切，不重试直接报错。"""
    bot = _bot()
    session = _session(model="opus[1m]")
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append(kwargs["model"])
        raise _safeguards_exc("sid_sg")

    await _run(bot, session, fake_run_agent)

    assert len(calls) == 1
    bot.store.set_model_override.assert_not_awaited()


async def test_safeguards_write_op_resumes_with_verify_nudge(monkeypatch):
    """写操作场景（开户等）也降级续跑，但提示换成「先核实上一步是否已生效」。"""
    monkeypatch.setenv("CC_LARK_WRITE_OP_MARKERS", "spxpay-sgb-va-onboarding")
    bot = _bot()
    session = _session()
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append(kwargs["message"])
        if len(calls) == 1:
            raise _safeguards_exc("sid_sgb")
        return "已开户", "sid_sgb", False

    await _run(bot, session, fake_run_agent,
               text="用 spxpay-sgb-va-onboarding 给客户开户")

    assert len(calls) == 2
    assert calls[1] == dispatcher._SAFEGUARDS_RESUME_NUDGE_WRITE


async def test_safeguards_without_session_resends_original_text():
    """首轮就被拦、无 session 可 resume（只读场景）：换模型原样重发用户诉求。"""
    bot = _bot()
    session = _session()
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append((kwargs["message"], kwargs["model"]))
        if len(calls) == 1:
            exc = RuntimeError(
                "Claude API 错误（stream_error）：API Error: Fable 5's "
                "safeguards flagged this message."
            )
            exc.cc_retryable_resume = False
            raise exc
        return "查完了", "sid_new", False

    await _run(bot, session, fake_run_agent, text="帮我查一下余额")

    assert len(calls) == 2
    assert calls[1] == ("帮我查一下余额", "opus[1m]")


async def test_safeguards_fallback_model_env_alias(monkeypatch):
    """env 可改降级模型，且支持 /model 同款别名。"""
    monkeypatch.setenv("CC_LARK_SAFEGUARDS_FALLBACK_MODEL", "opus5")
    bot = _bot()
    session = _session()
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise _safeguards_exc("sid_sg")
        return "好了", "sid_sg", False

    await _run(bot, session, fake_run_agent)

    assert calls[1] == "claude-opus-5[1m]"
