"""`/fable` `/opus` `/sonnet` `/haiku` 快捷指令：一条消息完成「切模型 + 执行」。

- 带指令：dispatcher 拦截 → 切模型 → 指令当普通消息继续跑（不回"已切换"卡片）
- 裸命令：handle_command 只切模型
- 沿用当前 session（Claude CLI `--resume` 可换 `--model`）
- runner 由 profile 钉死切不了：非 claude runner 明确拒绝、指令不执行
- `/model X` 老语义不变：仍重开 session
"""

import pytest

import commands
from commands import (
    MODEL_ALIASES,
    MODEL_SHORTCUTS,
    ModelShortcutUnavailable,
    apply_model_shortcut,
    parse_command,
)
from session_store import SessionStore


def _store(tmp_path, runner: str = "claude") -> SessionStore:
    return SessionStore(
        profile="spx",
        default_cwd=str(tmp_path),
        default_runner=runner,
        default_model="claude-sonnet-4-6" if runner == "claude" else "gpt-5.1-codex",
    )


def test_shortcuts_registered_aliased_and_documented():
    assert MODEL_SHORTCUTS == ("fable", "opus", "sonnet", "haiku")
    for cmd in MODEL_SHORTCUTS:
        assert cmd in commands.BOT_COMMANDS, cmd
        assert cmd in MODEL_ALIASES, cmd
    assert "`/fable` `/opus` `/sonnet` `/haiku`" in commands.HELP_TEXT
    assert "直接执行后面的指令" in commands.HELP_TEXT


def test_parse_shortcut_keeps_trailing_prompt_intact():
    assert parse_command("/fable 帮我查一下数据库 xxx") == ("fable", "帮我查一下数据库 xxx")
    # 大小写不敏感、多行指令整段保留
    assert parse_command("/Opus\n第一行\n第二行") == ("opus", "第一行\n第二行")
    # 裸命令
    assert parse_command("/haiku") == ("haiku", "")
    assert parse_command("  /sonnet   ") == ("sonnet", "")


async def test_shortcut_on_claude_runner_keeps_session(tmp_path):
    store = _store(tmp_path)
    await store.on_claude_response("ou_u", "oc_c", "sid-123", "hello")

    model = await apply_model_shortcut("fable", "ou_u", "oc_c", store)

    assert model == "fable[1m]"
    cur = await store.get_current("ou_u", "oc_c")
    assert cur.session_id == "sid-123", "沿用当前 session，不重开"
    assert cur.model == "fable[1m]"
    assert cur.runner == "claude"


async def test_shortcut_on_non_claude_runner_refuses_without_side_effects(tmp_path):
    store = _store(tmp_path, runner="codex")
    await store.on_claude_response("ou_u", "oc_c", "codex-sid", "hello")

    with pytest.raises(ModelShortcutUnavailable) as exc:
        await apply_model_shortcut("opus", "ou_u", "oc_c", store)

    assert "codex" in str(exc.value)
    assert "没有执行" in str(exc.value)
    cur = await store.get_current("ou_u", "oc_c")
    assert cur.runner == "codex"
    assert cur.session_id == "codex-sid", "拒绝时不能动 session"
    assert cur.model == "gpt-5.1-codex", "拒绝时不能留下 Claude 模型 override"


async def test_shortcut_rejects_non_shortcut_command(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        await apply_model_shortcut("model", "ou_u", "oc_c", store)


async def test_bare_shortcut_via_handle_command_only_switches_model(tmp_path):
    store = _store(tmp_path)
    await store.on_claude_response("ou_u", "oc_c", "sid-1", "hi")

    reply = await commands.handle_command("sonnet", "", "ou_u", "oc_c", store, bot=None)

    assert isinstance(reply, str)
    assert "sonnet[1m]" in reply
    assert "沿用当前 session" in reply
    cur = await store.get_current("ou_u", "oc_c")
    assert cur.session_id == "sid-1"
    assert cur.model == "sonnet[1m]"


async def test_bare_shortcut_on_codex_runner_returns_error_text(tmp_path):
    store = _store(tmp_path, runner="codex")

    reply = await commands.handle_command("haiku", "", "ou_u", "oc_c", store, bot=None)

    assert reply.startswith("❌")
    assert "claude runner" in reply
    assert "codex" in reply
    cur = await store.get_current("ou_u", "oc_c")
    assert cur.runner == "codex"
    assert cur.model == "gpt-5.1-codex"


async def test_model_command_still_starts_new_session(tmp_path):
    """老 `/model X` 语义不变：换模型 + 重开 session。"""
    store = _store(tmp_path)
    await store.on_claude_response("ou_u", "oc_c", "sid-1", "hi")

    reply = await commands.handle_command("model", "opus", "ou_u", "oc_c", store, bot=None)

    assert "已开始新 session" in reply
    cur = await store.get_current("ou_u", "oc_c")
    assert cur.session_id is None
    assert cur.model == "opus[1m]"
