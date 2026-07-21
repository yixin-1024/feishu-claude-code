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
    # 默认三个闸门全开 → 全部工具注册（dispatch 闸门含 dispatch/read/append/steer 四件套）
    assert [t["name"] for t in tools] == [
        "wake_me_in", "dispatch_task", "read_thread", "append_to_task", "steer_task",
        "schedule_cron", "list_crons",
    ]
    wake = tools[0]
    assert wake["inputSchema"]["required"] == ["minutes", "note"]


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
