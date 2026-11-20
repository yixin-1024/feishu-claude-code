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


AGY_SHORTCUTS = {
    "flash": "gemini-3.8-flash",
    "neon": "gemini-4-neon",
    "hydrogen": "gemini-4-hydrogen",
    "carbon": "gemini-4-carbon",
    "argon": "gemini-4-argon",
}


def test_shortcuts_registered_aliased_and_documented():
    assert MODEL_SHORTCUTS == ("fable", "opus", "sonnet", "haiku", *AGY_SHORTCUTS)
    for cmd in MODEL_SHORTCUTS:
        assert cmd in commands.BOT_COMMANDS, cmd
        # 切模型要写 session，不能被当成免排队的只读命令
        assert not commands.is_lock_free_command(cmd, ""), cmd
    assert "`/fable` `/opus` `/sonnet` `/haiku`" in commands.HELP_TEXT
    assert "直接执行后面的指令" in commands.HELP_TEXT
    assert "`/flash` `/neon` `/hydrogen` `/carbon` `/argon`" in commands.HELP_TEXT
    assert "仅 agy runner 可用" in commands.HELP_TEXT


def test_shortcut_targets_match_model_command_on_their_runner():
    """`/carbon` 和 `/model carbon` 在同一个 runner 上必须落到同一个模型。"""
    from bot_config import is_model_compatible_with_runner

    for cmd, model in AGY_SHORTCUTS.items():
        assert MODEL_SHORTCUT_TARGETS[cmd] == {"agy": model}, cmd
    for cmd, targets in MODEL_SHORTCUT_TARGETS.items():
        for runner, model in targets.items():
            assert is_model_compatible_with_runner(model, runner), (cmd, runner, model)
            assert commands.resolve_model_alias(cmd, runner) == model, (cmd, runner)


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


@pytest.mark.parametrize("cmd,model", list(AGY_SHORTCUTS.items()))
async def test_agy_shortcuts_switch_model_and_keep_session(tmp_path, cmd, model):
    store = _store(tmp_path, runner="agy")
    await store.on_claude_response("ou_u", "oc_c", "agy-conv-1", "hello")

    assert await apply_model_shortcut(cmd, "ou_u", "oc_c", store) == model

    cur = await store.get_current("ou_u", "oc_c")
    assert cur.model == model
    assert cur.session_id == "agy-conv-1"


@pytest.mark.parametrize("cmd", ["neon", "hydrogen", "carbon", "argon"])
async def test_gemini4_shortcuts_refused_on_claude(tmp_path, cmd):
    store = _store(tmp_path, runner="claude")

    reply = await commands.handle_command(cmd, "", "ou_u", "oc_c", store, bot=None)

    assert reply.startswith("❌") and "agy runner" in reply and "没有执行" in reply
    assert (await store.get_current_raw("ou_u", "oc_c"))["model_override"] is None


# ── `/model xxx`：后端专属名不用带前缀 ────────────────────────────────────

@pytest.mark.parametrize("name,model", [
    ("carbon", "gemini-4-carbon"),
    ("Neon", "gemini-4-neon"),
    ("hydrogen", "gemini-4-hydrogen"),
    ("argon", "gemini-4-argon"),
    ("flash", "gemini-3.8-flash"),
    ("37", "gemini-3.7-flash"),
    ("pro", "gemini-3.1-pro-high"),
    ("agy-carbon", "gemini-4-carbon"),          # 带前缀的老写法照样认
    ("gemini-4-argon", "gemini-4-argon"),       # 完整 id 原样
])
async def test_model_command_accepts_unprefixed_agy_names(tmp_path, name, model):
    store = _store(tmp_path, runner="agy")

    reply = await commands.handle_command("model", name, "ou_u", "oc_c", store, bot=None)

    assert reply.startswith("✅") and f"`{model}`" in reply, reply
    assert (await store.get_current("ou_u", "oc_c")).model == model


async def test_unprefixed_name_resolves_per_runner(tmp_path):
    from commands import resolve_model_alias

    # 同一个名字，在不同后端落到各自的模型
    assert resolve_model_alias("neon", "pi") == "google/gemini-4-neon"
    assert resolve_model_alias("neon", "agy") == "gemini-4-neon"
    # agy 的模型名不带 google/
    assert resolve_model_alias("flash", "agy") == "gemini-3.8-flash"
    assert resolve_model_alias("gemini38", "agy") == "gemini-3.8-flash"
    assert resolve_model_alias("flash", "pi") == "google/gemini-3.8-flash"
    # 通用表里已有的名字不被前缀版顶掉：agy 上 opus 仍是 Claude 的 opus（→ /model 拒绝）
    assert resolve_model_alias("opus", "agy") == "opus[1m]"
    assert resolve_model_alias("opus", "claude") == "opus[1m]"
    # 哪儿都没有：原样
    assert resolve_model_alias("gpt-5.5", "codex") == "gpt-5.5"
    assert resolve_model_alias("carbon", "") == "carbon"
    # Claude 系名字在 claude 上还是不兼容 agy 的（/model 会报不兼容，不会悄悄换模型）
    store = _store(tmp_path, runner="claude")
    reply = await commands.handle_command("model", "carbon", "ou_u", "oc_c", store, bot=None)
    assert reply.startswith("❌")


async def test_agy_model_card_buttons_use_unprefixed_names(tmp_path):
    store = _store(tmp_path, runner="agy")

    card = await commands.handle_command("model", "", "ou_u", "oc_c", store, bot=None)

    cmds = [b["value"]["cmd"] for b in card["buttons"]]
    assert cmds[:5] == ["/model neon", "/model hydrogen", "/model carbon", "/model argon", "/model flash"]
    assert not any("agy-" in c for c in cmds)
    # 每个按钮点出去都要真能切成功
    for c in cmds:
        reply = await commands.handle_command("model", c.split(" ", 1)[1], "ou_u", "oc_c", store, bot=None)
        assert reply.startswith("✅"), (c, reply)


async def test_agy_oauth_model_card_buttons_all_switch(tmp_path):
    """OAuth 订阅通道：3.8 Flash 不带前缀；第三方模型保留 agy- 前缀（裸 opus 是 Claude 的）。"""
    from types import SimpleNamespace

    store = _store(tmp_path, runner="agy")
    bot = SimpleNamespace(profile=SimpleNamespace(agy_model_provider="", runner="agy", name="agybot"))

    card = await commands.handle_command("model", "", "ou_u", "oc_c", store, bot=bot)

    cmds = [b["value"]["cmd"] for b in card["buttons"]]
    assert cmds == ["/model flash", "/model agy-opus", "/model agy-sonnet", "/model agy-gpt"]
    for c in cmds:
        reply = await commands.handle_command("model", c.split(" ", 1)[1], "ou_u", "oc_c", store, bot=None)
        assert reply.startswith("✅"), (c, reply)
