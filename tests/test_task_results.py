import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
import task_results


def test_owner_and_terminal_idempotency():
    task_results.begin(thread_id="omt_1", profile="spx", user_id="ou_owner",
                       title="sample", chat_id="oc_child")
    with pytest.raises(PermissionError):
        task_results.get("omt_1", profile="regtank", user_id="ou_owner")
    with pytest.raises(PermissionError):
        task_results.get("omt_1", profile="spx", user_id="ou_other")
    task_results.finish("omt_1", ok=True, text="real result")
    task_results.finish("omt_1", ok=False, text="duplicate must not overwrite")
    result = task_results.get("omt_1", profile="spx", user_id="ou_owner")
    assert result["status"] == "completed" and result["result"] == "real result"
    assert "user_id" not in result
    assert task_results._path("omt_1").stat().st_mode & 0o077 == 0


def test_missing_record_does_not_claim_completion():
    assert task_results.get("unknown", profile="x", user_id="u")["status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,status,text", [
    ((True, "done"), "completed", "done"),
    ((False, "failed reason"), "failed", "failed reason"),
    (RuntimeError("crash"), "failed", "RuntimeError: crash"),
    ("cancel", "cancelled", "子任务已取消"),
])
async def test_dispatch_callback_without_parent_notifications(monkeypatch, outcome, status, text):
    import dispatcher
    bot = NS(profile=NS(name="spx", runner="claude", dispatch_model="",
                         default_cwd="/tmp", default_model="",),
             feishu=NS(send_post_to_chat=AsyncMock(return_value="om_test"),
                       get_message_thread_id=AsyncMock(return_value="omt_result")))
    async def spawn(*args, **kwargs):
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(dispatcher, "handle_spawn", spawn)
    monkeypatch.setattr(dispatcher, "_dispatch_safe_reply", AsyncMock())
    monkeypatch.setattr(dispatcher, "_DISPATCH_CHILDREN", {})
    r = await dispatcher.dispatch_task(bot, user_id="ou_owner", group_chat_id="oc_child",
                                      title="sample", prompt="sample", parent_thread="", parent_anchor="")
    assert r["ok"]
    # The completion is written by the actual dispatcher future callback.
    for _ in range(4):
        await asyncio.sleep(0)
    result = task_results.get(r["thread_id"], profile="spx", user_id="ou_owner")
    assert (result["status"], result["result"]) == (status, text)
    dispatcher._dispatch_safe_reply.assert_not_called()


def test_mcp_result_is_structured_and_owner_checked(monkeypatch):
    import cc_mcp_server
    task_results.begin(thread_id="omt_private", profile="spx", user_id="ou_a", title="x", chat_id="oc_x")
    monkeypatch.setenv("CC_LARK_PROFILE", "spx")
    monkeypatch.setenv("CC_LARK_USER_ID", "ou_a")
    out = cc_mcp_server._tool_get_task_result({"thread_id": "omt_private"})
    assert out["structuredContent"]["status"] == "running"
    monkeypatch.setenv("CC_LARK_USER_ID", "ou_b")
    assert cc_mcp_server._tool_get_task_result({"thread_id": "omt_private"})["isError"]
