"""telegram_gateway 对抗性审计：把发现的缺陷钉死，并给「已确认安全」的路径上锁。

命名约定
    test_bug_*   —— 真缺陷。断言的是**期望行为**，标 xfail(strict=False)：
                    产品代码修好后自动变 XPASS，不用回来改测试。
    test_safe_*  —— 审计确认没问题的路径（ACL / offset 推进 / 词边界 / 类型映射），
                    当回归锁用，防以后重构把这几条踩坏。

审计范围只有 telegram_gateway.py；牵连到 telegram_client / tg_context 的地方
在对应 test 的 docstring 里点名，但不改那两个文件。
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


# ── fixtures ────────────────────────────────────────────────

def _bot(*, users=("777",), groups=("-100123", "-100999"), bot_id=42,
         username="spx_bot", **profile_kw) -> BotInstance:
    profile = Profile(
        name="tg", app_id="42", app_secret="42:secret", platform="telegram",
        domain="https://api.telegram.org", default_cwd="/tmp", bot_token="42:secret",
        allowed_open_ids=set(users), allowed_group_chat_ids=set(groups),
        **profile_kw,
    )
    bot = BotInstance(profile)
    bot.feishu._bot_id = bot_id
    bot.feishu._app_id = str(bot_id)
    bot.feishu._bot_username = username
    return bot


def _msg(**kw) -> dict:
    base = {
        "message_id": 5,
        "date": 1700000000,
        "chat": {"id": -100123, "type": "supergroup"},
        "from": {"id": 777, "first_name": "Yixin"},
        "text": "hello",
    }
    base.update(kw)
    return base


class _FakeUpdates:
    def __init__(self, batches):
        self.batches = list(batches)
        self.requests: list[dict] = []

    def __call__(self, method, payload, timeout, fresh=False):
        self.requests.append(payload)
        return self.batches.pop(0) if self.batches else []


def _poller(bot, batches, started_at=1700000000):
    bot.feishu._post_sync = _FakeUpdates(batches)
    submitted = []

    def submit(coro):
        submitted.append(coro)
        coro.close()

    async def on_message(b, ev):  # pragma: no cover — 只取协程不真跑
        pass

    poller = gw.TgPoller(bot, submit=submit, on_message=on_message,
                         touch=lambda: None, started_at=started_at)
    return poller, submitted


def _texts(bot, thread_id):
    return [
        (m.chat_id, json.loads(m.body.content).get("text", ""))
        for m in bot.feishu.buffer.thread_messages(thread_id)
    ]


# ── BUG 1：合成 thread id 没带 chat 作用域 → 跨群串上下文 ──────

def test_bug_forum_topic_thread_id_is_not_chat_scoped():
    """两个不同群的 topic 7 都被翻成 thread `f7`。

    thread id 是 TgBuffer 的**唯一**分桶键（tg_context.TgBuffer._threads），而
    dispatcher 注入上下文时只按 thread_id 拉（dispatcher.py:2221 →
    thread_context.build_thread_context → list_thread_messages(thread_id)），不带
    chat 过滤。于是 A 群 topic 7 说过的话会被当成 B 群 topic 7 的历史喂进 prompt。

    topic_id 就是"建话题"那条消息的 message_id，每个群各自从小数字开始 —— 撞号
    是常态而不是巧合。
    """
    bot = _bot()
    a = gw.build_event(bot, _msg(
        message_id=11, chat={"id": -100123, "type": "supergroup"},
        text="A 群机密：prod db 密码是 hunter2",
        is_topic_message=True, message_thread_id=7,
    ))
    b = gw.build_event(bot, _msg(
        message_id=12, chat={"id": -100999, "type": "supergroup"},
        text="@spx_bot B 群随便问点别的",
        is_topic_message=True, message_thread_id=7,
    ))
    assert a.event.message.thread_id != b.event.message.thread_id, (
        f"两个群的 topic 7 拿到同一个 thread id: {a.event.message.thread_id}"
    )
    # 期望：B 群那条 thread 里只看得到 B 群的消息
    leaked = [t for chat, t in _texts(bot, b.event.message.thread_id)
              if chat != "-100999"]
    assert leaked == [], f"B 群 session 里混进了别的群的消息: {leaked}"


def test_dispatch_child_thread_id_is_chat_scoped():
    """dispatch 子会话的 thread id 必须带 chat 作用域。

    锚点 message_id 只在单个 chat 内唯一（新群的 mid 都是小数字，撞号是常态）。
    不带 chat 的话两个群的子会话 transcript 会合成一桶，read_thread / 上下文注入
    都会串 —— session key 是 `<chat>:<thread>` 所以 session 不串，**串的是上下文**。
    """
    from telegram_client import new_thread_id

    bot = _bot()
    for chat in ("-100123", "-100999"):
        bot.feishu.buffer.record(
            message_id=f"{chat}:80", chat_id=chat,
            thread_id=new_thread_id(chat, 80),
            user_id="42", is_bot=True, content='{"text": "%s 的子会话顶楼"}' % chat,
        )
    ev = gw.build_event(bot, _msg(
        message_id=81, chat={"id": -100999, "type": "supergroup"},
        text="继续", reply_to_message={"message_id": 80, "from": {"id": 42}},
    ))
    leaked = [t for chat, t in _texts(bot, ev.event.message.thread_id)
              if chat != "-100999"]
    assert leaked == [], f"B 群子会话里混进了 A 群子会话的记录: {leaked}"


# ── BUG 2：先记缓冲后过白名单 ───────────────────────────────

def test_bug_unauthorized_message_is_persisted_before_acl():
    """任何陌生人私聊 bot，正文都会被写进 ~/.feishu-claude/tg/<profile>.jsonl。

    telegram_gateway.py:295 的 record 发生在 telegram_gateway.py:463 的 acl_reject
    之前，且 TgBuffer 的分桶数（_threads）和名字表（_names）都没有上限 —— bot 的
    用户名是公开的，等于给未授权者开了一个无限量的落盘/驻留内存写入口。
    """
    bot = _bot()
    gw._revealed.clear()
    stranger = _msg(message_id=1, chat={"id": 999, "type": "private"},
                    text="A" * 200)
    stranger["from"] = {"id": 999, "first_name": "陌生人"}
    poller, submitted = _poller(bot, [[{"update_id": 1, "message": stranger}]])
    poller.poll_once()

    assert list(bot.feishu.buffer._threads.keys()) == [], (
        "未授权会话被建了缓冲桶: " + str(list(bot.feishu.buffer._threads.keys()))
    )
    assert bot.feishu.buffer._names == {}, "未授权用户名被记住了"
    on_disk = ""
    if os.path.exists(bot.feishu.buffer.path):
        on_disk = open(bot.feishu.buffer.path, encoding="utf-8").read()
    assert "A" * 200 not in on_disk, "未授权正文被写进了缓冲文件"


def test_safe_unauthorized_message_never_reaches_dispatcher():
    """记缓冲是越权的，但**派发**没有越权：投给 dispatcher 的只有那条 reveal 私信。"""
    bot = _bot()
    gw._revealed.clear()
    stranger = _msg(message_id=1, chat={"id": 999, "type": "private"}, text="rm -rf /")
    stranger["from"] = {"id": 999, "first_name": "陌生人"}
    poller, submitted = _poller(bot, [[{"update_id": 1, "message": stranger}]])
    calls = []

    async def on_message(b, ev):  # pragma: no cover
        calls.append(ev)

    poller.on_message = on_message
    poller.poll_once()
    assert calls == []
    assert len(submitted) == 1  # 只有"你未授权 + 你的 id"这一条


# ── BUG 3：update_id 缺失/畸形会让 offset 倒退 ───────────────

def test_bug_missing_update_id_rewinds_offset():
    """update 里没有 update_id 时 offset 直接回到 1 = 整个积压队列被无限重投。

    telegram_gateway.py:508 的默认值 0 让 offset 单调性失效；offset 一旦倒退，
    Telegram 会把这个 offset 之后的所有 update 再推一遍，poll 循环从此空转刷屏
    （消息侧靠 grace/去重挡住副作用，callback_query 侧没有 grace）。
    """
    bot = _bot()
    poller, _ = _poller(bot, [[{"message": _msg(text="@spx_bot hi")}]])
    poller.offset = 5000
    poller.poll_once()
    assert poller.offset >= 5000, f"offset 倒退到 {poller.offset}"


def test_bug_non_numeric_update_id_aborts_whole_batch():
    """update_id 不是数字时 int() 在 try 之外抛出 → 整批未处理且 offset 不动。"""
    bot = _bot()
    poller, _ = _poller(bot, [[
        {"update_id": "??", "message": _msg(text="@spx_bot hi")},
        {"update_id": 902, "message": _msg(message_id=6, text="@spx_bot hi2")},
    ]])
    poller.offset = 900
    poller.poll_once()          # 期望：跳过畸形的那条，第二条照常处理
    assert poller.offset == 903


def test_safe_unknown_update_kind_still_advances_offset():
    """my_chat_member / edited_message 这类不处理的 update 也必须确认掉。"""
    bot = _bot()
    poller, submitted = _poller(bot, [[
        {"update_id": 100, "my_chat_member": {"chat": {"id": -100123}}},
        {"update_id": 101, "edited_message": _msg(text="@spx_bot typo fixed")},
    ]])
    assert poller.poll_once() == 0
    assert poller.offset == 102
    assert submitted == []


def test_safe_one_broken_update_does_not_sink_the_batch():
    bot = _bot()
    poller, submitted = _poller(bot, [[
        {"update_id": 10, "message": {"totally": "broken"}},
        {"update_id": 11, "message": _msg(text="@spx_bot hi")},
    ]])
    poller.poll_once()
    assert len(submitted) == 1
    assert poller.offset == 12


# ── BUG 4：子会话 thread 在锚点消失后静默塌回群主线 ──────────

def test_reply_falls_back_to_the_main_line_when_no_trace_of_a_child_thread():
    """缓冲里连子会话的**桶**都没有了 → 回复它落回群主线，这是有意的取舍。

    子会话只要还有一条消息在缓冲里，`_resolve_thread` 就靠 `has_thread` 认得出
    （见 test_bug_dispatch_child_collapses... 的修复：不再只依赖锚点那一条记录）。
    但整个桶都没了（换了 CC_TG_BUFFER_DIR / 删了 jsonl）时只有两个选择：
      · 落回群主线 —— 上下文可能串一点，但用户至少能继续对话；
      · 凭 `reply_to.from.id == bot_id` 就开子会话 —— 那么"回复 bot 在群里的任何
        一条普通回答"都会变成新 session，反而把正常对话切碎（见下一条 safe 测试）。
    选前者。
    """
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        message_id=81, text="继续（回复一条已经没有痕迹的 bot 消息）",
        reply_to_message={"message_id": 80, "from": {"id": 42, "is_bot": True}},
    ))
    assert ev.event.message.thread_id == "c-100123"


def test_safe_reply_to_normal_group_message_stays_on_main_line():
    """普通回复（非 t* 分支）仍归群主线 —— 这条是对的，别被上面的修法带跑。"""
    bot = _bot()
    bot.feishu.buffer.record(
        message_id="-100123:70", chat_id="-100123", thread_id="c-100123",
        user_id="777", content='{"text": "群里聊到一半"}',
    )
    ev = gw.build_event(bot, _msg(
        message_id=71, text="@spx_bot 接着说",
        reply_to_message={"message_id": 70, "from": {"id": 777}},
    ))
    assert ev.event.message.thread_id == "c-100123"


def test_safe_forum_topic_and_main_line_are_separate_sessions():
    """论坛 topic 和群主线必须是两个 session（否则丢/串上下文）。"""
    bot = _bot()
    topic = gw.build_event(bot, _msg(message_id=20, text="@spx_bot 在 topic 里",
                                     is_topic_message=True, message_thread_id=9))
    main = gw.build_event(bot, _msg(message_id=21, text="@spx_bot 在主线"))
    # thread id 带 chat 作用域（f<chat>#<topic>）：不带的话两个群的同号 topic 会
    # 撞进同一个上下文桶
    assert topic.event.message.thread_id == "f-100123#9"
    assert main.event.message.thread_id == "c-100123"
    assert extract_chat_info(topic)[1] != extract_chat_info(main)[1]


def test_safe_non_forum_reply_thread_id_is_ignored():
    """非论坛超级群里 Telegram 也会给回复带 message_thread_id，不能当 topic 用，
    否则同一个群的每条回复链都会被切成一个独立 session（丢上下文）。"""
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        message_id=22, text="@spx_bot 回复链", message_thread_id=15,
        reply_to_message={"message_id": 15, "from": {"id": 888}},
    ))
    assert ev.event.message.thread_id == "c-100123"


# ── BUG 5：bot_id=0（getMe 失败）时 @ 判定全放 ────────────────

def test_bug_zero_bot_id_marks_every_message_as_mention():
    """getMe 失败时 bot_id 留 0（main.py:231 只打一行 warn 就继续起轮询），
    于是 `int(reply_from.get("id") or 0) == bot_id` 对**任何**消息成立
    —— 连不是回复的消息也算被 @。

    目前后果被 dispatcher._is_current_bot_mentioned 兜住了（它要求
    get_bot_open_id() 非空且与 mention 的 open_id 相等），所以线上表现是"群里
    一句都不回"而不是"抢答所有人"。但这是纯巧合式的兜底：任何一处开始信
    event.mentions（比如把 @ 判定收敛到网关），当场变成群里逢消息就抢答。
    """
    bot = _bot(bot_id=0, username="")
    ev = gw.build_event(bot, _msg(text="我们自己聊聊，别叫 bot"))
    assert ev.event.message.mentions == [], (
        "bot_id=0 时把普通闲聊也判成了 @ 到我"
    )


# ── BUG 6：photo 只按 file_size 排序 ────────────────────────

def test_bug_photo_without_file_size_picks_the_thumbnail():
    """Bot API 的 PhotoSize.file_size 是 Optional。缺失时 `_photo_file_id` 的
    比较全是 0>0=False，于是留在 best 里的是**第一个**（Telegram 按尺寸升序给，
    第一个是 90px 缩略图）→ agent 拿到一张糊图去"读图分析"。

    修法：排序键改成 (file_size, width*height) 取最大。
    """
    assert gw._photo_file_id([
        {"file_id": "thumb", "width": 90, "height": 60},
        {"file_id": "mid", "width": 320, "height": 240},
        {"file_id": "full", "width": 1280, "height": 960},
    ]) == "full"


def test_safe_photo_with_file_size_picks_the_largest():
    assert gw._photo_file_id([
        {"file_id": "small", "file_size": 100},
        {"file_id": "big", "file_size": 900},
    ]) == "big"


# ── BUG 7：相册（media_group）不聚合 ────────────────────────


def test_bug_media_group_album_is_not_aggregated():
    """一次发 3 张图 + 一句说明，Telegram 推 3 条 message（同一个 media_group_id，
    只有第一条带 caption）。网关逐条翻译，dispatcher 里也没有任何 media_group
    处理（`grep -rn media_group *.py` 全仓 0 命中），后果：

      * 私聊：3 条都过 ACL、3 次抢 per-chat 锁 → **同一份相册跑 3 次 agent**，
        后两次的 prompt 是"用户发了一张图"，说明文字丢了；
      * 群聊：只有带 caption 那条能 @ 到 bot，另外两张被静默丢弃 → 用户以为
        3 张都给了，实际 agent 只看到 1 张。

    修法：按 media_group_id 攒 ~1s（Telegram 同组消息几乎同时到），合成一条
    post（caption + N 个 image_key），_post_content 本来就支持多图。
    """
    # 聚合发生在轮询层（TgPoller.deliver 攒同组、flush_albums 合并），
    # build_event 只负责翻译单条消息，所以测试要走 poller。
    bot = _bot()
    submitted = []

    def submit(coro):
        submitted.append(coro)
        coro.close()

    async def on_message(b, ev):  # pragma: no cover
        pass

    poller = gw.TgPoller(bot, submit=submit, on_message=on_message,
                         touch=lambda: None, started_at=1700000000)
    for mid, cap in ((1, "@spx_bot 这三张一起看"), (2, ""), (3, "")):
        msg = _msg(message_id=mid, text=None, caption=cap or None,
                   photo=[{"file_id": f"p{mid}", "file_size": 900}])
        msg["media_group_id"] = "MG1"
        assert poller.deliver(msg) is False        # 攒着，等同组到齐
    assert poller.flush_albums(force=True) == 1
    assert len(submitted) == 1, f"一次相册被翻成了 {len(submitted)} 条独立任务"

    from feishu_post import extract_post_image_keys, parse_post_content
    recorded = bot.feishu.buffer.thread_messages("c-100123")
    assert len(recorded) == 1
    assert extract_post_image_keys(recorded[0].body.content) == ["p1", "p2", "p3"]
    assert "这三张一起看" in parse_post_content(recorded[0].body.content)


# ── BUG 8：匿名管理员 / sender_chat 消息静默消失 ────────────


def test_bug_anonymous_admin_message_is_silently_dropped():
    """群管理员开了"匿名发言"时 Telegram 不给 `from`，只给 `sender_chat`。
    telegram_gateway.py:220 直接 return None：@ 了 bot 也不回，日志里一个字都没有，
    连缓冲都不记（那段对话在后续上下文里凭空缺一块）。

    取舍：**继续丢**（没有可鉴权的自然人，用群 id 当"发件人"会让"谁能艾特"这条
    白名单形同虚设），但必须留一行日志 —— 否则"我明明 @ 了它却一声不响"完全查不出
    原因。
    """
    bot = _bot()
    anon = _msg(text="@spx_bot 帮我看下这个")
    anon.pop("from")
    anon["sender_chat"] = {"id": -100123, "type": "supergroup", "title": "群"}
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ev = gw.build_event(bot, anon)
    assert ev is None
    assert "匿名发言" in buf.getvalue(), "至少要留一行日志，不能静默丢"


def test_safe_channel_post_is_dropped():
    """频道贴不该进业务流程（没有真实发件人可鉴权）。"""
    bot = _bot()
    post = _msg(chat={"id": -100777, "type": "channel"}, text="@spx_bot 广播")
    post.pop("from")
    assert gw.build_event(bot, post) is None


# ── BUG 9：callback token 无 TTL / 无一次性 / 不校验来源卡片 ──

def test_bug_callback_token_can_be_replayed():
    """Lark 侧一次点击要过 verify_action_value（HMAC + 30 天 TTL + `_cc_mid`
    绑定被点消息）再过 claim_event(event_id) 幂等门（dispatcher.py:2666）。
    TG 侧 handle_callback 只查 token 存在 + profile + `_cc_uid`：

      * entry["ts"] 存了但从不校验 → 按钮永不过期（只被 _CALLBACK_MAX=800 淘汰）；
      * entry 用完不 pop → 同一个 token 点 N 次执行 N 次；
      * entry["card"] 从不和被点消息比对 → 少一道"这个按钮属于这条卡片"的约束。

    最现实的坑：`run_cmd /new` 之类的副作用按钮，用户手滑连点两下就跑两遍
    （Lark 侧同样会，但 Lark 至少对**同一事件重投**是幂等的）。
    """
    bot = _bot()
    token = bot.feishu._register_callback(
        {"action": "run_cmd", "cmd": "/new", "cid": "-100123:c-100123",
         "profile": "tg", "_cc_uid": "777"}, "-100123:9")
    cq = {"id": "cq1", "data": token, "from": {"id": 777},
          "message": {"message_id": 9, "chat": {"id": -100123, "type": "supergroup"}}}

    def submit(coro):
        coro.close()

    with mock.patch("dispatcher.handle_menu_command") as handler:
        gw.handle_callback(bot, cq, submit)
        gw.handle_callback(bot, cq, submit)
    assert handler.call_count == 1, f"同一个 token 执行了 {handler.call_count} 次"


def test_safe_callback_acl_and_owner_binding_hold():
    """按钮的三道门（profile / _cc_uid / ACL）都真的拦得住。"""
    bot = _bot()

    def submit(coro):
        coro.close()

    def press(value, user_id, chat=None):
        token = bot.feishu._register_callback(value, "-100123:9")
        cq = {"id": "cq", "data": token, "from": {"id": user_id},
              "message": {"message_id": 9,
                          "chat": chat or {"id": -100123, "type": "supergroup"}}}
        with mock.patch("dispatcher.handle_menu_command") as handler:
            gw.handle_callback(bot, cq, submit)
        return handler.call_count

    base = {"action": "run_cmd", "cmd": "/new", "cid": "-100123:c-100123"}
    assert press({**base, "profile": "tg", "_cc_uid": "777"}, 777) == 1
    # 别人的按钮
    assert press({**base, "profile": "tg", "_cc_uid": "777"}, 888) == 0
    # 别的 bot 的按钮
    assert press({**base, "profile": "other", "_cc_uid": "777"}, 777) == 0
    # 白名单外的群里点（伪造不了 chat，但确认 ACL 真的按被点消息所在群判）
    assert press({**base, "profile": "tg", "_cc_uid": "666"}, 666,
                 chat={"id": -100555, "type": "supergroup"}) == 0


def test_safe_unknown_callback_token_only_acks():
    bot = _bot()
    acked = []

    def submit(coro):
        acked.append(coro)
        coro.close()

    gw.handle_callback(bot, {
        "id": "cq", "data": "forged-token", "from": {"id": 777},
        "message": {"message_id": 9, "chat": {"id": -100123, "type": "supergroup"}},
    }, submit)
    assert len(acked) == 1


# ── BUG 10：改错字 = 完全无响应 ─────────────────────────────

@pytest.mark.xfail(strict=False, reason="已知限制（有意不支持）：编辑消息不触发 bot。"
                                        "edited_message 的 message_id 与原消息相同，"
                                        "当新消息投进去会被 _is_duplicate_event(TTL 120s) "
                                        "吞掉；要支持得给去重键带上 edit_date。"
                                        "README「已知边界」里已写明：改错字请重发一条。")
def test_bug_edited_message_is_unsubscribed_and_unhandled():
    """用户把 `@spx_bot 帮我看下 foo.py` 里的错字改掉 → Telegram 只产生
    edited_message，而 poll_once 的 allowed_updates 只订了 message /
    callback_query（telegram_gateway.py:502），edited_message 既收不到也没分支
    → bot 一声不响，用户以为 bot 挂了。

    ⚠️ 修法有坑：edited_message 的 message_id 和原消息**相同**，直接当新消息投
    会被 dispatcher._is_duplicate_event（TTL 120s，dispatcher.py:78）吞掉——改
    错字通常就在 120s 内。要么把去重键带上 edit_date，要么只回一句"检测到消息
    编辑，请重发一条"。
    """
    bot = _bot()
    poller, submitted = _poller(bot, [[
        {"update_id": 200, "edited_message": _msg(text="@spx_bot 帮我看下 bar.py")},
    ]])
    poller.poll_once()
    req = bot.feishu._post_sync.requests[0]
    assert "edited_message" in (req.get("allowed_updates") or []), (
        "没订阅 edited_message，用户改错字后 bot 永远收不到"
    )
    assert submitted, "edited_message 收到了也没人处理"


# ── BUG 11：@ 正则没有左边界 ────────────────────────────────

def test_bug_mention_regex_has_no_left_boundary():
    """`ops@spx_bot.com`（或任何 `xxx@<username>` 形式）会被判成 @ 到 bot。
    影响面小（要正好撞上 bot 用户名），但一撞就是群里无故抢答。
    修法：正则前面加 `(?<![\\w@])`。
    """
    ms = gw._mentions_for("有问题发 ops@spx_bot.com", {}, "spx_bot", 42, True)
    assert ms == [], f"邮箱式文本被判成 @: {[m.key for m in ms]}"


def test_safe_mention_right_boundary_is_exact():
    """`@spx_bot2` / `@spx_bot_x` 不能命中 `spx_bot` —— 这条是对的。"""
    for text in ("@spx_bot2 hi", "@spx_botx", "@spx_bot_dev 上"):
        assert gw._mentions_for(text, {}, "spx_bot", 42, True) == [], text
    for text in ("@spx_bot hi", "@SPX_BOT hi", "看 @spx_bot，来一下", "@spx_bot"):
        assert [m.id.open_id for m in
                gw._mentions_for(text, {}, "spx_bot", 42, True)] == ["42"], text


def test_safe_mention_key_is_strippable_verbatim():
    """mention.key 必须是正文里的原样片段，否则 strip_lark_mentions 剥不掉，
    "@SPX_Bot" 这种大小写变体会留在 prompt 里。"""
    from feishu_post import strip_lark_mentions

    text = "@SPX_Bot 帮我看下"
    ms = gw._mentions_for(text, {}, "spx_bot", 42, True)
    assert strip_lark_mentions(text, ms) == "帮我看下"


def test_safe_caption_mention_and_command_use_caption_entities():
    """图片说明里的 @ 和 /cmd 走 caption / caption_entities，不能漏。"""
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text=None, caption="@spx_bot 这个报错什么意思",
        photo=[{"file_id": "big", "file_size": 900}]))
    assert [m.id.open_id for m in ev.event.message.mentions] == ["42"]

    ev2 = gw.build_event(bot, _msg(
        text=None, caption="/ws", caption_entities=[
            {"type": "bot_command", "offset": 0, "length": 3}],
        document={"file_id": "d1", "file_name": "a.txt"}))
    assert [m.id.open_id for m in ev2.event.message.mentions] == ["42"]


def test_safe_slash_command_routing():
    """裸 /cmd 算 @（命令菜单点出来的就是裸的）；/cmd@别的bot 不算。"""
    bot = _bot()
    ents = [{"type": "bot_command", "offset": 0, "length": 6}]
    assert gw.build_event(bot, _msg(text="/usage", entities=ents)
                          ).event.message.mentions
    assert gw.build_event(bot, _msg(text="/usage@other_bot", entities=ents)
                          ).event.message.mentions == []
    assert gw.build_event(bot, _msg(text="/usage@SPX_BOT", entities=ents)
                          ).event.message.mentions
    # 命令不在句首（引用别人的命令）不算
    assert gw.build_event(
        bot, _msg(text="别发 /usage 了",
                  entities=[{"type": "bot_command", "offset": 3, "length": 6}]),
    ).event.message.mentions == []


def test_safe_forged_mention_cannot_bypass_acl():
    """伪造 text_mention / reply_to.from.id 都过不了白名单这道门。"""
    bot = _bot()
    forged = _msg(
        chat={"id": -100555, "type": "supergroup"},   # 群不在白名单
        text="/new",
        entities=[{"type": "text_mention", "offset": 0, "length": 4,
                   "user": {"id": 42}}],
        reply_to_message={"message_id": 1, "from": {"id": 42}},
    )
    forged["from"] = {"id": 999, "first_name": "外人"}   # 人也不在白名单
    ev = gw.build_event(bot, forged)
    assert [m.id.open_id for m in ev.event.message.mentions] == ["42"]  # @ 判定成立
    assert gw.acl_reject(bot.profile, "999", "-100555", True)           # 但照样被拒
    assert gw.acl_reject(bot.profile, "777", "-100555", True)           # 群不在白名单
    assert gw.acl_reject(bot.profile, "999", "-100123", True)           # 人不在白名单


# ── ACL 回归锁 ──────────────────────────────────────────────

def test_safe_acl_matrix():
    bot = _bot()
    p = bot.profile
    assert gw.acl_reject(p, "777", "777", False) == ""
    assert gw.acl_reject(p, "777", "-100123", True) == ""
    assert gw.acl_reject(p, "888", "-100123", True)   # 用户不在白名单
    assert gw.acl_reject(p, "777", "-100555", True)   # 群不在白名单
    assert gw.acl_reject(p, "888", "888", False)      # 私聊也要过用户白名单


def test_safe_empty_allowlist_denies_everything():
    p = _bot(users=(), groups=("*",)).profile
    assert gw.acl_reject(p, "777", "777", False)
    assert gw.acl_reject(p, "777", "-100123", True)


def test_safe_group_wildcard_still_requires_user_allowlist():
    """`*` 只放开群，不放开人 —— 否则任何人把 bot 拉进群就能用。"""
    p = _bot(groups=("*",)).profile
    assert gw.acl_reject(p, "777", "-42424242", True) == ""
    assert gw.acl_reject(p, "888", "-42424242", True)


def test_safe_acl_is_type_agnostic():
    """白名单里是字符串，Telegram 给的是 int：两边都 str() 过，别回退成裸比较。"""
    bot = _bot()
    assert gw.acl_reject(bot.profile, 777, -100123, True) == ""
    assert gw.acl_reject(bot.profile, 778, -100123, True)
    ev = gw.build_event(bot, _msg())
    assert ev.event.sender.sender_id.open_id == "777"
    assert ev.event.message.chat_id == "-100123"


def test_safe_user_wildcard_is_not_a_thing():
    """把 ALLOWED_OPEN_IDS 配成 `*` 是 fail-closed（全拒）而不是全放 —— 记录
    这个行为，防以后"顺手支持一下 * "变成 Telegram 侧全网开放。"""
    p = _bot(users=("*",)).profile
    assert gw.acl_reject(p, "777", "777", False)


def test_safe_reveal_is_private_only_and_once():
    """未授权提示只在私聊发、每人一次；群里永远不吭声。"""
    bot = _bot()
    gw._revealed.clear()
    dm = _msg(message_id=1, chat={"id": 999, "type": "private"}, text="hi")
    dm["from"] = {"id": 999, "first_name": "陌生人"}
    grp = _msg(message_id=2, chat={"id": -100555, "type": "supergroup"},
               text="@spx_bot hi")
    poller, submitted = _poller(bot, [
        [{"update_id": 1, "message": dm}],
        [{"update_id": 2, "message": dm}, {"update_id": 3, "message": grp}],
    ])
    poller.poll_once()
    assert len(submitted) == 1
    poller.poll_once()
    assert len(submitted) == 1


def test_bug_revealed_set_is_shared_across_profiles():
    """两个 TG bot 跑在同一进程里时，第二个 bot 对同一个陌生人不再提示（因为
    第一个 bot 已经把这个 id 记进模块级 `_revealed`）；这个 set 也没有上限，
    刷子每换一个号就多驻留一条。修法：挪成 per-profile 的 LRU/TTL 集合。"""
    gw._revealed.clear()
    dm = _msg(message_id=1, chat={"id": 999, "type": "private"}, text="hi")
    dm["from"] = {"id": 999, "first_name": "陌生人"}
    counts = []
    for name in ("tg", "tg2"):
        bot = _bot()
        bot.profile.name = name
        poller, submitted = _poller(bot, [[{"update_id": 1, "message": dm}]])
        poller.poll_once()
        counts.append(len(submitted))
    assert counts == [1, 1], f"第二个 profile 没提示: {counts}"


# ── 离线堆积 / grace ────────────────────────────────────────

def test_safe_stale_backlog_is_dropped_but_confirmed():
    bot = _bot()
    stale = _msg(message_id=7, text="@spx_bot 三小时前")
    stale["date"] = 1700000000 - 3 * 3600
    fresh = _msg(message_id=8, text="@spx_bot 刚说的")
    poller, submitted = _poller(
        bot, [[{"update_id": 1, "message": stale}, {"update_id": 2, "message": fresh}]])
    poller.poll_once()
    assert len(submitted) == 1
    assert poller.offset == 3


def test_safe_stale_backlog_grace_boundary():
    """grace 是"启动前 180s"：卡在边界内的照跑，边界外的丢。"""
    bot = _bot()
    inside = _msg(message_id=9, text="@spx_bot 边界内")
    inside["date"] = 1700000000 - (gw._START_GRACE_SEC - 5)
    outside = _msg(message_id=10, text="@spx_bot 边界外")
    outside["date"] = 1700000000 - (gw._START_GRACE_SEC + 5)
    poller, submitted = _poller(bot, [[
        {"update_id": 1, "message": inside}, {"update_id": 2, "message": outside}]])
    poller.poll_once()
    assert len(submitted) == 1


def test_safe_stale_callback_query_has_no_grace_but_fails_closed():
    """callback_query 没有 date，grace 管不到它 —— 离线期间点的按钮重启后会被处理。
    目前无害：token 表是纯内存的，重启后必然 resolve 不到，只回一句"已过期"。
    这条锁住"无害"这个前提：一旦 token 改成落盘，就必须补 TTL/幂等。"""
    bot = _bot()
    acked = []

    def submit(coro):
        acked.append(coro)
        coro.close()

    poller, _ = _poller(bot, [[{
        "update_id": 1,
        "callback_query": {"id": "cq", "data": "token-from-before-restart",
                           "from": {"id": 777},
                           "message": {"message_id": 9,
                                       "chat": {"id": -100123, "type": "supergroup"}}},
    }]])
    poller.submit = submit
    with mock.patch("dispatcher.handle_menu_command") as handler:
        poller.poll_once()
    handler.assert_not_called()
    assert len(acked) == 1
    assert poller.offset == 2


# ── 消息类型 → dispatcher 分支 ──────────────────────────────

def test_safe_every_produced_type_has_a_dispatcher_branch():
    """网关只会产出 text/image/post/audio/file 这 5 种；_process_message 的
    else 分支是"直接 return"，多一种就是静默丢消息。"""
    import inspect
    import dispatcher

    src = inspect.getsource(dispatcher._process_message)
    bot = _bot()
    samples = {
        "text": _msg(),
        "image": _msg(text=None, photo=[{"file_id": "p", "file_size": 9}]),
        "post": _msg(text=None, caption="说明", photo=[{"file_id": "p", "file_size": 9}]),
        "audio": _msg(text=None, voice={"file_id": "v", "duration": 3}),
        "file": _msg(text=None, document={"file_id": "d", "file_name": "a.pdf"}),
    }
    for want, m in samples.items():
        ev = gw.build_event(bot, m)
        assert ev.event.message.message_type == want, want
        assert f'msg.message_type == "{want}"' in src, f"dispatcher 没有 {want} 分支"


def test_safe_audio_duration_unit_round_trips():
    """网关写 ms，dispatcher 拿 `duration_ms // 1000` 当秒念给用户听。"""
    bot = _bot()
    ev = gw.build_event(bot, _msg(text=None, voice={"file_id": "v", "duration": 7}))
    body = json.loads(ev.event.message.content)
    assert body == {"file_key": "v", "duration": 7000}
    assert body["duration"] // 1000 == 7


def test_safe_file_caption_reaches_the_prompt():
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text=None, caption="看下第三页", document={"file_id": "d", "file_name": "r.pdf"}))
    body = json.loads(ev.event.message.content)
    assert body["file_name"] == "r.pdf" and body["caption"] == "看下第三页"


def test_safe_post_json_is_lark_shaped():
    from feishu_post import extract_post_image_keys, parse_post_content

    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text=None, caption="这是什么错", photo=[{"file_id": "big", "file_size": 900}]))
    raw = json.loads(ev.event.message.content)
    assert set(raw) == {"zh_cn"} and set(raw["zh_cn"]) == {"title", "content"}
    assert parse_post_content(ev.event.message.content) == "这是什么错[图片]"
    assert extract_post_image_keys(ev.event.message.content) == ["big"]


def test_bug_voice_caption_is_dropped():
    """Bot API 允许 voice/audio 带 caption，网关的 audio 分支完全不读它
    （file 分支读了）。用户"发一段语音 + 一句文字说明"时说明会消失。
    顺带：`msg.get("voice") or msg.get("audio")` 把音乐文件也当语音送去转写，
    更合理的是 audio 有 file_name 时走 file 分支。"""
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text=None, caption="这段是客户原话",
        voice={"file_id": "v", "duration": 5}))
    assert json.loads(ev.event.message.content).get("caption") == "这段是客户原话"


def test_safe_video_note_and_nameless_document_get_a_filename():
    bot = _bot()
    assert json.loads(gw.build_event(bot, _msg(
        text=None, video_note={"file_id": "vn"})).event.message.content
    )["file_name"] == "video.mp4"
    assert json.loads(gw.build_event(bot, _msg(
        text=None, document={"file_id": "d"})).event.message.content
    )["file_name"] == "file"


def test_safe_empty_text_is_harmless():
    """text="" 走 text 分支，dispatcher 的 `if not text: return` 会静默丢掉。
    Telegram 发不出空消息，这里只是锁住"不炸"。"""
    bot = _bot()
    ev = gw.build_event(bot, _msg(text=""))
    assert ev.event.message.message_type == "text"
    assert json.loads(ev.event.message.content) == {"text": ""}


def test_safe_long_text_is_not_truncated():
    bot = _bot()
    long = "字" * 4096
    ev = gw.build_event(bot, _msg(text=long))
    assert json.loads(ev.event.message.content)["text"] == long


def test_safe_sticker_is_buffered_as_text_only():
    bot = _bot()
    assert gw.build_event(bot, _msg(text=None, sticker={"emoji": "🎉"})) is None
    assert _texts(bot, "c-100123") == [("-100123", "[贴纸 🎉]")]


def test_safe_service_message_is_dropped_entirely():
    bot = _bot()
    assert gw.build_event(bot, _msg(text=None, new_chat_members=[{"id": 42}])) is None
    assert bot.feishu.buffer.thread_messages("c-100123") == []


def test_safe_event_shape_matches_what_dispatcher_reads():
    """dispatcher 只读这几个字段；少一个就是 AttributeError 或串台。"""
    bot = _bot()
    ev = gw.build_event(bot, _msg(
        text="@spx_bot hi", reply_to_message={"message_id": 4, "from": {"id": 42}}))
    m = ev.event.message
    for attr in ("message_id", "chat_id", "chat_type", "thread_id", "message_type",
                 "content", "mentions", "parent_id", "create_time"):
        assert hasattr(m, attr), attr
    assert m.message_id == "-100123:5" and m.parent_id == "-100123:4"
    assert m.chat_type == "group" and m.create_time == "1700000000000"
    assert ev.event.sender.sender_id.open_id == "777"
    # 复合 key 是跨 chat 唯一的（dispatcher 的去重表和卡片锚点都是全局的）
    other = gw.build_event(bot, _msg(chat={"id": -100999, "type": "supergroup"}))
    assert other.event.message.message_id != m.message_id


def test_bug_first_poll_sends_null_offset():
    """`TelegramClient.call()` 会把 None 值剔掉，但 poll_once 直接调 `_post_sync`
    （为了自己的长轮询超时），于是首轮 body 里带 `"offset": null`。Telegram 目前
    容忍 null（等价于不传），所以线上没炸 —— 但这是运气，不是契约。
    修法：offset 为 None 时不要放进 payload。"""
    bot = _bot()
    poller, _ = _poller(bot, [[]])
    poller.poll_once()
    assert "offset" not in bot.feishu._post_sync.requests[0]
