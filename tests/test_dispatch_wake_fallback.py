"""dispatch_task 批次完成 → 唤醒父 agent 的兜底路径。

线上只有 spx 的 lark-cli 有 user 身份；agy / grok / regtank / seesaw 走 send-as-user
必失败（2026-09-03 16:07 实锤：agy 批次跑完，父 agent 永远没醒）。修复后：
send-as-user 失败 → wake_thread_internal 进程内直投（resume 父 thread session、等锁不丢）。
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


class _Bot:
    def __init__(self):
        self.profile = _Profile()
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
async def test_batch_wake_falls_back_to_internal_when_send_as_user_fails(monkeypatch):
    bot = _Bot()
    calls = {}

    async def fake_as_user(b, anchor, prompt):
        calls["as_user"] = (anchor, prompt)
        return False

    async def fake_internal(b, *, user_id, chat_id_raw, thread_id, anchor_msg_id, prompt):
        calls["internal"] = dict(user_id=user_id, chat_id_raw=chat_id_raw,
                                 thread_id=thread_id, anchor_msg_id=anchor_msg_id, prompt=prompt)
        return True

    monkeypatch.setattr(dispatcher, "wake_thread_as_user", fake_as_user)
    monkeypatch.setattr(dispatcher, "wake_thread_internal", fake_internal)

    await dispatcher._dispatch_wake_parent(_grp(bot))

    assert calls["as_user"][0] == "om_anchor"
    got = calls["internal"]
    assert got["user_id"] == "ou_owner"
    assert got["chat_id_raw"] == "oc_group"
    assert got["thread_id"] == "omt_parent"
    assert got["anchor_msg_id"] == "om_anchor"
    # 内联结果原样带进兜底 prompt
    assert "子任务批次完成" in got["prompt"]
    assert "结果 A 文本" in got["prompt"]
    assert "⚠️ 未完成" in got["prompt"]
    assert got["prompt"] == calls["as_user"][1]


@pytest.mark.asyncio
async def test_batch_wake_skips_internal_when_send_as_user_ok(monkeypatch):
    bot = _Bot()
    hit = []

    async def fake_as_user(b, anchor, prompt):
        return True

    async def fake_internal(b, **kw):
        hit.append(kw)
        return True

    monkeypatch.setattr(dispatcher, "wake_thread_as_user", fake_as_user)
    monkeypatch.setattr(dispatcher, "wake_thread_internal", fake_internal)
    await dispatcher._dispatch_wake_parent(_grp(bot))
    assert hit == []


@pytest.mark.asyncio
async def test_batch_wake_both_paths_fail_does_not_raise(monkeypatch):
    bot = _Bot()

    async def fake_as_user(b, anchor, prompt):
        return False

    async def fake_internal(b, **kw):
        return False

    monkeypatch.setattr(dispatcher, "wake_thread_as_user", fake_as_user)
    monkeypatch.setattr(dispatcher, "wake_thread_internal", fake_internal)
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
