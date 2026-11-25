"""长话题（超过 200 条）里先发几条、再单独 @ bot：要读到前面那几条，不能回「读不到前面的消息」。

2026-11-25 实例：research bot「工作空间」群的 StudentBench 话题已有 295 条消息。Jihui 先发了
「代码可以考虑公开…」「好的，前面4条都做…」两条（没 @），再单独发一条 @claude。
build_thread_context 只拉话题最早的 200 条，上次处理到的那条（第 292 条）不在里面，
于是「上次之后」的消息一条都认不出来，空正文直接回了「只 @ 了我但没有正文，也读不到前面的消息」。
"""

import json
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feishu_client
import thread_context


def _lm(mid, text="", *, sender="ou_jihui", app=False):
    return SimpleNamespace(
        message_id=mid, msg_type="text", body=SimpleNamespace(content=json.dumps({"text": text})),
        sender=SimpleNamespace(id=sender, sender_type="app" if app else "user"),
        create_time="1764050700000", mentions=None,
    )


class ThreadStub:
    """按真实接口语义模拟 list_thread_messages：tail=False 取最早 limit 条，tail=True 取最新 limit 条。"""

    def __init__(self, msgs):
        self.msgs = msgs
        self._app_id = "cli_self"
        self.calls: list[dict] = []

    async def list_thread_messages(self, thread_id, limit=200, tail=False, stop_at=""):
        self.calls.append({"limit": limit, "tail": tail, "stop_at": stop_at})
        return list(self.msgs[-limit:] if tail else self.msgs[:limit])

    async def batch_resolve_names(self, open_ids):
        return {"ou_jihui": "Jihui"}

    async def download_file(self, message_id, file_key, msg_type="file", file_name=""):
        return f"/tmp/dl/{file_key}"

    def get_card_text(self, message_id):
        return ""


def _long_thread(n_old=292):
    msgs = [_lm(f"om_{i}", f"旧消息 {i}") for i in range(n_old)]
    msgs += [
        _lm("om_q1", "代码可以考虑公开，你觉得怎样更好？"),
        _lm("om_q2", "好的，前面4条都做，你来修改吧"),
        _lm("om_at", ""),  # 单独的 @claude
    ]
    return msgs


async def test_long_thread_bare_mention_sees_messages_since_last_seen():
    fs = ThreadStub(_long_thread())
    ctx, paths, err = await thread_context.build_thread_context(
        fs, "omt_x", "om_291", "om_at",
    )
    assert err is None
    assert "【话题新增 · 2 条（距上次处理后）】" in ctx
    assert "代码可以考虑公开" in ctx and "前面4条都做" in ctx
    assert "旧消息" not in ctx
    assert fs.calls == [{"limit": 200, "tail": True, "stop_at": "om_291"}]


async def test_last_seen_equal_to_current_is_not_treated_as_missing():
    """定时唤醒 / 续跑锚定的就是上次处理到的那条：没有新消息，不能退回去重灌最近 30 条。"""
    fs = ThreadStub(_long_thread())
    ctx, _paths, err = await thread_context.build_thread_context(
        fs, "omt_x", "om_at", "om_at",
    )
    assert err is None and ctx == ""


async def test_last_seen_out_of_window_falls_back_to_recent_tail():
    fs = ThreadStub(_long_thread(n_old=400))
    ctx, _paths, err = await thread_context.build_thread_context(
        fs, "omt_x", "om_5", "om_at",
    )
    assert err is None
    n = thread_context.THREAD_FALLBACK_TAIL
    assert f"【话题最近 · {n} 条" in ctx
    assert "前面4条都做" in ctx
    lines = ctx.splitlines()
    assert len(lines) == n + 1
    assert lines[1].endswith("旧消息 372")  # 400 条里只带最后 30 条（372…399 + 两条新消息）


async def test_first_time_in_long_thread_takes_latest_not_earliest():
    fs = ThreadStub(_long_thread(n_old=400))
    ctx, _paths, err = await thread_context.build_thread_context(
        fs, "omt_x", "", "om_at",
    )
    assert err is None
    assert "话题较长，只取最近 200 条" in ctx
    assert "前面4条都做" in ctx
    lines = ctx.splitlines()
    assert lines[1].endswith("旧消息 203")  # 最新 200 条（含当前这条）去掉当前，从 203 开始


async def test_short_thread_unchanged():
    msgs = [_lm("om_root", "根"), _lm("om_a", "上次处理到这"), _lm("om_b", "新的一条"), _lm("om_at", "")]
    fs = ThreadStub(msgs)
    ctx, _paths, err = await thread_context.build_thread_context(fs, "omt_x", "om_a", "om_at")
    assert "【话题新增 · 1 条（距上次处理后）】" in ctx and "新的一条" in ctx
    ctx, _paths, err = await thread_context.build_thread_context(fs, "omt_x", "", "om_at")
    assert "【话题历史 · 3 条（按时间顺序）】" in ctx


# ── FeishuClient.list_thread_messages(tail=True) 的翻页 ─────────────


class _FakeReq:
    def __init__(self, fields):
        self.fields = fields


class _FakeBuilder:
    def __init__(self):
        self.fields = {}

    def __getattr__(self, name):
        def setter(value):
            self.fields[name] = value
            return self
        return setter

    def build(self):
        return _FakeReq(dict(self.fields))


def _client_with_pages(all_msgs_newest_first, page_size=50):
    """alist 按 page_token 切片返回（只支持 Desc，用于测 tail 模式）。"""
    seen_reqs = []

    async def alist(req):
        seen_reqs.append(req.fields)
        start = int(req.fields.get("page_token") or 0)
        size = req.fields["page_size"]
        items = all_msgs_newest_first[start:start + size]
        nxt = start + size
        has_more = nxt < len(all_msgs_newest_first)
        return SimpleNamespace(
            success=lambda: True, code=0, msg="",
            data=SimpleNamespace(items=items, has_more=has_more,
                                 page_token=str(nxt) if has_more else ""),
        )

    client = feishu_client.FeishuClient.__new__(feishu_client.FeishuClient)
    client.client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
        message=SimpleNamespace(alist=alist))))
    return client, seen_reqs


async def test_feishu_tail_pages_newest_first_stops_at_last_seen(monkeypatch):
    monkeypatch.setattr(feishu_client.ListMessageRequest, "builder", staticmethod(_FakeBuilder))
    newest_first = [_lm(f"om_{i}") for i in range(299, -1, -1)]
    client, reqs = _client_with_pages(newest_first)

    got = await client.list_thread_messages("omt_x", limit=200, tail=True, stop_at="om_260")
    assert [r["sort_type"] for r in reqs] == ["ByCreateTimeDesc"]  # 第一页就翻到了，不再翻
    assert [m.message_id for m in got] == [f"om_{i}" for i in range(250, 300)]  # 升序返回

    reqs.clear()
    got = await client.list_thread_messages("omt_x", limit=200, tail=True, stop_at="om_gone")
    assert len(reqs) == 4 and len(got) == 200
    assert got[0].message_id == "om_100" and got[-1].message_id == "om_299"
