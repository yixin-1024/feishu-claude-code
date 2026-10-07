"""
本地调用 Claude Code CLI 的统一入口。

两种后端：
  - "pty"   （默认）→ claude_pty.run_claude：在 PTY 里跑交互式 claude，结构化
                       事件靠 tail ~/.claude/projects/<cwd>/<sid>.jsonl 拿到。
                       不依赖 --print，可以保留 slash 命令 / 权限 UX / plan
                       mode 等交互式 Claude Code 的完整行为。
  - "print" → 本文件 _run_claude_print：经典 `claude --print --output-format
              stream-json` 子进程模式，按 token 级 delta 解析。

通过 CLAUDE_RUNNER 环境变量切换，默认 pty。

两种模式共享：
  - 复用 ~/.claude/ 中已有的 Max 订阅登录凭证
  - 相同的对外签名：run_claude(message, session_id, ...) →
        (full_text, new_session_id, used_fresh_session_fallback)
  - 相同的回调协议：on_text_chunk / on_tool_use / on_process_start / on_usage
"""

import asyncio
import json
import os
import subprocess as sp
import sys
import uuid
from typing import Any, Callable, Optional

from bot_config import (
    CLAUDE_CLI,
    CLAUDE_EFFORT_LEVELS,
    CLAUDE_WALL_CLOCK_LIMIT_DEFAULT,
    PERMISSION_MODE,
    resolve_claude_wall_clock_limit,
)

# ── 后端选择 ──────────────────────────────────────────────────
# 默认走 PTY 后端；线上要回退到 -p 时设置 CLAUDE_RUNNER=print
_RUNNER_BACKEND = os.getenv("CLAUDE_RUNNER", "pty").strip().lower()

IDLE_TIMEOUT = 300  # 5 分钟无输出且无子进程 → 视为挂死
_CHECK_INTERVAL = 30  # 静默时每 30 秒检查一次子进程
# 有子进程但 Claude 端持续无新输出的"卡死"上限。
# 专治 tail -f / watch / npm run dev：子进程一直在但 Claude 端再也不产出。
# 留得比 IDLE_TIMEOUT 宽，避免误杀正常长编译/长安装。
STUCK_CHILD_TIMEOUT = int(os.getenv("STUCK_CHILD_TIMEOUT_SEC", "3600"))  # 1 小时
# 单轮 wall-clock 最终保险：无论是否还在产出，都会强杀，防 runaway loop。
# 默认 60 分钟，可用 CLAUDE_WALL_CLOCK_LIMIT_SEC（或 <PROFILE>_ 前缀）改；设 0 = 永不。
# 具体解析见 bot_config.resolve_claude_wall_clock_limit。
WALL_CLOCK_LIMIT = CLAUDE_WALL_CLOCK_LIMIT_DEFAULT  # 兼容旧引用；实际取值按 env 动态解析


def _has_children(pid: int) -> bool:
    """进程是否有活跃子进程（说明在跑 bash 命令、编译等）。"""
    try:
        result = sp.run(["pgrep", "-P", str(pid)], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _extract_text_content(value) -> str:
    """Extract final assistant text from Claude CLI result payload."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


# 用量墙 / 限流：重试无益，得等配额恢复。
_RATE_LIMIT_PATTERNS = (
    "rate_limit",
    "rate limit",
    "usage limit",
    "session limit",
    "quota exceeded",
    "resource_exhausted",
    "resource has been exhausted",
    "429 too many requests",
    "error 429",
)

# 模型侧 safeguards 拦截（"Fable 5's safeguards flagged this message ...
# change your model"）：同一模型重试 / 续跑必然再被拦，不属于瞬时抖动。
# dispatcher 对它有专项处理：不重试，自动降级模型后 resume 续跑。
_SAFEGUARDS_ERR_PATTERNS = (
    "safeguards flagged",
)


def is_safeguards_error_text(blob: str) -> bool:
    """错误文本是否为模型 safeguards 拦截（换模型才有救，重试无益）。"""
    low = (blob or "").lower()
    return any(p in low for p in _SAFEGUARDS_ERR_PATTERNS)


# --resume 的 session 已经不在本机：Claude Code 默认 cleanupPeriodDays=30 会删
# ~/.claude/projects/*/<sid>.jsonl，而 thread→session 绑定永不过期，老话题一回消息
# 就拿几十天前的 sid 去 resume。CLI 返回 subtype=error_during_execution +
# duration_ms=0 + errors=["No conversation found with session ID: …"]（退出码 0，
# result 为空），光看 subtype 完全看不出真因。重试同一个 sid 必然再挂，只能换新 session。
_SESSION_MISSING_ERR_PATTERNS = (
    "no conversation found with session id",
)


def is_session_missing_error_text(blob: str) -> bool:
    """错误文本是否为「resume 的 session 已不存在」（换新 session 才有救）。"""
    low = (blob or "").lower()
    return any(p in low for p in _SESSION_MISSING_ERR_PATTERNS)


# 重试无益的错误：认证 / 余额 / 请求本身不合法 / CLI 参数错。命中这些才不自动续跑；
# 其余一律按「上游瞬时抖动」处理（流中断、5xx、overloaded、连接被掐、CLI 自己炸…），
# 因为崩溃前的 session JSONL 是干净可 --resume 的，续跑基本能恢复。
# ⚠️ 这里是「黑名单」而不是「白名单」：上游错误文案变来变去（"Response stalled
# mid-stream" / "Connection closed mid-response" / …），白名单漏一个就等于中断不续跑。
_FATAL_ERR_PATTERNS = (
    _RATE_LIMIT_PATTERNS + _SAFEGUARDS_ERR_PATTERNS + _SESSION_MISSING_ERR_PATTERNS
) + (
    "invalid api key",
    "invalid_api_key",
    "authentication_error",
    "authentication failed",
    "permission_error",
    "credit balance",
    "prompt is too long",
    "context_length",
    "invalid_request_error",
    "unknown option",
    "unknown argument",
)


def is_fatal_error_text(blob: str, status=None) -> bool:
    """错误文本 / HTTP 状态码是否属于「重试无益」（不该自动续跑）。

    4xx（除 408/409/425/429 这类可重试的）视为请求侧问题；429 与文案里的用量墙
    信号同样算 fatal。其余（5xx、无状态码的流中断、连接错误）都算瞬时抖动。
    """
    low = (blob or "").lower()
    if isinstance(status, int) and 400 <= status < 500 and status not in (408, 409, 425):
        return True
    return any(p in low for p in _FATAL_ERR_PATTERNS)


def _extract_errors_text(data: dict) -> str:
    """把 result 事件的 errors[] 摊平成一行文本；没有就返回空串。"""
    errors = data.get("errors")
    if isinstance(errors, str):
        return errors.strip()
    if not isinstance(errors, list):
        return ""
    parts = []
    for item in errors:
        if isinstance(item, str):
            piece = item.strip()
        elif isinstance(item, dict):
            piece = str(item.get("message") or item.get("error") or item).strip()
        else:
            piece = str(item).strip()
        if piece:
            parts.append(piece)
    return "; ".join(parts)


def _classify_result_error(data: dict, text: str):
    """判断 stream-json 的 result 事件是不是错误结果。

    返回 (人类可读错误消息, 是否瞬时可 resume 恢复)；正常结果返回 None。
    字段以实测 CLI 2.1.201 为准：subtype='success'/'error_during_execution'…、
    is_error=bool、api_error_status=int|None、result=错误文本（流中断时是
    "API Error: Response stalled mid-stream. ..." 或 "API Error: Connection closed
    mid-response. ..."，此时 subtype 可能仍是 'success' 只有 is_error=true）。

    ⚠️ 有一类错误 result 是空的、真因只在 errors[] 里（如 resume 了已被清理的
    session）。不读 errors 的话 detail 会回落成 subtype，错误卡变成
    "error_during_execution：error_during_execution" 这种自我复读。
    """
    subtype = str(data.get("subtype") or "")
    is_error = data.get("is_error") is True
    if not is_error and subtype in ("", "success"):
        return None
    status = data.get("api_error_status")
    errors_text = _extract_errors_text(data)
    detail = (text or "").strip() or errors_text or subtype or "unknown error"
    blob = f"{text}\n{errors_text}\n{subtype}"
    if status == 429 or any(p in blob.lower() for p in _RATE_LIMIT_PATTERNS):
        return (f"Claude Max 用量已达上限：{detail}", False)
    transient = not is_fatal_error_text(blob, status)
    # 流被掐断时 subtype 仍可能是 'success'——别把这个词打到错误卡上误导用户
    label = subtype if subtype and subtype != "success" else "stream_error"
    http_part = f", HTTP {status}" if isinstance(status, int) else ""
    return (f"Claude API 错误（{label}{http_part}）：{detail}", transient)


def _runner_backend_from_env(extra_env: Optional[dict]) -> str:
    return (
        (extra_env or {}).get("CLAUDE_RUNNER")
        or os.getenv("CLAUDE_RUNNER", _RUNNER_BACKEND)
        or "pty"
    ).strip().lower()


def _cc_lark_cli_args(extra_env: Optional[dict]) -> list[str]:
    """Build cc-lark specific Claude CLI flags shared by print and PTY modes."""
    args: list[str] = []
    default_deny = (
        "Task,Workflow,SendMessage,RemoteTrigger,"
        "ScheduleWakeup,Monitor,CronCreate,CronDelete,CronList"
    )
    deny = os.getenv("CC_LARK_DISALLOWED_TOOLS", default_deny).strip()
    if deny:
        args += ["--disallowedTools", deny]

    cc_cfg = cc_lark_mcp_config(extra_env, log_tag="claude_runner")
    if cc_cfg:
        args += ["--mcp-config", json.dumps(cc_cfg)]
    return args


def cc_lark_mcp_config(extra_env: Optional[dict], log_tag: str = "cc-mcp") -> Optional[dict]:
    """本轮的 cc-lark 运行时 MCP 配置（Claude `--mcp-config` 的 JSON 形状）。

    没有话题上下文（私聊直跑 / 非 bot 调用）或 CC_LARK_WAKE_MCP=0 时返回 None。
    CC_LARK_* 显式写进 server 的 env，不靠继承——并发话题各拿各的。
    qoder 的 `--mcp-config` 也吃这个形状，两边共用。
    """
    if not (extra_env or {}).get("CC_LARK_THREAD_ID"):
        return None
    if os.getenv("CC_LARK_WAKE_MCP", "1") == "0":
        return None
    try:
        cc_server = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "cc_mcp_server.py"
        )
        if not os.path.isfile(cc_server):
            return None
        cc_env = {
            k: str(v) for k, v in (extra_env or {}).items()
            if k.startswith("CC_LARK_")
            and k != "CC_LARK_CONTROL_TOKEN"
            and v is not None
        }
        # 能力闸门支持 per-profile 覆盖（<PROFILE>_<FLAG> 优先于全局 <FLAG>）。
        from bot_config import resolve_cc_lark_gates
        cc_env.update(resolve_cc_lark_gates((extra_env or {}).get("CC_LARK_PROFILE") or ""))
        return {
            "mcpServers": {
                "cc-lark": {
                    "command": sys.executable,
                    "args": [cc_server],
                    "env": cc_env,
                }
            }
        }
    except Exception as exc:
        print(
            f"[{log_tag}] cc-mcp 注入跳过: {type(exc).__name__}: {exc}",
            flush=True,
        )
        return None


async def _fire_callback(cb, *args):
    if cb is None:
        return
    if asyncio.iscoroutinefunction(cb):
        await cb(*args)
    else:
        cb(*args)


# ── 运行中插话（print 后端 + --input-format stream-json）────────────────
# 默认关；CLAUDE_PRINT_STREAM_INPUT=1 打开。打开后 stdin 不再写完就关，dispatcher
# 拿到 RunInput 句柄，可以往正在跑的进程里补消息、软中断。实测（CLI 2.1.289，
# 2026-10-06，调研文档见 memory project_print_stream_input_injection）：
#   priority=next  在下一个工具调用的间隙并进当前轮（长命令 / 纯文本长回答期间要等那一步结束）
#   priority=now   当前轮立刻以 aborted_* 结束，同进程同上下文新开一轮
#   priority=later 当前轮正常结束后，同进程接着跑
#   control interrupt  当场结束正在跑的工具，进程不退出
# 关 stdin 时已经写进去、还在排队的消息照样会跑完再退出，所以「关」永远不丢消息；
# 唯一要处理的竞态是「关了之后还想写」——send 返回 None，调用方退回排队。

def stream_input_enabled(extra_env: Optional[dict] = None) -> bool:
    raw = (extra_env or {}).get("CLAUDE_PRINT_STREAM_INPUT")
    if raw is None:
        raw = os.getenv("CLAUDE_PRINT_STREAM_INPUT", "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class RunInput:
    """一个正在跑的 `claude --print --input-format stream-json` 进程的输入端。

    stdout 解析循环把 command_lifecycle / result 事件喂进来（_on_lifecycle /
    _on_result），由它判断什么时候可以关 stdin 让进程退出：上一轮已结束、CLI 说
    没有排队的消息（queued_turn_count=0）、我们写进去的消息也都已处理完。
    注意 CLI 的 completed 生命周期事件在 result **之后**才到，所以 result 里的
    user_message_uuids 也要算作已处理。
    """

    def __init__(self, proc):
        self._proc = proc
        self._closed = False
        self._first_uid = ""
        self._outstanding: dict[str, str] = {}  # 已写入、还没处理完的消息 uuid → 正文
        self._started: set[str] = set()
        self._idle = False          # 收到 result 之后、下一轮 started 之前
        self._queued = 0            # 最近一个 result 报的排队数
        # 我们发过 now / interrupt：随后那个 aborted_* 的 result 是自己打断的，不算错误
        self.preempting = False
        # /stop 走的软停：进程会以退出码 1 退出，runner 不能把它当「resume 失败」再拉新进程
        self.stopped = False
        self._turn_ended = asyncio.Event()  # 每收到一个 result 置位
        # dispatcher 挂的回调：某条插话开始被处理 / 每轮结束（参数是这一轮的最终文本）
        self.on_started: Optional[Callable[[str], Any]] = None
        self.on_turn_result: Optional[Callable[[str], Any]] = None

    @property
    def open(self) -> bool:
        if self._closed or getattr(self._proc, "returncode", None) is not None:
            return False
        is_closing = getattr(self._proc.stdin, "is_closing", None)
        return not (callable(is_closing) and is_closing())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._proc.stdin.close()
        except Exception:
            pass

    def _write(self, obj: dict) -> bool:
        if not self.open:
            return False
        try:
            self._proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
            return True
        except Exception:
            self._closed = True
            return False

    async def _drain(self) -> None:
        try:
            await asyncio.wait_for(self._proc.stdin.drain(), timeout=10)
        except Exception:
            pass

    @staticmethod
    def _user_msg(text: str, priority: str, uid: str) -> dict:
        msg = {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
            "session_id": "",
            "uuid": uid,
        }
        if priority:
            # 不传时的默认值由服务端开关决定（实测像 next，没开时是 later），一律显式写
            msg["priority"] = priority
        return msg

    def _register(self, text: str, priority: str) -> Optional[str]:
        uid = str(uuid.uuid4())
        if not self._write(self._user_msg(text, priority, uid)):
            return None
        self._outstanding[uid] = text
        if priority == "now":
            self.preempting = True
        return uid

    async def send_first(self, text: str) -> Optional[str]:
        uid = self._register(text, "next")
        self._first_uid = uid or ""
        await self._drain()
        return uid

    async def send(self, text: str, priority: str = "next") -> Optional[str]:
        """补一条用户消息。进程已收尾（stdin 关了）返回 None。"""
        uid = self._register(text, priority)
        if uid:
            await self._drain()
        return uid

    async def steer(self, text: str) -> Optional[str]:
        """打断当前这一轮，立刻按新指令接着跑：软中断后紧跟一条 now。

        两行必须在同一个同步段里写完再 drain——中间一让出事件循环，解析循环可能
        先收到被打断那一轮的 result、看到没有待处理消息就把 stdin 关了，新指令就丢了。
        """
        if not self._write({
            "type": "control_request",
            "request_id": f"cc-lark-{uuid.uuid4().hex[:12]}",
            "request": {"subtype": "interrupt"},
        }):
            return None
        self.preempting = True
        uid = self._register(text, "now")
        await self._drain()
        return uid

    async def interrupt(self) -> bool:
        """软中断：结束正在跑的这一轮（含正在跑的工具），进程不退出。"""
        ok = self._write({
            "type": "control_request",
            "request_id": f"cc-lark-{uuid.uuid4().hex[:12]}",
            "request": {"subtype": "interrupt"},
        })
        if ok:
            self.preempting = True
            await self._drain()
        return ok

    async def stop(self, settle: float = 2.0) -> None:
        """/stop：软中断，等被打断那一轮的 result 回来再关 stdin，让进程自己干净退出。

        中断后立刻关 stdin 的话，CLI 偶尔会在杀掉正在跑的工具命令之前就退出，
        命令成了孤儿（实测 4 次里 1 次）。等到这一轮的 result（CLI 已处理完中断）
        再关；result 一直不来就最多等 settle 秒。
        """
        self.stopped = True
        self._turn_ended.clear()
        if await self.interrupt():
            try:
                await asyncio.wait_for(self._turn_ended.wait(), timeout=settle)
            except asyncio.TimeoutError:
                pass
        self.close()

    def undelivered(self) -> list[str]:
        """写进去了但模型还没开始处理的插话（进程意外退出时由调用方补发）。"""
        return [
            t for u, t in self._outstanding.items()
            if u not in self._started and u != self._first_uid
        ]

    def _maybe_close(self) -> None:
        if self._idle and self._queued <= 0 and not self._outstanding:
            self.close()

    async def _on_lifecycle(self, data: dict) -> None:
        uid = str(data.get("command_uuid") or "")
        state = data.get("state")
        if state == "started":
            self._idle = False
            if uid in self._outstanding and uid not in self._started:
                self._started.add(uid)
                if uid != self._first_uid:
                    await _fire_callback(self.on_started, uid)
        elif state in ("completed", "cancelled"):
            self._outstanding.pop(uid, None)
            self._maybe_close()

    async def _on_result(self, data: dict, final_text: str) -> None:
        for uid in data.get("user_message_uuids") or []:
            self._outstanding.pop(str(uid), None)
        try:
            self._queued = int(data.get("queued_turn_count") or 0)
        except (TypeError, ValueError):
            self._queued = 0
        self._idle = True
        self._turn_ended.set()
        await _fire_callback(self.on_turn_result, final_text)
        self._maybe_close()


def _is_preempted_result(data: dict) -> bool:
    """被 now / interrupt 打断的那一轮：terminal_reason=aborted_streaming / aborted_tools。"""
    return str(data.get("terminal_reason") or "").startswith("aborted")


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
    extra_env: Optional[dict] = None,
    effort: Optional[str] = None,
    on_input_ready: Optional[Callable[["RunInput"], None]] = None,
) -> tuple[str, Optional[str], bool]:
    """
    调用 Claude Code CLI 并流式解析输出。

    extra_env: 注入 spawn 子进程的额外环境变量（覆盖 os.environ）。用于把某个
    profile/bot 路由到不同模型供应商（如 DeepSeek 的 Anthropic 兼容端点）。
    若其中含 ANTHROPIC_MODEL，会用它覆盖命令行 --model（否则把 deepseek bot
    用 claude-opus-4-x 这种 Anthropic 模型名发去 deepseek 会 404）。

    on_input_ready: print 后端开了 CLAUDE_PRINT_STREAM_INPUT 时，进程起来后回调一个
    RunInput（可往运行中的进程补消息 / 软中断）；PTY 后端和没开时不会回调。

    后端由 CLAUDE_RUNNER 环境变量决定：
        pty   (默认) → PTY + JSONL tail，保留交互式 Claude Code 完整体验
        print        → 经典 `claude --print` 子进程，按 token delta 解析

    Returns:
        (full_response_text, new_session_id, used_fresh_session_fallback)
    """
    # spawn 前按需切账户：当前账户烧穿时在这一拍就切到好账户，不必等 quota_watcher
    # 10min 轮询。内部带探测节流（默认 45s 一次）+ 冷却防抖，健康路径几乎零开销；
    # probe 走网络，丢到 executor 跑别堵事件循环。失败/未启用时静默 no-op。
    try:
        from account_switcher import maybe_switch_before_spawn
        await asyncio.get_event_loop().run_in_executor(None, maybe_switch_before_spawn)
    except Exception:
        pass

    try:
        runner_backend = _runner_backend_from_env(extra_env)
        if runner_backend == "print":
            return await _run_claude_print(
                message=message,
                session_id=session_id,
                model=model,
                effort=effort,
                cwd=cwd,
                permission_mode=permission_mode,
                on_text_chunk=on_text_chunk,
                on_tool_use=on_tool_use,
                on_process_start=on_process_start,
                on_usage=on_usage,
                append_system_prompt=append_system_prompt,
                extra_env=extra_env,
                on_input_ready=on_input_ready,
            )
        # 默认 PTY 后端：延迟 import 避免 print-only 部署没装 termios 的奇怪环境出错
        from claude_pty import run_claude as _run_claude_pty
        return await _run_claude_pty(
            message=message,
            session_id=session_id,
            model=model,
            effort=effort,
            cwd=cwd,
            permission_mode=permission_mode,
            on_text_chunk=on_text_chunk,
            on_tool_use=on_tool_use,
            on_process_start=on_process_start,
            on_usage=on_usage,
            on_status=on_status,
            append_system_prompt=append_system_prompt,
            extra_env=extra_env,
        )
    finally:
        # spawn 结束后（含异常）：Claude CLI 这一轮可能自行轮换过 keychain token，
        # 把它回收进对应 saved 快照，否则快照与 keychain 脱节 →「未识别」+ 下次拿旧
        # refresh_token 刷新 invalid_grant 400。best-effort，丢 executor 别堵事件循环。
        try:
            from account_switcher import resync_current_from_keychain
            await asyncio.get_event_loop().run_in_executor(
                None, resync_current_from_keychain)
        except Exception:
            pass


async def _run_claude_print(
    message: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    permission_mode: Optional[str] = None,
    on_text_chunk: Optional[Callable[[str], None]] = None,
    on_tool_use: Optional[Callable[[str, dict], None]] = None,
    on_process_start: Optional[Callable[[asyncio.subprocess.Process], None]] = None,
    on_usage: Optional[Callable[[dict], None]] = None,
    append_system_prompt: Optional[str] = None,
    extra_env: Optional[dict] = None,
    effort: Optional[str] = None,
    on_input_ready: Optional[Callable[[RunInput], None]] = None,
) -> tuple[str, Optional[str], bool]:
    """`claude --print --output-format stream-json` 模式（兼容/兜底后端）。"""

    stream_input = stream_input_enabled(extra_env)
    stopped_by_user = False  # 本次进程是被 RunInput.stop() 软停的

    # extra_env 里的 ANTHROPIC_MODEL 覆盖命令行 --model（供应商路由用）
    effective_model = (extra_env or {}).get("ANTHROPIC_MODEL") or model

    async def _run_once(active_session_id: Optional[str]) -> tuple[str, Optional[str], int, str]:
        cmd = [
            CLAUDE_CLI,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", permission_mode or PERMISSION_MODE,
        ]
        if stream_input:
            cmd += ["--input-format", "stream-json"]
        if active_session_id:
            cmd += ["--resume", active_session_id]
        if effective_model:
            cmd += ["--model", effective_model]
        _cc_effort = (
            effort
            or (extra_env or {}).get("CLAUDE_EFFORT")
            or os.getenv("CLAUDE_EFFORT")
        )
        _cc_effort = str(_cc_effort).strip().lower() if _cc_effort else ""
        if _cc_effort and _cc_effort not in CLAUDE_EFFORT_LEVELS:
            raise ValueError(
                f"invalid Claude effort {_cc_effort!r}; "
                f"expected one of {list(CLAUDE_EFFORT_LEVELS)}"
            )
        if _cc_effort:
            cmd += ["--effort", _cc_effort]
        if append_system_prompt:
            cmd += ["--append-system-prompt", append_system_prompt]
        cmd += _cc_lark_cli_args(extra_env)

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        # 让 session-mirror 的 UserPromptSubmit hook 自我排除 bot spawn 的 claude，
        # 否则 bot 自己的会话也会被镜像进 Lark。见 claude_session_mirror.py。
        env["CC_LARK_MIRROR_OFF"] = "1"
        if extra_env:
            env.update(extra_env)
        if _cc_effort:
            # Claude Code 的官方环境变量优先级高于 CLI flag；同步设置 child env，
            # 确保话题级 /effort 不会被父进程已有配置反向覆盖。
            env["CLAUDE_EFFORT"] = _cc_effort
            env["CLAUDE_CODE_EFFORT_LEVEL"] = _cc_effort

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.path.expanduser("~"),
            env=env,
            limit=10 * 1024 * 1024,
            # stop_run() 会按进程组终止 runner 及其 MCP/tool 子进程。
            # print 后端也必须像 PTY/Codex/OpenCode/MiMo 一样成为独立组长；
            # 否则 killpg 会把同组的 main.py / .app wrapper 一起杀掉。
            start_new_session=True,
        )

        await _fire_callback(on_process_start, proc)

        run_input: Optional[RunInput] = None
        if stream_input:
            run_input = RunInput(proc)
            await run_input.send_first(message)
            await _fire_callback(on_input_ready, run_input)
        else:
            proc.stdin.write((message + "\n").encode())
            await proc.stdin.drain()
            proc.stdin.close()

        full_text = ""
        new_session_id = None
        pending_tool_name = ""
        pending_tool_input_json = ""

        idle_seconds = 0
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        wall_clock_limit = resolve_claude_wall_clock_limit(extra_env)
        # 插话会让一个进程跑好几轮：每条插话开跑时把上限顺延一个 limit，但整个进程
        # 最多 2 个 limit，防止一直插话让进程永远不结束。
        wall_deadline = start_time + wall_clock_limit

        try:
            while True:
                # wall-clock 最终保险，防 runaway loop（默认 60 分钟；limit<=0 = 关掉）
                if wall_clock_limit > 0 and loop.time() >= wall_deadline:
                    proc.kill()
                    await proc.wait()
                    raise RuntimeError(
                        f"Claude 单轮执行超过 wall-clock 最终上限（{int(wall_clock_limit)}秒），已终止进程。"
                    )

                try:
                    raw_line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=_CHECK_INTERVAL
                    )
                    idle_seconds = 0  # 收到输出，重置计时
                except asyncio.TimeoutError:
                    idle_seconds += _CHECK_INTERVAL
                    if run_input is not None and run_input.open and run_input._idle:
                        # 上一轮已结束、却没等到「全部处理完」的信号（CLI 版本变了、事件
                        # 缺字段）：静默 30s 就关 stdin。关不会丢已写进去的消息。
                        run_input.close()
                        continue
                    has_kids = _has_children(proc.pid)
                    threshold = STUCK_CHILD_TIMEOUT if has_kids else IDLE_TIMEOUT
                    if idle_seconds >= threshold:
                        proc.kill()
                        await proc.wait()
                        if has_kids:
                            raise RuntimeError(
                                f"Claude 执行超时（{threshold}秒有子进程但 Claude 端无任何新输出），已终止进程。"
                                f"常见原因：tail -f / watch / npm run dev 等永不退出的阻塞命令。"
                            )
                        raise RuntimeError(
                            f"Claude 执行超时（{threshold}秒无输出且无活跃子进程），已终止进程"
                        )
                    continue

                if not raw_line:  # EOF
                    break

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = data.get("type")

                if event_type == "command_lifecycle":
                    if run_input is not None:
                        if (
                            data.get("state") == "started"
                            and wall_clock_limit > 0
                            and str(data.get("command_uuid") or "") != run_input._first_uid
                        ):
                            wall_deadline = min(
                                start_time + 2 * wall_clock_limit,
                                max(wall_deadline, loop.time() + wall_clock_limit),
                            )
                        await run_input._on_lifecycle(data)

                elif event_type == "system":
                    sid = data.get("session_id")
                    if sid:
                        new_session_id = sid

                elif event_type == "stream_event":
                    evt = data.get("event", {})
                    evt_type = evt.get("type")

                    if evt_type == "content_block_delta":
                        delta = evt.get("delta", {})
                        delta_type = delta.get("type")

                        if delta_type == "text_delta":
                            chunk = delta.get("text", "")
                            if chunk:
                                full_text += chunk
                                await _fire_callback(on_text_chunk, chunk)

                        elif delta_type == "input_json_delta":
                            pending_tool_input_json += delta.get("partial_json", "")

                    elif evt_type == "content_block_start":
                        block = evt.get("content_block", {})
                        if block.get("type") == "tool_use":
                            pending_tool_name = block.get("name", "")
                            pending_tool_input_json = ""
                            await _fire_callback(on_tool_use, pending_tool_name, {})

                    elif evt_type == "content_block_stop":
                        if pending_tool_name and pending_tool_input_json:
                            try:
                                inp = json.loads(pending_tool_input_json)
                            except json.JSONDecodeError:
                                inp = {}
                            await _fire_callback(on_tool_use, pending_tool_name, inp)
                        pending_tool_name = ""
                        pending_tool_input_json = ""

                elif event_type == "result":
                    sid = data.get("session_id")
                    if sid:
                        new_session_id = sid
                    final_text = _extract_text_content(data.get("result", ""))
                    if (
                        run_input is not None
                        and run_input.preempting
                        and _is_preempted_result(data)
                    ):
                        # 我们自己用 now / interrupt 打断的那一轮：不是错误，也不是最终
                        # 结果（interrupt 的 result 还是 error_during_execution）。
                        run_input.preempting = False
                        await run_input._on_result(data, "")
                        continue
                    # 上游流中断 / 服务端错误时，CLI 也走 result 事件，只是 is_error=true /
                    # subtype=error_*，result 里塞的是 "API Error: Response stalled mid-stream…"。
                    # 旧逻辑会把这段错误文本当正常回复原样发出（还覆盖掉已流式出来的真内容）。
                    # 与 PTY 后端对齐：识别为错误就 raise，把可 resume 的 session id 带出去，
                    # 让 dispatcher 显示错误卡 + 自动续跑（瞬时错误）而不是伪装成回答。
                    err = _classify_result_error(data, final_text or full_text)
                    if err is not None:
                        msg, transient = err
                        exc = RuntimeError(msg)
                        exc.cc_session_id = new_session_id
                        exc.cc_retryable_resume = transient
                        # resume 的 session 已被清理：外层据此换新 session 重跑一次，
                        # 别把这个 sid 当"崩溃前可 resume 的会话"再传下去。
                        if is_session_missing_error_text(
                            f"{final_text or full_text}\n{_extract_errors_text(data)}"
                        ):
                            exc.cc_session_missing = True
                            exc.cc_session_id = None
                        raise exc
                    if final_text:
                        full_text = final_text
                    # usage 顶层是跨内部迭代的累加值，不反映上下文实际占用。
                    # iterations[-1] 才是最后一次内部调用的真实 prompt 大小，≈ 当前 context fill。
                    raw_usage = dict(data.get("usage") or {})
                    iterations = raw_usage.get("iterations") or []
                    if isinstance(iterations, list) and iterations:
                        last = iterations[-1]
                        if isinstance(last, dict):
                            usage = dict(last)
                        else:
                            usage = raw_usage
                    else:
                        usage = raw_usage
                    usage.pop("iterations", None)
                    # modelUsage 里带 contextWindow，优先用准确值
                    model_usage = data.get("modelUsage") or {}
                    if isinstance(model_usage, dict) and model_usage:
                        first = next(iter(model_usage.values()), {})
                        if isinstance(first, dict) and first.get("contextWindow"):
                            usage["_context_window"] = first["contextWindow"]
                    if usage:
                        await _fire_callback(on_usage, usage)
                    if run_input is not None:
                        # 正常结束的一轮：之前发的打断没赶上（这一轮自己先完了），标记作废，
                        # 免得后面真出错的 aborted 轮被当成自己打断的吞掉
                        run_input.preempting = False
                        # 这一轮结束：没有排队 / 待处理的插话就关 stdin，进程随后退出（EOF）
                        await run_input._on_result(data, final_text)

        finally:
            # 任何退出路径（含上面 raise 错误）都要关 stdin：stream-json 输入下进程
            # 会一直等下一条消息，不关就成了孤儿进程。
            if run_input is not None:
                run_input.close()

        stderr_output = await proc.stderr.read()
        await proc.wait()
        stderr_text = stderr_output.decode("utf-8", errors="replace").strip()
        if run_input is not None and run_input.stopped:
            nonlocal stopped_by_user
            stopped_by_user = True
        return full_text.strip(), new_session_id, proc.returncode, stderr_text

    used_fresh_session_fallback = False

    try:
        final_text, new_session_id, returncode, stderr_text = await _run_once(session_id)
        if stopped_by_user:
            # 软停后 CLI 以退出码 1 退出、可能没有输出——正好撞上下面「resume 哑失败
            # 换新 session 重跑」的签名。用户要的是停，绝不能再拉一个新进程。
            return final_text, new_session_id, False
    except RuntimeError as exc:
        # resume 的 session 已被 CLI 的 30 天清理删掉（errors[] 里 "No conversation
        # found with session ID"）。这条路径在 result 事件里就 raise 了，走不到下面
        # 那段 rc>0 的兜底 —— 不在这里接住的话，dispatcher 会拿同一个死 sid 重试到
        # 放弃，该话题从此每条消息都必挂。永久错误，只能换新 session 重跑一次。
        if not (session_id and getattr(exc, "cc_session_missing", False)):
            raise
        print(
            f"[run_claude] resume target gone ({exc}); retrying with fresh session; "
            f"sid={session_id} cwd={cwd}",
            flush=True,
        )
        final_text, new_session_id, returncode, stderr_text = await _run_once(None)
        used_fresh_session_fallback = True

    # 同一个"session 已被清理"，老 CLI 可能只落 stderr + 非零退出码、不吐 result
    # 事件（那样上面的 except 接不到）。这里按 stderr 再兜一次，两种表现都能自愈。
    if stopped_by_user:
        return final_text, new_session_id, used_fresh_session_fallback

    if (
        session_id
        and not used_fresh_session_fallback
        and returncode not in (0, None)
        and is_session_missing_error_text(stderr_text)
    ):
        print(
            f"[run_claude] resume target gone (stderr); retrying with fresh session; "
            f"sid={session_id} cwd={cwd}",
            flush=True,
        )
        final_text, new_session_id, returncode, stderr_text = await _run_once(None)
        used_fresh_session_fallback = True

    # resume 旧 session "哑失败"的兜底：code>0 + stderr 空 + 无输出。
    # 成因不止 cwd 变（那只是其一）：上一轮被杀致 JSONL 写一半、session 被 CLI 清掉、
    # resume 瞬时报错等都会撞这个签名。统一退回新 session，避免用户必须手动 /new。
    # 注意：用户文案别再写死"工作目录已变化"，那是误判（见 dispatcher fallback 提示）。
    # returncode 为负数 = 被信号杀（如 /stop 的 SIGTERM/SIGKILL），不能 fallback——
    # 否则用户 /stop 后会立刻在 lock 内拉起新进程，造成"队列说在跑、/stop 杀不死"的死循环。
    if (
        session_id
        and not used_fresh_session_fallback  # 上面已经换过新 session，别再换一次
        and returncode is not None
        and returncode > 0
        and not stderr_text
        and not final_text
    ):
        print(
            f"[run_claude] resume failed (code={returncode}, empty stderr/output), "
            f"retrying with fresh session; sid={session_id} cwd={cwd}",
            flush=True,
        )
        final_text, new_session_id, returncode, stderr_text = await _run_once(None)
        used_fresh_session_fallback = True

    if returncode != 0:
        detail = stderr_text or "no stderr"
        if final_text:
            detail += f" (partial output length={len(final_text)})"
        # 如果有部分输出，返回给用户看而不是抛异常
        if final_text:
            return final_text, new_session_id, used_fresh_session_fallback
        exc = RuntimeError(f"claude exited with code {returncode}: {detail}")
        # 进程级崩溃（网络抖断、CLI 自己炸）同样让 dispatcher 能 resume 续跑：
        # returncode < 0 = 被信号杀（/stop、restart），那是人为中断，不标可续跑；
        # stderr 命中 fatal 模式（用量墙 / 认证 / 参数错）也不续跑。
        if returncode > 0 and new_session_id and not is_fatal_error_text(stderr_text):
            exc.cc_session_id = new_session_id
            exc.cc_retryable_resume = True
        raise exc

    return final_text, new_session_id, used_fresh_session_fallback
