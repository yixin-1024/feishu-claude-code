"""外部群监听器：拍平 / 判定 / 状态 / 上下文 的单元测试。

重点覆盖两个曾经真实踩过的坑：
  1) 话题群里回复挂在根消息的 thread_replies 下，只扫顶层就永远扫不到；
  2) 用正文文本匹配 `@名字` 判 @ 会让 agent 自己的回复触发自己（死循环）。
"""

from __future__ import annotations

import json

import pytest

from external_watcher.config import ExternalWatcherConfig, GroupConfig
from external_watcher.lark_user_api import (
    chunk_text,
    extract_file_keys,
    extract_image_keys,
    strip_resource_markup,
    unwrap_card,
)
from external_watcher.state import SEEN_LIMIT, SELF_SENT_LIMIT, WatcherState
from external_watcher.thread_context import (
    build_thread_context,
    extract_attachments,
    extract_text,
    select_unseen,
    sender_label,
)
from external_watcher.watcher import (
    ExternalWebWatcher,
    flatten_thread_messages,
    mentions_owner,
)
from tests.fake_lark_user_api import FakeLarkUserApi

OWNER = "ou_owner"
OTHER = "ou_tyler"


def msg(
    mid,
    *,
    sender_id=OTHER,
    sender_name="Tyler",
    content="hi",
    msg_type="text",
    mentions=None,
    thread_id="omt_1",
    chat_id="oc_g",
    create_time="2026-09-04 10:00",
    position="1",
    replies=None,
    deleted=False,
):
    m = {
        "message_id": mid,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "msg_type": msg_type,
        "content": content,
        "create_time": create_time,
        "message_position": position,
        "deleted": deleted,
        "sender": {"id": sender_id, "name": sender_name, "sender_type": "user"},
    }
    if mentions is not None:
        m["mentions"] = mentions
    if replies is not None:
        m["thread_replies"] = replies
    return m


def mention(open_id=OWNER, name="Lu Yixin"):
    return [{"id": open_id, "key": "@_user_1", "name": name}]


# ── 拍平 ────────────────────────────────────────────────────────

def test_flatten_includes_thread_replies():
    """核心回归：话题内的回复必须被扫到（旧实现只看顶层，永远漏）。"""
    root = msg("om_root", create_time="2026-09-04 10:00", position="1",
               replies=[
                   msg("om_r1", create_time="2026-09-04 10:05", position="2"),
                   msg("om_r2", create_time="2026-09-04 10:06", position="3"),
               ])
    flat = flatten_thread_messages([root])
    assert [m["message_id"] for m in flat] == ["om_root", "om_r1", "om_r2"]


def test_flatten_sorts_chronologically_across_threads():
    a = msg("om_a", thread_id="omt_a", create_time="2026-09-04 10:10", position="5")
    b = msg("om_b", thread_id="omt_b", create_time="2026-09-04 10:01", position="2",
            replies=[msg("om_b1", thread_id="omt_b", create_time="2026-09-04 10:20", position="9")])
    flat = flatten_thread_messages([a, b])
    assert [m["message_id"] for m in flat] == ["om_b", "om_a", "om_b1"]


def test_flatten_dedupes_and_backfills_thread_id():
    root = msg("om_root", thread_id="omt_x")
    reply = {"message_id": "om_r", "msg_type": "text", "content": "y",
             "create_time": "2026-09-04 10:05", "message_position": "2",
             "sender": {"id": OTHER, "name": "Tyler"}}
    root["thread_replies"] = [reply, dict(reply)]  # 同一条出现两次
    flat = flatten_thread_messages([root])
    assert len(flat) == 2
    assert flat[1]["thread_id"] == "omt_x"
    assert flat[1]["chat_id"] == "oc_g"


def test_flatten_handles_empty_and_none():
    assert flatten_thread_messages([]) == []
    assert flatten_thread_messages([None, {}]) == []


# ── @ 判定 ──────────────────────────────────────────────────────

def test_mentions_owner_by_open_id():
    assert mentions_owner(msg("m", mentions=mention()), OWNER, "Lu Yixin")


def test_mentions_owner_ignores_other_person():
    assert not mentions_owner(
        msg("m", mentions=mention(OTHER, "Tyler")), OWNER, "Lu Yixin"
    )


def test_mentions_owner_never_matches_plain_text():
    """死循环防线：正文里出现 '@Lu Yixin' 但没有结构化 mention → 不算 @。

    agent 代发的回复里引用别人原话时极易出现这几个字，认文本就会自己触发自己。
    """
    m = msg("m", content="他刚才说 @Lu Yixin 你看一下", mentions=[])
    assert not mentions_owner(m, OWNER, "Lu Yixin")


def test_mentions_owner_name_fallback_when_no_open_id():
    m = msg("m", mentions=[{"key": "@_user_1", "name": "Lu Yixin"}])
    assert mentions_owner(m, OWNER, "Lu Yixin")


# ── 触发矩阵 ────────────────────────────────────────────────────

@pytest.fixture
def watcher(tmp_path, monkeypatch):
    cfg_path = tmp_path / "ext.yaml"
    cfg_path.write_text(json.dumps({
        "enabled": True,
        "user_profile": "spx",
        "owner_name": "Lu Yixin",
        "owner_open_id": OWNER,
        "session_dir": str(tmp_path),
        "download_dir": str(tmp_path / "dl"),
        "groups": [{"chat_id": "oc_g", "name": "外部群", "runner": "agy"}],
    }), encoding="utf-8")
    w = ExternalWebWatcher(str(cfg_path))
    return w


def group(**kw):
    base = dict(chat_id="oc_g", name="外部群", runner="agy")
    base.update(kw)
    return GroupConfig(**base)


def test_trigger_other_person_with_mention(watcher):
    assert watcher._should_trigger(msg("m", mentions=mention()), group())


def test_no_trigger_other_person_without_mention(watcher):
    assert not watcher._should_trigger(msg("m", mentions=[]), group())


def test_trigger_other_person_without_mention_when_not_required(watcher):
    assert watcher._should_trigger(msg("m", mentions=[]), group(require_mention=False))


def test_trigger_owner_self_mention(watcher):
    """用户明确要的：自己 @ 自己也触发。"""
    m = msg("m", sender_id=OWNER, sender_name="Lu Yixin", mentions=mention())
    assert watcher._should_trigger(m, group())


def test_no_trigger_owner_plain_message(watcher):
    """机主随口说话不触发——否则代发的回复会自己回自己。"""
    m = msg("m", sender_id=OWNER, sender_name="Lu Yixin", mentions=[])
    assert not watcher._should_trigger(m, group())


def test_no_trigger_owner_plain_message_even_when_mention_not_required(watcher):
    """require_mention=False 也不能放开机主自己的消息（死循环防线）。"""
    m = msg("m", sender_id=OWNER, sender_name="Lu Yixin", mentions=[])
    assert not watcher._should_trigger(m, group(require_mention=False))


def test_no_trigger_owner_when_self_mention_disabled(watcher):
    m = msg("m", sender_id=OWNER, sender_name="Lu Yixin", mentions=mention())
    assert not watcher._should_trigger(m, group(allow_self_mention=False))


def test_no_trigger_for_self_sent_reply(watcher):
    """监听器自己代发出去的消息，永远不能再触发一轮。"""
    watcher.state.mark_self_sent(["om_self"])
    m = msg("om_self", sender_id=OWNER, sender_name="Lu Yixin", mentions=mention())
    assert not watcher._should_trigger(m, group())


@pytest.mark.parametrize("msg_type", ["image", "file", "interactive", "system", "audio"])
def test_no_trigger_for_non_text_types(watcher, msg_type):
    m = msg("m", msg_type=msg_type, mentions=mention())
    assert not watcher._should_trigger(m, group())


def test_no_trigger_for_deleted(watcher):
    assert not watcher._should_trigger(msg("m", mentions=mention(), deleted=True), group())


def test_post_type_triggers(watcher):
    assert watcher._should_trigger(msg("m", msg_type="post", mentions=mention()), group())


# ── _check_group：基线 + 增量 ───────────────────────────────────

async def test_check_group_baseline_then_increment(watcher, monkeypatch):
    fired: list[str] = []
    monkeypatch.setattr(watcher, "_spawn", lambda g, m: fired.append(m["message_id"]))

    root = msg("om_root", mentions=mention(), replies=[msg("om_old", mentions=mention())])
    api = FakeLarkUserApi(chat_messages=[root])
    watcher.api = api

    # 第一轮：建基线，历史一条都不许触发
    await watcher._check_group(group())
    assert fired == []
    assert watcher.state.is_initialized("oc_g")

    # 第二轮：新回复进来才触发，且只触发新的那条
    root["thread_replies"].append(
        msg("om_new", mentions=mention(), create_time="2026-09-04 10:30", position="9")
    )
    await watcher._check_group(group())
    assert fired == ["om_new"]

    # 第三轮：没有新消息 → 不重复触发
    await watcher._check_group(group())
    assert fired == ["om_new"]


async def test_check_group_marks_seen_before_dispatch(watcher, monkeypatch):
    """分发抛异常也不能让同一条消息在下一轮重复触发。"""
    def boom(g, m):
        raise RuntimeError("dispatch exploded")

    api = FakeLarkUserApi(chat_messages=[msg("om_a", mentions=mention())])
    watcher.api = api
    watcher.state.mark_seen("oc_g", ["baseline"])  # 跳过基线阶段

    monkeypatch.setattr(watcher, "_spawn", boom)
    with pytest.raises(RuntimeError):
        await watcher._check_group(group())
    assert watcher.state.has_seen("oc_g", "om_a")


async def test_check_group_survives_api_error(watcher):
    api = FakeLarkUserApi()

    async def boom(*a, **k):
        raise RuntimeError("network down")

    api.list_chat_messages = boom
    watcher.api = api
    await watcher._poll_once()  # 不应该把循环带崩


# ── 状态持久化 ──────────────────────────────────────────────────

def test_state_roundtrip(tmp_path):
    path = str(tmp_path / "s.json")
    s = WatcherState(path)
    s.mark_seen("oc_g", ["a", "b"])
    s.mark_self_sent(["x"])
    s.update_thread("oc_g", "omt_1", session_id="sess-1", last_seen="b")
    s.save()

    s2 = WatcherState(path)
    assert s2.is_initialized("oc_g")
    assert s2.has_seen("oc_g", "a") and not s2.has_seen("oc_g", "zzz")
    assert s2.is_self_sent("x")
    st = s2.get_thread("oc_g", "omt_1")
    assert st.session_id == "sess-1" and st.last_seen == "b"


def test_state_survives_corrupt_file(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    s = WatcherState(str(path))
    assert s.seen == {} and not s.is_initialized("oc_g")


def test_state_seen_is_capped(tmp_path):
    s = WatcherState(str(tmp_path / "s.json"))
    s.mark_seen("oc_g", [f"m{i}" for i in range(SEEN_LIMIT + 50)])
    assert len(s.seen["oc_g"]) == SEEN_LIMIT
    assert not s.has_seen("oc_g", "m0")           # 最旧的被挤掉
    assert s.has_seen("oc_g", f"m{SEEN_LIMIT + 49}")
    assert len(s._seen_index["oc_g"]) == SEEN_LIMIT  # 索引跟着收缩，不泄漏


def test_state_self_sent_is_capped(tmp_path):
    s = WatcherState(str(tmp_path / "s.json"))
    s.mark_self_sent([f"m{i}" for i in range(SELF_SENT_LIMIT + 10)])
    assert len(s.self_sent) == SELF_SENT_LIMIT
    assert len(s._self_sent_index) == SELF_SENT_LIMIT


def test_state_initialized_only_after_mark(tmp_path):
    s = WatcherState(str(tmp_path / "s.json"))
    assert not s.is_initialized("oc_g")
    s.mark_seen("oc_g", [])
    assert s.is_initialized("oc_g")   # 空群也算建过基线


# ── 内容解析 ────────────────────────────────────────────────────

def test_extract_image_keys_and_strip():
    content = "![Image](img_v3_abc-1)\n看一下这个 ![Image](img_v3_abc-1) 和 ![x](img_v3_def)"
    assert extract_image_keys(content) == ["img_v3_abc-1", "img_v3_def"]
    assert strip_resource_markup(content) == "看一下这个  和"


def test_extract_file_keys():
    assert extract_file_keys("[报表.xlsx](file_v3_zzz)") == [("报表.xlsx", "file_v3_zzz")]


def test_unwrap_card():
    assert unwrap_card("<card>\n正文\n</card>") == "正文"
    assert unwrap_card("普通文本") == "普通文本"


def test_chunk_text_splits_on_line_boundary():
    text = "\n".join("x" * 40 for _ in range(20))
    chunks = chunk_text(text, size=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "\n".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_chunk_text_hard_splits_long_line():
    chunks = chunk_text("y" * 250, size=100)
    assert [len(c) for c in chunks] == [100, 100, 50]


def test_chunk_text_empty():
    assert chunk_text("") == [] and chunk_text("   ") == []


def test_extract_text_unwraps_card_and_drops_media():
    m = msg("m", msg_type="interactive", content="<card>报告正文</card>")
    assert extract_text(m) == "报告正文"
    m2 = msg("m", msg_type="post", content="![Image](img_v3_a) 看图")
    assert extract_text(m2) == "看图"


def test_extract_attachments_skips_cards():
    card = msg("m", msg_type="interactive", content="<card>![Image](img_v3_a)</card>")
    assert extract_attachments(card) == []
    post = msg("m", msg_type="post", content="![Image](img_v3_a)")
    assert extract_attachments(post) == [{"kind": "image", "key": "img_v3_a", "name": "img_v3_a"[:12]}]


def test_sender_label_marks_owner():
    assert sender_label(msg("m", sender_id=OWNER, sender_name="Lu Yixin"), OWNER) == "Lu Yixin(自己)"
    assert sender_label(msg("m"), OWNER) == "Tyler"


# ── select_unseen ───────────────────────────────────────────────

def test_select_unseen_after_last_seen():
    msgs = [msg(f"m{i}", position=str(i)) for i in range(5)]
    picked, truncated = select_unseen(msgs, last_seen="m1", current_message_id="m4")
    assert [m["message_id"] for m in picked] == ["m2", "m3"]
    assert not truncated


def test_select_unseen_no_last_seen_returns_all():
    msgs = [msg(f"m{i}") for i in range(3)]
    picked, truncated = select_unseen(msgs, last_seen="", current_message_id="m2")
    assert [m["message_id"] for m in picked] == ["m0", "m1"]
    assert not truncated


def test_select_unseen_falls_back_when_last_seen_out_of_window():
    msgs = [msg(f"m{i}") for i in range(40)]
    picked, truncated = select_unseen(msgs, last_seen="ancient", current_message_id="m39")
    assert truncated
    assert len(picked) == 15 and picked[-1]["message_id"] == "m38"


def test_select_unseen_empty_when_nothing_new():
    msgs = [msg("m0"), msg("m1")]
    picked, truncated = select_unseen(msgs, last_seen="m0", current_message_id="m1")
    assert picked == [] and not truncated


# ── build_thread_context ────────────────────────────────────────

async def test_build_thread_context_format(tmp_path):
    api = FakeLarkUserApi(thread_messages=[
        msg("m0", content="第一句", create_time="2026-09-04 10:00"),
        msg("m1", sender_id=OWNER, sender_name="Lu Yixin", content="我的回复",
            create_time="2026-09-04 10:05"),
        msg("m2", content="现在这条", create_time="2026-09-04 10:10"),
    ])
    ctx, paths, err = await build_thread_context(
        api, "oc_g", "omt_1", last_seen="", current_message_id="m2",
        owner_open_id=OWNER, download_dir=str(tmp_path),
    )
    assert err is None and paths == []
    assert ctx.startswith("【话题历史 · 2 条（按时间顺序）】")
    assert "[1] Tyler (09-04 10:00): 第一句" in ctx
    assert "[2] Lu Yixin(自己) (09-04 10:05): 我的回复" in ctx
    assert "现在这条" not in ctx  # 当前消息不进上下文


async def test_build_thread_context_increment_prefix(tmp_path):
    api = FakeLarkUserApi(thread_messages=[
        msg("m0"), msg("m1", content="新的"), msg("m2"),
    ])
    ctx, _p, err = await build_thread_context(
        api, "oc_g", "omt_1", last_seen="m0", current_message_id="m2",
        owner_open_id=OWNER, download_dir=str(tmp_path),
    )
    assert err is None
    assert ctx.startswith("【话题新增 · 1 条（距上次处理后）】")


async def test_build_thread_context_downloads_attachments(tmp_path):
    api = FakeLarkUserApi(thread_messages=[
        msg("m0", msg_type="post", content="![Image](img_v3_a) 看图"),
        msg("m1"),
    ])
    ctx, paths, err = await build_thread_context(
        api, "oc_g", "omt_1", last_seen="", current_message_id="m1",
        owner_open_id=OWNER, download_dir=str(tmp_path),
    )
    assert err is None
    assert len(paths) == 1 and paths[0].endswith(".jpg")
    assert "· 附件(image): " in ctx
    assert api.downloads == [("m0", "img_v3_a", "image")]


async def test_build_thread_context_download_failure_is_inline(tmp_path):
    api = FakeLarkUserApi(thread_messages=[
        msg("m0", msg_type="post", content="![Image](img_v3_a)"), msg("m1"),
    ])
    api.fail_download = RuntimeError("403")
    ctx, paths, err = await build_thread_context(
        api, "oc_g", "omt_1", last_seen="", current_message_id="m1",
        owner_open_id=OWNER, download_dir=str(tmp_path),
    )
    assert err is None and paths == []
    assert "[image 下载失败" in ctx


async def test_build_thread_context_reports_error(tmp_path):
    api = FakeLarkUserApi()
    api.fail_thread_list = RuntimeError("no permission")
    ctx, paths, err = await build_thread_context(
        api, "oc_g", "omt_1", last_seen="", current_message_id="m1",
        owner_open_id=OWNER, download_dir=str(tmp_path),
    )
    assert ctx == "" and paths == [] and "no permission" in err


async def test_build_thread_context_no_thread_id(tmp_path):
    api = FakeLarkUserApi(thread_messages=[msg("m0")])
    ctx, paths, err = await build_thread_context(
        api, "oc_g", "", last_seen="", current_message_id="m1",
        owner_open_id=OWNER, download_dir=str(tmp_path),
    )
    assert ctx == "" and err is None


# ── 配置 ────────────────────────────────────────────────────────

def test_config_defaults(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("enabled: true\ngroups:\n  - chat_id: oc_x\n", encoding="utf-8")
    c = ExternalWatcherConfig.load(str(p))
    assert c.enabled and c.poll_interval_sec == 5.0 and c.max_concurrent == 3
    assert c.browser_listener is False
    assert c.groups[0].require_mention and c.groups[0].allow_self_mention


def test_config_missing_file_disables():
    assert ExternalWatcherConfig.load("/nonexistent/x.yaml").enabled is False


def test_config_drops_groups_without_chat_id(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("enabled: true\ngroups:\n  - name: 没有 id\n  - chat_id: oc_ok\n", encoding="utf-8")
    assert [g.chat_id for g in ExternalWatcherConfig.load(str(p)).groups] == ["oc_ok"]


# ── lark-cli 调用层 ─────────────────────────────────────────────

class _FakeProc:
    def __init__(self, rc, out=b"", err=b""):
        self.returncode, self._out, self._err = rc, out, err

    async def communicate(self):
        return self._out, self._err

    def kill(self):
        pass


def _patch_exec(monkeypatch, proc):
    async def fake_exec(*a, **k):
        return proc
    import asyncio as _a
    monkeypatch.setattr(_a, "create_subprocess_exec", fake_exec)


async def test_run_surfaces_structured_error_from_stderr(monkeypatch):
    """回归：lark-cli 失败时把错误 JSON 打到 **stderr**，stdout 是空的。

    只看 stdout 会把「230027 user_unauthorized」糊成一句「输出不是 JSON」，
    线上排查时完全看不出真因（真踩过）。
    """
    from external_watcher.lark_user_api import LarkCliError, LarkUserApi

    err = json.dumps({
        "ok": False,
        "error": {"code": 230027, "subtype": "user_unauthorized",
                  "message": "access denied for this operation"},
    }).encode()
    _patch_exec(monkeypatch, _FakeProc(3, out=b"", err=err))

    with pytest.raises(LarkCliError) as ei:
        await LarkUserApi("spx")._run(["im", "+messages-reply"])
    detail = str(ei.value)
    assert "230027" in detail and "user_unauthorized" in detail
    assert "输出不是 JSON" not in detail


async def test_run_prefers_stdout_when_both_present(monkeypatch):
    from external_watcher.lark_user_api import LarkUserApi

    _patch_exec(monkeypatch, _FakeProc(
        0, out=b'{"ok": true, "data": {"from": "stdout"}}', err=b'{"ok": false}'))
    data = await LarkUserApi("spx")._run(["im", "x"])
    assert data == {"from": "stdout"}


async def test_run_raises_when_neither_stream_is_json(monkeypatch):
    from external_watcher.lark_user_api import LarkCliError, LarkUserApi

    _patch_exec(monkeypatch, _FakeProc(127, out=b"", err=b"command not found"))
    with pytest.raises(LarkCliError) as ei:
        await LarkUserApi("spx")._run(["im", "x"])
    assert "输出不是 JSON" in str(ei.value) and "command not found" in str(ei.value)


async def test_blocked_group_stops_spawning_but_keeps_watermark(watcher, monkeypatch):
    """群被熔断后：不再起 agent，但已读水位线继续跟进（恢复时不回放一大堆）。"""
    fired: list[str] = []
    monkeypatch.setattr(watcher, "_spawn", lambda g, m: fired.append(m["message_id"]))
    api = FakeLarkUserApi(chat_messages=[msg("om_a", mentions=mention())])
    watcher.api = api

    await watcher._check_group(group())          # 基线
    watcher._send_blocked.add("oc_g")
    api.chat_messages.append(msg("om_b", mentions=mention(),
                                 create_time="2026-09-04 10:30", position="9"))
    await watcher._check_group(group())

    assert fired == []
    assert watcher.state.has_seen("oc_g", "om_b")


async def test_send_blocked_trips_the_breaker(watcher, monkeypatch):
    from external_watcher.dispatcher_bridge import ExternalSendBlocked

    async def boom(**kw):
        raise ExternalSendBlocked("230027")

    monkeypatch.setattr("external_watcher.watcher.handle_external_message", boom)
    watcher._sem = __import__("asyncio").Semaphore(3)
    await watcher._run_one(group(), msg("om_a", mentions=mention()))
    assert "oc_g" in watcher._send_blocked
