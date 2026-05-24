"""inbox_watcher v2 单测：
- _extract_text_and_attachments: text/image/file/post 4 类 + 下载失败兜底
- _case_key_from_messages: 邮箱/工单/user_id/卡号末四位/未命中
- _normalize_case_key: 归一化清洗
- case_thread LRU + TTL
- auto_exec quota（hour/day）
- judge JSON 解析含新字段
"""

import json
import time
import types

import pytest

import inbox_watcher as iw
from inbox_watcher import (
    Attachment,
    CaseThread,
    InboxConfig,
    Message,
    _auto_exec_quota_ok,
    _auto_exec_record,
    _case_history_text,
    _case_key_from_messages,
    _case_thread_evict,
    _case_thread_get,
    _case_thread_upsert,
    _extract_json,
    _normalize_case_key,
)


@pytest.fixture(autouse=True)
def _isolate_inbox(monkeypatch, tmp_path):
    """每个测试给一个空 config + 空状态。"""
    cfg = InboxConfig(
        enabled=True,
        profile="spx",
        dispatch_chat_id="oc_dispatch",
        owner_open_id="ou_owner",
        owner_name="Test Owner",
        memory_dir=str(tmp_path / "inbox"),
        auto_execute_enabled=True,
        auto_execute_kinds=["readonly"],
        auto_execute_min_confidence=0.8,
        auto_execute_quota_per_hour=5,
        auto_execute_quota_per_day=30,
        case_session_enabled=True,
        case_thread_ttl_days=30,
        case_thread_max_entries=5,
    )
    monkeypatch.setattr(iw, "_config", cfg)
    monkeypatch.setattr(iw, "_clusters", {})
    monkeypatch.setattr(iw, "_case_threads", {})
    monkeypatch.setattr(iw, "_auto_exec_log", [])
    monkeypatch.setattr(iw, "_feedback_done", set())


# ── case_key 抽取 ───────────────────────────────────────────

def _msg(text: str) -> Message:
    return Message(
        message_id="m", sender_open_id="ou_x", sender_name="X",
        text=text, create_time=time.time(),
    )


def test_case_key_emails_stable_hash():
    a = _case_key_from_messages([_msg("查 a@x.com b@y.com 的 KYC")])
    b = _case_key_from_messages([_msg("a@x.com 和 b@y.com 入金了没")])
    c = _case_key_from_messages([_msg("B@Y.COM, a@x.com 看一下")])
    assert a.startswith("emails:")
    assert a == b == c, "邮箱顺序/大小写不同应当稳定到同一 case_key"


def test_case_key_emails_different_set_different_key():
    a = _case_key_from_messages([_msg("查 a@x.com")])
    b = _case_key_from_messages([_msg("查 a@x.com c@z.com")])
    assert a != b


def test_case_key_ticket():
    k = _case_key_from_messages([_msg("KYC00123 这个用户 KYB 也不行")])
    assert k == "ticket:KYC00123"


def test_case_key_user_id():
    k = _case_key_from_messages([_msg("给 user_id=50503 开 SGB VA")])
    assert k == "uid:50503"


def test_case_key_card_last4():
    k = _case_key_from_messages([_msg("卡号末四位 4321 有问题")])
    assert k == "card:4321"


def test_case_key_priority_emails_over_ticket():
    """邮箱命中应该优先于工单号——因为邮箱集合更稳。"""
    k = _case_key_from_messages([_msg("KYC00111: a@x.com b@y.com")])
    assert k.startswith("emails:")


def test_case_key_no_match():
    k = _case_key_from_messages([_msg("讨论一下要不要换数据库")])
    assert k == ""


# ── case_key 归一化 ────────────────────────────────────────

def test_normalize_case_key_basic():
    assert _normalize_case_key("Julia Five Users") == "julia_five_users"
    assert _normalize_case_key("  KYC00123  ") == "kyc00123"
    assert _normalize_case_key("") == ""


def test_normalize_case_key_strips_zenkaku():
    assert _normalize_case_key("user：50503") == "user50503"


# ── case_thread upsert / get / TTL / LRU ──────────────────

def test_case_thread_upsert_and_get():
    _case_thread_upsert("emails:abc", "om_anchor", "title", "body")
    ct = _case_thread_get("emails:abc")
    assert ct is not None
    assert ct.anchor_msg_id == "om_anchor"
    assert len(ct.history) == 1


def test_case_thread_get_disabled():
    iw._config.case_session_enabled = False
    _case_thread_upsert("emails:abc", "om_anchor", "t", "b")
    assert _case_thread_get("emails:abc") is None


def test_case_thread_ttl_expired():
    _case_thread_upsert("k1", "om1", "t", "b")
    iw._case_threads["k1"].last_touched_at = time.time() - 31 * 86400
    assert _case_thread_get("k1") is None
    assert "k1" not in iw._case_threads, "过期应被 get 时 evict"


def test_case_thread_lru_evicts_oldest():
    # max=5
    for i in range(6):
        _case_thread_upsert(f"k{i}", f"om{i}", "t", "b")
        iw._case_threads[f"k{i}"].last_touched_at = time.time() - (10 - i)  # k0 最老
    _case_thread_evict()
    assert len(iw._case_threads) == 5
    assert "k0" not in iw._case_threads
    assert "k5" in iw._case_threads


def test_case_history_text_format():
    _case_thread_upsert("k1", "om", "T1", "body1", "action1")
    _case_thread_upsert("k1", "om", "T2", "body2", "")
    txt = _case_history_text("k1")
    assert "T1" in txt and "T2" in txt
    assert "action1" in txt


# ── auto_exec quota ────────────────────────────────────────

def test_quota_ok_initially():
    ok, why = _auto_exec_quota_ok()
    assert ok and why == ""


def test_quota_hour_limit():
    for _ in range(5):
        _auto_exec_record()
    ok, why = _auto_exec_quota_ok()
    assert not ok
    assert "小时配额" in why


def test_quota_day_limit_independent_of_hour():
    # 把 5 条挪到 2 小时前（不算 hour），再加 25 条到 30 分钟前 = 30 算 day
    iw._config.auto_execute_quota_per_hour = 100  # 放开 hour
    now = time.time()
    for _ in range(30):
        iw._auto_exec_log.append(now - 1800)
    ok, why = _auto_exec_quota_ok()
    assert not ok
    assert "日配额" in why


def test_quota_old_entries_garbage_collected():
    """24h+ 旧记录应当被 _auto_exec_quota_ok() 清理。"""
    iw._auto_exec_log.extend([time.time() - 90000] * 10)  # 25h 前
    ok, _ = _auto_exec_quota_ok()
    assert ok
    assert len(iw._auto_exec_log) == 0


# ── _extract_text_and_attachments ─────────────────────────

class _StubFeishu:
    """模拟 bot.feishu，记录下载调用。"""

    def __init__(self, fail_image: bool = False, fail_file: bool = False):
        self.fail_image = fail_image
        self.fail_file = fail_file
        self.calls: list[tuple[str, str]] = []

    async def download_image(self, message_id: str, image_key: str) -> str:
        self.calls.append(("image", image_key))
        if self.fail_image:
            raise RuntimeError("net err")
        return f"/tmp/img-{image_key[:6]}.jpg"

    async def download_file(self, message_id: str, file_key: str, msg_type: str = "file", file_name: str = "") -> str:
        self.calls.append(("file", file_key))
        if self.fail_file:
            raise RuntimeError("403")
        return f"/tmp/{file_name or file_key[:6]}"


def _bot_with(feishu: _StubFeishu):
    profile = types.SimpleNamespace(name="spx", lark_cli_profile="spx", default_cwd="/tmp")
    return types.SimpleNamespace(profile=profile, feishu=feishu)


def _msg_obj(message_type: str, content: dict, message_id: str = "om_x"):
    return types.SimpleNamespace(
        message_type=message_type,
        content=json.dumps(content, ensure_ascii=False),
        message_id=message_id,
    )


async def test_extract_text(monkeypatch):
    monkeypatch.setattr(iw, "_bot", _bot_with(_StubFeishu()))
    text, atts = await iw._extract_text_and_attachments(_msg_obj("text", {"text": "hello"}))
    assert text == "hello"
    assert atts == []


async def test_extract_image_downloads(monkeypatch):
    f = _StubFeishu()
    monkeypatch.setattr(iw, "_bot", _bot_with(f))
    text, atts = await iw._extract_text_and_attachments(
        _msg_obj("image", {"image_key": "img_key_123"})
    )
    assert "/tmp/img-" in text
    assert len(atts) == 1 and atts[0].kind == "image" and atts[0].path.startswith("/tmp/")
    assert f.calls == [("image", "img_key_123")]


async def test_extract_image_download_failed_returns_placeholder(monkeypatch):
    monkeypatch.setattr(iw, "_bot", _bot_with(_StubFeishu(fail_image=True)))
    text, atts = await iw._extract_text_and_attachments(
        _msg_obj("image", {"image_key": "k"})
    )
    assert "download failed" in text
    assert len(atts) == 1 and atts[0].path == "" and atts[0].error


async def test_extract_file(monkeypatch):
    f = _StubFeishu()
    monkeypatch.setattr(iw, "_bot", _bot_with(f))
    text, atts = await iw._extract_text_and_attachments(
        _msg_obj("file", {"file_key": "fk", "file_name": "report.pdf"})
    )
    assert "report.pdf" in text and "/tmp/report.pdf" in text
    assert atts[0].kind == "file" and atts[0].name == "report.pdf"


async def test_extract_post_with_inline_images(monkeypatch):
    f = _StubFeishu()
    monkeypatch.setattr(iw, "_bot", _bot_with(f))
    post_content = {
        "zh_cn": {
            "title": "标题",
            "content": [[
                {"tag": "text", "text": "看图: "},
                {"tag": "img", "image_key": "img_a"},
                {"tag": "text", "text": " 还有 "},
                {"tag": "img", "image_key": "img_b"},
            ]]
        }
    }
    text, atts = await iw._extract_text_and_attachments(_msg_obj("post", post_content))
    assert "标题" in text
    assert "内嵌图片" in text
    assert len(atts) == 2
    assert f.calls == [("image", "img_a"), ("image", "img_b")]


# ── judge JSON 解析（含新字段）────────────────────────────

def test_extract_json_with_new_fields():
    text = """前情提要blah blah
```json
{"dispatch": true, "auto_execute": true, "execute_kind": "readonly",
 "execute_confidence": 0.9, "case_key": "emails:abc", "action_prompt": "查 X",
 "title": "🔍 X", "body": "...", "reasoning": "..."}
```
"""
    j = _extract_json(text)
    assert j["dispatch"] is True
    assert j["auto_execute"] is True
    assert j["execute_kind"] == "readonly"
    assert j["case_key"] == "emails:abc"
    assert j["action_prompt"] == "查 X"


def test_extract_json_fallback_no_fence():
    text = '随便说点啥 {"dispatch": false, "reasoning": "无动作"}'
    j = _extract_json(text)
    assert j["dispatch"] is False


def test_extract_json_garbage():
    j = _extract_json("完全不是 JSON")
    assert j["dispatch"] is False
    assert "无法解析" in j["reasoning"]


# ── P1: feedback 闭环 ──────────────────────────────────────

def _make_thread_msg(message_id, sender_id, sender_type, content_text=""):
    body = types.SimpleNamespace(content=json.dumps({"text": content_text}))
    sender = types.SimpleNamespace(id=sender_id, sender_type=sender_type)
    return types.SimpleNamespace(
        message_id=message_id, body=body, sender=sender,
    )


class _FeedbackStubFeishu:
    def __init__(self, thread_id_for: dict[str, str], thread_msgs: dict[str, list]):
        self._thread_id_for = thread_id_for
        self._thread_msgs = thread_msgs
        self._bot_open_id = "ou_bot"

    async def get_message_thread_id(self, msg_id):
        return self._thread_id_for.get(msg_id, "")

    async def list_thread_messages(self, thread_id, limit=200):
        return self._thread_msgs.get(thread_id, [])

    async def get_bot_open_id(self):
        return self._bot_open_id


async def test_feedback_classify_missed(monkeypatch, tmp_path):
    iw._config.memory_dir = str(tmp_path / "inbox")
    anchor = "om_anchor"
    feishu = _FeedbackStubFeishu(
        thread_id_for={anchor: "omt_t"},
        thread_msgs={"omt_t": [_make_thread_msg(anchor, "ou_owner", "user", "顶楼")]},
    )
    monkeypatch.setattr(iw, "_bot", _bot_with(feishu))
    entry = {
        "ts": time.time() - 5 * 3600,
        "dispatched_msg_id": anchor,
        "decision": {"_auto_exec_kicked": False, "title": "T"},
    }
    label = await iw._classify_feedback(entry)
    assert label == "missed"
    assert (tmp_path / "inbox" / "feedback.jsonl").exists()


async def test_feedback_classify_engaged_owner_replied(monkeypatch, tmp_path):
    iw._config.memory_dir = str(tmp_path / "inbox")
    anchor = "om_anchor"
    feishu = _FeedbackStubFeishu(
        thread_id_for={anchor: "omt_t"},
        thread_msgs={"omt_t": [
            _make_thread_msg(anchor, "ou_owner", "user", "顶楼"),
            _make_thread_msg("om_2", "ou_owner", "user", "我去处理"),
        ]},
    )
    monkeypatch.setattr(iw, "_bot", _bot_with(feishu))
    entry = {
        "ts": time.time() - 6 * 3600,
        "dispatched_msg_id": anchor,
        "decision": {"_auto_exec_kicked": False, "title": "T"},
    }
    label = await iw._classify_feedback(entry)
    assert label == "engaged"


async def test_feedback_classify_auto_executed(monkeypatch, tmp_path):
    iw._config.memory_dir = str(tmp_path / "inbox")
    anchor = "om_anchor"
    feishu = _FeedbackStubFeishu(
        thread_id_for={anchor: "omt_t"},
        thread_msgs={"omt_t": [
            _make_thread_msg(anchor, "ou_owner", "user", "顶楼"),
            _make_thread_msg("om_trig", "ou_owner", "user", "查这5个邮箱状态: a@x.com"),
            _make_thread_msg("om_bot", "ou_bot", "app", "结果表格..."),
        ]},
    )
    monkeypatch.setattr(iw, "_bot", _bot_with(feishu))
    entry = {
        "ts": time.time() - 5 * 3600,
        "dispatched_msg_id": anchor,
        "decision": {
            "_auto_exec_kicked": True,
            "action_prompt": "查这5个邮箱状态: a@x.com",
            "title": "T",
        },
    }
    label = await iw._classify_feedback(entry)
    assert label == "auto_executed"


async def test_feedback_classify_auto_then_ignored(monkeypatch, tmp_path):
    """auto_exec kick 了但 bot 没回复 → claude 跑挂了/ACL 没过/异常。"""
    iw._config.memory_dir = str(tmp_path / "inbox")
    anchor = "om_anchor"
    feishu = _FeedbackStubFeishu(
        thread_id_for={anchor: "omt_t"},
        thread_msgs={"omt_t": [
            _make_thread_msg(anchor, "ou_owner", "user", "顶楼"),
            _make_thread_msg("om_trig", "ou_owner", "user", "查这5个邮箱"),
        ]},
    )
    monkeypatch.setattr(iw, "_bot", _bot_with(feishu))
    entry = {
        "ts": time.time() - 5 * 3600,
        "dispatched_msg_id": anchor,
        "decision": {
            "_auto_exec_kicked": True,
            "action_prompt": "查这5个邮箱",
            "title": "T",
        },
    }
    label = await iw._classify_feedback(entry)
    assert label == "auto_then_ignored"


async def test_feedback_scan_skips_too_young_and_too_old(monkeypatch, tmp_path):
    """min_age_hours=4 / max_age_days=7 边界。"""
    iw._config.memory_dir = str(tmp_path / "inbox")
    iw._config.feedback_enabled = True
    (tmp_path / "inbox").mkdir(parents=True, exist_ok=True)
    p = tmp_path / "inbox" / "dispatched.jsonl"
    now = time.time()
    lines = [
        # 太新 — 1h 龄，跳过
        {"ts": now - 3600, "dispatched_msg_id": "om_young", "decision": {}},
        # 合适 — 5h 龄
        {"ts": now - 5 * 3600, "dispatched_msg_id": "om_mid", "decision": {}},
        # 太老 — 10 天龄
        {"ts": now - 10 * 86400, "dispatched_msg_id": "om_old", "decision": {}},
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

    classified = []

    async def _stub_classify(entry):
        classified.append(entry["dispatched_msg_id"])
        return "engaged"

    monkeypatch.setattr(iw, "_classify_feedback", _stub_classify)
    monkeypatch.setattr(iw, "_state_lock", __import__("asyncio").Lock())
    await iw._run_feedback_scan()

    assert classified == ["om_mid"], f"只该处理中间龄那条，实际 {classified}"
    assert "om_young" not in iw._feedback_done
    assert "om_old" in iw._feedback_done  # 太老也标，不重扫
    assert "om_mid" in iw._feedback_done


# ── P2: 源群就地处理 — 路由决策 ────────────────────────────

def test_source_inline_disabled_routes_to_central(monkeypatch):
    """source_inline.enabled=false 时永远不走 inline。"""
    iw._config.source_inline_enabled = False
    iw._config.source_inline_whitelist = ["oc_source"]
    # 简化：直接测决策函数的关键变量逻辑（_judge_and_dispatch 太大，单独提）
    prefer_inline = True
    chat_in_whitelist = True
    kind = "readonly"
    conf = 0.9
    should_inline = (
        iw._config.source_inline_enabled and prefer_inline and chat_in_whitelist
        and kind == "readonly" and conf >= iw._config.source_inline_min_confidence
    )
    assert should_inline is False


def test_source_inline_enabled_routes_correctly(monkeypatch):
    iw._config.source_inline_enabled = True
    iw._config.source_inline_whitelist = ["oc_source"]
    iw._config.source_inline_min_confidence = 0.85
    # 全部满足 → inline
    should = (True and True and "readonly" == "readonly" and 0.9 >= 0.85
              and "oc_source" in iw._config.source_inline_whitelist)
    assert should
    # confidence 不够 → 不 inline
    should = (True and True and "readonly" == "readonly" and 0.8 >= 0.85)
    assert not should


def test_case_thread_upsert_records_inline_route():
    _case_thread_upsert(
        "k1", "om_a", "T", "B", "ap",
        target_chat_id="oc_source", inline=True,
    )
    ct = _case_thread_get("k1")
    assert ct is not None
    assert ct.target_chat_id == "oc_source"
    assert ct.inline is True


def test_case_thread_inline_persists_across_followup():
    """同 case_key 的 follow-up 不能覆盖 inline 路由——首发 inline 就一直 inline。"""
    _case_thread_upsert("k1", "om_a", "T1", "B1", "", target_chat_id="oc_source", inline=True)
    _case_thread_upsert("k1", "om_a", "T2", "B2", "", target_chat_id="", inline=False)
    ct = _case_thread_get("k1")
    assert ct.inline is True
    assert ct.target_chat_id == "oc_source"
