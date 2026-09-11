"""外部群单轮编排（dispatcher_bridge）的端到端测试。

用假 lark-cli + 假 run_agent 把一整轮跑通，断言的是"喂给 agent 的 prompt 长什么样"
和"最终往群里发了什么"——这两件事出错时，线上表现是"回答驴唇不对马嘴"或"群里
乱发东西"，光靠不抛异常是测不出来的。
"""

from __future__ import annotations

import pytest

from bot_config import Profile
from external_watcher import dispatcher_bridge as bridge
from external_watcher.config import ExternalWatcherConfig, GroupConfig
from external_watcher.lark_user_api import LarkCliError
from external_watcher.state import WatcherState
from tests.fake_lark_user_api import FakeLarkUserApi
from tests.test_external_watcher import OWNER, OTHER, mention, msg

GROUP = GroupConfig(chat_id="oc_g", name="外部群", runner="agy", cwd="/tmp")


@pytest.fixture
def cfg(tmp_path):
    return ExternalWatcherConfig(
        enabled=True, user_profile="spx", owner_name="Lu Yixin",
        owner_open_id=OWNER, session_dir=str(tmp_path),
        download_dir=str(tmp_path / "dl"),
    )


@pytest.fixture
def state(tmp_path):
    return WatcherState(str(tmp_path / "s.json"))


@pytest.fixture(autouse=True)
def _fake_profile(monkeypatch):
    """别让测试去读真 .env 里的 profile 列表。"""
    p = Profile(
        name="spx", app_id="cli_t", app_secret="s", platform="lark",
        domain="open.larksuite.com", default_cwd="/tmp", runner="agy",
    )
    monkeypatch.setattr(bridge, "_resolve_profile", lambda name: p)
    return p


class RunAgentSpy:
    """替身 run_agent：记下参数，按需返回文本 / 抛异常。"""

    def __init__(self, reply="好的，我看一下。", session_id="sess-new", exc=None):
        self.reply, self.session_id, self.exc = reply, session_id, exc
        self.calls: list[dict] = []

    async def __call__(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return self.reply, self.session_id, False

    @property
    def prompt(self) -> str:
        return self.calls[-1]["message"]

    @property
    def system(self) -> str:
        return self.calls[-1]["append_system_prompt"]


async def run_turn(cfg, state, api, monkeypatch, *, incoming=None, spy=None, group=GROUP):
    spy = spy or RunAgentSpy()
    monkeypatch.setattr(bridge, "run_agent", spy)
    await bridge.handle_external_message(
        cfg=cfg, group=group, api=api, state=state,
        msg=incoming or msg("om_cur", content="@Lu Yixin 帮我看下这个报错",
                            mentions=mention()),
        owner_open_id=OWNER, owner_name="Lu Yixin",
        download_dir=cfg.download_dir,
    )
    return spy


# ── prompt 组装 ─────────────────────────────────────────────────

async def test_prompt_has_turn_header_context_and_body(cfg, state, monkeypatch):
    api = FakeLarkUserApi(thread_messages=[
        msg("m0", content="项目进度怎么样", create_time="2026-09-04 09:00"),
        msg("om_cur", content="@Lu Yixin 帮我看下这个报错", mentions=mention()),
    ])
    spy = await run_turn(cfg, state, api, monkeypatch)

    prompt = spy.prompt
    assert prompt.startswith("【本轮 · 消息 id: om_cur · 提问者 open_id: ou_tyler】")
    assert "【话题历史 · 1 条（按时间顺序）】" in prompt
    assert "[1] Tyler (09-04 09:00): 项目进度怎么样" in prompt
    assert "【Tyler 刚刚 @ 了你，说】" in prompt
    # @ 占位被剥掉，只剩真正的问题
    assert "帮我看下这个报错" in prompt
    assert "@Lu Yixin" not in prompt.split("【Tyler 刚刚")[-1]


async def test_prompt_without_thread_history_is_just_body(cfg, state, monkeypatch):
    api = FakeLarkUserApi(thread_messages=[msg("om_cur", mentions=mention())])
    spy = await run_turn(cfg, state, api, monkeypatch)
    assert "【话题" not in spy.prompt
    assert spy.prompt.endswith("帮我看下这个报错")


async def test_system_prompt_is_external_persona(cfg, state, monkeypatch):
    api = FakeLarkUserApi()
    spy = await run_turn(cfg, state, api, monkeypatch)
    sys_prompt = spy.system
    assert "Lu Yixin 本人的身份" in sys_prompt
    assert "--as user" in sys_prompt
    assert "外部群" in sys_prompt
    assert "${" not in sys_prompt          # 模板变量全部替换掉了
    assert "mcp__cc-lark__" not in sys_prompt or "当作不存在" in sys_prompt


async def test_no_runtime_mcp_context_leaks(cfg, state, monkeypatch):
    """外部群不给 CC_LARK_THREAD_ID：给了 agent 就会去排 wake/dispatch，
    而那些东西要往 bot 能发卡片的话题投递，这里 bot 根本不在群里。"""
    api = FakeLarkUserApi()
    spy = await run_turn(cfg, state, api, monkeypatch)
    wake = spy.calls[-1]["wake_context"]
    assert "CC_LARK_THREAD_ID" not in wake
    assert wake["CC_LARK_MESSAGE_ID"] == "om_cur"
    assert wake["CC_LARK_USER_ID"] == OTHER
    assert wake["CC_LARK_EXTERNAL"] == "1"


async def test_images_are_downloaded_and_described(cfg, state, monkeypatch):
    incoming = msg("om_cur", msg_type="post",
                   content="![Image](img_v3_a)\n@Lu Yixin 这个报错怎么解",
                   mentions=mention())
    api = FakeLarkUserApi()
    spy = await run_turn(cfg, state, api, monkeypatch, incoming=incoming)
    assert api.downloads == [("om_cur", "img_v3_a", "image")]
    assert "含 1 张图片" in spy.prompt
    assert "图片路径：" in spy.prompt
    assert "这个报错怎么解" in spy.prompt


async def test_image_download_failure_still_runs_with_text(cfg, state, monkeypatch):
    incoming = msg("om_cur", msg_type="post",
                   content="![Image](img_v3_a)\n@Lu Yixin 看下",
                   mentions=mention())
    api = FakeLarkUserApi()
    api.fail_download = RuntimeError("410 gone")
    spy = await run_turn(cfg, state, api, monkeypatch, incoming=incoming)
    assert "含" not in spy.prompt          # 没有图片就不吹嘘有图片
    assert "看下" in spy.prompt


# ── 回复 ────────────────────────────────────────────────────────

async def test_reply_is_posted_in_thread_as_user(cfg, state, monkeypatch):
    api = FakeLarkUserApi()
    await run_turn(cfg, state, api, monkeypatch)
    assert api.replies == [("om_cur", "好的，我看一下。", True)]


async def test_reply_ids_recorded_to_prevent_self_trigger(cfg, state, monkeypatch):
    api = FakeLarkUserApi()
    await run_turn(cfg, state, api, monkeypatch)
    assert state.is_self_sent("om_sent_1")
    assert state.has_seen("oc_g", "om_sent_1")


async def test_skip_sentinel_posts_nothing(cfg, state, monkeypatch):
    api = FakeLarkUserApi()
    await run_turn(cfg, state, api, monkeypatch, spy=RunAgentSpy(reply="-"))
    assert api.replies == []


async def test_empty_output_posts_nothing(cfg, state, monkeypatch):
    api = FakeLarkUserApi()
    await run_turn(cfg, state, api, monkeypatch, spy=RunAgentSpy(reply="   "))
    assert api.replies == []


async def test_reply_failure_notifies_owner_not_group(cfg, state, monkeypatch):
    api = FakeLarkUserApi()
    api.fail_reply = LarkCliError("token expired")
    await run_turn(cfg, state, api, monkeypatch)
    assert api.replies == []
    assert any("--user-id" in c and OWNER in c for c in api.raw_calls)


# ── 会话与水位线 ────────────────────────────────────────────────

async def test_session_is_created_then_resumed(cfg, state, monkeypatch):
    api = FakeLarkUserApi()
    spy = await run_turn(cfg, state, api, monkeypatch)
    assert spy.calls[-1]["session_id"] is None            # 首轮开新会话
    assert state.get_thread("oc_g", "omt_1").session_id == "sess-new"

    spy2 = await run_turn(cfg, state, api, monkeypatch,
                          incoming=msg("om_cur2", mentions=mention()))
    assert spy2.calls[-1]["session_id"] == "sess-new"     # 次轮续上


async def test_last_seen_advances_on_success(cfg, state, monkeypatch):
    api = FakeLarkUserApi(thread_messages=[msg("m0"), msg("om_cur", mentions=mention())])
    await run_turn(cfg, state, api, monkeypatch)
    assert state.get_thread("oc_g", "omt_1").last_seen == "om_cur"


async def test_last_seen_not_advanced_when_history_unreadable(cfg, state, monkeypatch):
    """读不到历史时不能推进水位线，否则这段 backlog 永远补不回来。"""
    api = FakeLarkUserApi()
    api.fail_thread_list = RuntimeError("no permission")
    await run_turn(cfg, state, api, monkeypatch)
    assert state.get_thread("oc_g", "omt_1").last_seen == ""
    assert api.replies  # 但当前这条还是要正常回


async def test_agent_failure_notifies_owner_and_keeps_watermark(cfg, state, monkeypatch):
    api = FakeLarkUserApi(thread_messages=[msg("m0"), msg("om_cur", mentions=mention())])
    spy = RunAgentSpy(exc=RuntimeError("runner died"))
    await run_turn(cfg, state, api, monkeypatch, spy=spy)
    assert api.replies == []                                    # 外部群里一个字不发
    assert state.get_thread("oc_g", "omt_1").last_seen == ""    # 水位线不推进
    assert any("runner died" in " ".join(c) for c in api.raw_calls)  # 私聊通报机主


async def test_error_notify_can_be_disabled(cfg, state, monkeypatch):
    cfg.notify_owner_on_error = False
    api = FakeLarkUserApi()
    await run_turn(cfg, state, api, monkeypatch, spy=RunAgentSpy(exc=RuntimeError("x")))
    assert api.raw_calls == []


async def test_group_runner_and_cwd_are_honored(cfg, state, monkeypatch):
    api = FakeLarkUserApi()
    g = GroupConfig(chat_id="oc_g", name="外部群", runner="claude",
                    model="opus", cwd="/srv/work")
    spy = await run_turn(cfg, state, api, monkeypatch, group=g)
    call = spy.calls[-1]
    assert call["runner"] == "claude" and call["model"] == "opus" and call["cwd"] == "/srv/work"


async def test_mention_only_message_with_history_still_runs(cfg, state, monkeypatch):
    """只 @ 一下没正文，但话题里有内容 → 照样跑，让它基于上下文回答。"""
    api = FakeLarkUserApi(thread_messages=[
        msg("m0", content="这个方案你怎么看"), msg("om_cur", mentions=mention()),
    ])
    incoming = msg("om_cur", content="@Lu Yixin", mentions=mention())
    spy = await run_turn(cfg, state, api, monkeypatch, incoming=incoming)
    assert "没有新正文" in spy.prompt
    assert api.replies


async def test_mention_only_message_without_history_is_skipped(cfg, state, monkeypatch):
    """只 @ 一下、话题里也没别的东西 → 不值当跑一次 runner。"""
    api = FakeLarkUserApi(thread_messages=[msg("om_cur", mentions=mention())])
    incoming = msg("om_cur", content="@Lu Yixin", mentions=mention())
    spy = RunAgentSpy()
    monkeypatch.setattr(bridge, "run_agent", spy)
    await bridge.handle_external_message(
        cfg=cfg, group=GROUP, api=api, state=state, msg=incoming,
        owner_open_id=OWNER, owner_name="Lu Yixin", download_dir=cfg.download_dir,
    )
    assert spy.calls == [] and api.replies == []


# ── 「可以不回」逃生阀的开关 ────────────────────────────────────

async def test_skip_sentinel_not_offered_when_mention_required(cfg, state, monkeypatch):
    """回归：默认（必须 @ 才唤醒）不能告诉 agent 可以不回。

    真机上 agy 拿到"@ 我 + 没什么正文"就一律吐 `-`，表现是"艾特了没反应"。
    """
    api = FakeLarkUserApi()
    spy = await run_turn(cfg, state, api, monkeypatch)
    assert "只输出一个字符" not in spy.system


async def test_skip_sentinel_offered_when_mention_not_required(cfg, state, monkeypatch):
    """require_mention=False 的群里什么都会触发，这时才需要让它自己判断该不该插嘴。"""
    api = FakeLarkUserApi()
    g = GroupConfig(chat_id="oc_g", name="外部群", runner="agy", require_mention=False)
    spy = await run_turn(cfg, state, api, monkeypatch, group=g)
    assert "只输出一个字符" in spy.system


async def test_sentinel_still_honored_if_agent_emits_it(cfg, state, monkeypatch):
    """哨兵的处理逻辑保留（没教它也不该在群里发一个光秃秃的 '-'）。"""
    api = FakeLarkUserApi()
    await run_turn(cfg, state, api, monkeypatch, spy=RunAgentSpy(reply="-"))
    assert api.replies == []


# ── 发送被永久拒绝时的熔断 ──────────────────────────────────────

async def test_permanent_send_denial_raises_blocked(cfg, state, monkeypatch):
    """230027 是永久性拒绝，重试无意义 → 抛 ExternalSendBlocked 让 watcher 熔断。"""
    api = FakeLarkUserApi()
    api.fail_reply = LarkCliError("denied", code=230027, subtype="user_unauthorized")
    with pytest.raises(bridge.ExternalSendBlocked):
        await run_turn(cfg, state, api, monkeypatch)
    # 通报里要带上白跑出来的答案，别让这轮的产出彻底丢掉
    note = " ".join(api.raw_calls[-1])
    assert "230027" in note and "好的，我看一下。" in note


async def test_transient_send_failure_does_not_block(cfg, state, monkeypatch):
    """网络抖动这类临时失败不熔断，下一条消息照常处理。"""
    api = FakeLarkUserApi()
    api.fail_reply = LarkCliError("timeout", code=0)
    await run_turn(cfg, state, api, monkeypatch)   # 不抛
    assert api.replies == []


# ── system prompt 变更 → 作废旧会话 ─────────────────────────────

async def test_session_reused_when_prompt_unchanged(cfg, state, monkeypatch):
    api = FakeLarkUserApi()
    await run_turn(cfg, state, api, monkeypatch)
    spy2 = await run_turn(cfg, state, api, monkeypatch,
                          incoming=msg("om_cur2", mentions=mention()))
    assert spy2.calls[-1]["session_id"] == "sess-new"


async def test_session_dropped_when_prompt_changes(cfg, state, monkeypatch):
    """回归：改了 system prompt 必须开新会话。

    `append_system_prompt` 只作用于本轮，已经写进对话历史的旧指令撤不掉 ——
    真机上删掉「可以输出 `-` 不回」的指令后，resume 出来的老会话照旧吐 `-`。
    """
    api = FakeLarkUserApi()
    await run_turn(cfg, state, api, monkeypatch)
    assert state.get_thread("oc_g", "omt_1").session_id == "sess-new"

    # 换个群名 → system prompt 变 → 指纹变
    g2 = GroupConfig(chat_id="oc_g", name="改了名字的群", runner="agy", cwd="/tmp")
    spy2 = await run_turn(cfg, state, api, monkeypatch, group=g2,
                          incoming=msg("om_cur2", mentions=mention()))
    assert spy2.calls[-1]["session_id"] is None


async def test_legacy_session_without_signature_is_dropped(cfg, state, monkeypatch):
    """升级前存下来的会话没有指纹，而它们恰恰是最需要作废的那批。"""
    state.update_thread("oc_g", "omt_1", session_id="poisoned-b7328190")
    assert state.get_thread("oc_g", "omt_1").prompt_sig == ""

    api = FakeLarkUserApi()
    spy = await run_turn(cfg, state, api, monkeypatch)
    assert spy.calls[-1]["session_id"] is None
    assert state.get_thread("oc_g", "omt_1").prompt_sig != ""


def test_prompt_sig_survives_state_reload(tmp_path):
    from external_watcher.state import WatcherState
    path = str(tmp_path / "s.json")
    s = WatcherState(path)
    s.update_thread("oc_g", "omt_1", session_id="a", prompt_sig="deadbeef1234")
    s.save()
    assert WatcherState(path).get_thread("oc_g", "omt_1").prompt_sig == "deadbeef1234"
