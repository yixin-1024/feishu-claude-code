import os
import sys

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http_server


class _Store:
    def find_primary_user(self):
        return "ou_primary"


class _Bot:
    def __init__(self, name="hermes"):
        self.profile = type("Profile", (), {"name": name})()
        self.store = _Store()


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
