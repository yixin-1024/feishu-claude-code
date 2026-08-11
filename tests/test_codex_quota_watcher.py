from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import codex_quota_watcher as cqw


def _patch_state(monkeypatch, holder):
    monkeypatch.setattr(cqw, "_load_state", lambda: dict(holder.get("state") or {}))
    monkeypatch.setattr(
        cqw, "_save_state", lambda s: holder.__setitem__("state", dict(s))
    )


def _patch_rate_limits(monkeypatch, snapshot):
    import commands

    monkeypatch.setattr(commands, "_get_codex_rate_limits", lambda: snapshot)


def test_first_run_is_silent(monkeypatch):
    holder = {"state": {}}
    _patch_state(monkeypatch, holder)
    _patch_rate_limits(
        monkeypatch, {"primary": {"usedPercent": 100, "windowDurationMins": 10080}}
    )
    sent = []
    cqw.poll_once(sent.append)
    assert sent == []
    # 冷启动水位已落地
    assert holder["state"]["primary"]["used"] == 100.0


def test_reset_via_used_percent_drop_notifies(monkeypatch):
    holder = {"state": {"primary": {"used": 100.0, "resets": None}}}
    _patch_state(monkeypatch, holder)
    _patch_rate_limits(
        monkeypatch, {"primary": {"usedPercent": 3, "windowDurationMins": 10080}}
    )
    sent = []
    cqw.poll_once(sent.append)
    assert len(sent) == 1
    assert "已重置" in sent[0]
    assert "7天窗口" in sent[0]


def test_reset_via_resets_at_advance_notifies(monkeypatch):
    holder = {"state": {"primary": {"used": 80.0, "resets": 1_000_000}}}
    _patch_state(monkeypatch, holder)
    _patch_rate_limits(
        monkeypatch,
        {
            "primary": {
                "usedPercent": 40,
                "windowDurationMins": 10080,
                "resetsAt": 1_000_000 + 7 * 86400,
            }
        },
    )
    sent = []
    cqw.poll_once(sent.append)
    assert len(sent) == 1
    assert "已重置" in sent[0]


def test_no_alert_when_usage_climbs(monkeypatch):
    holder = {"state": {"primary": {"used": 40.0, "resets": None}}}
    _patch_state(monkeypatch, holder)
    _patch_rate_limits(
        monkeypatch, {"primary": {"usedPercent": 100, "windowDurationMins": 10080}}
    )
    sent = []
    cqw.poll_once(sent.append)
    assert sent == []


def test_small_drop_below_threshold_is_ignored(monkeypatch):
    # 40% → 30%（回落 10 < 20）且起点 < 50 → 不算重置
    holder = {"state": {"primary": {"used": 40.0, "resets": None}}}
    _patch_state(monkeypatch, holder)
    _patch_rate_limits(
        monkeypatch, {"primary": {"usedPercent": 30, "windowDurationMins": 10080}}
    )
    sent = []
    cqw.poll_once(sent.append)
    assert sent == []


def test_fetch_failure_leaves_state_untouched(monkeypatch):
    holder = {"state": {"primary": {"used": 100.0, "resets": None}}}
    _patch_state(monkeypatch, holder)
    _patch_rate_limits(monkeypatch, {})
    sent = []
    cqw.poll_once(sent.append)
    assert sent == []
    # 拉取失败不改 state（否则下一轮真重置会因缺 prev 被当冷启动漏报）
    assert holder["state"]["primary"]["used"] == 100.0


def test_resets_at_microdrift_not_treated_as_reset(monkeypatch):
    # resets_at 只漂 1 秒、used 不降 → 不是重置
    holder = {"state": {"primary": {"used": 100.0, "resets": 1785078071}}}
    _patch_state(monkeypatch, holder)
    _patch_rate_limits(
        monkeypatch,
        {"primary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 1785078072}},
    )
    sent = []
    cqw.poll_once(sent.append)
    assert sent == []


def test_resets_at_advance_while_low_usage_climbs_is_not_reset(monkeypatch):
    """截图回归：1% → 3% 明明在上涨，不能因 resets_at 后移就误报重置。"""
    holder = {"state": {"primary": {"used": 1.0, "resets": 1_000_000}}}
    _patch_state(monkeypatch, holder)
    _patch_rate_limits(
        monkeypatch,
        {
            "primary": {
                "usedPercent": 3,
                "windowDurationMins": 10080,
                "resetsAt": 1_000_000 + 7 * 86400,
            }
        },
    )
    sent = []
    cqw.poll_once(sent.append)
    assert sent == []


def test_resets_at_advance_while_still_near_full_is_not_reset(monkeypatch):
    holder = {"state": {"primary": {"used": 90.0, "resets": 1_000_000}}}
    _patch_state(monkeypatch, holder)
    _patch_rate_limits(
        monkeypatch,
        {
            "primary": {
                "usedPercent": 85,
                "windowDurationMins": 10080,
                "resetsAt": 1_000_000 + 7 * 86400,
            }
        },
    )
    sent = []
    cqw.poll_once(sent.append)
    assert sent == []


def test_concurrent_pollers_only_notify_once(monkeypatch, tmp_path):
    """滚动部署短暂双进程时，两次并发 poll 只允许一条重置通报。"""
    state_dir = tmp_path / "state"
    monkeypatch.setattr(cqw, "_STATE_DIR", str(state_dir))
    monkeypatch.setattr(cqw, "_STATE_FILE", str(state_dir / "quota.json"))
    monkeypatch.setattr(cqw, "_STATE_LOCK_FILE", str(state_dir / "quota.lock"))
    cqw._save_state({"primary": {"used": 100.0, "resets": 1_000_000}})

    gate = Barrier(2)
    snapshot = {
        "primary": {
            "usedPercent": 3,
            "windowDurationMins": 10080,
            "resetsAt": 1_000_000 + 7 * 86400,
        }
    }
    import commands

    def fetch_together():
        gate.wait(timeout=5)
        return snapshot

    monkeypatch.setattr(commands, "_get_codex_rate_limits", fetch_together)
    sent = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(cqw.poll_once, sent.append) for _ in range(2)]
        for future in futures:
            future.result(timeout=5)

    assert len(sent) == 1
