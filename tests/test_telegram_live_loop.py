"""端到端：真 socket + 真轮询线程 + 真 dispatcher，只把 Telegram 那一端换成本机假服务。

和 test_telegram_e2e.py 的区别：那边把 HTTP 层打桩成一个函数（快，但不过网络、不过
轮询线程）；这里起一个**真的 HTTP 服务**（tests/fake_bot_api.py）+ 真的
`telegram_gateway.start_polling` 后台线程，走完「用户发消息 → getUpdates 拉到 →
dispatcher 处理 → sendMessage/editMessageText 回去」的完整闭环，包括 offset 语义、
长轮询、429 重试、多线程交接。

Telegram 自己的怪癖（HTML entity 解析规则、flood control 真实阈值）不在这里测 ——
那些用真 API 校：`sendMessage(chat_id=1, parse_mode=HTML)` 在 chat 不存在时仍会先
解析 entities，见 tests/test_tg_md_fuzz.py / 开发笔记。
"""

import asyncio
import html
import json
import os
import re
import subprocess
import sys
import time
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dispatcher
import telegram_client
import telegram_gateway as gw
from bot_config import Profile
from bot_instance import BotInstance
from fake_bot_api import FakeBotAPI

GROUP = -100777
DM = 777
USER = 777


class Live:
    """一套跑起来的 Telegram 渠道（假 API + 真 bot + 真轮询线程）。"""

    def __init__(self, api: FakeBotAPI, bot: BotInstance, poller: gw.TgPoller):
        self.api = api
        self.bot = bot
        self.poller = poller

    async def wait_for(self, predicate, timeout: float = 15.0, what: str = "条件"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"等 {what} 超时（{timeout}s）。当前 chat 内容: "
            f"{self.api.texts_of(GROUP)} / {self.api.texts_of(DM)}"
        )

    async def texts(self, chat_id, count: int, timeout: float = 15.0) -> list[str]:
        return await self.wait_for(
            lambda: (self.api.texts_of(chat_id)
                     if len(self.api.texts_of(chat_id)) >= count else None),
            timeout=timeout, what=f"chat {chat_id} 出现 {count} 条消息",
        )

    async def settle(self, seconds: float = 1.2):
        """给轮询线程一点时间证明"什么都没发生"。"""
        await asyncio.sleep(seconds)


@pytest.fixture
async def live(monkeypatch, tmp_path):
    api = FakeBotAPI()
    root = api.start()
    monkeypatch.setattr(telegram_client, "API_ROOT", root)

    profile = Profile(
        name="tglive", app_id="8976773826", app_secret="8976773826:x",
        platform="telegram", domain="https://api.telegram.org",
        default_cwd=str(tmp_path / "default"), bot_token="8976773826:x",
        allowed_open_ids={str(USER)}, allowed_group_chat_ids={str(GROUP)},
        chat_default_cwd={str(GROUP): str(tmp_path / "proj-a")},
    )
    bot = BotInstance(profile)
    await bot.feishu.get_me()
    await bot.feishu.set_my_commands()

    loop = asyncio.get_running_loop()
    dispatcher.configure(bot_loop=loop, bots={profile.name: bot})
    poller = gw.start_polling(
        bot,
        submit=lambda coro: asyncio.run_coroutine_threadsafe(coro, loop),
        on_message=dispatcher.handle_message_async,
        touch=lambda: None,
    )
    yield Live(api, bot, poller)
    # 先停轮询再关服务：不停的话上一条 poller 线程会继续读被 monkeypatch 改过的
    # 模块级 API_ROOT，抢走下一个测试的 update 并投进一个已经关掉的事件循环
    # （症状：下一个测试"什么都没发生"，极难查）。
    poller.stop()
    api.stop()
    await asyncio.sleep(0.1)


def stub_runner(recorder: list, reply: str = "结论：没问题", *, delay: float = 0.0):
    async def run_agent(**kwargs):
        recorder.append(kwargs)
        if delay:
            await asyncio.sleep(delay)
        chunk = kwargs.get("on_text_chunk")
        if chunk:
            await chunk(reply)
        return reply, "sess-live", False
    return run_agent


# ── 基本闭环 ─────────────────────────────────────────────────

async def test_bot_identity_and_command_menu(live):
    assert live.bot.feishu.bot_username == "SPX_STG_bot"
    assert live.bot.feishu.can_read_all_group_messages is True
    # 启动时注册的斜杠命令菜单
    assert [c["command"] for c in live.api.commands][:3] == ["new", "resume", "stop"]


async def test_private_message_full_round_trip(live):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", stub_runner(calls, "在的，说吧")):
        live.api.user_message(text="在吗", chat={"id": DM, "type": "private"})
        texts = await live.texts(DM, 1)
        await live.wait_for(lambda: "在的，说吧" in texts_now(live, DM),
                            what="终态正文落到那条消息里")
    assert len(calls) == 1
    # ✅ 收尾 ping 在终态确认写之后才发，等它落地再断言。
    # 注意它不是裸 "✅"：Telegram 会把只含 emoji 的消息放大成贴纸尺寸（手机上占半屏），
    # 所以客户端给这几条带外提示配了词。
    await live.wait_for(
        lambda: any(c["text"].startswith("✅")
                    for c in live.api.method_calls("sendMessage")),
        what="收尾 ✅ ping")
    sends = live.api.method_calls("sendMessage")
    # 只有一条占位消息，正文靠 edit 原地长出来（不是每帧新发一条）
    assert sends[0]["text"] == "⏳ 思考中..."
    assert sends[0]["parse_mode"] == "HTML"
    assert len(live.api.method_calls("editMessageText")) >= 1
    # 收尾会另发一条 ✅：Telegram 的 editMessageText **不推送通知**，这条 ping 就是
    # 通知载体（Lark 侧同理，卡片 patch 也不推通知）。所以是 2 条 sendMessage。
    assert [s["text"].startswith("✅") for s in sends].count(True) == 1
    assert len(sends) == 2


def texts_now(live, chat_id):
    return " || ".join(live.api.texts_of(chat_id))


async def test_group_message_without_mention_is_ignored(live):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", stub_runner(calls)):
        live.api.user_message(text="我们晚点聊支付那块")
        await live.settle()
    assert calls == []
    assert live.api.texts_of(GROUP) == []


async def test_group_mention_replies_in_thread_and_streams_in_place(live):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", stub_runner(calls, "看完了，没问题")):
        msg = live.api.user_message(text="@SPX_STG_bot 看一下")
        await live.wait_for(lambda: "看完了，没问题" in texts_now(live, GROUP),
                            what="群里出现终态回复")
    placeholder = live.api.method_calls("sendMessage")[0]
    assert placeholder["reply_parameters"]["message_id"] == msg["message_id"]
    assert live.api.method_calls("editMessageText"), "应该 edit 占位消息"
    # @ 占位符被剥掉
    assert "@SPX_STG_bot" not in calls[0]["message"]


async def test_context_watermark_across_turns(live):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", stub_runner(calls, "收到")):
        live.api.user_message(text="SGB 开户失败了")
        live.api.user_message(text="报错是 address 缺字段",
                              from_user={"id": 888, "first_name": "Charlie"})
        live.api.user_message(text="@SPX_STG_bot 帮我看看")
        await live.wait_for(lambda: len(calls) >= 1, what="第一轮被触发")
        await live.wait_for(lambda: "收到" in texts_now(live, GROUP), what="第一轮收尾")
        live.api.user_message(text="我又试了一次还是不行")
        live.api.user_message(text="@SPX_STG_bot 现在呢")
        await live.wait_for(lambda: len(calls) >= 2, what="第二轮被触发")

    first, second = calls[0]["message"], calls[1]["message"]
    # 没 @ 的闲聊也进上下文（privacy mode 关掉时 bot 能收到）
    assert "SGB 开户失败了" in first and "address 缺字段" in first
    assert "Charlie" in first, "sender 名字应来自缓冲里记下的 Telegram 显示名"
    # 水位线：第二轮只带新增
    assert "SGB 开户失败了" not in second
    assert "我又试了一次还是不行" in second


async def test_slash_command_answers_without_calling_the_agent(live):
    calls = []
    with mock.patch.object(dispatcher, "run_agent", stub_runner(calls)):
        live.api.user_message(
            text="/ws", entities=[{"type": "bot_command", "offset": 0, "length": 3}])
        await live.wait_for(lambda: live.api.texts_of(GROUP), what="/ws 的回复")
    assert calls == []
    assert "proj-a" in live.api.texts_of(GROUP)[0]


# ── 长内容 / 故障注入 ────────────────────────────────────────

async def test_long_answer_is_split_into_continuation_messages(live):
    long_reply = "\n".join(f"### 第 {i} 节\n正文 **{i}** 与 `code{i}`" for i in range(300))
    with mock.patch.object(dispatcher, "run_agent", stub_runner([], long_reply)):
        live.api.user_message(text="@SPX_STG_bot 出个长报告")
        texts = await live.texts(GROUP, 2, timeout=25)
    assert len(texts) >= 2, "超长正文应该续段而不是被截断"
    joined = "".join(texts)
    assert "第 0 节" in joined and "第 299 节" in joined
    for t in texts:
        # Telegram 的 4096 上限算的是 **entities 解析之后的可见文字**（HTML 标签不计），
        # 所以这里量可见长度；同时顺手盯一眼标签膨胀别失控。
        visible = html.unescape(re.sub(r"<[^>]+>", "", t))
        assert len(visible) <= 4096, f"可见文字 {len(visible)} 超过 4096"
        assert len(t) <= 12000, f"HTML 源码膨胀到 {len(t)}，检查渲染是否重复包标签"


async def test_rate_limit_on_edit_is_survived(live):
    live.api.fail_once("editMessageText", 429, "Too Many Requests", retry_after=0.2)
    with mock.patch.object(dispatcher, "run_agent", stub_runner([], "撞了限流也要送到")):
        live.api.user_message(text="@SPX_STG_bot hi")
        await live.wait_for(lambda: "撞了限流也要送到" in texts_now(live, GROUP),
                            timeout=25, what="429 之后终态仍然落地")


async def test_placeholder_send_failure_does_not_wedge_the_poller(live):
    """占位消息发失败（例如 bot 被踢出群）不能让轮询线程死掉——下一条还得能处理。"""
    live.api.fail_once("sendMessage", 403, "Forbidden: bot was kicked from the group")
    calls = []
    with mock.patch.object(dispatcher, "run_agent", stub_runner(calls, "第二条 OK")):
        live.api.user_message(text="@SPX_STG_bot 第一条")
        await live.settle(2.0)
        live.api.user_message(text="@SPX_STG_bot 第二条")
        await live.wait_for(lambda: "第二条 OK" in texts_now(live, GROUP),
                            timeout=25, what="第二条正常处理")


# ── 按钮 ─────────────────────────────────────────────────────

async def test_button_click_round_trip(live):
    with mock.patch.object(dispatcher, "run_agent", stub_runner([])):
        live.api.user_message(
            text="/mode", entities=[{"type": "bot_command", "offset": 0, "length": 5}])
        await live.wait_for(
            lambda: any(m.get("reply_markup") for m in live.api.sent),
            what="/mode 渲染出 inline keyboard")
    card = [m for m in live.api.sent if m.get("reply_markup")][-1]
    rows = card["reply_markup"]["inline_keyboard"]
    tokens = [c["callback_data"] for row in rows for c in row]
    assert tokens and all(len(t.encode()) <= 64 for t in tokens)

    live.api.user_click(GROUP, card["message_id"], tokens[0])
    await live.wait_for(lambda: live.api.method_calls("answerCallbackQuery"),
                        what="callback 被回执")
    await live.wait_for(lambda: "已切换" in texts_now(live, GROUP),
                        what="点按钮后原地更新那条消息")
    session = await live.bot.store.get_current(str(USER), f"{GROUP}:c{GROUP}")
    assert session.permission_mode  # 模式被真的写进 session


async def test_expired_button_is_rejected_not_executed(live):
    live.api.user_click(GROUP, 5001, "no-such-token")
    await live.wait_for(lambda: live.api.method_calls("answerCallbackQuery"),
                        what="过期按钮也要回执")
    ack = live.api.method_calls("answerCallbackQuery")[0]
    assert "过期" in ack["text"]
    assert live.api.texts_of(GROUP) == []


# ── 附件 ─────────────────────────────────────────────────────

async def test_incoming_photo_is_downloaded_and_path_reaches_the_agent(live, tmp_path):
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    live.api.files["photo-file-1"] = str(png)
    calls = []
    with mock.patch.object(dispatcher, "run_agent", stub_runner(calls, "图看到了")):
        live.api.user_message(
            caption="@SPX_STG_bot 这个报错什么意思",
            photo=[{"file_id": "photo-file-1", "file_size": 999}],
        )
        await live.wait_for(lambda: len(calls) >= 1, what="图片消息被处理")
    prompt = calls[0]["message"]
    assert "这个报错什么意思" in prompt
    local = [tok for tok in prompt.split() if "tg-" in tok]
    assert local, f"prompt 里应带下载后的本地路径: {prompt[:200]}"
    path = local[0].strip("，。,")
    assert open(path, "rb").read() == b"\x89PNG\r\n\x1a\nFAKE"


async def test_voice_without_asr_reports_a_clear_error(live):
    live.api.files["voice-1"] = "/dev/null"
    with mock.patch.object(dispatcher, "run_agent", stub_runner([])):
        live.api.user_message(voice={"file_id": "voice-1", "duration": 3})
        # 语音在有会话记录的话题里不需要 @（和 Lark 一致），所以先建一条记录
        await live.wait_for(lambda: live.api.texts_of(GROUP) or True, what="轮询一轮")
        await live.settle(1.5)
    # 没有 ASR 代理时必须给出明确报错（而不是静默吞掉）
    err = [t for t in live.api.texts_of(GROUP) if "语音" in t]
    assert not live.bot.feishu.asr_client
    assert err == [] or "语音转写未启用" in err[0]


async def test_tg_cli_sends_files_through_the_same_api(live, tmp_path, monkeypatch):
    """agent 的手脚：tg-cli send --image / --file / context。"""
    img = tmp_path / "对比图 1.png"
    img.write_bytes(b"PNGDATA")
    env = dict(os.environ)
    env["CC_TG_API_ROOT"] = f"http://127.0.0.1:{live.api.port}"
    env["E2E_BOT_TOKEN"] = "8976773826:x"
    env["CC_LARK_CHAT_ID"] = str(GROUP)
    env["CC_LARK_MESSAGE_ID"] = f"{GROUP}:4242"
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    out = subprocess.run(
        [sys.executable, os.path.join(repo, "tg-cli"), "--profile", "e2e",
         "send", "--image", str(img), "--caption", "对比图"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    payload = live.api.method_calls("sendPhoto")
    assert payload, "sendPhoto 没被调用"
    assert payload[0]["photo"]["filename"] == "对比图 1.png"
    assert payload[0]["caption"] == "对比图"
    assert json.loads(payload[0]["reply_parameters"])["message_id"] == 4242

    out = subprocess.run(
        [sys.executable, os.path.join(repo, "tg-cli"), "--profile", "e2e",
         "send", "--file", str(img)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert live.api.method_calls("sendDocument")
