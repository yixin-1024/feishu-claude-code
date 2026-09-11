"""platform=telegram 的 profile 加载。

Telegram 没有 app_id/app_secret，只有一个 bot token；下游（thread_context 判"这条
是不是我发的"、card_security 的 HMAC key）仍然读 app_id/app_secret 两个字段，所以
加载时必须把 token 拆好填进去，而不是在下游到处加分支。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config
from bot_config import _load_profile


@pytest.fixture
def env(monkeypatch):
    def _set(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(k, v)
    return _set


def test_token_is_split_into_app_id_and_secret(env):
    env(T_PLATFORM="telegram", T_BOT_TOKEN="8976773826:AAEsecret",
        T_DEFAULT_CWD="/tmp/x")
    p = _load_profile("t")
    assert p.platform == "telegram" and p.is_telegram
    assert p.app_id == "8976773826"          # = 数字 bot id
    assert p.app_secret == "8976773826:AAEsecret"
    assert p.bot_token == "8976773826:AAEsecret"
    assert p.domain == "https://api.telegram.org"
    assert p.brand_label == "Telegram"
    assert p.default_cwd == "/tmp/x"


def test_app_id_and_secret_are_not_required(env):
    """Telegram profile 不该被"缺 APP_ID"卡住。"""
    env(T2_PLATFORM="telegram", T2_BOT_TOKEN="1:x")
    assert _load_profile("t2").app_id == "1"


def test_missing_token_is_a_clear_error(env):
    env(T3_PLATFORM="telegram")
    with pytest.raises(ValueError, match="T3_BOT_TOKEN"):
        _load_profile("t3")


def test_malformed_token_is_rejected(env):
    env(T4_PLATFORM="telegram", T4_BOT_TOKEN="没有冒号")
    with pytest.raises(ValueError, match="T4_BOT_TOKEN"):
        _load_profile("t4")


def test_unknown_platform_lists_all_three(env):
    env(T5_PLATFORM="wechat", T5_APP_ID="a", T5_APP_SECRET="b")
    with pytest.raises(ValueError, match="feishu, lark, telegram"):
        _load_profile("t5")


def test_lark_profile_still_requires_app_credentials(env):
    env(T6_PLATFORM="lark")
    with pytest.raises(ValueError, match="T6_APP_ID"):
        _load_profile("t6")


def test_flags_default_on_and_can_be_turned_off(env):
    env(T7_PLATFORM="telegram", T7_BOT_TOKEN="1:x")
    p = _load_profile("t7")
    assert p.tg_commands_imply_mention == 1
    assert p.tg_reveal_unauthorized == 1

    env(T8_PLATFORM="telegram", T8_BOT_TOKEN="1:x",
        T8_COMMANDS_IMPLY_MENTION="0", T8_REVEAL_UNAUTHORIZED="off")
    p = _load_profile("t8")
    assert p.tg_commands_imply_mention == 0
    assert p.tg_reveal_unauthorized == 0


def test_per_group_workspace_map_accepts_negative_chat_ids(env):
    """Telegram 群 id 是负数，env key 里带 `-` 也要能被扫到。"""
    env(T9_PLATFORM="telegram", T9_BOT_TOKEN="1:x",
        **{"T9_CHAT_CWD_-1001234567890": "/tmp/proj-a"})
    p = _load_profile("t9")
    assert p.chat_default_cwd["-1001234567890"] == "/tmp/proj-a"


def test_runner_and_model_config_is_shared_with_lark_profiles(env):
    """"底层还是同一套 runner" —— 渠道不影响 runner/model/effort 那套配置。"""
    env(TA_PLATFORM="telegram", TA_BOT_TOKEN="1:x", TA_RUNNER="codex",
        TA_DEFAULT_MODEL="gpt-5.6-sol", TA_DISPATCH_MODEL="gpt-5.6-sol")
    p = _load_profile("ta")
    assert (p.runner, p.default_model, p.dispatch_model) == (
        "codex", "gpt-5.6-sol", "gpt-5.6-sol")


def test_find_primary_user_accepts_numeric_telegram_ids():
    """dispatch_task / schedule_wake 的"归属人兜底"在 telegram profile 上也要有解。"""
    from session_store import SessionStore

    store = SessionStore(profile="tgprim")
    store._data = {"__thread__": {"-100:c-100": {}}, "123456789": {"private": {}}}
    assert store.find_primary_user() == "123456789"

    store._data = {"999": {"private": {}}, "ou_abc": {"private": {}}}
    assert store.find_primary_user() == "ou_abc"   # Lark 优先，行为不变
