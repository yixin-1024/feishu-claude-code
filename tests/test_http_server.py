import json
import os
import stat
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http_server
from card_security import sign_action_value


class _Store:
    def find_primary_user(self):
        return "ou_primary"


class _Bot:
    def __init__(self, name="hermes"):
        self.profile = type("Profile", (), {
            "name": name,
            "app_id": f"app_{name}",
            "app_secret": f"secret_{name}",
            "verification_token": "",
            "allowed_open_ids": {"ou_allowed"},
            "allowed_group_chat_ids": {"oc_allowed"},
        })()
        self.store = _Store()


def _request(base, path, *, method="GET", payload=None, token=""):
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@contextmanager
def _running(server):
    host, port = server.server_address[:2]
    try:
        yield f"http://127.0.0.1:{port}", host
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def control_runtime(monkeypatch):
    calls = []
    handlers = SimpleNamespace(
        list_crons=lambda chat_id: calls.append(chat_id) or {
            "ok": True,
            "jobs": [{"name": "safe", "next_run": "tomorrow"}],
        },
    )
    monkeypatch.setattr(http_server, "_handlers", handlers)
    monkeypatch.setattr(http_server, "_control_token", "test-secret")
    return calls


def test_resolve_spawn_request_passes_optional_model(monkeypatch):
    monkeypatch.setattr(http_server, "_bots", {"hermes": _Bot()})

    bot, user_id, kwargs, err = http_server._resolve_spawn_request({
        "profile": "hermes",
        "chat_id": "oc_chat",
        "thread_id": "omt_thread",
        "anchor_message_id": "om_anchor",
        "prompt": "hello",
        "model": "google/gemini-3.1-pro-preview",
    })

    assert err is None
    assert bot.profile.name == "hermes"
    assert user_id == "ou_primary"
    assert kwargs["model"] == "google/gemini-3.1-pro-preview"


@pytest.mark.parametrize("method,path", [
    ("POST", "/spawn"),
    ("POST", "/trigger"),
    ("POST", "/reload"),
    ("POST", "/wake"),
    ("POST", "/dispatch"),
    ("POST", "/read_thread"),
    ("POST", "/schedule_cron"),
    ("POST", "/list_crons"),
    ("GET", "/spawn"),
    ("GET", "/trigger"),
    ("GET", "/reload"),
    ("GET", "/handover"),
])
def test_public_listener_hides_every_control_route(control_runtime, method, path):
    with _running(http_server.start_callback_server(0)) as (base, _host):
        payload = {} if method == "POST" else None
        status, result = _request(
            base, path, method=method, payload=payload, token="test-secret",
        )

    assert status == 404
    assert result == {"error": "not found"}
    assert control_runtime == []


@pytest.mark.parametrize("path", ["/callback", "/"])
def test_public_callback_url_verification_still_works(control_runtime, path):
    with _running(http_server.start_callback_server(0)) as (base, _host):
        status, result = _request(
            base,
            path,
            method="POST",
            payload={"type": "url_verification", "challenge": "ok"},
        )

    assert status == 200
    assert result == {"challenge": "ok"}


def test_public_url_verification_checks_configured_token(monkeypatch):
    bot = _Bot()
    bot.profile.verification_token = "official-token"
    monkeypatch.setattr(http_server, "_bots", {"hermes": bot})
    monkeypatch.setattr(http_server, "_handlers", SimpleNamespace())

    with _running(http_server.start_callback_server(0)) as (base, _host):
        denied = _request(
            base,
            "/callback",
            method="POST",
            payload={"type": "url_verification", "token": "wrong", "challenge": "no"},
        )
        accepted = _request(
            base,
            "/callback",
            method="POST",
            payload={
                "type": "url_verification",
                "token": "official-token",
                "challenge": "ok",
            },
        )

    assert denied == (403, {"error": "invalid verification token"})
    assert accepted == (200, {"challenge": "ok"})


def test_public_unknown_post_is_not_treated_as_callback(control_runtime):
    with _running(http_server.start_callback_server(0)) as (base, _host):
        status, result = _request(base, "/unknown", method="POST", payload={})

    assert status == 404
    assert result == {"error": "not found"}


def _card_payload(value, *, event_id="evt_1", token=""):
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "card.action.trigger",
            "app_id": "app_hermes",
            "token": token,
        },
        "event": {
            "operator": {"open_id": "ou_allowed"},
            "action": {"value": value},
            "context": {
                "open_message_id": "om_card",
                "open_chat_id": "oc_allowed",
            },
        },
    }


def test_public_callback_rejects_forged_unsigned_action(monkeypatch):
    bot = _Bot()
    monkeypatch.setattr(http_server, "_bots", {"hermes": bot})
    monkeypatch.setattr(http_server, "_handlers", SimpleNamespace())

    with _running(http_server.start_callback_server(0)) as (base, _host):
        status, result = _request(
            base,
            "/callback",
            method="POST",
            payload=_card_payload({
                "action": "run_cmd",
                "cmd": "/restart",
                "cid": "oc_allowed",
                "profile": "hermes",
            }),
        )

    assert status == 200
    assert result["toast"]["type"] == "warning"


def test_public_callback_accepts_signed_bound_action_once(monkeypatch):
    bot = _Bot()
    submitted = []

    async def handle_menu_command(*args):
        return None

    def capture_submit(coro):
        submitted.append(coro)
        coro.close()

    monkeypatch.setattr(http_server, "_bots", {"hermes": bot})
    monkeypatch.setattr(
        http_server, "_handlers", SimpleNamespace(handle_menu_command=handle_menu_command),
    )
    monkeypatch.setattr(http_server, "_submit", capture_submit)
    monkeypatch.setattr("card_security._seen_events", {})
    value = sign_action_value(
        {
            "action": "run_cmd",
            "cmd": "/status",
            "cid": "oc_allowed",
            "profile": "hermes",
        },
        "secret_hermes",
        user_id="ou_allowed",
        message_id="om_card",
    )

    with _running(http_server.start_callback_server(0)) as (base, _host):
        first = _request(
            base, "/callback", method="POST", payload=_card_payload(value, event_id="evt_once"),
        )
        second = _request(
            base, "/callback", method="POST", payload=_card_payload(value, event_id="evt_once"),
        )

    assert first[0] == 200
    assert first[1]["toast"]["type"] == "info"
    assert second == (200, {"toast": {"type": "info", "content": "该操作已处理"}})
    assert len(submitted) == 1


def test_public_callback_dispatches_switch_usage(monkeypatch):
    """/usage 里的账户按钮走 HTTP 回调路径时应命中 switch_usage 分支（而非落到
    通用 '已发送:' 兜底）。"""
    bot = _Bot()
    calls = []

    async def handle_switch_usage(bot_, user_id, chat_id, name, msg_id):
        calls.append((name, chat_id))

    def capture_submit(coro):
        calls.append("submitted")
        coro.close()

    monkeypatch.setattr(http_server, "_bots", {"hermes": bot})
    monkeypatch.setattr(
        http_server, "_handlers", SimpleNamespace(handle_switch_usage=handle_switch_usage),
    )
    monkeypatch.setattr(http_server, "_submit", capture_submit)
    monkeypatch.setattr("card_security._seen_events", {})
    value = sign_action_value(
        {
            "action": "switch_usage",
            "name": "mar",
            "cid": "oc_allowed",
            "profile": "hermes",
        },
        "secret_hermes",
        user_id="ou_allowed",
        message_id="om_card",
    )

    with _running(http_server.start_callback_server(0)) as (base, _host):
        status, result = _request(
            base, "/callback", method="POST", payload=_card_payload(value, event_id="evt_switch"),
        )

    assert status == 200
    assert result["toast"]["type"] == "info"
    assert "mar" in result["toast"]["content"]  # 不是 "已发送:"
    assert "submitted" in calls  # 确实提交了协程


def test_public_callback_checks_profile_verification_token(monkeypatch):
    bot = _Bot()
    bot.profile.verification_token = "official-token"
    monkeypatch.setattr(http_server, "_bots", {"hermes": bot})
    monkeypatch.setattr(http_server, "_handlers", SimpleNamespace())
    value = sign_action_value(
        {
            "action": "run_cmd",
            "cmd": "/status",
            "cid": "oc_allowed",
            "profile": "hermes",
        },
        "secret_hermes",
        user_id="ou_allowed",
        message_id="om_card",
    )

    with _running(http_server.start_callback_server(0)) as (base, _host):
        status, result = _request(
            base,
            "/callback",
            method="POST",
            payload=_card_payload(value, event_id="evt_bad_token", token="wrong"),
        )

    assert status == 200
    assert result["toast"]["type"] == "warning"


def test_public_callback_rejects_oversized_body_before_parsing(control_runtime, monkeypatch):
    monkeypatch.setattr(http_server, "_MAX_REQUEST_BODY", 8)
    with _running(http_server.start_callback_server(0)) as (base, _host):
        req = urllib.request.Request(
            base + "/callback",
            data=b"123456789",
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as e:
            status = e.code
            result = json.loads(e.read())

    assert status == 413
    assert result == {"error": "request body too large"}


@pytest.mark.parametrize("token", ["", "wrong-secret"])
def test_control_listener_rejects_missing_or_wrong_token(control_runtime, token):
    with _running(http_server.start_control_server(0)) as (base, host):
        status, result = _request(
            base, "/list_crons", method="POST", payload={}, token=token,
        )

    assert host == "127.0.0.1"
    assert status == 401
    assert result == {"error": "invalid control API token"}
    assert control_runtime == []


def test_control_auth_runs_before_payload_parsing(control_runtime):
    with _running(http_server.start_control_server(0)) as (base, _host):
        req = urllib.request.Request(
            base + "/spawn", data=b"not-json", method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as e:
            status = e.code
            result = json.loads(e.read())

    assert status == 401
    assert result == {"error": "invalid control API token"}


def test_control_listener_accepts_valid_token(control_runtime):
    with _running(http_server.start_control_server(0)) as (base, host):
        status, result = _request(
            base,
            "/list_crons",
            method="POST",
            payload={"chat_id": "oc_current"},
            token="test-secret",
        )

    assert host == "127.0.0.1"
    assert status == 200
    assert result["ok"] is True
    assert control_runtime == ["oc_current"]


def test_control_listener_rejects_oversized_body_after_valid_auth(
    control_runtime, monkeypatch,
):
    monkeypatch.setattr(http_server, "_MAX_REQUEST_BODY", 8)
    with _running(http_server.start_control_server(0)) as (base, _host):
        req = urllib.request.Request(
            base + "/spawn",
            data=b"123456789",
            headers={"Authorization": "Bearer test-secret"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)

    assert exc.value.code == 413
    assert json.loads(exc.value.read()) == {"error": "request body too large"}


def test_control_listener_does_not_serve_callback(control_runtime):
    with _running(http_server.start_control_server(0)) as (base, _host):
        status, result = _request(
            base,
            "/callback",
            method="POST",
            payload={"type": "url_verification", "challenge": "no"},
            token="test-secret",
        )

    assert status == 404
    assert result == {"error": "not found"}


def test_control_token_is_stable_and_mode_0600(monkeypatch, tmp_path):
    monkeypatch.delenv("CC_LARK_CONTROL_TOKEN", raising=False)
    path = tmp_path / "state" / "control-token"

    first = http_server.load_or_create_control_token(str(path))
    second = http_server.load_or_create_control_token(str(path))

    assert first == second
    assert len(first) >= 40
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_ngrok_reuse_only_matches_callback_port():
    tunnels = {
        "tunnels": [
            {
                "proto": "https",
                "public_url": "https://wrong.example",
                "config": {"addr": "http://localhost:9982"},
            },
            {
                "proto": "https",
                "public_url": "https://remote-same-port.example",
                "config": {"addr": "http://remote.example:9981"},
            },
            {
                "proto": "https",
                "public_url": "https://callback.example",
                "config": {"addr": "http://localhost:9981"},
            },
        ],
    }

    assert http_server._matching_ngrok_tunnel(tunnels, 9981) == "https://callback.example"
    assert http_server._matching_ngrok_tunnel(tunnels, 9999) is None
