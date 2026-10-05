"""`/server`（别名 `/sys`）—— 本机 CPU / 负载 / 内存 / 磁盘 / 进程一屏看完。

只读 /proc、/sys/fs/cgroup 和 statvfs，不引入 psutil。非 Linux（Mac 部署）没有
/proc，只出负载和根盘。

2026-08-31 财务机内存打满挂死 36 分钟、内核全程没触发 OOM、日志零告警——所以
内存压力（PSI）和 cc-lark 服务自己的 cgroup 用量是这里最该一眼看到的两项：
PSI 在机器开始 thrash 时先涨，远早于"内存用了多少"变红。
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

_PROC = "/proc"
_CGROUP_ROOT = "/sys/fs/cgroup"
_SAMPLE_SEC = 0.5
_TOP_N = 5
_TZ = ZoneInfo("Asia/Shanghai")

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


# ── 解析（纯函数，单测直接喂文本）──────────────────────────────

def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo → {字段: 字节}。"""
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            val = int(parts[0])
        except ValueError:
            continue
        out[key.strip()] = val * 1024 if len(parts) > 1 and parts[1] == "kB" else val
    return out


def parse_cpu_times(text: str) -> tuple[int, int]:
    """/proc/stat 首行 → (idle+iowait, total) jiffies。"""
    for line in text.splitlines():
        if line.startswith("cpu "):
            nums = [int(x) for x in line.split()[1:]]
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
            # guest / guest_nice 已计入 user / nice，别重复算
            return idle, sum(nums[:8])
    return 0, 0


def cpu_percent(before: tuple[int, int], after: tuple[int, int]) -> Optional[float]:
    d_total = after[1] - before[1]
    if d_total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1 - (after[0] - before[0]) / d_total)))


def parse_pressure(text: str) -> dict[str, float]:
    """/proc/pressure/<res> → {"some": avg10, "full": avg10}（百分比）。"""
    out: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        for kv in parts[1:]:
            if kv.startswith("avg10="):
                try:
                    out[parts[0]] = float(kv[6:])
                except ValueError:
                    pass
    return out


def parse_proc_stat(text: str) -> Optional[tuple[str, int, int]]:
    """/proc/<pid>/stat → (comm, utime+stime ticks, rss 字节)。

    comm 可能含空格和括号，所以按最后一个 ')' 切。
    """
    lpar, rpar = text.find("("), text.rfind(")")
    if lpar < 0 or rpar < lpar:
        return None
    comm = text[lpar + 1:rpar]
    rest = text[rpar + 2:].split()
    # rest[0] 是 state（原第 3 列）；utime/stime 原第 14/15 列，rss 原第 24 列
    try:
        return comm, int(rest[11]) + int(rest[12]), int(rest[21]) * _PAGE
    except (IndexError, ValueError):
        return None


def service_cgroup(self_cgroup_text: str) -> Optional[str]:
    """/proc/self/cgroup（v2）→ 所在 systemd .service 的 cgroup 路径。

    开了 Delegate 后 bot 自己在 `<unit>.service/supervisor` 叶子里，要往上找到
    `.service` 那一层才是整个服务（含所有 claude 子进程）的总账。
    """
    for line in self_cgroup_text.splitlines():
        if not line.startswith("0::"):
            continue
        parts = line[3:].strip("/").split("/")
        for i in range(len(parts) - 1, -1, -1):
            if parts[i].endswith(".service"):
                return "/" + "/".join(parts[:i + 1])
    return None


# ── 采集 ────────────────────────────────────────────────────

def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _proc_table() -> dict[int, tuple[str, int, int]]:
    table: dict[int, tuple[str, int, int]] = {}
    try:
        pids = [int(n) for n in os.listdir(_PROC) if n.isdigit()]
    except OSError:
        return table
    for pid in pids:
        row = parse_proc_stat(_read(f"{_PROC}/{pid}/stat"))
        if row:
            table[pid] = row
    return table


def _disks() -> list[dict]:
    """真实块设备挂载点（跳过 tmpfs / squashfs 快照 / 容器层），同一设备只算一次。"""
    out, seen = [], set()
    for line in _read(f"{_PROC}/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1].replace("\\040", " "), parts[2]
        if not dev.startswith("/dev/") or fstype == "squashfs" or dev in seen:
            continue
        seen.add(dev)
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            continue
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        out.append({"mount": mnt, "total": total, "used": used, "free": free})
    return out


def _cgroup_mem() -> Optional[dict]:
    path = service_cgroup(_read(f"{_PROC}/self/cgroup"))
    if not path:
        return None
    base = f"{_CGROUP_ROOT}{path}"
    cur = _read(f"{base}/memory.current").strip()
    if not cur.isdigit():
        return None
    mx = _read(f"{base}/memory.max").strip()
    peak = _read(f"{base}/memory.peak").strip()
    oom_kill = 0
    for line in _read(f"{base}/memory.events").splitlines():
        k, _, v = line.partition(" ")
        if k == "oom_kill" and v.strip().isdigit():
            oom_kill = int(v)
    return {
        "unit": path.rsplit("/", 1)[-1],
        "current": int(cur),
        "max": int(mx) if mx.isdigit() else None,
        "peak": int(peak) if peak.isdigit() else None,
        "oom_kill": oom_kill,
    }


def _os_name() -> str:
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return f"{platform.system()} {platform.release()}"


def collect() -> dict:
    """采一份快照。CPU 占用靠前后两次 /proc 采样求差，会阻塞 _SAMPLE_SEC 秒。"""
    snap: dict = {
        "host": socket.gethostname(),
        "os": _os_name(),
        "cores": os.cpu_count() or 1,
        "now": datetime.now(_TZ),
        "load": os.getloadavg() if hasattr(os, "getloadavg") else None,
        "linux": os.path.exists(f"{_PROC}/meminfo"),
    }
    if not snap["linux"]:
        du = shutil.disk_usage("/")
        snap["disks"] = [{"mount": "/", "total": du.total, "used": du.used, "free": du.free}]
        return snap

    cpu0, procs0, t0 = parse_cpu_times(_read(f"{_PROC}/stat")), _proc_table(), time.monotonic()
    time.sleep(_SAMPLE_SEC)
    cpu1, procs1, t1 = parse_cpu_times(_read(f"{_PROC}/stat")), _proc_table(), time.monotonic()

    elapsed = max(t1 - t0, 1e-3)
    procs = []
    for pid, (comm, ticks, rss) in procs1.items():
        prev = procs0.get(pid)
        pct = (ticks - prev[1]) / _CLK_TCK / elapsed * 100 if prev and prev[0] == comm else 0.0
        procs.append({"pid": pid, "comm": comm, "rss": rss, "cpu": max(pct, 0.0)})

    up = _read(f"{_PROC}/uptime").split()
    snap.update({
        "cpu": cpu_percent(cpu0, cpu1),
        "mem": parse_meminfo(_read(f"{_PROC}/meminfo")),
        "psi_mem": parse_pressure(_read(f"{_PROC}/pressure/memory")),
        "psi_io": parse_pressure(_read(f"{_PROC}/pressure/io")),
        "uptime": float(up[0]) if up else None,
        "disks": _disks(),
        "cgroup": _cgroup_mem(),
        "procs": procs,
    })
    return snap


# ── 渲染 ────────────────────────────────────────────────────

def _lvl(pct: Optional[float], warn: float, crit: float) -> str:
    if pct is None:
        return "⚪"
    return "🔴" if pct >= crit else ("🟡" if pct >= warn else "🟢")


def _bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _size(n: Optional[float]) -> str:
    if n is None:
        return "?"
    if n >= 1024 ** 4:
        return f"{n / 1024 ** 4:.1f} TB"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{n / 1024 ** 2:.0f} MB"


def _dur(secs: float) -> str:
    secs = int(secs)
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    return f"{d}天{h}小时" if d else f"{h}小时{m}分"


def render(snap: dict) -> str:
    cores = snap["cores"]
    head = f"🖥️ **服务器状态** · `{snap['host']}` · {snap['now'].strftime('%m-%d %H:%M:%S')}（+08）"
    sub = [snap["os"], f"{cores} 核"]
    if snap.get("uptime"):
        sub.append(f"已运行 {_dur(snap['uptime'])}")
    lines = [head, " · ".join(sub), ""]

    lines.append("**CPU / 负载**")
    if snap.get("cpu") is not None:
        cpu = snap["cpu"]
        lines.append(f"{_lvl(cpu, 70, 90)} CPU {_bar(cpu)} {cpu:.0f}%")
    if snap.get("load"):
        l1, l5, l15 = snap["load"]
        lines.append(f"{_lvl(l1 / cores * 100, 80, 150)} 负载 {l1:.2f} / {l5:.2f} / {l15:.2f}"
                     f"（1/5/15 分钟，满载≈{cores}）")
    if snap.get("psi_io"):
        io = snap["psi_io"].get("some", 0.0)
        lines.append(f"{_lvl(io, 10, 30)} IO 等待压力 {io:.1f}%（近 10 秒）")
    lines.append("")

    mem = snap.get("mem") or {}
    if mem.get("MemTotal"):
        lines.append("**内存**")
        total, avail = mem["MemTotal"], mem.get("MemAvailable", mem.get("MemFree", 0))
        used_pct = (total - avail) / total * 100
        lines.append(f"{_lvl(used_pct, 80, 90)} 内存 {_bar(used_pct)} {used_pct:.0f}%"
                     f" · 已用 {_size(total - avail)} / {_size(total)}，可用 {_size(avail)}")
        swap_total = mem.get("SwapTotal", 0)
        if swap_total:
            swap_used = swap_total - mem.get("SwapFree", 0)
            sp = swap_used / swap_total * 100
            lines.append(f"{_lvl(sp, 50, 80)} Swap {_bar(sp)} {sp:.0f}%"
                         f" · {_size(swap_used)} / {_size(swap_total)}")
        else:
            lines.append("⚪ Swap 未开启")
        if snap.get("psi_mem"):
            some = snap["psi_mem"].get("some", 0.0)
            full = snap["psi_mem"].get("full", 0.0)
            lines.append(f"{_lvl(some, 5, 20)} 内存压力 {some:.1f}%（近 10 秒，完全卡住 {full:.1f}%）"
                         "——持续上涨＝开始抖，先于内存条变红")
        cg = snap.get("cgroup")
        if cg:
            cap = cg["max"]
            if cap:
                cp = cg["current"] / cap * 100
                cg_line = (f"{_lvl(cp, 80, 90)} `{cg['unit']}` 服务 {_size(cg['current'])}"
                           f" / 上限 {_size(cap)}（{cp:.0f}%）")
            else:
                cg_line = f"⚪ `{cg['unit']}` 服务 {_size(cg['current'])}（无上限）"
            if cg.get("peak"):
                cg_line += f"，峰值 {_size(cg['peak'])}"
            if cg["oom_kill"]:
                cg_line += f"，⚠️ 已被 OOM 杀过 {cg['oom_kill']} 次"
            lines.append(cg_line)
        lines.append("")

    if snap.get("disks"):
        lines.append("**磁盘**")
        for d in snap["disks"]:
            # 跟 df 的 Use% 同口径：分母不含 root 保留块
            denom = d["used"] + d["free"]
            pct = d["used"] / denom * 100 if denom else 0.0
            lines.append(f"{_lvl(pct, 80, 90)} `{d['mount']}` {_bar(pct)} {pct:.0f}%"
                         f" · {_size(d['used'])} / {_size(d['total'])}，剩 {_size(d['free'])}")
        lines.append("")

    procs = snap.get("procs")
    if procs:
        n_claude = sum(1 for p in procs if p["comm"] == "claude")
        lines.append(f"**进程**（共 {len(procs)} 个，其中 claude {n_claude} 个）")
        top_mem = sorted(procs, key=lambda p: p["rss"], reverse=True)[:_TOP_N]
        lines.append("内存前 5：" + " · ".join(
            f"`{p['comm']}`({p['pid']}) {_size(p['rss'])}" for p in top_mem))
        top_cpu = [p for p in sorted(procs, key=lambda p: p["cpu"], reverse=True)[:_TOP_N]
                   if p["cpu"] >= 0.5]
        lines.append("CPU 前 5：" + (" · ".join(
            f"`{p['comm']}`({p['pid']}) {p['cpu']:.0f}%" for p in top_cpu) or "都很闲"))
    elif not snap.get("linux"):
        lines.append("_（非 Linux：只能看负载和根盘）_")

    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def get_report() -> str:
    try:
        return render(collect())
    except Exception as e:  # noqa: BLE001 —— 看板命令本身不能把 bot 带崩
        return f"❌ 读取服务器指标失败：{type(e).__name__}: {e}"
