"""本地调用 Apache Maka (Incubating) CLI 的统一入口。

    maka run - --cwd <dir> [--connection <slug>] [--model <id>] [--thinking <lvl>]
              [--timeout <sec>] [--max-steps <n>] [--yolo] [--resume <session-id>]

与前四个后端最大的不同：**maka run 完全没有流式输出**。它把整轮 turn 的事件写进
Runtime Event Log（<workspace>/runtime.sqlite），成功结束时才把 outcome.finalOutput
一次性 print 到 stdout（见上游 packages/cli/src/run-command-core.ts）。所以本 runner：

  * 不做增量 on_text_chunk：整轮结束后一次性返回 full_text，卡片由 dispatcher 收尾渲染；
  * 没有工具事件可播报（on_tool_use / on_usage / on_status 都不会被调用）；
  * 判活只能靠 wall-clock，不能像 grok/claude 那样用"多久没输出"——maka 正常工作时
    stdout 也是一片空白，idle 检测会误杀。

prompt 走 stdin（`maka run -`）：既避开 argv 长度上限，也躲开"stdin 不是 TTY 时
maka 会把 stdin 当额外上下文拼进 prompt"这个坑。

会话：maka run 不打印 session id，只能事后从 runtime.sqlite 的 session_metadata 里
认领——跑前快照 session_id 集合，跑完取「新增 ∩ cwd 匹配」，恰好一条才认。⚠️ 不能靠
会话名区分：maka 用**拼好的 prompt 首行前 42 字符**当名字，而 cc-lark 会把 Lark 前言
拼在最前面，于是所有话题的 session 都叫同一个名字（实测）。认领到的 id 下一轮用
`--resume` 续，续不上就退回新会话（used_fresh_session_fallback=True）。

模型连接：maka 的连接目录在 <workspace>/connection-catalog.json，凭证在
credential-vault.json。首次启动时 runtime-host 会 bootstrap：永远种一条免费的
`opencode-free`，另外按环境变量种一条——DEEPSEEK_API_KEY(+DEEPSEEK_BASE_URL) /
ANTHROPIC_API_KEY / OPENAI_API_KEY 三者取第一个非空（见上游
packages/runtime-host/src/server/bootstrap-runtime-policy.ts）。本 runner 就是靠
这条路把 key 喂进去的：api_key + api_key_env(默认 DEEPSEEK_API_KEY) +
base_url + base_url_env(默认 DEEPSEEK_BASE_URL)。⚠️ bootstrap 只在连接目录为空时
生效，之后改 env 不会重新种。

MCP：maka 给 stdio MCP 子进程的环境是**白名单**（PATH/HOME/USER/... + LC_*/XDG_*
+ mcp.json 里显式写的 env），不像 grok 那样整体继承父进程 env，所以 cc-lark 运行时
MCP（wake_me_in / dispatch_task）暂未接到本后端上。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import sys
from typing import Callable, Optional

from bot_config import PERMISSION_MODE, resolve_claude_wall_clock_limit
from claude_runner import _fire_callback, is_fatal_error_text

# maka 无流式输出，"多久没输出"没有意义：这里的超时是整轮 wall-clock 上限。
# 实际生效值 = min(profile 的 idle_timeout_sec, 全局 CLAUDE_WALL_CLOCK_LIMIT_SEC)。
DEFAULT_WALL_LIMIT = 1800
# 留一点余量让 maka 自己的 --timeout 先到期（它会优雅 stop 掉 session 并写完事件），
# 我们的硬杀在它之后，这样 runtime.sqlite 不会留下半截状态。
_GRACE_SEC = 20

# maka run --thinking 认的档位；cc-lark 的 none 映射到 off
MAKA_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "default")
_THINKING_ALIASES = {"none": "off", "": ""}

# 视为"这条 session 续不上了"的 stderr 特征 → 退回新会话重跑一次
_RESUME_BROKEN_PATTERNS = (
    "requires --yolo",
    "cannot resume",
    "externally isolated",
    "no compatible",
    "not found",
    "unknown session",
    "incompatible",
)

# 配置类错误（没连接 / 没模型 / key 不对）——重跑一遍也是同样结果，不要伪装成可续跑
_SETUP_ERROR_PATTERNS = (
    "task is not ready",
    "model_empty_model_list",
    "repair connection",
    "no model connection",
    "missing prompt input",
)


def resolve_maka_bin(configured: Optional[str] = None) -> str:
    if configured:
        return os.path.expanduser(configured)
    found = shutil.which("maka")
    if found:
        return found
    return "maka"


def resolve_maka_workspace_root(configured: Optional[str] = None) -> str:
    """默认与上游 resolveMakaWorkspaceRoot 一致（profile 名 "Maka"，workspace "default"）。"""
    if configured:
        return os.path.expanduser(configured)
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/Maka")
    elif sys.platform == "win32":
        base = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~/AppData/Roaming"), "Maka")
    else:
        xdg = os.getenv("XDG_CONFIG_HOME") or ""
        base = os.path.join(xdg if os.path.isabs(xdg) else os.path.expanduser("~/.config"), "Maka")
    return os.path.join(base, "workspaces", "default")


def _normalize_thinking(effort: Optional[str]) -> Optional[str]:
    e = (effort or "").strip().lower()
    e = _THINKING_ALIASES.get(e, e)
    if not e:
        return None
    if e not in MAKA_THINKING_LEVELS:
        raise ValueError(
            f"invalid Maka thinking level {e!r}; expected one of {list(MAKA_THINKING_LEVELS)}"
        )
    return e


def _wants_full_access(permission_mode: Optional[str], dangerous_skip: bool) -> bool:
    """maka 只有一个开关：--yolo（文件+网络全放开）。其余模式一律留在沙箱里。

    非交互模式下 maka 不会向用户请求扩权（会直接 deny 并以 rc=1 结束），所以
    bot 场景默认必须给 --yolo，否则任何写文件/联网的任务都会失败。
    """
    mode = (permission_mode or PERMISSION_MODE or "").strip()
    if mode in {"bypassPermissions", "acceptEdits", "auto", "dontAsk", "yolo"}:
        return True
    if mode in {"default", "plan", "ask", "sandbox"}:
        return False
    return bool(dangerous_skip)


def _session_name_for(prompt: str) -> str:
    """复刻上游 makaRunSessionName：prompt 首行前 42 个字符，空白折叠后 trim。"""
    first_line = (prompt or "").splitlines()[0] if (prompt or "").strip() else ""
    return re.sub(r"\s+", " ", first_line[:42]).strip()


def _session_ids_snapshot(workspace_root: str) -> Optional[set[str]]:
    """跑前快照全部 session_id。返回 None = 读不到库（认领时退化为按 cwd 取最新）。

    早先用「max(created_at) 水位线 + 会话名」认领，在真实 bot 里失效：cc-lark 会把
    Lark 前言（append_system_prompt）拼在 prompt 最前面，而 maka 用**拼好的 prompt
    首行前 42 字符**当会话名，于是所有话题的 session 全叫同一个名字，名字这一维不再
    有区分度。改成集合差集：跑完只认「本轮新出现且 cwd 对得上」的那一条。
    """
    db = os.path.join(workspace_root, "runtime.sqlite")
    if not os.path.exists(db):
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("select session_id from session_metadata").fetchall()
        finally:
            con.close()
        return {r[0] for r in rows}
    except (sqlite3.Error, OSError):
        return None


def _claim_session_id(
    workspace_root: str, cwd: str, before: Optional[set[str]]
) -> Optional[str]:
    """认领本轮新建的 session = 「跑后新增 ∩ cwd 匹配」里恰好一条的那个。

    maka 把 cwd realpath 之后存下来（/tmp → /private/tmp），所以两边都 realpath 再比。
    新增里同 cwd 有多条（同目录并发跑了两个话题）就**谁都不认**：下一轮开新会话最多
    丢一轮上下文，认错了却会把两个话题的历史串到一起，后者严重得多。
    """
    db = os.path.join(workspace_root, "runtime.sqlite")
    if not os.path.exists(db):
        return None
    try:
        target_cwd = os.path.realpath(cwd)
    except OSError:
        target_cwd = cwd
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "select session_id, created_at, payload_json from session_metadata "
                "order by created_at desc limit 200"
            ).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return None

    # payload_json 里的 cwd 是权威值；只做子串判断，避免为一次认领反序列化整个 payload
    same_cwd = [r for r in rows if f'"cwd":"{target_cwd}"' in (r[2] or "")]
    if before is None:
        # 跑前没读到库（首次运行等）：只能退回"该 cwd 下最新的一条"
        return same_cwd[0][0] if same_cwd else None
    fresh = [r[0] for r in same_cwd if r[0] not in before]
    if len(fresh) == 1:
        return fresh[0]
    if len(fresh) > 1:
        print(
            f"[run_maka] 同一 cwd 本轮新增 {len(fresh)} 条 session，无法确定归属，"
            f"放弃认领（下一轮开新会话）: cwd={target_cwd}",
            flush=True,
        )
    return None


async def run_maka(
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
    extra_env: Optional[dict] = None,
    effort: Optional[str] = None,
    maka_bin: Optional[str] = None,
    workspace_root: Optional[str] = None,
    connection: Optional[str] = None,
    api_key: Optional[str] = None,
    api_key_env: Optional[str] = None,
    base_url: Optional[str] = None,
    base_url_env: Optional[str] = None,
    max_steps: int = 0,
    dangerously_skip_permissions: bool = True,
    idle_timeout_sec: int = DEFAULT_WALL_LIMIT,
) -> tuple[str, Optional[str], bool]:
    """返回 (full_text, session_id, used_fresh_session_fallback)。"""
    # maka run 不吐工具/用量事件，没东西可播报；on_status 也不能借用——dispatcher 的
    # on_status 是「PTY 抓到 API 限流/过载」告警通道（pty_warning），只有 level="clear"
    # 才会消除，拿它发进度提示会在收尾卡片上留一条永不消失的 🚦 假告警（实测过）。
    del on_tool_use, on_usage, on_status

    thinking = _normalize_thinking(effort)
    full_access = _wants_full_access(permission_mode, dangerously_skip_permissions)
    ws_root = resolve_maka_workspace_root(workspace_root)
    prompt = f"{append_system_prompt}\n\n{message}" if append_system_prompt else message

    # 两个上限取小：profile 的 MAKA_IDLE_TIMEOUT_SEC 和全局 CLAUDE_WALL_CLOCK_LIMIT_SEC。
    # 后者默认 3600 且几乎不会是 0，直接"有值就用它"会让 MAKA_IDLE_TIMEOUT_SEC 成为死配置。
    limits = [
        float(v)
        for v in (resolve_claude_wall_clock_limit(extra_env), idle_timeout_sec)
        if v and float(v) > 0
    ]
    wall_limit = min(limits) if limits else float(DEFAULT_WALL_LIMIT)
    # 让 maka 自己先超时收尾，我们的硬杀留在后面
    maka_timeout = max(30, int(wall_limit) - _GRACE_SEC)

    async def _run_once(
        active_session_id: Optional[str],
    ) -> tuple[str, Optional[str], Optional[int], str]:
        cmd = [
            resolve_maka_bin(maka_bin),
            "run",
            "-",  # prompt 从 stdin 读全文
            "--cwd", cwd or os.path.expanduser("~"),
            "--timeout", str(maka_timeout),
        ]
        if active_session_id:
            cmd += ["--resume", active_session_id]
        else:
            # 续会话时连接/模型跟随该 session 自己的设置，只有新会话才由我们指定
            if connection:
                cmd += ["--connection", connection]
            if model:
                cmd += ["--model", model]
        if thinking:
            cmd += ["--thinking", thinking]
        if max_steps > 0:
            cmd += ["--max-steps", str(max_steps)]
        if full_access:
            cmd += ["--yolo"]

        env = os.environ.copy()
        env["CC_LARK_MIRROR_OFF"] = "1"
        if api_key:
            env[(api_key_env or "DEEPSEEK_API_KEY").strip() or "DEEPSEEK_API_KEY"] = api_key
        if base_url:
            env[(base_url_env or "DEEPSEEK_BASE_URL").strip() or "DEEPSEEK_BASE_URL"] = base_url
        if extra_env:
            env.update(extra_env)

        before_ids = _session_ids_snapshot(ws_root) if not active_session_id else None

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.path.expanduser("~"),
            env=env,
            limit=10 * 1024 * 1024,
            # 与其它后端一致：独立进程组，/stop 时 killpg 不会误伤 main.py
            start_new_session=True,
        )
        await _fire_callback(on_process_start, proc)

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")), timeout=wall_limit
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"Maka 单轮执行超过 wall-clock 上限（{int(wall_limit)}秒），已终止进程。"
            )

        stdout_text = stdout_data.decode("utf-8", errors="replace")
        stderr_text = stderr_data.decode("utf-8", errors="replace").strip()
        # node 的 SQLite 实验特性警告每次都往 stderr 刷，别把它当错误详情报给用户
        stderr_text = "\n".join(
            line
            for line in stderr_text.splitlines()
            if "ExperimentalWarning" not in line and "trace-warnings" not in line
        ).strip()

        claimed = active_session_id or _claim_session_id(
            ws_root, cwd or os.path.expanduser("~"), before_ids
        )
        return stdout_text.strip(), claimed, proc.returncode, stderr_text

    final_text, new_session_id, returncode, stderr_text = await _run_once(session_id)
    used_fresh_session_fallback = False

    # resume 续不上（session 被归档/删了、上一轮 --yolo 状态不匹配、换了 workspace）
    # → 退回新会话重跑一次。returncode<0 = 被信号杀（/stop、restart），人为中断绝不重试。
    if (
        session_id
        and returncode is not None
        and returncode > 0
        and not final_text
        and any(p in stderr_text.lower() for p in _RESUME_BROKEN_PATTERNS)
    ):
        print(
            f"[run_maka] resume failed (code={returncode}, stderr={stderr_text[:160]!r}), "
            f"retrying with fresh session; sid={session_id} cwd={cwd}",
            flush=True,
        )
        final_text, new_session_id, returncode, stderr_text = await _run_once(None)
        used_fresh_session_fallback = True

    if returncode != 0:
        detail = stderr_text or "no stderr"
        if final_text:
            # rc!=0 但已经吐出了正文（沙箱边界被拒之类）——正文比错误更有用
            return final_text, new_session_id, used_fresh_session_fallback
        lowered = detail.lower()
        if any(p in lowered for p in _SETUP_ERROR_PATTERNS):
            raise RuntimeError(
                f"Maka 未就绪：{detail}\n"
                f"（连接目录 {os.path.join(ws_root, 'connection-catalog.json')}；"
                f"给 profile 配 MAKA_API_KEY/MAKA_BASE_URL 或 MAKA_CONNECTION 后重试）"
            )
        exc = RuntimeError(f"maka exited with code {returncode}: {detail}")
        if returncode > 0 and new_session_id and not is_fatal_error_text(detail):
            exc.cc_session_id = new_session_id
            exc.cc_retryable_resume = True
        raise exc

    if not final_text:
        raise RuntimeError("maka run 正常退出但没有任何输出（finalOutput 为空）")

    # 没有流式通道，只能在收尾时把整段正文一次性交给卡片
    await _fire_callback(on_text_chunk, final_text)
    return final_text, new_session_id, used_fresh_session_fallback
