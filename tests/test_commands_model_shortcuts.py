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
    MODEL_SHORTCUT_TARGETS,
    MODEL_SHORTCUTS,
    ModelShortcutUnavailable,
    apply_model_shortcut,
    parse_command,
)
from session_store import SessionStore


_DEFAULT_MODELS = {"claude": "claude-sonnet-4-6", "agy": "gemini-4-neon"}


def _store(tmp_path, runner: str = "claude") -> SessionStore:
    return SessionStore(
        profile="spx",
        default_cwd=str(tmp_path),
        default_runner=runner,
        default_model=_DEFAULT_MODELS.get(runner, "gpt-5.1-codex"),
    )


def test_shortcuts_registered_aliased_and_documented():
    assert MODEL_SHORTCUTS == ("fable", "opus", "sonnet", "haiku", "flash")
    for cmd in MODEL_SHORTCUTS:
        assert cmd in commands.BOT_COMMANDS, cmd
        assert cmd in MODEL_ALIASES, cmd
        # 切模型要写 session，不能被当成免排队的只读命令
        assert not commands.is_lock_free_command(cmd, ""), cmd
    assert "`/fable` `/opus` `/sonnet` `/haiku`" in commands.HELP_TEXT
    assert "直接执行后面的指令" in commands.HELP_TEXT
    assert "`/flash`" in commands.HELP_TEXT and "仅 agy runner 可用" in commands.HELP_TEXT


def test_shortcut_targets_are_valid_models_for_their_runner():
    from bot_config import is_model_compatible_with_runner

    assert MODEL_SHORTCUT_TARGETS["flash"] == {"agy": "gemini-3.8-flash"}
    for cmd, targets in MODEL_SHORTCUT_TARGETS.items():
        for runner, model in targets.items():
            assert is_model_compatible_with_runner(model, runner), (cmd, runner, model)


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


# ── `/flash`：agy 上的同款快捷指令，切到 Gemini 3.8 Flash ──────────────────

async def test_flash_on_agy_switches_to_38_flash_and_keeps_session(tmp_path):
    store = _store(tmp_path, runner="agy")
    await store.on_claude_response("ou_u", "oc_c", "agy-conv-1", "hello")

    model = await apply_model_shortcut("flash", "ou_u", "oc_c", store)

    assert model == "gemini-3.8-flash"
    cur = await store.get_current("ou_u", "oc_c")
    assert cur.model == "gemini-3.8-flash"
    assert cur.session_id == "agy-conv-1", "agy 续轮可换 --model，沿用当前会话"
    assert cur.runner == "agy"


async def test_flash_keeps_effort_override(tmp_path):
    """切模型不动推理强度；Neon 上设的 xhigh 交给 agy_runner 收敛到 3.8 Flash 的 high。"""
    store = _store(tmp_path, runner="agy")
    await store.set_effort("ou_u", "oc_c", "xhigh")

    await apply_model_shortcut("flash", "ou_u", "oc_c", store)

    cur = await store.get_current("ou_u", "oc_c")
    assert cur.effort == "xhigh"
    from agy_runner import _resolve_model_effort
    assert _resolve_model_effort(cur.model, cur.effort) == ("gemini-3.8-flash", "high")


@pytest.mark.parametrize("runner", ["claude", "codex", "pi"])
async def test_flash_refused_off_agy_without_side_effects(tmp_path, runner):
    store = _store(tmp_path, runner=runner)
    await store.on_claude_response("ou_u", "oc_c", "sid-x", "hello")
    before = (await store.get_current("ou_u", "oc_c")).model

    with pytest.raises(ModelShortcutUnavailable) as exc:
        await apply_model_shortcut("flash", "ou_u", "oc_c", store)

    msg = str(exc.value)
    assert "agy runner" in msg and f"`{runner}`" in msg and "没有执行" in msg
    cur = await store.get_current("ou_u", "oc_c")
    assert cur.model == before
    assert cur.session_id == "sid-x"
    assert (await store.get_current_raw("ou_u", "oc_c"))["model_override"] is None


async def test_claude_shortcut_still_refused_on_agy(tmp_path):
    store = _store(tmp_path, runner="agy")

    with pytest.raises(ModelShortcutUnavailable) as exc:
        await apply_model_shortcut("opus", "ou_u", "oc_c", store)

    assert "claude runner" in str(exc.value) and "`agy`" in str(exc.value)
    assert (await store.get_current_raw("ou_u", "oc_c"))["model_override"] is None


async def test_bare_flash_via_handle_command(tmp_path):
    store = _store(tmp_path, runner="agy")
    await store.on_claude_response("ou_u", "oc_c", "agy-conv-1", "hi")

    reply = await commands.handle_command("flash", "", "ou_u", "oc_c", store, bot=None)

    assert reply.startswith("✅") and "gemini-3.8-flash" in reply and "沿用当前 session" in reply
    assert "`/flash <指令>`" in reply
    cur = await store.get_current("ou_u", "oc_c")
    assert cur.model == "gemini-3.8-flash"
    assert cur.session_id == "agy-conv-1"
