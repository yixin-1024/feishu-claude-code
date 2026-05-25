"""scheduler 的 quota 预检 + 派单跳过路径单测。

行为目标：用量耗尽时 scheduler 不调 spawn_fn，只发一条"跳过本轮"顶楼。
否则会派出去一个一定撞 rate_limit 的 session，浪费一次 anchor + 用户疑惑。
"""

import asyncio
import os
import sys

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
