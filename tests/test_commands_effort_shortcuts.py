"""`/low` `/medium` `/high` `/xhigh` `/max` 快捷指令：一条消息完成「切推理强度 + 执行」。

- 带指令：dispatcher 拦截 → 切档 → 指令当普通消息继续跑（见 test_integration）
- 裸命令：handle_command 只切档（= `/effort X`）
- 不动 session
- 当前 runner/模型没有这一档：明确拒绝、不留 override、指令不执行
"""

import pytest

import commands
from commands import (
    EFFORT_SHORTCUTS,
    EffortShortcutUnavailable,
    apply_effort_shortcut,
)
from session_store import SessionStore


def _store(tmp_path, runner: str = "claude", model: str = "claude-sonnet-4-6") -> SessionStore:
    return SessionStore(
        profile="spx",
        default_cwd=str(tmp_path),
        default_runner=runner,
        default_model=model,
    )


def test_effort_shortcuts_registered_and_documented():
    assert EFFORT_SHORTCUTS == ("low", "medium", "high", "xhigh", "max")
    for cmd in EFFORT_SHORTCUTS:
        assert cmd in commands.BOT_COMMANDS, cmd
        # 切档要写 session，不能被当成免排队的只读命令
        assert not commands.is_lock_free_command(cmd, ""), cmd
    assert "`/low` `/medium` `/high` `/xhigh` `/max`" in commands.HELP_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
async def test_effort_shortcut_on_claude_sets_override_and_keeps_session(tmp_path, level):
    store = _store(tmp_path)
    await store.on_claude_response("ou_u", "oc_c", "sid-123", "hello")

    applied = await apply_effort_shortcut(level, "ou_u", "oc_c", store)

    assert applied == level
    cur = await store.get_current("ou_u", "oc_c")
    assert cur.effort == level
    assert cur.session_id == "sid-123", "切档不重开 session"
    assert cur.model == "claude-sonnet-4-6", "切档不动模型"


@pytest.mark.asyncio
async def test_effort_shortcut_rejects_level_missing_on_runner(tmp_path):
    """agy 只有 low/medium/high：`/max` 要拒绝，不能静默按旧档位跑。"""
    store = _store(tmp_path, runner="agy", model="gemini-3.8-flash")
    await store.set_effort("ou_u", "oc_c", "low")

    with pytest.raises(EffortShortcutUnavailable) as exc:
        await apply_effort_shortcut("max", "ou_u", "oc_c", store)

    msg = str(exc.value)
    assert "agy" in msg and "没有执行" in msg and "`high`" in msg
    cur = await store.get_current("ou_u", "oc_c")
    assert cur.effort == "low", "拒绝时不能改掉原有档位"

    assert await apply_effort_shortcut("high", "ou_u", "oc_c", store) == "high"


@pytest.mark.asyncio
async def test_effort_shortcut_rejects_model_without_effort(tmp_path):
    store = _store(tmp_path, runner="agy", model="claude-sonnet-4-6")

    with pytest.raises(EffortShortcutUnavailable) as exc:
        await apply_effort_shortcut("high", "ou_u", "oc_c", store)

    msg = str(exc.value)
    assert "固定推理深度" in msg and "没有执行" in msg
    assert not msg.startswith("ℹ"), "前缀 emoji 由调用方统一加"
    assert (await store.get_current_raw("ou_u", "oc_c"))["effort_override"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", ["opencode", "mimo"])
async def test_effort_shortcut_rejects_runner_without_effort(tmp_path, runner):
    store = _store(tmp_path, runner=runner, model="whatever")

    with pytest.raises(EffortShortcutUnavailable) as exc:
        await apply_effort_shortcut("max", "ou_u", "oc_c", store)

    assert f"`{runner}` 暂不支持" in str(exc.value)
    assert (await store.get_current_raw("ou_u", "oc_c"))["effort_override"] is None


@pytest.mark.asyncio
async def test_effort_shortcut_rejects_non_shortcut_command(tmp_path):
    with pytest.raises(ValueError):
        await apply_effort_shortcut("ultra", "ou_u", "oc_c", _store(tmp_path))


@pytest.mark.asyncio
async def test_bare_effort_shortcut_via_handle_command(tmp_path):
    store = _store(tmp_path)
    await store.on_claude_response("ou_u", "oc_c", "sid-1", "hi")

    reply = await commands.handle_command("max", "", "ou_u", "oc_c", store, bot=None)

    assert isinstance(reply, str) and reply.startswith("✅")
    assert "`max`" in reply and "沿用当前 session" in reply
    cur = await store.get_current("ou_u", "oc_c")
    assert cur.effort == "max"
    assert cur.session_id == "sid-1"


@pytest.mark.asyncio
async def test_bare_effort_shortcut_refusal_is_error_text(tmp_path):
    store = _store(tmp_path, runner="agy", model="gemini-3.8-flash")

    reply = await commands.handle_command("xhigh", "", "ou_u", "oc_c", store, bot=None)

    assert reply.startswith("❌") and "xhigh" in reply
    assert (await store.get_current_raw("ou_u", "oc_c"))["effort_override"] is None
