import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import commands
import dispatcher


@pytest.fixture(autouse=True)
def reset_restart_state():
    old_bots = dispatcher._bots
    old_committed = dispatcher._restart_committed
    dispatcher._restart_in_progress = False
    dispatcher._restart_committed = False
    yield
    dispatcher._restart_in_progress = False
    dispatcher._restart_committed = old_committed
    dispatcher._bots = old_bots


def _bot(active_runs=1):
    bot = SimpleNamespace(
        profile=SimpleNamespace(name="test"),
        feishu=AsyncMock(),
        active_runs=SimpleNamespace(
            _runs={f"run-{i}": object() for i in range(active_runs)},
            start_run=MagicMock(),
        ),
    )
    dispatcher._bots = {"test": bot}
    return bot


async def test_restart_orders_notice_before_drain_before_trigger():
    bot = _bot(active_runs=2)
    events = []

    async def send_text(_user_id, content):
        assert "中断 2 个未完成任务" in content
        events.append("notice")

    async def drain(_bot):
        events.append("drain")
        return 2

    bot.feishu.send_text_to_user.side_effect = send_text

    with (
        patch.object(commands, "restart_strategy", return_value="launchd"),
        patch.object(commands, "_trigger_restart", side_effect=lambda: events.append("trigger")),
        patch.object(dispatcher, "_handle_restart_command", side_effect=drain),
    ):
        restarted = await dispatcher._handle_restart_request(
            bot, "ou_user", False, "om_restart",
        )

    assert restarted is True
    assert events == ["notice", "drain", "trigger"]


async def test_restart_notice_failure_is_logged_but_restart_continues():
    bot = _bot()
    bot.feishu.send_text_to_user.side_effect = RuntimeError("lark down")
    drain = AsyncMock(return_value=1)
    trigger = MagicMock()

    with (
        patch.object(commands, "restart_strategy", return_value="launchd"),
        patch.object(commands, "_trigger_restart", trigger),
        patch.object(dispatcher, "_handle_restart_command", drain),
        patch.object(dispatcher, "log") as log,
    ):
        restarted = await dispatcher._handle_restart_request(
            bot, "ou_user", False, "om_restart",
        )

    assert restarted is True
    drain.assert_awaited_once_with(bot)
    trigger.assert_called_once_with()
    assert any(
        call.args[1:3] == ("restart", "warn")
        and "om_restart" in call.args[3]
        and "lark down" in call.args[3]
        for call in log.call_args_list
    )


async def test_group_restart_replies_before_drain():
    bot = _bot()
    events = []

    async def reply_text(message_id, content):
        assert message_id == "om_group_restart"
        assert "正在立即重启" in content
        events.append("notice")

    async def drain(_bot):
        events.append("drain")
        return 1

    bot.feishu.reply_text.side_effect = reply_text
    with (
        patch.object(commands, "restart_strategy", return_value="launchd"),
        patch.object(commands, "_trigger_restart", side_effect=lambda: events.append("trigger")),
        patch.object(dispatcher, "_handle_restart_command", side_effect=drain),
    ):
        restarted = await dispatcher._handle_restart_request(
            bot, "ou_user", True, "om_group_restart",
        )

    assert restarted is True
    assert events == ["notice", "drain", "trigger"]
    bot.feishu.send_text_to_user.assert_not_awaited()


async def test_restart_notice_timeout_still_drains_and_triggers():
    bot = _bot()

    async def never_sends(_user_id, _content):
        await asyncio.Event().wait()

    bot.feishu.send_text_to_user.side_effect = never_sends
    with (
        patch.object(commands, "restart_strategy", return_value="launchd"),
        patch.object(commands, "_trigger_restart") as trigger,
        patch.object(dispatcher, "_handle_restart_command", new_callable=AsyncMock) as drain,
        patch.object(dispatcher, "_RESTART_NOTICE_TIMEOUT_SECONDS", 0.01),
        patch.object(dispatcher, "log") as log,
    ):
        restarted = await dispatcher._handle_restart_request(
            bot, "ou_user", False, "om_restart",
        )

    assert restarted is True
    drain.assert_awaited_once_with(bot)
    trigger.assert_called_once_with()
    assert any(
        call.args[1:3] == ("restart", "warn")
        and "om_restart" in call.args[3]
        for call in log.call_args_list
    )


async def test_restart_drain_timeout_still_triggers_restart():
    bot = _bot()

    async def never_finishes(_bot):
        await asyncio.Event().wait()

    with (
        patch.object(commands, "restart_strategy", return_value="launchd"),
        patch.object(commands, "_trigger_restart") as trigger,
        patch.object(dispatcher, "_handle_restart_command", side_effect=never_finishes),
        patch.object(dispatcher, "_RESTART_DRAIN_TIMEOUT_SECONDS", 0.01),
    ):
        restarted = await dispatcher._handle_restart_request(
            bot, "ou_user", False, "om_restart",
        )

    assert restarted is True
    trigger.assert_called_once_with()


async def test_restart_finalizes_interrupted_streaming_cards():
    run = SimpleNamespace(
        user_id="ou_user",
        chat_id="oc_group:omt_thread",
        card_msg_id="om_active_card",
        card_update_lock=asyncio.Lock(),
    )
    bot = _bot(active_runs=0)
    bot.active_runs._runs = {"active": run}

    async def stop_and_announce(
        _registry, _user_id, _chat_id, *, on_stopped, grace_seconds,
    ):
        assert grace_seconds == 1.5
        await on_stopped(run)
        return True

    with patch.object(dispatcher, "stop_run", side_effect=stop_and_announce):
        affected = await dispatcher._handle_restart_command(bot)

    assert affected == 1
    bot.feishu.update_card.assert_awaited_once()
    bot.feishu.finalize_streaming_card.assert_awaited_once_with("om_active_card")


async def test_duplicate_restart_only_triggers_once():
    bot = _bot()
    drain = AsyncMock(return_value=1)
    trigger = MagicMock()

    with (
        patch.object(commands, "restart_strategy", return_value="launchd"),
        patch.object(commands, "_trigger_restart", trigger),
        patch.object(dispatcher, "_handle_restart_command", drain),
    ):
        assert await dispatcher._handle_restart_request(
            bot, "ou_user", False, "om_first",
        ) is True
        assert await dispatcher._handle_restart_request(
            bot, "ou_user", False, "om_second",
        ) is True

    drain.assert_awaited_once_with(bot)
    trigger.assert_called_once_with()
    assert "重启请求已经在处理中" in bot.feishu.send_text_to_user.await_args_list[-1].args[1]


async def test_bare_process_refuses_restart():
    bot = _bot()
    with (
        patch.object(commands, "restart_strategy", return_value="bare"),
        patch.object(commands, "_trigger_restart") as trigger,
        patch.object(dispatcher, "_handle_restart_command", new_callable=AsyncMock) as drain,
    ):
        restarted = await dispatcher._handle_restart_request(
            bot, "ou_user", False, "om_restart",
        )

    assert restarted is False
    assert dispatcher._restart_in_progress is False
    drain.assert_not_awaited()
    trigger.assert_not_called()


async def test_trigger_failure_reopens_restart_gate():
    bot = _bot()
    with (
        patch.object(commands, "restart_strategy", return_value="launchd"),
        patch.object(commands, "_trigger_restart", side_effect=RuntimeError("kickstart failed")),
        patch.object(dispatcher, "_handle_restart_command", new_callable=AsyncMock),
    ):
        restarted = await dispatcher._handle_restart_request(
            bot, "ou_user", False, "om_restart",
        )

    assert restarted is False
    assert dispatcher._restart_in_progress is False
    assert "触发重启失败" in bot.feishu.send_text_to_user.await_args_list[-1].args[1]


async def test_cancelled_restart_probe_reopens_restart_gate():
    bot = _bot()
    probe_started = asyncio.Event()
    hold_probe = asyncio.Event()

    async def slow_probe(_func):
        probe_started.set()
        await hold_probe.wait()
        return "systemd"

    with (
        patch.object(dispatcher.asyncio, "to_thread", new=slow_probe),
        patch.object(commands, "_trigger_restart") as trigger,
        patch.object(dispatcher, "_handle_restart_command", new_callable=AsyncMock) as drain,
    ):
        restart_task = asyncio.create_task(dispatcher._handle_restart_request(
            bot, "ou_user", False, "om_restart",
        ))
        await probe_started.wait()
        assert dispatcher._restart_in_progress is True

        restart_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await restart_task

    assert dispatcher._restart_in_progress is False
    assert dispatcher._restart_committed is False
    drain.assert_not_awaited()
    trigger.assert_not_called()


async def test_menu_restart_uses_same_orchestration():
    bot = _bot()
    bot.store = MagicMock()
    with patch.object(dispatcher, "_handle_restart_request", new_callable=AsyncMock) as restart:
        await dispatcher.handle_menu_command(
            bot, "ou_user", "oc_group", "/restart", "om_card",
        )

    restart.assert_awaited_once_with(
        bot, "ou_user", True, "", card_msg_id="om_card",
    )


async def test_new_run_is_rejected_during_restart():
    bot = _bot()
    dispatcher._restart_in_progress = True

    await dispatcher._run_and_display(
        bot, "ou_user", "oc_group", True,
        "do work", "om_card", MagicMock(), "om_anchor",
    )

    bot.active_runs.start_run.assert_not_called()
    bot.feishu.update_card.assert_awaited_once()
    assert "未执行" in bot.feishu.update_card.await_args.args[1]


async def test_stopped_runner_partial_result_cannot_overwrite_interrupt_notice():
    active_run = SimpleNamespace(
        stop_requested=False,
        card_update_lock=asyncio.Lock(),
    )
    bot = _bot(active_runs=0)
    bot.active_runs.start_run.return_value = active_run
    bot.active_runs.clear_run = MagicMock()
    bot.store = AsyncMock()
    session = SimpleNamespace(
        runner="claude",
        model="claude-test",
        session_id=None,
        cwd="/tmp",
        permission_mode="bypassPermissions",
    )

    async def stopped_with_partial(**_kwargs):
        active_run.stop_requested = True
        return "partial output", "sid-partial", False

    with patch.object(dispatcher, "run_agent", side_effect=stopped_with_partial):
        result = await dispatcher._run_and_display(
            bot, "ou_user", "ou_user", False,
            "do work", "om_card", session, "om_anchor",
        )

    assert result is None
    bot.feishu.update_card.assert_not_awaited()
    bot.feishu.send_text_to_user.assert_not_awaited()
    bot.active_runs.clear_run.assert_called_once_with("ou_user", "ou_user", active_run)


def _display_test_context():
    active_run = SimpleNamespace(
        stop_requested=False,
        card_update_lock=asyncio.Lock(),
    )
    bot = _bot(active_runs=0)
    bot.active_runs.start_run.return_value = active_run
    bot.active_runs.clear_run = MagicMock()
    bot.store = AsyncMock()
    session = SimpleNamespace(
        runner="claude",
        model="claude-test",
        session_id=None,
        cwd="/tmp",
        permission_mode="bypassPermissions",
    )
    return bot, active_run, session


async def test_restart_during_final_card_update_suppresses_success_notice():
    bot, _active_run, session = _display_test_context()

    async def restart_while_patching(_message_id, _content):
        dispatcher._restart_in_progress = True
        dispatcher._restart_committed = True

    # 终态写现在走 update_card_final（收尾确认写，抗飞书 patch 乱序）。
    bot.feishu.update_card_final.side_effect = restart_while_patching
    with patch.object(
        dispatcher, "run_agent",
        new=AsyncMock(return_value=("finished", "sid-finished", False)),
    ):
        result = await dispatcher._run_and_display(
            bot, "ou_user", "ou_user", False,
            "do work", "om_card", session, "om_anchor",
        )

    assert result is None
    bot.feishu.update_card_final.assert_awaited_once_with("om_card", "finished")
    bot.feishu.send_text_to_user.assert_not_awaited()


async def test_restart_during_error_card_update_suppresses_error_notice():
    bot, _active_run, session = _display_test_context()

    async def restart_while_patching(_message_id, _content):
        dispatcher._restart_in_progress = True
        dispatcher._restart_committed = True

    # 报错终态卡也走 update_card_final。
    bot.feishu.update_card_final.side_effect = restart_while_patching
    with patch.object(
        dispatcher, "run_agent",
        new=AsyncMock(side_effect=ValueError("runner failed")),
    ):
        result = await dispatcher._run_and_display(
            bot, "ou_user", "ou_user", False,
            "do work", "om_card", session, "om_anchor",
        )

    assert result is None
    bot.feishu.update_card_final.assert_awaited_once()
    assert "runner failed" in bot.feishu.update_card_final.await_args.args[1]
    bot.feishu.send_text_to_user.assert_not_awaited()


async def test_restart_during_failed_card_update_suppresses_result_fallback():
    bot, _active_run, session = _display_test_context()

    async def fail_as_restart_starts(_message_id, _content):
        dispatcher._restart_in_progress = True
        dispatcher._restart_committed = True
        raise RuntimeError("card request interrupted")

    bot.feishu.update_card_final.side_effect = fail_as_restart_starts
    with patch.object(
        dispatcher, "run_agent",
        new=AsyncMock(return_value=("finished", "sid-finished", False)),
    ):
        result = await dispatcher._run_and_display(
            bot, "ou_user", "ou_user", False,
            "do work", "om_card", session, "om_anchor",
        )

    assert result is None
    bot.feishu.send_text_to_user.assert_not_awaited()
    bot.feishu.reply_card.assert_not_awaited()


async def test_cancelled_restart_probe_does_not_swallow_existing_run_result():
    bot, _active_run, session = _display_test_context()
    agent_started = asyncio.Event()
    finish_agent = asyncio.Event()
    probe_started = threading.Event()
    finish_probe = threading.Event()

    async def runner_finishes_during_probe(**_kwargs):
        agent_started.set()
        await finish_agent.wait()
        return "finished during probe", "sid-finished", False

    def slow_bare_probe():
        probe_started.set()
        finish_probe.wait(timeout=2.0)
        return "bare"

    with (
        patch.object(dispatcher, "run_agent", side_effect=runner_finishes_during_probe),
        patch.object(commands, "restart_strategy", side_effect=slow_bare_probe),
        patch.object(commands, "_trigger_restart") as trigger,
    ):
        display_task = asyncio.create_task(dispatcher._run_and_display(
            bot, "ou_user", "ou_user", False,
            "do work", "om_card", session, "om_anchor",
        ))
        await agent_started.wait()

        restart_task = asyncio.create_task(dispatcher._handle_restart_request(
            bot, "ou_user", False, "om_restart",
        ))
        assert await asyncio.to_thread(probe_started.wait, 1.0)
        assert dispatcher._restart_in_progress is True
        assert dispatcher._restart_committed is False

        finish_agent.set()
        result = await display_task
        finish_probe.set()
        restarted = await restart_task

    assert result == "finished during probe"
    assert restarted is False
    assert dispatcher._restart_in_progress is False
    assert dispatcher._restart_committed is False
    bot.feishu.update_card_final.assert_awaited_once_with("om_card", "finished during probe")
    assert any(call.args[1] == "✅" for call in bot.feishu.send_text_to_user.await_args_list)
    trigger.assert_not_called()


async def test_commands_restart_fallback_never_triggers_directly():
    with patch.object(commands, "_trigger_restart") as trigger:
        reply = await commands.handle_command(
            "restart", "", "ou_user", "chat", MagicMock(),
        )

    trigger.assert_not_called()
    assert "消息分发器" in reply
