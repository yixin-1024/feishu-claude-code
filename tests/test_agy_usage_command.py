"""/usage 对 agy 的支持：解析 `agy -p /usage` 的 tab 分隔输出并渲染。

agy 报的是**剩余**百分比（不是已用），窗口有 Weekly + Five Hour 两层，
且模型分成 Gemini / Claude+GPT 两个独立池子。
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commands
from commands import _agy_iso_to_unix, _agy_usage_bar_lines, _fetch_agy_quota

REAL_OUTPUT = (
    "Gemini Models\tWeekly Limit Remaining\t100%\t2026-09-14T04:41:43Z\n"
    "Gemini Models\tFive Hour Limit Remaining\t87.5%\t2026-09-07T09:41:43Z\n"
    "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-09-14T04:57:53Z\n"
    "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-09-07T09:57:53Z\n"
)


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _patch_run(monkeypatch, proc_or_exc):
    def fake_run(*a, **k):
        if isinstance(proc_or_exc, Exception):
            raise proc_or_exc
        return proc_or_exc
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_fetch_agy_quota_parses_four_windows(monkeypatch):
    _patch_run(monkeypatch, FakeProc(REAL_OUTPUT))
    rows = _fetch_agy_quota()
    assert len(rows) == 4
    assert rows[0] == {
        "group": "Gemini Models",
        "window": "Weekly Limit Remaining",
        "remaining_pct": 100.0,
        "resets_at": "2026-09-14T04:41:43Z",
    }
    assert rows[1]["remaining_pct"] == 87.5


def test_fetch_agy_quota_skips_noise_lines(monkeypatch):
    noisy = (
        "Welcome to Antigravity CLI\n"
        "\n"
        "Gemini Models\tWeekly Limit Remaining\t42%\t2026-09-14T04:41:43Z\n"
        "some\tgarbage\tnot-a-percent\tx\n"
        "Gemini Models\tFive Hour Limit Remaining\tNaN%\t2026-09-07T09:41:43Z\n"
        "Gemini Models\tFive Hour Limit Remaining\t9%\n"  # 缺重置时刻也要收
    )
    _patch_run(monkeypatch, FakeProc(noisy))
    rows = _fetch_agy_quota()
    assert [r["remaining_pct"] for r in rows] == [42.0, 9.0]
    assert rows[1]["resets_at"] == ""


def test_fetch_agy_quota_survives_timeout(monkeypatch):
    # API key 模式 / 未登录 / agy 挂住，都必须降级成空列表而不是抛异常
    _patch_run(monkeypatch, subprocess.TimeoutExpired(cmd="agy", timeout=1))
    assert _fetch_agy_quota() == []
    _patch_run(monkeypatch, OSError("no such binary"))
    assert _fetch_agy_quota() == []
    _patch_run(monkeypatch, FakeProc(""))
    assert _fetch_agy_quota() == []


def test_agy_iso_to_unix():
    # ISO8601 UTC（Z 结尾）→ unix 秒
    assert _agy_iso_to_unix("2026-09-14T04:41:43Z") == 1789360903  # 2026-09-14T04:41:43Z
    assert _agy_iso_to_unix("") is None
    assert _agy_iso_to_unix("not-a-time") is None


def test_fetch_agy_quota_clamps_out_of_range(monkeypatch):
    _patch_run(monkeypatch, FakeProc(
        "G\tWeekly Limit Remaining\t101.5%\t2026-09-14T04:41:43Z\n"
        "G\tFive Hour Limit Remaining\t-3%\t2026-09-07T09:41:43Z\n"
    ))
    assert [r["remaining_pct"] for r in _fetch_agy_quota()] == [100.0, 0.0]


def test_agy_usage_bar_lines_groups_and_shows_remaining(monkeypatch):
    _patch_run(monkeypatch, FakeProc(REAL_OUTPUT))
    lines = _agy_usage_bar_lines(_fetch_agy_quota())
    text = "\n".join(lines)
    # 两个池子各自分节，且保持 agy 的出现顺序
    assert text.index("**Gemini Models**") < text.index("**Claude and GPT models**")
    # 两层窗口都要在
    assert "Weekly 剩余" in text and "Five Hour 剩余" in text
    # 剩余语义：87.5% 的条不能是满格
    assert "87.5%" in text
    bar_87 = next(l for l in lines if "87.5%" in l)
    assert "░" in bar_87
    bar_100 = next(l for l in lines if "100.0%" in l)
    assert "░" not in bar_100


def test_agy_usage_bar_lines_empty_when_no_rows():
    assert _agy_usage_bar_lines([]) == []
