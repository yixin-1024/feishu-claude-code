"""后台会话摘要补齐：文件已清理的会话要标记跳过，不能永远卡在前 5 个死条目上。

线上：spx 1883 个未摘要会话、7 天 0 成功 0 报错——老循环永远取 unsummarized[:5]，
而这 5 个的 jsonl 早被 CLI 清了；同时 _get_api_token 走了不带 -a 的 keychain 读法拿到
过期 token 静默返回空。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runtime
import session_store


class _Store:
    def __init__(self, data):
        self._data = data
        self.saved = 0

    def get_all_unsummarized(self):
        out = []
        for uid, ud in self._data.items():
            summ = ud.get("summaries", {})
            for k, v in ud.items():
                if isinstance(v, dict) and "history" in v:
                    for h in v["history"]:
                        if not summ.get(h["session_id"]):
                            out.append((uid, h["session_id"]))
        return out

    def _save(self):
        self.saved += 1


class _Bot:
    def __init__(self, store):
        self.store = store

        class _P:
            name = "spx"

        self.profile = _P()


def _bots(sids):
    data = {"u1": {"summaries": {}, "oc_1": {"history": [{"session_id": s} for s in sids]}}}
    return {"spx": _Bot(_Store(data))}


def test_missing_files_are_marked_and_live_sessions_summarized(monkeypatch):
    bots = _bots(["dead1", "dead2", "live1"])
    monkeypatch.setattr(session_store, "_find_session_file",
                        lambda sid: f"/tmp/{sid}.jsonl" if sid.startswith("live") else None)
    monkeypatch.setattr(session_store, "_get_api_token", lambda: "tok")
    calls = []
    monkeypatch.setattr(runtime, "generate_summary", lambda sid, token=None: calls.append(sid) or "查余额")
    monkeypatch.setattr(runtime, "_write_custom_title", lambda sid, title: None)

    stats = runtime._summary_pass(bots, sleep_fn=lambda s: None)

    summ = bots["spx"].store._data["u1"]["summaries"]
    assert summ["dead1"] == runtime.SUMMARY_MISSING_MARK
    assert summ["dead2"] == runtime.SUMMARY_MISSING_MARK
    assert summ["live1"] == "查余额"
    assert calls == ["live1"], "死条目不该占用 API 配额，也不该挡住后面活着的会话"
    assert stats["spx"] == {"api": 1, "missing": 2, "pending": 3}
    assert bots["spx"].store.saved == 1
    # 第二轮：全部已处理，不再重复
    assert runtime._summary_pass(bots, sleep_fn=lambda s: None) == {}


def test_no_token_only_marks_missing_without_hitting_api(monkeypatch):
    bots = _bots(["dead1", "live1"])
    monkeypatch.setattr(session_store, "_find_session_file",
                        lambda sid: f"/tmp/{sid}.jsonl" if sid.startswith("live") else None)
    monkeypatch.setattr(session_store, "_get_api_token", lambda: None)
    monkeypatch.setattr(runtime, "generate_summary",
                        lambda sid, token=None: (_ for _ in ()).throw(AssertionError("不该调 API")))
    stats = runtime._summary_pass(bots, sleep_fn=lambda s: None)
    summ = bots["spx"].store._data["u1"]["summaries"]
    assert summ["dead1"] == runtime.SUMMARY_MISSING_MARK and "live1" not in summ
    assert stats["spx"]["api"] == 0 and stats["spx"]["missing"] == 1


def test_get_api_token_reads_keychain_via_account_switcher(monkeypatch):
    """keychain 回落必须走带 -a 的 _read_keychain_blob（否则命中死条目拿到过期 token）。"""
    import json
    import account_switcher

    monkeypatch.setattr(session_store.os.path, "isfile", lambda p: False)  # 没有 credentials 文件
    far = int(session_store.datetime.now().timestamp() * 1000) + 3_600_000
    blob = json.dumps({"claudeAiOauth": {"accessToken": "sk-live", "expiresAt": far}})
    monkeypatch.setattr(account_switcher, "_read_keychain_blob", lambda: blob)
    monkeypatch.setattr(account_switcher, "ensure_keychain_intact", lambda: ("ok", None))
    assert session_store._get_api_token() == "sk-live"

    monkeypatch.setattr(account_switcher, "_read_keychain_blob", lambda: None)
    assert session_store._get_api_token() is None


def test_rotate_log_copytruncate(tmp_path):
    p = tmp_path / "bot.log"
    p.write_bytes(b"x" * 2048)
    assert runtime.rotate_log_if_needed(str(p), max_bytes=1024, keep=2) is True
    assert p.stat().st_size == 0
    assert (tmp_path / "bot.log.1").stat().st_size == 2048
    p.write_bytes(b"y" * 4096)
    assert runtime.rotate_log_if_needed(str(p), max_bytes=1024, keep=2) is True
    assert (tmp_path / "bot.log.1").read_bytes() == b"y" * 4096
    assert (tmp_path / "bot.log.2").read_bytes() == b"x" * 2048
    # 未超阈值不动
    assert runtime.rotate_log_if_needed(str(p), max_bytes=1024, keep=2) is False
