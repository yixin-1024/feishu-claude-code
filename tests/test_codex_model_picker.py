"""`/model` 选择卡对 codex 照 codex 自己的模型目录（~/.codex/models_cache.json）出按钮。

以前按钮写死成 Codex Max / Codex / GPT-5.1（gpt-5.1 那一代），模型目录早就没有它们了，
点了会切到用不了的模型。现在每个系列只列最新一款，目录读不到才用兜底表。
"""

import json
from types import SimpleNamespace

import pytest

import commands
import session_store as session_store_module
from commands import handle_command, resolve_model_alias
from session_store import SessionStore


def _catalog(*models):
    return {"fetched_at": "2026-11-21T00:00:00Z", "models": list(models)}


def _m(slug, priority, visibility="list", display=None, levels=("low", "medium", "high", "xhigh")):
    return {
        "slug": slug,
        "display_name": display or slug.upper(),
        "priority": priority,
        "visibility": visibility,
        "supported_reasoning_levels": [{"effort": e} for e in levels],
    }


@pytest.fixture
def codex_store(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "state"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_store_module, "SESSIONS_DIR", str(sessions_dir))
    return SessionStore(
        profile="fscodex",
        default_cwd=str(tmp_path),
        default_runner="codex",
        default_model="gpt-6.2-astra",
    )


@pytest.fixture
def write_catalog(tmp_path, monkeypatch):
    path = tmp_path / "models_cache.json"
    monkeypatch.setattr(commands, "CODEX_MODELS_CACHE", str(path))

    def _write(data):
        path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")

    return _write


def _bot():
    return SimpleNamespace(profile=SimpleNamespace(name="fscodex", runner="codex", default_model="gpt-6.2-astra"))


async def _picker(store):
    reply = await handle_command("model", "", "user_1", "oc_codex", store, bot=_bot())
    return [(b["text"], b["value"]["cmd"]) for b in reply["buttons"]]


@pytest.mark.asyncio
async def test_buttons_follow_catalog_newest_per_family(codex_store, write_catalog):
    write_catalog(_catalog(
        _m("gpt-6.2-sol", 1, display="GPT-6.2-Sol"),
        _m("gpt-6.2-astra", 2, display="GPT-6.2-Astra"),
        _m("gpt-6.1-terra", 3, display="GPT-6.1 Terra"),
        _m("gpt-reserve", 4, visibility="hide"),
        _m("gpt-6.1-sol", 5),
        _m("gpt-6-astra", 6),
        _m("gpt-6-luna", 8, display="GPT-6 Luna"),
        _m("gpt-5.6-terra", 10),
        _m("gpt-5.6-luna", 11),
        _m("codex-auto-review", 12, visibility="hide"),
    ))
    assert await _picker(codex_store) == [
        ("GPT-6.2-Sol", "/model gpt-6.2-sol"),
        ("GPT-6.2-Astra", "/model gpt-6.2-astra"),
        ("GPT-6.1 Terra", "/model gpt-6.1-terra"),
        ("GPT-6 Luna", "/model gpt-6-luna"),
    ]


@pytest.mark.asyncio
async def test_newest_version_wins_even_if_catalog_order_is_odd(codex_store, write_catalog):
    # 版本号按数字比：6.10 比 6.9 新；目录顺序不可靠时也挑对
    write_catalog(_catalog(_m("gpt-6.9-sol", 1), _m("gpt-6.10-sol", 2), _m("gpt-6-sol", 3)))
    assert [cmd for _, cmd in await _picker(codex_store)] == ["/model gpt-6.10-sol"]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "not json", json.dumps({"models": "oops"}), json.dumps(_catalog())])
async def test_missing_or_broken_catalog_falls_back(codex_store, write_catalog, content):
    if content is not None:
        write_catalog(content)
    cmds = [cmd for _, cmd in await _picker(codex_store)]
    assert cmds == [f"/model {slug}" for slug, _ in commands._CODEX_FALLBACK_MODEL_BUTTONS]


@pytest.mark.asyncio
async def test_no_stale_gpt51_buttons(codex_store, write_catalog):
    write_catalog(_catalog(_m("gpt-6.2-astra", 1)))
    texts_and_cmds = " ".join(t + c for t, c in await _picker(codex_store))
    assert "5.1" not in texts_and_cmds and "codex-max" not in texts_and_cmds


@pytest.mark.asyncio
async def test_button_switches_model(codex_store, write_catalog):
    write_catalog(_catalog(_m("gpt-6.2-sol", 1), _m("gpt-6.2-astra", 2)))
    out = await handle_command("model", "gpt-6.2-sol", "user_1", "oc_codex", codex_store, bot=_bot())
    assert "gpt-6.2-sol" in out
    cur = await codex_store.get_current("user_1", "oc_codex")
    assert cur.model == "gpt-6.2-sol"


def test_old_aliases_point_at_current_models():
    assert resolve_model_alias("codex", "codex") == "gpt-6.2-astra"
    assert resolve_model_alias("codex-max", "codex") == "gpt-6.2-astra"
    assert not any(v.startswith("gpt-5.1") for v in commands.MODEL_ALIASES.values())


def test_effort_levels_still_read_catalog(write_catalog):
    write_catalog(_catalog(_m("gpt-6.2-astra", 1, levels=("low", "high", "xhigh", "max", "ultra"))))
    assert commands._codex_effort_levels("gpt-6.2-astra") == ("low", "high", "xhigh", "max", "ultra")
    assert commands._codex_effort_levels("unknown-model") == commands.CODEX_EFFORT_LEVELS


def test_runner_switch_default_follows_catalog(write_catalog):
    write_catalog(_catalog(_m("gpt-6.2-sol", 1), _m("gpt-6.2-astra", 2)))
    bot = SimpleNamespace(profile=SimpleNamespace(runner="claude", default_model="opus[1m]"))
    assert commands._runner_default_model(bot, "codex") == "gpt-6.2-sol"
