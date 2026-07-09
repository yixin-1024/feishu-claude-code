import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http_server import resolve_target_agent


class _P:
    def __init__(self, name, runner):
        self.name = name
        self.runner = runner


class _B:
    def __init__(self, name, runner):
        self.profile = _P(name, runner)


def _bots():
    # 复刻线上多 profile 拓扑：两个 claude、两个 codex(GPT)、一个 opencode、一个 mimo。
    return {
        "spx": _B("spx", "claude"),
        "seesaw": _B("seesaw", "claude"),
        "regtank": _B("regtank", "codex"),
        "sscodex": _B("sscodex", "codex"),
        "hermes": _B("hermes", "opencode"),
        "mimo": _B("mimo", "mimo"),
    }


def test_alias_gpt_maps_to_codex():
    b, err = resolve_target_agent(_bots(), "gpt", exclude="spx")
    assert err == ""
    assert b.profile.runner == "codex"


def test_exact_profile_name_wins():
    b, err = resolve_target_agent(_bots(), "hermes")
    assert err == ""
    assert b.profile.name == "hermes"


def test_exact_profile_name_case_insensitive():
    b, err = resolve_target_agent(_bots(), "SSCodex")
    assert err == ""
    assert b.profile.name == "sscodex"


def test_alias_prefers_non_excluded_candidate():
    # 调用方是 regtank(codex)；要另一个 codex → 应选 sscodex，不回自己。
    b, err = resolve_target_agent(_bots(), "codex", exclude="regtank")
    assert err == ""
    assert b.profile.name == "sscodex"


def test_alias_falls_back_when_only_excluded_matches():
    # 唯一的 mimo 就是调用方自己 → 没别的候选就返回它（同 agent，不报错）。
    b, err = resolve_target_agent(_bots(), "mimo", exclude="mimo")
    assert err == ""
    assert b.profile.name == "mimo"


def test_gemini_alias_maps_to_opencode():
    b, err = resolve_target_agent(_bots(), "gemini", exclude="spx")
    assert err == ""
    assert b.profile.runner == "opencode"


def test_unknown_agent_lists_options():
    b, err = resolve_target_agent(_bots(), "llama")
    assert b is None
    assert "已加载可选" in err
    assert "spx(claude)" in err


def test_empty_spec_errs():
    b, err = resolve_target_agent(_bots(), "")
    assert b is None
