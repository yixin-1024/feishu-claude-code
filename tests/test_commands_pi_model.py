"""pi 的 /model 面板：本地几家一家一个按钮，当前那款带 ●，正文报余额；按钮能切到另一家。"""
import os
from types import SimpleNamespace

import pytest

import pi_runner
import session_store as session_store_module
from commands import handle_command
from session_store import SessionStore

LEBREL = "lebrel/lebrel/deepseek-v4.1-flash-uncensored"
VENICE = "venice/deepseek-v4-1-flash-1017-uncensored"


@pytest.fixture
def pi_store(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("PI_LOCAL_") or k == "PI_GEMINI_BUTTONS":
            monkeypatch.delenv(k, raising=False)
    for k in ("GEMINI_API_KEY", "AGY_API_KEY", "PI_GEMINI_KEY_FROM", "PI_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PI_LOCAL_PROVIDER", "lebrel")
    monkeypatch.setenv("PI_LOCAL_MODELS", "lebrel/deepseek-v4.1-flash-uncensored")
    monkeypatch.setenv("PI_LOCAL_LABEL", "🐕 Lebrel")
    monkeypatch.setenv("PI_LOCAL_MORE_PROVIDERS", "venice")
    monkeypatch.setenv("PI_LOCAL_VENICE_MODELS", "deepseek-v4-1-flash-1017-uncensored")
    monkeypatch.setenv("PI_LOCAL_VENICE_LABEL", "🐋 Venice")
    monkeypatch.setenv("WUFENGT_PI_MODEL", LEBREL)
    monkeypatch.setattr(pi_runner, "local_balance_lines", lambda: ["💰 Venice 余额 $8.79"])
    sessions_dir = tmp_path / "state"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_store_module, "SESSIONS_DIR", str(sessions_dir))
    return SessionStore(profile="wufengt", default_cwd=str(tmp_path), default_runner="pi", default_model=LEBREL)


def _bot():
    return SimpleNamespace(profile=SimpleNamespace(name="wufengt", runner="pi", default_model=LEBREL))


@pytest.mark.asyncio
async def test_pi_model_card_buttons_mark_current_and_show_balance(pi_store):
    reply = await handle_command("model", "", "u1", "oc_x", pi_store, bot=_bot())
    labels = [b["text"] for b in reply["buttons"]]
    assert labels == ["● 🐕 Lebrel", "🐋 Venice"]
    assert "💰 Venice 余额 $8.79" in reply["text"]
    cmds = [b["value"]["cmd"] for b in reply["buttons"]]
    assert cmds == [f"/model {LEBREL}", f"/model {VENICE}"]

    # 点 Venice 按钮 → 本话题换成 Venice，再开面板 ● 跟着走
    out = await handle_command("model", VENICE, "u1", "oc_x", pi_store, bot=_bot())
    assert f"`{VENICE}`" in out
    assert (await pi_store.get_current("u1", "oc_x")).model == VENICE
    reply = await handle_command("model", "", "u1", "oc_x", pi_store, bot=_bot())
    assert [b["text"] for b in reply["buttons"]][:2] == ["🐕 Lebrel", "● 🐋 Venice"]


@pytest.mark.asyncio
async def test_pi_model_card_survives_balance_failure(pi_store, monkeypatch):
    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(pi_runner, "local_balance_lines", boom)
    reply = await handle_command("model", "", "u1", "oc_x", pi_store, bot=_bot())
    assert "余额" not in reply["text"]
    assert len(reply["buttons"]) == 2
