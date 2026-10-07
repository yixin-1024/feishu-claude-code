"""dots_runner：Lark 私聊 ⇄ 豆包 的传话筒——提示词整形、发送顺序、回复转发（含附件）、dispatcher 分流。

真 Chrome / 真 Dot 不在单测里碰：用一个假的 browser-harness（读 env 里预置的事件
逐行打印）顶替，走的是和线上一模一样的子进程 + JSON 行协议。
"""

import asyncio
import json
import os
import stat
import sys
import textwrap

import pytest

import dots_runner as dr


FAKE_HARNESS = textwrap.dedent("""\
    #!{python}
    import json, os, sys, time
    sys.stdin.read()  # 真 browser-harness 会把 stdin 当代码执行；假的只管吃掉
    action = os.environ.get("DOTS_ACTION")
    with open(os.environ["FAKE_DOTS_LOG"], "a") as f:
        f.write(json.dumps({{
            "action": action,
            "after": os.environ.get("DOTS_AFTER", ""),
            "prompt": open(os.environ["DOTS_PROMPT_FILE"]).read() if os.environ.get("DOTS_PROMPT_FILE") else None,
            "files": os.environ.get("DOTS_FILES", ""),
        }}, ensure_ascii=False) + "\\n")
    for ev in json.loads(os.environ.get("FAKE_DOTS_EVENTS_" + action.upper(), "[]")):
        if ev == "SLEEP":
            time.sleep(30)
            continue
        if isinstance(ev, (int, float)):
            time.sleep(ev)
            continue
        print(json.dumps(ev, ensure_ascii=False), flush=True)
""")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    harness = tmp_path / "browser-harness"
    harness.write_text(FAKE_HARNESS.format(python=sys.executable))
    harness.chmod(harness.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("DOTS_BROWSER_HARNESS_BIN", str(harness))
    monkeypatch.setenv("FAKE_DOTS_LOG", str(log))
    monkeypatch.setenv("CC_LARK_DOTS_STATE", str(tmp_path / "dots_state.json"))
    monkeypatch.setattr(dr.tempfile, "tempdir", str(tmp_path))  # wake 文件别落到真 /tmp
    dr._SEND_LOCKS.clear()
    dr._RELAYS.clear()
    # 转发器是常驻后台循环，单测里不起；要测它的用例直接调 relay_once
    monkeypatch.setattr(dr, "start_relay", lambda bot: None)

    class Fake:
        def events(self, action, evs):
            monkeypatch.setenv("FAKE_DOTS_EVENTS_" + action.upper(), json.dumps(evs, ensure_ascii=False))

        def calls(self):
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines()]

    return Fake()


def _msg(i, text, files=None):
    return {"ev": "msg", "id": f"m{i}", "text": text, "files": files or []}


# ── 提示词整形 ─────────────────────────────────────────────

def test_prompt_strips_turn_header_and_thread_history():
    msg = (
        "【本轮 · 消息 id: om_1 · 提问者 open_id: ou_1】\n\n"
        "【话题历史 · 2 条（按时间顺序）】\n[1] A: 旧话\n[2] B: 更旧\n\n"
        "【用户刚刚 @ 你并说】\n帮我查下天气\n第二行"
    )
    assert dr.build_dots_prompt(msg) == ("帮我查下天气\n第二行", [])


def test_prompt_can_forward_history_when_enabled():
    msg = "【本轮 · 消息 id: om_1】\n\n【话题历史 · 1 条】\n[1] A: 旧话\n\n【用户刚刚 @ 你并说】\n新问题"
    text, _ = dr.build_dots_prompt(msg, forward_history=True)
    assert text.startswith("【话题历史")
    assert text.endswith("新问题")


def test_prompt_strips_wake_reminder():
    msg = (
        "【本轮 · 消息 id: om_1】\n\n"
        "【⏰ 待办唤醒提醒】当前话题存在未触发的排定唤醒：原定 10:00 唤醒（note: 'x'）。请主动调用 `cancel_wake`。\n\n"
        "你好"
    )
    assert dr.build_dots_prompt(msg) == ("你好", [])


def test_prompt_image_becomes_upload(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    msg = f"【本轮 · 消息 id: om_1】\n\n[用户发送了一张图片，路径：{img}，请读取并分析这张图片，直接回复用中文]"
    assert dr.build_dots_prompt(msg) == ("", [str(img)])


def test_prompt_file_with_caption(tmp_path):
    f = tmp_path / "r.pdf"
    f.write_bytes(b"%PDF")
    msg = (
        f"[用户发送了文件：r.pdf，本地路径：{f}。请根据需要读取该文件并分析，用中文回复。]\n"
        "用户对这个文件的说明：总结一下"
    )
    assert dr.build_dots_prompt(msg) == ("总结一下", [str(f)])


def test_prompt_post_with_images(tmp_path):
    a, b = tmp_path / "1.png", tmp_path / "2.png"
    a.write_bytes(b"1")
    b.write_bytes(b"2")
    msg = (
        "[用户发送了富文本消息，含 2 张图片]\n文字内容：看这两张\n图片路径：\n"
        f"  - {a}\n  - {b}\n请读取并分析这些图片，结合文字回复（中文）。"
    )
    assert dr.build_dots_prompt(msg) == ("看这两张", [str(a), str(b)])


def test_prompt_voice_keeps_transcript_only():
    msg = "[用户发送了一条语音消息（3s），以下为自动转写，可能存在同音字/分词误差，请按口语理解]\n今天几号"
    assert dr.build_dots_prompt(msg) == ("今天几号", [])


def test_prompt_drops_missing_files():
    msg = "[用户发送了一张图片，路径：/nonexistent/x.png，请读取并分析这张图片，直接回复用中文]"
    assert dr.build_dots_prompt(msg) == ("", [])


def test_profile_override_beats_global(monkeypatch):
    monkeypatch.setenv("DOTS_NAME", "全局")
    monkeypatch.setenv("DOTSBOT_DOTS_NAME", "豆包")
    assert dr.dots_cfg("dotsbot", "NAME") == "豆包"
    assert dr.dots_cfg("other", "NAME") == "全局"



# ── 接线 ───────────────────────────────────────────────────

def test_wiring_accepts_dots_runner():
    from agent_alias import AGENT_RUNNER_ALIASES
    from bot_config import is_model_compatible_with_runner

    assert AGENT_RUNNER_ALIASES["dots"] == "dots"
    assert is_model_compatible_with_runner("dots", "dots")
    assert not is_model_compatible_with_runner("opus", "dots")


def test_session_store_set_runner_dots(tmp_path):
    import session_store

    store = session_store.SessionStore(profile="t", default_runner="dots")
    assert store._default_runner == "dots"
    asyncio.run(store.set_runner("ou_1", "oc_1", "dot", model="dots"))
    cur = asyncio.run(store.get_current("ou_1", "oc_1"))
    assert cur.runner == "dots"




# ── 进：Lark → 豆包 ─────────────────────────────────────────

class _FakeFeishu:
    def __init__(self, fail=False):
        self.cards = []
        self.fail = fail

    async def send_card_to_user(self, open_id, content="", loading=True):
        if self.fail:
            raise RuntimeError("lark down")
        self.cards.append((open_id, content, loading))
        return "om_card"


class _FakeBot:
    def __init__(self, name="p", fail=False, owners=("ou_owner",)):
        self.profile = type("P", (), {"name": name, "is_telegram": False, "runner": "dots",
                                      "allowed_open_ids": set(owners)})()
        self.feishu = _FakeFeishu(fail)


def _sent_ok(i):
    return [{"ev": "room", "room": "R1", "name": "豆包"}, {"ev": "sent", "id": f"u{i}"},
            {"ev": "done", "reason": "sent", "last_id": f"u{i}"}]


def test_forward_sends_binds_user_and_wakes_relay(fake, tmp_path):
    fake.events("send", _sent_ok(1))
    bot = _FakeBot()

    async def prepare():
        return "我12月想跑马拉松", []

    assert asyncio.run(dr.forward(bot, prepare, user_id="ou_me")) == "u1"
    assert fake.calls()[0]["action"] == "send"
    assert fake.calls()[0]["prompt"] == "我12月想跑马拉松"
    assert dr.get_state("p")["user_id"] == "ou_me"
    assert os.path.exists(dr._wake_path("p"))


def test_forward_empty_message_sends_nothing(fake):
    async def prepare():
        return "  ", []

    assert asyncio.run(dr.forward(_FakeBot(), prepare, user_id="ou_me")) == ""
    assert fake.calls() == []


def test_forward_keeps_arrival_order_even_if_first_needs_download(fake):
    fake.events("send", _sent_ok(1))

    async def go():
        bot = _FakeBot()

        async def slow():  # 第一条要先下载图片，慢
            await asyncio.sleep(0.4)
            return "第一条", []

        async def fast():
            return "第二条", []

        t1 = asyncio.create_task(dr.forward(bot, slow))
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(dr.forward(bot, fast))
        await asyncio.gather(t1, t2)

    asyncio.run(go())
    assert [c["prompt"] for c in fake.calls()] == ["第一条", "第二条"]


def test_forward_error_raises(fake):
    fake.events("send", [{"ev": "room", "room": "R1"},
                         {"ev": "error", "code": "composer_dirty", "msg": "网页输入框里有草稿"}])

    async def prepare():
        return "hi", []

    with pytest.raises(dr.DotsError) as ei:
        asyncio.run(dr.forward(_FakeBot(), prepare, user_id="ou_me"))
    assert "草稿" in str(ei.value)
    assert getattr(ei.value, "cc_retryable_resume", None) is False
    assert "user_id" not in dr.get_state("p")


def test_forward_passes_files(fake, tmp_path):
    fake.events("send", _sent_ok(1))
    img = tmp_path / "a.png"
    img.write_bytes(b"x")

    async def prepare():
        return "", [str(img)]

    asyncio.run(dr.forward(_FakeBot(), prepare))
    assert fake.calls()[0]["files"] == str(img)


def test_run_dots_only_sends_and_points_to_dm(fake):
    fake.events("send", _sent_ok(1))
    text, sid, fresh = asyncio.run(dr.run_dots(
        "【本轮 · 消息 id: om_1】\n\n【用户刚刚 @ 你并说】\n周报时间到了", profile_name="p",
        append_system_prompt="系统提示词不能发给豆包", on_text_chunk=lambda c: None,
    ))
    assert "私聊" in text and sid is None and fresh is False
    assert fake.calls()[0]["prompt"] == "周报时间到了"


def test_run_dots_error_is_not_retryable(fake):
    fake.events("send", [{"ev": "error", "code": "no_tab", "msg": "Chrome 里没找到 Dots 页面"}])
    with pytest.raises(dr.DotsError) as ei:
        asyncio.run(dr.run_dots("你好", profile_name="p"))
    assert "没找到 Dots 页面" in str(ei.value)
    assert ei.value.cc_retryable_resume is False


# ── 出：豆包 → Lark 私聊 ────────────────────────────────────

def test_render_text_notes_failed_attachments():
    ev = {"text": "截图在这", "files": [{"name": "a.jpg", "path": "/x"}, {"name": "b.pdf", "error": "HTTP 403"}]}
    assert dr.render_dot_text(ev) == "截图在这\n\n📎 b.pdf（没能取回：HTTP 403）"


def _capture_uploads(monkeypatch):
    sent = []

    async def img(bot, open_id, path):
        sent.append(("image", open_id, os.path.basename(path)))

    async def fil(bot, open_id, path, name):
        sent.append(("file", open_id, name))

    monkeypatch.setattr(dr, "_send_image", img)
    monkeypatch.setattr(dr, "_send_file", fil)
    return sent


def test_relay_forwards_every_dot_message_to_dm_with_attachments(fake, tmp_path, monkeypatch):
    uploads = _capture_uploads(monkeypatch)
    asyncio.run(dr._update_state("p", delivered_id="m0", user_id="ou_me"))
    jpg, pdf = tmp_path / "shot.jpg", tmp_path / "r.pdf"
    jpg.write_bytes(b"\xff\xd8")
    pdf.write_bytes(b"%PDF")
    fake.events("stream", [
        {"ev": "room", "room": "R1", "name": "豆包"},
        {"ev": "cursor", "id": "m0"},
        _msg(1, "稍等"),
        _msg(2, "截图和报告", files=[{"name": "shot.jpg", "mime": "image/jpeg", "path": str(jpg)},
                                  {"name": "r.pdf", "mime": "application/pdf", "path": str(pdf)}]),
        {"ev": "cursor", "id": "u3"},
    ])
    bot = _FakeBot()
    with pytest.raises(dr.DotsError) as ei:  # 假 driver 吐完就退出 = 转发器要重启
        asyncio.run(dr.relay_once(bot))
    assert ei.value.code == "driver_exit"
    assert [c[1] for c in bot.feishu.cards] == ["稍等", "截图和报告"]
    assert all(c[0] == "ou_me" and c[2] is False for c in bot.feishu.cards)
    assert uploads == [("image", "ou_me", "shot.jpg"), ("file", "ou_me", "r.pdf")]
    assert not jpg.exists() and not pdf.exists()  # 发完删掉本地副本
    assert fake.calls()[0]["after"] == "m0"
    st = dr.get_state("p")
    assert st["delivered_id"] == "u3" and st["room_id"] == "R1"


def test_relay_first_run_starts_from_latest_without_backfill(fake):
    fake.events("stream", [{"ev": "room", "room": "R1"}, {"ev": "cursor", "id": "m9"}])
    bot = _FakeBot()
    with pytest.raises(dr.DotsError):
        asyncio.run(dr.relay_once(bot))
    assert fake.calls()[0]["after"] == ""
    assert bot.feishu.cards == []
    assert dr.get_state("p")["delivered_id"] == "m9"


def test_relay_keeps_watermark_when_lark_delivery_fails(fake, monkeypatch):
    monkeypatch.setattr(dr.asyncio, "sleep", _no_sleep)
    asyncio.run(dr._update_state("p", delivered_id="m0", user_id="ou_me"))
    fake.events("stream", [{"ev": "room", "room": "R1"}, _msg(1, "a"), _msg(2, "b")])
    with pytest.raises(dr.DotsError) as ei:
        asyncio.run(dr.relay_once(_FakeBot(fail=True)))
    assert ei.value.code == "deliver"
    assert dr.get_state("p")["delivered_id"] == "m0"


async def _no_sleep(*_a, **_k):
    return None


def test_relay_surfaces_driver_error(fake):
    fake.events("stream", [{"ev": "error", "code": "no_tab", "msg": "Chrome 里没找到 Dots 页面"}])
    with pytest.raises(dr.DotsError) as ei:
        asyncio.run(dr.relay_once(_FakeBot()))
    assert ei.value.code == "no_tab"


def test_target_falls_back_to_owner_then_config(fake, monkeypatch):
    bot = _FakeBot(owners=("ou_b", "ou_a"))
    assert dr._target_open_id(bot) == "ou_a"
    monkeypatch.setenv("P_DOTS_NOTIFY_OPEN_ID", "ou_cfg")
    assert dr._target_open_id(bot) == "ou_cfg"
    asyncio.run(dr._update_state("p", user_id="ou_me"))
    assert dr._target_open_id(bot) == "ou_me"


# ── dispatcher 分流 ────────────────────────────────────────

class _Msg:
    def __init__(self, content, mtype="text"):
        self.message_type = mtype
        self.content = json.dumps(content)
        self.message_id = "om_new"
        self.mentions = None


def _handle(monkeypatch, msg, *, is_group=False, runner="dots", fail=None):
    import dispatcher

    bot = _FakeBot()
    bot.profile.runner = runner
    got, acks, replies = [], [], []

    async def fake_forward(b, prepare, user_id=""):
        if fail:
            raise dr.DotsError("x", fail)
        text, files = await prepare()
        got.append((text, files, user_id))
        return "u1"

    async def fake_ack(b, mid):
        acks.append(mid)

    async def reply_text(mid, text):
        replies.append(text)

    bot.feishu.reply_text = reply_text
    monkeypatch.setattr(dr, "forward", fake_forward)
    monkeypatch.setattr(dispatcher, "_dots_ack", fake_ack)
    handled = asyncio.run(dispatcher._dots_handle_message(bot, "ou_me", is_group, msg))
    return handled, got, acks, replies


def test_dm_text_goes_straight_to_dots(monkeypatch):
    handled, got, acks, _ = _handle(monkeypatch, _Msg({"text": "你好"}))
    assert handled and got == [("你好", [], "ou_me")] and acks == ["om_new"]


def test_group_messages_are_ignored(monkeypatch):
    handled, got, acks, replies = _handle(monkeypatch, _Msg({"text": "@Lark CLI 你好"}), is_group=True)
    assert handled and not got and not acks and not replies


def test_dm_commands_and_other_runners_fall_through(monkeypatch):
    assert not _handle(monkeypatch, _Msg({"text": "/status"}))[0]
    assert not _handle(monkeypatch, _Msg({"text": "hi"}), runner="claude")[0]
    assert not _handle(monkeypatch, _Msg({}, mtype="sticker"))[0]


def test_dm_image_is_downloaded_and_sent(monkeypatch):
    import dispatcher

    async def dl(mid, key):
        return f"/tmp/{key}.png"

    bot_holder = {}
    orig = _FakeBot.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        self.feishu.download_image = dl
        bot_holder["b"] = self

    monkeypatch.setattr(_FakeBot, "__init__", init)
    handled, got, _, _ = _handle(monkeypatch, _Msg({"image_key": "img_1"}, mtype="image"))
    assert handled and got == [("", ["/tmp/img_1.png"], "ou_me")]


def test_dm_forward_failure_is_reported(monkeypatch):
    handled, got, acks, replies = _handle(monkeypatch, _Msg({"text": "hi"}), fail="网页输入框里有草稿")
    assert handled and not acks
    assert replies and "草稿" in replies[0]


def test_retry_after_partial_delivery_does_not_resend_text(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(dr.asyncio, "sleep", _no_sleep)
    asyncio.run(dr._update_state("p", delivered_id="m0", user_id="ou_me"))
    jpg = tmp_path / "s.jpg"
    jpg.write_bytes(b"\xff\xd8")
    tries = []

    async def flaky_img(bot, open_id, path):
        tries.append(os.path.exists(path))
        if len(tries) == 1:
            raise RuntimeError("upload 5xx")

    monkeypatch.setattr(dr, "_send_image", flaky_img)
    fake.events("stream", [{"ev": "room", "room": "R1"},
                           _msg(1, "截图", files=[{"name": "s.jpg", "mime": "image/jpeg", "path": str(jpg)}])])
    bot = _FakeBot()
    with pytest.raises(dr.DotsError):
        asyncio.run(dr.relay_once(bot))
    assert [c[1] for c in bot.feishu.cards] == ["截图"]   # 正文只发一次
    assert tries == [True, True]                          # 重试时图片还在，补发成功
    assert not jpg.exists()
    assert dr.get_state("p")["delivered_id"] == "m1"


# ── driver：哪些豆包消息要附上云电脑画面 ─────────────────────

def _driver_ns():
    src = open(os.path.join(os.path.dirname(dr.__file__), "dots_driver.py"), encoding="utf-8").read()
    assert src.rstrip().endswith("_main()")
    ns = {"cdp": lambda *a, **k: {}}
    exec(src.rstrip()[: -len("_main()")], ns)  # 只加载函数，不连浏览器
    return ns


def test_driver_attaches_screen_for_signin_handoff_and_qr_talk():
    wants = _driver_ns()["_wants_screen"]
    assert wants({"content": {"text": "Sign in to continue", "elicitation": {"request_id": "x"}}})
    assert wants({"content": {"text": "点这里打开"}, "message_metadata": {"cloud_browser_handoff": {"tab_id": "10"}}})
    assert wants({"content": {"text": "二维码已刷新，请扫码"}})
    assert not wants({"content": {"text": "今晚露营找到两个候选"}})
    # 10-01：光是汇报「云端浏览器里仍登录着」不该截图（之前因此发出过黑图）
    assert not wants({"content": {"text": "查到了：云端浏览器里「人间漂流」仍然登录着"}})


def test_driver_js_error_is_one_readable_line():
    # 10-01：附件下载失败时把整段 exceptionDetails（带调用栈）转进了用户私聊
    ns = _driver_ns()
    ns["cdp"] = lambda *a, **k: {"exceptionDetails": {
        "exceptionId": 9, "text": "Uncaught (in promise)",
        "exception": {"description": "TypeError: Failed to fetch\n    at <anonymous>:1:172646"},
        "stackTrace": {"callFrames": [{"functionName": "", "scriptId": "590"}]},
    }}
    with pytest.raises(Exception) as ei:
        ns["_ev"]("1")
    assert str(ei.value) == "TypeError: Failed to fetch"


def test_driver_api_retries_network_blips_and_5xx(monkeypatch):
    ns = _driver_ns()
    monkeypatch.setattr(ns["_time"], "sleep", lambda s: None)  # 是真 time 模块，必须用 monkeypatch 还原
    calls = []

    def fake_ev(expr, timeout=60):
        calls.append(1)
        if len(calls) == 1:
            raise ns["_DotsError"]("js", "TypeError: Failed to fetch")
        if len(calls) == 2:
            return {"status": 500, "text": "oops"}
        return {"status": 200, "text": '{"items": []}'}

    ns["_ev"] = fake_ev
    assert ns["_api"]("/backend-api/x") == {"items": []}
    assert len(calls) == 3


# ── driver：找 / 开 Dots 标签页（10-07：标签被关了，转发器报了一晚上 no_tab）────────

_ROOM_PATH = "/dots/01a0f09e-4f2c-70d1-9e34-67a3aeb94018"


def _tab_ns(monkeypatch, tmp_path, pages, *, rooms=("R1",), paths=None):
    """假 Chrome：pages 是现有标签；createTarget 会往里加一个；paths 是新标签依次跳过的地址。"""
    ns = _driver_ns()
    monkeypatch.setattr(ns["_time"], "sleep", lambda s: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    ns["_WAKE_FILE"] = str(tmp_path / "cc-dots-wake-p")
    log = {"created": [], "closed": []}

    def cdp(method, **kw):
        if method == "Target.getTargets":
            return {"targetInfos": list(pages)}
        if method == "Target.createTarget":
            log["created"].append(kw)
            tid = "NEW%d" % len(log["created"])
            pages.append({"type": "page", "targetId": tid, "url": kw["url"], "browserContextId": "C0"})
            return {"targetId": tid}
        if method == "Target.attachToTarget":
            return {"sessionId": "S-" + kw["targetId"]}
        if method == "Target.getTargetInfo":
            t = next(p for p in pages if p["targetId"] == kw["targetId"])
            return {"targetInfo": {"browserContextId": t["browserContextId"]}}
        if method == "Target.closeTarget":
            log["closed"].append(kw["targetId"])
            pages[:] = [p for p in pages if p["targetId"] != kw["targetId"]]
            return {}
        if method == "Target.getBrowserContexts":
            return {"browserContextIds": [], "defaultBrowserContextId": "C0"}
        return {}

    seq = iter(paths or [])

    def fake_ev(expr, timeout=60):
        assert expr == ns["_PAGE_JS"], expr
        path = next(seq, _ROOM_PATH)
        log.setdefault("paths", []).append(path)
        return {"ready": "complete", "path": path, "composer": path == _ROOM_PATH}

    ns["cdp"] = cdp
    ns["_ev"] = fake_ev
    ns["_api"] = lambda path, *a, **k: {"items": [
        {"id": r, "name": "豆包", "members": [{"account_user_id": "calpico-" + r, "name": "豆包"}]} for r in rooms]}
    return ns, log


def test_driver_uses_existing_dots_tab(monkeypatch, tmp_path):
    pages = [{"type": "page", "targetId": "OLD", "url": "https://chatgpt.com" + _ROOM_PATH, "browserContextId": "C0"}]
    ns, log = _tab_ns(monkeypatch, tmp_path, pages)
    ns["_attach"](need_composer=True)
    assert ns["_SID"] == "S-OLD" and log["created"] == []


def test_driver_opens_background_tab_and_waits_for_route_jumps(monkeypatch, tmp_path):
    ns, log = _tab_ns(monkeypatch, tmp_path, [], paths=["/dots", "/dots/home", _ROOM_PATH])
    ns["_remember"](room="R1")
    ns["_attach"](need_composer=True)
    assert log["created"] == [{"url": "https://chatgpt.com/dots", "background": True}]
    assert log["closed"] == [] and ns["_SID"] == "S-NEW1"
    # /dots → /dots/home 时输入框随时会被换掉，要等到落在对话页
    assert log["paths"][:3] == ["/dots", "/dots/home", _ROOM_PATH]


def test_driver_wrong_account_in_default_context_falls_back_to_chrome_profile(monkeypatch, tmp_path):
    import subprocess
    pages = []
    ns, log = _tab_ns(monkeypatch, tmp_path, pages, rooms=("OTHER",))
    ns["_remember"](room="R1", profile_dir="Profile 38")
    runs = []

    def fake_run(args, **kw):
        runs.append(args)
        if args[0] == "/bin/sh":  # lsappinfo 查前台 app
            return subprocess.CompletedProcess(args, 0, stdout='[ NULL ] ASN:0x0-0x1:\n    bundleID="com.larksuite.larkApp"\n')
        if "--profile-directory=Profile 38" in args:
            pages.append({"type": "page", "targetId": "VIA_OPEN", "url": "https://chatgpt.com/dots", "browserContextId": "C38"})
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ns["_attach"](need_composer=True)
    # 默认上下文登录的是别的账号：那个标签要关掉，不能把话发给别人的 Dot
    assert log["closed"] == ["NEW1"]
    assert ns["_SID"] == "S-VIA_OPEN"
    assert ["/usr/bin/open", "-g", "-na", "Google Chrome", "--args",
            "--profile-directory=Profile 38", "https://chatgpt.com/dots"] in runs
    # open 会把 Chrome 拉到前台，开完要把用户原来的前台 app 切回去
    assert runs[-1] == ["/usr/bin/open", "-b", "com.larksuite.larkApp"]


def test_driver_no_tab_and_nothing_works_says_so(monkeypatch, tmp_path):
    ns, log = _tab_ns(monkeypatch, tmp_path, [], rooms=())
    with pytest.raises(Exception) as ei:
        ns["_attach"]()
    assert ei.value.code == "no_tab" and "DOTS_CHROME_PROFILE" in str(ei.value)
    assert log["closed"] == ["NEW1"]


def test_driver_send_retypes_when_composer_was_swapped(monkeypatch):
    ns = _driver_ns()
    monkeypatch.setattr(ns["_time"], "sleep", lambda s: None)
    typed = iter(["", "你好呀"])  # 第一次写进了被换掉的输入框，读回来是空的

    def fake_ev(expr, timeout=60):
        if expr == ns["_PAGE_JS"]:
            return {"ready": "complete", "path": _ROOM_PATH, "composer": True}
        if expr == ns["_COMPOSER_JS"]:
            return "ok"
        if expr == ns["_COMPOSER_TEXT_JS"]:
            return next(typed)
        if "b.click()" in expr:
            return "ok"
        return None

    inserted = []
    ns["_ev"] = fake_ev
    ns["cdp"] = lambda m, **k: inserted.append(k["text"]) if m == "Input.insertText" else {}
    ns["_latest_id"] = lambda room: "a0"
    ns["_messages_after"] = lambda room, after, limit=30: [{"id": "m1", "account_user_id": "user-1"}]
    assert ns["_send"]("R1", {"calpico-R1"}, "你好呀", []) == "m1"
    assert inserted == ["你好呀", "你好呀"]


# ── driver：截图前把豆包的电脑面板打开（10-08 网页改版后它默认不显示了）──────────

def _panel_ns(monkeypatch, answers):
    ns = _driver_ns()
    monkeypatch.setattr(ns["_time"], "sleep", lambda s: None)
    calls = []

    def fake_ev(expr, timeout=60):
        for key in ("_PANEL_LIVE_JS", "_PANEL_PROFILE_JS", "_PANEL_COMPUTER_JS", "_ESCAPE_JS"):
            if expr == ns[key]:
                calls.append(key)
                seq = answers[key]
                return seq.pop(0) if len(seq) > 1 else seq[0]
        raise AssertionError(expr)

    ns["_ev"] = fake_ev
    return ns, calls


def test_driver_panel_already_open_touches_nothing(monkeypatch):
    ns, calls = _panel_ns(monkeypatch, {"_PANEL_LIVE_JS": [True], "_PANEL_PROFILE_JS": ["ok"],
                                        "_PANEL_COMPUTER_JS": ["ok"], "_ESCAPE_JS": [None]})
    assert ns["_ensure_panel"]() is None
    assert calls == ["_PANEL_LIVE_JS"]


def test_driver_opens_panel_via_dot_profile(monkeypatch):
    ns, calls = _panel_ns(monkeypatch, {"_PANEL_LIVE_JS": [False, False, True], "_PANEL_PROFILE_JS": ["ok"],
                                        "_PANEL_COMPUTER_JS": ["loading", "ok"], "_ESCAPE_JS": [None]})
    assert ns["_ensure_panel"]() is None
    assert calls.count("_PANEL_PROFILE_JS") == 1 and calls.count("_PANEL_COMPUTER_JS") == 2
    assert "_ESCAPE_JS" not in calls


def test_driver_panel_entry_missing_closes_profile_dialog(monkeypatch):
    clock = iter(range(0, 1000, 3))
    ns, calls = _panel_ns(monkeypatch, {"_PANEL_LIVE_JS": [False], "_PANEL_PROFILE_JS": ["ok"],
                                        "_PANEL_COMPUTER_JS": ["missing"], "_ESCAPE_JS": [None]})
    monkeypatch.setattr(ns["_time"], "time", lambda: next(clock))
    assert ns["_ensure_panel"]() == "个人资料里没找到电脑入口"
    assert calls[-1] == "_ESCAPE_JS"


def test_driver_panel_selectors_never_match_pause_or_call():
    # 同一个弹窗里有「暂停 豆包」「呼叫」，面板上有「获取控制权」——选择器只认「…的电脑」
    import re
    ns = _driver_ns()
    pat = re.search(r"\.find\(b=>/(.+?)/i\.test\(first\(b\)\)\)", ns["_PANEL_COMPUTER_JS"]).group(1)
    rx = re.compile(pat.replace("\\'", "'"), re.I)
    assert rx.search("豆包的电脑")
    for label in ("暂停 豆包", "活跃", "呼叫", "电脑", "获取控制权", "Slack"):
        assert not rx.search(label), label
