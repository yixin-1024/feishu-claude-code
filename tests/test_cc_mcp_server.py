import io
import json

import cc_mcp_server


def test_control_base_defaults_to_private_port(monkeypatch):
    monkeypatch.delenv("CC_LARK_CONTROL_PORT", raising=False)
    monkeypatch.delenv("CC_LARK_HTTP_PORT", raising=False)
    monkeypatch.delenv("CC_LARK_CALLBACK_PORT", raising=False)

    assert cc_mcp_server._control_base() == "http://127.0.0.1:9982"


def test_tools_list_exposes_all_runtime_tools():
    resp = cc_mcp_server._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    tools = resp["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "wake_me_in", "cancel_wake",
        "dispatch_task", "handover", "read_thread", "append_to_task", "steer_task", "get_task_result",
        "schedule_cron", "list_crons",
        "cancel_cron", "pause_cron", "resume_cron", "update_cron",
    ]
    wake = tools[0]
    assert wake["inputSchema"]["required"] == ["minutes", "note"]
    cancel = tools[1]
    assert cancel["name"] == "cancel_wake"


def test_wake_me_in_posts_current_context(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true, "fire_at_local": "06/30 12:40"}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["headers"] = req.header_items()
        return FakeResp()

    monkeypatch.setenv("CC_LARK_CALLBACK_PORT", "9981")
    monkeypatch.setenv("CC_LARK_CONTROL_PORT", "9988")
    monkeypatch.setenv("CC_LARK_CONTROL_TOKEN", "control-secret")
    monkeypatch.setenv("CC_LARK_PROFILE", "work")
    monkeypatch.setenv("CC_LARK_CHAT_ID", "oc_1")
    monkeypatch.setenv("CC_LARK_THREAD_ID", "omt_1")
    monkeypatch.setenv("CC_LARK_ANCHOR", "om_1")
    monkeypatch.setenv("CC_LARK_USER_ID", "ou_1")
    monkeypatch.setattr(cc_mcp_server.urllib.request, "urlopen", fake_urlopen)

    result = cc_mcp_server._tool_wake_me_in({"minutes": 3, "note": "check CI"})

    assert result["isError"] is False
    assert captured["url"] == "http://127.0.0.1:9988/wake"
    assert captured["timeout"] == 10
    headers = {k.lower(): v for k, v in req_headers(captured).items()}
    assert headers["authorization"] == "Bearer control-secret"
    assert captured["body"] == {
        "profile": "work",
        "chat_id": "oc_1",
        "thread_id": "omt_1",
        "anchor_message_id": "om_1",
        "user_id": "ou_1",
        "minutes": 3,
        "note": "check CI",
    }


def req_headers(captured):
    """urllib 会规范化 header 大小写，统一转 dict 供断言。"""
    return dict(captured["headers"])


def test_cancel_wake_posts_to_cancel_endpoint(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true, "count": 1, "cancelled": [{"job_id": "wake-1", "note": "task"}]}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["headers"] = req.header_items()
        return FakeResp()

    monkeypatch.setenv("CC_LARK_CONTROL_PORT", "9988")
    monkeypatch.setenv("CC_LARK_CONTROL_TOKEN", "control-secret")
    monkeypatch.setenv("CC_LARK_PROFILE", "work")
    monkeypatch.setenv("CC_LARK_CHAT_ID", "oc_1")
    monkeypatch.setenv("CC_LARK_THREAD_ID", "omt_1")
    monkeypatch.setattr(cc_mcp_server.urllib.request, "urlopen", fake_urlopen)

    # 1) 默认不传参数：自动取消当前话题
    result = cc_mcp_server._tool_cancel_wake({})
    assert result["isError"] is False
    assert "Successfully cancelled 1 scheduled wake" in result["content"][0]["text"]
    assert captured["url"] == "http://127.0.0.1:9988/wake/cancel"
    assert captured["body"]["thread_id"] == "omt_1"
    assert captured["body"]["chat_id"] == "oc_1"
    assert captured["body"]["job_id"] is None

    # 2) 显式传 job_id
    result2 = cc_mcp_server._tool_cancel_wake({"job_id": "wake-specific"})
    assert result2["isError"] is False
    assert captured["body"]["job_id"] == "wake-specific"


def test_cancel_wake_without_context_fails(monkeypatch):
    monkeypatch.delenv("CC_LARK_THREAD_ID", raising=False)
    monkeypatch.delenv("CC_LARK_CHAT_ID", raising=False)
    result = cc_mcp_server._tool_cancel_wake({})
    assert result["isError"] is True
    assert "No Lark thread context available" in result["content"][0]["text"]


def _fake_steer_urlopen(captured, resp_body=b'{"ok": true, "stopped": true, "queued": false}'):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return resp_body

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp()

    return fake_urlopen


def test_append_to_task_posts_steer_without_stop(monkeypatch):
    captured = {}
    monkeypatch.setenv("CC_LARK_CONTROL_PORT", "9988")
    monkeypatch.setenv("CC_LARK_PROFILE", "work")
    monkeypatch.setenv("CC_LARK_CHAT_ID", "oc_1")
    monkeypatch.setenv("CC_LARK_USER_ID", "ou_1")
    monkeypatch.setattr(
        cc_mcp_server.urllib.request, "urlopen",
        _fake_steer_urlopen(captured, b'{"ok": true, "queued": true}'),
    )

    result = cc_mcp_server._tool_append_to_task({"thread_id": "omt_9", "message": "也顺手加个测试"})

    assert result["isError"] is False
    assert captured["url"] == "http://127.0.0.1:9988/steer"
    assert captured["body"] == {
        "profile": "work", "chat_id": "oc_1", "user_id": "ou_1",
        "thread_id": "omt_9", "instruction": "也顺手加个测试", "stop_first": False,
    }


def test_steer_task_posts_steer_with_stop(monkeypatch):
    captured = {}
    monkeypatch.setenv("CC_LARK_CONTROL_PORT", "9988")
    monkeypatch.setenv("CC_LARK_PROFILE", "work")
    monkeypatch.setenv("CC_LARK_CHAT_ID", "oc_1")
    monkeypatch.setenv("CC_LARK_USER_ID", "ou_1")
    monkeypatch.setattr(
        cc_mcp_server.urllib.request, "urlopen", _fake_steer_urlopen(captured),
    )

    result = cc_mcp_server._tool_steer_task({"thread_id": "omt_9", "message": "方向错了，改用方案 B"})

    assert result["isError"] is False
    assert captured["url"] == "http://127.0.0.1:9988/steer"
    assert captured["body"]["stop_first"] is True
    assert captured["body"]["instruction"] == "方向错了，改用方案 B"


def test_steer_requires_thread_and_message(monkeypatch):
    monkeypatch.setenv("CC_LARK_CHAT_ID", "oc_1")
    assert cc_mcp_server._tool_append_to_task({"message": "x"})["isError"] is True
    assert cc_mcp_server._tool_steer_task({"thread_id": "omt_9", "message": "  "})["isError"] is True


def test_write_framed_message_is_newline_delimited(monkeypatch):
    """写侧必须是换行分隔 JSON（一行一条、无内嵌换行）。
    Claude Code 只认换行帧——用 Content-Length 写回会 30s 握手超时、工具全不注册。"""
    out = io.BytesIO()
    monkeypatch.setattr(cc_mcp_server.sys, "stdout", type("Stdout", (), {"buffer": out})())

    cc_mcp_server._write_framed_message({"jsonrpc": "2.0", "id": 1, "result": {}})

    raw = out.getvalue()
    assert b"Content-Length" not in raw
    assert raw.endswith(b"\n")
    line = raw[:-1]
    assert b"\n" not in line  # 单条消息内不许有换行
    assert json.loads(line.decode("utf-8"))["id"] == 1


def _fake_http_error_urlopen(status: int, body: bytes, captured=None):
    """模拟 bot 侧 _mcp_respond 的行为：ok=false → HTTP 400 + body 里带真实原因。"""
    def fake_urlopen(req, timeout=None):
        if captured is not None:
            captured["url"] = req.full_url
        raise cc_mcp_server.urllib.error.HTTPError(
            req.full_url, status, "Bad Request", {}, io.BytesIO(body),
        )

    return fake_urlopen


def test_post_json_surfaces_error_body_on_4xx(monkeypatch):
    """4xx 不能把 body 吞掉：bot 的真实原因必须原样带回给调用方。
    urllib 默认把 4xx 抛成 HTTPError，调用方只能拿到 'HTTP Error 400: Bad Request'。"""
    monkeypatch.setenv("CC_LARK_CONTROL_PORT", "9988")
    monkeypatch.setattr(
        cc_mcp_server.urllib.request, "urlopen",
        _fake_http_error_urlopen(400, b'{"ok": false, "error": "\\u5efa\\u8bdd\\u9898\\u5931\\u8d25: 230002"}'),
    )

    body = cc_mcp_server._post_json("/dispatch", {})

    assert body["ok"] is False
    assert "建话题失败: 230002" in body["error"]
    assert "HTTP 400 /dispatch" in body["error"]


def test_dispatch_task_reports_real_reason_from_400(monkeypatch):
    """端到端：dispatch 被拒时模型看到的文案必须含真实原因，而不是裸 400。"""
    monkeypatch.setenv("CC_LARK_CONTROL_PORT", "9988")
    monkeypatch.setenv("CC_LARK_PROFILE", "spx")
    monkeypatch.setenv("CC_LARK_CHAT_ID", "oc_1")
    monkeypatch.setenv("CC_LARK_USER_ID", "ou_1")
    monkeypatch.setattr(
        cc_mcp_server.urllib.request, "urlopen",
        _fake_http_error_urlopen(
            400, json.dumps({"ok": False, "error": "建话题失败: 230002 Bot/User can NOT be out of the chat"}).encode(),
        ),
    )

    result = cc_mcp_server._tool_dispatch_task({"prompt": "去干活"})

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "230002" in text
    assert "Bad Request" not in text


def test_wake_and_cron_tools_also_surface_error_body(monkeypatch):
    """不是只给 dispatch 打补丁：所有走 _post_json 的工具都应拿到真实原因。"""
    monkeypatch.setenv("CC_LARK_CONTROL_PORT", "9988")
    monkeypatch.setenv("CC_LARK_PROFILE", "spx")
    monkeypatch.setenv("CC_LARK_CHAT_ID", "oc_1")
    monkeypatch.setenv("CC_LARK_THREAD_ID", "omt_1")
    monkeypatch.setenv("CC_LARK_ANCHOR", "om_1")
    monkeypatch.setenv("CC_LARK_USER_ID", "ou_1")
    monkeypatch.setattr(
        cc_mcp_server.urllib.request, "urlopen",
        _fake_http_error_urlopen(400, b'{"ok": false, "error": "profile not loaded"}'),
    )

    wake = cc_mcp_server._tool_wake_me_in({"minutes": 3, "note": "check CI"})
    assert wake["isError"] is True
    assert "profile not loaded" in wake["content"][0]["text"]

    crons = cc_mcp_server._tool_list_crons({})
    assert crons["isError"] is True
    assert "profile not loaded" in crons["content"][0]["text"]

    steer = cc_mcp_server._tool_steer_task({"thread_id": "omt_9", "message": "改方向"})
    assert steer["isError"] is True
    assert "profile not loaded" in steer["content"][0]["text"]


def test_post_json_tolerates_non_json_error_body(monkeypatch):
    """body 不是 JSON（如反代回的 HTML 502）也不许抛，要压成可读的 ok=false。"""
    monkeypatch.setenv("CC_LARK_CONTROL_PORT", "9988")
    monkeypatch.setattr(
        cc_mcp_server.urllib.request, "urlopen",
        _fake_http_error_urlopen(502, b"<html>Bad Gateway</html>"),
    )

    body = cc_mcp_server._post_json("/dispatch", {})

    assert body["ok"] is False
    assert "502" in body["error"] and "Bad Gateway" in body["error"]


def test_post_json_tolerates_unreadable_error_body(monkeypatch):
    """连 body 都读不出来（read 抛）时也必须给出可读结论，不能往上抛。"""
    class Boom:
        def read(self):
            raise OSError("socket closed")

        def close(self):  # HTTPError 会在 GC 时关掉 fp
            pass

    def fake_urlopen(req, timeout=None):
        raise cc_mcp_server.urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, Boom())

    monkeypatch.setenv("CC_LARK_CONTROL_PORT", "9988")
    monkeypatch.setattr(cc_mcp_server.urllib.request, "urlopen", fake_urlopen)

    body = cc_mcp_server._post_json("/dispatch", {})

    assert body["ok"] is False
    assert "HTTP 400" in body["error"]


def test_post_json_tolerates_non_json_success_body(monkeypatch):
    """200 但 body 不是 JSON：同样压成 ok=false，不抛 JSONDecodeError。"""
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"not json at all"

    monkeypatch.setenv("CC_LARK_CONTROL_PORT", "9988")
    monkeypatch.setattr(cc_mcp_server.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())

    body = cc_mcp_server._post_json("/dispatch", {})

    assert body["ok"] is False
    assert "not json at all" in body["error"]
