"""Codex 后端的 app-server 传输：每轮拉起一个 `codex app-server`，走 stdio JSON-RPC。

和 `codex exec --json` 的区别（做法参考 t3code 的 CodexAdapterV2，MIT）：

- 结构化事件：正文增量（item/agentMessage/delta）、工具 item 的开始 / 完成、
  turn/completed 带状态（completed / interrupted / failed）和结构化错误
  （codexErrorInfo），不用再猜「进程退出了是不是干完了」。
- 原生会话：thread/start 新建，thread/resume 用 thread id 接回原生会话（不重放记录）。
  exec 留下的 thread id 是同一套 rollout，直接能 resume。
- 运行中插话：turn/steer 把消息追加进正在跑的 turn（模型下一步就看到）；
  `!` 打断改道 = turn/interrupt + 同一进程里新开一个 turn；/stop = turn/interrupt。
- 用量：thread/tokenUsage/updated 给卡片 footer 的上下文占用，
  account/rateLimits/updated 推给 codex_quota_watcher。

一轮 = 一个子进程：进程活着时可以连跑好几个 turn（插话没赶上 / 打断改道 / 完成标记续跑），
都在同一条 thread 上，不再像 exec 那样每次续跑都重新拉进程 resume。

协议以实际 codex 版本为准（0.164 用 `codex app-server generate-json-schema` 核对过）：
帧是一行一个 JSON，没有 "jsonrpc" 字段；服务端请求（审批等）带 id + method，要回 result。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

CLIENT_NAME = "cc-lark"
CLIENT_VERSION = "1"

# 我们用不到、量又大的通知：diff 我们不展示，推理增量不上卡片（和 exec 一致）。
_OPT_OUT_NOTIFICATIONS = [
    "turn/diff/updated",
    "item/reasoning/textDelta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "item/fileChange/outputDelta",
]

# 单帧上限：commandExecution 的 aggregatedOutput 可能很大
_LINE_LIMIT = 64 * 1024 * 1024
_STDERR_KEEP = 64 * 1024
_REQUEST_TIMEOUT = 120.0

# thread/resume 报这些 = 原会话没了（被删 / 换了 CODEX_HOME），开新会话并告诉用户
_RESUME_MISSING_MARKERS = (
    "not found", "no rollout", "does not exist", "unknown thread", "no such thread",
    "invalid thread", "failed to load rollout", "no thread",
)
_RESUME_ARCHIVED_MARKERS = ("is archived", "codex unarchive")


class AppServerError(RuntimeError):
    """app-server 回了 JSON-RPC error，或进程中途没了。"""

    def __init__(self, message: str, code: Optional[int] = None, method: str = ""):
        super().__init__(message)
        self.code = code
        self.method = method


async def _fire(cb, *args):
    if cb is None:
        return None
    result = cb(*args)
    if asyncio.iscoroutine(result):
        return await result
    return result


# ── JSON-RPC 连接 ─────────────────────────────────────────────────────────


def _open_trace():
    """CODEX_APP_SERVER_TRACE=<文件>：把双向帧逐行追加进去（排障用，默认关）。

    帧里有完整的提示词和命令输出，只在排查时临时打开。正文增量不记（量大没信息）。
    """
    path = (os.getenv("CODEX_APP_SERVER_TRACE") or "").strip()
    if not path:
        return None
    try:
        return open(os.path.expanduser(path), "a", encoding="utf-8")
    except OSError:
        return None


class _Connection:
    """一条到 app-server 子进程的 JSON-RPC 连接。

    读循环在后台跑：响应按 id 交给等待的 request()；通知进队列，由驱动 turn 的
    主循环按顺序消费；服务端请求（审批 / 提问）就地交给 server_request_handler 回复。
    """

    def __init__(
        self,
        proc,
        server_request_handler: Callable[[str, dict], Awaitable[Any]],
        on_frame: Optional[Callable[[], None]] = None,
    ):
        self.proc = proc
        self._handler = server_request_handler
        self._on_frame = on_frame
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.notifications: asyncio.Queue = asyncio.Queue()
        self.closed = asyncio.Event()
        self.close_reason = ""
        self._reader: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()
        self._trace = _open_trace()

    def _log(self, direction: str, msg: dict) -> None:
        if self._trace is None or msg.get("method") == "item/agentMessage/delta":
            return
        try:
            self._trace.write(json.dumps({"t": round(time.time(), 3), "dir": direction, "msg": msg},
                                         ensure_ascii=False) + "\n")
            self._trace.flush()
        except Exception:
            pass

    def start(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())

    def _write(self, msg: dict) -> bool:
        stdin = getattr(self.proc, "stdin", None)
        if stdin is None or self.closed.is_set():
            return False
        is_closing = getattr(stdin, "is_closing", None)
        if callable(is_closing) and is_closing():
            return False
        try:
            stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self._log("out", msg)
            return True
        except Exception:
            return False

    async def _drain(self) -> None:
        stdin = getattr(self.proc, "stdin", None)
        drain = getattr(stdin, "drain", None)
        if drain is None:
            return
        try:
            await asyncio.wait_for(drain(), timeout=10)
        except Exception:
            pass

    async def request(self, method: str, params: Optional[dict] = None,
                      timeout: Optional[float] = _REQUEST_TIMEOUT) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg: dict[str, Any] = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        if not self._write(msg):
            self._pending.pop(rid, None)
            raise AppServerError(f"app-server 已退出，{method} 发不出去", method=method)
        await self._drain()
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise AppServerError(f"app-server {method} 超时（>{timeout:.0f}s）", method=method)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        if self._write(msg):
            await self._drain()

    def close_stdin(self) -> None:
        stdin = getattr(self.proc, "stdin", None)
        if stdin is None:
            return
        try:
            stdin.close()
        except Exception:
            pass

    async def _readline(self) -> bytes:
        stdout = self.proc.stdout
        try:
            return await stdout.readuntil(b"\n")
        except asyncio.IncompleteReadError as e:
            return e.partial  # EOF：最后一段没换行
        except asyncio.LimitOverrunError as e:
            # 超长帧：整帧丢掉（读到换行为止），别让连接卡死
            await stdout.readexactly(e.consumed)
            while True:
                try:
                    await stdout.readuntil(b"\n")
                    break
                except asyncio.LimitOverrunError as e2:
                    await stdout.readexactly(e2.consumed)
            print("[codex_app_server] 丢弃一条超长帧", file=sys.stderr, flush=True)
            return b"\n"

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._readline()
                if not raw:
                    self.close_reason = "eof"
                    break
                if self._on_frame is not None:
                    self._on_frame()
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                self._log("in", msg)
                method = msg.get("method")
                if method and "id" in msg:
                    task = asyncio.create_task(self._answer(msg))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                elif method:
                    self.notifications.put_nowait(msg)
                elif "id" in msg:
                    fut = self._pending.get(msg.get("id"))
                    if fut is None or fut.done():
                        continue
                    if "error" in msg and msg["error"] is not None:
                        err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
                        fut.set_exception(AppServerError(
                            str(err.get("message") or err), code=err.get("code"),
                        ))
                    else:
                        fut.set_result(msg.get("result"))
        except Exception as e:  # pragma: no cover - 防御：读循环自己别把 runner 带崩
            self.close_reason = f"reader error: {type(e).__name__}: {e}"
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AppServerError("app-server 进程已退出"))
            self.notifications.put_nowait(None)  # 叫醒消费方

    async def _answer(self, msg: dict) -> None:
        rid = msg.get("id")
        method = str(msg.get("method") or "")
        try:
            result = await self._handler(method, msg.get("params") or {})
        except Exception as e:
            self._write({"id": rid, "error": {"code": -32603, "message": str(e)}})
            await self._drain()
            return
        if result is _UNSUPPORTED:
            self._write({"id": rid, "error": {"code": -32601, "message": f"{method} not supported by cc-lark"}})
        else:
            self._write({"id": rid, "result": result})
        await self._drain()

    async def aclose(self) -> None:
        if self._trace is not None:
            try:
                self._trace.close()
            except Exception:
                pass
            self._trace = None
        for task in list(self._tasks):
            task.cancel()
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            try:
                await self._reader
            except BaseException:
                pass


_UNSUPPORTED = object()


# ── 服务端请求（审批 / 提问）────────────────────────────────────────────


def make_server_request_handler(on_tool_use=None) -> Callable[[str, dict], Awaitable[Any]]:
    """cc-lark 无人值守：审批一律拒绝，问题一律空答。

    正常配置（approvalPolicy=never）下这些请求根本不会来；真来了说明 profile 配了要审批的
    策略，此时拒绝是唯一安全的回答（bot 前面没有人点「允许」），并在卡片上留一行说明。
    """

    async def _note(label: str, params: dict):
        if on_tool_use is None:
            return
        detail = params.get("command") or params.get("reason") or params.get("message") or ""
        if isinstance(detail, list):
            detail = " ".join(str(x) for x in detail)
        await _fire(on_tool_use, "codex_approval", {
            "description": f"{label}（无人值守，已自动拒绝）",
            "command": str(detail)[:200],
            "status": "failed",
        })

    async def handler(method: str, params: dict):
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            await _note("需要审批的操作", params)
            return {"decision": "decline"}
        if method in ("execCommandApproval", "applyPatchApproval"):
            await _note("需要审批的操作", params)
            return {"decision": "denied"}
        if method == "item/permissions/requestApproval":
            await _note("申请额外权限", params)
            return {"permissions": {}, "scope": "turn"}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        if method == "mcpServer/elicitation/request":
            await _note("MCP 请求确认", params)
            return {"action": "decline"}
        if method == "item/tool/call":
            return {"contentItems": [], "success": False}
        return _UNSUPPORTED

    return handler


# ── 运行中插话 ─────────────────────────────────────────────────────────────


class CodexRunInput:
    """一个正在跑的 codex app-server 会话的输入端，接口对齐 claude_runner.RunInput。

    dispatcher 只用到：open / send(text, priority) / steer(text) / stop() /
    undelivered() / on_turn_result。

    - send：有活动 turn 就 turn/steer 追加进去；turn 刚好收尾（steer 被拒）就排进
      「下一个 turn」，由 runner 在当前 turn 结束后接着开。
    - steer（`!` 打断）：turn/interrupt，当前 turn 以 interrupted 结束后 runner 用新指令
      在同一进程里开新 turn。
    - stop（/stop）：turn/interrupt，等这个 turn 收尾（最多 settle 秒）后关 stdin，
      app-server 读到 EOF 自己退出。
    """

    def __init__(self, conn: _Connection, thread_id: str):
        self._conn = conn
        self.thread_id = thread_id
        self.turn_id: Optional[str] = None      # 当前活动 turn（None = 两个 turn 之间）
        self._closed = False
        self.stopped = False
        self.preempting = False
        self._steered: dict[str, str] = {}       # clientUserMessageId → 正文（已 steer，未见回显）
        self._queued: list[tuple[str, str]] = []  # (uid, 正文)：等下一个 turn 再发
        self.pending_now: Optional[tuple[str, str]] = None  # `!` 打断后要立刻接着跑的指令
        self._turn_ended = asyncio.Event()
        self.on_started: Optional[Callable[[str], Any]] = None
        self.on_turn_result: Optional[Callable[[str], Any]] = None

    @property
    def open(self) -> bool:
        if self._closed or self._conn.closed.is_set():
            return False
        return getattr(self._conn.proc, "returncode", None) is None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._conn.close_stdin()

    # runner 侧钩子 ---------------------------------------------------------
    def _turn_started(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self._turn_ended.clear()

    def _turn_finished(self) -> None:
        self.turn_id = None
        self._turn_ended.set()

    async def _user_message_seen(self, client_id: Optional[str]) -> None:
        if client_id and client_id in self._steered:
            self._steered.pop(client_id, None)
            await _fire(self.on_started, client_id)

    def _take_followups(self) -> list[tuple[str, str]]:
        """当前 turn 结束时还没被模型收到的插话：steer 了但没回显的 + 排队的。"""
        items = list(self._steered.items()) + self._queued
        self._steered.clear()
        self._queued = []
        return items

    # dispatcher 侧 ---------------------------------------------------------
    async def send(self, text: str, priority: str = "next") -> Optional[str]:
        del priority  # app-server 只有一种：追加进当前 turn，下一步就送达
        if not self.open:
            return None
        uid = f"cc-lark-{uuid.uuid4().hex[:12]}"
        turn_id = self.turn_id
        if turn_id is None:
            self._queued.append((uid, text))
            return uid
        self._steered[uid] = text
        try:
            await self._conn.request("turn/steer", {
                "threadId": self.thread_id,
                "expectedTurnId": turn_id,
                "clientUserMessageId": uid,
                "input": [_text_input(text)],
            }, timeout=30)
        except AppServerError:
            # turn 刚好结束 / 换了 turn：改排到下一个 turn
            if self._steered.pop(uid, None) is not None:
                if not self.open:
                    return None
                self._queued.append((uid, text))
        return uid

    async def steer(self, text: str) -> Optional[str]:
        if not self.open:
            return None
        uid = f"cc-lark-{uuid.uuid4().hex[:12]}"
        self.pending_now = (uid, text)
        self.preempting = True
        if self.turn_id is not None:
            await self.interrupt()
        return uid

    async def interrupt(self) -> bool:
        turn_id = self.turn_id
        if turn_id is None or not self.open:
            return False
        try:
            await self._conn.request("turn/interrupt", {
                "threadId": self.thread_id, "turnId": turn_id,
            }, timeout=15)
            return True
        except AppServerError:
            return False

    async def stop(self, settle: float = 2.0) -> None:
        self.stopped = True
        self.pending_now = None
        if self.turn_id is not None and await self.interrupt():
            try:
                await asyncio.wait_for(self._turn_ended.wait(), timeout=settle)
            except asyncio.TimeoutError:
                pass
        self.close()

    def undelivered(self) -> list[str]:
        out = list(self._steered.values()) + [t for _, t in self._queued]
        if self.pending_now is not None:
            out.append(self.pending_now[1])
        return out


def _text_input(text: str) -> dict:
    return {"type": "text", "text": text, "text_elements": []}


# ── 事件 → cc-lark 回调 ──────────────────────────────────────────────────


_TOOL_STATUS = {
    "inProgress": "in_progress",
    "completed": "completed",
    "failed": "failed",
    "declined": "failed",
}


def item_to_tool(item: dict, completed: bool) -> Optional[tuple[str, dict]]:
    """把 app-server 的 ThreadItem 映射成 dispatcher 认识的 (tool_name, input)。"""
    itype = str(item.get("type") or "")
    status = _TOOL_STATUS.get(str(item.get("status") or ""), "completed" if completed else "in_progress")
    if itype == "commandExecution":
        return ("bash", {
            "command": str(item.get("command") or ""),
            "output": item.get("aggregatedOutput") or "",
            "exit_code": item.get("exitCode"),
            "status": status,
        })
    if itype == "fileChange":
        changes = item.get("changes") or []
        paths = [str(c.get("path") or "") for c in changes if isinstance(c, dict)]
        target = paths[0] if paths else ""
        if len(paths) > 1:
            target += f" 等 {len(paths)} 个文件"
        return ("edit", {"file_path": target, "status": status})
    if itype == "mcpToolCall":
        return ("call_mcp_tool", {
            "server": str(item.get("server") or ""),
            "tool": str(item.get("tool") or ""),
            "status": status,
        })
    if itype == "dynamicToolCall":
        return (str(item.get("tool") or "tool"), {"status": status})
    if itype == "webSearch":
        return ("websearch", {"query": str(item.get("query") or ""), "status": status})
    if itype == "imageView":
        return ("read", {"file_path": str(item.get("path") or ""), "status": status})
    if itype == "collabAgentToolCall":
        return ("task", {"description": str(item.get("prompt") or item.get("tool") or "子 agent"), "status": status})
    if itype == "contextCompaction":
        return ("compact", {"status": status})
    return None


def normalize_token_usage(token_usage: dict) -> dict[str, int]:
    """thread/tokenUsage/updated → footer 字段（和 exec 路径读 session 文件的口径一致：取 last）。"""
    last = token_usage.get("last") or {}
    usage = {
        "input_tokens": int(last.get("inputTokens") or 0),
        "output_tokens": int(last.get("outputTokens") or 0),
    }
    cached = int(last.get("cachedInputTokens") or 0)
    if cached:
        usage["_cached_input_tokens"] = cached
    reasoning = int(last.get("reasoningOutputTokens") or 0)
    if reasoning:
        usage["reasoning_output_tokens"] = reasoning
    window = int(token_usage.get("modelContextWindow") or 0)
    if window:
        usage["_context_window"] = window
    return usage


def format_turn_error(err: Optional[dict]) -> str:
    if not isinstance(err, dict):
        return ""
    msg = str(err.get("message") or "").strip()
    details = str(err.get("additionalDetails") or "").strip()
    if details and details not in msg:
        msg = f"{msg}\n{details}" if msg else details
    return msg


def error_info_code(err: Optional[dict]) -> str:
    info = (err or {}).get("codexErrorInfo") if isinstance(err, dict) else None
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and info:
        return next(iter(info.keys()))
    return ""


class _TurnText:
    """一个 turn 里 agentMessage 的拼接与流式差量（含完成标记的剥离与防泄漏）。"""

    def __init__(self, sentinel: str = ""):
        self.sentinel = sentinel
        self.order: list[str] = []
        self.text: dict[str, str] = {}
        self.phase: dict[str, str] = {}
        self.emitted = ""
        self.saw_sentinel = False

    def start(self, item_id: str, phase: str = "") -> None:
        if item_id not in self.text:
            self.order.append(item_id)
            self.text[item_id] = ""
        if phase:
            self.phase[item_id] = phase

    def delta(self, item_id: str, delta: str) -> None:
        self.start(item_id)
        self.text[item_id] += delta

    def complete(self, item_id: str, text: str, phase: str = "") -> None:
        self.start(item_id, phase)
        if text:
            self.text[item_id] = text

    def _strip(self, s: str) -> str:
        if self.sentinel and self.sentinel in s:
            self.saw_sentinel = True
            s = s.replace(self.sentinel, "")
        return s

    def composed(self) -> str:
        parts = [self.text[i].strip() for i in self.order if self.text[i].strip()]
        return self._strip("\n\n".join(parts))

    def final(self) -> str:
        """turn 的最终产出：有 final_answer 阶段的消息就只取它们（commentary 留在过程区）。"""
        finals = [self.text[i].strip() for i in self.order
                  if self.phase.get(i) == "final_answer" and self.text[i].strip()]
        if finals:
            return self._strip("\n\n".join(finals)).strip()
        return self.composed().strip()

    def next_chunk(self) -> str:
        live = self.composed()
        if self.sentinel:
            # 尾巴可能是半个完成标记（⟦CC_TA…），先扣住别发出去
            for k in range(len(self.sentinel) - 1, 0, -1):
                if live.endswith(self.sentinel[:k]):
                    live = live[:-k]
                    break
        if not live or live == self.emitted:
            return ""
        if live.startswith(self.emitted):
            chunk = live[len(self.emitted):]
        else:
            chunk = live  # 前文被改写（极少见）：整段重发，和 exec 路径一致
        self.emitted = live
        return chunk


# ── 进程 ─────────────────────────────────────────────────────────────────


async def spawn_app_server(cmd: list[str], cwd: Optional[str], env: Optional[dict] = None):
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd or os.path.expanduser("~"),
        start_new_session=True,
        limit=_LINE_LIMIT,
        env=env,
    )


async def _drain_stderr(proc, keep: bytearray) -> None:
    stream = getattr(proc, "stderr", None)
    if stream is None:
        return
    try:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            keep.extend(chunk)
            if len(keep) > _STDERR_KEEP:
                del keep[: len(keep) - _STDERR_KEEP]
    except Exception:
        return


async def _shutdown(proc, conn: _Connection, grace: float = 5.0) -> None:
    conn.close_stdin()
    if getattr(proc, "returncode", None) is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            _signal_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                _signal_group(proc, signal.SIGKILL)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
    await conn.aclose()


def _signal_group(proc, sig) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except Exception:
        try:
            proc.send_signal(sig)
        except Exception:
            pass


# ── 一轮 ─────────────────────────────────────────────────────────────────


class TurnOutcome:
    def __init__(self):
        self.status = ""
        self.error: Optional[dict] = None
        self.text = ""
        self.final = ""
        self.saw_sentinel = False


async def run_app_server_session(
    *,
    cmd: list[str],
    message: str,
    session_id: Optional[str],
    cwd: Optional[str],
    model: Optional[str],
    effort: Optional[str],
    sandbox: str,
    approval_policy: str,
    developer_instructions: Optional[str],
    on_text_chunk=None,
    on_tool_use=None,
    on_process_start=None,
    on_usage=None,
    on_status=None,
    on_input_ready=None,
    on_rate_limits=None,
    should_stop: Optional[Callable[[], bool]] = None,
    idle_timeout_sec: int = 3600,
    auto_continue: Optional[dict] = None,
    spawn=spawn_app_server,
) -> tuple[str, Optional[str], bool]:
    """跑一轮：起 app-server → initialize → start/resume thread → turn/start → 收事件到 turn 结束。

    auto_continue: None = 不续跑；否则 {"sentinel", "nudge", "max_passes", "budget"}——
    模型没输出完成标记时，在同一进程同一 thread 上再开一个 turn 催它（不再重拉进程 resume）。

    返回 (最后一个 turn 的最终文本, thread_id, 是否因原会话丢失而新开了会话)。
    """
    proc = await spawn(cmd, cwd)
    await _fire(on_process_start, proc)
    stderr_buf = bytearray()
    stderr_task = asyncio.create_task(_drain_stderr(proc, stderr_buf))
    last_frame = [time.monotonic()]

    def _touch():
        last_frame[0] = time.monotonic()

    conn = _Connection(proc, make_server_request_handler(on_tool_use), on_frame=_touch)
    conn.start()
    run_input: Optional[CodexRunInput] = None
    fresh_fallback = False
    thread_id: Optional[str] = session_id

    def _stderr_text() -> str:
        from codex_runner import _clean_stderr
        return _clean_stderr(bytes(stderr_buf).decode("utf-8", errors="replace"))

    def _stop_requested() -> bool:
        if run_input is not None and run_input.stopped:
            return True
        if isinstance(getattr(proc, "returncode", None), int) and proc.returncode < 0:
            return True
        if should_stop is None:
            return False
        try:
            return bool(should_stop())
        except Exception:
            return False

    try:
        try:
            await conn.request("initialize", {
                "clientInfo": {"name": CLIENT_NAME, "title": "cc-lark", "version": CLIENT_VERSION},
                "capabilities": {"optOutNotificationMethods": _OPT_OUT_NOTIFICATIONS},
            }, timeout=60)
        except AppServerError as e:
            if "already initialized" not in str(e).lower():
                raise
        await conn.notify("initialized", {})

        thread_params: dict[str, Any] = {
            "sandbox": sandbox,
            "approvalPolicy": approval_policy,
        }
        if cwd:
            thread_params["cwd"] = cwd
        if model:
            thread_params["model"] = model
        if developer_instructions:
            thread_params["developerInstructions"] = developer_instructions

        result = None
        if session_id:
            resume_params = {"threadId": session_id, "excludeTurns": True, **thread_params}
            try:
                result = await conn.request("thread/resume", resume_params)
            except AppServerError as e:
                low = str(e).lower()
                if any(m in low for m in _RESUME_ARCHIVED_MARKERS):
                    await conn.request("thread/unarchive", {"threadId": session_id})
                    result = await conn.request("thread/resume", resume_params)
                elif any(m in low for m in _RESUME_MISSING_MARKERS) and not conn.closed.is_set():
                    print(f"[codex_app_server] resume {session_id} 失败（{e}），开新会话", file=sys.stderr, flush=True)
                    fresh_fallback = True
                else:
                    raise
        if result is None:
            result = await conn.request("thread/start", thread_params)
        thread_id = str(((result or {}).get("thread") or {}).get("id") or "") or None
        if not thread_id:
            raise AppServerError("app-server 没有返回 thread id")

        run_input = CodexRunInput(conn, thread_id)
        if on_input_ready is not None:
            await _fire(on_input_ready, run_input)

        sentinel = (auto_continue or {}).get("sentinel") or ""
        max_passes = int((auto_continue or {}).get("max_passes") or 0)
        budget = float((auto_continue or {}).get("budget") or 0)
        started = time.monotonic()
        passes = 0
        next_input: Optional[str] = message
        last_final = ""

        while next_input is not None:
            if _stop_requested():
                break
            outcome = await _run_turn(
                conn, run_input, thread_id, next_input,
                model=model, effort=effort, cwd=cwd, sentinel=sentinel,
                on_text_chunk=on_text_chunk, on_tool_use=on_tool_use, on_usage=on_usage,
                on_status=on_status, on_rate_limits=on_rate_limits,
                idle_timeout_sec=idle_timeout_sec, last_frame=last_frame,
                proc=proc, stderr_text=_stderr_text,
            )
            next_input = None
            if outcome.final:
                last_final = outcome.final
            await _fire(run_input.on_turn_result, outcome.final)

            if _stop_requested():
                break
            if outcome.status == "interrupted":
                if run_input.pending_now is not None:
                    uid, text = run_input.pending_now
                    run_input.pending_now = None
                    run_input.preempting = False
                    followups = run_input._take_followups()
                    next_input = _join_inputs([t for _, t in followups] + [text])
                    continue
                # 不是我们打断的（外部 / app-server 自己）：当没跑完处理
                raise _incomplete(
                    "Codex 这一轮被中断了，没有跑完。回复「继续」可接着做。",
                    thread_id, outcome,
                )
            if outcome.status == "failed":
                reason = format_turn_error(outcome.error) or "Codex turn failed"
                if not outcome.text:
                    raise RuntimeError(reason)
                raise _incomplete(f"Codex 中途报错：{reason}", thread_id, outcome)
            if outcome.status != "completed":
                raise _incomplete(
                    f"Codex turn 以未知状态结束（{outcome.status or '无 turn/completed'}）。",
                    thread_id, outcome,
                )

            followups = run_input._take_followups()
            if run_input.pending_now is not None:
                followups.append(run_input.pending_now)
                run_input.pending_now = None
            if followups:
                next_input = _join_inputs([t for _, t in followups])
                continue
            if sentinel and not outcome.saw_sentinel:
                passes += 1
                if passes > max_passes or time.monotonic() - started >= budget:
                    break
                await _fire(on_text_chunk, f"\n\n━━━━━━━ 🔁 自动续跑 · 第 {passes + 1} 轮 ━━━━━━━\n\n")
                if _stop_requested():
                    break
                next_input = (auto_continue or {}).get("nudge") or "继续"
        # 循环结束：关掉输入端（之后的插话走 dispatcher 排队）
        if run_input is not None:
            run_input._closed = True
        return last_final, thread_id, fresh_fallback
    except AppServerError as e:
        if _stop_requested():
            return "", thread_id, fresh_fallback
        detail = _stderr_text()
        msg = str(e)
        if detail and conn.closed.is_set():
            msg = f"{msg}\n{detail[-2000:]}"
        raise RuntimeError(msg) from e
    finally:
        if run_input is not None:
            run_input._closed = True
        await _shutdown(proc, conn)
        try:
            await asyncio.wait_for(stderr_task, timeout=2)
        except BaseException:
            stderr_task.cancel()


def _join_inputs(texts: list[str]) -> str:
    texts = [t for t in texts if t and t.strip()]
    return "\n\n---\n\n".join(texts)


def _incomplete(reason: str, thread_id: Optional[str], outcome: TurnOutcome):
    from auto_continue import IncompleteTaskError
    err = IncompleteTaskError(reason, thread_id)
    err.partial_text = outcome.final or outcome.text  # type: ignore[attr-defined]
    return err


async def _run_turn(
    conn: _Connection,
    run_input: CodexRunInput,
    thread_id: str,
    text: str,
    *,
    model, effort, cwd, sentinel,
    on_text_chunk, on_tool_use, on_usage, on_status, on_rate_limits,
    idle_timeout_sec: int, last_frame: list, proc, stderr_text,
) -> TurnOutcome:
    params: dict[str, Any] = {"threadId": thread_id, "input": [_text_input(text)]}
    if model:
        params["model"] = model
    if effort:
        params["effort"] = effort
    if cwd:
        params["cwd"] = cwd
    result = await conn.request("turn/start", params)
    turn_id = str(((result or {}).get("turn") or {}).get("id") or "")
    if not turn_id:
        raise AppServerError("turn/start 没有返回 turn id")
    run_input._turn_started(turn_id)

    out = TurnOutcome()
    tt = _TurnText(sentinel)
    warned = False
    idle = max(0, int(idle_timeout_sec or 0))

    async def _emit_text():
        chunk = tt.next_chunk()
        if chunk:
            await _fire(on_text_chunk, chunk)

    try:
        while True:
            timeout = None
            if idle:
                timeout = max(1.0, idle - (time.monotonic() - last_frame[0]))
            try:
                msg = await asyncio.wait_for(conn.notifications.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if idle and time.monotonic() - last_frame[0] >= idle:
                    _signal_group(proc, signal.SIGTERM)
                    raise RuntimeError(f"Codex 长时间无输出（>{idle}s），进程已终止。")
                continue
            if msg is None:
                # 连接断了：进程没了还没等到 turn/completed
                if run_input.stopped:
                    out.status = "interrupted"
                    break
                detail = stderr_text()
                raise AppServerError(
                    "Codex app-server 意外退出" + (f"：{detail[-1500:]}" if detail else ""),
                )
            method = msg.get("method")
            p = msg.get("params") or {}
            if p.get("threadId") not in (None, thread_id):
                continue  # 子 agent 等其它 thread 的事件
            if p.get("turnId") not in (None, turn_id) and method != "turn/completed":
                continue

            if method == "item/agentMessage/delta":
                tt.delta(str(p.get("itemId") or ""), str(p.get("delta") or ""))
                await _emit_text()
            elif method in ("item/started", "item/completed"):
                item = p.get("item") or {}
                itype = item.get("type")
                done = method == "item/completed"
                if itype == "agentMessage":
                    iid = str(item.get("id") or "")
                    if done:
                        tt.complete(iid, str(item.get("text") or ""), str(item.get("phase") or ""))
                    else:
                        tt.start(iid, str(item.get("phase") or ""))
                    await _emit_text()
                elif itype == "userMessage":
                    await run_input._user_message_seen(item.get("clientId"))
                else:
                    tool = item_to_tool(item, done)
                    if tool:
                        await _fire(on_tool_use, tool[0], tool[1])
            elif method == "thread/tokenUsage/updated":
                usage = normalize_token_usage(p.get("tokenUsage") or {})
                if usage.get("input_tokens") or usage.get("output_tokens"):
                    await _fire(on_usage, usage)
            elif method == "account/rateLimits/updated":
                if on_rate_limits is not None and isinstance(p.get("rateLimits"), dict):
                    try:
                        await _fire(on_rate_limits, p["rateLimits"])
                    except Exception as e:
                        print(f"[codex_app_server] rate limit hook 失败: {e}", file=sys.stderr, flush=True)
            elif method == "error":
                err = p.get("error") or {}
                if p.get("willRetry"):
                    warned = True
                    label = format_turn_error(err) or "Codex 正在重试"
                    await _fire(on_status, "warn", f"Codex 重试中：{label[:120]}")
                else:
                    out.error = err
            elif method == "turn/completed":
                turn = p.get("turn") or {}
                if str(turn.get("id") or "") != turn_id:
                    continue
                out.status = str(turn.get("status") or "")
                if turn.get("error"):
                    out.error = turn.get("error")
                # turn 里带回的最终 agentMessage（流里漏了的话兜底）
                for item in turn.get("items") or []:
                    if isinstance(item, dict) and item.get("type") == "agentMessage" and item.get("text"):
                        iid = str(item.get("id") or "")
                        if not tt.text.get(iid):
                            tt.complete(iid, str(item["text"]), str(item.get("phase") or ""))
                await _emit_text()
                break
    finally:
        run_input._turn_finished()
        if warned:
            await _fire(on_status, "clear", "")

    out.text = tt.composed().strip()
    out.final = tt.final()
    out.saw_sentinel = tt.saw_sentinel
    return out
