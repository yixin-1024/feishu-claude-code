"""resolve_cc_lark_gates —— cc-lark 运行时 MCP 能力闸门的 per-profile 解析。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_config import resolve_cc_lark_gates  # noqa: E402

_FLAGS = ("CC_LARK_ALLOW_DISPATCH", "CC_LARK_ALLOW_WAKE", "CC_LARK_ALLOW_CRON")


def _clear(monkeypatch):
    for f in _FLAGS:
        monkeypatch.delenv(f, raising=False)
        for p in ("SPX_", "REGTANK_"):
            monkeypatch.delenv(p + f, raising=False)


def test_unset_returns_empty(monkeypatch):
    """一个都没设 → 空 dict（交给 cc_mcp_server 默认放行）。"""
    _clear(monkeypatch)
    assert resolve_cc_lark_gates("") == {}
    assert resolve_cc_lark_gates("spx") == {}


def test_global_fallback(monkeypatch):
    """只设全局 → 所有 profile 都拿到全局值。"""
    _clear(monkeypatch)
    monkeypatch.setenv("CC_LARK_ALLOW_DISPATCH", "0")
    assert resolve_cc_lark_gates("") == {"CC_LARK_ALLOW_DISPATCH": "0"}
    assert resolve_cc_lark_gates("regtank") == {"CC_LARK_ALLOW_DISPATCH": "0"}


def test_per_profile_overrides_global(monkeypatch):
    """<PROFILE>_<FLAG> 覆盖全局 <FLAG>，且只对该 profile 生效。"""
    _clear(monkeypatch)
    monkeypatch.setenv("CC_LARK_ALLOW_DISPATCH", "0")       # 全局关
    monkeypatch.setenv("SPX_CC_LARK_ALLOW_DISPATCH", "1")   # 只给 spx 开
    assert resolve_cc_lark_gates("spx") == {"CC_LARK_ALLOW_DISPATCH": "1"}
    assert resolve_cc_lark_gates("regtank") == {"CC_LARK_ALLOW_DISPATCH": "0"}


def test_profile_name_normalized(monkeypatch):
    """profile 名归一成前缀：大写 + 非字母数字→下划线。"""
    _clear(monkeypatch)
    monkeypatch.setenv("SS_CODEX_CC_LARK_ALLOW_WAKE", "0")
    assert resolve_cc_lark_gates("ss-codex") == {"CC_LARK_ALLOW_WAKE": "0"}
