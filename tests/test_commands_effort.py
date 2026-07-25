from types import SimpleNamespace

import pytest

import commands
from bot_config import CLAUDE_EFFORT_LEVELS, CODEX_EFFORT_LEVELS
from session_store import SessionStore


def _bot(runner: str):
    return SimpleNamespace(
        profile=SimpleNamespace(
            name="regtank",
            runner=runner,
            default_model=(
                "gpt-5.6-sol" if runner == "codex" else "claude-sonnet-4-6"
            ),
            claude_env_file="",
        )
    )


def _store(tmp_path, runner: str) -> SessionStore:
    return SessionStore(
        profile="regtank",
        default_cwd=str(tmp_path),
        default_runner=runner,
        default_model=(
            "gpt-5.6-sol" if runner == "codex" else "claude-sonnet-4-6"
        ),
    )


def _button_commands(reply: dict) -> list[str]:
    return [button["value"]["cmd"] for button in reply["buttons"]]


def test_effort_is_registered_and_documented():
    assert "effort" in commands.BOT_COMMANDS
    assert "/effort [级别]" in commands.HELP_TEXT
    assert "default / low / medium" in commands.HELP_TEXT


@pytest.mark.asyncio
async def test_codex_effort_picker_has_all_supported_levels_and_chat_binding(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("REGTANK_CODEX_REASONING_EFFORT", "ultra")
    store = _store(tmp_path, "codex")

    reply = await commands.handle_command(
        "effort", "", "ou_user", "oc_chat", store, bot=_bot("codex")
    )

    assert "当前 runner：**codex**" in reply["text"]
    assert "当前推理强度：**ultra**（跟随默认）" in reply["text"]
    assert "profile 默认：**ultra**" in reply["text"]
    assert _button_commands(reply) == [
        f"/effort {level}" for level in CODEX_EFFORT_LEVELS
    ]
    assert all(button["value"]["cid"] == "oc_chat" for button in reply["buttons"])


@pytest.mark.asyncio
async def test_claude_effort_picker_excludes_codex_only_ultra(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_EFFORT", raising=False)
    store = _store(tmp_path, "claude")

    reply = await commands.handle_command(
        "effort", "", "ou_user", "oc_chat", store, bot=_bot("claude")
    )

    assert "当前 runner：**claude**" in reply["text"]
    assert "profile 默认：**CLI 默认**" in reply["text"]
    assert _button_commands(reply) == [
        f"/effort {level}" for level in CLAUDE_EFFORT_LEVELS
    ]
    assert "/effort ultra" not in _button_commands(reply)


@pytest.mark.asyncio
async def test_text_effort_set_and_default_keep_existing_session(tmp_path, monkeypatch):
    monkeypatch.setenv("REGTANK_CODEX_REASONING_EFFORT", "ultra")
    store = _store(tmp_path, "codex")
    raw = await store.get_current_raw("ou_user", "oc_chat")
    raw["session_id"] = "sid_keep"
    await store._save_async()

    set_reply = await commands.handle_command(
        "effort", "HIGH", "ou_user", "oc_chat", store, bot=_bot("codex")
    )
    after_set = await store.get_current_raw("ou_user", "oc_chat")

    assert "推理强度已设为 `high`" in set_reply
    assert "session 保持不变" in set_reply
    assert after_set["effort_override"] == "high"
    assert after_set["session_id"] == "sid_keep"

    picker = await commands.handle_command(
        "effort", "", "ou_user", "oc_chat", store, bot=_bot("codex")
    )
    assert "/effort default" in _button_commands(picker)

    default_reply = await commands.handle_command(
        "effort", "default", "ou_user", "oc_chat", store, bot=_bot("codex")
    )
    after_default = await store.get_current_raw("ou_user", "oc_chat")

    assert "跟随 profile 默认 `ultra`" in default_reply
    assert after_default["effort_override"] is None
    assert after_default["session_id"] == "sid_keep"


@pytest.mark.asyncio
async def test_invalid_effort_does_not_change_override_or_session(tmp_path):
    store = _store(tmp_path, "codex")
    raw = await store.get_current_raw("ou_user", "oc_chat")
    raw["session_id"] = "sid_keep"
    raw["effort_override"] = "medium"
    await store._save_async()

    reply = await commands.handle_command(
        "effort", "impossible", "ou_user", "oc_chat", store, bot=_bot("codex")
    )
    current = await store.get_current_raw("ou_user", "oc_chat")

    assert "不支持推理强度 `impossible`" in reply
    assert "`default`" in reply
    assert current["effort_override"] == "medium"
    assert current["session_id"] == "sid_keep"


@pytest.mark.asyncio
async def test_codex_picker_and_validation_follow_current_model_catalog(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, "codex")
    monkeypatch.setattr(commands, "_codex_effort_levels", lambda _model: ("low", "high"))

    picker = await commands.handle_command(
        "effort", "", "ou_user", "oc_chat", store, bot=_bot("codex")
    )
    rejected = await commands.handle_command(
        "effort", "ultra", "ou_user", "oc_chat", store, bot=_bot("codex")
    )

    assert _button_commands(picker) == ["/effort low", "/effort high"]
    assert "可用档位受当前模型能力限制" in picker["text"]
    assert "不支持推理强度 `ultra`" in rejected
    assert (await store.get_current("ou_user", "oc_chat")).effort is None


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", ["opencode", "mimo"])
async def test_effort_rejects_unsupported_runners(tmp_path, runner):
    store = _store(tmp_path, runner)

    reply = await commands.handle_command(
        "effort", "low", "ou_user", "oc_chat", store, bot=_bot(runner)
    )

    assert f"当前 runner `{runner}` 暂不支持 `/effort`" in reply
    assert (await store.get_current_raw("ou_user", "oc_chat"))["effort_override"] is None


@pytest.mark.asyncio
async def test_status_shows_profile_default_and_conversation_override(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("REGTANK_CODEX_REASONING_EFFORT", "ultra")
    monkeypatch.setattr(commands, "_format_codex_rate_line", lambda _sid=None: "")
    store = _store(tmp_path, "codex")

    default_status = await commands.handle_command(
        "status", "", "ou_user", "oc_chat", store, bot=_bot("codex")
    )
    assert "推理强度: `ultra`（跟随默认）" in default_status

    await store.set_effort("ou_user", "oc_chat", "low")
    override_status = await commands.handle_command(
        "status", "", "ou_user", "oc_chat", store, bot=_bot("codex")
    )
    assert "推理强度: `low`（当前对话覆盖）" in override_status


@pytest.mark.asyncio
async def test_status_omits_effort_for_unsupported_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, "_format_context_line", lambda *_args, **_kwargs: "")
    store = _store(tmp_path, "opencode")

    status = await commands.handle_command(
        "status", "", "ou_user", "oc_chat", store, bot=_bot("opencode")
    )

    assert "Runner: `opencode`" in status
    assert "推理强度:" not in status
