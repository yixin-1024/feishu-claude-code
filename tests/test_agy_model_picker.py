"""`/model` 选择卡对 agy 只列本 bot 那条鉴权通道能用的模型。

agy 1.5.9 实测两条通道的模型表不重叠：API key 有 Gemini 4、没有 Claude/GPT；
OAuth 订阅有 Claude 4.6 / GPT-OSS、没有 Gemini 4。点了另一边的模型 agy 直接报
not recognized，所以按钮必须跟着 profile.agy_model_provider 走。
"""

from types import SimpleNamespace

import pytest

import session_store as session_store_module
from commands import handle_command, resolve_model_alias
from session_store import SessionStore


@pytest.fixture
def agy_store(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "state"
    sessions_dir.mkdir()
    monkeypatch.setattr(session_store_module, "SESSIONS_DIR", str(sessions_dir))
    return SessionStore(
        profile="antigravity",
        default_cwd=str(tmp_path),
        default_runner="agy",
        default_model="gemini-4-carbon",
    )


def _bot(provider):
    return SimpleNamespace(
        profile=SimpleNamespace(
            runner="agy",
            default_model="gemini-4-carbon",
            agy_model_provider=provider,
            agy_home="",
        )
    )


async def _picker_models(store, bot):
    reply = await handle_command("model", "", "user_1", "oc_agy", store, bot=bot)
    cmds = [b["value"]["cmd"] for b in reply["buttons"]]
    assert all(c.startswith("/model ") for c in cmds)
    # 按钮上是本后端的短名（/model carbon），按 /model 同一套解析还原成模型 id
    return {resolve_model_alias(c.split(" ", 1)[1], "agy") for c in cmds}


@pytest.mark.asyncio
async def test_api_key_bot_lists_gemini4_and_no_claude(agy_store):
    models = await _picker_models(agy_store, _bot("gemini"))
    assert {"gemini-4-neon", "gemini-4-carbon", "gemini-4-argon", "gemini-4-hydrogen"} <= models
    assert not any(m.startswith(("claude-", "gpt-")) for m in models)


@pytest.mark.asyncio
async def test_oauth_bot_lists_subscription_models_only(agy_store):
    models = await _picker_models(agy_store, _bot(""))
    assert models == {
        "gemini-3.8-flash",
        "claude-opus-4-6-thinking",
        "claude-sonnet-4-6",
        "gpt-oss-120b-medium",
    }
