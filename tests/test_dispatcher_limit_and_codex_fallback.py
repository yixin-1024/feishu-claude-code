"""用量墙紧急切账户 + resume、配额恢复自动唤醒、codex 模型被拒 fallback（_execute_run 分支）。

线上 7 天：spx 15 轮直接 ❌ 在 "Claude Max 用量已达上限"，用户手点 13 次切账户；
regtank 3 轮全部 400 "gpt-5.6-sol model is not supported when using Codex with a
ChatGPT account"。
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
import scheduler
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


def _session(session_id=None, model="claude-opus-5[1m]", runner="claude"):
    return mock.Mock(
        session_id=session_id, model=model, effort=None, cwd="/tmp",
        permission_mode="bypassPermissions", runner=runner,
    )


def _limit_exc(session_id="sid_rl", text="You've hit your session limit · resets 12:20pm (Asia/Shanghai)"):
    exc = RuntimeError(f"Claude Max 用量已达上限：{text}")
    exc.cc_session_id = session_id
    exc.cc_retryable_resume = False
    return exc


async def _run(bot, session, fake_run_agent, chat_id="c1", text="帮我查一下余额"):
    with mock.patch.object(dispatcher, "run_agent", fake_run_agent):
        await dispatcher._run_and_display(
            bot, user_id="u1", chat_id=chat_id, is_group=True,
            text=text, card_msg_id="card_id_1", session=session, notify_msg_id="msg_orig",
        )


def _notices(bot):
    return [c.args[1] for c in bot.feishu.reply_text.await_args_list if len(c.args) > 1]


def _final_body(bot):
    final = bot.feishu.update_card_final.await_args
    return final.args[1] if len(final.args) > 1 else final.kwargs.get("content", "")


async def test_rate_limit_switches_account_and_resumes_same_session(monkeypatch):
    bot = _bot()
    session = _session()
    calls = []
    switch_calls = []

    def fake_switch():
        switch_calls.append(1)
        return {"switched": "reg", "from": "mar", "reason": "5h 已用 12% / 7d 已用 40%",
                "current_reset_epoch": None}

    monkeypatch.setattr(dispatcher, "_emergency_account_switch", fake_switch)

    async def fake_run_agent(**kwargs):
        calls.append((kwargs["message"], kwargs["session_id"]))
        if len(calls) == 1:
            raise _limit_exc("sid_rl")
        return "余额 100 USD", "sid_rl", False

    await _run(bot, session, fake_run_agent)

    assert switch_calls == [1]
    assert len(calls) == 2, "撞墙后应切账户并自动续跑"
    assert calls[1][1] == "sid_rl" and calls[1][0] == dispatcher._LIMIT_RESUME_NUDGE
    assert any("reg" in t and "用量墙" in t for t in _notices(bot)), _notices(bot)
    body = _final_body(bot)
    assert "余额 100 USD" in body and "❌" not in body


async def test_rate_limit_without_spare_account_schedules_wake_at_reset(monkeypatch):
    bot = _bot()
    session = _session()
    reset_at = time.time() + 30 * 60

    monkeypatch.setattr(dispatcher, "_emergency_account_switch",
                        lambda: {"switched": None, "from": "mar", "reason": "无可切换账户",
                                 "current_reset_epoch": reset_at})
    wakes = []

    def fake_schedule_wake(**kw):
        wakes.append(kw)
        return {"ok": True, "fire_at_local": "09/02 12:22", "job_id": "wake-x"}

    monkeypatch.setattr(scheduler, "schedule_wake", fake_schedule_wake)

    async def fake_run_agent(**kwargs):
        raise _limit_exc("sid_rl")

    await _run(bot, session, fake_run_agent, chat_id="oc_1:omt_thread1")

    assert len(wakes) == 1, "没有可切账户时应在话题里排配额恢复唤醒"
    w = wakes[0]
    assert w["thread_id"] == "omt_thread1" and w["chat_id"] == "oc_1" and w["anchor_message_id"] == "msg_orig"
    assert 30 <= w["minutes"] <= 33
    assert "用量墙" in w["note"] and "帮我查一下余额" in w["note"]
    body = _final_body(bot)
    assert "❌" in body and "⏰" in body and "12:22" in body
    # 崩溃前 session 仍然要保存，唤醒后才能 resume
    bot.store.on_agent_response.assert_awaited()


async def test_rate_limit_wake_falls_back_to_parsing_reset_text(monkeypatch):
    bot = _bot()
    session = _session()
    monkeypatch.setattr(dispatcher, "_emergency_account_switch",
                        lambda: {"switched": None, "from": None, "reason": "没有 saved 账户",
                                 "current_reset_epoch": None})
    wakes = []
    monkeypatch.setattr(scheduler, "schedule_wake",
                        lambda **kw: wakes.append(kw) or {"ok": True, "fire_at_local": "x", "job_id": "j"})

    async def fake_run_agent(**kwargs):
        raise _limit_exc("sid_rl", text="You've hit your session limit · resets 11pm (Asia/Shanghai)")

    await _run(bot, session, fake_run_agent, chat_id="oc_1:omt_t")
    assert len(wakes) == 1 and 1 <= wakes[0]["minutes"] <= 1440


async def test_rate_limit_in_private_chat_has_no_wake(monkeypatch):
    bot = _bot()
    session = _session()
    monkeypatch.setattr(dispatcher, "_emergency_account_switch",
                        lambda: {"switched": None, "from": "mar", "reason": "x", "current_reset_epoch": time.time() + 600})
    called = []
    monkeypatch.setattr(scheduler, "schedule_wake", lambda **kw: called.append(kw))

    async def fake_run_agent(**kwargs):
        raise _limit_exc("sid_rl")

    await _run(bot, session, fake_run_agent, chat_id="c1")  # 无 thread
    assert called == []
    assert "❌" in _final_body(bot)


def test_parse_reset_minutes():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 9, 2, 11, 0, tzinfo=tz).timestamp()
    assert dispatcher._parse_reset_minutes("hit your session limit · resets 12:20pm (Asia/Shanghai)", now) == 82
    assert dispatcher._parse_reset_minutes("resets 11pm", now) == 12 * 60 + 2
    # 已过的时间点 → 明天同一时刻
    assert dispatcher._parse_reset_minutes("resets 10am", now) == 23 * 60 + 2
    assert dispatcher._parse_reset_minutes("no reset info here", now) is None
    # 超 24h（weekly limit 带日期）→ None，不排 wake
    assert dispatcher._parse_reset_minutes("resets Sep 5 at 3pm", now) is None


def test_rate_limit_detection():
    assert dispatcher._is_rate_limit_error(_limit_exc())
    assert not dispatcher._is_rate_limit_error(RuntimeError("API Error: Connection closed mid-response."))
    assert dispatcher._limit_detail(_limit_exc()).startswith("You've hit your session limit")


async def test_codex_unsupported_model_falls_back_and_resends(monkeypatch):
    monkeypatch.delenv("CODEX_MODEL_FALLBACK", raising=False)
    bot = _bot()
    session = _session(session_id="thr_1", model="gpt-5.6-sol", runner="codex")
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append((kwargs["message"], kwargs["model"], kwargs["session_id"]))
        if len(calls) == 1:
            raise RuntimeError(
                '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
                '"message":"The \'gpt-5.6-sol\' model is not supported when using Codex with a ChatGPT account."}}'
            )
        return "done", "thr_1", False

    await _run(bot, session, fake_run_agent, text="审一下这个 PR")

    assert len(calls) == 2
    assert calls[1][1] == "gpt-5.5", "应换 fallback 模型"
    assert calls[1][0].endswith("审一下这个 PR"), "原样重发用户诉求（带【本轮】头）"
    assert calls[1][2] == "thr_1"
    # 不粘住：不写 model_override
    bot.store.set_model_override.assert_not_awaited()
    assert any("gpt-5.5" in t and "gpt-5.6-sol" in t for t in _notices(bot))
    assert "done" in _final_body(bot)


async def test_codex_fallback_disabled_by_empty_env(monkeypatch):
    monkeypatch.setenv("CODEX_MODEL_FALLBACK", "")
    bot = _bot()
    session = _session(session_id="thr_1", model="gpt-5.6-sol", runner="codex")
    calls = []

    async def fake_run_agent(**kwargs):
        calls.append(1)
        raise RuntimeError("The 'gpt-5.6-sol' model is not supported when using Codex with a ChatGPT account.")

    await _run(bot, session, fake_run_agent)
    assert len(calls) == 1
    assert "❌" in _final_body(bot)


def test_acl_log_dedupes_per_chat(monkeypatch):
    lines = []
    monkeypatch.setattr(dispatcher, "log", lambda *a: lines.append(a[3]))
    dispatcher._acl_log_last.clear()
    dispatcher._acl_log_suppressed.clear()
    for _ in range(5):
        dispatcher._log_acl_group_skip("spx", "oc_noisy_group")
    assert len(lines) == 1
    dispatcher._acl_log_last["oc_noisy_group"] = time.time() - 4000
    dispatcher._log_acl_group_skip("spx", "oc_noisy_group")
    assert len(lines) == 2 and "另有 4 条" in lines[1]
