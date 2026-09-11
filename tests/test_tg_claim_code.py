"""自助授权口令：机主私聊 bot 发一串口令 → 当场入白名单并落盘。

存在的意义：Telegram 白名单填的是数字 user id，而机主一开始并不知道自己的 id。
没有这条通道就得「先发消息 → 有人去日志里捞 id → 写 .env → 再重启一次」，中间那个
"有人"不在就卡死。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tg_allowlist
import telegram_gateway as gw
from bot_config import Profile, _load_profile
from bot_instance import BotInstance

CODE = "spx-tg-claim-7f3a91"


@pytest.fixture(autouse=True)
def _isolate_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TG_ALLOWLIST_DIR", str(tmp_path / "allow"))


def _bot(**kw) -> BotInstance:
    profile = Profile(
        name="tgclaim", app_id="42", app_secret="42:s", platform="telegram",
        domain="https://api.telegram.org", default_cwd="/tmp", bot_token="42:s",
        allowed_open_ids=set(kw.pop("allowed", set())),
        allowed_group_chat_ids={"*"},
        claim_code=kw.pop("code", CODE),
        **kw,
    )
    bot = BotInstance(profile)
    bot.feishu._bot_id = 42
    bot.feishu._app_id = "42"
    bot.feishu._bot_username = "spx_bot"
    return bot


def _poller(bot):
    sent = []

    def submit(coro):
        sent.append(coro)
        coro.close()

    async def on_message(b, ev):  # pragma: no cover
        pass

    return gw.TgPoller(bot, submit=submit, on_message=on_message,
                       touch=lambda: None, started_at=1700000000), sent


def _dm(text, uid=777):
    return {
        "message_id": 1, "date": 1700000000,
        "chat": {"id": uid, "type": "private"},
        "from": {"id": uid, "first_name": "Yixin"},
        "text": text,
    }


def _group_msg(text, uid=777):
    return {
        "message_id": 2, "date": 1700000000,
        "chat": {"id": -100123, "type": "supergroup"},
        "from": {"id": uid, "first_name": "Yixin"},
        "text": text,
    }


def test_correct_code_in_dm_authorizes_and_persists():
    bot = _bot()
    poller, sent = _poller(bot)

    assert poller.deliver(_dm(CODE)) is False        # 口令本身不当任务跑
    assert len(sent) == 1                            # 只回了一条"已授权"
    assert "777" in bot.profile.allowed_open_ids     # 本进程内立刻生效

    # 落盘：换个进程（重新 _load_profile）也认
    stored = json.load(open(tg_allowlist.path_for("tgclaim"), encoding="utf-8"))
    assert stored["user_ids"] == ["777"]
    assert oct(os.stat(tg_allowlist.path_for("tgclaim")).st_mode)[-3:] == "600"


def test_authorized_user_can_work_right_after_claiming():
    bot = _bot()
    poller, sent = _poller(bot)
    poller.deliver(_dm(CODE))
    sent.clear()

    assert poller.deliver(_dm("帮我看下 sgb 的开户失败", uid=777)) is True
    assert len(sent) == 1                            # 这条真派给 dispatcher 了


def test_persisted_ids_are_merged_at_profile_load(monkeypatch):
    tg_allowlist.add("tgload", "999")
    monkeypatch.setenv("TGLOAD_PLATFORM", "telegram")
    monkeypatch.setenv("TGLOAD_BOT_TOKEN", "42:s")
    monkeypatch.setenv("TGLOAD_ALLOWED_OPEN_IDS", "111")
    profile = _load_profile("tgload")
    assert profile.allowed_open_ids == {"111", "999"}


def test_code_in_a_group_is_ignored():
    """群里发口令一律无效——口令会留在群历史里，不能长期有效。"""
    bot = _bot()
    poller, sent = _poller(bot)
    assert poller.deliver(_group_msg(CODE)) is False
    assert bot.profile.allowed_open_ids == set()
    assert tg_allowlist.load("tgclaim") == set()


def test_wrong_or_embedded_code_does_not_authorize():
    bot = _bot()
    poller, sent = _poller(bot)
    for text in ("spx-tg-claim-WRONG", f"口令是 {CODE}", f"{CODE} 顺便帮我看下日志",
                 CODE.upper(), ""):
        assert poller.deliver(_dm(text)) is False
    assert bot.profile.allowed_open_ids == set()
    assert tg_allowlist.load("tgclaim") == set()


def test_without_a_configured_code_the_channel_is_closed():
    bot = _bot(code="")
    poller, sent = _poller(bot)
    gw._revealed.clear()
    assert poller.deliver(_dm(CODE)) is False
    assert bot.profile.allowed_open_ids == set()
    # 只会收到那条"你的 user id 是 X"的提示
    assert len(sent) == 1


def test_claim_is_idempotent():
    bot = _bot()
    poller, sent = _poller(bot)
    poller.deliver(_dm(CODE))
    poller.deliver(_dm(CODE))
    assert tg_allowlist.load("tgclaim") == {"777"}
    assert len(sent) == 2                            # 两次都给了回执


def test_a_second_person_can_also_claim():
    """口令不作废：机主可能有第二台设备、或要给同事开通。每次授权都有 warn 日志。"""
    bot = _bot()
    poller, sent = _poller(bot)
    poller.deliver(_dm(CODE, uid=777))
    poller.deliver(_dm(CODE, uid=888))
    assert tg_allowlist.load("tgclaim") == {"777", "888"}


def test_corrupt_allowlist_file_is_ignored(tmp_path):
    target = tg_allowlist.path_for("broken")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    open(target, "w").write("{不是 json")
    assert tg_allowlist.load("broken") == set()
    assert tg_allowlist.add("broken", "5") is True
    assert tg_allowlist.load("broken") == {"5"}
