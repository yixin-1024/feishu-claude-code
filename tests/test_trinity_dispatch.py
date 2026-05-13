"""trinity_dispatch 单测：bot↔bot 调度的识别 + 校验逻辑。

不走 Lark WS 路径，直接调 maybe_handle_trinity，验证：
- Boss 找御史台 → 创建 ticket
- 找错角色（中书直接接 Boss）→ 被拒绝
- 跨级 @（中书直接找尚书）→ 被拒绝
- 上行链路按顺序推进
"""

import pytest

import bot_config
import trinity_dispatch as td
from bot_config import Profile
from permissions import Role, State


@pytest.fixture(autouse=True)
def _reset_trinity_singleton(tmp_path, monkeypatch):
    """每个测试用独立的 TicketStore 文件，避免互相污染。"""
    from ticket_store import TicketStore
    monkeypatch.setattr(td, "_ticket_store_singleton", TicketStore(path=str(tmp_path / "tickets.json")))
    yield


def _trinity_profile(role: str) -> Profile:
    return Profile(
        name=role,
        app_id=f"cli_{role}",
        app_secret="s",
        platform="lark",
        domain="open.larksuite.com",
        default_cwd="/tmp",
        role=role,
        bot_open_id=f"ou_{role}_bot",
        court_chat_id="oc_court",
        yushitai_open_id="ou_yushitai_bot",
        zhongshu_open_id="ou_zhongshu_bot",
        menxia_open_id="ou_menxia_bot",
        shangshu_open_id="ou_shangshu_bot",
        ganhuode_open_id="ou_ganhuode_bot",
        boss_open_id="ou_boss",
    )


@pytest.fixture
def all_profiles(monkeypatch):
    """注册 5 个 trinity profile 到 PROFILES_BY_BOT_OPEN_ID。"""
    profiles = {role: _trinity_profile(role) for role in
                ["yushitai", "zhongshu", "menxia", "shangshu", "ganhuode"]}
    monkeypatch.setattr(
        bot_config, "PROFILES_BY_BOT_OPEN_ID",
        {p.bot_open_id: p for p in profiles.values()},
    )
    return profiles


async def test_boss_to_yushitai_creates_ticket(all_profiles):
    """Boss 第一次找御史台 → 创建 ticket，进入 TRIAGING。"""
    decision = await td.maybe_handle_trinity(
        profile=all_profiles["yushitai"],
        sender_open_id="ou_boss",
        chat_id_raw="oc_court",
        thread_id="omt_new",
        message_id="om_1",
    )
    assert decision.handled
    assert decision.context is not None
    assert decision.context.new_state == State.TRIAGING
    assert decision.context.sender_role == Role.USER
    assert decision.context.recipient_role == Role.YUSHITAI


async def test_boss_to_zhongshu_rejected(all_profiles):
    """Boss 不能直接找中书——只有御史台对接 Boss。"""
    decision = await td.maybe_handle_trinity(
        profile=all_profiles["zhongshu"],
        sender_open_id="ou_boss",
        chat_id_raw="oc_court",
        thread_id="omt_x",
        message_id="om_1",
    )
    assert decision.handled
    assert decision.reject_reason
    assert "御史台" in decision.reject_reason


async def test_full_downward_chain(all_profiles):
    """御史台 → 中书 → 门下 → 尚书 → 干活的，每跳推进 state。"""
    tid_args = dict(chat_id_raw="oc_court", thread_id="omt_chain")

    # Boss 起 ticket
    await td.maybe_handle_trinity(
        profile=all_profiles["yushitai"],
        sender_open_id="ou_boss",
        message_id="om_1",
        **tid_args,
    )

    # 御史台 → 中书
    d = await td.maybe_handle_trinity(
        profile=all_profiles["zhongshu"],
        sender_open_id="ou_yushitai_bot",
        message_id="om_2",
        **tid_args,
    )
    assert d.context.new_state == State.DRAFTING

    # 中书 → 门下
    d = await td.maybe_handle_trinity(
        profile=all_profiles["menxia"],
        sender_open_id="ou_zhongshu_bot",
        message_id="om_3",
        **tid_args,
    )
    assert d.context.new_state == State.AUDITING

    # 门下 → 尚书
    d = await td.maybe_handle_trinity(
        profile=all_profiles["shangshu"],
        sender_open_id="ou_menxia_bot",
        message_id="om_4",
        **tid_args,
    )
    assert d.context.new_state == State.DISPATCHED

    # 尚书 → 干活的
    d = await td.maybe_handle_trinity(
        profile=all_profiles["ganhuode"],
        sender_open_id="ou_shangshu_bot",
        message_id="om_5",
        **tid_args,
    )
    assert d.context.new_state == State.DOING


async def test_cross_level_rejected(all_profiles):
    """中书直接 @ 尚书 → 权限矩阵拒绝。"""
    tid_args = dict(chat_id_raw="oc_court", thread_id="omt_cross")

    await td.maybe_handle_trinity(
        profile=all_profiles["yushitai"],
        sender_open_id="ou_boss",
        message_id="om_1",
        **tid_args,
    )
    await td.maybe_handle_trinity(
        profile=all_profiles["zhongshu"],
        sender_open_id="ou_yushitai_bot",
        message_id="om_2",
        **tid_args,
    )

    # 中书直接找尚书，应被拒绝
    d = await td.maybe_handle_trinity(
        profile=all_profiles["shangshu"],
        sender_open_id="ou_zhongshu_bot",
        message_id="om_3",
        **tid_args,
    )
    assert d.handled
    assert d.reject_reason
    assert "非法" in d.reject_reason or "state" in d.reject_reason.lower()


async def test_unknown_sender_silently_ignored(all_profiles):
    """既不是 Boss 也不是 trinity bot → 静默忽略（不发拒绝消息）。"""
    d = await td.maybe_handle_trinity(
        profile=all_profiles["yushitai"],
        sender_open_id="ou_random_user",
        chat_id_raw="oc_court",
        thread_id="omt_x",
        message_id="om_1",
    )
    assert d.handled
    assert d.context is None
    assert d.reject_reason == ""


async def test_non_trinity_profile_skipped():
    """profile 没配 role → 不走 trinity 路径。"""
    plain = Profile(
        name="plain", app_id="x", app_secret="x",
        platform="lark", domain="open.larksuite.com", default_cwd="/tmp",
    )
    d = await td.maybe_handle_trinity(
        profile=plain,
        sender_open_id="ou_someone",
        chat_id_raw="oc_x",
        thread_id="",
        message_id="om_x",
    )
    assert not d.handled
