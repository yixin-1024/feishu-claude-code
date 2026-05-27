"""account_switcher 单测：打分 / 硬筛 / 决策 / 冷却 / 切换执行 / save / use / identity 同步。

关键路径全部 mock 掉外部依赖（HTTP probe、keychain、~/.claude.json），跑纯函数 + 流程编排。
"""

import json
import os
import sys
import time
import tempfile
from unittest.mock import patch, MagicMock

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


# ────────────────── probe 401 自愈 ──────────────────


def _fake_urlopen_factory(responses):
    """造一个 urlopen mock，依次按 responses 列表返回。

    每项可以是：
      dict {"headers": {...}}                          → 模拟成功响应
      dict {"http_code": 401, "headers": {...}}       → 模拟 HTTPError
    """
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, headers):
            self.headers = headers
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b'{"id":"x"}'

    def _open(req, *_a, **_kw):
        import urllib.error
        idx = calls["n"]
        calls["n"] += 1
        if idx >= len(responses):
            raise RuntimeError(f"unexpected extra urlopen call #{idx}")
        r = responses[idx]
        code = r.get("http_code")
        if code is None:
            return FakeResp(r["headers"])
        # 模拟 HTTPError
        err = urllib.error.HTTPError(
            url="x", code=code, msg="err",
            hdrs=r.get("headers") or {}, fp=None,
        )
        # urlopen 抛 HTTPError
        raise err

    return _open, calls


def test_probe_one_force_refreshes_on_401(monkeypatch, tmp_path):
    """access_token 本地未过期但服务端 401 → force refresh + retry probe 拿到 quota。"""
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {
        "reg": {"claudeAiOauth": {
            "accessToken": "sk-ant-oat01-revoked",
            "refreshToken": "rt-old",
            "expiresAt": int((time.time() + 3 * 3600) * 1000),
        }},
    }))

    # mock OAuth refresh endpoint → 返回新 token
    def fake_refresh(name, *, force=False):
        # 模拟 refresh 成功，写新 token 回 reg.json
        import json as _json
        path = os.path.join(accs.ACCOUNTS_DIR, "reg.json")
        d = _json.load(open(path))
        d["claudeAiOauth"]["accessToken"] = "sk-ant-oat01-fresh"
        d["claudeAiOauth"]["refreshToken"] = "rt-new"
        d["claudeAiOauth"]["expiresAt"] = int((time.time() + 8 * 3600) * 1000)
        with open(path, "w") as f:
            _json.dump(d, f)
        return (True, "ok")
    refresh_calls = []
    def _track(name, *, force=False):
        refresh_calls.append((name, force))
        return fake_refresh(name, force=force)
    monkeypatch.setattr(accs, "_refresh_account_inplace", _track)

    # 第一次 urlopen 401，第二次 200 + ratelimit headers
    fake_open, _ = _fake_urlopen_factory([
        {"http_code": 401, "headers": {}},
        {"headers": {
            "anthropic-ratelimit-unified-5h-utilization": "0.10",
            "anthropic-ratelimit-unified-7d-utilization": "0.20",
            "anthropic-ratelimit-unified-5h-status": "allowed",
            "anthropic-ratelimit-unified-7d-status": "allowed",
        }},
    ])
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)

    acc = accs.load_account("reg")
    out = accs._probe_one(acc)
    assert out.probe_error is None
    assert out.u5h == 0.1 and out.u7d == 0.2
    # 强制 refresh 被调过一次，且 force=True
    assert refresh_calls == [("reg", True)]


def test_probe_one_401_then_refresh_also_fails_reports_relogin(monkeypatch, tmp_path):
    """access_token 401 + refresh_token 也被服务端废 → probe_error 明示需要重 login。"""
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {
        "reg": {"claudeAiOauth": {
            "accessToken": "sk-ant-oat01-dead",
            "refreshToken": "rt-dead",
            "expiresAt": int((time.time() + 3 * 3600) * 1000),
        }},
    }))
    monkeypatch.setattr(accs, "_refresh_account_inplace",
                        lambda name, *, force=False: (False, "HTTP 400: invalid_grant"))

    fake_open, _ = _fake_urlopen_factory([{"http_code": 401, "headers": {}}])
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)

    acc = accs.load_account("reg")
    out = accs._probe_one(acc)
    assert out.probe_error is not None
    assert "re-login" in out.probe_error
    assert "invalid_grant" in out.probe_error


def test_refresh_account_force_bypasses_fast_path(monkeypatch, tmp_path):
    """force=True 时不应因为 expiresAt 还远就 short-circuit。"""
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {
        "reg": {"claudeAiOauth": {
            "accessToken": "old",
            "refreshToken": "rt",
            "expiresAt": int((time.time() + 10 * 3600) * 1000),  # 还远着呢
        }},
    }))

    # 不真打网络 — mock urlopen 给个新 token 响应
    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({
                "access_token": "new",
                "refresh_token": "new-rt",
                "expires_in": 3600,
                "scope": "user:inference",
            }).encode()
    import urllib.request
    called = {"n": 0}
    def _open(req, *a, **k):
        called["n"] += 1
        return _R()
    monkeypatch.setattr(urllib.request, "urlopen", _open)

    ok, msg = accs._refresh_account_inplace("reg", force=True)
    assert ok, msg
    assert called["n"] == 1  # 真打了 OAuth endpoint
    # 不强制时同等 expiresAt 会 short-circuit 不打
    called["n"] = 0
    ok2, msg2 = accs._refresh_account_inplace("reg")  # 默认 force=False
    assert ok2 and "already refreshed" in msg2
    assert called["n"] == 0


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
    # 优化切换场景（当前仍可用、候选明显更优）应被冷却挡住。
    # 注：现在先 probe + decide 再判 gate，所以断言改成「没真切」(use_account 未调)。
    isolated_state.write_text(json.dumps({"last_switch_at": time.time() - 300}))
    sw = AccountSwitcher(enabled=True, cooldown_sec=1800)
    fake_probes = {
        "cur": mk("cur", u5=0.75, u7=0.5),   # usable 但 5h 偏紧
        "alt": mk("alt", u5=0.05, u7=0.05),  # 明显更优
    }
    with patch.object(accs, "probe_all", return_value=fake_probes), \
         patch.object(accs, "current_account_name", return_value="cur"), \
         patch.object(accs, "use_account") as sw_call:
        assert sw.maybe_switch() is None
        sw_call.assert_not_called()


def test_maybe_switch_proceeds_despite_active_children(isolated_state):
    # 活跃子进程不再阻挡切换——Claude 支持 keychain 热切换。
    # 优化切换场景、冷却已过，即使此刻有 claude 子进程在跑，也应正常切。
    sent = []
    sw = AccountSwitcher(send_fn=lambda t: sent.append(t), enabled=True)
    fake_probes = {
        "cur": mk("cur", u5=0.75, u7=0.5),   # usable 但 5h 偏紧
        "alt": mk("alt", u5=0.05, u7=0.05),  # 明显更优
    }
    with patch.object(accs, "probe_all", return_value=fake_probes), \
         patch.object(accs, "current_account_name", return_value="cur"), \
         patch.object(accs, "auto_stash_identity_for_current", return_value=("noop", None)), \
         patch.object(accs, "use_account", return_value=(True, "switched to alt (team/default_claude_max_5x)")) as sw_call:
        out = sw.maybe_switch()
    assert out == "alt"
    sw_call.assert_called_once_with("alt")


def test_maybe_switch_emergency_bypasses_cooldown(isolated_state):
    # 紧急切换：当前账户被硬筛淘汰（5h 满 + reset 远），即使冷却未过也必须切——
    # 这是修复「当前账户挂了一直不换」的核心保证。
    isolated_state.write_text(json.dumps({"last_switch_at": time.time() - 60}))
    sent = []
    sw = AccountSwitcher(
        send_fn=lambda t: sent.append(t),
        enabled=True,
        cooldown_sec=1800,
    )
    fake_probes = {
        "cur": mk("cur", u5=1.01, u7=0.1, r5_in=50 * 60),  # 5h 爆、50min 后才 reset → unusable
        "alt": mk("alt", u5=0.06, u7=0.1),
    }
    with patch.object(accs, "probe_all", return_value=fake_probes), \
         patch.object(accs, "current_account_name", return_value="cur"), \
         patch.object(accs, "auto_stash_identity_for_current", return_value=("noop", None)), \
         patch.object(accs, "use_account", return_value=(True, "switched to alt (team/default_claude_max_5x)")) as sw_call:
        out = sw.maybe_switch()
    assert out == "alt"
    sw_call.assert_called_once_with("alt")


def test_maybe_switch_before_spawn_noop_without_default():
    # 没注册 default switcher → no-op，绝不抛、不 probe。
    accs.set_default_switcher(None)
    accs._last_spawn_probe_at = 0.0
    assert accs.maybe_switch_before_spawn() is None


def test_maybe_switch_before_spawn_invokes_and_throttles():
    # spawn 前触发：第一次调 maybe_switch；节流窗口内第二次不再调。
    fake_sw = MagicMock()
    fake_sw.enabled = True
    fake_sw.maybe_switch.return_value = "alt"
    accs.set_default_switcher(fake_sw)
    accs._last_spawn_probe_at = 0.0
    try:
        assert accs.maybe_switch_before_spawn() == "alt"
        assert fake_sw.maybe_switch.call_count == 1
        # 节流窗口内（默认 45s）再调一次 → 直接 no-op，不再触达 maybe_switch
        assert accs.maybe_switch_before_spawn() is None
        assert fake_sw.maybe_switch.call_count == 1
        # force=True 跳过节流
        assert accs.maybe_switch_before_spawn(force=True) == "alt"
        assert fake_sw.maybe_switch.call_count == 2
    finally:
        accs.set_default_switcher(None)
        accs._last_spawn_probe_at = 0.0


def test_maybe_switch_no_action_when_current_healthy(isolated_state):
    sent = []
    sw = AccountSwitcher(send_fn=lambda t: sent.append(t), enabled=True)
    fake_probes = {
        "cur": mk("cur", u5=0.2, u7=0.2),
        "alt": mk("alt", u5=0.1, u7=0.1),
    }
    with patch.object(accs, "probe_all", return_value=fake_probes), \
         patch.object(accs, "current_account_name", return_value="cur"), \
         patch.object(accs, "use_account") as sw_call:
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
         patch.object(accs, "auto_stash_identity_for_current", return_value=("noop", None)), \
         patch.object(accs, "use_account", return_value=(True, "switched to alt (team/default_claude_max_5x)")) as sw_call:
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
         patch.object(accs, "auto_stash_identity_for_current", return_value=("noop", None)), \
         patch.object(accs, "use_account", return_value=(False, "boom")):
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


# ────────────────── decode_security_stdout (hex 兜底) ──────────────────

def test_decode_security_stdout_passthrough_json():
    raw = '{"claudeAiOauth": {"accessToken": "sk-ant-oat01-abc"}}\n'
    assert accs.decode_security_stdout(raw) == raw.strip()


def test_decode_security_stdout_unhexes_when_hex_only():
    """`security -w` 在 blob 含非可打印字符时会整段 hex 化（无 0x 前缀）。
    若上游不反解，下游 `json.loads` 报 'Extra data: line 1 column 2 (char 1)'。
    """
    payload = '{"claudeAiOauth": {"accessToken": "sk-ant-oat01-eW7Bfjhicpi"}}'
    hexed = payload.encode("utf-8").hex()  # 全 0-9a-f，偶数长度
    out = accs.decode_security_stdout(hexed + "\n")
    assert out == payload
    import json
    assert json.loads(out)["claudeAiOauth"]["accessToken"].startswith("sk-ant-oat01-")


def test_decode_security_stdout_odd_length_passthrough():
    """奇数长度 hex 串不反解（避免误伤）。"""
    assert accs.decode_security_stdout("abc") == "abc"


def test_read_keychain_blob_handles_hex_output(monkeypatch):
    """_read_keychain_blob 走 security hex 输出路径时应反解为 JSON 串。"""
    payload = '{"claudeAiOauth": {"accessToken": "sk-ant-oat01-via"}}'
    hexed = payload.encode("utf-8").hex()

    class FakeProc:
        returncode = 0
        stdout = hexed + "\n"

    monkeypatch.setattr(accs.subprocess, "run", lambda *a, **kw: FakeProc())
    blob = accs._read_keychain_blob()
    assert blob == payload
    assert accs._token_fingerprint(blob) == "sk-ant-oat01-via"


# ────────────────── ensure_keychain_intact (自愈) ──────────────────


def _make_account_dir(tmp_path, files: dict[str, dict]) -> str:
    """在 tmp_path 下造 saved accounts 目录，返回目录路径。
    files = {name: dict_to_dump_as_json}。
    """
    d = tmp_path / "accounts"
    d.mkdir()
    for name, payload in files.items():
        (d / f"{name}.json").write_text(json.dumps(payload))
    return str(d)


def test_ensure_keychain_intact_no_op_when_blob_complete(monkeypatch, tmp_path):
    monkeypatch.setattr(accs, "_read_keychain_blob",
                        lambda: '{"claudeAiOauth": {"accessToken": "sk-ant-oat01-ok"}}')
    write_calls = []
    monkeypatch.setattr(accs, "_write_keychain_blob",
                        lambda blob: write_calls.append(blob) or (True, ""))
    status, name = accs.ensure_keychain_intact()
    assert status == "ok"
    assert name is None
    assert write_calls == []  # 完整时不应写


def test_ensure_keychain_intact_restores_from_last_switch_to(monkeypatch, tmp_path):
    """blob 缺 claudeAiOauth → 优先用 state.last_switch_to 恢复。"""
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {
        "reg": {"claudeAiOauth": {"accessToken": "sk-ant-oat01-reg-tok"}, "mcpOAuth": {"x": 1}},
        "via": {"claudeAiOauth": {"accessToken": "sk-ant-oat01-via-tok"}},
    }))
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_switch_to": "reg"}))
    monkeypatch.setattr(accs, "STATE_FILE", str(state_file))
    monkeypatch.setattr(accs, "STATE_DIR", str(tmp_path))

    # 模拟 wiped keychain（只有 mcpOAuth）
    monkeypatch.setattr(accs, "_read_keychain_blob",
                        lambda: '{"mcpOAuth": {"x": 1}}')
    written = {}

    def fake_write(blob):
        written["blob"] = blob
        return (True, "")
    monkeypatch.setattr(accs, "_write_keychain_blob", fake_write)

    status, name = accs.ensure_keychain_intact()
    assert status == "restored"
    assert name == "reg"
    # 恢复用的 blob 应来自 reg.json
    assert "sk-ant-oat01-reg-tok" in written["blob"]


def test_ensure_keychain_intact_falls_back_to_newest_when_no_state(monkeypatch, tmp_path):
    """state 里没有 last_switch_to → 用 mtime 最新的 saved 文件。"""
    acc_dir = _make_account_dir(tmp_path, {
        "old": {"claudeAiOauth": {"accessToken": "sk-ant-oat01-old"}},
        "new": {"claudeAiOauth": {"accessToken": "sk-ant-oat01-new"}},
    })
    # 让 new.json 的 mtime 比 old.json 新
    os.utime(os.path.join(acc_dir, "old.json"), (time.time() - 3600, time.time() - 3600))
    os.utime(os.path.join(acc_dir, "new.json"), (time.time(), time.time()))
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", acc_dir)
    monkeypatch.setattr(accs, "STATE_FILE", str(tmp_path / "no_state.json"))  # 不存在
    monkeypatch.setattr(accs, "STATE_DIR", str(tmp_path))

    monkeypatch.setattr(accs, "_read_keychain_blob", lambda: None)
    written = {}

    def _w(blob):
        written.setdefault("blob", blob)
        return (True, "")
    monkeypatch.setattr(accs, "_write_keychain_blob", _w)

    status, name = accs.ensure_keychain_intact()
    assert status == "restored"
    assert name == "new"
    assert "sk-ant-oat01-new" in written["blob"]


def test_ensure_keychain_intact_no_active_when_dir_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", str(tmp_path / "empty"))
    os.makedirs(str(tmp_path / "empty"))
    monkeypatch.setattr(accs, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(accs, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(accs, "_read_keychain_blob", lambda: None)
    monkeypatch.setattr(accs, "_write_keychain_blob",
                        lambda blob: (_ for _ in ()).throw(AssertionError("should not write")))
    status, name = accs.ensure_keychain_intact()
    assert status == "no_active"
    assert name is None


def test_ensure_keychain_intact_skips_saved_files_without_oauth(monkeypatch, tmp_path):
    """saved 文件本身不含 claudeAiOauth 的应该被跳过。"""
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {
        "broken": {"mcpOAuth": {"x": 1}},  # 缺 claudeAiOauth
        "good": {"claudeAiOauth": {"accessToken": "sk-ant-oat01-good"}},
    }))
    # state 指向 broken，应该跳过去用 good
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_switch_to": "broken"}))
    monkeypatch.setattr(accs, "STATE_FILE", str(state_file))
    monkeypatch.setattr(accs, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(accs, "_read_keychain_blob", lambda: None)
    written = {}

    def _w(blob):
        written.setdefault("blob", blob)
        return (True, "")
    monkeypatch.setattr(accs, "_write_keychain_blob", _w)
    status, name = accs.ensure_keychain_intact()
    assert status == "restored"
    assert name == "good"
    assert "sk-ant-oat01-good" in written["blob"]


def test_ensure_keychain_intact_propagates_write_error(monkeypatch, tmp_path):
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {
        "reg": {"claudeAiOauth": {"accessToken": "sk-ant-oat01-reg"}},
    }))
    monkeypatch.setattr(accs, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(accs, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(accs, "_read_keychain_blob", lambda: None)
    monkeypatch.setattr(accs, "_write_keychain_blob",
                        lambda blob: (False, "permission denied"))
    status, msg = accs.ensure_keychain_intact()
    assert status == "error"
    assert "permission" in msg


def test_ensure_keychain_intact_strips_meta_before_writing_keychain(monkeypatch, tmp_path):
    """v2 saved file 含 _meta，写 keychain 前必须 strip 掉，否则可能 confuse CLI。"""
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {
        "reg": {
            "claudeAiOauth": {"accessToken": "sk-ant-oat01-reg-tok"},
            "mcpOAuth": {"foo": "bar"},
            "_meta": {"schema_version": 2, "identity": {
                "oauthAccount": {"accountUuid": "uuid-reg", "emailAddress": "reg@example.com"},
                "userID": "uid-reg",
            }},
        },
    }))
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"last_switch_to": "reg"}))
    monkeypatch.setattr(accs, "STATE_FILE", str(state_file))
    monkeypatch.setattr(accs, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(accs, "_read_keychain_blob", lambda: None)

    written = {}
    monkeypatch.setattr(accs, "_write_keychain_blob",
                        lambda blob: (written.setdefault("blob", blob), (True, ""))[1])

    # patch_identity 也 mock，不真碰 ~/.claude.json
    patched = {}
    monkeypatch.setattr(accs, "_patch_identity",
                        lambda ident: (patched.setdefault("ident", ident), (True, "ok"))[1])

    status, name = accs.ensure_keychain_intact()
    assert status == "restored"
    # 写到 keychain 的 blob 不应含 _meta
    parsed = json.loads(written["blob"])
    assert "_meta" not in parsed
    assert parsed["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-reg-tok"
    assert parsed["mcpOAuth"] == {"foo": "bar"}
    # identity 应该被传去 patch ~/.claude.json
    assert patched["ident"]["oauthAccount"]["accountUuid"] == "uuid-reg"
    assert patched["ident"]["userID"] == "uid-reg"


# ────────────────── save_current_account / use_account / identity ──────────────────


def test_save_current_account_writes_schema_v2_with_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", str(tmp_path / "accounts"))
    monkeypatch.setattr(accs, "_read_keychain_blob",
                        lambda: json.dumps({
                            "claudeAiOauth": {
                                "accessToken": "sk-ant-oat01-foo",
                                "subscriptionType": "team",
                                "rateLimitTier": "default_claude_max_5x",
                            },
                            "mcpOAuth": {"x": 1},
                        }))
    ident = {
        "oauthAccount": {"accountUuid": "uuid-foo", "emailAddress": "foo@example.com"},
        "userID": "uid-foo",
    }
    monkeypatch.setattr(accs, "_read_identity", lambda: ident)

    ok, msg = accs.save_current_account("foo")
    assert ok, msg
    path = os.path.join(accs.ACCOUNTS_DIR, "foo.json")
    saved = json.loads(open(path).read())
    assert saved["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-foo"
    assert saved["mcpOAuth"] == {"x": 1}
    assert saved["_meta"]["schema_version"] == 2
    assert saved["_meta"]["identity"]["oauthAccount"]["accountUuid"] == "uuid-foo"
    assert "foo@example.com" in msg


def test_save_current_account_warns_when_identity_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", str(tmp_path / "accounts"))
    monkeypatch.setattr(accs, "_read_keychain_blob",
                        lambda: json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat01-bar"}}))
    monkeypatch.setattr(accs, "_read_identity", lambda: None)

    ok, msg = accs.save_current_account("bar")
    assert ok
    assert "identity missing" in msg
    saved = json.loads(open(os.path.join(accs.ACCOUNTS_DIR, "bar.json")).read())
    assert saved["_meta"]["identity"] is None


def test_save_current_account_rejects_invalid_name(monkeypatch, tmp_path):
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", str(tmp_path / "accounts"))
    monkeypatch.setattr(accs, "_read_keychain_blob",
                        lambda: json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    monkeypatch.setattr(accs, "_read_identity", lambda: None)
    ok, msg = accs.save_current_account("bad name with spaces")
    assert not ok and "name must" in msg


def test_save_current_account_rejects_empty_keychain(monkeypatch, tmp_path):
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", str(tmp_path / "accounts"))
    monkeypatch.setattr(accs, "_read_keychain_blob", lambda: None)
    ok, msg = accs.save_current_account("foo")
    assert not ok and "keychain" in msg


def test_use_account_writes_keychain_and_patches_identity(monkeypatch, tmp_path):
    """切到 reg：keychain 收 strip 过 _meta 的 blob；~/.claude.json 收 identity patch。"""
    blob = {
        "claudeAiOauth": {"accessToken": "sk-ant-oat01-reg", "subscriptionType": "team", "rateLimitTier": "t1"},
        "mcpOAuth": {"a": 1},
        "_meta": {"schema_version": 2, "identity": {
            "oauthAccount": {"accountUuid": "uuid-reg"},
            "userID": "uid-reg",
        }},
    }
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {"reg": blob}))
    written_kc = {}
    monkeypatch.setattr(accs, "_write_keychain_blob",
                        lambda b: (written_kc.setdefault("blob", b), (True, ""))[1])
    patched = {}
    monkeypatch.setattr(accs, "_patch_identity",
                        lambda ident: (patched.setdefault("ident", ident), (True, "ok"))[1])

    ok, msg = accs.use_account("reg")
    assert ok, msg
    parsed = json.loads(written_kc["blob"])
    assert "_meta" not in parsed
    assert parsed["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-reg"
    assert patched["ident"]["oauthAccount"]["accountUuid"] == "uuid-reg"


def test_use_account_warns_when_identity_missing_in_saved_file(monkeypatch, tmp_path):
    """v1 schema saved file 没 identity → use 仍写 keychain，但 msg 带 warn 且不 patch identity。"""
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {
        "old": {"claudeAiOauth": {"accessToken": "sk-ant-oat01-old"}},
    }))
    monkeypatch.setattr(accs, "_write_keychain_blob", lambda b: (True, ""))
    patch_calls = []
    monkeypatch.setattr(accs, "_patch_identity",
                        lambda ident: (patch_calls.append(ident), (True, "ok"))[1])
    ok, msg = accs.use_account("old")
    assert ok
    assert "identity missing" in msg
    assert patch_calls == []  # 没 identity 就不调 _patch_identity


def test_use_account_unknown_name(monkeypatch, tmp_path):
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {}))
    ok, msg = accs.use_account("ghost")
    assert not ok and "no saved account" in msg


def test_auto_stash_identity_writes_into_v1_file(monkeypatch, tmp_path):
    """当前 keychain = 某 v1 saved 账户 + ~/.claude.json 有 identity → 自动 stash 回该文件。"""
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {
        "via": {"claudeAiOauth": {"accessToken": "sk-ant-oat01-viatok"}},
    }))
    monkeypatch.setattr(accs, "current_account_name", lambda: "via")
    monkeypatch.setattr(accs, "_read_identity", lambda: {
        "oauthAccount": {"accountUuid": "uuid-via", "emailAddress": "via@example.com"},
        "userID": "uid-via",
    })
    st, nm = accs.auto_stash_identity_for_current()
    assert st == "stashed" and nm == "via"
    saved = json.loads(open(os.path.join(accs.ACCOUNTS_DIR, "via.json")).read())
    assert saved["_meta"]["identity"]["oauthAccount"]["accountUuid"] == "uuid-via"
    # keychain blob 部分原样保留
    assert saved["claudeAiOauth"]["accessToken"] == "sk-ant-oat01-viatok"


def test_auto_stash_identity_noop_when_already_has(monkeypatch, tmp_path):
    """saved file 已带 identity → 不重复写。"""
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {
        "via": {
            "claudeAiOauth": {"accessToken": "sk-ant-oat01-vtok"},
            "_meta": {"identity": {"oauthAccount": {"accountUuid": "uuid-via"}, "userID": "uid"}},
        },
    }))
    monkeypatch.setattr(accs, "current_account_name", lambda: "via")
    monkeypatch.setattr(accs, "_read_identity", lambda: {"oauthAccount": {"accountUuid": "other"}})
    st, _ = accs.auto_stash_identity_for_current()
    assert st == "noop"
    saved = json.loads(open(os.path.join(accs.ACCOUNTS_DIR, "via.json")).read())
    # 没被覆盖
    assert saved["_meta"]["identity"]["oauthAccount"]["accountUuid"] == "uuid-via"


def test_auto_stash_identity_noop_when_no_current(monkeypatch, tmp_path):
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", str(tmp_path / "x"))
    monkeypatch.setattr(accs, "current_account_name", lambda: None)
    monkeypatch.setattr(accs, "_read_identity", lambda: {"oauthAccount": {"accountUuid": "u"}, "userID": "u"})
    st, _ = accs.auto_stash_identity_for_current()
    assert st == "noop"


def test_patch_identity_atomic_partial_overwrite(monkeypatch, tmp_path):
    """只覆盖 _IDENTITY_KEYS 这两个字段，其它字段原样保留。"""
    ip = tmp_path / "claude.json"
    ip.write_text(json.dumps({
        "userID": "old-uid",
        "oauthAccount": {"accountUuid": "old", "emailAddress": "old@x.com"},
        "settings": {"theme": "dark"},   # 不该被动
        "cachedStuff": [1, 2, 3],
    }))
    monkeypatch.setattr(accs, "IDENTITY_PATH", str(ip))
    ok, msg = accs._patch_identity({
        "oauthAccount": {"accountUuid": "new", "emailAddress": "new@x.com"},
        "userID": "new-uid",
    })
    assert ok, msg
    after = json.loads(ip.read_text())
    assert after["userID"] == "new-uid"
    assert after["oauthAccount"]["accountUuid"] == "new"
    assert after["settings"] == {"theme": "dark"}  # 保留
    assert after["cachedStuff"] == [1, 2, 3]


def test_patch_identity_noop_when_already_in_sync(monkeypatch, tmp_path):
    ip = tmp_path / "claude.json"
    ip.write_text(json.dumps({"oauthAccount": {"accountUuid": "u"}, "userID": "uid"}))
    monkeypatch.setattr(accs, "IDENTITY_PATH", str(ip))
    mtime_before = os.path.getmtime(ip)
    time.sleep(0.05)
    ok, msg = accs._patch_identity({"oauthAccount": {"accountUuid": "u"}, "userID": "uid"})
    assert ok and "in sync" in msg
    # 文件没被重写
    assert os.path.getmtime(ip) == mtime_before


def test_strip_meta_keeps_only_keychain_keys():
    blob = {
        "claudeAiOauth": {"accessToken": "tok"},
        "mcpOAuth": {"x": 1},
        "_meta": {"identity": {"y": 2}},
        "junk": "drop me",
    }
    out = accs._strip_meta(blob)
    assert out == {"claudeAiOauth": {"accessToken": "tok"}, "mcpOAuth": {"x": 1}}


def test_remove_account(monkeypatch, tmp_path):
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {
        "foo": {"claudeAiOauth": {"accessToken": "t"}},
    }))
    ok, _ = accs.remove_account("foo")
    assert ok
    assert not os.path.exists(os.path.join(accs.ACCOUNTS_DIR, "foo.json"))
    ok2, msg2 = accs.remove_account("foo")
    assert not ok2 and "no saved" in msg2


def test_list_accounts_summary_marks_active_and_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(accs, "ACCOUNTS_DIR", _make_account_dir(tmp_path, {
        "via": {
            "claudeAiOauth": {"accessToken": "via-tok", "subscriptionType": "team", "rateLimitTier": "t1"},
            "_meta": {"identity": {"oauthAccount": {"emailAddress": "via@x.com"}, "userID": "u"}},
        },
        "reg": {"claudeAiOauth": {"accessToken": "reg-tok"}},  # v1, no identity
    }))
    monkeypatch.setattr(accs, "current_account_name", lambda: "via")
    rows = {r["name"]: r for r in accs.list_accounts_summary()}
    assert rows["via"]["active"] is True and rows["via"]["has_identity"] is True
    assert rows["via"]["email"] == "via@x.com"
    assert rows["reg"]["active"] is False and rows["reg"]["has_identity"] is False

