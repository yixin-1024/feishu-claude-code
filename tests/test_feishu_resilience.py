"""发送韧性三件套单测：outbox 兜底 / 额度码不可重试 / CardKit 流式路由。

对应借鉴自上游 fork 的三个改进：
- #1 outbox：发送彻底失败时结果落 logs/outbox-<label>.md，不丢。
- #2 额度码 99991403 不可重试：避免重试白烧额度。
- #3 CardKit 流式卡（LARK_CARD_STREAMING=1）：按 message_id 路由，默认关闭走 PATCH。
"""

import importlib

import lark_oapi as lark
import pytest

import outbox
import feishu_client as fc
from feishu_client import FeishuApiError, FeishuClient


def _make_client() -> FeishuClient:
    lc = lark.Client.builder().app_id("x").app_secret("y").build()
    return FeishuClient(lc, app_id="cli_app_123456", app_secret="y", label="spx")


# ── #1 outbox ────────────────────────────────────────────────

def test_outbox_writes_block(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "_LOG_DIR", str(tmp_path))
    path = outbox.record("spx", "最终结果内容", kind="result",
                         error="99991403 quota", meta={"chat_id": "oc_x"})
    assert path and path.endswith("outbox-spx.md")
    body = (tmp_path / "outbox-spx.md").read_text(encoding="utf-8")
    assert "最终结果内容" in body
    assert "99991403 quota" in body
    assert "chat_id: oc_x" in body


def test_outbox_appends(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "_LOG_DIR", str(tmp_path))
    outbox.record("spx", "第一条")
    outbox.record("spx", "第二条")
    body = (tmp_path / "outbox-spx.md").read_text(encoding="utf-8")
    assert "第一条" in body and "第二条" in body
    assert body.count("---") >= 2


def test_outbox_empty_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "_LOG_DIR", str(tmp_path))
    assert outbox.record("spx", "") is None


def test_outbox_label_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "_LOG_DIR", str(tmp_path))
    path = outbox.record("a/b c", "x")
    assert "outbox-a_b_c.md" in path


def test_save_outbox_uses_label(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox, "_LOG_DIR", str(tmp_path))
    client = _make_client()
    p = client.save_outbox("内容", error="boom")
    assert p.endswith("outbox-spx.md")


# ── #2 额度码不可重试 ─────────────────────────────────────────

def test_non_retryable_codes_default():
    importlib.reload(fc)
    assert 99991403 in fc.NON_RETRYABLE_CODES


def test_non_retryable_codes_env_override(monkeypatch):
    monkeypatch.setenv("LARK_NO_RETRY_CODES", "111,222")
    codes = fc._load_non_retryable_codes()
    assert codes == frozenset({111, 222})


def test_non_retryable_codes_blank_falls_back(monkeypatch):
    monkeypatch.setenv("LARK_NO_RETRY_CODES", "   ")
    assert fc._load_non_retryable_codes() == frozenset({99991403})


async def test_retry_short_circuits_on_quota_code():
    client = _make_client()
    calls = {"n": 0}

    async def _quota_fail():
        calls["n"] += 1
        raise FeishuApiError("发送卡片消息失败", 99991403, "quota exhausted")

    with pytest.raises(FeishuApiError) as ei:
        await client._retry_with_backoff(_quota_fail, max_retries=3, initial_delay=0)
    assert ei.value.code == 99991403
    assert calls["n"] == 1  # 立即放弃，零重试


async def test_retry_still_retries_other_errors():
    client = _make_client()
    calls = {"n": 0}

    async def _transient():
        calls["n"] += 1
        raise FeishuApiError("patch 卡片失败", 230099, "server internal error")

    with pytest.raises(FeishuApiError):
        await client._retry_with_backoff(_transient, max_retries=2, initial_delay=0)
    assert calls["n"] == 3  # 首次 + 2 次重试


async def test_retry_returns_on_success():
    client = _make_client()
    calls = {"n": 0}

    async def _ok():
        calls["n"] += 1
        if calls["n"] < 2:
            raise FeishuApiError("patch 卡片失败", 230099, "transient")
        return "done"

    assert await client._retry_with_backoff(_ok, max_retries=3, initial_delay=0) == "done"
    assert calls["n"] == 2


# ── #3 CardKit 流式路由 ───────────────────────────────────────

def test_streaming_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LARK_CARD_MODE", raising=False)
    monkeypatch.delenv("LARK_CARD_STREAMING", raising=False)
    monkeypatch.delenv("LARK_CARD_SCHEMA", raising=False)
    assert fc._card_mode() == "v2"
    assert fc._streaming_enabled() is False
    assert fc._use_v1_card() is False


def test_streaming_flag_on(monkeypatch):
    monkeypatch.delenv("LARK_CARD_MODE", raising=False)
    monkeypatch.setenv("LARK_CARD_STREAMING", "1")
    assert fc._streaming_enabled() is True
    assert fc._card_mode() == "cardkit"


def test_card_mode_explicit_overrides_legacy(monkeypatch):
    # LARK_CARD_MODE 显式设置时，盖过旧的两个布尔开关
    monkeypatch.setenv("LARK_CARD_STREAMING", "1")
    monkeypatch.setenv("LARK_CARD_SCHEMA", "1.0")
    monkeypatch.setenv("LARK_CARD_MODE", "v2")
    assert fc._card_mode() == "v2"
    assert fc._streaming_enabled() is False
    assert fc._use_v1_card() is False


def test_card_mode_v1_and_cardkit(monkeypatch):
    monkeypatch.delenv("LARK_CARD_STREAMING", raising=False)
    monkeypatch.delenv("LARK_CARD_SCHEMA", raising=False)
    monkeypatch.setenv("LARK_CARD_MODE", "v1")
    assert fc._card_mode() == "v1"
    assert fc._use_v1_card() is True
    monkeypatch.setenv("LARK_CARD_MODE", "cardkit")
    assert fc._card_mode() == "cardkit"
    assert fc._streaming_enabled() is True


def test_card_mode_legacy_schema_fallback(monkeypatch):
    # 未设 LARK_CARD_MODE 时回退旧 LARK_CARD_SCHEMA
    monkeypatch.delenv("LARK_CARD_MODE", raising=False)
    monkeypatch.delenv("LARK_CARD_STREAMING", raising=False)
    monkeypatch.setenv("LARK_CARD_SCHEMA", "1.0")
    assert fc._card_mode() == "v1"
    assert fc._use_v1_card() is True


def test_register_and_next_seq():
    client = _make_client()
    client._register_streaming("om_1", "card_abc", "md_stream")
    assert client._next_seq("om_1") == ("card_abc", "md_stream", 1)
    assert client._next_seq("om_1") == ("card_abc", "md_stream", 2)
    assert client._next_seq("om_unknown") is None


async def test_update_card_routes_to_stream_for_registered(monkeypatch):
    client = _make_client()
    client._register_streaming("om_1", "card_abc", "md_stream")
    routed = {}

    async def _fake_stream(message_id, content):
        routed["msg"] = message_id
        routed["content"] = content

    monkeypatch.setattr(client, "_stream_update_text", _fake_stream)
    # PATCH 路径若被误调用会因无网络/假 client 报错；这里断言没走 PATCH
    await client.update_card("om_1", "增量文本")
    assert routed == {"msg": "om_1", "content": "增量文本"}
    assert client.get_card_text("om_1") == "增量文本"


async def test_finalize_unregisters_and_never_raises():
    client = _make_client()
    client._register_streaming("om_1", "card_abc", "md_stream")
    # 假 client 没有真实 cardkit 后端，asettings 会抛——finalize 必须吞掉且注销登记
    await client.finalize_streaming_card("om_1")
    assert "om_1" not in client._streaming_cards
    # 未登记的 message_id：纯 no-op
    await client.finalize_streaming_card("om_nope")


def test_streaming_max_eviction():
    client = _make_client()
    client._STREAMING_MAX = 8
    for i in range(12):
        client._register_streaming(f"om_{i}", f"card_{i}", "md_stream")
    assert len(client._streaming_cards) <= 8


# ── 收尾确认写：抗飞书 patch 乱序，防计时卡冻结 ─────────────────────

async def test_update_card_final_double_writes_v2(monkeypatch):
    """v2 静态卡收尾必须写两次（首次 + 确认写），确认写稳定压过紧邻的流式 patch。"""
    client = _make_client()
    client._FINAL_CONFIRM_DELAY = 0.6
    calls = []

    async def _fake_update(mid, content):
        calls.append((mid, content))

    slept = []

    async def _fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(client, "update_card", _fake_update)
    monkeypatch.setattr(fc.asyncio, "sleep", _fake_sleep)

    await client.update_card_final("om_1", "✅ 完成态")

    assert calls == [("om_1", "✅ 完成态"), ("om_1", "✅ 完成态")]
    assert slept == [0.6]  # 两次写之间隔了确认延时


async def test_update_card_final_single_write_when_delay_disabled(monkeypatch):
    """LARK_CARD_FINAL_CONFIRM_DELAY<=0 时退回单次写，不加确认写。"""
    client = _make_client()
    client._FINAL_CONFIRM_DELAY = 0
    calls = []

    async def _fake_update(mid, content):
        calls.append((mid, content))

    monkeypatch.setattr(client, "update_card", _fake_update)
    await client.update_card_final("om_1", "final")
    assert calls == [("om_1", "final")]


async def test_update_card_final_no_double_write_for_streaming(monkeypatch):
    """cardkit 流式卡有 sequence 保序，收尾只写一次（避免多推打字机动画）。"""
    client = _make_client()
    client._FINAL_CONFIRM_DELAY = 0.6
    client._register_streaming("om_1", "card_abc", "md_stream")
    calls = []

    async def _fake_update(mid, content):
        calls.append((mid, content))

    slept = []

    async def _fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(client, "update_card", _fake_update)
    monkeypatch.setattr(fc.asyncio, "sleep", _fake_sleep)

    await client.update_card_final("om_1", "final")
    assert calls == [("om_1", "final")]
    assert slept == []  # 流式卡不进确认写分支


async def test_update_card_final_confirm_write_failure_swallowed(monkeypatch):
    """确认写抛异常必须被吞掉——首次已成功，收尾不该因二次写失败而崩。"""
    client = _make_client()
    client._FINAL_CONFIRM_DELAY = 0.6
    n = {"i": 0}

    async def _flaky_update(mid, content):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("boom on confirm write")

    async def _fake_sleep(sec):
        pass

    monkeypatch.setattr(client, "update_card", _flaky_update)
    monkeypatch.setattr(fc.asyncio, "sleep", _fake_sleep)

    await client.update_card_final("om_1", "final")  # 不抛
    assert n["i"] == 2
