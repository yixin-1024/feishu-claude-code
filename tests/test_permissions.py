"""permissions 模块单测：transition 表 + 校验逻辑。"""

import pytest

from permissions import Role, State, validate_transition


@pytest.mark.parametrize(
    "desc, sender, recipient, state, hint, expect_state",
    [
        ("Boss 起新需求", Role.USER, Role.YUSHITAI, None, None, State.TRIAGING),
        ("御史台派给中书", Role.YUSHITAI, Role.ZHONGSHU, State.TRIAGING, None, State.DRAFTING),
        ("中书拟完交门下", Role.ZHONGSHU, Role.MENXIA, State.DRAFTING, None, State.AUDITING),
        ("门下批准 → 尚书", Role.MENXIA, Role.SHANGSHU, State.AUDITING, None, State.DISPATCHED),
        ("尚书派工", Role.SHANGSHU, Role.GANHUODE, State.DISPATCHED, None, State.DOING),
        ("干活的回奏尚书", Role.GANHUODE, Role.SHANGSHU, State.DOING, None, State.AGGREGATING),
        ("尚书交门下影子复审", Role.SHANGSHU, Role.MENXIA, State.AGGREGATING, None, State.SHADOWING),
        ("门下复审通过", Role.MENXIA, Role.YUSHITAI, State.SHADOWING, None, State.SUMMARIZING),
        ("门下驳回", Role.MENXIA, Role.ZHONGSHU, State.AUDITING, None, State.REJECTED),
        ("中书重拟", Role.ZHONGSHU, Role.MENXIA, State.REJECTED, None, State.AUDITING),
        ("御史台简单任务直接答", Role.YUSHITAI, Role.USER, State.TRIAGING, State.DONE, State.DONE),
        ("御史台总结回 Boss", Role.YUSHITAI, Role.USER, State.SUMMARIZING, State.DONE, State.DONE),
        ("御史台升级", Role.YUSHITAI, Role.USER, State.TRIAGING, State.ESCALATED, State.ESCALATED),
    ],
)
def test_legal_transitions(desc, sender, recipient, state, hint, expect_state):
    """合法 transition 应正确推进状态。"""
    r = validate_transition(sender, recipient, state, hint)
    assert r.ok, f"{desc}: {r.reason}"
    assert r.new_state == expect_state


@pytest.mark.parametrize(
    "desc, sender, recipient, state",
    [
        ("中书不能跳过门下", Role.ZHONGSHU, Role.SHANGSHU, State.DRAFTING),
        ("干活的不能直接找门下", Role.GANHUODE, Role.MENXIA, State.DOING),
        ("尚书不能跳过门下回御史台", Role.SHANGSHU, Role.YUSHITAI, State.AGGREGATING),
        ("state 错位", Role.MENXIA, Role.SHANGSHU, State.DOING),
    ],
)
def test_illegal_transitions(desc, sender, recipient, state):
    """非法 transition 应被拒绝。"""
    r = validate_transition(sender, recipient, state, None)
    assert not r.ok, desc


def test_multi_candidate_requires_hint():
    """yushitai→user 在 TRIAGING 时有 DONE / ESCALATED 两个候选，必须指定 hint。"""
    r = validate_transition(Role.YUSHITAI, Role.USER, State.TRIAGING, None)
    assert not r.ok
    assert "action_hint" in r.reason or "候选" in r.reason


def test_full_happy_path():
    """完整链路：TRIAGING → DRAFTING → AUDITING → DISPATCHED → DOING → AGGREGATING → SHADOWING → SUMMARIZING → DONE"""
    transitions = [
        (Role.USER, Role.YUSHITAI, None, None, State.TRIAGING),
        (Role.YUSHITAI, Role.ZHONGSHU, State.TRIAGING, None, State.DRAFTING),
        (Role.ZHONGSHU, Role.MENXIA, State.DRAFTING, None, State.AUDITING),
        (Role.MENXIA, Role.SHANGSHU, State.AUDITING, None, State.DISPATCHED),
        (Role.SHANGSHU, Role.GANHUODE, State.DISPATCHED, None, State.DOING),
        (Role.GANHUODE, Role.SHANGSHU, State.DOING, None, State.AGGREGATING),
        (Role.SHANGSHU, Role.MENXIA, State.AGGREGATING, None, State.SHADOWING),
        (Role.MENXIA, Role.YUSHITAI, State.SHADOWING, None, State.SUMMARIZING),
        (Role.YUSHITAI, Role.USER, State.SUMMARIZING, State.DONE, State.DONE),
    ]
    state = None
    for sender, recipient, expected_from, hint, expected_to in transitions:
        assert state == expected_from, f"state drift at {sender.value}→{recipient.value}"
        r = validate_transition(sender, recipient, state, hint)
        assert r.ok
        state = r.new_state
    assert state == State.DONE


def test_reject_and_redraft_loop():
    """门下驳回 → 中书重拟 → 再交门下，循环 N 次都合法。"""
    state = State.AUDITING
    for _ in range(3):
        r = validate_transition(Role.MENXIA, Role.ZHONGSHU, state, None)
        assert r.ok
        state = r.new_state
        assert state == State.REJECTED

        r = validate_transition(Role.ZHONGSHU, Role.MENXIA, state, None)
        assert r.ok
        state = r.new_state
        assert state == State.AUDITING
