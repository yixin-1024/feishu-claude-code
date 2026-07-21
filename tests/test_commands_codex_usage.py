import commands


def test_format_codex_rate_line_uses_live_dynamic_window(monkeypatch):
    monkeypatch.setattr(
        commands,
        "_fetch_codex_rate_limits",
        lambda: {
            "primary": {"usedPercent": 17, "windowDurationMins": 10080},
            "secondary": None,
        },
    )

    assert commands._format_codex_rate_line("old-session") == "Codex 配额: `7天 17.0%`"


def test_format_codex_rate_line_supports_multiple_server_windows(monkeypatch):
    monkeypatch.setattr(
        commands,
        "_fetch_codex_rate_limits",
        lambda: {
            "primary": {"usedPercent": 12, "windowDurationMins": 60},
            "secondary": {"usedPercent": 34, "windowDurationMins": 1440},
        },
    )

    assert commands._format_codex_rate_line() == "Codex 配额: `1小时 12.0% · 1天 34.0%`"


def test_format_codex_rate_line_does_not_show_stale_data_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(commands, "_fetch_codex_rate_limits", lambda: {})
    monkeypatch.setattr(commands, "_read_latest_codex_rate_limits", lambda: {})

    assert commands._format_codex_rate_line("old-session") == "Codex 配额: `实时获取失败，请稍后重试`"


def test_format_codex_rate_line_uses_latest_global_report_as_fallback(monkeypatch):
    monkeypatch.setattr(commands, "_fetch_codex_rate_limits", lambda: {})
    monkeypatch.setattr(
        commands,
        "_read_latest_codex_rate_limits",
        lambda: {
            "_source": "reported",
            "primary": {"usedPercent": 49, "windowDurationMins": 10080},
        },
    )

    assert commands._format_codex_rate_line() == "Codex 配额（最近任务上报）: `7天 49.0%`"


def test_codex_usage_bar_lines_render_progress_bar():
    lines = commands._codex_usage_bar_lines(
        {"primary": {"usedPercent": 100, "windowDurationMins": 10080}, "secondary": None}
    )
    # 空行 + 标题 + 进度条（100% 时全满，无重置行）
    assert lines[0] == ""
    assert lines[1] == "**7天窗口**"
    assert lines[2] == "████████████████████ 100.0%"
    assert len(lines) == 3


def test_codex_usage_bar_lines_multiple_windows_with_reset():
    lines = commands._codex_usage_bar_lines(
        {
            "primary": {"usedPercent": 0, "windowDurationMins": 60, "resetsAt": 1},
            "secondary": {"usedPercent": 50, "windowDurationMins": 1440},
        }
    )
    assert "**1小时窗口**" in lines
    assert "**1天窗口**" in lines
    # 0% 全空条 + 50% 半满条
    assert "░░░░░░░░░░░░░░░░░░░░ 0.0%" in lines
    assert "██████████░░░░░░░░░░ 50.0%" in lines
    # resetsAt 存在 → 有重置行
    assert any(l.startswith("重置时间：") for l in lines)


def test_codex_usage_bar_lines_skips_windows_without_usage():
    assert commands._codex_usage_bar_lines({"primary": None, "secondary": None}) == []


def test_fmt_codex_reset_ts_shows_days_for_far_reset():
    import time as _t

    ts = int(_t.time()) + 6 * 86400 + 3 * 3600 + 120  # 6 天 3 小时后（+2m 余量抗截断）
    out = commands._fmt_codex_reset_ts(ts)
    assert "6天3h 后" in out


def test_fmt_codex_reset_ts_past_marks_reset():
    assert "已重置" in commands._fmt_codex_reset_ts(1)
