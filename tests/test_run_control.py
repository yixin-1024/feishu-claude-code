import asyncio
import signal
import unittest
from unittest import mock

import run_control
from run_control import ActiveRunRegistry, _kill_pgroup, stop_run


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls = 0

    def terminate(self):
        self.terminate_called = True
        self.returncode = -15

    def kill(self):
        self.kill_called = True
        self.returncode = -9

    async def wait(self):
        self.wait_calls += 1
        return self.returncode


class NeverReapsProc(FakeProc):
    def terminate(self):
        self.terminate_called = True

    def kill(self):
        self.kill_called = True

    async def wait(self):
        self.wait_calls += 1
        await asyncio.Event().wait()


class RunControlTests(unittest.IsolatedAsyncioTestCase):
    def test_kill_pgroup_refuses_bot_shared_group(self):
        # pid == pgid，但这个组恰好就是 bot 自己的组：只命中 self-group 防线。
        proc = mock.Mock(pid=900)
        with (
            mock.patch("run_control.os.getpgid", return_value=900),
            mock.patch("run_control.os.getpgrp", return_value=900),
            mock.patch("run_control.os.killpg") as killpg,
        ):
            self.assertFalse(_kill_pgroup(proc, signal.SIGTERM))
        killpg.assert_not_called()

    def test_kill_pgroup_refuses_process_that_is_not_group_leader(self):
        proc = mock.Mock(pid=1234)
        with (
            mock.patch("run_control.os.getpgid", return_value=800),
            mock.patch("run_control.os.getpgrp", return_value=900),
            mock.patch("run_control.os.killpg") as killpg,
        ):
            self.assertFalse(_kill_pgroup(proc, signal.SIGTERM))
        killpg.assert_not_called()

    def test_kill_pgroup_allows_isolated_group_leader(self):
        proc = mock.Mock(pid=1234)
        with (
            mock.patch("run_control.os.getpgid", return_value=1234),
            mock.patch("run_control.os.getpgrp", return_value=900),
            mock.patch("run_control.os.killpg") as killpg,
        ):
            self.assertTrue(_kill_pgroup(proc, signal.SIGTERM))
        killpg.assert_called_once_with(1234, signal.SIGTERM)

    async def test_stop_run_returns_false_when_no_active_run(self):
        registry = ActiveRunRegistry()

        stopped = await stop_run(registry, "user-1", "chat-1")

        self.assertFalse(stopped)

    async def test_stop_run_terminates_active_process_and_marks_state(self):
        registry = ActiveRunRegistry()
        run = registry.start_run("user-1", "chat-1", "card-1")
        proc = FakeProc()
        registry.attach_process("user-1", "chat-1", proc)
        stopped_runs = []

        async def on_stopped(active_run):
            stopped_runs.append(active_run)

        stopped = await stop_run(registry, "user-1", "chat-1", on_stopped=on_stopped)

        self.assertTrue(stopped)
        self.assertTrue(run.stop_requested)
        self.assertTrue(run.stop_announced)
        self.assertTrue(proc.terminate_called)
        self.assertFalse(proc.kill_called)
        self.assertEqual(proc.wait_calls, 1)
        self.assertEqual(stopped_runs, [run])

    async def test_attach_process_terminates_if_stop_was_requested_earlier(self):
        registry = ActiveRunRegistry()
        run = registry.start_run("user-1", "chat-1", "card-1")
        run.stop_requested = True
        proc = FakeProc()

        registry.attach_process("user-1", "chat-1", proc)

        self.assertIs(run.proc, proc)
        self.assertTrue(proc.terminate_called)

    async def test_stop_run_does_not_hang_after_kill_wait_timeout(self):
        registry = ActiveRunRegistry()
        run = registry.start_run("user-1", "chat-1", "card-1")
        proc = NeverReapsProc()
        registry.attach_process("user-1", "chat-1", proc)
        stopped_runs = []

        async def on_stopped(active_run):
            stopped_runs.append(active_run)

        with mock.patch.object(run_control, "_KILL_WAIT_TIMEOUT_SECONDS", 0.01):
            stopped = await asyncio.wait_for(
                stop_run(
                    registry, "user-1", "chat-1",
                    on_stopped=on_stopped, grace_seconds=0.01,
                ),
                timeout=0.2,
            )

        self.assertTrue(stopped)
        self.assertTrue(proc.terminate_called)
        self.assertTrue(proc.kill_called)
        self.assertEqual(proc.wait_calls, 2)
        self.assertEqual(stopped_runs, [run])

    async def test_runs_are_isolated_per_chat(self):
        registry = ActiveRunRegistry()
        run_a = registry.start_run("user-1", "chat-a", "card-a")
        run_b = registry.start_run("user-1", "chat-b", "card-b")

        self.assertIs(registry.get_run("user-1", "chat-a"), run_a)
        self.assertIs(registry.get_run("user-1", "chat-b"), run_b)

        registry.clear_run("user-1", "chat-a", run_a)
        self.assertIsNone(registry.get_run("user-1", "chat-a"))
        self.assertIs(registry.get_run("user-1", "chat-b"), run_b)


if __name__ == "__main__":
    unittest.main()
