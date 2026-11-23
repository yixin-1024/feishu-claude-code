"""scheduler 派单路径单测。

用量刹车线已于 2026-11-23 去掉：Claude 额度用到多少，定时任务都照派，
不再发「⏸️ 跳过本轮」，也不再派单前去拉 quota。
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


# ────────────────── _fire 集成：耗尽时不调 spawn ──────────────────

class _FakeFeishu:
    def __init__(self):
        self.posts: list[dict] = []

    async def send_post_to_chat(self, *, chat_id, title, body_text, mention_open_id=""):
        self.posts.append({"chat_id": chat_id, "title": title,
                           "body": body_text, "mention": mention_open_id})
        return "om_fake_anchor"


class _FakeProfile:
    def __init__(self, runner: str):
        self.name = "spx"
        self.runner = runner


class _FakeBot:
    def __init__(self, runner: str | None = None):
        self.feishu = _FakeFeishu()
        # runner=None 模拟拿不到 profile 的老对象（应按 claude 兜底）
        if runner is not None:
            self.profile = _FakeProfile(runner)


def _make_task():
    return scheduler.ScheduledTask(
        name="t1", profile="spx", cron="0 0 * * *", timezone="Asia/Shanghai",
        chat_id="oc_test", user_id="ou_user", topic_title="日报",
        topic_body="🧵 接管", prompt="hello",
    )


@pytest.mark.parametrize("runner", [None, "claude", "agy"])
def test_fire_never_skips_for_quota(monkeypatch, runner):
    """额度用到 100% 也照常发顶楼 + 派单，不发「跳过本轮」，也不去拉 quota。"""
    monkeypatch.delenv("SCHED_MENTION_OWNER", raising=False)
    bot = _FakeBot(runner=runner)
    task = _make_task()
    spawn_called: list[dict] = []
    fetched: list[int] = []

    async def spawn_fn(bot_, **kw):
        spawn_called.append(kw)

    import commands

    def _fetch():
        fetched.append(1)
        return {"ok": True, "u5h": 1.0, "u7d": 1.0,
                "r5h": 9999999999, "r7d": 9999999999,
                "s5h": "exceeded", "s7d": "exceeded"}

    monkeypatch.setattr(commands, "fetch_quota_headers", _fetch)

    asyncio.run(scheduler._make_async_fire(task, bot, spawn_fn)())

    assert len(spawn_called) == 1, "用量满了也必须派单"
    assert spawn_called[0]["prompt"] == "hello"
    assert fetched == [], "派单前不该再去拉 quota"
    assert len(bot.feishu.posts) == 1
    assert bot.feishu.posts[0]["title"] == "日报", "不该发跳过通报"
    assert "跳过本轮" not in bot.feishu.posts[0]["title"]


def test_brake_line_helpers_are_gone():
    """刹车线的配置项和判定函数都已删除，别被人悄悄加回来。"""
    assert not hasattr(scheduler, "_quota_skip_reason")
    assert not hasattr(scheduler, "_skip_util_threshold")


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
    # 显式打开 @（本机 .env 可能把 SCHED_MENTION_OWNER 关了，这条测的是归一不是默认）
    monkeypatch.setenv("SCHED_MENTION_OWNER", "1")
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


# ────────────────── mention 开关：顶楼要不要 @ 归属人 ──────────────────

def test_from_dict_mentions_owner_by_default(monkeypatch):
    monkeypatch.delenv("SCHED_MENTION_OWNER", raising=False)
    assert scheduler.ScheduledTask.from_dict(_raw_task(), base_dir=".").mention is True


def test_from_dict_mention_false_disables_at(monkeypatch):
    monkeypatch.delenv("SCHED_MENTION_OWNER", raising=False)
    task = scheduler.ScheduledTask.from_dict(_raw_task(mention=False), base_dir=".")
    assert task.mention is False


def test_env_can_turn_mention_off_globally(monkeypatch):
    """SCHED_MENTION_OWNER=0 → 所有任务（含 agent 自己 schedule_cron 建的、
    yaml 里没写 mention 的）都不再 @ 归属人。"""
    monkeypatch.setenv("SCHED_MENTION_OWNER", "0")
    assert scheduler.ScheduledTask.from_dict(_raw_task(), base_dir=".").mention is False
    # 单条任务可以把它顶回来
    assert scheduler.ScheduledTask.from_dict(
        _raw_task(mention=True), base_dir="."
    ).mention is True


def test_from_dict_mention_accepts_yaml_strings(monkeypatch):
    monkeypatch.delenv("SCHED_MENTION_OWNER", raising=False)
    for raw, want in (("false", False), ("no", False), ("off", False),
                      ("true", True), ("1", True), ("绝对不行", True)):
        # 看不懂的值不该把任务弄坏 —— 回落到默认（此处 = 开）
        task = scheduler.ScheduledTask.from_dict(_raw_task(mention=raw), base_dir=".")
        assert task.mention is want, raw


def test_fire_omits_at_tag_when_mention_disabled(monkeypatch):
    """mention=false：顶楼照发、spawn 照走（user_id 仍是归属人），只是不带 at tag。"""
    monkeypatch.delenv("SCHED_MENTION_OWNER", raising=False)
    bot = _FakeBot()
    task = scheduler.ScheduledTask.from_dict(_raw_task(mention=False), base_dir=".")
    spawn_called: list[dict] = []

    async def spawn_fn(bot_, **kw):
        spawn_called.append(kw)

    import commands
    monkeypatch.setattr(commands, "fetch_quota_headers",
                        lambda: {"ok": True, "u5h": 0.2, "u7d": 0.1,
                                 "r5h": 9999999999, "r7d": 9999999999,
                                 "s5h": "allowed", "s7d": "allowed"})

    asyncio.run(scheduler._make_async_fire(task, bot, spawn_fn)())

    assert len(bot.feishu.posts) == 1
    assert bot.feishu.posts[0]["mention"] == ""
    assert bot.feishu.posts[0]["title"] == "t1", "顶楼本身照发，只是没了 at tag"
    assert len(spawn_called) == 1
    assert spawn_called[0]["user_id"] == "ou_user", "归属人不能因为不 @ 就丢"

