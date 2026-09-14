"""`/usage` 卡片底部账号按钮（switch_usage）按当前 runner 分流。

Claude runner 点了切 Claude Code 账户，agy runner 点了切 Antigravity 的 Google 号，
两边都在原卡片上重渲染一份对应 runner 的 /usage。
"""

import base64
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands
import dispatcher


class _FakeFeishu:
    def __init__(self):
        self.updates = []

    async def update_card_with_buttons(self, msg_id, text, buttons, flow=False):
        self.updates.append({"msg_id": msg_id, "text": text, "buttons": buttons})

    async def update_card(self, msg_id, text):
        self.updates.append({"msg_id": msg_id, "text": text, "buttons": []})


class _FakeStore:
    def __init__(self, runner):
        self.runner = runner
        self.default_model = "gemini-3.8-flash"

    async def get_current_raw(self, _user_id, _chat_id):
        return {"runner": self.runner, "session_id": "sid_1"}


def _fake_bot(runner):
    return SimpleNamespace(
        store=_FakeStore(runner),
        feishu=_FakeFeishu(),
        profile=SimpleNamespace(name="spx", runner=runner),
    )


def _seed_two_agy_accounts(monkeypatch, tmp_path):
    import agy_account_switcher as aas

    monkeypatch.setattr(aas, "ACCOUNTS_DIR", str(tmp_path / "agy"))
    os.makedirs(aas.ACCOUNTS_DIR, exist_ok=True)
    blobs = {}
    for name, email in (("cactrinh383", "cactrinh383@gmail.com"),
                        ("luyixin75", "luyixin75@gmail.com")):
        claims = base64.urlsafe_b64encode(
            json.dumps({"email": email}).encode()).decode().rstrip("=")
        blob = {
            "token": {"access_token": f"ya29.{name}", "refresh_token": f"1//{name}",
                      "expiry": "2026-09-13T01:34:56.1+08:00"},
            "auth_method": "consumer",
            "id_token": f"h.{claims}.s",
        }
        blobs[name] = blob
        with open(os.path.join(aas.ACCOUNTS_DIR, f"{name}.json"), "w") as f:
            json.dump(blob, f)
    state = {"raw": aas.encode_raw(blobs["luyixin75"])}
    monkeypatch.setattr(aas, "_read_keychain_raw", lambda: state["raw"])
    return state


@pytest.mark.asyncio
async def test_switch_usage_under_agy_runner_switches_agy_account(monkeypatch, tmp_path):
    _seed_two_agy_accounts(monkeypatch, tmp_path)
    monkeypatch.setattr(commands, "_fetch_agy_quota", lambda: [])
    monkeypatch.setattr(commands, "_format_context_line", lambda *a, **k: "上下文：1k/1M")
    switched = []
    monkeypatch.setattr(commands, "_switch_agy_account",
                        lambda name: switched.append(name) or f"✅ agy 账号已切换：{name}")
    monkeypatch.setattr(commands, "_switch_claude_account",
                        lambda _n: pytest.fail("agy runner 不该动 Claude 凭证"))

    bot = _fake_bot("agy")
    await dispatcher.handle_switch_usage(bot, "ou_user", "oc_chat", "cactrinh383", "om_card")

    assert switched == ["cactrinh383"]
    update = bot.feishu.updates[-1]
    assert update["msg_id"] == "om_card"
    # 切换结果 headline 在顶，下面是重渲染出来的 agy /usage
    assert update["text"].startswith("✅ agy 账号已切换：cactrinh383")
    assert "Antigravity CLI 用量" in update["text"]
    # 按钮保留，且仍是 agy 的账号按钮
    assert [b["value"]["action"] for b in update["buttons"]] == [
        "switch_usage", "switch_usage", "run_cmd",
    ]
    assert {b["value"].get("name") for b in update["buttons"][:2]} == {
        "cactrinh383", "luyixin75"}


@pytest.mark.asyncio
async def test_switch_usage_under_claude_runner_still_switches_claude(monkeypatch):
    monkeypatch.setattr(commands, "_switch_claude_account",
                        lambda name: f"✅ Claude Code 账户已切换为 `{name}`。")
    monkeypatch.setattr(commands, "_switch_agy_account",
                        lambda _n: pytest.fail("claude runner 不该动 agy 凭证"))
    monkeypatch.setattr(commands, "_get_usage",
                        lambda chat_id: {"text": "📊 Claude Max 用量", "buttons": []})

    bot = _fake_bot("claude")
    await dispatcher.handle_switch_usage(bot, "ou_user", "oc_chat", "info", "om_card")

    update = bot.feishu.updates[-1]
    assert update["text"].startswith("✅ Claude Code 账户已切换为 `info`。")
    assert "Claude Max 用量" in update["text"]
