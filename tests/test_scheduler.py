"""scheduler 的 quota 预检 + 派单跳过路径单测。

行为目标：用量耗尽时 scheduler 不调 spawn_fn，只发一条"跳过本轮"顶楼。
否则会派出去一个一定撞 rate_limit 的 session，浪费一次 anchor + 用户疑惑。
"""

import asyncio
import os
import sys
import threading

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scheduler  # noqa: E402


# ────────────────── _quota_skip_reason 纯函数 ──────────────────

def test_quota_skip_reason_allows_when_under_limit():
    q = {"ok": True, "u5h": 0.31, "u7d": 0.13,
         "r5h": 9999999999, "r7d": 9999999999, "s5h": "allowed", "s7d": "allowed"}
    assert scheduler._quota_skip_reason(q) is None


def test_quota_skip_reason_ignores_failed_fetch():
    # 拿不到 quota 时不阻塞——优先让 spawn 跑，由 PTY runner 兜底
    assert scheduler._quota_skip_reason({"ok": False, "error": "x"}) is None


def test_quota_skip_reason_blocks_when_status_not_allowed():
    q = {"ok": True, "u5h": 0.5, "u7d": 0.1,
         "r5h": 9999999999, "r7d": 9999999999, "s5h": "exceeded", "s7d": "allowed"}
    out = scheduler._quota_skip_reason(q)
    assert out is not None
    reason, lines = out
    assert "用量" in reason
    assert any("5h" in l for l in lines)


def test_quota_skip_reason_blocks_when_near_full():
    q = {"ok": True, "u5h": 0.99, "u7d": 0.1,
         "r5h": 9999999999, "r7d": 9999999999, "s5h": "allowed", "s7d": "allowed"}
    out = scheduler._quota_skip_reason(q)
    assert out is not None
    _, lines = out
    assert any("5h" in l for l in lines)


def test_quota_skip_reason_unknown_status_does_not_block():
    # status 字段缺失（"unknown" 来自 fetch_quota_headers 的兜底）不应误判
    q = {"ok": True, "u5h": 0.5, "u7d": 0.1,
         "r5h": None, "r7d": None, "s5h": "unknown", "s7d": "unknown"}
    assert scheduler._quota_skip_reason(q) is None


# ────────────────── _fire 集成：耗尽时不调 spawn ──────────────────

class _FakeFeishu:
    def __init__(self):
        self.posts: list[dict] = []

    async def send_post_to_chat(self, *, chat_id, title, body_text, mention_open_id=""):
        self.posts.append({"chat_id": chat_id, "title": title,
                           "body": body_text, "mention": mention_open_id})
        return "om_fake_anchor"


class _FakeBot:
    def __init__(self):
        self.feishu = _FakeFeishu()


def _make_task():
    return scheduler.ScheduledTask(
        name="t1", profile="spx", cron="0 0 * * *", timezone="Asia/Shanghai",
        chat_id="oc_test", user_id="ou_user", topic_title="日报",
        topic_body="🧵 接管", prompt="hello",
    )


def test_fire_skips_spawn_when_quota_exhausted(monkeypatch):
    """quota 耗尽时：发跳过通报、不调 spawn_fn。"""
    bot = _FakeBot()
    task = _make_task()
    spawn_called: list[dict] = []

    async def spawn_fn(bot_, **kw):
        spawn_called.append(kw)

    monkeypatch.setattr(
        scheduler, "fetch_quota_headers",
        lambda: {"ok": True, "u5h": 0.99, "u7d": 0.1,
                 "r5h": 9999999999, "r7d": 9999999999,
                 "s5h": "exceeded", "s7d": "allowed"},
        raising=False,
    )
    # _fire 里通过 `from commands import fetch_quota_headers` 拿到——所以 patch commands
    import commands
    monkeypatch.setattr(commands, "fetch_quota_headers",
                        lambda: {"ok": True, "u5h": 0.99, "u7d": 0.1,
                                 "r5h": 9999999999, "r7d": 9999999999,
                                 "s5h": "exceeded", "s7d": "allowed"})

    fire = scheduler._make_async_fire(task, bot, spawn_fn)
    asyncio.run(fire())

    assert spawn_called == [], "quota 耗尽时不应调 spawn_fn"
    assert len(bot.feishu.posts) == 1, "应发一条跳过通报"
    post = bot.feishu.posts[0]
    assert "跳过本轮" in post["title"]
    assert "用量" in post["body"]


def test_fire_proceeds_when_quota_ok(monkeypatch):
    """quota 正常时：正常发顶楼 + 调 spawn_fn。"""
    bot = _FakeBot()
    task = _make_task()
    spawn_called: list[dict] = []

    async def spawn_fn(bot_, **kw):
        spawn_called.append(kw)

    import commands
    monkeypatch.setattr(commands, "fetch_quota_headers",
                        lambda: {"ok": True, "u5h": 0.20, "u7d": 0.10,
                                 "r5h": 9999999999, "r7d": 9999999999,
                                 "s5h": "allowed", "s7d": "allowed"})

    fire = scheduler._make_async_fire(task, bot, spawn_fn)
    asyncio.run(fire())

    assert len(spawn_called) == 1, "quota 正常时应调一次 spawn_fn"
    assert spawn_called[0]["prompt"] == "hello"
    assert len(bot.feishu.posts) == 1
    assert bot.feishu.posts[0]["title"] == "日报"


def test_fire_proceeds_when_quota_fetch_fails(monkeypatch):
    """fetch_quota_headers 抛异常时：不阻塞，正常派单（fail-open）。"""
    bot = _FakeBot()
    task = _make_task()
    spawn_called: list[dict] = []

    async def spawn_fn(bot_, **kw):
        spawn_called.append(kw)

    def _boom():
        raise RuntimeError("network down")

    import commands
    monkeypatch.setattr(commands, "fetch_quota_headers", _boom)

    fire = scheduler._make_async_fire(task, bot, spawn_fn)
    asyncio.run(fire())

    assert len(spawn_called) == 1, "fetch 失败时仍应派单"


# ────────────────── schedule_wake：排定后往话题贴可见公告 ──────────────────

class _RecordingFeishu:
    def __init__(self):
        self.replies: list[tuple[str, str]] = []
        self.done = threading.Event()

    async def reply_text(self, message_id, text):
        self.replies.append((message_id, text))
        self.done.set()
        return "om_reply"


class _WakeBot:
    def __init__(self):
        self.feishu = _RecordingFeishu()

        class _Profile:
            name = "spx"

        class _Store:
            def find_primary_user(self_inner):
                return "ou_primary"

        self.profile = _Profile()
        self.store = _Store()


class _RecordingScheduler:
    def __init__(self):
        self.jobs: list[dict] = []

    def add_job(self, fn, **kw):
        self.jobs.append(kw)


def test_schedule_wake_posts_visible_announcement(monkeypatch):
    """排定唤醒成功后，应往本话题（reply anchor）贴一条含唤醒时间的可见公告，
    让用户知道已排好、不会再手动重复唤醒（用户反馈的痛点）。"""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    bot = _WakeBot()
    sched = _RecordingScheduler()

    monkeypatch.setitem(scheduler._STATE, "scheduler", sched)
    monkeypatch.setitem(scheduler._STATE, "bots", {"spx": bot})
    monkeypatch.setitem(scheduler._STATE, "bot_loop", loop)
    monkeypatch.setitem(scheduler._STATE, "spawn_fn", lambda *a, **k: None)

    try:
        res = scheduler.schedule_wake(
            profile="spx", chat_id="oc_x", thread_id="omt_abcd1234",
            anchor_message_id="om_anchor", user_id="ou_user",
            minutes=5, note="check CI status",
        )
        assert res["ok"] is True
        assert len(sched.jobs) == 1, "应挂一个一次性 wake job"
        assert bot.feishu.done.wait(timeout=3), "应异步贴出唤醒公告"
        msg_id, text = bot.feishu.replies[0]
        assert msg_id == "om_anchor", "公告应 reply 到 anchor（落在本话题）"
        assert "5 分钟" in text and "自动唤醒" in text
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=3)
        loop.close()


def test_schedule_wake_reply_failure_does_not_break_scheduling(monkeypatch):
    """公告发送失败（reply_text 抛错）时，唤醒仍应成功排定（best-effort，不回滚）。"""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    boom_called = threading.Event()

    class _BoomFeishu:
        async def reply_text(self, message_id, text):
            boom_called.set()
            raise RuntimeError("lark down")

    bot = _WakeBot()
    bot.feishu = _BoomFeishu()
    sched = _RecordingScheduler()

    monkeypatch.setitem(scheduler._STATE, "scheduler", sched)
    monkeypatch.setitem(scheduler._STATE, "bots", {"spx": bot})
    monkeypatch.setitem(scheduler._STATE, "bot_loop", loop)
    monkeypatch.setitem(scheduler._STATE, "spawn_fn", lambda *a, **k: None)

    try:
        res = scheduler.schedule_wake(
            profile="spx", chat_id="oc_x", thread_id="omt_abcd1234",
            anchor_message_id="om_anchor", user_id="ou_user",
            minutes=10, note="deploy check",
        )
        assert res["ok"] is True, "公告失败不应让唤醒排定失败"
        assert len(sched.jobs) == 1
        assert boom_called.wait(timeout=3)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=3)
        loop.close()


# ────────────────── model / effort：任务级模型与推理强度 ──────────────────

def _raw_task(**extra):
    raw = {"name": "t1", "profile": "spx", "cron": "0 0 * * *",
           "chat_id": "oc_test", "user_id": "ou_user", "prompt": "hello"}
    raw.update(extra)
    return raw


def test_from_dict_defaults_model_and_effort_to_empty():
    """没写 model/effort 的老任务照常加载，语义 = 跟随 profile / CLI 默认。"""
    task = scheduler.ScheduledTask.from_dict(_raw_task(), base_dir=".")
    assert task.model == ""
    assert task.effort == ""


def test_from_dict_resolves_model_alias_and_effort():
    task = scheduler.ScheduledTask.from_dict(
        _raw_task(model="opus", effort="HIGH"), base_dir="."
    )
    from commands import MODEL_ALIASES
    assert task.model == MODEL_ALIASES["opus"]
    assert task.effort == "high"


def test_from_dict_keeps_unknown_model_string_verbatim():
    """非别名的完整模型串原样透传，别把新模型挡在外面。"""
    task = scheduler.ScheduledTask.from_dict(
        _raw_task(model="claude-something-new[1m]"), base_dir="."
    )
    assert task.model == "claude-something-new[1m]"


def test_from_dict_effort_default_means_no_override():
    task = scheduler.ScheduledTask.from_dict(_raw_task(effort="default"), base_dir=".")
    assert task.effort == ""


def test_from_dict_rejects_bogus_effort():
    with pytest.raises(ValueError, match="effort"):
        scheduler.ScheduledTask.from_dict(_raw_task(effort="turbo"), base_dir=".")


def test_fire_passes_model_and_effort_to_spawn(monkeypatch):
    """任务里声明的 model/effort 必须透传给 spawn —— 新话题不继承任何设置。"""
    bot = _FakeBot()
    task = scheduler.ScheduledTask.from_dict(
        _raw_task(model="opus", effort="xhigh"), base_dir="."
    )
    spawn_called: list[dict] = []

    async def spawn_fn(bot_, **kw):
        spawn_called.append(kw)

    import commands
    monkeypatch.setattr(commands, "fetch_quota_headers",
                        lambda: {"ok": True, "u5h": 0.2, "u7d": 0.1,
                                 "r5h": 9999999999, "r7d": 9999999999,
                                 "s5h": "allowed", "s7d": "allowed"})

    asyncio.run(scheduler._make_async_fire(task, bot, spawn_fn)())

    assert len(spawn_called) == 1
    assert spawn_called[0]["model"] == commands.MODEL_ALIASES["opus"]
    assert spawn_called[0]["effort"] == "xhigh"


# ────────────────── user_id 归一：顶楼 @ 必须能落到人 ──────────────────

def test_from_dict_takes_first_open_id_when_user_id_is_a_list():
    """yaml 里 user_id 常直接引 ${*_ALLOWED_OPEN_IDS}（逗号串）。
    at tag 只认单个 open_id，整串塞进去 Lark 不报错但解析不出 mention
    → 渲染成光秃秃的「@」、归属人收不到通知。必须取第一个。"""
    task = scheduler.ScheduledTask.from_dict(
        _raw_task(user_id="ou_first, ou_second ,ou_third"), base_dir="."
    )
    assert task.user_id == "ou_first"


def test_fire_mentions_single_open_id(monkeypatch):
    """顶楼 post 的 mention_open_id 必须是单个 id，不能是逗号串。"""
    bot = _FakeBot()
    task = scheduler.ScheduledTask.from_dict(
        _raw_task(user_id="ou_first,ou_second"), base_dir="."
    )

    async def spawn_fn(bot_, **kw):
        pass

    import commands
    monkeypatch.setattr(commands, "fetch_quota_headers",
                        lambda: {"ok": True, "u5h": 0.2, "u7d": 0.1,
                                 "r5h": 9999999999, "r7d": 9999999999,
                                 "s5h": "allowed", "s7d": "allowed"})

    asyncio.run(scheduler._make_async_fire(task, bot, spawn_fn)())

    assert len(bot.feishu.posts) == 1
    assert bot.feishu.posts[0]["mention"] == "ou_first"
