"""三省体系：bot↔bot 调度的识别 + transition 校验入口。

`main.handle_message_async` 在判断完群白名单 / 用户白名单之后会调
`maybe_handle_trinity()`，由本模块决定：
    - 这条消息是不是 trinity 体系内的（profile 配了 role）
    - 发件人是 Boss 还是上游 bot
    - 当前 ticket state 是否允许此次调度
    - 通过则推进 ticket、返回上下文（注入到 Claude prompt）
    - 拒绝则发一条短消息回话题群，并 return（不进 Claude session）

设计原则：本模块**只做识别 + 校验 + 状态推进**，不发 Lark 消息（除拒绝路径）；
prompt 由 lark_prompts.render_lark_prompt 用本模块返回的 TrinityContext 渲染。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import bot_config
from bot_config import Profile
from permissions import Role, State, STATE_OWNER, validate_transition
from ticket_store import TicketStore, Ticket
from log_util import log


# 进程内单例：所有 5 个 profile 共享同一份 ticket 状态
_ticket_store_singleton: Optional[TicketStore] = None


def get_ticket_store() -> TicketStore:
    """懒加载单例。"""
    global _ticket_store_singleton
    if _ticket_store_singleton is None:
        _ticket_store_singleton = TicketStore()
    return _ticket_store_singleton


def ticket_id_for(chat_id_raw: str, thread_id: str) -> str:
    """ticket_id 复用 session_key 规则：chat:thread。"""
    return f"{chat_id_raw}:{thread_id}" if thread_id else chat_id_raw


def identify_sender(profile: Profile, sender_open_id: str) -> Optional[Role]:
    """根据发件人 open_id 判断角色。

    优先级：
        1. 在 PROFILES_BY_BOT_OPEN_ID 里 → 对应 profile 的 role
        2. 等于 profile.boss_open_id → Role.USER
        3. 其他 → None（普通用户消息，走原路径）
    """
    sender_profile = bot_config.PROFILES_BY_BOT_OPEN_ID.get(sender_open_id)
    if sender_profile and sender_profile.role:
        try:
            return Role(sender_profile.role)
        except ValueError:
            return None

    if sender_open_id and sender_open_id == profile.boss_open_id:
        return Role.USER

    return None


@dataclass
class TrinityContext:
    """trinity 路径校验通过后注入到 prompt 的上下文。"""
    ticket: Ticket
    sender_role: Role
    recipient_role: Role
    new_state: State
    history_text: str


@dataclass
class TrinityDecision:
    """maybe_handle_trinity 的返回结果。

    handled=False, context=None  → 这条消息不在 trinity 路径，走原路径
    handled=True,  context=ctx   → trinity 路径，已推进 ticket，prompt 用 ctx
    handled=True,  context=None  → trinity 路径，但被拒绝（reason 已发回话题），return
    """
    handled: bool
    context: Optional[TrinityContext] = None
    reject_reason: str = ""


async def maybe_handle_trinity(
    profile: Profile,
    sender_open_id: str,
    chat_id_raw: str,
    thread_id: str,
    message_id: str,
) -> TrinityDecision:
    """trinity 路径分发入口。

    在 handle_message_async 里这样用：

        decision = await maybe_handle_trinity(bot.profile, user_id, raw_chat_id, thread_id, msg_id)
        if decision.reject_reason:
            await bot.feishu.reply_text(msg_id, decision.reject_reason)
            return
        # decision.context 非空时，把它注入到 _process_message 的 prompt 上下文

    本模块返回 reject_reason 而不直接发 Lark 消息，避免对 FeishuClient 的依赖。
    """
    if not profile.is_trinity:
        return TrinityDecision(handled=False)

    sender_role = identify_sender(profile, sender_open_id)
    if sender_role is None:
        # 既不是 Boss 也不是同体系 bot —— 拒绝（trinity bot 只接受这两类发件人）
        log(profile.name, "trinity", "warn",
            f"非授权发件人 sender={sender_open_id[:14]}...，trinity bot 不响应")
        return TrinityDecision(handled=True, reject_reason="")  # 静默忽略，不暴露 bot 存在

    try:
        my_role = Role(profile.role)
    except ValueError:
        log(profile.name, "trinity", "error", f"profile.role={profile.role!r} 不是合法角色")
        return TrinityDecision(handled=False)

    tid = ticket_id_for(chat_id_raw, thread_id)
    store = get_ticket_store()
    ticket = store.get(tid)

    # 情景 1: Boss 找 yushitai 起新 ticket（state=None → TRIAGING）
    if sender_role == Role.USER and ticket is None:
        if my_role != Role.YUSHITAI:
            return TrinityDecision(
                handled=True,
                reject_reason=f"⚠️ 我是【{_zh(my_role)}】，不直接对接皇帝。请去找御史台。",
            )
        ticket = await store.create(
            ticket_id=tid,
            chat_id=chat_id_raw,
            thread_id=thread_id,
            boss_open_id=sender_open_id,
            message_id=message_id,
        )
        log(profile.name, "trinity", "info",
            f"create ticket {tid[:24]}... by Boss")
        return TrinityDecision(handled=True, context=TrinityContext(
            ticket=ticket,
            sender_role=Role.USER,
            recipient_role=my_role,
            new_state=State.TRIAGING,
            history_text=store.render_history(tid),
        ))

    # 情景 2: ticket 已存在，校验 transition
    if ticket is None:
        # 没有 ticket 也不是 Boss 起新单（比如某个 bot 在新话题里跨级发消息）
        return TrinityDecision(
            handled=True,
            reject_reason=f"⚠️ 这个话题没有 ticket，但 {sender_role.value} 不能在这里起新单。",
        )

    result = await store.transition(
        ticket_id=tid,
        sender=sender_role,
        recipient=my_role,
        message_id=message_id,
    )
    if not result.ok:
        log(profile.name, "trinity", "warn",
            f"reject transition {sender_role.value}→{my_role.value}: {result.reason}")
        return TrinityDecision(
            handled=True,
            reject_reason=f"⚠️ 非法转移：{result.reason}",
        )

    log(profile.name, "trinity", "info",
        f"transition ok {sender_role.value}→{my_role.value}, ticket={tid[:24]}... state={result.new_state.value}")
    return TrinityDecision(handled=True, context=TrinityContext(
        ticket=ticket,
        sender_role=sender_role,
        recipient_role=my_role,
        new_state=result.new_state,
        history_text=store.render_history(tid),
    ))


_ZH = {
    Role.YUSHITAI: "御史台",
    Role.ZHONGSHU: "中书",
    Role.MENXIA: "门下",
    Role.SHANGSHU: "尚书",
    Role.GANHUODE: "干活的",
    Role.USER: "皇帝",
}


def _zh(role: Role) -> str:
    return _ZH.get(role, role.value)
