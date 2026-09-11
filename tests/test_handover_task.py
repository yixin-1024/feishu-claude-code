"""会话移交（dispatcher.handover_task / MCP handover）：交所有权而不是派子任务。

⚠️ 与 tests/test_handover.py 无关——那份测的是 handover.py，即"本机终端 CLI 会话被
Lark bot 接管"（GET /handover）。这份测的是"把整项任务交给另一个 agent"（POST
/handover_task）。同名两件事，别混。

移交与 dispatch_task 的分界就是这里要钉死的东西——**移交方不收回报**：
子会话跑完不往原话题贴完成通知、不登记批次、不唤醒移交方；同时原话题挂着的
定时唤醒要被清掉（否则老 agent 会带着爆掉的上下文醒回来抢同一件活）。
这份用例覆盖：简报的渲染/落盘、dispatcher.handover_task 的行为、以及
MCP 工具 → HTTP control → dispatcher 整条链路。
"""

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import cc_mcp_server
import dispatcher
import handover_store
import http_server

CHAT = "oc_handover"
NEW_THREAD = "omt_successor"
OLD_THREAD = "omt_predecessor"
OLD_ANCHOR = "om_predecessor"

BRIEF = {
    "goal": "把 handover 功能实现完\n验收：单测全绿",
    "completed": "已经写好 handover_store 并接进 dispatcher",
    "remaining": "补 README 与 prompt 文档",
    "notes": "别碰 prod",
    "files": "/repo/handover_store.py",
}


class _Bot:
    def __init__(self, name="spx", runner="claude"):
        self.profile = NS(name=name, runner=runner, app_id=f"app_{name}",
                          dispatch_model="", default_model="")
        self.store = NS(find_primary_user=lambda: f"ou_{name}")
        self.feishu = NS(
            reply_text=AsyncMock(return_value="om_note"),
            send_post_to_chat=AsyncMock(return_value="om_root"),
            get_message_thread_id=AsyncMock(return_value=NEW_THREAD),
        )


@pytest.fixture
def bots(monkeypatch):
    """真跑 dispatcher.handover_task/dispatch_task，只把子会话与 scheduler 换成假的。"""
    caller, target = _Bot("spx", "claude"), _Bot("gpt", "codex")
    spawns = []

    async def _fake_spawn(_bot, **kwargs):
        spawns.append({"bot": _bot, **kwargs})
        return (True, "successor done")

    cancelled = []

    def _fake_cancel_wake(**kwargs):
        cancelled.append(kwargs)
        return {"ok": True, "count": 2, "cancelled": [{"job_id": "wake-1"}, {"job_id": "wake-2"}]}

    import scheduler
    monkeypatch.setattr(dispatcher, "handle_spawn", _fake_spawn)
    monkeypatch.setattr(scheduler, "cancel_wake", _fake_cancel_wake)
    monkeypatch.setattr(dispatcher, "_DISPATCH_TASKS", set())
    monkeypatch.setattr(dispatcher, "_DISPATCH_CHILDREN", {})
    monkeypatch.setattr(dispatcher, "_DISPATCH_PARENTS", {})
    yield NS(caller=caller, target=target, spawns=spawns, cancelled=cancelled)
    dispatcher._DISPATCH_CHILDREN.clear()
    dispatcher._DISPATCH_PARENTS.clear()


async def _drain():
    """等 fire-and-forget 的子会话 + 回报 task 全部落地（回报本该一个都没有）。"""
    for _ in range(6):
        await asyncio.sleep(0)
    pending = [t for t in dispatcher._DISPATCH_TASKS if not t.done()]
    if pending:
        await asyncio.wait_for(asyncio.gather(*pending), 2)


async def _handover(bots, brief=None, **kwargs):
    result = await dispatcher.handover_task(
        bots.caller, user_id="ou_spx", group_chat_id=CHAT,
        brief=brief if brief is not None else dict(BRIEF),
        from_thread=OLD_THREAD, from_anchor=OLD_ANCHOR, **kwargs,
    )
    await _drain()
    return result


# ── 简报本身 ──────────────────────────────────────────────────

def test_normalize_accepts_string_or_list():
    out = handover_store.normalize({"goal": " g ", "completed": ["done a", "- done b"],
                                    "remaining": "r", "junk": "dropped"})
    assert out["goal"] == "g"
    assert out["completed"] == "- done a\n- done b"
    assert set(out) == {key for key, _ in handover_store.SECTIONS}


def test_normalize_rejects_non_object():
    with pytest.raises(ValueError):
        handover_store.normalize("just a string")


def test_missing_required_lists_empty_core_fields():
    assert handover_store.missing_required(handover_store.normalize({"goal": "g"})) == [
        "completed", "remaining"]
    assert handover_store.missing_required(handover_store.normalize(BRIEF)) == []


def test_render_keeps_all_sections_and_marks_gaps():
    text = handover_store.render(
        handover_store.normalize({"goal": "g", "completed": "c", "remaining": "r"}),
        {"title": "T", "from": "spx[claude]", "to": "gpt[codex]", "from_thread": OLD_THREAD},
    )
    for _, title in handover_store.SECTIONS:
        assert f"## {title}" in text
    assert "spx[claude]" in text and "gpt[codex]" in text and OLD_THREAD in text
    assert text.count("（移交方未填写）") == 2  # notes + files 未填


def test_save_never_overwrites_a_previous_brief():
    first = handover_store.save("# one\n", title="same title")
    second = handover_store.save("# two\n", title="same title")
    assert first != second
    assert open(first, encoding="utf-8").read() == "# one\n"
    assert open(second, encoding="utf-8").read() == "# two\n"


def test_append_target_records_successor_thread():
    path = handover_store.save("# brief\n", title="t")
    handover_store.append_target(path, NEW_THREAD)
    assert NEW_THREAD in open(path, encoding="utf-8").read()


# ── dispatcher.handover_task ──────────────────────────────────

async def test_handover_never_reports_back_to_predecessor(bots):
    """核心差异：移交后子会话跑完**不**回报、**不**登记批次、**不**唤醒移交方。"""
    result = await _handover(bots)

    assert result["ok"] is True and result["thread_id"] == NEW_THREAD
    assert dispatcher._DISPATCH_PARENTS == {}  # 没有父批次 = 没有收口唤醒
    # 原话题只该收到那一条移交标记，子会话结束后不再有任何 🔔 完成通知
    notes = [call.args[1] for call in bots.caller.feishu.reply_text.await_args_list]
    assert len(notes) == 1 and notes[0].startswith("🔀")
    assert NEW_THREAD in notes[0] and "不再跟进" in notes[0]


async def test_successor_prompt_carries_ownership_and_brief(bots):
    await _handover(bots)

    prompt = bots.spawns[0]["prompt"]
    assert "HANDOVER" in prompt and "由你负责" in prompt
    assert "不要**回报给移交方" in prompt
    assert OLD_THREAD in prompt                      # 要细节可回看原话题
    assert "把 handover 功能实现完" in prompt        # 简报内联，接手方无需别处取
    assert "补 README 与 prompt 文档" in prompt
    assert "别碰 prod" in prompt


async def test_brief_is_persisted_and_referenced_in_prompt(bots):
    result = await _handover(bots)

    path = result["brief_path"]
    assert path and path in bots.spawns[0]["prompt"]
    saved = open(path, encoding="utf-8").read()
    assert "把 handover 功能实现完" in saved
    assert NEW_THREAD in saved  # 移交成功后补记接手话题


async def test_handover_survives_brief_write_failure(bots, monkeypatch):
    """落盘只是副本，简报已内联在 prompt 里——写盘失败不该让移交失败。"""
    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(handover_store, "save", _boom)
    result = await _handover(bots)

    assert result["ok"] is True and result["brief_path"] == ""
    assert "落盘在" not in bots.spawns[0]["prompt"]
    assert "把 handover 功能实现完" in bots.spawns[0]["prompt"]


async def test_handover_cancels_predecessor_pending_wakes(bots):
    result = await _handover(bots)

    assert bots.cancelled == [{"thread_id": OLD_THREAD, "chat_id": CHAT}]
    assert result["cancelled_wakes"] == 2
    assert "2 个待触发的定时唤醒" in bots.caller.feishu.reply_text.await_args.args[1]


async def test_wake_cleanup_failure_does_not_fail_handover(bots, monkeypatch):
    import scheduler

    def _boom(**_kw):
        raise RuntimeError("scheduler down")

    monkeypatch.setattr(scheduler, "cancel_wake", _boom)
    result = await _handover(bots)

    assert result["ok"] is True and result["cancelled_wakes"] == 0


@pytest.mark.parametrize("missing", ["goal", "completed", "remaining"])
async def test_incomplete_brief_is_rejected_before_creating_a_thread(bots, missing):
    brief = {**BRIEF, missing: "  "}
    result = await _handover(bots, brief=brief)

    assert result["ok"] is False and missing in result["error"]
    bots.caller.feishu.send_post_to_chat.assert_not_awaited()
    assert bots.spawns == []


async def test_context_dump_sized_brief_is_rejected(bots):
    result = await _handover(bots, brief={**BRIEF, "notes": "x" * handover_store.MAX_BRIEF_CHARS})

    assert result["ok"] is False and "太长" in result["error"]
    bots.caller.feishu.send_post_to_chat.assert_not_awaited()


async def test_failed_dispatch_leaves_predecessor_untouched(bots):
    """派发被并发闸门拒 → 不能清掉移交方的唤醒、也不能贴"已移交"标记。"""
    result = await _handover(bots, cap=0)

    assert result["ok"] is False
    assert bots.cancelled == []
    bots.caller.feishu.reply_text.assert_not_awaited()


async def test_cross_agent_handover_runs_on_target_bot(bots):
    result = await _handover(bots, target_bot=bots.target, model="gpt-5.5")

    assert result["agent"] == "gpt" and result["agent_runner"] == "codex"
    # 话题由接手方 bot 建、子会话在接手方 bot 名下跑，@ 的也是它自己的归属人
    bots.target.feishu.send_post_to_chat.assert_awaited_once()
    assert bots.target.feishu.send_post_to_chat.await_args.kwargs["mention_open_id"] == "ou_gpt"
    assert bots.spawns[0]["bot"] is bots.target
    # 移交标记仍由移交方 bot 贴回自己的原话题
    bots.caller.feishu.reply_text.assert_awaited_once()
    assert "gpt[codex]" in bots.caller.feishu.reply_text.await_args.args[1]


async def test_topic_marks_the_handover_for_the_user(bots):
    await _handover(bots)

    post = bots.caller.feishu.send_post_to_chat.await_args.kwargs
    assert "移交" in post["title"]
    assert "会话移交" in post["body_text"] and OLD_THREAD in post["body_text"]


# ── MCP 工具（stdio 前端）─────────────────────────────────────

@pytest.fixture
def mcp_env(monkeypatch):
    monkeypatch.setenv("CC_LARK_CONTROL_PORT", "9988")
    monkeypatch.setenv("CC_LARK_CONTROL_TOKEN", "control-secret")
    monkeypatch.setenv("CC_LARK_PROFILE", "spx")
    monkeypatch.setenv("CC_LARK_CHAT_ID", CHAT)
    monkeypatch.setenv("CC_LARK_USER_ID", "ou_spx")
    monkeypatch.setenv("CC_LARK_THREAD_ID", OLD_THREAD)
    monkeypatch.setenv("CC_LARK_ANCHOR", OLD_ANCHOR)


def _fake_http(monkeypatch, response: dict) -> dict:
    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps(response).encode()

    def _urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(cc_mcp_server.urllib.request, "urlopen", _urlopen)
    return captured


def test_tool_posts_brief_without_any_parent_context(monkeypatch, mcp_env):
    captured = _fake_http(monkeypatch, {
        "ok": True, "thread_id": NEW_THREAD, "agent": "gpt", "agent_runner": "codex",
        "active_after": 1, "cap": 7, "cancelled_wakes": 1, "brief_path": "/tmp/b.md",
    })

    result = cc_mcp_server._tool_handover({**BRIEF, "agent": "gpt"})

    assert result["isError"] is False
    assert captured["url"] == "http://127.0.0.1:9988/handover_task"
    body = captured["body"]
    assert body["brief"] == BRIEF
    assert body["from_thread"] == OLD_THREAD and body["from_anchor"] == OLD_ANCHOR
    # 移交没有回报闭环：parent_* 一个都不许出现（那是 dispatch_task 的字段）
    assert "parent_thread" not in body and "parent_anchor" not in body
    text = result["content"][0]["text"]
    assert NEW_THREAD in text and "END YOUR TURN" in text and "NOT report back" in text


@pytest.mark.parametrize("missing", ["goal", "completed", "remaining"])
def test_tool_refuses_an_empty_brief_without_calling_the_bot(monkeypatch, mcp_env, missing):
    captured = _fake_http(monkeypatch, {"ok": True})

    result = cc_mcp_server._tool_handover({**BRIEF, missing: "   "})

    assert result["isError"] is True and missing in result["content"][0]["text"]
    assert captured == {}


def test_tool_requires_group_context(monkeypatch, mcp_env):
    monkeypatch.delenv("CC_LARK_CHAT_ID")
    captured = _fake_http(monkeypatch, {"ok": True})

    result = cc_mcp_server._tool_handover(dict(BRIEF))

    assert result["isError"] is True and "group session" in result["content"][0]["text"]
    assert captured == {}


def test_tool_surfaces_bot_rejection(monkeypatch, mcp_env):
    _fake_http(monkeypatch, {"ok": False, "error": "并发已达上限 7"})

    result = cc_mcp_server._tool_handover(dict(BRIEF))

    assert result["isError"] is True and "并发已达上限 7" in result["content"][0]["text"]


# ── 整链路：MCP 工具 → HTTP control → dispatcher ───────────────

async def test_mcp_http_dispatcher_handover_integration(bots, monkeypatch, mcp_env):
    monkeypatch.setattr(http_server, "_bots", {"spx": bots.caller, "gpt": bots.target})
    monkeypatch.setattr(http_server, "_bot_loop", asyncio.get_running_loop())
    monkeypatch.setattr(http_server, "_handlers",
                        NS(handover_task=dispatcher.handover_task))
    monkeypatch.setattr(http_server, "_control_token", "test-handover-token")
    server = http_server.start_control_server(0)
    monkeypatch.setenv("CC_LARK_CONTROL_PORT", str(server.server_address[1]))
    monkeypatch.setenv("CC_LARK_CONTROL_TOKEN", "test-handover-token")
    try:
        result = await asyncio.to_thread(
            cc_mcp_server._tool_handover, {**BRIEF, "agent": "gpt", "title": "接着做 handover"})
        await _drain()
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()

    assert result["isError"] is False
    assert f"thread_id={NEW_THREAD}" in result["content"][0]["text"]
    assert bots.spawns[0]["bot"] is bots.target          # 跨 agent 落到 codex bot
    assert "由你负责" in bots.spawns[0]["prompt"]
    assert dispatcher._DISPATCH_PARENTS == {}            # 全链路下也没有回报闭环
    assert bots.cancelled == [{"thread_id": OLD_THREAD, "chat_id": CHAT}]
