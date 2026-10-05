import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http_server import resolve_target_agent


class _P:
    def __init__(self, name, runner, groups=()):
        self.name = name
        self.runner = runner
        # 群白名单：既有的"这个 bot 能不能在这个群干活"闸门（"*" = 任意群）
        self.allowed_group_chat_ids = set(groups)


class _B:
    def __init__(self, name, runner, groups=()):
        self.profile = _P(name, runner, groups)


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


# ── 同家族命中时必须先看"在不在本群" ──────────────────────────────
#
# 线上复现：spx(claude) 会话里调 dispatch_task(agent="claude") → 家族候选
# [spx, seesaw] → 旧逻辑"优先选别人"挑中 seesaw，而 seesaw 根本不在本群 →
# 建话题 230002 Bot/User can NOT be out of the chat → HTTP 400。

CHAT = "oc_here"


def _chat_bots():
    """本群里只有 spx(claude) 和 regtank(codex)；seesaw/sscodex 属于另一租户的两个群。
    dict 顺序刻意把"不在本群"的同族 bot 排在前面，以确保过滤真的生效（而不是碰巧）。"""
    return {
        "seesaw": _B("seesaw", "claude", {"oc_other1", "oc_other2"}),
        "spx": _B("spx", "claude", {CHAT, "oc_other3"}),
        "sscodex": _B("sscodex", "codex", {"oc_other1", "oc_other2"}),
        "regtank": _B("regtank", "codex", {CHAT}),
        "hermes": _B("hermes", "opencode", {"oc_other1"}),
    }


def test_same_family_prefers_caller_instead_of_bot_outside_chat():
    # 用户说"派给 claude"，调用方自己就是 claude → 选自己（它一定在本群），
    # 绝不能踢给不在本群的 seesaw。
    b, err = resolve_target_agent(_chat_bots(), "claude", exclude="spx", chat_id=CHAT)
    assert err == ""
    assert b.profile.name == "spx"


def test_cross_agent_still_crosses_and_picks_the_bot_in_this_chat():
    # 跨 agent 意图不受影响：spx 里 agent="gpt" 仍要拿到 codex bot，
    # 且必须是本群里的那个（regtank），不是排在前面但不在本群的 sscodex。
    b, err = resolve_target_agent(_chat_bots(), "gpt", exclude="spx", chat_id=CHAT)
    assert err == ""
    assert b.profile.runner == "codex"
    assert b.profile.name == "regtank"


def test_wildcard_group_counts_as_in_chat():
    # "*" = 任意群，所以 tg 算在本群；排在前面的 seesaw 不在本群应被滤掉。
    bots = {
        "seesaw": _B("seesaw", "claude", {"oc_other1"}),
        "tg": _B("tg", "claude", {"*"}),
        "spx": _B("spx", "codex", {CHAT}),
    }
    b, err = resolve_target_agent(bots, "claude", exclude="spx", chat_id=CHAT)
    assert err == ""
    assert b.profile.name == "tg"


def test_falls_back_to_full_family_when_nobody_serves_this_chat():
    # 整个家族都不在本群 → 不因为过滤把功能掐死，退回旧行为选一个
    # （真失败时 dispatch_task 的 230002 提示 + _post_json 透传会说清原因）。
    b, err = resolve_target_agent(_chat_bots(), "gemini", exclude="spx", chat_id=CHAT)
    assert err == ""
    assert b.profile.name == "hermes"


def test_explicit_profile_name_still_wins_over_chat_filter():
    # 显式点名 profile 是最高优先级：用户自己知道要谁，不替他改。
    b, err = resolve_target_agent(_chat_bots(), "seesaw", exclude="spx", chat_id=CHAT)
    assert err == ""
    assert b.profile.name == "seesaw"
