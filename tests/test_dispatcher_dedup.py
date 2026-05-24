"""dispatcher._is_duplicate_event 单测。

背景：Lark WS 是 at-least-once，断线重连会重投 receive_v1，导致同一条 om_
被 handle_message_async 跑两遍。该函数在 dispatcher 入口拦截重复事件。
"""

import time

import pytest

import dispatcher


@pytest.fixture(autouse=True)
def _reset_seen():
    """每个 case 跑完清空 module-level state，避免互相污染。"""
    dispatcher._seen_messages.clear()
    yield
    dispatcher._seen_messages.clear()


def test_first_seen_returns_false():
    assert dispatcher._is_duplicate_event("om_aaa") is False
    assert "om_aaa" in dispatcher._seen_messages


def test_second_seen_returns_true():
    dispatcher._is_duplicate_event("om_aaa")
    assert dispatcher._is_duplicate_event("om_aaa") is True


def test_different_ids_both_pass():
    assert dispatcher._is_duplicate_event("om_aaa") is False
    assert dispatcher._is_duplicate_event("om_bbb") is False
    # 各自都已记录
    assert "om_aaa" in dispatcher._seen_messages
    assert "om_bbb" in dispatcher._seen_messages


def test_empty_id_never_duplicate():
    """空 message_id 防御：返回 False 且不污染 dict。"""
    assert dispatcher._is_duplicate_event("") is False
    assert dispatcher._is_duplicate_event("") is False
    assert "" not in dispatcher._seen_messages


def test_expired_entry_is_evicted(monkeypatch):
    """超过 TTL 的条目下次调用时会被清掉，同 ID 再来当新消息。"""
    monkeypatch.setattr(dispatcher, "_SEEN_MSG_TTL_SEC", 1)
    assert dispatcher._is_duplicate_event("om_aaa") is False
    # 手动把时间戳改成 2 秒前，触发清理
    dispatcher._seen_messages["om_aaa"] = time.time() - 2
    assert dispatcher._is_duplicate_event("om_aaa") is False
    # 清理后又重新记录
    assert "om_aaa" in dispatcher._seen_messages


def test_unexpired_entry_still_dedupes(monkeypatch):
    """没过期的条目不会被清掉。"""
    monkeypatch.setattr(dispatcher, "_SEEN_MSG_TTL_SEC", 120)
    dispatcher._is_duplicate_event("om_aaa")
    dispatcher._is_duplicate_event("om_bbb")
    # 第二次重投都被拦
    assert dispatcher._is_duplicate_event("om_aaa") is True
    assert dispatcher._is_duplicate_event("om_bbb") is True


def test_cleanup_only_evicts_expired(monkeypatch):
    """清理只动过期的，没过期的留下。"""
    monkeypatch.setattr(dispatcher, "_SEEN_MSG_TTL_SEC", 10)
    now = time.time()
    dispatcher._seen_messages["om_old"] = now - 100   # 早就过期
    dispatcher._seen_messages["om_fresh"] = now - 1   # 还没过期
    # 任意一次调用都会顺带清理
    dispatcher._is_duplicate_event("om_new")
    assert "om_old" not in dispatcher._seen_messages
    assert "om_fresh" in dispatcher._seen_messages
    assert "om_new" in dispatcher._seen_messages
