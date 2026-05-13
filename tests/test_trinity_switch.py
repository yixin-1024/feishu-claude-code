"""Trinity 主开关 ENABLE_TRINITY 的行为验证。

确保：
    1. 默认不设置 → TRINITY_ENABLED=False，即使 profile 配了 ROLE 也被清空
    2. ENABLE_TRINITY=true → 启用，profile 的 ROLE 字段生效
    3. 几种 falsy 值都视为 OFF
"""

import importlib
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _reload_with_env(enable_value: str | None, monkeypatch):
    """删除并重新 import bot_config，让 ENABLE_TRINITY env 重新生效。"""
    if enable_value is None:
        monkeypatch.delenv("ENABLE_TRINITY", raising=False)
    else:
        monkeypatch.setenv("ENABLE_TRINITY", enable_value)
    # 给一组最小可加载的 profile env
    monkeypatch.setenv("PROFILES", "alpha")
    monkeypatch.setenv("ALPHA_APP_ID", "cli_alpha")
    monkeypatch.setenv("ALPHA_APP_SECRET", "secret")
    monkeypatch.setenv("ALPHA_PLATFORM", "lark")
    monkeypatch.setenv("ALPHA_ROLE", "yushitai")  # 配了 role

    if "bot_config" in sys.modules:
        del sys.modules["bot_config"]
    import bot_config
    return bot_config


def test_default_off_clears_role(monkeypatch):
    cfg = _reload_with_env(None, monkeypatch)
    assert cfg.TRINITY_ENABLED is False
    assert cfg.PROFILES[0].role == ""        # 被软清空
    assert cfg.PROFILES[0].is_trinity is False
    assert cfg.PROFILES_BY_ROLE == {}


def test_explicit_off(monkeypatch):
    cfg = _reload_with_env("false", monkeypatch)
    assert cfg.TRINITY_ENABLED is False
    assert cfg.PROFILES[0].role == ""


def test_explicit_on_keeps_role(monkeypatch):
    cfg = _reload_with_env("true", monkeypatch)
    assert cfg.TRINITY_ENABLED is True
    assert cfg.PROFILES[0].role == "yushitai"
    assert cfg.PROFILES[0].is_trinity is True
    assert "yushitai" in cfg.PROFILES_BY_ROLE


def test_truthy_variants(monkeypatch):
    for v in ["TRUE", "1", "yes", "ON", "Y"]:
        cfg = _reload_with_env(v, monkeypatch)
        assert cfg.TRINITY_ENABLED is True, f"expected ENABLE_TRINITY={v!r} → on"


def test_falsy_variants(monkeypatch):
    for v in ["false", "0", "no", "off", "", "random"]:
        cfg = _reload_with_env(v, monkeypatch)
        assert cfg.TRINITY_ENABLED is False, f"expected ENABLE_TRINITY={v!r} → off"
