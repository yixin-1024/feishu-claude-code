"""凭证被 env 钉死（CLAUDE_CODE_OAUTH_TOKEN 等）时，账户池必须整体让路。

2026-09-14 财务机实测的三连翻车：.env 钉了 reg 席位的 setup-token，但
  ① 撞用量墙后仍然去切账户池（切了完全不生效，只是把活跃 slot 改乱）；
  ② 卡片按 credentials.json 的名义 slot 自报"账户 info 撞墙 → 已切到 reg"，
     而真正在跑的一直是 env token 的 reg 席位；
  ③ 配额恢复唤醒用名义账户 info 的 5h 窗口（17:50）而不是实际席位的（13:50），
     把任务白推迟了 4 小时。
外加 "resets 5:50am (UTC)" 被当成北京时间解析。
"""

import os
import sys
import time
from unittest import mock

os.environ.setdefault("FEISHU_APP_ID", "test-app-id")
os.environ.setdefault("FEISHU_APP_SECRET", "test-app-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import account_switcher as accs  # noqa: E402
import dispatcher  # noqa: E402
from account_switcher import Account  # noqa: E402


def _clear_pins(monkeypatch):
    for k in accs._POOL_PINNING_ENVS:
        monkeypatch.delenv(k, raising=False)


# ── pinned 探测 helper ───────────────────────────────────────────

def test_pinned_env_credential_detects_and_ignores_blank(monkeypatch):
    _clear_pins(monkeypatch)
    assert accs.pinned_env_credential() == ("", "")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "   ")
    assert accs.pinned_env_credential() == ("", ""), "空白串不算钉死"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xyz")
    assert accs.pinned_env_credential() == ("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xyz")


def test_probe_pinned_uses_env_token_not_pool(monkeypatch):
    _clear_pins(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-env")
    seen = {}

    def fake_probe(acc, is_current=False):
        seen["token"] = acc.access_token
        acc.u5h, acc.u7d, acc.r5h = 0.07, 0.22, int(time.time()) + 60
        return acc

    monkeypatch.setattr(accs, "_probe_one", fake_probe)
    out = accs.probe_pinned_env_credential()
    assert seen["token"] == "tok-env", "必须探 env 那份凭证本身"
    assert out.u5h == 0.07


# ── ① 撞墙不切号 + ③ 恢复时刻按真实席位算 ────────────────────────

def test_emergency_switch_refuses_when_pinned(monkeypatch):
    _clear_pins(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-reg")
    reset_at = int(time.time()) + 60          # 实际席位 1 分钟后重置
    monkeypatch.setattr(accs, "probe_all", mock.Mock(side_effect=AssertionError("不该探账户池")))
    monkeypatch.setattr(accs, "use_account", mock.Mock(side_effect=AssertionError("不该切号")))
    monkeypatch.setattr(accs, "probe_pinned_env_credential",
                        lambda: Account(name="env:x", u5h=1.0, u7d=0.21, r5h=reset_at))

    out = accs.emergency_switch_on_limit(notify=False)

    assert out["switched"] is None, "凭证被钉死时切号无效，不许切"
    assert out["from"] == "env:CLAUDE_CODE_OAUTH_TOKEN"
    assert out["current_reset_epoch"] == float(reset_at), "恢复时刻要按真正在跑的席位算"
    assert "钉死" in out["reason"] and "CLAUDE_CODE_OAUTH_TOKEN" in out["reason"]


def test_emergency_switch_still_switches_when_not_pinned(monkeypatch):
    """反向守卫：没钉死时该切照切（别把功能整个关掉）。"""
    _clear_pins(monkeypatch)
    now = time.time()
    pool = {
        "a": Account(name="a", u5h=1.0, u7d=0.3, r5h=int(now + 3600), r7d=int(now + 8 * 3600)),
        "b": Account(name="b", u5h=0.1, u7d=0.1, r5h=int(now + 3600), r7d=int(now + 8 * 3600)),
    }
    monkeypatch.setattr(accs, "probe_all", lambda *a, **k: pool)
    monkeypatch.setattr(accs, "current_account_name", lambda: "a")
    monkeypatch.setattr(accs, "auto_stash_identity_for_current", lambda: ("noop", ""))
    monkeypatch.setattr(accs, "use_account", lambda name: (True, f"switched to {name}"))
    monkeypatch.setattr(accs, "_save_state", lambda state: None)

    out = accs.emergency_switch_on_limit(notify=False)
    assert out["switched"] == "b"


def test_spawn_switch_is_noop_when_pinned(monkeypatch):
    _clear_pins(monkeypatch)
    sw = mock.Mock(enabled=True)
    sw.maybe_switch = mock.Mock(side_effect=AssertionError("不该切号"))
    monkeypatch.setattr(accs, "_DEFAULT_SWITCHER", sw)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-relay-xxx")
    assert accs.maybe_switch_before_spawn(force=True) is None
    sw.maybe_switch.assert_not_called()


# ── ② 手动切号必须说清"切了也不生效" ─────────────────────────────

def test_manual_switch_warns_when_pinned(monkeypatch):
    _clear_pins(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-reg")
    monkeypatch.setattr(accs, "_validate_name", lambda n: None)
    monkeypatch.setattr(accs, "current_account_name", lambda: "reg")
    monkeypatch.setattr(accs, "resync_current_from_keychain", lambda: ("noop", ""))
    monkeypatch.setattr(accs, "use_account", lambda name: (True, f"switched to {name}"))
    monkeypatch.setattr(accs, "_load_state", lambda: {})
    monkeypatch.setattr(accs, "_save_state", lambda state: None)

    ok, msg = accs.switch_account_manually("info")
    assert ok
    assert "不生效" in msg and "CLAUDE_CODE_OAUTH_TOKEN" in msg


# ── ④ "resets 5:50am (UTC)" 不能按北京时间算 ─────────────────────

def _sh_epoch(text: str) -> float:
    """北京时间字面量 → epoch（不依赖跑测试这台机器的时区，财务机是 UTC）。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def test_parse_reset_minutes_honours_utc_suffix():
    # 北京 2026-09-14 13:49 = UTC 05:49；文案说 UTC 05:50 → 应是 1 分钟后，
    # 按北京解释则会变成次日 05:50（~960 分钟）。
    mins = dispatcher._parse_reset_minutes(
        "You've hit your session limit · resets 5:50am (UTC)",
        now=_sh_epoch("2026-09-14 13:49:00"))
    assert mins is not None and mins <= 5, f"UTC 文案被当成本地时间了：{mins} 分钟"


def test_parse_reset_minutes_local_text_unchanged():
    """没有 UTC 后缀的老文案仍按 Asia/Shanghai。"""
    mins = dispatcher._parse_reset_minutes(
        "resets 12:20pm", now=_sh_epoch("2026-09-14 11:00:00"))
    assert mins is not None and 75 <= mins <= 85, mins
