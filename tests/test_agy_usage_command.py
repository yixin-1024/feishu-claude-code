"""/usage 对 agy 的支持：解析 `agy -p /usage` 的 tab 分隔输出并渲染。

agy 报的是**剩余**百分比（不是已用），窗口有 Weekly + Five Hour 两层，
且模型分成 Gemini / Claude+GPT 两个独立池子。
"""

import base64
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands
from commands import _agy_iso_to_unix, _agy_usage_bar_lines, _fetch_agy_quota

REAL_OUTPUT = (
    "Gemini Models\tWeekly Limit Remaining\t100%\t2026-09-14T04:41:43Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t87.5%\t2026-09-07T09:41:43Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-09-14T04:57:53Z\n"
    "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-09-07T09:57:53Z\n"
)


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _patch_run(monkeypatch, proc_or_exc):
    def fake_run(*a, **k):
        if isinstance(proc_or_exc, Exception):
            raise proc_or_exc
        return proc_or_exc
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_fetch_agy_quota_parses_four_windows(monkeypatch):
    _patch_run(monkeypatch, FakeProc(REAL_OUTPUT))
    rows = _fetch_agy_quota()
    assert len(rows) == 4
    assert rows[0] == {
        "group": "Gemini Models",
        "window": "Weekly Limit Remaining",
        "remaining_pct": 100.0,
        "resets_at": "2026-09-14T04:41:43Z",
    }
    assert rows[1]["remaining_pct"] == 87.5


def test_fetch_agy_quota_skips_noise_lines(monkeypatch):
    noisy = (
        "Welcome to Antigravity CLI\n"
        "\n"
        "Gemini Models\tWeekly Limit Remaining\t42%\t2026-09-14T04:41:43Z\n"
        "some\tgarbage\tnot-a-percent\tx\n"
        "Gemini Models\tFive Hour Limit Remaining\tNaN%\t2026-09-07T09:41:43Z\n"
        "Gemini Models\tFive Hour Limit Remaining\t9%\n"  # 缺重置时刻也要收
    )
    _patch_run(monkeypatch, FakeProc(noisy))
    rows = _fetch_agy_quota()
    assert [r["remaining_pct"] for r in rows] == [42.0, 9.0]
    assert rows[1]["resets_at"] == ""


def test_fetch_agy_quota_survives_timeout(monkeypatch):
    # API key 模式 / 未登录 / agy 挂住，都必须降级成空列表而不是抛异常
    _patch_run(monkeypatch, subprocess.TimeoutExpired(cmd="agy", timeout=1))
    assert _fetch_agy_quota() == []
    _patch_run(monkeypatch, OSError("no such binary"))
    assert _fetch_agy_quota() == []
    _patch_run(monkeypatch, FakeProc(""))
    assert _fetch_agy_quota() == []


def test_agy_iso_to_unix():
    # ISO8601 UTC（Z 结尾）→ unix 秒
    assert _agy_iso_to_unix("2026-09-14T04:41:43Z") == 1789360903  # 2026-09-14T04:41:43Z
    assert _agy_iso_to_unix("") is None
    assert _agy_iso_to_unix("not-a-time") is None


def test_fetch_agy_quota_clamps_out_of_range(monkeypatch):
    _patch_run(monkeypatch, FakeProc(
        "G\tWeekly Limit Remaining\t101.5%\t2026-09-14T04:41:43Z\n"
        "G\tFive Hour Limit Remaining\t-3%\t2026-09-07T09:41:43Z\n"
    ))
    assert [r["remaining_pct"] for r in _fetch_agy_quota()] == [100.0, 0.0]


def test_agy_usage_bar_lines_groups_and_shows_remaining(monkeypatch):
    _patch_run(monkeypatch, FakeProc(REAL_OUTPUT))
    lines = _agy_usage_bar_lines(_fetch_agy_quota())
    text = "\n".join(lines)
    # 两个池子各自分节，且保持 agy 的出现顺序
    assert text.index("**Gemini Models**") < text.index("**Claude and GPT models**")
    # 两层窗口都要在
    assert "Weekly 剩余" in text and "Five Hour 剩余" in text
    # 剩余语义：87.5% 的条不能是满格
    assert "87.5%" in text
    bar_87 = next(l for l in lines if "87.5%" in l)
    assert "░" in bar_87
    bar_100 = next(l for l in lines if "100.0%" in l)
    assert "░" not in bar_100


def test_agy_usage_bar_lines_empty_when_no_rows():
    assert _agy_usage_bar_lines([]) == []


class _DummyStore:
    def __init__(self, runner="agy", model="gemini-3.8-flash"):
        self.runner = runner
        self.default_model = model

    async def get_current_raw(self, user_id, chat_id):
        return {"runner": self.runner, "model_override": self.default_model, "session_id": "sid_1"}


def _seed_agy_accounts(monkeypatch, tmp_path, accounts, current=None):
    """把 agy 账号快照目录隔离到 tmp 并塞几个号，返回内存 keychain state。

    accounts: [(name, email)]；current: 其中哪个是 keychain 里当前登录的号。
    不隔离的话这些用例会读到本机真实的 ~/.gemini/accounts。
    """
    import agy_account_switcher as aas

    monkeypatch.setattr(aas, "ACCOUNTS_DIR", str(tmp_path))
    state = {"raw": None}
    for name, email in accounts:
        payload = base64.urlsafe_b64encode(
            json.dumps({"email": email}).encode()
        ).decode().rstrip("=")
        blob = {
            "token": {"access_token": f"ya29.{name}", "refresh_token": f"1//{name}",
                      "expiry": "2026-09-13T01:34:56.123456789+08:00"},
            "auth_method": "consumer",
            "id_token": f"h.{payload}.s",
        }
        (tmp_path / f"{name}.json").write_text(json.dumps(blob))
        if name == current:
            state["raw"] = aas.encode_raw(blob)
    monkeypatch.setattr(aas, "_read_keychain_raw", lambda: state["raw"])
    return state


@pytest.mark.asyncio
async def test_agy_usage_command_with_chat_id_appends_refresh_button(monkeypatch, tmp_path):
    """agy runner 执行 /usage 时，带 chat_id 应返回带刷新按钮的卡片 dict。"""
    _seed_agy_accounts(monkeypatch, tmp_path, [])
    monkeypatch.setattr(commands, "_fetch_agy_quota", lambda: [])
    monkeypatch.setattr(commands, "_format_context_line", lambda *a, **k: "上下文：1k/1M")
    store = _DummyStore(runner="agy")
    reply = await commands.handle_command("usage", "", "ou_user", "oc_test_chat", store)
    assert isinstance(reply, dict)
    assert "Antigravity CLI 用量" in reply["text"]
    assert reply["buttons"] == [{
        "text": "🔄 刷新",
        "value": {"action": "run_cmd", "cmd": "/usage", "cid": "oc_test_chat"},
    }]
    # 没号可切时不要出那行没用的引导
    assert "点账号按钮" not in reply["text"]


@pytest.mark.asyncio
async def test_agy_usage_shows_account_switch_buttons(monkeypatch, tmp_path):
    """存了多个 agy 号时，/usage 底部要像 Claude 版一样给切号按钮 + 刷新。"""
    _seed_agy_accounts(
        monkeypatch, tmp_path,
        [("cactrinh383", "cactrinh383@gmail.com"), ("luyixin75", "luyixin75@gmail.com")],
        current="luyixin75",
    )
    monkeypatch.setattr(commands, "_fetch_agy_quota", lambda: [])
    monkeypatch.setattr(commands, "_format_context_line", lambda *a, **k: "上下文：1k/1M")
    store = _DummyStore(runner="agy")

    reply = await commands.handle_command("usage", "", "ou_user", "oc_test_chat", store)

    assert reply["text"].startswith("📈 **Antigravity CLI 用量** — 当前 `luyixin75`")
    assert "👇 点账号按钮切换 Antigravity 账号" in reply["text"]
    assert reply["buttons"] == [
        {"text": "cactrinh383",
         "value": {"action": "switch_usage", "name": "cactrinh383", "cid": "oc_test_chat"}},
        {"text": "● luyixin75",
         "value": {"action": "switch_usage", "name": "luyixin75", "cid": "oc_test_chat"}},
        {"text": "🔄 刷新",
         "value": {"action": "run_cmd", "cmd": "/usage", "cid": "oc_test_chat"}},
    ]


@pytest.mark.asyncio
async def test_agy_usage_single_account_has_no_switch_button(monkeypatch, tmp_path):
    """只有一个号时不给无意义的切换按钮，但标题仍标出当前号。"""
    _seed_agy_accounts(monkeypatch, tmp_path,
                       [("solo", "solo@gmail.com")], current="solo")
    monkeypatch.setattr(commands, "_fetch_agy_quota", lambda: [])
    monkeypatch.setattr(commands, "_format_context_line", lambda *a, **k: "上下文：1k/1M")
    store = _DummyStore(runner="agy")

    reply = await commands.handle_command("usage", "", "ou_user", "oc_test_chat", store)

    assert "— 当前 `solo`" in reply["text"]
    assert [b["value"]["action"] for b in reply["buttons"]] == ["run_cmd"]
    assert "点账号按钮" not in reply["text"]


@pytest.mark.asyncio
async def test_agy_usage_command_without_chat_id_returns_string(monkeypatch, tmp_path):
    """不传 chat_id 时保持纯文本返回。"""
    _seed_agy_accounts(monkeypatch, tmp_path, [("solo", "solo@gmail.com")], current="solo")
    monkeypatch.setattr(commands, "_fetch_agy_quota", lambda: [])
    monkeypatch.setattr(commands, "_format_context_line", lambda *a, **k: "上下文：1k/1M")
    store = _DummyStore(runner="agy")
    reply = await commands.handle_command("usage", "", "ou_user", "", store)
    assert isinstance(reply, str)
    assert "Antigravity CLI 用量" in reply


@pytest.mark.asyncio
async def test_codex_usage_command_with_chat_id_appends_refresh_button(monkeypatch):
    """codex runner 执行 /usage 时，带 chat_id 应返回带刷新按钮的卡片 dict。"""
    monkeypatch.setattr(commands, "_get_codex_rate_limits", lambda: {})
    monkeypatch.setattr(commands, "_format_context_line", lambda *a, **k: "上下文：5k/1M")
    store = _DummyStore(runner="codex", model="gpt-5.5")
    reply = await commands.handle_command("usage", "", "ou_user", "oc_test_chat", store)
    assert isinstance(reply, dict)
    assert "Codex 用量" in reply["text"]
    assert reply["buttons"] == [{
        "text": "🔄 刷新",
        "value": {"action": "run_cmd", "cmd": "/usage", "cid": "oc_test_chat"},
    }]
