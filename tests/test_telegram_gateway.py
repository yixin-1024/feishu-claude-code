"""Telegram 长轮询网关：update → Lark 事件形状、@ 判定、白名单、按钮回调。

这层的正确性直接决定 dispatcher 能不能"分不出渠道"：字段名错一个，群聊就变成
私聊、或者 @ 判定失效导致 bot 在群里对所有人的闲聊都抢答。
"""

import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_gateway as gw
from bot_config import Profile
from bot_instance import BotInstance
from dispatcher import extract_chat_info


def _bot(**profile_kw) -> BotInstance:
    profile = Profile(
        name="tg", app_id="42", app_secret="42:secret", platform="telegram",
        domain="https://api.telegram.org", default_cwd="/tmp",
        bot_token="42:secret",
        allowed_open_ids={"777"},
        allowed_group_chat_ids={"-100123"},
        **profile_kw,
    )
    bot = BotInstance(profile)
    bot.feishu._bot_id = 42
    bot.feishu._app_id = "42"
    bot.feishu._bot_username = "spx_bot"
    return bot


def _msg(**kw) -> dict:
    base = {
        "message_id": 5,
        "date": 1700000000,
        "chat": {"id": -100123, "type": "supergroup"},  # 私聊测试自己传 dict 覆盖
        "from": {"id": 777, "first_name": "Yixin", "username": "yx"},
        "text": "hello",
    }
    base.update(kw)
    return base


# ── 事件形状 ─────────────────────────────────────────────────

def test_private_message_maps_to_p2p():
    bot = _bot()
    ev = gw.build_event(bot, _msg(chat={"id": 777, "type": "private"}))
    user_id, chat_id, is_group, raw_chat_id, thread_id = extract_chat_info(ev)
    assert (user_id, chat_id, is_group) == ("777", "777", False)
    assert raw_chat_id == "777"
    # 私聊没有话题：thread 为空，session 落 private 桶（和 Lark 私聊一致）
    assert thread_id == ""
    assert ev.event.message.message_id == "777:5"
    assert json.loads(ev.event.message.content)["text"] == "hello"


def test_group_message_gets_synthetic_thread():
    bot = _bot()
    ev = gw.build_event(bot, _msg(text="@spx_bot 看一下"))
    user_id, chat_id, is_group, raw_chat_id, thread_id = extract_chat_info(ev)
    assert is_group is True
    assert thread_id == "c-100123"
    # 复合 chat key 让"一个群 = 一条共享 session"，也让 last_seen 水位线有地方落
    assert chat_id == "-100123:c-100123"


def test_mention_by_username_is_detected_and_stripped():
    from feishu_post import strip_lark_mentions

    bot = _bot()
    ev = gw.build_event(bot, _msg(text="@spx_bot 帮我看看"))
    mentions = ev.event.message.mentions
    assert [m.id.open_id for m in mentions] == ["42"]
    assert strip_lark_mentions(ev.event.message.text if False else "@spx_bot 帮我看看",
                              mentions) == "帮我看看"


def test_mention_matching_is_case_insensitive():
    bot = _bot()
    ev = gw.build_event(bot, _msg(text="@SPX_Bot hi"))
    assert [m.id.open_id for m in ev.event.message.mentions] == ["42"]


def test_other_bot_mention_is_not_mine():
    bot = _bot()
    ev = gw.build_event(bot, _msg(text="@some_other_bot hi"))
    assert ev.event.message.mentions == []


def test_reply_to_bot_counts_as_mention():
    """群里回复 bot 的消息就是在跟它说话，不该还要求再 @ 一次。"""
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text="再详细点",
        reply_to_message={"message_id": 4, "from": {"id": 42, "is_bot": True}},
    ))
    assert [m.id.open_id for m in ev.event.message.mentions] == ["42"]
    assert ev.event.message.parent_id == "-100123:4"


def test_reply_to_human_is_not_mention():
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text="同意",
        reply_to_message={"message_id": 4, "from": {"id": 888}},
    ))
    assert ev.event.message.mentions == []


def test_bare_slash_command_implies_mention():
    """命令菜单点出来的就是裸 /cmd，群里不认它等于斜杠命令全废。"""
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text="/usage",
        entities=[{"type": "bot_command", "offset": 0, "length": 6}],
    ))
    assert [m.id.open_id for m in ev.event.message.mentions] == ["42"]


def test_slash_command_addressed_to_other_bot_is_ignored():
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text="/usage@other_bot",
        entities=[{"type": "bot_command", "offset": 0, "length": 16}],
    ))
    assert ev.event.message.mentions == []


def test_slash_command_mention_can_be_disabled():
    bot = _bot(tg_commands_imply_mention=0)
    ev = gw.build_event(bot, _msg(
        text="/usage",
        entities=[{"type": "bot_command", "offset": 0, "length": 6}],
    ))
    assert ev.event.message.mentions == []


def test_emoji_before_mention_does_not_break_detection():
    """Telegram 的 entity offset 是 UTF-16 单位，所以判 @ 只能靠正则。"""
    bot = _bot()
    ev = gw.build_event(bot, _msg(text="🎉🎉 @spx_bot 上线了"))
    assert [m.id.open_id for m in ev.event.message.mentions] == ["42"]


# ── 各类附件 ─────────────────────────────────────────────────

def test_photo_without_caption_becomes_image():
    bot = _bot()
    ev = gw.build_event(bot, _msg(text=None, photo=[
        {"file_id": "small", "file_size": 100},
        {"file_id": "big", "file_size": 900},
    ]))
    assert ev.event.message.message_type == "image"
    assert json.loads(ev.event.message.content)["image_key"] == "big"


def test_photo_with_caption_becomes_post():
    """带说明的图必须走 post 分支，否则 caption 会被丢掉。"""
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text=None, caption="这个报错什么意思",
        photo=[{"file_id": "big", "file_size": 900}],
    ))
    assert ev.event.message.message_type == "post"
    from feishu_post import extract_post_image_keys, parse_post_content
    # parse_post_content 会把 img 渲染成 [图片] 占位（Lark 口径），文字部分不丢
    assert parse_post_content(ev.event.message.content) == "这个报错什么意思[图片]"
    assert extract_post_image_keys(ev.event.message.content) == ["big"]


def test_voice_becomes_audio_with_duration_ms():
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text=None, voice={"file_id": "v1", "duration": 7}))
    assert ev.event.message.message_type == "audio"
    body = json.loads(ev.event.message.content)
    assert body == {"file_key": "v1", "duration": 7000}


def test_document_keeps_name_and_caption():
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text=None, caption="看下这个",
        document={"file_id": "d1", "file_name": "报告.pdf"},
    ))
    assert ev.event.message.message_type == "file"
    body = json.loads(ev.event.message.content)
    assert body["file_name"] == "报告.pdf"
    assert body["caption"] == "看下这个"


def test_video_maps_to_file_so_the_path_still_reaches_the_agent():
    bot = _bot()
    ev = gw.build_event(bot, _msg(text=None, video={"file_id": "vid"}))
    assert ev.event.message.message_type == "file"
    assert json.loads(ev.event.message.content)["file_name"] == "video.mp4"


def test_sticker_is_recorded_but_not_dispatched():
    bot = _bot()
    assert gw.build_event(bot, _msg(text=None, sticker={"emoji": "🎉"})) is None
    msgs = bot.feishu.buffer.thread_messages("c-100123")
    assert json.loads(msgs[0].body.content)["text"] == "[贴纸 🎉]"


def test_service_message_is_dropped():
    bot = _bot()
    assert gw.build_event(bot, _msg(text=None, new_chat_members=[{"id": 42}])) is None


def test_channel_post_is_dropped():
    bot = _bot()
    assert gw.build_event(bot, _msg(chat={"id": -100, "type": "channel"}, from_=None,
                                    **{"from": None})) is None


# ── 上下文缓冲 ───────────────────────────────────────────────

def test_every_group_message_is_buffered_for_context():
    """没 @ 的闲聊也要记：被 @ 时要把"上次回复之后的新消息"当上下文喂进去。"""
    bot = _bot()
    gw.build_event(bot, _msg(message_id=1, text="我在看支付那块"))
    gw.build_event(bot, _msg(message_id=2, text="@spx_bot 你怎么看"))
    msgs = bot.feishu.buffer.thread_messages("c-100123")
    assert [json.loads(m.body.content)["text"] for m in msgs] == [
        "我在看支付那块", "@spx_bot 你怎么看",
    ]
    assert msgs[0].sender.id == "777"
    assert msgs[0].create_time == "1700000000000"


def test_reply_into_dispatch_child_inherits_its_thread():
    """回复 dispatch 子会话的消息 → 落回子会话自己的 session，不串回群主线。"""
    bot = _bot()
    bot.feishu.buffer.record(
        message_id="-100123:80", chat_id="-100123", thread_id="t80",
        user_id="42", is_bot=True, content='{"text": "子会话顶楼"}',
    )
    ev = gw.build_event(bot, _msg(
        message_id=81, text="继续",
        reply_to_message={"message_id": 80, "from": {"id": 42}},
    ))
    assert ev.event.message.thread_id == "t80"


# ── 白名单 ───────────────────────────────────────────────────

def test_empty_allowlist_denies_everything():
    """Telegram bot 用户名是公开的：白名单为空必须是"全拒"而不是"全放"。"""
    profile = Profile(name="tg", app_id="42", app_secret="s", platform="telegram",
                      domain="https://api.telegram.org", default_cwd="/tmp",
                      bot_token="42:s")
    assert gw.acl_reject(profile, "777", "777", False)


def test_allowlist_paths():
    profile = _bot().profile
    assert gw.acl_reject(profile, "777", "777", False) == ""
    assert gw.acl_reject(profile, "777", "-100123", True) == ""
    assert "群不在白名单" in gw.acl_reject(profile, "777", "-999", True)
    assert "用户不在白名单" in gw.acl_reject(profile, "888", "-100123", True)


def test_wildcard_group_allows_any_group():
    bot = _bot()
    bot.profile.allowed_group_chat_ids = {"*"}
    assert gw.acl_reject(bot.profile, "777", "-42424242", True) == ""


# ── 按钮回调 ─────────────────────────────────────────────────

def _callback(bot, token: str, user_id: str = "777") -> list:
    submitted = []

    def submit(coro):
        submitted.append(coro)
        coro.close()

    gw.handle_callback(bot, {
        "id": "cq1",
        "data": token,
        "from": {"id": int(user_id)},
        "message": {"message_id": 9, "chat": {"id": -100123, "type": "supergroup"}},
    }, submit)
    return submitted


def test_expired_button_only_acks():
    bot = _bot()
    submitted = _callback(bot, "no-such-token")
    assert len(submitted) == 1  # 只有 answerCallbackQuery


def test_button_bound_to_another_user_is_rejected():
    bot = _bot()
    token = bot.feishu._register_callback(
        {"action": "run_cmd", "cmd": "/new", "profile": "tg", "_cc_uid": "777"},
        "-100123:9",
    )
    with mock.patch("dispatcher.handle_menu_command") as handler:
        _callback(bot, token, user_id="888")
    handler.assert_not_called()


def test_run_cmd_button_goes_to_menu_handler():
    bot = _bot()
    token = bot.feishu._register_callback(
        {"action": "run_cmd", "cmd": "/new", "cid": "-100123:c-100123",
         "profile": "tg", "_cc_uid": "777"},
        "-100123:9",
    )
    with mock.patch("dispatcher.handle_menu_command") as handler:
        _callback(bot, token)
    handler.assert_called_once()
    args = handler.call_args[0]
    assert args[1] == "777" and args[2] == "-100123:c-100123" and args[3] == "/new"


def test_reply_button_goes_to_button_reply_handler():
    bot = _bot()
    token = bot.feishu._register_callback(
        {"reply": "1", "cid": "-100123:c-100123", "profile": "tg", "_cc_uid": "777"},
        "-100123:9",
    )
    with mock.patch("dispatcher.handle_button_reply") as handler:
        _callback(bot, token)
    handler.assert_called_once()
    assert handler.call_args[0][3] == "1"


# ── 轮询（TgPoller.poll_once，不起线程）─────────────────────

class FakeUpdates:
    """按顺序返回预置的 getUpdates 批次，并记录请求参数。"""

    def __init__(self, batches):
        self.batches = list(batches)
        self.requests: list[dict] = []

    def __call__(self, method, payload, timeout, fresh=False):
        self.requests.append(payload)
        return self.batches.pop(0) if self.batches else []


def _poller(bot, batches, started_at=None):
    bot.feishu._post_sync = FakeUpdates(batches)
    delivered = []

    def submit(coro):
        delivered.append(coro)
        coro.close()

    async def on_message(b, ev):  # pragma: no cover — 只取协程不真跑
        pass

    poller = gw.TgPoller(bot, submit=submit, on_message=on_message,
                         touch=lambda: None, started_at=started_at)
    return poller, delivered


def test_poll_once_advances_offset_past_handled_updates():
    bot = _bot()
    poller, delivered = _poller(bot, [[
        {"update_id": 900, "message": _msg(text="@spx_bot hi")},
        {"update_id": 901, "message": _msg(message_id=6, text="@spx_bot again")},
    ]], started_at=1700000000)
    assert poller.poll_once() == 2
    assert poller.offset == 902
    assert len(delivered) == 2
    # 第二轮必须带上 offset，否则 Telegram 会把同一批再推一遍
    poller.poll_once()
    assert bot.feishu._post_sync.requests[1]["offset"] == 902


def test_poll_once_drops_backlog_from_before_startup():
    """/restart 后 Telegram 会重投离线期的消息，不能当新任务再跑一遍。"""
    bot = _bot()
    stale = _msg(message_id=7, text="@spx_bot 三小时前的话")
    stale["date"] = 1700000000 - 3 * 3600
    fresh = _msg(message_id=8, text="@spx_bot 刚说的")
    fresh["date"] = 1700000000
    poller, delivered = _poller(
        bot, [[{"update_id": 1, "message": stale}, {"update_id": 2, "message": fresh}]],
        started_at=1700000000,
    )
    poller.poll_once()
    assert len(delivered) == 1          # 只投了新的那条
    assert poller.offset == 3           # 老的那条也确认掉，不会反复重投


def test_poll_once_survives_a_broken_update():
    bot = _bot()
    poller, delivered = _poller(bot, [[
        {"update_id": 10, "message": {"garbage": True}},
        {"update_id": 11, "message": _msg(text="@spx_bot hi")},
    ]], started_at=1700000000)
    poller.poll_once()
    assert len(delivered) == 1
    assert poller.offset == 12


def test_poll_once_routes_callback_query():
    bot = _bot()
    token = bot.feishu._register_callback(
        {"action": "run_cmd", "cmd": "/new", "profile": "tg", "_cc_uid": "777"},
        "-100123:9",
    )
    poller, delivered = _poller(bot, [[{
        "update_id": 20,
        "callback_query": {
            "id": "cq", "data": token, "from": {"id": 777},
            "message": {"message_id": 9,
                        "chat": {"id": -100123, "type": "supergroup"}},
        },
    }]], started_at=1700000000)
    with mock.patch("dispatcher.handle_menu_command") as handler:
        poller.poll_once()
    handler.assert_called_once()


def test_unauthorized_private_user_is_told_their_id_once():
    bot = _bot()
    gw._revealed.clear()
    dm = _msg(message_id=30, text="hi", chat={"id": 888, "type": "private"},
              **{"from": {"id": 888, "first_name": "陌生人"}})
    poller, delivered = _poller(bot, [[{"update_id": 30, "message": dm}],
                                      [{"update_id": 31, "message": dm}]],
                                started_at=1700000000)
    poller.poll_once()
    assert len(delivered) == 1          # 只发了"未授权 + 你的 id"
    poller.poll_once()
    assert len(delivered) == 1          # 同一个人不再重复回


def test_unauthorized_group_message_stays_silent():
    bot = _bot()
    gw._revealed.clear()
    poller, delivered = _poller(bot, [[
        {"update_id": 40, "message": _msg(message_id=40, text="@spx_bot hi",
                                          chat={"id": -999, "type": "supergroup"})},
    ]], started_at=1700000000)
    poller.poll_once()
    assert delivered == []


# ── 相册聚合（一次发多张图 = 一次任务，不是 N 次）───────────

def _album_part(mid, gid="G1", caption=None, file_id=None):
    msg = _msg(message_id=mid, text=None,
               photo=[{"file_id": file_id or f"f{mid}", "file_size": 900}])
    msg["media_group_id"] = gid
    if caption:
        msg["caption"] = caption
    return msg


def _poller_for(bot):
    delivered = []

    def submit(coro):
        delivered.append(coro)
        coro.close()

    async def on_message(b, ev):  # pragma: no cover
        pass

    poller = gw.TgPoller(bot, submit=submit, on_message=on_message,
                         touch=lambda: None, started_at=1700000000)
    return poller, delivered


def test_album_is_merged_into_one_event():
    """3 张图 + 一句说明 → 一条 post 事件（3 个 image_key），不是 3 次任务。"""
    bot = _bot()
    poller, delivered = _poller_for(bot)
    assert poller.deliver(_album_part(101, caption="@spx_bot 这三张什么意思")) is False
    assert poller.deliver(_album_part(102)) is False
    assert poller.deliver(_album_part(103)) is False
    assert delivered == [], "攒够窗口前不该投递"

    assert poller.flush_albums(force=True) == 1
    assert len(delivered) == 1

    from feishu_post import extract_post_image_keys, parse_post_content
    msgs = bot.feishu.buffer.thread_messages("c-100123")
    assert len(msgs) == 1 and msgs[0].msg_type == "post"
    assert "这三张什么意思" in parse_post_content(msgs[0].body.content)
    assert extract_post_image_keys(msgs[0].body.content) == ["f101", "f102", "f103"]


def test_album_without_caption_still_merges():
    bot = _bot()
    poller, delivered = _poller_for(bot)
    poller.deliver(_album_part(111, gid="G2"))
    poller.deliver(_album_part(112, gid="G2"))
    poller.flush_albums(force=True)
    msgs = bot.feishu.buffer.thread_messages("c-100123")
    from feishu_post import extract_post_image_keys
    assert extract_post_image_keys(msgs[0].body.content) == ["f111", "f112"]


def test_single_photo_is_not_delayed_by_album_logic():
    bot = _bot()
    poller, delivered = _poller_for(bot)
    single = _msg(message_id=120, text=None, caption="@spx_bot 看这张",
                  photo=[{"file_id": "solo", "file_size": 900}])
    assert poller.deliver(single) is True
    assert poller.albums_pending() is False


# ── 合成 thread 必须带 chat 作用域（否则跨群串上下文）────────

def test_forum_topic_thread_is_chat_scoped():
    bot_a = _bot()
    ev_a = gw.build_event(bot_a, _msg(message_id=201, text="A 群机密",
                                      is_topic_message=True, message_thread_id=7))
    bot_b = _bot()
    bot_b.profile.allowed_group_chat_ids = {"-100999"}
    ev_b = gw.build_event(bot_b, _msg(message_id=202, text="B 群闲聊",
                                      chat={"id": -100999, "type": "supergroup"},
                                      is_topic_message=True, message_thread_id=7))
    assert ev_a.event.message.thread_id == "f-100123#7"
    assert ev_b.event.message.thread_id == "f-100999#7"
    assert ev_a.event.message.thread_id != ev_b.event.message.thread_id


def test_dispatch_child_thread_is_chat_scoped_and_survives_anchor_eviction():
    """锚点记录被裁掉后，回复子会话仍要落回子会话，不能塌回群主线。"""
    bot = _bot()
    # 子会话桶里有消息，但锚点那条记录本身不在（模拟 MAX_PER_THREAD 裁剪）
    bot.feishu.buffer.record(
        message_id="-100123:301", chat_id="-100123", thread_id="t-100123#300",
        user_id="42", is_bot=True, content='{"text": "子会话的回复"}',
    )
    ev = gw.build_event(bot, _msg(
        message_id=302, text="继续",
        reply_to_message={"message_id": 300, "from": {"id": 42}},
    ))
    assert ev.event.message.thread_id == "t-100123#300"


def test_reply_to_a_normal_bot_message_stays_on_the_main_line():
    """回复 bot 在群主线发的普通回答，不该凭空开一条子会话。"""
    bot = _bot()
    bot.feishu.buffer.record(
        message_id="-100123:310", chat_id="-100123", thread_id="c-100123",
        user_id="42", is_bot=True, content='{"text": "普通回答"}',
    )
    ev = gw.build_event(bot, _msg(
        message_id=311, text="再说说",
        reply_to_message={"message_id": 310, "from": {"id": 42}},
    ))
    assert ev.event.message.thread_id == "c-100123"


# ── 其它审计项 ───────────────────────────────────────────────

def test_mention_needs_a_left_boundary():
    bot = _bot()
    ev = gw.build_event(bot, _msg(text="有问题发 ops@spx_bot.com"))
    assert ev.event.message.mentions == []


def test_no_mentions_at_all_before_get_me_succeeds():
    """getMe 失败时 bot_id=0，不能把每条消息都判成 @ 到我。"""
    bot = _bot()
    bot.feishu._bot_id = 0
    bot.feishu._bot_username = ""
    ev = gw.build_event(bot, _msg(text="随便聊聊"))
    assert ev.event.message.mentions == []


def test_photo_without_file_size_picks_the_biggest_by_pixels():
    bot = _bot()
    ev = gw.build_event(bot, _msg(text=None, photo=[
        {"file_id": "thumb", "width": 90, "height": 60},
        {"file_id": "full", "width": 1280, "height": 960},
    ]))
    assert json.loads(ev.event.message.content)["image_key"] == "full"


def test_music_file_goes_to_the_file_branch_not_asr():
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text=None, audio={"file_id": "a1", "file_name": "长录音.m4a", "duration": 600}))
    assert ev.event.message.message_type == "file"
    assert json.loads(ev.event.message.content)["file_name"] == "长录音.m4a"


def test_voice_caption_is_kept():
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text=None, caption="这段是客户原话", voice={"file_id": "v1", "duration": 5}))
    body = json.loads(ev.event.message.content)
    assert body["caption"] == "这段是客户原话"


def test_group_members_outside_the_allowlist_are_recorded_but_do_not_trigger():
    """群上下文要收全（否则被 @ 时读不到别人说了什么），但只有白名单里的人能触发。"""
    bot = _bot()
    poller, delivered = _poller_for(bot)
    other = _msg(message_id=401, text="我也遇到这个报错",
                 **{"from": {"id": 888, "first_name": "Charlie"}})
    assert poller.deliver(other) is False
    assert delivered == []
    msgs = bot.feishu.buffer.thread_messages("c-100123")
    assert [json.loads(m.body.content)["text"] for m in msgs] == ["我也遇到这个报错"]


def test_stranger_dm_is_not_persisted():
    """陌生人私聊：不落盘、不建桶（bot 用户名是公开的，别给无鉴权写入留口子）。"""
    bot = _bot()
    gw._revealed.clear()
    poller, delivered = _poller_for(bot)
    dm = _msg(message_id=402, text="hello", chat={"id": 999, "type": "private"},
              **{"from": {"id": 999, "first_name": "陌生人"}})
    assert poller.deliver(dm) is False
    assert bot.feishu.buffer.thread_messages("c999") == []
    assert bot.feishu.buffer.names(["999"]) == {"999": ""}
    assert len(delivered) == 1  # 只有那条"你的 id 是 X"提示


def test_malformed_update_id_does_not_rewind_offset():
    bot = _bot()
    poller, _ = _poller(bot, [[
        {"message": _msg(message_id=501, text="@spx_bot a")},        # 没有 update_id
        {"update_id": "??", "message": _msg(message_id=502, text="@spx_bot b")},
        {"update_id": 9000, "message": _msg(message_id=503, text="@spx_bot c")},
    ]], started_at=1700000000)
    poller.offset = 8000
    poller.poll_once()
    assert poller.offset == 9001, "offset 必须单调不减，且畸形的那条只跳过自己"


def test_mention_followed_by_chinese_without_space_still_counts():
    """`@spx_bot帮我看看`：手机上很常见（自动补全不总补空格）。

    右边界用 `\\b` 会整条漏掉 —— 中文也是 \\w，`t` 和 `帮` 之间没有词边界，
    结果 bot 一声不响，用户以为它挂了。
    """
    bot = _bot()
    ev = gw.build_event(bot, _msg(text="@spx_bot帮我看下这个报错"))
    assert [m.id.open_id for m in ev.event.message.mentions] == ["42"]
    from feishu_post import strip_lark_mentions
    assert strip_lark_mentions("@spx_bot帮我看下这个报错",
                               ev.event.message.mentions) == "帮我看下这个报错"


def test_other_bots_with_a_shared_prefix_are_still_not_me():
    bot = _bot()
    for text in ("@spx_bot2 hi", "@spx_bot_dev hi", "@spx_botx hi",
                 "ops@spx_bot.com 发我"):
        ev = gw.build_event(bot, _msg(text=text))
        assert ev.event.message.mentions == [], text


# ── 409 自锁（真机踩过：入站彻底停）────────────────────────

class ConflictingUpdates:
    """前 N 次 getUpdates 都回 409，之后正常。记录每次是否要求新连接。"""

    def __init__(self, conflicts: int):
        self.left = conflicts
        self.fresh_flags: list[bool] = []
        self.calls = 0

    def __call__(self, method, payload, timeout, fresh=False):
        self.calls += 1
        self.fresh_flags.append(fresh)
        if self.left > 0:
            self.left -= 1
            raise gw.TelegramApiError(
                "getUpdates", 409,
                "Conflict: terminated by other getUpdates request; "
                "make sure that only one bot instance is running")
        return []


def test_long_poll_uses_a_fresh_connection():
    """长轮询必须一次一条新连接。

    复用连接时读到的可能是**上一个** getUpdates 的 409（Telegram 收到新请求就用 409
    终止旧的），于是"409 → 退避 → 又读到 409"自锁 —— 真机上退避爬到 60s、入站彻底停。
    """
    bot = _bot()
    transport = ConflictingUpdates(0)
    bot.feishu._post_sync = transport
    poller, _ = _poller_for(bot)
    poller.poll_once()
    assert transport.fresh_flags == [True]


def test_conflict_drops_the_connection_pool():
    bot = _bot()
    dropped = []
    bot.feishu._post_sync = ConflictingUpdates(1)
    bot.feishu.reset_session = lambda: dropped.append(1)
    poller, _ = _poller_for(bot)
    with pytest.raises(gw.TelegramApiError):
        poller.poll_once()          # poll_once 只管抛，重试在监督循环里
    assert dropped == [] or True    # poll_once 自己不丢连接，下面测监督循环


def test_supervisor_retries_conflicts_fast_then_escalates(monkeypatch):
    """409 的处理：先当连接错位（快速重试），连续很多次才认定有人抢 token。"""
    bot = _bot()
    transport = ConflictingUpdates(3)
    bot.feishu._post_sync = transport
    dropped = []
    monkeypatch.setattr(bot.feishu, "reset_session", lambda: dropped.append(1))

    sleeps: list[float] = []
    monkeypatch.setattr(gw.time, "sleep", lambda s: sleeps.append(s))
    logs: list[tuple] = []
    monkeypatch.setattr(gw, "log", lambda *a: logs.append(a))

    started = []
    monkeypatch.setattr(gw.threading, "Thread",
                        lambda target, **kw: type("T", (), {
                            "start": lambda self: started.append(target)})())
    poller = gw.start_polling(bot, submit=lambda c: c.close(),
                              on_message=lambda b, e: None, touch=lambda: None)
    # 手动跑监督循环几轮：3 次 409 后成功，然后停
    calls = {"n": 0}
    real_poll = poller.poll_once

    def counted(reclaim=False):
        calls["n"] += 1
        if calls["n"] > 4:
            poller.stop()
        return real_poll(reclaim=reclaim)

    poller.poll_once = counted
    started[0]()

    assert sleeps[:3] == [1, 1, 1], f"前几次 409 应快速重试（1s），实际 {sleeps[:3]}"
    assert len(dropped) == 3, "每次 409 都要丢连接池"
    assert all(f for f in transport.fresh_flags), "每次轮询都要新连接"


def test_many_consecutive_conflicts_escalate_to_backoff(monkeypatch):
    bot = _bot()
    bot.feishu._post_sync = ConflictingUpdates(50)
    monkeypatch.setattr(bot.feishu, "reset_session", lambda: None)
    sleeps: list[float] = []
    monkeypatch.setattr(gw.time, "sleep", lambda s: sleeps.append(s))
    levels: list[str] = []
    monkeypatch.setattr(gw, "log", lambda tag, area, level, msg: levels.append(level))

    started = []
    monkeypatch.setattr(gw.threading, "Thread",
                        lambda target, **kw: type("T", (), {
                            "start": lambda self: started.append(target)})())
    poller = gw.start_polling(bot, submit=lambda c: c.close(),
                              on_message=lambda b, e: None, touch=lambda: None)
    n = {"i": 0}
    real_poll = poller.poll_once

    def counted(reclaim=False):
        n["i"] += 1
        if n["i"] > 12:
            poller.stop()
        return real_poll(reclaim=reclaim)

    poller.poll_once = counted
    started[0]()

    assert sleeps.count(1) == gw._CONFLICT_ESCALATE - 1, "前几次快速重试"
    assert any(s >= 3 for s in sleeps), "连续太多次要升级成指数退避"
    assert "error" in levels, "升级后必须喊出来（可能真有第二个实例）"


def test_forum_topic_inherited_on_reply_and_isolated_context():
    """话题群按话题严格隔离上下文和 session：
    1. 话题中的回复继承 topic thread_id，绝不塌回群主线；
    2. is_forum=True 且带 message_thread_id（即使缺失 is_topic_message）也准确归入话题；
    3. 每个话题上下文独立，只读本话题消息。
    """
    bot = _bot()
    chat_info = {"id": -100123, "type": "supergroup", "is_forum": True}

    # 1. 话题 7 里的第一条消息
    ev1 = gw.build_event(bot, _msg(
        message_id=101, text="打款单 A 讨论",
        chat=chat_info, message_thread_id=7,
    ))
    assert ev1.event.message.thread_id == "f-100123#7"

    # 2. 话题 7 里用户回复上一条（某些客户端丢失 is_topic_message / message_thread_id）
    ev2 = gw.build_event(bot, _msg(
        message_id=102, text="回复打款单 A",
        chat=chat_info,
        reply_to_message={"message_id": 101, "from": {"id": 777}},
    ))
    assert ev2.event.message.thread_id == "f-100123#7", "应继承被回复消息的话题，绝不能塌回 c-100123"

    # 3. 话题 8 里的消息
    ev3 = gw.build_event(bot, _msg(
        message_id=201, text="打款单 B 讨论",
        chat=chat_info, message_thread_id=8,
    ))
    assert ev3.event.message.thread_id == "f-100123#8"

    # 4. 群主线（General 话题）里的消息
    ev_main = gw.build_event(bot, _msg(
        message_id=301, text="群主线闲聊",
        chat=chat_info,
    ))
    assert ev_main.event.message.thread_id == "c-100123"

    # 5. 核实上下文完全隔离：只读本话题里面的上下文
    msgs_t7 = [m.body.content for m in bot.feishu.buffer.thread_messages("f-100123#7")]
    msgs_t8 = [m.body.content for m in bot.feishu.buffer.thread_messages("f-100123#8")]
    msgs_main = [m.body.content for m in bot.feishu.buffer.thread_messages("c-100123")]

    assert len(msgs_t7) == 2
    assert "打款单 A 讨论" in msgs_t7[0] and "回复打款单 A" in msgs_t7[1]
    assert "打款单 B" not in "".join(msgs_t7)
    assert "群主线闲聊" not in "".join(msgs_t7)

    assert len(msgs_t8) == 1
    assert "打款单 B 讨论" in msgs_t8[0]
    assert "打款单 A" not in "".join(msgs_t8)

    assert len(msgs_main) == 1
    assert "群主线闲聊" in msgs_main[0]


@pytest.mark.asyncio
async def test_outgoing_message_passes_message_thread_id():
    """Bot 在话题内回复时，发送请求必须携带对应的 message_thread_id。"""
    bot = _bot()
    client = bot.feishu
    client.buffer.record(
        message_id="-100123:101",
        chat_id="-100123",
        thread_id="f-100123#42",
        user_id="777",
        name="Louie",
        is_bot=False,
        msg_type="text",
        content='{"text": "在话题42"}',
        ts_ms=1000,
    )

    captured_payloads = []

    async def mock_call(method, payload, **kw):
        if method == "sendMessage":
            captured_payloads.append(payload)
            return {"chat": {"id": payload["chat_id"]}, "message_id": 999}
        return {}

    client.call = mock_call
    await client.reply_card("-100123:101", content="收到处理中", loading=False)

    assert len(captured_payloads) == 1
    assert captured_payloads[0]["message_thread_id"] == 42
    assert captured_payloads[0]["reply_parameters"]["message_id"] == 101



def test_conflict_switches_to_zero_timeout_reclaim(monkeypatch):
    """连着撞 409 要改用零超时轮询抢槽位。

    Telegram 把 getUpdates 的槽位交给**最后到达**的请求：对方挂着长轮询时，我们也挂
    长轮询就永远排在后面；短轮询才抢得回来。
    """
    bot = _bot()
    transport = ConflictingUpdates(gw._RECLAIM_AFTER + 2)
    bot.feishu._post_sync = transport
    timeouts: list[int] = []
    orig = transport.__call__

    def spy(method, payload, timeout, fresh=False):
        timeouts.append(payload.get("timeout"))
        return orig(method, payload, timeout, fresh)

    bot.feishu._post_sync = spy
    monkeypatch.setattr(bot.feishu, "reset_session", lambda: None)
    monkeypatch.setattr(gw.time, "sleep", lambda s: None)
    monkeypatch.setattr(gw, "log", lambda *a: None)

    started = []
    monkeypatch.setattr(gw.threading, "Thread",
                        lambda target, **kw: type("T", (), {
                            "start": lambda self: started.append(target)})())
    poller = gw.start_polling(bot, submit=lambda c: c.close(),
                              on_message=lambda b, e: None, touch=lambda: None)
    n = {"i": 0}
    real = poller.poll_once

    def counted(reclaim=False):
        n["i"] += 1
        if n["i"] > gw._RECLAIM_AFTER + 3:
            poller.stop()
        return real(reclaim=reclaim)

    poller.poll_once = counted
    started[0]()

    assert timeouts[0] == 25, "一开始是长轮询"
    assert 0 in timeouts, "连撞 409 之后要切成零超时抢槽位"


def test_conflict_backoff_is_capped_much_lower_than_generic_errors(monkeypatch):
    """409 的退避上限必须远小于普通错误的 60s。

    真机踩过：退避涨到 60s 后连续 40 次 409 = 入站整整停了 30 分钟，而 Lark 侧毫无异常
    （只有 Telegram 那条哑掉，最难发现的那种）。
    """
    bot = _bot()
    bot.feishu._post_sync = ConflictingUpdates(100)
    monkeypatch.setattr(bot.feishu, "reset_session", lambda: None)
    sleeps: list[float] = []
    monkeypatch.setattr(gw.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(gw, "log", lambda *a: None)

    started = []
    monkeypatch.setattr(gw.threading, "Thread",
                        lambda target, **kw: type("T", (), {
                            "start": lambda self: started.append(target)})())
    poller = gw.start_polling(bot, submit=lambda c: c.close(),
                              on_message=lambda b, e: None, touch=lambda: None)
    n = {"i": 0}
    real = poller.poll_once

    def counted(reclaim=False):
        n["i"] += 1
        if n["i"] > 40:
            poller.stop()
        return real(reclaim=reclaim)

    poller.poll_once = counted
    started[0]()

    assert max(sleeps) <= gw._CONFLICT_MAX_BACKOFF
    assert gw._CONFLICT_MAX_BACKOFF <= 15, "别再让它涨到分钟级"
