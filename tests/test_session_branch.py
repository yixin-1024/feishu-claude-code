"""/rewind /fork：下次 resume 的参数（session_branch）+ 回退点计算 + runner 带参。"""

import asyncio
import json
import os
import sys
from unittest import mock

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import session_branch


@pytest.fixture(autouse=True)
def opts_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_LARK_RESUME_OPTS_FILE", str(tmp_path / "opts.json"))


def _write_session(path, recs):
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def _u(uid, parent, text):
    return {"type": "user", "uuid": uid, "parentUuid": parent, "message": {"role": "user", "content": text}}


def _a(uid, parent, text="ok"):
    return {"type": "assistant", "uuid": uid, "parentUuid": parent,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _tool_result(uid, parent):
    return {"type": "user", "uuid": uid, "parentUuid": parent,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}}


def test_cli_args_shapes():
    assert session_branch.cli_args("", None) == []
    assert session_branch.cli_args("S", None) == ["--resume", "S"]
    assert session_branch.cli_args("S", {"resume_at": "A"}) == ["--resume", "S", "--resume-session-at", "A"]
    assert session_branch.cli_args("N", {"fork_from": "S"}) == [
        "--resume", "S", "--fork-session", "--session-id", "N"]
    assert session_branch.cli_args("N", {"fork_from": "S", "resume_at": "A"}) == [
        "--resume", "S", "--resume-session-at", "A", "--fork-session", "--session-id", "N"]


def test_options_persist_consume_and_give_up_after_failed_starts():
    session_branch.set_option("S", resume_at="A")
    assert session_branch.get_option("S") == {"resume_at": "A"}
    session_branch.consume("S")
    assert session_branch.get_option("S") is None
    session_branch.set_option("S", resume_at="A")
    session_branch.note_failed_start("S")
    assert session_branch.get_option("S")["failed_starts"] == 1
    session_branch.note_failed_start("S")
    assert session_branch.get_option("S") is None


def test_rewind_target_walks_active_branch(tmp_path):
    f = tmp_path / "s.jsonl"
    _write_session(f, [
        _u("u1", None, "【本轮 · 消息 id: om_1】\n\n记住 APPLE"),
        _a("a1", "u1"),
        _u("u2", "a1", "记住 BANANA"),
        _a("a2t", "u2", ""), _tool_result("t2", "a2t"), _a("a2", "t2"),
        # 之前退回过一次：从 a1 长出的新分支在文件最后
        _u("u3", "a1", "【话题新增 · 1 条】\n…\n【用户刚刚 @ 你并说】\n记住 CHERRY"),
        _a("a3", "u3"),
    ])
    target, undone = session_branch.rewind_target(str(f), 1)
    assert target == "a1" and undone == ["记住 CHERRY"]
    # 在废弃的分支上数：从 a2 往上
    target, undone = session_branch.rewind_target(str(f), 1, leaf_uuid="a2")
    assert target == "a1" and undone == ["记住 BANANA"]
    with pytest.raises(ValueError, match="第一轮"):
        session_branch.rewind_target(str(f), 2)
    with pytest.raises(ValueError, match="只有 2 轮"):
        session_branch.rewind_target(str(f), 3)


async def test_runner_passes_and_consumes_branch_args(monkeypatch):
    import claude_runner
    from tests.test_claude_runner_stream_input import FakeProc, _result

    session_branch.set_option("NEW", fork_from="OLD")
    proc = FakeProc()
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("CLAUDE_PRINT_STREAM_INPUT", "1")
    task = asyncio.create_task(claude_runner.run_claude("hi", session_id="NEW"))
    while not proc.stdin.lines:
        await asyncio.sleep(0.005)
    args = list(captured["args"])
    i = args.index("--resume")
    assert args[i:i + 5] == ["--resume", "OLD", "--fork-session", "--session-id", "NEW"]
    assert session_branch.get_option("NEW")  # 还没确认 CLI 接受，先不删
    proc.stdout.feed({"type": "system", "subtype": "init", "session_id": "NEW"})
    proc.stdout.feed(_result("ok", [proc.stdin.lines[0]["uuid"]]))
    await asyncio.wait_for(task, 2)
    assert session_branch.get_option("NEW") is None


# ── dispatcher 的 /rewind /fork ──────────────────────────────────────


def _bot(session):
    import dispatcher  # noqa: F401
    from bot_config import Profile
    from bot_instance import BotInstance
    from run_control import ActiveRunRegistry
    bot = BotInstance.__new__(BotInstance)
    bot.profile = Profile(name="test", app_id="cli_x", app_secret="s",
                          platform="lark", domain="open.larksuite.com", default_cwd="/tmp")
    bot.feishu = mock.AsyncMock()
    bot.active_runs = ActiveRunRegistry()
    bot.store = mock.MagicMock()
    bot.store.get_current = mock.AsyncMock(return_value=session)
    bot.store.handover_session = mock.AsyncMock(return_value={})
    return bot


async def test_rewind_command_sets_option_and_stacks(tmp_path, monkeypatch):
    import dispatcher
    f = tmp_path / "S.jsonl"
    _write_session(f, [_u("u1", None, "一"), _a("a1", "u1"), _u("u2", "a1", "二"), _a("a2", "u2"),
                       _u("u3", "a2", "三"), _a("a3", "u3")])
    monkeypatch.setattr(dispatcher, "_find_session_file", lambda sid: str(f))
    bot = _bot(mock.Mock(runner="claude", session_id="S", cwd="/tmp"))
    reply = await dispatcher._handle_rewind(bot, "ou", "oc:omt", "")
    assert "退回 1 轮" in reply and "三" in reply
    assert session_branch.get_option("S") == {"resume_at": "a2"}
    reply = await dispatcher._handle_rewind(bot, "ou", "oc:omt", "1")  # 再退一轮：从 a2 接着往前
    assert "二" in reply and session_branch.get_option("S") == {"resume_at": "a1"}
    assert "退不了" in await dispatcher._handle_rewind(bot, "ou", "oc:omt", "5")
    assert "用法" in await dispatcher._handle_rewind(bot, "ou", "oc:omt", "abc")


async def test_rewind_needs_claude_session():
    import dispatcher
    bot = _bot(mock.Mock(runner="codex", session_id="S", cwd="/tmp"))
    assert "没有 Claude 会话" in await dispatcher._handle_rewind(bot, "ou", "oc:omt", "")


async def test_fork_creates_topic_and_binds_new_session(tmp_path, monkeypatch):
    import dispatcher
    monkeypatch.setattr(dispatcher, "_find_session_file", lambda sid: "/x.jsonl")
    bot = _bot(mock.Mock(runner="claude", session_id="OLD", cwd="/work"))
    bot.feishu.send_post_to_chat = mock.AsyncMock(return_value="om_root")
    bot.feishu.get_message_thread_id = mock.AsyncMock(return_value="omt_new")
    session_branch.set_option("OLD", resume_at="a1")  # 原话题有没用掉的回退：分叉也从那里分
    reply = await dispatcher._handle_fork(bot, "ou", "oc_g:omt_old", True, "omt_old", "试试方案B")
    assert "已分叉" in reply and "试试方案B" in reply
    (uid, key, new_sid), kw = bot.store.handover_session.call_args
    assert key == "oc_g:omt_new" and kw["cwd"] == "/work"
    assert session_branch.get_option(new_sid) == {"resume_at": "a1", "fork_from": "OLD"}
    assert session_branch.get_option("OLD") == {"resume_at": "a1"}  # 原话题不动
    assert bot.feishu.send_post_to_chat.call_args[0][0] == "oc_g"


async def test_fork_only_in_topic_threads():
    import dispatcher
    bot = _bot(mock.Mock(runner="claude", session_id="OLD", cwd="/work"))
    assert "话题群" in await dispatcher._handle_fork(bot, "ou", "ou", False, "", "")
