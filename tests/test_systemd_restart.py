import subprocess
from unittest.mock import MagicMock, mock_open, patch

import pytest

import commands


def _systemd_properties(*, pid="4242", active="active", restart="always"):
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=f"MainPID={pid}\nActiveState={active}\nRestart={restart}\n",
    )


def test_systemd_unit_detects_current_restartable_service():
    cgroup = "0::/system.slice/cc-lark.service\n"
    with (
        patch.object(commands.sys, "platform", "linux"),
        patch("builtins.open", mock_open(read_data=cgroup)),
        patch.object(commands.os, "getpid", return_value=4242),
        patch.object(
            commands.subprocess,
            "run",
            return_value=_systemd_properties(),
        ) as run,
    ):
        assert commands._systemd_unit() == "cc-lark.service"

    run.assert_called_once_with(
        [
            "systemctl", "show",
            "--property=MainPID",
            "--property=ActiveState",
            "--property=Restart",
            "--no-pager",
            "--",
            "cc-lark.service",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
    )


@pytest.mark.parametrize(
    ("pid", "active", "restart"),
    [
        ("9999", "active", "always"),
        ("4242", "inactive", "always"),
        ("4242", "active", "no"),
    ],
)
def test_systemd_unit_rejects_unsafe_service(pid, active, restart):
    with (
        patch.object(commands.sys, "platform", "linux"),
        patch(
            "builtins.open",
            mock_open(read_data="0::/system.slice/cc-lark.service\n"),
        ),
        patch.object(commands.os, "getpid", return_value=4242),
        patch.object(
            commands.subprocess,
            "run",
            return_value=_systemd_properties(
                pid=pid,
                active=active,
                restart=restart,
            ),
        ),
    ):
        assert commands._systemd_unit() is None


def test_systemd_unit_rejects_missing_cgroup_or_failed_probe():
    with (
        patch.object(commands.sys, "platform", "linux"),
        patch("builtins.open", mock_open(read_data="0::/user.slice/session.scope\n")),
        patch.object(commands.subprocess, "run") as run,
    ):
        assert commands._systemd_unit() is None
        run.assert_not_called()

    with (
        patch.object(commands.sys, "platform", "linux"),
        patch(
            "builtins.open",
            mock_open(read_data="0::/system.slice/cc-lark.service\n"),
        ),
        patch.object(
            commands.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("systemctl", 5),
        ),
    ):
        assert commands._systemd_unit() is None


def test_restart_strategy_prefers_systemd_before_app():
    with (
        patch.object(commands, "_launchd_target", return_value=None),
        patch.object(commands, "_systemd_unit", return_value="cc-lark.service"),
        patch.object(commands.os.path, "isdir", return_value=True),
    ):
        assert commands.restart_strategy() == "systemd"


def test_trigger_restart_exits_for_verified_systemd_without_shelling_out():
    loop = MagicMock()
    with (
        patch.object(commands, "_launchd_target", return_value=None),
        patch.object(commands, "_systemd_unit", return_value="cc-lark.service"),
        patch.object(commands.asyncio, "get_event_loop", return_value=loop),
        patch.object(commands.subprocess, "Popen") as popen,
    ):
        commands._trigger_restart()

    popen.assert_not_called()
    loop.call_later.assert_called_once()
    delay, callback = loop.call_later.call_args.args
    assert delay == 1.0
    with patch.object(commands.os, "_exit") as exit_process:
        callback()
    exit_process.assert_called_once_with(0)


def test_trigger_restart_refuses_bare_process():
    loop = MagicMock()
    with (
        patch.object(commands, "_launchd_target", return_value=None),
        patch.object(commands, "_systemd_unit", return_value=None),
        patch.object(commands.os.path, "isdir", return_value=False),
        patch.object(commands.asyncio, "get_event_loop", return_value=loop),
        patch.object(commands.subprocess, "Popen") as popen,
    ):
        with pytest.raises(RuntimeError, match="no supported supervisor"):
            commands._trigger_restart()

    popen.assert_not_called()
    loop.call_later.assert_not_called()


async def test_restart_command_reports_systemd_strategy():
    with (
        patch.object(commands, "restart_strategy", return_value="systemd"),
        patch.object(commands, "_trigger_restart") as trigger,
    ):
        reply = await commands.handle_command(
            "restart", "", "ou_user", "oc_chat", MagicMock(),
        )

    trigger.assert_called_once_with()
    assert "systemd" in reply
    assert "Restart=always" in reply
