"""fetch_quota_headers 的跨平台行为：Linux 上不再直接拒绝，凭证读取按后端分流。

历史 bug：`/usage`（以及共用同一入口的 quota watcher、定时任务的用量刹车线、
`/status` 的配额行）开头有一句 `if sys.platform != "darwin"` 直接返回
「目前只支持 macOS」，Linux 部署（GCP 上那台 systemd 跑的 cc-lark）永远看不到用量。
"""

import io
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import account_switcher  # noqa: E402
import commands  # noqa: E402

_BLOB = json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-test"}})


def _stub_creds(monkeypatch, blob):
    monkeypatch.setattr(account_switcher, "_read_keychain_blob", lambda: blob)
    monkeypatch.setattr(account_switcher, "ensure_keychain_intact", lambda: ("ok", None))


def _headers_response(monkeypatch, headers: dict):
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        @property
        def headers(self_inner):
            return headers

    monkeypatch.setattr(account_switcher, "urlopen_with_retry",
                        lambda req, **kw: _Resp())


def test_linux_reaches_api_instead_of_refusing(monkeypatch):
    monkeypatch.setattr(commands.sys, "platform", "linux")
    monkeypatch.setenv("CC_LARK_CRED_BACKEND", "file")
    _stub_creds(monkeypatch, _BLOB)
    _headers_response(monkeypatch, {
        "anthropic-ratelimit-unified-5h-utilization": "0.42",
        "anthropic-ratelimit-unified-7d-utilization": "0.18",
        "anthropic-ratelimit-unified-5h-reset": "1788344400",
        "anthropic-ratelimit-unified-7d-status": "allowed",
    })

    data = commands.fetch_quota_headers()

    assert data["ok"] is True
    assert data["u5h"] == 0.42
    assert data["u7d"] == 0.18
    assert data["r5h"] == 1788344400


def test_missing_credentials_names_the_store(monkeypatch):
    """凭证存储是空的 → 报错文案要指名是 keychain 还是哪个文件，别只说"读取凭证失败"。"""
    monkeypatch.setenv("CC_LARK_CRED_BACKEND", "file")
    _stub_creds(monkeypatch, None)

    data = commands.fetch_quota_headers()

    assert data["ok"] is False
    assert account_switcher.CREDENTIALS_FILE in data["error"]


def test_401_message_names_the_store(monkeypatch):
    """401 分支引用了 credentials_store_label——这里顺带钉住它不会 NameError。"""
    monkeypatch.setenv("CC_LARK_CRED_BACKEND", "file")
    _stub_creds(monkeypatch, _BLOB)

    def _raise_401(req, **kw):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b""))

    monkeypatch.setattr(account_switcher, "urlopen_with_retry", _raise_401)

    data = commands.fetch_quota_headers()

    assert data["ok"] is False
    assert "401" in data["error"]
    assert account_switcher.CREDENTIALS_FILE in data["error"]
