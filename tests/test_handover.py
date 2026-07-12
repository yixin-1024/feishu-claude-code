import os
import sys

import handover


def test_handover_uses_control_port_from_environment(monkeypatch):
    monkeypatch.setenv("CALLBACK_PORT", "12000")
    monkeypatch.setenv("CONTROL_PORT", "12009")

    assert handover._handover_url() == "http://127.0.0.1:12009/handover"


def test_handover_default_control_port_follows_callback(monkeypatch):
    monkeypatch.setenv("CALLBACK_PORT", "12000")
    monkeypatch.delenv("CONTROL_PORT", raising=False)
    monkeypatch.setattr(handover, "_REPO_ENV", "/nonexistent/.env")

    assert handover._handover_url() == "http://127.0.0.1:12001/handover"


def test_handover_reads_generated_control_token_file(monkeypatch, tmp_path):
    token_file = tmp_path / "control-token"
    token_file.write_text("generated-secret\n", encoding="utf-8")
    monkeypatch.delenv("CC_LARK_CONTROL_TOKEN", raising=False)
    monkeypatch.setenv("CC_LARK_CONTROL_TOKEN_FILE", os.fspath(token_file))
    monkeypatch.setattr(handover, "_REPO_ENV", "/nonexistent/.env")

    assert handover._control_token() == "generated-secret"


def test_handover_request_carries_bearer_token(monkeypatch):
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["authorization"] = req.get_header("Authorization")
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(sys, "argv", ["handover.py", "fingerprint", "work"])
    monkeypatch.setattr(handover, "_find_session", lambda _fp: ("sid_1", "/tmp/project"))
    monkeypatch.setattr(handover, "_control_token", lambda: "control-secret")
    monkeypatch.setattr(handover, "_handover_url", lambda: "http://127.0.0.1:9982/handover")
    monkeypatch.setattr(handover.urllib.request, "urlopen", fake_urlopen)

    handover.main()

    assert captured["authorization"] == "Bearer control-secret"
    assert captured["url"].startswith("http://127.0.0.1:9982/handover?")
    assert captured["timeout"] == 10
