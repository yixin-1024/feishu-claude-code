from datetime import datetime

import pytest

import commands
import server_stats as ss


def test_parse_meminfo_converts_kb_to_bytes():
    mem = ss.parse_meminfo(
        "MemTotal:       16000000 kB\nMemAvailable:    4000000 kB\nHugePages_Total:       0\n"
    )
    assert mem["MemTotal"] == 16000000 * 1024
    assert mem["MemAvailable"] == 4000000 * 1024
    assert mem["HugePages_Total"] == 0


def test_cpu_percent_from_two_stat_samples():
    before = ss.parse_cpu_times("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 2 3 4\n")
    after = ss.parse_cpu_times("cpu  250 0 250 1300 200 0 0 0 0 0\n")
    # 两次之间 total +1000，idle+iowait +700 → 忙 30%
    assert ss.cpu_percent(before, after) == pytest.approx(30.0)
    assert ss.cpu_percent(after, after) is None


def test_parse_pressure_takes_avg10():
    psi = ss.parse_pressure(
        "some avg10=12.50 avg60=3.00 avg300=1.00 total=1\n"
        "full avg10=4.25 avg60=1.00 avg300=0.50 total=1\n"
    )
    assert psi == {"some": 12.5, "full": 4.25}


def test_parse_proc_stat_handles_parens_and_spaces_in_comm():
    fields = ["S"] + ["0"] * 10 + ["30", "12"] + ["0"] * 8 + ["250"] + ["0"] * 20
    row = ss.parse_proc_stat("4242 (weird (name) x) " + " ".join(fields))
    assert row == ("weird (name) x", 42, 250 * ss._PAGE)
    assert ss.parse_proc_stat("garbage") is None


def test_service_cgroup_climbs_from_delegated_leaf():
    assert ss.service_cgroup("0::/system.slice/cc-lark.service/supervisor\n") == \
        "/system.slice/cc-lark.service"
    assert ss.service_cgroup("0::/user.slice/user-1000.slice/session-3.scope\n") is None


def _snap(**over):
    gb = 1024 ** 3
    snap = {
        "host": "box", "os": "Ubuntu", "cores": 4,
        "now": datetime(2026, 9, 28, 10, 0, 0), "load": (6.5, 2.0, 1.0),
        "linux": True, "cpu": 95.0, "uptime": 90000.0,
        "mem": {"MemTotal": 16 * gb, "MemAvailable": 1 * gb,
                "SwapTotal": 8 * gb, "SwapFree": 8 * gb},
        "psi_mem": {"some": 25.0, "full": 9.0}, "psi_io": {"some": 0.0},
        "disks": [{"mount": "/", "total": 50 * gb, "used": 20 * gb, "free": 30 * gb}],
        "cgroup": {"unit": "cc-lark.service", "current": 3 * gb, "max": None,
                   "peak": 5 * gb, "oom_kill": 2},
        "procs": [
            {"pid": 1, "comm": "claude", "rss": 2 * gb, "cpu": 80.0},
            {"pid": 2, "comm": "python", "rss": 1 * gb, "cpu": 0.1},
        ],
    }
    snap.update(over)
    return snap


def test_render_flags_hot_spots_and_lists_top_processes():
    text = ss.render(_snap())
    assert "🔴 CPU" in text                    # 95%
    assert "🔴 负载 6.50" in text               # 6.5 / 4 核 > 150%
    assert "🔴 内存" in text and "94%" in text  # 15/16 GB
    assert "🔴 内存压力 25.0%" in text
    assert "🟢 `/`" in text and "40%" in text   # 20 / (20+30)
    assert "已被 OOM 杀过 2 次" in text
    assert "claude 1 个" in text
    assert "`claude`(1) 80%" in text
    assert "`python`(2)" not in text.split("CPU 前 5：")[1]  # 0.1% 不上榜


def test_render_non_linux_only_load_and_disk():
    gb = 1024 ** 3
    text = ss.render({
        "host": "mac", "os": "Darwin", "cores": 8, "now": datetime(2026, 9, 28),
        "load": (1.0, 1.0, 1.0), "linux": False,
        "disks": [{"mount": "/", "total": 100 * gb, "used": 50 * gb, "free": 50 * gb}],
    })
    assert "负载 1.00" in text and "`/`" in text and "非 Linux" in text
    assert "**内存**" not in text


def test_get_report_never_raises(monkeypatch):
    monkeypatch.setattr(ss, "collect", lambda: (_ for _ in ()).throw(OSError("boom")))
    assert ss.get_report().startswith("❌ 读取服务器指标失败")


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["server", "sys"])
async def test_server_command_returns_report_with_refresh_button(monkeypatch, cmd):
    monkeypatch.setattr(ss, "get_report", lambda: "REPORT")
    reply = await commands.handle_command(cmd, "", "ou_user", "oc_chat", store=None)
    assert reply["text"] == "REPORT"
    assert reply["buttons"][0]["value"] == {
        "action": "run_cmd", "cmd": "/server", "cid": "oc_chat",
    }
    assert await commands.handle_command(cmd, "", "ou_user", "", store=None) == "REPORT"
