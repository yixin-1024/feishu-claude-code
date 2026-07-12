from types import SimpleNamespace

import pytest

import commands


class _Store:
    def __init__(self, runner: str = "claude"):
        self.runner = runner

    async def get_current(self, _user_id: str, _chat_id: str):
        return SimpleNamespace(runner=self.runner)


@pytest.mark.asyncio
async def test_switch_success_for_claude_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(
        commands,
        "_switch_claude_account",
        lambda name: calls.append(name) or f"✅ switched {name}",
    )

    reply = await commands.handle_command(
        "switch", "info", "ou_user", "oc_chat", _Store("claude")
    )

    assert calls == ["info"]
    assert "✅" in reply and "info" in reply


@pytest.mark.asyncio
async def test_switch_without_args_shows_picker_without_switching(monkeypatch):
    monkeypatch.setattr(
        commands,
        "_switch_claude_account",
        lambda _name: pytest.fail("no-arg /switch must not mutate credentials"),
    )
    monkeypatch.setattr(
        commands,
        "_get_account_switch_picker",
        lambda chat_id: {"text": "picker", "buttons": [{"chat_id": chat_id}]},
    )

    reply = await commands.handle_command(
        "switch", "", "ou_user", "oc_chat", _Store("claude")
    )

    assert reply["text"] == "picker"
    assert reply["buttons"][0]["chat_id"] == "oc_chat"


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", ["codex", "opencode", "mimo"])
async def test_switch_rejected_for_non_claude_runner(monkeypatch, runner):
    monkeypatch.setattr(
        commands,
        "_switch_claude_account",
        lambda _name: pytest.fail("non-Claude runner must not switch Claude credentials"),
    )

    reply = await commands.handle_command(
        "switch", "info", "ou_user", "oc_chat", _Store(runner)
    )

    assert "只用于 Claude Code" in reply
    assert f"`{runner}`" in reply


def test_switch_helper_reports_unknown_account(monkeypatch):
    import account_switcher as accs

    monkeypatch.setattr(
        accs,
        "switch_account_manually",
        lambda name: (False, f"no saved account '{name}'"),
    )
    monkeypatch.setattr(accs, "list_account_files", lambda: ["info", "mar", "spx"])

    reply = commands._switch_claude_account("ghost")

    assert reply.startswith("❌")
    assert "ghost" in reply
    assert "`info`" in reply and "`spx`" in reply


def test_switch_helper_preserves_identity_warning(monkeypatch):
    import account_switcher as accs

    monkeypatch.setattr(
        accs,
        "switch_account_manually",
        lambda _name: (True, "switched to info (identity missing)"),
    )
    monkeypatch.setattr(accs, "list_account_files", lambda: ["info"])
    monkeypatch.setattr(accs, "manual_switch_hold_seconds", lambda: 1800)

    reply = commands._switch_claude_account("info")

    assert reply.startswith("✅")
    assert "正在运行的任务不受影响" in reply
    assert "30 分钟" in reply
    assert "⚠️" in reply and "identity" in reply


def test_switch_is_registered_and_documented():
    assert "switch" in commands.BOT_COMMANDS
    assert "/switch <账户>" in commands.HELP_TEXT
