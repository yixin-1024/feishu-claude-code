"""Exercise the real dispatcher callbacks: long process text must survive finalization."""

import asyncio
import json
import re
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import dispatcher
from bot_config import Profile
from display_pages import publish_full_display, split_display_pages
from feishu_client import _card_json


def unlabel(text):
    return re.sub(r"^\*\*（完整记录 \d+/\d+）\*\*\n\n", "", text)


@pytest.mark.parametrize("text", ["", "短回复", "中文😀\\\"\n" * 10000, "x" * 60000])
@pytest.mark.parametrize("mode", ["v1", "v2", "cardkit"])
def test_pages_are_lossless_and_fit_serialized_card_budget(text, mode, monkeypatch):
    monkeypatch.setenv("LARK_CARD_MODE", mode)
    pages = split_display_pages(text)
    assert "".join(pages) == text
    for page in pages:
        payload = json.dumps({"content": _card_json(page)}, ensure_ascii=False)
        assert len(payload.encode()) < 28000


async def exercise(monkeypatch, process, result="最终完成。", *, runner="agy", error=False,
                   fail_delivery=False):
    client = AsyncMock()
    client.reply_card.return_value = "continuation"
    client.save_outbox = Mock(return_value="saved")
    if fail_delivery:
        client.reply_card.side_effect = RuntimeError("delivery failed")
    active = Mock(stop_requested=False, last_body="")
    active.card_update_lock = asyncio.Lock()
    bot = Mock(profile=Profile(name="test", app_id="x", app_secret="s",
                              platform="lark", domain="open.larksuite.com", default_cwd="/tmp"),
               feishu=client, store=AsyncMock(), active_runs=MagicMock())
    bot.active_runs.start_run.return_value = active
    session = Mock(session_id=None, model="gemini-3.8-flash", effort=None,
                   cwd="/tmp", permission_mode="bypassPermissions", runner=runner)

    async def run(**kwargs):
        await kwargs["on_text_chunk"](process)
        if error:
            raise dispatcher.IncompleteTaskError("测试上限", "sid")
        await kwargs["on_text_chunk"](result)
        return result, "sid", False

    monkeypatch.setattr(dispatcher, "run_agent", run)
    await dispatcher._run_and_display(
        bot, user_id="u", chat_id="c", is_group=True, text="测试",
        card_msg_id="original", session=session, notify_msg_id="anchor")
    return client


def delivered(client):
    first = client.update_card_final.await_args.args[1]
    extra = [call.kwargs["content"] for call in client.reply_card.await_args_list]
    return "".join(map(unlabel, [first, *extra]))


async def test_long_agy_process_retains_beginning_middle_end(monkeypatch):
    process = "开头\n" + "逐步检查内容。\n" * 1800 + "过程结尾\n"
    client = await exercise(monkeypatch, process)
    body = delivered(client)
    assert process in body
    assert body.count("最终完成。") == 1
    assert "仅显示末段" not in body
    assert client.reply_card.await_count > 1
    assert all(c.args[0] == "anchor" for c in client.reply_card.await_args_list)
    assert client.reply_text.await_args.args[1] == "✅"


async def test_short_agy_reply_remains_single_clean_card(monkeypatch):
    client = await exercise(monkeypatch, "", "一句话。")
    assert delivered(client) == "一句话。"
    client.reply_card.assert_not_awaited()


async def test_other_runners_keep_existing_display_behavior(monkeypatch):
    client = await exercise(monkeypatch, "旧过程" * 2000, runner="claude")
    assert "仅显示末段" in delivered(client)
    client.reply_card.assert_not_awaited()


async def test_failed_agy_run_retains_all_progress_without_success(monkeypatch):
    process = "出错前开头\n" + "已做的步骤。\n" * 1600
    client = await exercise(monkeypatch, process, error=True)
    assert process in delivered(client)
    assert "任务未完成" in delivered(client)
    assert all(c.args[1] != "✅" for c in client.reply_text.await_args_list)


async def test_failed_continuation_is_saved_and_does_not_claim_success(monkeypatch):
    client = await exercise(monkeypatch, "过程内容\n" * 2000, fail_delivery=True)
    client.save_outbox.assert_called_once()
    assert "最终完成。" in client.save_outbox.call_args.args[0]
    assert all(c.args[1] != "✅" for c in client.reply_text.await_args_list)
    assert "发送未完成" in client.reply_text.await_args.args[1]


async def test_buttons_are_attached_to_last_page(monkeypatch):
    monkeypatch.setattr(dispatcher, "_extract_options", lambda _: [("确定", "yes")])
    client = await exercise(monkeypatch, "长过程\n" * 2000)
    call = client.update_card_with_buttons.await_args
    assert call.args[0] == "continuation"
    assert "最终完成。" in call.args[1]
    assert call.args[2][0]["value"]["reply"] == "yes"


async def test_private_pages_and_stop_guard():
    client = AsyncMock()
    stopped = False

    async def stop_after_first(*args):
        nonlocal stopped
        stopped = True

    client.finalize_streaming_card.side_effect = stop_after_first
    ok = await publish_full_display(client, "内容" * 10000, card_id="original",
                                    reply_to=None, user_id="u", stopped=lambda: stopped)
    assert not ok
    client.send_card_to_user.assert_not_awaited()
    client.finalize_streaming_card.side_effect = None
    ok = await publish_full_display(client, "内容" * 10000, card_id="original",
                                    reply_to=None, user_id="u", stopped=lambda: False)
    assert ok
    assert client.send_card_to_user.await_count > 0
    client.reply_card.assert_not_awaited()
