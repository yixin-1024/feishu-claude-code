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

    def add_job(self, fn, **kw):
        self.jobs.append((fn, kw))


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

    async def fake_wake(bot_, anchor, prompt):
        fired.set()
        return True

    import dispatcher
    monkeypatch.setattr(dispatcher, "wake_thread_as_user", fake_wake)
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
