"""wake_me_in 落盘 + 重启恢复。

以前一次性唤醒只活在内存 BackgroundScheduler 里，/restart 一次全部静默蒸发。
现在每条 pending wake 写 data/pending_wakes.json（测试里由 conftest 重定向到 tmp），
fire 后清掉；start_scheduler 末尾 restore_pending_wakes() 重装，过期的立即补跑。
"""

import asyncio
import json
import os
import sys
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler


class _Feishu:
    async def reply_text(self, message_id, text):
        return "om_reply"


class _Bot:
    def __init__(self, name="spx"):
        self.feishu = _Feishu()

        class _Profile:
            pass

        self.profile = _Profile()
        self.profile.name = name

        class _Store:
            def find_primary_user(self_inner):
                return "ou_primary"

        self.store = _Store()


class _Sched:
    """记录 add_job 的 fn + kwargs，能手动触发。"""

    def __init__(self):
        self.jobs: list[tuple] = []
        self.removed_jobs: list[str] = []

    def add_job(self, fn, **kw):
        self.jobs.append((fn, kw))

    def remove_job(self, job_id):
        self.removed_jobs.append(job_id)


def _state(monkeypatch, sched, bots, loop):
    monkeypatch.setitem(scheduler._STATE, "scheduler", sched)
    monkeypatch.setitem(scheduler._STATE, "bots", bots)
    monkeypatch.setitem(scheduler._STATE, "bot_loop", loop)
    monkeypatch.setitem(scheduler._STATE, "spawn_fn", lambda *a, **k: None)


def _store():
    return json.load(open(scheduler._wake_store_path(), encoding="utf-8"))


def test_schedule_wake_persists_record_and_fire_removes_it(monkeypatch):
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    sched = _Sched()
    bot = _Bot()
    _state(monkeypatch, sched, {"spx": bot}, loop)

    fired = threading.Event()
    seen: dict = {}

    async def fake_wake(bot_, **kw):
        seen.update(kw)
        fired.set()
        return True

    import dispatcher
    monkeypatch.setattr(dispatcher, "wake_thread_announced", fake_wake)
    try:
        res = scheduler.schedule_wake(
            profile="spx", chat_id="oc_x", thread_id="omt_abcd1234",
            anchor_message_id="om_anchor", user_id="ou_user", minutes=7, note="check CI",
        )
        assert res["ok"] is True
        data = _store()
        assert res["job_id"] in data, "排定后必须落盘"
        rec = data[res["job_id"]]
        assert rec["thread_id"] == "omt_abcd1234" and rec["anchor"] == "om_anchor"
        assert rec["note"] == "check CI" and rec["minutes"] == 7 and rec["profile"] == "spx"
        assert len(sched.jobs) == 1

        fn, _kw = sched.jobs[0]
        fn()  # 模拟 APScheduler 到点触发
        assert fired.wait(timeout=3)
        assert res["job_id"] not in _store(), "触发后要从落盘里清掉"
        # 群里那行公告是 bot 自己的人话摘要；完整 prompt 只进 session
        assert seen["announce"].startswith("⏰ 自动唤醒")
        assert "check CI" in seen["announce"]
        assert "[⏰ 自动唤醒]" in seen["prompt"] and "check CI" in seen["prompt"]
        assert seen["user_id"] == "ou_user" and seen["chat_id_raw"] == "oc_x"
        assert seen["thread_id"] == "omt_abcd1234" and seen["anchor_msg_id"] == "om_anchor"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=3)
        loop.close()


def test_restore_rearms_future_and_replays_overdue(monkeypatch):
    loop = asyncio.new_event_loop()
    sched = _Sched()
    bot = _Bot()
    _state(monkeypatch, sched, {"spx": bot}, loop)
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)
    past = {
        "job_id": "wake-past-1", "profile": "spx", "chat_id": "oc_x", "thread_id": "omt_p",
        "anchor": "om_p", "user_id": "ou_u", "minutes": 30, "note": "deploy check",
        "fire_at": (now - timedelta(minutes=20)).isoformat(),
    }
    future = {
        "job_id": "wake-future-1", "profile": "spx", "chat_id": "oc_x", "thread_id": "omt_f",
        "anchor": "om_f", "user_id": "ou_u", "minutes": 60, "note": "poll CI",
        "fire_at": (now + timedelta(minutes=40)).isoformat(),
    }
    orphan = {  # profile 不存在 → 丢弃
        "job_id": "wake-orphan", "profile": "gone", "chat_id": "oc_x", "thread_id": "omt_o",
        "anchor": "om_o", "user_id": "ou_u", "minutes": 5, "note": "x",
        "fire_at": (now + timedelta(minutes=5)).isoformat(),
    }
    scheduler._save_pending_wakes({r["job_id"]: r for r in (past, future, orphan)})

    restored, late = scheduler.restore_pending_wakes()
    assert (restored, late) == (2, 1)
    ids = {kw["id"] for _fn, kw in sched.jobs}
    assert ids == {"wake-past-1", "wake-future-1"}

    by_id = {kw["id"]: kw for _fn, kw in sched.jobs}
    # 过期的改到几秒后补跑；未到点的保持原 fire 时间
    assert by_id["wake-past-1"]["trigger"].run_date <= now + timedelta(seconds=30)
    assert abs((by_id["wake-future-1"]["trigger"].run_date - (now + timedelta(minutes=40))).total_seconds()) < 5

    data = _store()
    assert "wake-orphan" not in data, "无效记录应被清理"
    assert "wake-future-1" in data and "wake-past-1" in data
    assert "补跑" in scheduler._build_wake_prompt(data["wake-past-1"])
    assert "补跑" not in scheduler._build_wake_prompt(data["wake-future-1"])
    loop.close()


def test_restore_with_empty_store_is_noop(monkeypatch):
    loop = asyncio.new_event_loop()
    sched = _Sched()
    _state(monkeypatch, sched, {"spx": _Bot()}, loop)
    assert scheduler.restore_pending_wakes() == (0, 0)
    assert sched.jobs == []
    loop.close()


def test_cancel_wake_by_thread_and_job_id(monkeypatch):
    loop = asyncio.new_event_loop()
    sched = _Sched()
    bot = _Bot()
    _state(monkeypatch, sched, {"spx": bot}, loop)

    # 排 3 个 wake，其中 2 个在同一 thread
    r1 = scheduler.schedule_wake(
        profile="spx", chat_id="oc_x", thread_id="omt_thread1",
        anchor_message_id="om_1", user_id="ou_u", minutes=10, note="task 1",
    )
    r2 = scheduler.schedule_wake(
        profile="spx", chat_id="oc_x", thread_id="omt_thread1",
        anchor_message_id="om_2", user_id="ou_u", minutes=20, note="task 2",
    )
    r3 = scheduler.schedule_wake(
        profile="spx", chat_id="oc_x", thread_id="omt_thread2",
        anchor_message_id="om_3", user_id="ou_u", minutes=15, note="task 3",
    )
    assert len(_store()) == 3

    # 1) 查询 thread1 的 pending wakes
    pending_t1 = scheduler.get_pending_wakes_for_thread("omt_thread1")
    assert len(pending_t1) == 2
    assert {p["job_id"] for p in pending_t1} == {r1["job_id"], r2["job_id"]}

    # 2) 精确按 job_id 取消 r1
    c1 = scheduler.cancel_wake(job_id=r1["job_id"])
    assert c1["ok"] is True and c1["count"] == 1
    assert r1["job_id"] in sched.removed_jobs
    assert r1["job_id"] not in _store()
    assert len(_store()) == 2

    # 3) 按 thread_id 批量取消 omt_thread1 剩余的任务（r2）
    c2 = scheduler.cancel_wake(thread_id="omt_thread1")
    assert c2["ok"] is True and c2["count"] == 1
    assert c2["cancelled"][0]["job_id"] == r2["job_id"]
    assert r2["job_id"] in sched.removed_jobs
    assert r2["job_id"] not in _store()
    assert len(_store()) == 1

    # 4) 重复取消已不存在的任务：安全返回 0
    c_noop = scheduler.cancel_wake(thread_id="omt_thread1")
    assert c_noop["ok"] is True and c_noop["count"] == 0

    # 5) omt_thread2 的任务仍保留
    assert r3["job_id"] in _store()
    loop.close()


def test_cancel_wake_prevents_firing_if_job_executed(monkeypatch):
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    sched = _Sched()
    bot = _Bot()
    _state(monkeypatch, sched, {"spx": bot}, loop)

    fired = threading.Event()

    async def fake_wake(bot_, **kw):
        fired.set()
        return True

    import dispatcher
    monkeypatch.setattr(dispatcher, "wake_thread_announced", fake_wake)
    try:
        res = scheduler.schedule_wake(
            profile="spx", chat_id="oc_x", thread_id="omt_cancel_test",
            anchor_message_id="om_anchor", user_id="ou_user", minutes=5, note="will cancel",
        )
        assert len(sched.jobs) == 1
        fn, _kw = sched.jobs[0]

        # 在执行前先取消
        c = scheduler.cancel_wake(thread_id="omt_cancel_test")
        assert c["count"] == 1

        # 模拟 APScheduler 触发此前已注册的闭包
        fn()

        # 等待 1 秒，确认 fake_wake 根本没有被触发
        assert not fired.wait(timeout=1.0), "已被取消的 wake 绝不应执行唤醒"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=3)
        loop.close()


def test_dispatcher_turn_header_wake_reminder(monkeypatch):
    import dispatcher

    # 1) 没有 pending wake 时，只包含消息 ID 和提问者
    header_none = dispatcher._turn_header("om_123", "ou_456", thread_id="omt_empty")
    assert "om_123" in header_none
    assert "ou_456" in header_none
    assert "待办唤醒提醒" not in header_none

    # 2) 构造 1 条 pending wake
    loop = asyncio.new_event_loop()
    sched = _Sched()
    bot = _Bot()
    _state(monkeypatch, sched, {"spx": bot}, loop)
    scheduler.schedule_wake(
        profile="spx", chat_id="oc_x", thread_id="omt_with_wake",
        anchor_message_id="om_a", user_id="ou_u", minutes=15, note="等待CI跑完",
    )

    header_with = dispatcher._turn_header("om_123", "ou_456", thread_id="omt_with_wake")
    assert "【⏰ 待办唤醒提醒】" in header_with
    assert "等待CI跑完" in header_with
    assert "cancel_wake" in header_with
    loop.close()

