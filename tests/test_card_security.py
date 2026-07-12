import card_security
from feishu_client import FeishuClient


class _Profile:
    allowed_open_ids = {"ou_allowed"}
    allowed_group_chat_ids = {"oc_allowed"}


def test_signed_action_is_bound_to_body_user_and_message(monkeypatch):
    monkeypatch.setenv("CC_LARK_CARD_ACTION_TTL_SEC", "3600")
    signed = card_security.sign_action_value(
        {"action": "run_cmd", "cmd": "/status", "profile": "work"},
        "app-secret",
        user_id="ou_allowed",
        message_id="om_card",
        now=1000,
    )

    assert card_security.verify_action_value(
        signed, "app-secret", user_id="ou_allowed", message_id="om_card", now=1100,
    ) == (True, "")
    assert card_security.verify_action_value(
        signed, "app-secret", user_id="ou_other", message_id="om_card", now=1100,
    )[0] is False
    assert card_security.verify_action_value(
        signed, "app-secret", user_id="ou_allowed", message_id="om_other", now=1100,
    )[0] is False


def test_signed_action_rejects_tampering_and_expiry(monkeypatch):
    monkeypatch.setenv("CC_LARK_CARD_ACTION_TTL_SEC", "60")
    signed = card_security.sign_action_value(
        {"reply": "A"}, "app-secret", user_id="ou_allowed", message_id="om_card", now=1000,
    )
    tampered = {**signed, "reply": "B"}

    assert card_security.verify_action_value(
        tampered, "app-secret", user_id="ou_allowed", message_id="om_card", now=1001,
    )[0] is False
    assert card_security.verify_action_value(
        signed, "app-secret", user_id="ou_allowed", message_id="om_card", now=1061,
    ) == (False, "expired action")


def test_unsigned_action_and_missing_signing_context_fail_closed():
    assert card_security.verify_action_value(
        {"cmd": "/restart"}, "app-secret", user_id="ou_allowed", message_id="om_card",
    ) == (False, "unsigned action")

    for kwargs in ({"user_id": "", "message_id": "om_card"},
                   {"user_id": "ou_allowed", "message_id": ""}):
        try:
            card_security.sign_action_value({"reply": "A"}, "app-secret", **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError("missing card binding must fail closed")


def test_card_action_allowlists_cover_user_and_group():
    profile = _Profile()

    assert card_security.card_action_allowed(profile, "ou_allowed", "ou_allowed")
    assert card_security.card_action_allowed(profile, "ou_allowed", "oc_allowed:omt_thread")
    assert not card_security.card_action_allowed(profile, "ou_other", "oc_allowed")
    assert not card_security.card_action_allowed(profile, "ou_allowed", "oc_other")
    assert card_security.card_context_matches("ou_allowed", "ou_allowed", "oc_dm")
    assert card_security.card_context_matches(
        "ou_allowed", "oc_allowed:omt_thread", "oc_allowed",
    )
    assert not card_security.card_context_matches(
        "ou_allowed", "oc_allowed:omt_thread", "oc_other",
    )


def test_callback_event_ids_are_claimed_once(monkeypatch):
    monkeypatch.setattr(card_security, "_seen_events", {})

    assert card_security.claim_event("work", "evt_1", now=1000)
    assert not card_security.claim_event("work", "evt_1", now=1001)
    assert card_security.claim_event("other", "evt_1", now=1001)
    assert card_security.claim_event("work", "evt_2", now=1001)


def test_arbitrary_card_elements_get_identical_signed_callback_values():
    client = FeishuClient(None, app_secret="app-secret")
    protected = client._protect_card_elements([{
        "tag": "column_set",
        "columns": [{
            "tag": "column",
            "elements": [{
                "tag": "button",
                "value": {"reply": "A", "profile": "work", "_cc_uid": "ou_allowed"},
                "behaviors": [{
                    "type": "callback",
                    "value": {"reply": "A", "profile": "work", "_cc_uid": "ou_allowed"},
                }],
            }],
        }],
    }], "om_card")

    button = protected[0]["columns"][0]["elements"][0]
    assert button["value"] == button["behaviors"][0]["value"]
    assert card_security.verify_action_value(
        button["value"],
        "app-secret",
        user_id="ou_allowed",
        message_id="om_card",
    ) == (True, "")
