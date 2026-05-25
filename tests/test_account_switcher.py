"""account_switcher 单测：打分 / 硬筛 / 决策 / 冷却 / 切换执行。

关键路径全部 mock 掉外部依赖（HTTP probe、keychain、claude-switch 子进程），
跑纯函数 + 流程编排。
"""

import json
import os
import sys
import time
import tempfile
from unittest.mock import patch

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import account_switcher as accs  # noqa: E402
from account_switcher import (  # noqa: E402
    Account,
    AccountSwitcher,
    decide,
    evaluate,
)


# ────────────────── helpers ──────────────────

def mk(name, *, u5=0.1, u7=0.1, r5_in=4 * 3600, r7_in=6 * 24 * 3600,
       s5="allowed", s7="allowed", err=None):
    """造一个已"探测过"的 Account。r5_in / r7_in 单位 = 秒（相对于 now）。"""
    now = time.time()
    return Account(
        name=name,
        access_token="tok-" + name,
        u5h=u5, u7d=u7,
        r5h=int(now + r5_in) if r5_in is not None else None,
        r7d=int(now + r7_in) if r7_in is not None else None,
        s5h=s5, s7d=s7,
        probe_error=err,
        probed_at=now,
    )


# ────────────────── evaluate (打分 + 硬筛) ──────────────────

def test_evaluate_normal_scoring():
    a = mk("foo", u5=0.2, u7=0.3)
    evaluate(a, current_name="other")
    assert a.usable
    # h7 = 0.7, h5 = 0.8, no bonus → 0.65*0.7 + 0.30*0.8 = 0.455 + 0.24 = 0.695
    assert 0.69 < a.score < 0.70
    assert a.is_current is False


def test_evaluate_current_gets_bonus():
    a = mk("foo", u5=0.2, u7=0.3)
    evaluate(a, current_name="foo")
    assert a.is_current
    # 比上面多 0.05
    assert 0.74 < a.score < 0.75


def test_evaluate_5h_reset_bonus_treats_h5_as_full():
    # 5h 87% 但 10 分钟后 reset → h5 视为 1.0
    a = mk("foo", u5=0.87, u7=0.3, r5_in=10 * 60)
    evaluate(a, current_name=None)
    assert a.usable
    # score = 0.65*0.7 + 0.30*1.0 = 0.755
    assert 0.75 < a.score < 0.76
    assert any("bonus" in r for r in a.reasons)


def test_evaluate_hard_limit_7d():
    a = mk("foo", u5=0.1, u7=0.99)
    evaluate(a, current_name=None)
    assert not a.usable
    assert a.score == 0.0
    assert any("7d" in r for r in a.reasons)


def test_evaluate_hard_limit_5h_when_reset_far():
    a = mk("foo", u5=0.99, u7=0.1, r5_in=2 * 3600)  # 2h 后 reset，仍远
    evaluate(a, current_name=None)
    assert not a.usable
    assert any("5h" in r for r in a.reasons)


def test_evaluate_5h_full_but_imminent_reset_still_usable():
    # 5h 99%，但只剩 2 分钟 reset → 不淘汰
    a = mk("foo", u5=0.99, u7=0.1, r5_in=2 * 60)
    evaluate(a, current_name=None)
    assert a.usable


def test_evaluate_blocked_status_disqualifies():
    a = mk("foo", u5=0.5, u7=0.5, s5="blocked")
    evaluate(a, current_name=None)
    assert not a.usable


def test_evaluate_probe_error_unusable():
    a = mk("foo", err="auth 401")
    a.u5h = None  # 探测失败时不会填
    a.u7d = None
    evaluate(a, current_name=None)
    assert not a.usable
    assert any("auth" in r for r in a.reasons)


# ────────────────── decide ──────────────────

def test_decide_keeps_current_when_no_clear_winner():
    accs_ = {
        "cur": mk("cur", u5=0.4, u7=0.3),
        "alt": mk("alt", u5=0.3, u7=0.3),
    }
    # 当前 5h=0.4 < 0.7 阈值 → 即使候选稍好也不切
    assert decide(accs_, current_name="cur") is None


def test_decide_switches_when_current_unusable():
    accs_ = {
        "cur": mk("cur", u5=0.1, u7=0.99),  # 7d 爆了
        "alt": mk("alt", u5=0.2, u7=0.2),
    }
    assert decide(accs_, current_name="cur") == "alt"


def test_decide_switches_when_candidate_clearly_better_and_5h_tight():
    accs_ = {
        "cur": mk("cur", u5=0.85, u7=0.6),  # 5h 紧 + 7d 半满
        "alt": mk("alt", u5=0.0, u7=0.1),
    }
    out = decide(accs_, current_name="cur")
    assert out == "alt"


def test_decide_no_switch_when_5h_not_tight_even_if_candidate_better():
    # 候选明显更好，但当前 5h<0.7 → 不抖动
    accs_ = {
        "cur": mk("cur", u5=0.5, u7=0.4),
        "alt": mk("alt", u5=0.0, u7=0.0),
    }
    assert decide(accs_, current_name="cur") is None


def test_decide_returns_none_when_no_candidates():
    accs_ = {"cur": mk("cur", u5=0.1, u7=0.1)}
    assert decide(accs_, current_name="cur") is None


def test_decide_picks_best_score_when_multiple_candidates():
    accs_ = {
        "cur": mk("cur", u5=0.99, u7=0.99),  # unusable
        "mid": mk("mid", u5=0.5, u7=0.5),
        "best": mk("best", u5=0.1, u7=0.1),
    }
    assert decide(accs_, current_name="cur") == "best"


def test_decide_user_scenario_both_idle_prefers_more_7d_headroom():
    # 用户语境："5小时和7天都空闲很多 我肯定用这个七天的"
    accs_ = {
        "a": mk("a", u5=0.1, u7=0.6),  # 7d 用得多
        "b": mk("b", u5=0.1, u7=0.1),  # 7d 还很多
    }
    # 当前不存在 → 必须切到 best
    chosen = decide(accs_, current_name=None)
    assert chosen == "b"  # 7d headroom 大的胜出


def test_decide_user_scenario_5h_about_to_reset_with_headroom_preferred():
    # 用户语境："5小时快到期 但是还剩很多 肯定优先这个"
    # 解读：5h 即将 reset 的账户即使 5h 已用了一些也优先（因为很快补满），
    # 实际比的是 7d。这里造一个 5h 已用 60% 但 20 分钟后 reset 的账户 a，
    # 和一个 5h 全空但 7d 略多的账户 b，应该是 a 胜（h5 被视为满 + 7d 更宽）。
    accs_ = {
        "a": mk("a", u5=0.6, u7=0.2, r5_in=20 * 60),
        "b": mk("b", u5=0.05, u7=0.35, r5_in=4 * 3600),
    }
    chosen = decide(accs_, current_name=None)
    assert chosen == "a"


# ────────────────── AccountSwitcher.maybe_switch 流程 ──────────────────

@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    """把 state 文件指向临时目录，避免污染真实 ~/.feishu-claude。"""
    sf = tmp_path / "state.json"
    monkeypatch.setattr(accs, "STATE_FILE", str(sf))
    monkeypatch.setattr(accs, "STATE_DIR", str(tmp_path))
    yield sf


def test_maybe_switch_skips_when_disabled(isolated_state):
    sw = AccountSwitcher(enabled=False)
    with patch.object(accs, "probe_all") as p:
        assert sw.maybe_switch() is None
        p.assert_not_called()


def test_maybe_switch_skips_when_cooldown_active(isolated_state):
    # 写一个 5 分钟前的切换记录，冷却 1800s
    isolated_state.write_text(json.dumps({"last_switch_at": time.time() - 300}))
    sw = AccountSwitcher(enabled=True, cooldown_sec=1800)
    with patch.object(accs, "probe_all") as p:
        assert sw.maybe_switch() is None
        p.assert_not_called()


def test_maybe_switch_skips_when_active_children(isolated_state):
    sw = AccountSwitcher(
        enabled=True,
        has_active_children_fn=lambda: True,
    )
    with patch.object(accs, "probe_all") as p:
        assert sw.maybe_switch() is None
        p.assert_not_called()


def test_maybe_switch_no_action_when_current_healthy(isolated_state):
    sent = []
    sw = AccountSwitcher(send_fn=lambda t: sent.append(t), enabled=True)
    fake_probes = {
        "cur": mk("cur", u5=0.2, u7=0.2),
        "alt": mk("alt", u5=0.1, u7=0.1),
    }
    with patch.object(accs, "probe_all", return_value=fake_probes), \
         patch.object(accs, "current_account_name", return_value="cur"), \
         patch.object(accs, "_run_claude_switch_use") as sw_call:
        assert sw.maybe_switch() is None
        sw_call.assert_not_called()
    assert sent == []


def test_maybe_switch_executes_and_notifies(isolated_state):
    sent = []
    sw = AccountSwitcher(send_fn=lambda t: sent.append(t), enabled=True)
    fake_probes = {
        "cur": mk("cur", u5=0.1, u7=0.99),  # 7d 爆
        "alt": mk("alt", u5=0.1, u7=0.1),
    }
    with patch.object(accs, "probe_all", return_value=fake_probes), \
         patch.object(accs, "current_account_name", return_value="cur"), \
         patch.object(accs, "_run_claude_switch_use", return_value=(True, "ok")) as sw_call:
        out = sw.maybe_switch()
    assert out == "alt"
    sw_call.assert_called_once_with("alt")
    assert any("cur" in t and "alt" in t for t in sent)
    # 冷却应该已经被写进 state
    state = json.loads(isolated_state.read_text())
    assert "last_switch_at" in state
    assert state["last_switch_to"] == "alt"


def test_maybe_switch_handles_claude_switch_failure(isolated_state):
    sent = []
    sw = AccountSwitcher(send_fn=lambda t: sent.append(t), enabled=True)
    fake_probes = {
        "cur": mk("cur", u5=0.1, u7=0.99),
        "alt": mk("alt", u5=0.1, u7=0.1),
    }
    with patch.object(accs, "probe_all", return_value=fake_probes), \
         patch.object(accs, "current_account_name", return_value="cur"), \
         patch.object(accs, "_run_claude_switch_use", return_value=(False, "boom")):
        out = sw.maybe_switch()
    assert out is None
    assert any("失败" in t for t in sent)
    # 失败不写冷却（下一轮还能再试）
    assert not isolated_state.exists() or "last_switch_at" not in (
        json.loads(isolated_state.read_text()) if isolated_state.exists() else {}
    )


def test_maybe_switch_concurrent_only_runs_once(isolated_state):
    """两个线程同时调 maybe_switch，第二个应被 lock 立刻退出。"""
    import threading

    sw = AccountSwitcher(send_fn=lambda t: None, enabled=True)
    in_probe = threading.Event()
    release_probe = threading.Event()
    call_count = {"n": 0}

    def slow_probe():
        call_count["n"] += 1
        in_probe.set()
        release_probe.wait(timeout=2)
        return {"cur": mk("cur", u5=0.1, u7=0.1)}

    results = []
    with patch.object(accs, "probe_all", side_effect=slow_probe), \
         patch.object(accs, "current_account_name", return_value="cur"):
        def runner():
            results.append(sw.maybe_switch())

        t1 = threading.Thread(target=runner)
        t1.start()
        in_probe.wait(timeout=2)
        t2 = threading.Thread(target=runner)
        t2.start()
        t2.join(timeout=2)
        release_probe.set()
        t1.join(timeout=2)

    assert call_count["n"] == 1  # 第二个 caller 没进探测
    assert None in results


# ────────────────── render_matrix 烟雾测 ──────────────────

def test_render_matrix_with_accounts():
    sw = AccountSwitcher()
    accs_ = {
        "cur": mk("cur", u5=0.6, u7=0.3),
        "alt": mk("alt", u5=0.1, u7=0.1),
        "dead": mk("dead", err="auth 401"),
    }
    accs_["dead"].u5h = None
    accs_["dead"].u7d = None
    out = sw.render_matrix(accs_, current="cur")
    assert "cur" in out
    assert "alt" in out
    assert "dead" in out
    assert "● **cur**" in out


def test_render_matrix_empty():
    sw = AccountSwitcher()
    out = sw.render_matrix({}, current=None)
    assert "无保存的账户" in out


# ────────────────── /usage 渲染（多账户路径）──────────────────

def test_get_usage_multi_account(monkeypatch):
    """/usage 在保存了多账户时应展示当前账户详尽 + 其他账户简表。"""
    import commands
    fake_probes = {
        "via": mk("via", u5=0.29, u7=0.10),
        "reg": mk("reg", u5=0.93, u7=0.21),
    }
    monkeypatch.setattr(accs, "probe_all", lambda: fake_probes)
    monkeypatch.setattr(accs, "current_account_name", lambda: "via")
    monkeypatch.delenv("ACCOUNT_AUTO_SWITCH", raising=False)

    out = commands._get_usage()
    # 当前账户标题
    assert "当前 `via`" in out
    # 当前账户的 bar 段（5h 29%）
    assert "29.0%" in out
    # 其他账户的简表行
    assert "`reg`" in out and "93%" in out
    # 自动切换状态
    assert "自动切换" in out and "未启用" in out


def test_get_usage_falls_back_when_no_accounts(monkeypatch):
    """没保存账户时退回老视图（只有当前 keychain 单账户）。"""
    import commands
    monkeypatch.setattr(accs, "probe_all", lambda: {})
    monkeypatch.setattr(
        commands, "fetch_quota_headers",
        lambda: {"ok": True, "u5h": 0.4, "u7d": 0.2,
                 "r5h": int(time.time() + 3600), "r7d": int(time.time() + 86400),
                 "s5h": "allowed", "s7d": "allowed"},
    )
    out = commands._get_usage()
    assert "Claude Max 用量" in out
    assert "其他账户" not in out  # 没有多账户段
    assert "40.0%" in out


def test_get_usage_shows_recommend_switch_when_alt_clearly_better(monkeypatch):
    """当前 5h 90% + 候选很空 → "（推荐切换）" 提示。"""
    import commands
    fake_probes = {
        "cur": mk("cur", u5=0.90, u7=0.60),
        "alt": mk("alt", u5=0.05, u7=0.05),
    }
    monkeypatch.setattr(accs, "probe_all", lambda: fake_probes)
    monkeypatch.setattr(accs, "current_account_name", lambda: "cur")
    out = commands._get_usage()
    assert "推荐切换" in out
