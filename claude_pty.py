"""
通过 PTY 运行 claude 交互式 CLI（不走 --print），结构化事件靠 tail
~/.claude/projects/<encoded-cwd>/<session-id>.jsonl 拿到——绕开扒 TUI。

为什么这样设计：
  - --print 路径会丢掉真正交互式 Claude Code 的一些行为（slash 命令、permission
    UX 等）。
  - 但 TUI 渲染没法稳定解析。
  - 幸运的是 Claude Code 把每次 Anthropic API 响应都 append 到 JSONL，结构
    完整：thinking / text / tool_use / usage / stop_reason 一应俱全。
  - 所以让 claude 跑在 PTY 里（满足 TTY 要求），同时实时 tail JSONL 拿事件。

粒度：JSONL 是"每条 Anthropic 响应一行"，不是 token 级。即 text_delta 不能从这里
拿到。对纯文本回答，整段文本会在一条 assistant 行里一次性出现（约几秒）；对多
步工具调用，每个 tool_use / 中间 text 都各自落地，流式感不弱于 -p 模式。

接口：与 claude_runner.run_claude 完全一致，dispatcher 无感切换。
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import subprocess as sp
import termios
import time
from typing import Callable, Optional

from bot_config import CLAUDE_CLI, PERMISSION_MODE

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CLAUDE_CONFIG_PATH = os.path.expanduser("~/.claude.json")

# 与 claude_runner.py 保持语义一致的超时参数
IDLE_TIMEOUT = 300        # 第一条 assistant 之后又卡住的"空闲"阈值
STUCK_CHILD_TIMEOUT = 900  # 有子进程但 Claude 端持续无新输出
WALL_CLOCK_LIMIT = 3600   # 单轮 wall-clock 兜底
# 第一条 user/assistant 行都没落进 JSONL 就 spawn 后这么久 → 多半是输入被
# 拒了（slash 等）。这是 idle-since-last-event 维度，meta 事件会 reset。
# 历史调整：
#   90s （初版）→ 大 session resume 撞穿（389KB 都 fail）
#   180s（V2）  → 还不够，2MB session（8caf110a 那种）resume 后 meta 事件
#                 写完到 user 行落地之间间隔 >180s
#   600s（V3）  → 给到 10 分钟。slash 已被 `_escape_for_pty` 在源头处理掉，
#                 这里主要兜超大 session resume，从严没意义
NO_ASSISTANT_TIMEOUT = 600

# Watchdog: 主动检测 claude **client 端 hung**（HTTPS 连接开着但不发字节、CPU=0、
# TUI 不再 render、JSONL 也不再写）。三个 0 增长信号合起来给出 90s 的快速判定，
# 比 IDLE/NO_ASSISTANT 维度的纯 wall-clock 等敏感得多。生产撞过 2 次：
#   1. PID 24808 fresh session 初始化卡死 → 没人写 JSONL，180s 也没救
#   2. PID 46588 P2P resume 只发 2.5KB 到 anthropic 就停摆 → 5 min CPU 累计 5s
# 三个信号都靠 bot 端能直接观察，零侵入 claude。
WATCHDOG_INTERVAL = 30.0       # 每 30s 检查一次
WATCHDOG_MAX_STALL = 90.0      # 任何信号没动这么久 → 判 hung
WATCHDOG_CPU_MIN_INC = 0.5     # 30s 窗口里累计 CPU 增长少于 0.5s 视为 0 增长

# PTY / JSONL 轮询参数
_JSONL_POLL_INTERVAL = 0.2    # 200ms 拉一次 jsonl 增量
_NEW_SESSION_WAIT = 300.0     # 起新 session 时，最多等多久 jsonl 文件出现（MCP 冷启动 / 大 paste / URL detect 都可能 >90s）
_PTY_INIT_GRACE = 2.0         # 看到 TUI 第一字节后再等多久才发输入（MCP 启动期 stdin 会被吞）
_PTY_READY_TIMEOUT = 4.0      # 等 TUI 起来的硬上限
_DONE_QUIET_PERIOD = 1.2      # stop_reason=end_turn 后再静默这么久才算彻底完事
_PTY_TAIL_BUFFER = 8192       # PTY 输出最多留多少字节做错误诊断

# PTY 输出里出现这些字面 → 父 claude 在等 Anthropic API 重试，bot 端可以暴露
# 给用户（jsonl 不会写任何事件，只能从 TUI 输出抓）。pattern 用 7-bit ASCII，
# 不会被 ANSI 控制序列拆开。
_RATE_LIMIT_PATTERNS: tuple[tuple[bytes, str], ...] = (
    (b"rate_limit_error", "API 限流 (rate_limit_error)"),
    (b"overloaded_error", "API 过载 (overloaded_error)"),
    (b"Overloaded",       "API 过载 (Overloaded)"),
)
_PTY_WARNING_MIN_IDLE = 30.0  # JSONL 静默至少多久才扫 tail——避免历史 buffer 误报

# 用 ANSI bracketed paste 包裹用户输入，避免内嵌 \n 被 Claude Code 当成多次提交
_BRACKETED_PASTE_START = b"\x1b[200~"
_BRACKETED_PASTE_END = b"\x1b[201~"


# ────────────────── 工具函数 ──────────────────

def _encode_cwd_to_project_dir(cwd: str) -> str:
    """Claude Code 把绝对路径 '/foo/bar' 编码成目录名 '-foo-bar'。"""
    abs_cwd = os.path.abspath(cwd)
    return os.path.join(CLAUDE_PROJECTS_DIR, abs_cwd.replace("/", "-"))


def _snapshot_session_uuids(project_dir: str) -> set[str]:
    if not os.path.isdir(project_dir):
        return set()
    out = set()
    for f in os.listdir(project_dir):
        if f.endswith(".jsonl") and len(f) == 36 + 6:  # uuid + .jsonl
            out.add(f[:-6])
    return out


def _jsonl_birth_time(path: str) -> Optional[float]:
    """文件创建时间（macOS st_birthtime；Linux 没有则回退到 st_ctime）。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return getattr(st, "st_birthtime", st.st_ctime)


def _jsonl_first_user_content(path: str, max_lines: int = 16) -> Optional[str]:
    """读 jsonl 前若干行，返回第一条"真正用户消息"的 content 文本。
    跳过 tool_result 这类 user 事件——那是工具回写不是用户输入。
    返回 None 表示前 max_lines 行里还没出现真正的用户消息（不一定不是自己的，
    等下次 poll 再试）。OSError / JSON 错误一律吞掉——文件可能正在写。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "user":
                    continue
                msg = ev.get("message") or {}
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    text_parts: list[str] = []
                    has_non_text = False
                    for blk in content:
                        if not isinstance(blk, dict):
                            continue
                        btype = blk.get("type")
                        if btype == "text":
                            t = blk.get("text")
                            if isinstance(t, str):
                                text_parts.append(t)
                        elif btype:
                            has_non_text = True
                    if text_parts:
                        return "".join(text_parts)
                    if has_non_text:
                        # 整条事件只有 tool_result/image/etc — 不是用户输入，继续找
                        continue
                # 其他奇怪格式——保险起见跳过继续
                continue
    except OSError:
        return None
    return None


def _expected_match_prefix(message: str) -> Optional[str]:
    """从用户消息里挑一段稳定的前缀做 jsonl 匹配 key。
    太短或全空白时返回 None——并发 race 概率极低，没法匹配就走单候选默认接受路径。
    """
    if not message:
        return None
    stripped = message.strip()
    if len(stripped) < 6:
        return None
    # 截前 32 字符——避开 Claude Code 对前置 `/ ! #` 加空格的 escape 差异
    return stripped[:32]


# _jsonl_owns_message 的三态返回：
#   True  → 这个 jsonl 是本次 spawn 的（前缀匹配 / 单候选放行）
#   False → 是别的并发 spawn 写的，跳过
#   None  → user 事件还没写到 jsonl，等下次 poll 再说
def _jsonl_owns_message(
    path: str,
    expected_prefix: Optional[str],
    only_candidate: bool,
) -> Optional[bool]:
    if expected_prefix is None:
        # 没有可用前缀（消息太短）。单候选直接收，多候选无法分辨——保守也收最早那个。
        return True
    content = _jsonl_first_user_content(path)
    if content is None:
        # 文件已存在但还没写 user 事件。单候选场景下可以认；多候选必须等内容
        # 落地再判，否则可能错选。
        return True if only_candidate else None
    return expected_prefix in content


def _ensure_cwd_trusted(cwd: str) -> None:
    """预先把 cwd 标记为 trusted，避免 PTY 模式下 claude 弹"Is this a project
    you trust?" 对话框堵住整个会话。

    --print 模式 Claude Code 会自动跳过 trust dialog，但 PTY 交互模式不会。
    存储在 ~/.claude.json → projects[abs_cwd].hasTrustDialogAccepted=true。

    任何异常都吞掉——claude 自己处理 fallback 也比把这里炸了好。
    """
    if not os.path.isfile(CLAUDE_CONFIG_PATH):
        return
    abs_cwd = os.path.abspath(cwd)
    try:
        with open(CLAUDE_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(cfg, dict):
        return
    projects = cfg.setdefault("projects", {})
    if not isinstance(projects, dict):
        return
    proj = projects.get(abs_cwd)
    if isinstance(proj, dict) and proj.get("hasTrustDialogAccepted") is True:
        return
    # 新建或补字段。保留 claude 自己之后会塞的其他 key。
    new_proj = dict(proj) if isinstance(proj, dict) else {}
    new_proj["hasTrustDialogAccepted"] = True
    new_proj.setdefault("allowedTools", [])
    new_proj.setdefault("mcpContextUris", [])
    new_proj.setdefault("mcpServers", {})
    new_proj.setdefault("enabledMcpjsonServers", [])
    new_proj.setdefault("disabledMcpjsonServers", [])
    new_proj.setdefault("hasClaudeMdExternalIncludesApproved", False)
    new_proj.setdefault("hasClaudeMdExternalIncludesWarningShown", False)
    new_proj.setdefault("exampleFiles", [])
    new_proj.setdefault("projectOnboardingSeenCount", 0)
    projects[abs_cwd] = new_proj
    try:
        tmp = CLAUDE_CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CLAUDE_CONFIG_PATH)
    except OSError:
        return


def _has_children(pid: int) -> bool:
    try:
        return sp.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            timeout=5,
        ).returncode == 0
    except Exception:
        return False


def _get_proc_cpu_seconds(pid: int) -> float:
    """读 ps 拿进程累计 CPU 秒数（u+s time）。返回 -1.0 表示读失败。

    用于 watchdog 检测"claude 进程是否还在干本地活"。CPU 完全停不动 +
    JSONL 不写 + TUI 不输出 三个全 0 增长 90s = client 端 hung。
    """
    try:
        result = sp.run(
            ["ps", "-o", "time=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return -1.0
        s = result.stdout.strip()
        # 格式：'MMM:SS.ss' 或 'HH:MM:SS' （macOS ps），如 '0:05.44' 或 '1:31.53'
        parts = s.split(":")
        try:
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            if len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        except ValueError:
            return -1.0
        return -1.0
    except Exception:
        return -1.0


def _extract_text_from_content(content) -> str:
    """从 assistant message.content 抽所有 text 块拼成字符串。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", "") or "")
    return "".join(parts)


def _extract_tool_uses(content) -> list[tuple[str, dict]]:
    """从 assistant message.content 抽所有 tool_use → [(name, input)]"""
    if not isinstance(content, list):
        return []
    out: list[tuple[str, dict]] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            out.append((block.get("name", "") or "", block.get("input", {}) or {}))
    return out


async def _fire(cb, *args):
    if cb is None:
        return
    if asyncio.iscoroutinefunction(cb):
        await cb(*args)
    else:
        cb(*args)


# ────────────────── PTY 辅助 ──────────────────

def _set_winsize(fd: int, rows: int = 40, cols: int = 160):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except Exception:
        pass


def _set_fd_nonblocking(fd: int):
    """让 fd 进入 O_NONBLOCK，配合 loop.add_reader 用——读没数据立刻 EAGAIN 返回。"""
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


async def _drain_pty(
    master_fd: int,
    tail_buffer: bytearray,
    ready_event: Optional[asyncio.Event] = None,
):
    """非阻塞 drain PTY 输出 + 可选的"第一个字节到达"信号。

    单一 reader 全程跑——避免"撤旧 reader → 加新 reader"中间空窗导致 PTY
    缓冲撑爆 claude 写阻塞。

    **绝对不能**用阻塞 os.read + executor——claude 起死不写 PTY 时这个 worker
    会被永远占住，多个并发 run_claude 共存时撞到 ThreadPoolExecutor 池上限就
    会饿死整个 bot 的 asyncio loop（2026-05-15 生产撞过）。

    实现：fd 设 O_NONBLOCK + loop.add_reader 注册到 selector/kqueue，回调里
    os.read 立刻返回。零 executor 占用。
    """
    loop = asyncio.get_event_loop()
    finished = asyncio.Event()

    _set_fd_nonblocking(master_fd)

    def _on_readable():
        try:
            data = os.read(master_fd, 65536)
        except BlockingIOError:
            return  # 假信号，等下次
        except OSError:
            finished.set()
            return
        if not data:  # EOF
            finished.set()
            return
        tail_buffer.extend(data)
        if len(tail_buffer) > _PTY_TAIL_BUFFER:
            del tail_buffer[:-_PTY_TAIL_BUFFER]
        if ready_event is not None and not ready_event.is_set():
            ready_event.set()

    try:
        loop.add_reader(master_fd, _on_readable)
    except Exception:
        if ready_event is not None:
            ready_event.set()
        return

    try:
        await finished.wait()
    except asyncio.CancelledError:
        pass
    finally:
        try:
            loop.remove_reader(master_fd)
        except Exception:
            pass


def _scan_tail_for_warning(tail: bytes) -> Optional[str]:
    """从 PTY tail buffer 里找出当下最显眼的 API 异常字面。

    只匹配字面 token：claude TUI 的 ANSI 染色不会插在 `rate_limit_error` 中间。
    """
    for pat, label in _RATE_LIMIT_PATTERNS:
        if pat in tail:
            return label
    return None


async def _wait_for_ready(ready_event: asyncio.Event, timeout: float) -> bool:
    """等 _drain_pty 通过 ready_event 信号"看到第一个字节了"，或超时。

    取代旧的 _wait_pty_ready——后者会临时 add_reader / remove_reader，跟主
    drain_task 之间有时间窗口让 PTY 缓冲撑爆。
    """
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


# 白名单：真正想透传给 claude 内置 slash 的命令。其他 / 开头的会被前置空格降级
# 成普通文本，避免误触发 claude 内置（用户的"/pwd"通常是想让模型执行 pwd）。
_CLAUDE_BUILTIN_PASSTHROUGH = ("/compact",)


def _is_builtin_passthrough(message: str) -> bool:
    """消息是不是命中 _CLAUDE_BUILTIN_PASSTHROUGH 的精确命令（带参数也算）。

    `startswith` 直接对 tuple 会误中 `/compactfoo`，得严格匹配 cmd 本身或 cmd
    后面跟空白。
    """
    if not message:
        return False
    stripped = message.lstrip()
    for cmd in _CLAUDE_BUILTIN_PASSTHROUGH:
        if stripped == cmd or stripped.startswith(cmd + " ") or stripped.startswith(cmd + "\n"):
            return True
    return False


def _escape_for_pty(message: str) -> str:
    """把消息处理成 PTY 安全的格式，避免 Claude Code 把它当成内置控制操作。

    - 以 `/` 开头：claude 交互式会当成 slash command（/help /clear /pwd …）。bot
      用户的"/pwd"实际是想让模型执行 pwd，不是触发 claude 内置命令。前置一个空格
      让 claude 走普通输入路径。-p 模式没这问题，是 PTY 特有。
      例外：_CLAUDE_BUILTIN_PASSTHROUGH 里的命令透传，让 claude 真正执行（如 /compact）。
    - 以 `!` 开头：claude 会进 shell 模式（bash 单行）。如果用户真要 shell，会走
      bot 的 /exec 路径，所以这里也屏蔽。
    - 以 `#` 开头：claude 把它当作 memory 写入。同样屏蔽。
    """
    if not message:
        return message
    if _is_builtin_passthrough(message):
        return message
    first = message[0]
    if first in "/!#":
        return " " + message
    return message


async def _write_all_to_pty(
    master_fd: int,
    data: bytes,
    chunk: int = 512,
    inter_chunk_sleep: float = 0.005,
) -> None:
    """把整段 data 全部写到 PTY master fd。

    master_fd 在 _drain_pty 里被设成 O_NONBLOCK，单次 os.write 超过 PTY 输入
    buffer（macOS MAX_INPUT=1024 字节）时返回**部分写入字节数**，剩余 bytes
    被静默丢弃——大消息 paste 会丢掉 bracketed-paste 结束符 \\x1b[201~，
    claude TUI 永远等不到 paste 收尾。

    所以这里必须分块写 + EAGAIN 时 yield 让 PTY reader（claude TUI 端）
    把 stdin 消化掉再继续。
    """
    view = memoryview(data)
    pos = 0
    backoff = 0.005
    total = len(view)
    while pos < total:
        end = min(pos + chunk, total)
        try:
            n = os.write(master_fd, view[pos:end])
        except BlockingIOError:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 0.05)
            continue
        if n <= 0:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 0.05)
            continue
        pos += n
        backoff = 0.005
        if pos < total:
            await asyncio.sleep(inter_chunk_sleep)


async def _send_user_input(master_fd: int, message: str):
    """把用户消息送进 Claude Code 的输入框并提交。

    实测：bracketed-paste 结束序列后立刻跟 \\r 不会被 Claude Code 当成提交——
    paste 处理还没收尾就被 \\r 撞上去了。必须拆成两次写入 + 几十毫秒间隔。
    长 paste（thread context 注入后动辄几 KB）需要更长的 settle，否则 \\r
    撞在 paste 解析途中会被吞或被当成 paste 内的换行。
    """
    safe = _escape_for_pty(message)
    encoded = safe.encode("utf-8")
    if b"\n" in encoded:
        # 多行：用 bracketed paste 保护内部换行不被当作多次提交
        await _write_all_to_pty(master_fd, _BRACKETED_PASTE_START)
        await _write_all_to_pty(master_fd, encoded)
        await _write_all_to_pty(master_fd, _BRACKETED_PASTE_END)
    else:
        await _write_all_to_pty(master_fd, encoded)
    # 给 Claude Code 时间消化 paste 序列 / readline 缓冲。长消息按 256B/10ms
    # 加码，封顶 ~450ms——长 paste 比短 paste 需要更多解析时间。
    settle = 0.12 + min(len(encoded) // 256, 32) * 0.01
    await asyncio.sleep(settle)
    await _write_all_to_pty(master_fd, b"\r")


# ────────────────── 主入口 ──────────────────

async def run_claude(
    message: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    permission_mode: Optional[str] = None,
    on_text_chunk: Optional[Callable[[str], None]] = None,
    on_tool_use: Optional[Callable[[str, dict], None]] = None,
    on_process_start: Optional[Callable[[asyncio.subprocess.Process], None]] = None,
    on_usage: Optional[Callable[[dict], None]] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
    append_system_prompt: Optional[str] = None,
) -> tuple[str, Optional[str], bool]:
    """drop-in 替代 claude_runner.run_claude。内部走 PTY + tail JSONL。

    Returns:
        (full_response_text, new_session_id, used_fresh_session_fallback)
    """

    async def _run_once(active_session_id: Optional[str]):
        run_cwd = cwd or os.path.expanduser("~")
        # 关键：预先把 cwd 标为 trusted，否则 PTY 模式 claude 会弹对话框死等
        _ensure_cwd_trusted(run_cwd)
        project_dir = _encode_cwd_to_project_dir(run_cwd)
        os.makedirs(project_dir, exist_ok=True)

        # ── 准备 jsonl 起点 ──────────────────────────────
        if active_session_id:
            jsonl_path: Optional[str] = os.path.join(
                project_dir, f"{active_session_id}.jsonl"
            )
            start_offset = (
                os.path.getsize(jsonl_path) if os.path.isfile(jsonl_path) else 0
            )
            before_uuids: set[str] = set()
        else:
            jsonl_path = None
            start_offset = 0
            # before_uuids 真正拍照延后到 spawn proc 之后——缩短"拍照→spawn"
            # 之间被别的并发 task 创建新 jsonl 的 race window。
            before_uuids = set()

        # ── 构造命令 ─────────────────────────────────────
        cmd = [CLAUDE_CLI]
        if active_session_id:
            cmd += ["--resume", active_session_id]
        if model:
            cmd += ["--model", model]
        if append_system_prompt:
            cmd += ["--append-system-prompt", append_system_prompt]
        cmd += ["--permission-mode", permission_mode or PERMISSION_MODE]

        # ── 起 PTY ────────────────────────────────────────
        master_fd, slave_fd = pty.openpty()
        _set_winsize(slave_fd)

        proc: Optional[asyncio.subprocess.Process] = None
        drain_task: Optional[asyncio.Task] = None
        tail_buffer = bytearray()
        full_text = ""
        new_session_id = active_session_id
        last_usage: dict = {}
        early_return: Optional[tuple] = None  # 设上就是非正常结束，跳过 usage 推送

        # 透传给 claude 内置 slash 命令（如 /compact）走的是 claude 自己的处理，
        # 大上下文压缩会发巨型 API 请求，期间 CPU/PTY/JSONL 三信号都可能长时间
        # 0 增长，且 NO_ASSISTANT_TIMEOUT/IDLE_TIMEOUT 都会撞——全部关掉，只
        # 留 WALL_CLOCK_LIMIT 兜底。
        is_builtin_slash = _is_builtin_passthrough(message)

        try:
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            env.setdefault("TERM", "xterm-256color")
            env["COLUMNS"] = "160"
            env["LINES"] = "40"

            # spawn_at 紧贴 spawn 动作记录——用于过滤 birth_time 早于自己 spawn
            # 的 jsonl。wall-clock（time.time），与 st_birthtime/st_ctime 对齐。
            spawn_at = time.time()

            # exec 完，slave 立刻关——否则父进程 fd 残留导致 EOF 永远不到
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=run_cwd,
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                )
            finally:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass

            # 拍 before_uuids 延后到 spawn 之后——缩短"拍照→spawn"之间被
            # 并发 task 抢先创建 jsonl 的 race window。新 spawn 出来的 claude
            # 写 jsonl 至少要几百毫秒后才发生（node 起 + init），所以这里赶在
            # 它写之前拍一般来得及。最后还要 birth_time >= spawn_at 二次过滤。
            if active_session_id is None:
                before_uuids = _snapshot_session_uuids(project_dir)

            await _fire(on_process_start, proc)

            loop = asyncio.get_event_loop()
            start_time = loop.time()

            # PTY drain 后台任务：**马上**挂，全程跑，避免空窗让 PTY 缓冲撑爆。
            # ready_event 在第一个字节到达时由 drain 内部 set——取代之前的两段式
            # （先 _wait_pty_ready 撤掉 reader，再 _drain_pty 加 reader），中间
            # 0.6s 空窗已经在生产上把单一并发 claude 整死过。
            ready_event = asyncio.Event()
            drain_task = asyncio.create_task(
                _drain_pty(master_fd, tail_buffer, ready_event)
            )

            # ── 等 TUI 起来 ──────────────────────────────
            ready = await _wait_for_ready(ready_event, _PTY_READY_TIMEOUT)
            if not ready and proc.returncode is not None:
                early_return = ("", None, proc.returncode or -1, _decode_tail(tail_buffer))
            else:
                # TUI 余下初始化时间（drain 仍在持续吃 PTY 输出，不会撑爆）
                await asyncio.sleep(_PTY_INIT_GRACE)

                # ── 发送用户消息 ──────────────────────────
                try:
                    await _send_user_input(master_fd, message)
                except (OSError, BrokenPipeError):
                    early_return = (
                        "", None,
                        proc.returncode or -1,
                        _decode_tail(tail_buffer) or "PTY broken on send",
                    )

            # ── 新 session：等 jsonl 文件出现 ─────────────
            # 并发 spawn 时，所有 task 看到的 new_uuids 集合可能都包含别的
            # task 的 jsonl——按 mtime max 选会互相串台。这里改成：
            #   1. 只接受 birth_time >= spawn_at 的 jsonl（spawn 之后才创建）
            #   2. 按 birth_time 升序遍历候选，用 jsonl 第一条 user 事件的
            #      message.content 是否包含 expected_prefix 来鉴权
            #   3. 三态结果（True/False/None）让"user 事件未写入"的候选保留
            #      到下次 poll，避免误拒
            if early_return is None and jsonl_path is None:
                expected_prefix = _expected_match_prefix(message)
                rejected: set[str] = set()
                deadline = loop.time() + _NEW_SESSION_WAIT
                while loop.time() < deadline:
                    new_uuids = (
                        _snapshot_session_uuids(project_dir)
                        - before_uuids
                        - rejected
                    )
                    # 收集合规候选（birth_time >= spawn_at - 容差 0.5s）
                    candidates: list[tuple[float, str, str]] = []
                    for u in new_uuids:
                        p = os.path.join(project_dir, f"{u}.jsonl")
                        birth = _jsonl_birth_time(p)
                        if birth is None:
                            continue
                        if birth + 0.5 < spawn_at:
                            # spawn 之前就存在的，肯定不是自己——拉黑省得反复检
                            rejected.add(u)
                            continue
                        candidates.append((birth, u, p))
                    candidates.sort()  # birth 升序
                    only_candidate = len(candidates) == 1
                    chosen_path: Optional[tuple[str, str]] = None
                    any_pending = False
                    for _birth, u, p in candidates:
                        verdict = _jsonl_owns_message(
                            p, expected_prefix, only_candidate
                        )
                        if verdict is True:
                            chosen_path = (u, p)
                            break
                        if verdict is False:
                            rejected.add(u)
                        else:  # None：内容还没写到 user 行
                            any_pending = True
                    if chosen_path is not None:
                        u, p = chosen_path
                        active_session_id = u
                        new_session_id = u
                        jsonl_path = p
                        break
                    if proc.returncode is not None:
                        break
                    # 没有"被接受"的候选——下次 poll；any_pending 提示我们至少
                    # 有一个文件存在但还没写 user 行，正常情况下马上就写完
                    _ = any_pending
                    await asyncio.sleep(_JSONL_POLL_INTERVAL)
                if jsonl_path is None:
                    rc = proc.returncode if proc.returncode is not None else -1
                    detail = _decode_tail(tail_buffer)
                    early_return = ("", None, rc, f"new session jsonl never appeared. {detail}")

            # ── 主循环：tail JSONL ───────────────────────
            if early_return is None:
                last_stop_reason: Optional[str] = None
                offset = start_offset
                leftover = b""
                last_event_time = loop.time()
                idle_seconds = 0.0
                announce_user_seen = False        # JSONL 出现 user 行 = 输入已被 claude 接收
                announce_assistant_started = False
                last_warning: Optional[str] = None  # 当前向 dispatcher 暴露的 PTY warning
                pending_synthetic_assistant = False  # 上一条 user 是 isMeta=True → 下一条 assistant 是 resume 收尾 dummy
                synthetic_cycle_seen = False         # 本轮是否经历过 orphan-resume 修复对话——用于事后判断要不要 fresh 重试

                # Watchdog 状态：跟踪三个独立的"claude 还活着"信号
                last_progress_time = loop.time()   # 任一信号有进展就刷新
                wd_last_check = loop.time()
                wd_last_cpu = _get_proc_cpu_seconds(proc.pid)
                wd_last_tail_size = len(tail_buffer)
                wd_progressed_in_window = False    # 本窗口内 JSONL 有事件吗

                while True:
                    now = loop.time()
                    if now - start_time >= WALL_CLOCK_LIMIT:
                        raise RuntimeError(
                            f"Claude 单轮执行超过 wall-clock 最终上限"
                            f"（{WALL_CLOCK_LIMIT}秒），已终止进程。"
                        )

                    if (
                        last_stop_reason == "end_turn"
                        and now - last_event_time >= _DONE_QUIET_PERIOD
                    ):
                        break

                    # Watchdog: CPU + tail + JSONL 三信号 0 增长 → client 端 hung
                    # /compact 类内置命令期间会发巨型 API 请求，CPU/PTY/JSONL 都可能
                    # 长时间静止——这里跳过 watchdog，靠 WALL_CLOCK_LIMIT 兜底
                    if not is_builtin_slash and now - wd_last_check >= WATCHDOG_INTERVAL:
                        cur_cpu = _get_proc_cpu_seconds(proc.pid)
                        cur_tail_size = len(tail_buffer)
                        cpu_advanced = (
                            cur_cpu >= 0
                            and wd_last_cpu >= 0
                            and (cur_cpu - wd_last_cpu) > WATCHDOG_CPU_MIN_INC
                        )
                        tail_advanced = cur_tail_size > wd_last_tail_size
                        if cpu_advanced or tail_advanced or wd_progressed_in_window:
                            last_progress_time = now
                        wd_last_cpu = cur_cpu
                        wd_last_tail_size = cur_tail_size
                        wd_last_check = now
                        wd_progressed_in_window = False
                        if now - last_progress_time >= WATCHDOG_MAX_STALL:
                            raise RuntimeError(
                                f"Claude 客户端疑似 hung：CPU/PTY/JSONL 三信号 "
                                f"{WATCHDOG_MAX_STALL:.0f}s 全 0 增长，已终止进程。"
                                f"常见原因：API request 发到一半 client 端 stuck、"
                                f"内部 promise/state machine 死锁、TLS 池握手挂掉。"
                            )

                    events: list[dict] = []
                    try:
                        sz = os.path.getsize(jsonl_path)
                    except OSError:
                        sz = offset
                    if sz > offset:
                        try:
                            with open(jsonl_path, "rb") as f:
                                f.seek(offset)
                                chunk = f.read(sz - offset)
                            offset = sz
                        except OSError:
                            chunk = b""
                        data = leftover + chunk
                        lines = data.split(b"\n")
                        leftover = lines.pop() if lines else b""
                        for raw in lines:
                            raw = raw.strip()
                            if not raw:
                                continue
                            try:
                                events.append(
                                    json.loads(raw.decode("utf-8", errors="replace"))
                                )
                            except json.JSONDecodeError:
                                continue

                    progressed = False
                    for ev in events:
                        progressed = True
                        wd_progressed_in_window = True
                        last_event_time = loop.time()
                        ev_type = ev.get("type")

                        if ev_type == "assistant":
                            msg = ev.get("message", {}) or {}
                            content = msg.get("content", [])
                            # Claude CLI 在 --resume 一个被中断的 session 时（上一轮
                            # tool_use/tool_result 之后没收到 end_turn 就被杀），会
                            # 自己注入一对 housekeeping 事件给上轮收尾：
                            #   USER  isMeta=True  "Continue from where you left off."
                            #   ASSIST stop_sequence=''  "No response requested."
                            # 这对事件不是对用户输入的真实响应——必须过滤掉，否则
                            # full_text 会被这条 "No response requested." 污染，
                            # dispatcher 会把它当成回复贴给用户（实际看到 ⏱ 10s 那张卡）。
                            if pending_synthetic_assistant:
                                pending_synthetic_assistant = False
                                synthetic_cycle_seen = True
                                # 不写 full_text、不发 tool/text chunk、不刷 stop_reason
                                continue
                            announce_assistant_started = True
                            # tool_use 先发，再发 text：dispatcher 期待 tool 行在文本上方
                            for tname, tinp in _extract_tool_uses(content):
                                await _fire(on_tool_use, tname, tinp)
                            text = _extract_text_from_content(content)
                            if text:
                                full_text += text
                                await _fire(on_text_chunk, text)
                            u = msg.get("usage")
                            if isinstance(u, dict) and u:
                                last_usage = dict(u)
                            sr = msg.get("stop_reason")
                            if sr:
                                last_stop_reason = sr
                        elif ev_type == "user":
                            # isMeta=True 是 Claude CLI 自己注入的（resume 收尾）—— 不算
                            # 真实用户输入，下一条 assistant 也要跟着扔掉
                            if ev.get("isMeta") is True:
                                pending_synthetic_assistant = True
                            else:
                                # 用户消息已被 claude 写入 JSONL → 输入肯定到位了，
                                # 后面 claude 在思考也好、调用工具也好，都是 IDLE_TIMEOUT 的事
                                announce_user_seen = True
                        elif ev_type == "system":
                            sid = ev.get("sessionId") or ev.get("session_id")
                            if sid:
                                new_session_id = sid

                    if proc.returncode is not None and not progressed:
                        break

                    if progressed:
                        idle_seconds = 0.0
                        if last_warning is not None:
                            last_warning = None
                            await _fire(on_status, "clear", "")
                    else:
                        await asyncio.sleep(_JSONL_POLL_INTERVAL)
                        idle_seconds += _JSONL_POLL_INTERVAL
                        # JSONL 静默 ≥ _PTY_WARNING_MIN_IDLE 时扫 tail buffer，
                        # 看 claude TUI 是不是在打 "rate_limit_error / Overloaded"
                        # 之类的 API 退避提示——这些事件不会写 JSONL，但 PTY 上能看到
                        if (
                            announce_assistant_started
                            and idle_seconds >= _PTY_WARNING_MIN_IDLE
                        ):
                            warning = _scan_tail_for_warning(bytes(tail_buffer))
                            if warning != last_warning:
                                last_warning = warning
                                await _fire(
                                    on_status,
                                    "rate_limit" if warning else "clear",
                                    warning or "",
                                )
                        # /compact 类内置 slash 命令：claude 内部消化、可能根本不写
                        # user/assistant 事件，也可能压缩好几分钟。所有 idle 维度
                        # 阈值都关掉，只靠 WALL_CLOCK_LIMIT 兜底
                        if is_builtin_slash:
                            pass
                        elif not announce_user_seen and not announce_assistant_started:
                            # JSONL 完全没动（除了开机的 system 事件）→ 输入多半被 claude
                            # 输入框拒了（slash 等）。90s 兜底，比 IDLE_TIMEOUT 短得多
                            threshold = NO_ASSISTANT_TIMEOUT
                            if idle_seconds >= threshold:
                                raise RuntimeError(
                                    f"Claude 启动后 {threshold}s 仍未写入任何 user/assistant 事件——"
                                    f"消息可能被 PTY 输入框拒绝（例如未转义的 slash 命令）。"
                                )
                        elif not announce_assistant_started:
                            # user 行已经在 JSONL 里 = 输入被接收，claude 在思考。
                            # 长上下文（数百 KB JSONL + Opus 4.7）的 first-token 时间能
                            # 轻松到 1-2 分钟。给到 IDLE_TIMEOUT 让它充分跑
                            threshold = IDLE_TIMEOUT
                            if idle_seconds >= threshold:
                                raise RuntimeError(
                                    f"Claude 已接收输入但 {threshold}s 内没产出 assistant 响应，"
                                    f"已终止进程"
                                )
                        else:
                            has_kids = _has_children(proc.pid)
                            threshold = STUCK_CHILD_TIMEOUT if has_kids else IDLE_TIMEOUT
                            if idle_seconds >= threshold:
                                if has_kids:
                                    raise RuntimeError(
                                        f"Claude 执行超时（{threshold}秒有子进程但 Claude 端无任何新输出），已终止进程。"
                                        f"常见原因：tail -f / watch / npm run dev 等永不退出的阻塞命令。"
                                    )
                                raise RuntimeError(
                                    f"Claude 执行超时（{threshold}秒无输出且无活跃子进程），已终止进程"
                                )
        finally:
            # 顺序：先 cancel drain（停止读 fd） → 再杀进程 → 最后关 master_fd
            if drain_task is not None and not drain_task.done():
                drain_task.cancel()
                try:
                    await asyncio.wait_for(drain_task, timeout=0.5)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
            if proc is not None:
                await _ensure_terminated(proc)
            try:
                os.close(master_fd)
            except OSError:
                pass

        # ── 走完正常路径 → 收尾 ──────────────────────────
        if early_return is not None:
            return early_return

        # 整理 usage（与 -p 路径对齐：取 iterations 最后一项）
        if last_usage:
            iterations = last_usage.get("iterations") or []
            if isinstance(iterations, list) and iterations and isinstance(iterations[-1], dict):
                usage_out = dict(iterations[-1])
            else:
                usage_out = dict(last_usage)
            usage_out.pop("iterations", None)
            if usage_out:
                await _fire(on_usage, usage_out)

        rc = proc.returncode if proc and proc.returncode is not None else 0
        stderr_text = ""
        if rc != 0 and not full_text:
            stderr_text = _decode_tail(tail_buffer)
        # synthetic_cycle_seen + 空 full_text = 这次 resume 撞上 orphan session，
        # Claude CLI 自己跑完了 housekeeping 修复对话就退了，根本没处理用户的真实输入。
        # 用一个 sentinel stderr 透出去，让外层 retry fresh session（哪怕 rc==0）。
        if synthetic_cycle_seen and not full_text and not stderr_text:
            stderr_text = "_orphan_resume_no_response"
        return full_text.strip(), new_session_id, rc, stderr_text

    # ── resume 失败兜底（与 -p 版本一致）────────────────
    # watchdog hung 抛出来的 RuntimeError 也算 resume 失败的一种：旧 session 在
    # Claude 服务端的 conversation state 大概率已经 dirty（API request 发到一半
    # 卡死、内部状态机锁住），同一个 id 再 resume 必然撞同一个坑。这里必须自动
    # fresh 一次才能解套，否则 dispatcher 层 auto-retry 也救不回来。
    _WATCHDOG_HUNG_MARKER = "客户端疑似 hung"
    try:
        final_text, new_session_id, rc, stderr_text = await _run_once(session_id)
        watchdog_hung_on_resume = False
    except RuntimeError as exc:
        if session_id and _WATCHDOG_HUNG_MARKER in str(exc):
            print(
                f"[run_claude_pty] resume hung by watchdog, fresh-fallback: {exc}",
                flush=True,
            )
            final_text = ""
            new_session_id = None
            rc = -1
            stderr_text = ""
            watchdog_hung_on_resume = True
        else:
            raise

    used_fresh_session_fallback = False

    orphan_resume = stderr_text == "_orphan_resume_no_response"
    if session_id and (
        watchdog_hung_on_resume
        or (rc is not None and rc > 0 and not stderr_text and not final_text)
        or orphan_resume
    ):
        if watchdog_hung_on_resume:
            reason = "hung-by-watchdog"
        elif orphan_resume:
            reason = "orphaned"
        else:
            reason = "failed without stderr"
        print(
            f"[run_claude_pty] resume {reason}, retrying fresh session",
            flush=True,
        )
        final_text, new_session_id, rc, stderr_text = await _run_once(None)
        used_fresh_session_fallback = True

    if rc != 0:
        detail = stderr_text or "no stderr"
        if final_text:
            return final_text, new_session_id, used_fresh_session_fallback
        raise RuntimeError(f"claude (pty) exited with code {rc}: {detail}")

    return final_text, new_session_id, used_fresh_session_fallback


# ────────────────── 收尾辅助 ──────────────────

def _signal_group(pid: int, sig: int) -> bool:
    """给 pid 所在进程组发信号。spawn 时用了 start_new_session=True，
    proc 是 pgid leader——只杀 leader 时 claude 起的 MCP server / hook 子进程
    会变成孤儿继续跑（生产撞过 PID 63320 SIGTERM 不死的现象）。
    """
    try:
        os.killpg(os.getpgid(pid), sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


async def _ensure_terminated(proc: asyncio.subprocess.Process):
    if proc.returncode is not None:
        return
    if not _signal_group(proc.pid, signal.SIGTERM):
        # pgid 拿不到（极少见，例如 pid 已经被 reaper 回收），退回单 PID 杀
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
        return
    except asyncio.TimeoutError:
        pass
    if not _signal_group(proc.pid, signal.SIGKILL):
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pass


def _decode_tail(buf: bytearray) -> str:
    if not buf:
        return ""
    return bytes(buf[-_PTY_TAIL_BUFFER:]).decode("utf-8", errors="replace").strip()
