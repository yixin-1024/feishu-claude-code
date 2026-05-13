"""三省体系 · Ticket 持久化（草稿）

每个 thread_id（一个话题）= 一条 ticket。
TicketStore 跨 profile 共享：5 个 bot（御史台/中书/门下/尚书/干活的）
都要读同一份 ticket 状态，所以这里**不像 SessionStore 那样按 profile 分文件**。

存储位置：~/.feishu-claude/tickets.json（和 sessions-*.json 同目录）
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from permissions import Role, State, validate_transition, TransitionResult

TICKETS_FILE = os.path.expanduser("~/.feishu-claude/tickets.json")


@dataclass
class TicketEvent:
    """一次状态转移的不可变记录。"""
    ts: str                  # ISO timestamp
    sender_role: str         # Role.value
    recipient_role: str
    from_state: Optional[str]
    to_state: str
    message_id: str = ""     # Lark message id（触发本次转移的消息）
    note: str = ""           # 可选附言（如驳回理由）


@dataclass
class Ticket:
    """一条 ticket = 一个话题。"""
    ticket_id: str                 # = chat_id:thread_id（沿用 session key 规则）
    chat_id: str                   # Lark chat_id（朝廷群）
    thread_id: str                 # Lark thread_id (omt_xxx)
    boss_open_id: str           # 业主（Boss）open_id
    state: str = State.TRIAGING.value   # 当前 state（用 Role/State.value 而非枚举，方便 json）
    summary: str = ""              # 御史台分诊时填的一行简介
    history: list[TicketEvent] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_json(self) -> dict:
        return {
            **asdict(self),
            "history": [asdict(e) for e in self.history],
        }

    @classmethod
    def from_json(cls, d: dict) -> "Ticket":
        history = [TicketEvent(**e) for e in d.pop("history", [])]
        return cls(history=history, **d)


class TicketStore:
    def __init__(self, path: str = TICKETS_FILE):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._lock = asyncio.Lock()
        self._data: dict[str, Ticket] = self._load()

    def _load(self) -> dict[str, Ticket]:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return {tid: Ticket.from_json(d) for tid, d in raw.items()}

    async def _save(self):
        async with self._lock:
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {tid: t.to_json() for tid, t in self._data.items()},
                    f, indent=2, ensure_ascii=False,
                )
            os.replace(tmp, self._path)

    def get(self, ticket_id: str) -> Optional[Ticket]:
        return self._data.get(ticket_id)

    async def create(
        self,
        ticket_id: str,
        chat_id: str,
        thread_id: str,
        boss_open_id: str,
        message_id: str = "",
        summary: str = "",
    ) -> Ticket:
        """Boss 第一次找御史台时创建。state 直接进 TRIAGING。"""
        now = datetime.now().isoformat()
        ticket = Ticket(
            ticket_id=ticket_id,
            chat_id=chat_id,
            thread_id=thread_id,
            boss_open_id=boss_open_id,
            state=State.TRIAGING.value,
            summary=summary,
            created_at=now,
            updated_at=now,
            history=[TicketEvent(
                ts=now,
                sender_role=Role.USER.value,
                recipient_role=Role.YUSHITAI.value,
                from_state=None,
                to_state=State.TRIAGING.value,
                message_id=message_id,
            )],
        )
        self._data[ticket_id] = ticket
        await self._save()
        return ticket

    async def transition(
        self,
        ticket_id: str,
        sender: Role,
        recipient: Role,
        message_id: str = "",
        note: str = "",
        action_hint: Optional[State] = None,
    ) -> TransitionResult:
        """尝试推进 ticket。校验失败时不改 state、不写 history、返回失败原因。"""
        ticket = self._data.get(ticket_id)
        if not ticket:
            return TransitionResult(ok=False, reason=f"ticket {ticket_id} 不存在")

        cur = State(ticket.state) if ticket.state else None
        result = validate_transition(sender, recipient, cur, action_hint)
        if not result.ok:
            return result

        now = datetime.now().isoformat()
        ticket.history.append(TicketEvent(
            ts=now,
            sender_role=sender.value,
            recipient_role=recipient.value,
            from_state=ticket.state,
            to_state=result.new_state.value,
            message_id=message_id,
            note=note,
        ))
        ticket.state = result.new_state.value
        ticket.updated_at = now
        await self._save()
        return result

    def current_owner(self, ticket_id: str) -> Optional[Role]:
        """当前 state 归谁管。bot 收到 @ 时校验：state 不属于我 → 拒绝。"""
        from permissions import STATE_OWNER
        ticket = self._data.get(ticket_id)
        if not ticket:
            return None
        try:
            return STATE_OWNER[State(ticket.state)]
        except (KeyError, ValueError):
            return None

    def render_history(self, ticket_id: str, limit: int = 20) -> str:
        """渲染近 N 条 history 为文本，给后续 bot 注入到 prompt 用。"""
        ticket = self._data.get(ticket_id)
        if not ticket:
            return ""
        lines = []
        for e in ticket.history[-limit:]:
            line = f"[{e.ts[:19]}] {e.sender_role} → {e.recipient_role} | {e.from_state or 'INIT'} → {e.to_state}"
            if e.note:
                line += f" | {e.note}"
            lines.append(line)
        return "\n".join(lines)


# ── 自检 ───────────────────────────────────────────────────────────

async def _self_test():
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as tmp:
        path = str(pathlib.Path(tmp) / "tickets.json")
        store = TicketStore(path)

        tid = "oc_xxx:omt_yyy"
        await store.create(
            ticket_id=tid,
            chat_id="oc_xxx",
            thread_id="omt_yyy",
            boss_open_id="ou_boss",
            message_id="om_init",
            summary="王女士开 SGB",
        )
        assert store.get(tid).state == "TRIAGING"
        assert store.current_owner(tid) == Role.YUSHITAI

        # 御史台派给中书
        r = await store.transition(tid, Role.YUSHITAI, Role.ZHONGSHU, "om_2")
        assert r.ok and r.new_state == State.DRAFTING, r
        assert store.current_owner(tid) == Role.ZHONGSHU

        # 中书 → 门下
        r = await store.transition(tid, Role.ZHONGSHU, Role.MENXIA, "om_3")
        assert r.ok, r

        # 门下驳回
        r = await store.transition(tid, Role.MENXIA, Role.ZHONGSHU, "om_4",
                                   note="SoF 没核实")
        assert r.ok and r.new_state == State.REJECTED

        # 中书重拟
        r = await store.transition(tid, Role.ZHONGSHU, Role.MENXIA, "om_5")
        assert r.ok and r.new_state == State.AUDITING

        # 非法转移测试
        r = await store.transition(tid, Role.GANHUODE, Role.SHANGSHU, "om_x")
        assert not r.ok
        assert "state 不允许" in r.reason or "非法" in r.reason, r.reason

        print("ticket_store self-test PASS")
        print("\nhistory:")
        print(store.render_history(tid))


if __name__ == "__main__":
    asyncio.run(_self_test())
