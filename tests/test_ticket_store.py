"""ticket_store 单测：CRUD + transition 推进 + 历史日志。"""

import pytest

from permissions import Role, State
from ticket_store import TicketStore


@pytest.fixture
async def store(tmp_path):
    return TicketStore(path=str(tmp_path / "tickets.json"))


async def test_create_starts_in_triaging(store):
    ticket = await store.create(
        ticket_id="oc_x:omt_y",
        chat_id="oc_x",
        thread_id="omt_y",
        boss_open_id="ou_c",
    )
    assert ticket.state == State.TRIAGING.value
    assert store.current_owner("oc_x:omt_y") == Role.YUSHITAI
    assert len(ticket.history) == 1
    assert ticket.history[0].sender_role == Role.USER.value


async def test_transition_recorded_in_history(store):
    tid = "t1"
    await store.create(tid, "oc", "omt", "ou_c")

    r = await store.transition(tid, Role.YUSHITAI, Role.ZHONGSHU)
    assert r.ok
    assert r.new_state == State.DRAFTING

    ticket = store.get(tid)
    assert ticket.state == State.DRAFTING.value
    assert len(ticket.history) == 2
    assert ticket.history[-1].sender_role == Role.YUSHITAI.value
    assert ticket.history[-1].to_state == State.DRAFTING.value


async def test_illegal_transition_does_not_mutate(store):
    tid = "t2"
    await store.create(tid, "oc", "omt", "ou_c")

    r = await store.transition(tid, Role.GANHUODE, Role.SHANGSHU)
    assert not r.ok

    ticket = store.get(tid)
    assert ticket.state == State.TRIAGING.value  # 不变
    assert len(ticket.history) == 1


async def test_full_lifecycle(store):
    tid = "lifecycle"
    await store.create(tid, "oc", "omt", "ou_c")

    steps = [
        (Role.YUSHITAI, Role.ZHONGSHU, State.DRAFTING),
        (Role.ZHONGSHU, Role.MENXIA, State.AUDITING),
        (Role.MENXIA, Role.SHANGSHU, State.DISPATCHED),
        (Role.SHANGSHU, Role.GANHUODE, State.DOING),
        (Role.GANHUODE, Role.SHANGSHU, State.AGGREGATING),
        (Role.SHANGSHU, Role.MENXIA, State.SHADOWING),
        (Role.MENXIA, Role.YUSHITAI, State.SUMMARIZING),
    ]
    for sender, recipient, expected in steps:
        r = await store.transition(tid, sender, recipient)
        assert r.ok, f"{sender.value}→{recipient.value}: {r.reason}"
        assert r.new_state == expected

    # 御史台终审回 Boss（DONE）
    r = await store.transition(
        tid, Role.YUSHITAI, Role.USER, action_hint=State.DONE,
    )
    assert r.ok
    assert r.new_state == State.DONE

    ticket = store.get(tid)
    assert len(ticket.history) == 9  # 1 create + 8 transitions


async def test_rejection_redraft_loop(store):
    tid = "reject"
    await store.create(tid, "oc", "omt", "ou_c")
    await store.transition(tid, Role.YUSHITAI, Role.ZHONGSHU)
    await store.transition(tid, Role.ZHONGSHU, Role.MENXIA)

    # 驳回 → REJECTED
    r = await store.transition(tid, Role.MENXIA, Role.ZHONGSHU, note="缺 SoF")
    assert r.ok and r.new_state == State.REJECTED

    # 重拟 → AUDITING
    r = await store.transition(tid, Role.ZHONGSHU, Role.MENXIA)
    assert r.ok and r.new_state == State.AUDITING

    # history 应保留驳回理由
    ticket = store.get(tid)
    notes = [e.note for e in ticket.history if e.note]
    assert "缺 SoF" in notes


async def test_persistence_round_trip(tmp_path):
    """存盘后再读，状态应恢复。"""
    path = str(tmp_path / "tickets.json")
    store1 = TicketStore(path=path)
    await store1.create("t", "oc", "omt", "ou_c")
    await store1.transition("t", Role.YUSHITAI, Role.ZHONGSHU)

    store2 = TicketStore(path=path)
    ticket = store2.get("t")
    assert ticket is not None
    assert ticket.state == State.DRAFTING.value
    assert len(ticket.history) == 2


async def test_unknown_ticket_returns_error(store):
    r = await store.transition("nonexistent", Role.USER, Role.YUSHITAI)
    assert not r.ok
    assert "不存在" in r.reason
