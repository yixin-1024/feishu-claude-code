"""重启后自动续跑：落盘 → 中断打标 → 启动重投。

用户痛点（2026-09-04）：`/restart` 打断正在跑的任务后，卡片只说"~5s 后再发一遍"，
人得回来手动重发指令 / 说"继续"。这里覆盖整条链路：
run 开跑落盘 → /restart 打断点标记 → 下次启动按记录把任务投回原话题续跑。
"""

import asyncio
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher
import resume_store
import scheduler


@pytest.fixture(autouse=True)
def _reset_dispatcher_state():
    old_bots = dispatcher._bots
    dispatcher._restart_in_progress = False
    dispatcher._restart_committed = False
    yield
    dispatcher._restart_in_progress = False
    dispatcher._restart_committed = False
    dispatcher._bots = old_bots


def _bot(profile="test"):
    bot = SimpleNamespace(
        profile=SimpleNamespace(name=profile, lark_cli_profile=profile),
        feishu=AsyncMock(),
        _locks={},
    )
    bot._ensure_chat_lock = lambda cid: bot._locks.setdefault(cid, asyncio.Lock())
    return bot


def _run(**kw):
    from run_control import ActiveRun

    run = ActiveRun(
        user_id=kw.pop("user_id", "ou_user"),
        chat_id=kw.pop("chat_id", "oc_group:omt_thread"),
        card_msg_id=kw.pop("card_msg_id", "om_card"),
    )
    run.prompt = kw.pop("prompt", "把报表跑完")
    run.anchor_msg_id = kw.pop("anchor_msg_id", "om_anchor")
    run.is_group = kw.pop("is_group", True)
    run.thread_id = kw.pop("thread_id", "omt_thread")
    for k, v in kw.items():
        setattr(run, k, v)
    return run


# ── 落盘生命周期 ────────────────────────────────────────────

def test_record_then_drop_leaves_no_trace():
    bot, run = _bot(), _run()
    key = dispatcher._record_resume(bot, run)

    assert key and run.resume_key == key
    assert [r["key"] for r in resume_store.load_all()] == [key]

    dispatcher._drop_resume(bot, run)
    assert resume_store.load_all() == []


def test_interrupted_record_survives_run_cleanup():
    """/restart 打过标的记录不能被 run 的 finally 删掉——那是续跑的唯一凭据。"""
    bot, run = _bot(), _run()
    dispatcher._record_resume(bot, run)
    run.last_body = "🔧 Bash(ls)\n\n跑到一半"
    dispatcher._record_resume(bot, run, reason="restart", interrupted=True)

    dispatcher._drop_resume(bot, run)  # run 的 finally 照常调用

    recs = resume_store.load_all()
    assert len(recs) == 1
    assert recs[0]["reason"] == "restart"
    assert recs[0]["progress"] == "🔧 Bash(ls)\n\n跑到一半"
    assert recs[0]["interrupted_at"] > 0


def test_record_is_disabled_by_env(monkeypatch):
    monkeypatch.setenv("CC_LARK_RESUME_AFTER_RESTART", "0")
    bot, run = _bot(), _run()

    assert dispatcher._record_resume(bot, run) == ""
    assert resume_store.load_all() == []


def test_mark_interrupted_ignores_already_finished_run():
    """run 刚好跑完（记录已删）时打标必须是 no-op，绝不能凭空造一条续跑记录。"""
    resume_store.mark_interrupted("test::ou_user::oc_chat", reason="restart", progress="x")
    assert resume_store.load_all() == []


# ── /restart 广播 ───────────────────────────────────────────

async def test_restart_marks_active_runs_for_resume():
    from run_control import ActiveRunRegistry

    bot = _bot()
    registry = ActiveRunRegistry()
    bot.active_runs = registry
    run = registry.start_run("ou_user", "oc_chat:omt_t", "om_card")
    run.prompt = "把这批用户开完卡"
    run.anchor_msg_id = "om_anchor"
    run.is_group = True
    run.thread_id = "omt_t"
    run.last_body = "已经开到第 3 个"
    run.resume_key = "test::ou_user::oc_chat:omt_t"  # 已进 _execute_run
    dispatcher._bots = {"test": bot}

    with patch.object(dispatcher, "stop_run", AsyncMock(return_value=True)):
        affected = await dispatcher._handle_restart_command(bot)

    assert affected == 1
    recs = resume_store.load_all()
    assert len(recs) == 1
    assert recs[0]["reason"] == "restart"
    assert recs[0]["prompt"] == "把这批用户开完卡"
    assert recs[0]["anchor"] == "om_anchor"
    assert recs[0]["progress"] == "已经开到第 3 个"
    assert run.keep_resume is True


async def test_restart_marks_queued_run_as_queued_not_midway():
    """还在排队（没进 _execute_run，没有 resume_key）的任务续跑时该从头跑。"""
    from run_control import ActiveRunRegistry

    bot = _bot()
    registry = ActiveRunRegistry()
    bot.active_runs = registry
    run = registry.start_run("ou_user", "oc_chat:omt_t", "om_card")
    run.prompt = "生成周报"
    run.is_group = True
    dispatcher._bots = {"test": bot}

    with patch.object(dispatcher, "stop_run", AsyncMock(return_value=True)):
        await dispatcher._handle_restart_command(bot)

    assert resume_store.load_all()[0]["reason"] == "queued"


async def test_restart_card_tells_user_it_will_auto_resume():
    from run_control import ActiveRunRegistry

    bot = _bot()
    registry = ActiveRunRegistry()
    bot.active_runs = registry
    run = registry.start_run("ou_user", "oc_chat", "om_card")
    run.prompt = "干活"
    run.last_body = "跑了一半"
    dispatcher._bots = {"test": bot}

    async def stop_and_announce(_registry, _user, _chat, *, on_stopped, grace_seconds):
        await on_stopped(run)
        return True

    with patch.object(dispatcher, "stop_run", side_effect=stop_and_announce):
        await dispatcher._handle_restart_command(bot)

    _, content = bot.feishu.update_card.await_args.args
    assert content.startswith("跑了一半")
    assert "自动接着跑" in content
    assert "再发一遍" in content  # "不用再发一遍"


# ── 续跑 prompt ─────────────────────────────────────────────

def test_resume_prompt_midway_demands_verification_before_continuing():
    rec = {"prompt": "给这 20 个用户开 SGB", "reason": "restart",
           "progress": "开到第 3 个", "attempt": 0}
    text = resume_store.build_resume_prompt(rec)

    assert "给这 20 个用户开 SGB" in text
    assert "开到第 3 个" in text
    assert "先核实" in text
    assert "不要重复执行" in text or "绝不要重复执行" in text


def test_resume_prompt_for_queued_run_says_run_from_scratch():
    rec = {"prompt": "生成周报", "reason": "queued", "attempt": 0}
    text = resume_store.build_resume_prompt(rec)

    assert "从头执行" in text
    assert "先核实" not in text


def test_resume_prompt_flags_repeated_interruptions():
    rec = {"prompt": "干活", "reason": "restart", "attempt": 2}
    assert "第 3 次" in resume_store.build_resume_prompt(rec)


def test_resume_prompt_defaults_to_crash_wording():
    """记录没被打过标 = 上个进程是硬死的（没走中断编排）。"""
    text = resume_store.build_resume_prompt({"prompt": "干活", "reason": ""})
    assert "意外退出" in text


# ── 进程内直投 ──────────────────────────────────────────────

async def test_resume_run_internal_replays_into_same_thread(monkeypatch):
    bot = _bot()
    seen = {}

    async def fake_process(b, user_id, chat_id, is_group, thread_id, msg):
        seen.update(user_id=user_id, chat_id=chat_id, is_group=is_group,
                    thread_id=thread_id, msg=msg)

    monkeypatch.setattr(dispatcher, "_process_message", fake_process)

    ok = await dispatcher.resume_run_internal(
        bot, user_id="ou_user", chat_id="oc_group:omt_t", is_group=True,
        thread_id="omt_t", anchor_msg_id="om_anchor", prompt="接着跑",
    )

    assert ok is True
    assert seen["chat_id"] == "oc_group:omt_t"
    assert seen["is_group"] is True
    assert seen["msg"].message_id == "om_anchor"
    assert "接着跑" in seen["msg"].content


async def test_resume_run_internal_supports_private_chat(monkeypatch):
    """私聊 run 也会被重启打断——wake_thread_internal 写死 is_group=True 覆盖不到。"""
    bot = _bot()
    seen = {}

    async def fake_process(b, user_id, chat_id, is_group, thread_id, msg):
        seen.update(chat_id=chat_id, is_group=is_group, thread_id=thread_id,
                    chat_type=msg.chat_type)

    monkeypatch.setattr(dispatcher, "_process_message", fake_process)

    ok = await dispatcher.resume_run_internal(
        bot, user_id="ou_user", chat_id="ou_user", is_group=False,
        thread_id="", anchor_msg_id="om_anchor", prompt="接着跑",
    )

    assert ok is True
    assert seen == {"chat_id": "ou_user", "is_group": False,
                    "thread_id": "", "chat_type": "p2p"}


async def test_resume_run_internal_reports_failure(monkeypatch):
    bot = _bot()

    async def boom(*_a, **_kw):
        raise RuntimeError("session 挂了")

    monkeypatch.setattr(dispatcher, "_process_message", boom)

    assert await dispatcher.resume_run_internal(
        bot, user_id="ou_user", chat_id="oc:omt", is_group=True,
        thread_id="omt", anchor_msg_id="om", prompt="x",
    ) is False


# ── 启动重投 ────────────────────────────────────────────────

class _FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, fn, **kw):
        self.jobs.append((fn, kw))


@pytest.fixture
def _sched_state(monkeypatch):
    bot = _bot()
    fake = _FakeScheduler()
    monkeypatch.setitem(scheduler._STATE, "scheduler", fake)
    monkeypatch.setitem(scheduler._STATE, "bots", {"test": bot})
    monkeypatch.setitem(scheduler._STATE, "bot_loop", asyncio.new_event_loop())
    monkeypatch.setitem(scheduler._STATE, "spawn_fn", AsyncMock())
    return fake, bot


def _put(**over):
    """造一条"上个进程留下的"续跑记录；over 里的字段直接覆盖到落盘记录上。"""
    profile = over.pop("profile", "test")
    key = resume_store.record(
        profile=profile, user_id=over.pop("user_id", "ou_user"),
        chat_id=over.pop("chat_id", "oc_group:omt_t"),
        is_group=True, thread_id="omt_t", anchor="om_anchor",
        card_msg_id="om_card", prompt="接着跑",
    )
    resume_store.mark_interrupted(key, reason="restart", progress="一半")
    if over:
        def _mutate(data):
            data[key].update(over)
            return True
        resume_store._write(_mutate)
    return key


def test_restore_arms_one_job_per_leftover_record(_sched_state):
    fake, _bot_ = _sched_state
    _put()

    armed, skipped = scheduler.restore_pending_resumes()

    assert (armed, skipped) == (1, 0)
    assert len(fake.jobs) == 1
    assert fake.jobs[0][1]["id"].startswith("resume-")


def test_restore_is_noop_when_disabled(monkeypatch, _sched_state):
    fake, _bot_ = _sched_state
    monkeypatch.setenv("CC_LARK_RESUME_AFTER_RESTART", "0")
    _sched_state  # 记录本身也不会被写（record 已被开关拦掉）

    assert scheduler.restore_pending_resumes() == (0, 0)
    assert fake.jobs == []


def test_restore_skips_record_that_hit_attempt_ceiling(monkeypatch, _sched_state):
    fake, _bot_ = _sched_state
    monkeypatch.setenv("CC_LARK_RESUME_MAX_ATTEMPTS", "2")
    _put(attempt=2)

    with patch.object(scheduler, "_submit_resume_notice") as notice:
        armed, skipped = scheduler.restore_pending_resumes()

    assert (armed, skipped) == (0, 1)
    assert fake.jobs == []
    notice.assert_called_once()
    assert resume_store.load_all() == []  # 丢弃，不再无限重投


def test_restore_skips_stale_record(monkeypatch, _sched_state):
    fake, _bot_ = _sched_state
    monkeypatch.setenv("CC_LARK_RESUME_MAX_AGE_MIN", "60")
    _put(interrupted_at=time.time() - 3 * 3600)

    with patch.object(scheduler, "_submit_resume_notice"):
        armed, skipped = scheduler.restore_pending_resumes()

    assert (armed, skipped) == (0, 1)
    assert fake.jobs == []


def test_restore_drops_record_of_unloaded_profile(_sched_state):
    fake, _bot_ = _sched_state
    _put(profile="gone")

    armed, skipped = scheduler.restore_pending_resumes()

    assert (armed, skipped) == (0, 1)
    assert resume_store.load_all() == []


def test_fired_job_injects_resume_prompt_and_bumps_attempt(_sched_state, monkeypatch):
    fake, bot = _sched_state
    key = _put()
    scheduler.restore_pending_resumes()

    loop = asyncio.new_event_loop()
    monkeypatch.setitem(scheduler._STATE, "bot_loop", loop)
    seen = {}

    async def fake_resume(b, *, user_id, chat_id, is_group, thread_id, anchor_msg_id, prompt):
        seen.update(user_id=user_id, chat_id=chat_id, is_group=is_group,
                    thread_id=thread_id, anchor=anchor_msg_id, prompt=prompt)
        return True

    monkeypatch.setattr(dispatcher, "resume_run_internal", fake_resume)

    fire, kw = fake.jobs[0]
    # scheduler 线程调同步 wrapper → 投回 bot_loop；测试里直接把协程跑完。
    with patch.object(asyncio, "run_coroutine_threadsafe",
                      side_effect=lambda coro, _loop: loop.run_until_complete(coro)):
        fire()

    assert seen["chat_id"] == "oc_group:omt_t"
    assert seen["anchor"] == "om_anchor"
    assert "接着跑" in seen["prompt"] and "先核实" in seen["prompt"]
    assert resume_store._ATTEMPTS[key] == 1
    loop.close()


def test_fired_job_end_to_end_injects_synthetic_message(_sched_state, monkeypatch):
    """restore → fire → 进程内直投：落到 _process_message 的就是续跑指令本身。"""
    fake, bot = _sched_state
    _put()
    scheduler.restore_pending_resumes()

    loop = asyncio.new_event_loop()
    monkeypatch.setitem(scheduler._STATE, "bot_loop", loop)
    seen = {}

    async def fake_process(b, user_id, chat_id, is_group, thread_id, msg):
        seen.update(chat_id=chat_id, is_group=is_group, content=msg.content,
                    message_id=msg.message_id)

    monkeypatch.setattr(dispatcher, "_process_message", fake_process)

    fire, _kw = fake.jobs[0]
    with patch.object(asyncio, "run_coroutine_threadsafe",
                      side_effect=lambda coro, _loop: loop.run_until_complete(coro)):
        fire()

    assert seen["chat_id"] == "oc_group:omt_t"
    assert seen["message_id"] == "om_anchor"
    assert "重启续跑" in seen["content"] and "接着跑" in seen["content"]
    loop.close()


def test_resumed_run_inherits_attempt_counter():
    """续跑起来的 run 落新记录时要带上已累计的次数，否则计数永远归零、拦不住死循环。"""
    bot, run = _bot(), _run(chat_id="oc_group:omt_t")
    key = dispatcher._record_resume(bot, run)
    resume_store.bump_attempt(key)

    dispatcher._record_resume(bot, run)  # 新进程里同一条 chat 重新开跑

    assert resume_store.load_all()[0]["attempt"] == 1


def test_second_interruption_stores_original_prompt_not_nested_resume_text():
    """续跑起来的 run 再被打断时，落盘要存最初那句指令 —— 否则 prompt 层层套娃。"""
    bot, run = _bot(), _run(chat_id="oc_group:omt_t", prompt="把报表跑完")
    key = dispatcher._record_resume(bot, run)
    fired = resume_store.build_resume_prompt(resume_store.load_all()[0])
    resume_store.bump_attempt(key, "把报表跑完")  # 投递续跑（scheduler 做的事）

    run.prompt = fired  # 续跑起来的新 run，输入是那段续跑指令
    dispatcher._record_resume(bot, run)

    stored = resume_store.load_all()[0]
    assert stored["prompt"] == "把报表跑完"
    assert "重启续跑" not in stored["prompt"]
    # 第二次续跑的指令里只有一层模板，原指令不会被埋在嵌套里
    assert resume_store.build_resume_prompt(stored).count("重启续跑") == 1
