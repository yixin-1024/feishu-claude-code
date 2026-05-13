"""三省体系 · 角色 / 状态机 / Transition 表（草稿）

5 个 role：
    USER (Boss) → YUSHITAI → ZHONGSHU → MENXIA → SHANGSHU → GANHUODE
    上行回奏：     ← YUSHITAI ← MENXIA  ← SHANGSHU  ← GANHUODE

状态机说明见 DESIGN.md §2。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Role(str, Enum):
    USER = "user"             # Boss 本人
    YUSHITAI = "yushitai"     # 御史台（含原太子分拣 + 终审）
    ZHONGSHU = "zhongshu"     # 中书（拟令）
    MENXIA = "menxia"         # 门下（事前审议 + 事后影子复审）
    SHANGSHU = "shangshu"     # 尚书（派工 + 聚合）
    GANHUODE = "ganhuode"     # 干活的（六部人格在 prompt 内切换）


class State(str, Enum):
    # 工作态：每个 state 唯一归属一个 role
    TRIAGING = "TRIAGING"          # yushitai 与 Boss 对话 / 分诊
    DRAFTING = "DRAFTING"          # zhongshu 拟令
    AUDITING = "AUDITING"          # menxia 事前审议
    DISPATCHED = "DISPATCHED"      # shangshu 收到批准的 ticket，准备派工
    DOING = "DOING"                # ganhuode 执行
    AGGREGATING = "AGGREGATING"    # shangshu 收到下游回奏，聚合
    SHADOWING = "SHADOWING"        # menxia 事后影子复审
    SUMMARIZING = "SUMMARIZING"    # yushitai 总结准备回 Boss

    # 终态
    DONE = "DONE"
    REJECTED = "REJECTED"          # 门下驳回，中书要重拟
    ESCALATED = "ESCALATED"        # 御史台让 Boss 补信息


# 哪个 state 在哪个 role 手里（供 bot 收到消息时校验：state 不属于我，我不该接活）
STATE_OWNER: dict[State, Role] = {
    State.TRIAGING: Role.YUSHITAI,
    State.DRAFTING: Role.ZHONGSHU,
    State.AUDITING: Role.MENXIA,
    State.DISPATCHED: Role.SHANGSHU,
    State.DOING: Role.GANHUODE,
    State.AGGREGATING: Role.SHANGSHU,
    State.SHADOWING: Role.MENXIA,
    State.SUMMARIZING: Role.YUSHITAI,
    State.DONE: Role.YUSHITAI,        # 由御史台收尾
    State.REJECTED: Role.ZHONGSHU,    # 重拟由中书接手
    State.ESCALATED: Role.USER,
}


@dataclass(frozen=True)
class Transition:
    valid_from: frozenset[Optional[State]]   # None = ticket 还不存在（新建）
    to: State
    note: str = ""


# 转移表：(sender, recipient) → 允许的 transition
# 同一组 (sender, recipient) 可能对应多条候选（比如 yushitai→user 可能是 DONE 或 ESCALATED），
# 用接收方当前 state 区分。
TRANSITIONS: dict[tuple[Role, Role], list[Transition]] = {
    # ── 下行链路 ──
    (Role.USER, Role.YUSHITAI): [
        Transition(frozenset({None, State.ESCALATED, State.DONE}), State.TRIAGING,
                   "Boss 找御史台（新需求 / 补充信息 / 新对话）"),
    ],
    (Role.YUSHITAI, Role.ZHONGSHU): [
        Transition(frozenset({State.TRIAGING}), State.DRAFTING,
                   "御史台分诊为复杂任务，派给中书拟令"),
    ],
    (Role.ZHONGSHU, Role.MENXIA): [
        Transition(frozenset({State.DRAFTING, State.REJECTED}), State.AUDITING,
                   "中书拟完（或被驳回后重拟）交门下审"),
    ],
    (Role.MENXIA, Role.SHANGSHU): [
        Transition(frozenset({State.AUDITING}), State.DISPATCHED,
                   "门下批准，交尚书派工"),
    ],
    (Role.SHANGSHU, Role.GANHUODE): [
        Transition(frozenset({State.DISPATCHED}), State.DOING,
                   "尚书派工给干活的"),
    ],

    # ── 上行链路 ──
    (Role.GANHUODE, Role.SHANGSHU): [
        Transition(frozenset({State.DOING}), State.AGGREGATING,
                   "干活的完成回奏尚书"),
    ],
    (Role.SHANGSHU, Role.MENXIA): [
        Transition(frozenset({State.AGGREGATING}), State.SHADOWING,
                   "尚书聚合完毕交门下影子复审"),
    ],
    (Role.MENXIA, Role.YUSHITAI): [
        Transition(frozenset({State.SHADOWING}), State.SUMMARIZING,
                   "门下影子复审通过，交御史台总结"),
    ],
    (Role.YUSHITAI, Role.USER): [
        # 三条候选，由 sender bot 在派发时附 action 字段决定走哪条
        Transition(frozenset({State.TRIAGING}), State.DONE,
                   "御史台简单任务直接答完（0 跳完整链路）"),
        Transition(frozenset({State.SUMMARIZING}), State.DONE,
                   "御史台完整流程总结，回 Boss 收尾"),
        Transition(frozenset({State.TRIAGING}), State.ESCALATED,
                   "御史台判不了需求，让 Boss 补信息"),
    ],

    # ── 驳回 ──
    (Role.MENXIA, Role.ZHONGSHU): [
        Transition(frozenset({State.AUDITING}), State.REJECTED,
                   "门下封驳，中书重拟"),
    ],
}


@dataclass
class TransitionResult:
    ok: bool
    new_state: Optional[State] = None
    reason: str = ""        # 失败原因（bot 拒绝时显示给上游）


def validate_transition(
    sender: Role,
    recipient: Role,
    current_state: Optional[State],
    action_hint: Optional[State] = None,
) -> TransitionResult:
    """校验一次 bot↔bot 调度是否合法。

    Args:
        sender: 发件人 role。
        recipient: 收件人 role。
        current_state: ticket 当前 state（None = 新建）。
        action_hint: 当一组 (sender, recipient) 有多条候选时（如 yushitai→user），
                     由发件人显式指明要走哪条。匹配候选的 `to` 字段。

    Returns:
        TransitionResult(ok, new_state, reason)
    """
    key = (sender, recipient)
    candidates = TRANSITIONS.get(key)
    if not candidates:
        return TransitionResult(
            ok=False,
            reason=f"非法调度路径：{sender.value} → {recipient.value} 不在权限矩阵里",
        )

    # 过滤出当前 state 允许的候选
    valid = [t for t in candidates if current_state in t.valid_from]
    if not valid:
        allowed = sorted({s.value if s else "INITIAL" for t in candidates for s in t.valid_from})
        cur = current_state.value if current_state else "INITIAL"
        return TransitionResult(
            ok=False,
            reason=(
                f"state 不允许此转移：当前 state={cur}，"
                f"{sender.value}→{recipient.value} 要求 state ∈ {allowed}"
            ),
        )

    # 多候选时用 action_hint 选定
    if len(valid) > 1:
        if action_hint is None:
            return TransitionResult(
                ok=False,
                reason=(
                    f"{sender.value}→{recipient.value} 有多条候选，需指定 action_hint："
                    + ", ".join(t.to.value for t in valid)
                ),
            )
        matched = [t for t in valid if t.to == action_hint]
        if not matched:
            return TransitionResult(
                ok=False,
                reason=(
                    f"action_hint={action_hint.value} 不在候选中："
                    + ", ".join(t.to.value for t in valid)
                ),
            )
        return TransitionResult(ok=True, new_state=matched[0].to)

    return TransitionResult(ok=True, new_state=valid[0].to)


# ── 自检：跑 `python permissions.py` 直接看测试输出 ─────────────────

def _self_test():
    cases = [
        # (描述, sender, recipient, current_state, action_hint, 期望 ok, 期望 new_state)
        ("Boss 起新需求",          Role.USER, Role.YUSHITAI, None, None, True, State.TRIAGING),
        ("御史台派给中书",            Role.YUSHITAI, Role.ZHONGSHU, State.TRIAGING, None, True, State.DRAFTING),
        ("中书拟完交门下",            Role.ZHONGSHU, Role.MENXIA, State.DRAFTING, None, True, State.AUDITING),
        ("门下驳回",                  Role.MENXIA, Role.ZHONGSHU, State.AUDITING, None, True, State.REJECTED),
        ("中书重拟再交门下",          Role.ZHONGSHU, Role.MENXIA, State.REJECTED, None, True, State.AUDITING),
        ("门下批准 → 尚书",           Role.MENXIA, Role.SHANGSHU, State.AUDITING, None, True, State.DISPATCHED),
        ("尚书派工",                  Role.SHANGSHU, Role.GANHUODE, State.DISPATCHED, None, True, State.DOING),
        ("干活的回奏尚书",            Role.GANHUODE, Role.SHANGSHU, State.DOING, None, True, State.AGGREGATING),
        ("尚书交门下影子复审",        Role.SHANGSHU, Role.MENXIA, State.AGGREGATING, None, True, State.SHADOWING),
        ("门下复审通过交御史台",      Role.MENXIA, Role.YUSHITAI, State.SHADOWING, None, True, State.SUMMARIZING),
        ("御史台总结回 Boss",      Role.YUSHITAI, Role.USER, State.SUMMARIZING, State.DONE, True, State.DONE),
        ("御史台简单任务直接答",      Role.YUSHITAI, Role.USER, State.TRIAGING, State.DONE, True, State.DONE),
        ("御史台升级",                Role.YUSHITAI, Role.USER, State.TRIAGING, State.ESCALATED, True, State.ESCALATED),

        # 非法路径
        ("中书不能直接找尚书",        Role.ZHONGSHU, Role.SHANGSHU, State.DRAFTING, None, False, None),
        ("干活的不能直接找门下",      Role.GANHUODE, Role.MENXIA, State.DOING, None, False, None),
        ("尚书不能跳过门下回御史台",  Role.SHANGSHU, Role.YUSHITAI, State.AGGREGATING, None, False, None),
        ("state 错位：DOING 时门下找尚书",
                                       Role.MENXIA, Role.SHANGSHU, State.DOING, None, False, None),
        ("yushitai→user 多候选未指定 hint",
                                       Role.YUSHITAI, Role.USER, State.TRIAGING, None, False, None),
    ]

    pass_count = 0
    fail = []
    for desc, sender, recipient, state, hint, expect_ok, expect_state in cases:
        r = validate_transition(sender, recipient, state, hint)
        ok = (r.ok == expect_ok) and (not expect_ok or r.new_state == expect_state)
        if ok:
            pass_count += 1
        else:
            fail.append((desc, r, expect_ok, expect_state))

    print(f"PASS {pass_count}/{len(cases)}")
    for desc, r, eok, est in fail:
        print(f"FAIL: {desc}")
        print(f"  got ok={r.ok} new_state={r.new_state} reason={r.reason}")
        print(f"  expected ok={eok} new_state={est}")


if __name__ == "__main__":
    _self_test()
