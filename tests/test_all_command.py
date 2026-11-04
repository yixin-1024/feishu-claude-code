"""/all：用量 + 服务器状态一张卡（2026-11-04 用户要的）。

- 样式按用户给的样板：用量是精简版（标题不带 token 名、重置后面带倒计时、
  没有「其他 token」小标题和自动切换那行），服务器是精简版（没有主机名/系统/IO/Swap/
  cgroup//boot）。
- 刷新按钮重跑 /all（两块一起刷），切号按钮切完还是 /all 卡。
- 右上角是刷新时间（corner），服务器状态放在按钮下面（after）。
"""

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace

import lark_oapi as lark

import commands
import feishu_client as fc
import server_stats
from feishu_client import FeishuClient


class _Store:
    def __init__(self, runner="claude"):
        self.runner = runner

    async def get_current_raw(self, user_id, chat_id):
        return {"runner": self.runner}


def _usage_reply(chat_id="oc_chat"):
    return {
        "text": "📊 **Claude 用量**\n\n剩余 **43%** · 今天 18:00 重置 (6小时55分后)\n\n👇 点按钮切 token",
        "buttons": [
            {"text": "● token1", "value": {"action": "switch_usage", "name": "token1", "cid": chat_id}},
            {"text": "token2", "value": {"action": "switch_usage", "name": "token2", "cid": chat_id}},
            {"text": "🔄 刷新", "value": {"action": "run_cmd", "cmd": "/usage", "cid": chat_id}},
        ],
    }


# ── 倒计时 ──────────────────────────────────────────────────

def test_fmt_reset_in_days_hours_minutes():
    now = time.time()
    assert commands._fmt_reset_in(now + 3 * 86400 + 4 * 3600 + 120) == "3天4小时后"
    assert commands._fmt_reset_in(now + 6 * 3600 + 55 * 60 + 30) == "6小时55分后"
    assert commands._fmt_reset_in(now + 12 * 60 + 30) == "12分钟后"
    assert commands._fmt_reset_in(now + 20) == "1分钟后"
    assert commands._fmt_reset_in(now - 5) == ""
    assert commands._fmt_reset_in("garbage") == ""


def test_quota_line_countdown_only_when_asked():
    ts = int(time.time()) + 2 * 86400 + 3600 + 60
    plain = commands._quota_line("剩余", 0.57, None, ts)
    assert "(" not in plain and plain.endswith("重置")
    with_cd = commands._quota_line("剩余", 0.57, None, ts, countdown=True)
    assert with_cd.endswith("重置 (2天1小时后)")


# ── 精简版 token 池用量 ──────────────────────────────────────

def _acc(name, u7d, r7d, usable=True):
    return SimpleNamespace(
        name=name, probe_error=None, u7d=u7d, u5h=None, r5h=None, r7d=r7d,
        s5h=None, s7d="allowed", scoped7d=[{"name": "Fable", "u": 0.0, "r": r7d, "sev": None}],
        usable=usable, score=1.0,
    )


def test_env_token_usage_compact_matches_template(monkeypatch):
    import account_switcher

    r1 = int(time.time()) + 7 * 3600
    r2 = int(time.time()) + 3 * 86400 + 5 * 3600
    accounts = {"token1": _acc("token1", 0.56, r1), "token2": _acc("token2", 0.13, r2)}
    monkeypatch.setattr(account_switcher, "active_env_token_name", lambda: "token1")
    monkeypatch.setattr(account_switcher, "probe_env_tokens", lambda: accounts)
    monkeypatch.setattr(account_switcher, "evaluate", lambda a, active: None)
    monkeypatch.setattr(account_switcher, "switch_on_limit_enabled", lambda: True)
    monkeypatch.setattr(commands, "_account_switch_buttons", lambda chat_id, for_usage=False: [
        {"text": "● token1", "value": {"action": "switch_usage", "name": "token1", "cid": chat_id}},
        {"text": "token2", "value": {"action": "switch_usage", "name": "token2", "cid": chat_id}},
        {"text": "🔄 刷新", "value": {"action": "run_cmd", "cmd": "/usage", "cid": chat_id}},
    ])

    out = commands._get_env_token_usage("oc_chat", compact=True)
    lines = out["text"].split("\n")
    assert lines[0] == "📊 **Claude 用量**"  # 不带 `token1`
    assert lines[2].startswith("剩余 **44%**") and lines[2].endswith("后)")
    assert lines[3] == "Fable 剩余 **100%**"  # 重置时刻同周额度 → 不重复
    assert lines[5].startswith("`token2` ✅ 剩 87%") and lines[5].endswith("后)")
    assert lines[-1] == "👇 点按钮切 token"
    assert "其他 token" not in out["text"]
    assert "自动切换" not in out["text"]

    # /usage 自己的样式不受影响
    full = commands._get_env_token_usage("oc_chat")["text"]
    assert "当前 `token1`" in full and "**其他 token**" in full and "撞墙自动切换" in full
    assert "后)" not in full


# ── 精简版服务器状态 ──────────────────────────────────────────

def _snap():
    gb = 1024 ** 3
    return {
        "host": "spxpay-staging", "os": "Ubuntu", "cores": 8, "uptime": 3600 * 30,
        "now": datetime(2026, 11, 4, 11, 0, 0, tzinfo=server_stats._TZ),
        "cpu": 87.0, "load": (18.81, 9.45, 5.69), "psi_io": {"some": 0.7},
        "mem": {"MemTotal": 32 * gb, "MemAvailable": 23 * gb, "SwapTotal": 8 * gb, "SwapFree": 7 * gb},
        "psi_mem": {"some": 0.0, "full": 0.0},
        "cgroup": {"unit": "cc-lark.service", "current": 17 * gb, "max": 26 * gb, "peak": 18 * gb, "oom_kill": 0},
        "disks": [
            {"mount": "/", "used": 38 * gb, "free": 9 * gb, "total": 47 * gb},
            {"mount": "/boot", "used": gb // 10, "free": gb, "total": gb},
            {"mount": "/boot/efi", "used": gb // 100, "free": gb // 10, "total": gb // 10},
            {"mount": "/mnt/data", "used": 84 * gb, "free": 14 * gb, "total": 98 * gb},
        ],
        "procs": [{"comm": "claude", "pid": 1, "rss": gb, "cpu": 50.0}],
        "linux": True,
    }


def test_server_compact_only_keeps_template_lines():
    text = server_stats.render(_snap(), compact=True)
    lines = text.split("\n")
    assert lines[0] == "🖥️ **服务器状态**"
    assert "spxpay-staging" not in text and "Ubuntu" not in text
    assert any(l.endswith("负载 18.81 / 9.45 / 5.69") for l in lines)
    for gone in ("IO 等待", "Swap", "内存压力", "cc-lark.service", "/boot"):
        assert gone not in text, gone
    assert "`/`" in text and "`/mnt/data`" in text and "**进程**" in text

    full = server_stats.render(_snap())
    for kept in ("spxpay-staging", "IO 等待", "Swap", "内存压力", "cc-lark.service", "/boot"):
        assert kept in full, kept


# ── /all 命令 ────────────────────────────────────────────────

def test_all_combines_usage_and_server(monkeypatch):
    calls = []
    monkeypatch.setattr(commands, "_get_usage", lambda chat_id, compact=False: (
        calls.append(compact) or _usage_reply(chat_id)))
    monkeypatch.setattr(server_stats, "get_report", lambda compact=False: f"🖥️ server compact={compact}")

    out = asyncio.run(commands.handle_command("all", "", "ou_user", "oc_chat", _Store()))

    assert calls == [True]
    assert out["text"].startswith("📊 **Claude 用量**")
    assert out["after"] == "🖥️ server compact=True"
    assert out["corner"].endswith(" 刷新")
    datetime.strptime(out["corner"].split(" 刷新")[0], "%m-%d %H:%M:%S")
    cmds = [b["value"]["cmd"] for b in out["buttons"]]
    assert cmds == ["/all switch token1", "/all switch token2", "/all"]
    assert all(b["value"]["action"] == "run_cmd" for b in out["buttons"])
    assert [b["text"] for b in out["buttons"]] == ["● token1", "token2", "🔄 刷新"]


def test_all_switch_then_rerender(monkeypatch):
    switched = []
    monkeypatch.setattr(commands, "_switch_claude_account", lambda name: (
        switched.append(name) or f"✅ 已切换到 token `{name}`。\n新启动的任务会使用它"))
    monkeypatch.setattr(commands, "_get_usage", lambda chat_id, compact=False: _usage_reply(chat_id))
    monkeypatch.setattr(server_stats, "get_report", lambda compact=False: "server")

    out = asyncio.run(commands.handle_command("all", "switch token2", "ou_user", "oc_chat", _Store()))
    assert switched == ["token2"]
    assert "已切换" not in out["text"]  # 成功不写，卡上 ● 自己就换了
    assert out["after"] == "server"


def test_all_switch_failure_shows_under_title(monkeypatch):
    monkeypatch.setattr(commands, "_switch_claude_account", lambda name: "❌ 切换 token 失败：nope")
    monkeypatch.setattr(commands, "_get_usage", lambda chat_id, compact=False: _usage_reply(chat_id))
    monkeypatch.setattr(server_stats, "get_report", lambda compact=False: "server")

    out = asyncio.run(commands.handle_command("all", "switch tokenX", "ou_user", "oc_chat", _Store()))
    lines = out["text"].split("\n")
    assert lines[0] == "📊 **Claude 用量**"  # 标题仍在第一行（右上角时间靠它对齐）
    assert lines[2] == "❌ 切换 token 失败：nope"


def test_all_switch_routes_agy(monkeypatch):
    switched = []
    monkeypatch.setattr(commands, "_switch_agy_account", lambda name: switched.append(name) or "✅ ok")
    monkeypatch.setattr(commands, "_switch_claude_account", lambda name: (_ for _ in ()).throw(AssertionError))

    async def fake_usage(cmd, args, user_id, chat_id, store, bot=None):
        assert cmd == "usage"
        return {"text": "📈 **Antigravity CLI 用量**", "buttons": [
            {"text": "acct", "value": {"action": "switch_usage", "name": "acct", "cid": chat_id}},
        ]}

    real = commands.handle_command

    async def route(cmd, args, user_id, chat_id, store, bot=None):
        if cmd == "usage":
            return await fake_usage(cmd, args, user_id, chat_id, store, bot)
        return await real(cmd, args, user_id, chat_id, store, bot=bot)

    monkeypatch.setattr(commands, "handle_command", route)
    monkeypatch.setattr(server_stats, "get_report", lambda compact=False: "server")
    out = asyncio.run(route("all", "switch acct", "ou_user", "oc_chat", _Store("agy")))
    assert switched == ["acct"]
    # 没有刷新按钮的 /usage 也会补一个 /all 刷新
    assert [b["value"]["cmd"] for b in out["buttons"]] == ["/all switch acct", "/all"]


def test_all_without_chat_is_plain_text(monkeypatch):
    monkeypatch.setattr(commands, "_get_usage", lambda chat_id, compact=False: "usage text")
    monkeypatch.setattr(server_stats, "get_report", lambda compact=False: "server text")
    out = asyncio.run(commands.handle_command("all", "", "ou_user", "", _Store()))
    assert out == "usage text\n\nserver text"


def test_all_registration_and_toast():
    assert "all" in commands.BOT_COMMANDS
    assert commands.is_lock_free_command("all", "")
    assert not commands.is_lock_free_command("all", "switch token2")
    assert commands.run_cmd_toast("/all") == "正在刷新…"
    assert commands.run_cmd_toast("/all switch token2") == "正在切换到 token2…"
    assert commands.run_cmd_toast("/usage") == "正在刷新用量…"
    assert commands.run_cmd_toast("/server") == "/server"
    assert "`/all`" in commands.HELP_TEXT


# ── 飞书卡片排版 ──────────────────────────────────────────────

def _client_capturing_patches(monkeypatch):
    sent = []

    async def apatch(req):
        sent.append(json.loads(req.request_body.content))
        return SimpleNamespace(success=lambda: True)

    lc = lark.Client.builder().app_id("x").app_secret("y").build()
    client = FeishuClient(lc, app_id="cli_app_123456", app_secret="y", label="spx")
    client.client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(apatch=apatch))))
    client._FINAL_CONFIRM_DELAY = 0
    monkeypatch.setattr(fc, "_use_v1_card", lambda: False)
    return client, sent


def test_card_layout_corner_title_buttons_then_after(monkeypatch):
    client, sent = _client_capturing_patches(monkeypatch)
    buttons = [{"text": "🔄 刷新", "value": {"action": "run_cmd", "cmd": "/all", "_cc_uid": "ou_user"}}]
    asyncio.run(client.update_card_with_buttons(
        "om_card", "📊 **Claude 用量**\n\n剩余 **43%**", buttons, flow=True,
        corner="11-04 11:00:00 刷新", after="🖥️ **服务器状态**"))

    els = sent[-1]["body"]["elements"]
    head = els[0]
    assert head["tag"] == "column_set"
    left, right = head["columns"]
    assert left["elements"][0]["content"] == "📊 **Claude 用量**"
    assert right["elements"][0]["text_align"] == "right"
    assert "11-04 11:00:00 刷新" in right["elements"][0]["content"]
    assert els[1] == {"tag": "markdown", "content": "剩余 **43%**"}
    assert els[2]["tag"] == "column_set" and els[2]["columns"][0]["elements"][0]["tag"] == "button"
    assert els[3] == {"tag": "markdown", "content": "🖥️ **服务器状态**"}
    assert "服务器状态" in client._card_text_cache["om_card"]


def test_card_layout_unchanged_without_extras(monkeypatch):
    client, sent = _client_capturing_patches(monkeypatch)
    buttons = [{"text": "🔄 刷新", "value": {"action": "run_cmd", "cmd": "/usage", "_cc_uid": "ou_user"}}]
    asyncio.run(client.update_card_with_buttons("om_card", "line1\nline2", buttons, flow=True))
    els = sent[-1]["body"]["elements"]
    assert els[0] == {"tag": "markdown", "content": "line1\nline2"}
    assert len(els) == 2
