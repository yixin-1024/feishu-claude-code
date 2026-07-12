import asyncio
from types import SimpleNamespace

import dispatcher
from card_security import sign_action_value
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger


def _bot():
    profile = SimpleNamespace(
        name="work",
        app_id="app_work",
        app_secret="secret_work",
        allowed_open_ids={"ou_allowed"},
        allowed_group_chat_ids={"oc_allowed"},
    )
    return SimpleNamespace(profile=profile)


def _event(value, *, event_id="evt_1", app_id="app_work"):
    return P2CardActionTrigger({
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "card.action.trigger",
            "app_id": app_id,
        },
        "event": {
            "operator": {"open_id": "ou_allowed"},
            "action": {"value": value},
            "context": {
                "open_message_id": "om_card",
                "open_chat_id": "oc_allowed",
            },
        },
    })


def test_ws_card_action_uses_bound_bot_and_rejects_value_rerouting(monkeypatch):
    bot = _bot()
    scheduled = []

    def capture(coro, _loop):
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", capture)
    monkeypatch.setattr("card_security._seen_events", {})
    value = sign_action_value(
        {
            "action": "run_cmd",
            "cmd": "/status",
            "cid": "oc_allowed",
            "profile": "other",
        },
        "secret_work",
        user_id="ou_allowed",
        message_id="om_card",
    )

    response = dispatcher.on_card_action(bot, _event(value))

    assert response.toast.type == "warning"
    assert scheduled == []


def test_ws_card_action_dispatches_valid_signed_event_once(monkeypatch):
    bot = _bot()
    scheduled = []

    def capture(coro, _loop):
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", capture)
    monkeypatch.setattr("card_security._seen_events", {})
    value = sign_action_value(
        {
            "action": "run_cmd",
            "cmd": "/status",
            "cid": "oc_allowed",
            "profile": "work",
        },
        "secret_work",
        user_id="ou_allowed",
        message_id="om_card",
    )

    first = dispatcher.on_card_action(bot, _event(value, event_id="evt_once"))
    second = dispatcher.on_card_action(bot, _event(value, event_id="evt_once"))

    assert first.toast.type == "info"
    assert second.toast.content == "该操作已处理"
    assert len(scheduled) == 1
