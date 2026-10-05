"""分模型周额度（Fable 7d）：usage 端点拿不到时退到 Fable 探测请求的 headers。

2026-09-28 财务机：.env 钉的是 setup-token（CLAUDE_CODE_OAUTH_TOKEN，只有
user:inference），/api/oauth/usage 要 user:profile → 403（还常年 429），
/usage 卡片就悄悄少了 Fable 那一行。Fable 周额度其实也在 /v1/messages 的
unified-7d_oi-* headers 里，只是得用 Fable 模型发请求才回。
"""

import email.message
import json
import os
import sys
import urllib.error

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import account_switcher as accs  # noqa: E402
import commands  # noqa: E402

_FABLE_HEADERS = {
    "anthropic-ratelimit-unified-7d_oi-utilization": "0.37",
    "anthropic-ratelimit-unified-7d_oi-reset": "1790762400",
    "anthropic-ratelimit-unified-7d_oi-status": "allowed",
}


class _Resp:
    def __init__(self, headers=None, body=b"{}"):
        self.headers = headers or {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _http_error(code, headers=None):
    msg = email.message.Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(url="x", code=code, msg="err", hdrs=msg, fp=None)


@pytest.fixture(autouse=True)
def _clean_backoff():
    accs._usage_skip_until.clear()
    yield
    accs._usage_skip_until.clear()


def _route(monkeypatch, usage, messages):
    """usage / messages：返回 _Resp 或抛出的异常。记录每次请求。"""
    seen = []

    def fake(req, **_kw):
        seen.append(req)
        r = usage if req.full_url == accs._USAGE_URL else messages
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(accs, "urlopen_with_retry", fake)
    return seen


def test_usage_403_falls_back_to_fable_probe_headers(monkeypatch):
    seen = _route(monkeypatch, _http_error(403), _Resp(_FABLE_HEADERS))

    out = accs.fetch_scoped_weekly_limits("sk-ant-oat01-setup")

    assert out == [{"name": "Fable", "u": 0.37, "r": 1790762400, "sev": "allowed"}]
    probe = seen[-1]
    assert probe.full_url == accs._API_URL
    body = json.loads(probe.data)
    assert body["model"] == accs._SCOPED_PROBE_MODEL and body["max_tokens"] == 1
    # 订阅 OAuth 调非 haiku 模型不带 Claude Code 身份一律 429 "Error"
    assert body["system"][0]["text"] == accs._CLAUDE_CODE_SYSTEM
    assert "claude-code-20250219" in probe.get_header("Anthropic-beta")
    # 带旧版 CLI 的 UA 会被服务端按版本拒掉新模型
    assert probe.get_header("User-agent") is None


def test_usage_403_backs_off_so_next_call_skips_endpoint(monkeypatch):
    seen = _route(monkeypatch, _http_error(403), _Resp(_FABLE_HEADERS))
    accs.fetch_scoped_weekly_limits("tok")
    seen.clear()

    accs.fetch_scoped_weekly_limits("tok")

    assert [r.full_url for r in seen] == [accs._API_URL], "403 后不该再去撞 usage 端点"


def test_usage_429_honours_retry_after(monkeypatch):
    _route(monkeypatch, _http_error(429, {"Retry-After": "1352"}), _Resp(_FABLE_HEADERS))
    accs.fetch_scoped_weekly_limits("tok")
    (until,) = accs._usage_skip_until.values()
    import time
    assert 1300 < until - time.time() <= 1352


def test_usage_endpoint_success_skips_probe(monkeypatch):
    payload = {"limits": [{
        "kind": "weekly_scoped",
        "scope": {"model": {"display_name": "Fable"}},
        "percent": 12,
        "resets_at": "2026-09-30T10:00:00+00:00",
        "severity": "ok",
    }]}
    seen = _route(monkeypatch, _Resp(body=json.dumps(payload).encode()),
                  AssertionError("usage 端点有数时不该再发 Fable 请求"))

    out = accs.fetch_scoped_weekly_limits("tok")

    assert [r.full_url for r in seen] == [accs._USAGE_URL]
    assert out[0]["name"] == "Fable" and out[0]["u"] == pytest.approx(0.12)


def test_fable_limit_exhausted_429_still_parsed(monkeypatch):
    hdrs = dict(_FABLE_HEADERS, **{
        "anthropic-ratelimit-unified-7d_oi-utilization": "1.0",
        "anthropic-ratelimit-unified-7d_oi-status": "rejected",
    })
    _route(monkeypatch, _http_error(403), _http_error(429, hdrs))

    out = accs.fetch_scoped_weekly_limits("tok")

    assert out == [{"name": "Fable", "u": 1.0, "r": 1790762400, "sev": "rejected"}]


def test_probe_without_7d_oi_headers_returns_empty(monkeypatch):
    _route(monkeypatch, _http_error(403), _Resp({
        "anthropic-ratelimit-unified-7d-utilization": "0.15",
    }))
    assert accs.fetch_scoped_weekly_limits("tok") == []


def test_network_failure_returns_empty(monkeypatch):
    _route(monkeypatch, OSError("boom"), OSError("boom"))
    assert accs.fetch_scoped_weekly_limits("tok") == []


def test_usage_card_renders_fable_row():
    lines = commands._usage_single_account_lines({
        "u5h": 0.1, "u7d": 0.2, "r5h": None, "r7d": None,
        "s5h": "allowed", "s7d": "allowed",
        "scoped7d": [{"name": "Fable", "u": 0.0, "r": None, "sev": "allowed"}],
    }, account_label="env-token（钉死）")
    text = "\n".join(lines)
    assert "**7天窗口（Fable）**（状态：allowed）" in text
    assert "0.0%" in text, "0% 也要显示，不能当成没数据跳过"
