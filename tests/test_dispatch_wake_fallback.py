"""dispatch_task 批次完成 → 唤醒父 agent 的路径。

默认姿势 = wake_thread_announced：bot 以**自己的身份**贴一行公告（人话、短），再把内联了
全部子任务结果的长 prompt **进程内直投**给自己（resume 父 thread session、等锁不丢）。
老的 send-as-user @bot 降级为兜底——它借 owner 身份发消息、把整段 prompt 铺在群里刷屏，
而且要求该 profile 的 lark-cli 有 user 身份（线上只有 spx 有；2026-09-03 16:07 实锤：
agy 批次跑完，父 agent 永远没醒）。
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dispatcher


class _Profile:
    name = "agy"
    lark_cli_profile = "agy"


class _Feishu:
    """记录 bot 身份发出的公告；返回新 message_id 当后续回答卡片的锚点。"""

    def __init__(self):
        self.replies: list[tuple[str, str]] = []
        self.boom = False

    async def reply_text(self, message_id: str, text: str) -> str:
        if self.boom:
            raise RuntimeError("Lark 挂了")
        self.replies.append((message_id, text))
        return "om_announce"


class _Bot:
    def __init__(self):
        self.profile = _Profile()
        self.feishu = _Feishu()
        self._locks: dict[str, asyncio.Lock] = {}

    def _ensure_chat_lock(self, chat_id: str) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())


def _grp(bot):
    return {
        "bot": bot, "thread": "omt_parent", "anchor": "om_anchor",
        "chat": "oc_group", "user": "ou_owner", "pending": 0,
        "results": [("子任务A", "omt_childA", "结果 A 文本", True),
                    ("子任务B", "omt_childB", "", False)],
    }


@pytest.mark.asyncio
async def test_batch_wake_announces_as_bot_then_injects_and_never_speaks_as_user(monkeypatch):
    """默认路径：群里只多一行 bot 公告，长 prompt 走进程内直投，绝不借 owner 身份 @bot。"""
    bot = _Bot()
    calls = {}

    async def fake_as_user(b, anchor, prompt):
        calls["as_user"] = (anchor, prompt)
        return True

    async def fake_process(b, user_id, chat_id, is_group, thread_id, msg, trinity_ctx=None):
        calls["process"] = dict(user_id=user_id, chat_id=chat_id, thread_id=thread_id,
                                anchor=msg.message_id, prompt=json.loads(msg.content)["text"])

    monkeypatch.setattr(dispatcher, "wake_thread_as_user", fake_as_user)
    monkeypatch.setattr(dispatcher, "_process_message", fake_process)

    await dispatcher._dispatch_wake_parent(_grp(bot))

    assert "as_user" not in calls, "默认路径不许再借 owner 身份 @bot"
    # 群里可见的只有一行人话公告，回到父 thread 的原锚点
    assert len(bot.feishu.replies) == 1
    anchor, announce = bot.feishu.replies[0]
    assert anchor == "om_anchor"
    assert "2 个子任务" in announce and "结果 A 文本" not in announce

    got = calls["process"]
    assert got["user_id"] == "ou_owner"
    assert got["chat_id"] == "oc_group:omt_parent"
    assert got["thread_id"] == "omt_parent"
    assert got["anchor"] == "om_announce", "回答卡片应挂在公告底下"
    # 内联结果只进 session，不进群
    assert "子任务批次完成" in got["prompt"]
    assert "结果 A 文本" in got["prompt"]
    assert "⚠️ 未完成" in got["prompt"]


@pytest.mark.asyncio
async def test_batch_wake_still_injects_when_announcement_fails(monkeypatch):
    """公告发失败（Lark 抽风）不许拖累唤醒：退回用原 anchor 直投。"""
    bot = _Bot()
    bot.feishu.boom = True
    seen = {}

    async def fake_process(b, user_id, chat_id, is_group, thread_id, msg, trinity_ctx=None):
        seen["anchor"] = msg.message_id

    monkeypatch.setattr(dispatcher, "_process_message", fake_process)
    await dispatcher._dispatch_wake_parent(_grp(bot))
    assert seen["anchor"] == "om_anchor"


@pytest.mark.asyncio
async def test_batch_wake_falls_back_to_send_as_user_when_injection_fails(monkeypatch):
    bot = _Bot()
    calls = {}

    async def fake_announced(b, **kw):
        calls["announced"] = kw
        return False

    async def fake_as_user(b, anchor, prompt):
        calls["as_user"] = (anchor, prompt)
        return True

    monkeypatch.setattr(dispatcher, "wake_thread_announced", fake_announced)
    monkeypatch.setattr(dispatcher, "wake_thread_as_user", fake_as_user)
    await dispatcher._dispatch_wake_parent(_grp(bot))

    assert calls["as_user"][0] == "om_anchor"
    assert calls["as_user"][1] == calls["announced"]["prompt"]


@pytest.mark.asyncio
async def test_batch_wake_both_paths_fail_does_not_raise(monkeypatch):
    bot = _Bot()

    async def fake_announced(b, **kw):
        return False

    async def fake_as_user(b, anchor, prompt):
        return False

    monkeypatch.setattr(dispatcher, "wake_thread_announced", fake_announced)
    monkeypatch.setattr(dispatcher, "wake_thread_as_user", fake_as_user)
    await dispatcher._dispatch_wake_parent(_grp(bot))  # 只 log，不冒泡


@pytest.mark.asyncio
async def test_wake_thread_internal_resumes_thread_via_process_message(monkeypatch):
    bot = _Bot()
    seen = {}

    async def fake_process(b, user_id, chat_id, is_group, thread_id, msg, trinity_ctx=None):
        seen.update(user_id=user_id, chat_id=chat_id, is_group=is_group,
                    thread_id=thread_id, msg=msg,
                    locked=b._ensure_chat_lock(chat_id).locked())

    monkeypatch.setattr(dispatcher, "_process_message", fake_process)

    ok = await dispatcher.wake_thread_internal(
        bot, user_id="ou_owner", chat_id_raw="oc_group", thread_id="omt_parent",
        anchor_msg_id="om_anchor", prompt="[🔔 子任务批次完成] 结果……",
    )
    assert ok is True
    assert seen["user_id"] == "ou_owner"
    assert seen["chat_id"] == "oc_group:omt_parent"      # 与真实 @bot 入站同一把 chat_key → resume 同一 session
    assert seen["is_group"] is True
    assert seen["thread_id"] == "omt_parent"
    assert seen["locked"] is True                          # 在 per-chat 锁内执行
    msg = seen["msg"]
    assert msg.message_type == "text"
    assert msg.message_id == "om_anchor"                   # 回复锚点 = 父 thread 原消息
    assert json.loads(msg.content)["text"].startswith("[🔔 子任务批次完成]")
    assert getattr(msg, "mentions", None) is None
    # 锁用完释放
    assert bot._ensure_chat_lock("oc_group:omt_parent").locked() is False


@pytest.mark.asyncio
async def test_wake_thread_internal_waits_for_busy_thread(monkeypatch):
    """父 thread 正在跑别的 turn 时：排队等锁，而不是像 handle_spawn 那样 reject。"""
    bot = _Bot()
    lock = bot._ensure_chat_lock("oc_group:omt_parent")
    order = []

    async def fake_process(b, user_id, chat_id, is_group, thread_id, msg, trinity_ctx=None):
        order.append("wake-ran")

    monkeypatch.setattr(dispatcher, "_process_message", fake_process)

    await lock.acquire()
    task = asyncio.create_task(dispatcher.wake_thread_internal(
        bot, user_id="u", chat_id_raw="oc_group", thread_id="omt_parent",
        anchor_msg_id="om_a", prompt="p"))
    await asyncio.sleep(0.05)
    assert order == []            # 还在等锁
    order.append("release")
    lock.release()
    assert await task is True
    assert order == ["release", "wake-ran"]


@pytest.mark.asyncio
async def test_wake_thread_internal_missing_params_or_exception_returns_false(monkeypatch):
    bot = _Bot()
    assert await dispatcher.wake_thread_internal(
        bot, user_id="", chat_id_raw="oc", thread_id="omt", anchor_msg_id="om", prompt="p") is False

    async def boom(*a, **k):
        raise RuntimeError("agent 炸了")

    monkeypatch.setattr(dispatcher, "_process_message", boom)
    assert await dispatcher.wake_thread_internal(
        bot, user_id="u", chat_id_raw="oc", thread_id="omt", anchor_msg_id="om", prompt="p") is False
    assert bot._ensure_chat_lock("oc:omt").locked() is False
