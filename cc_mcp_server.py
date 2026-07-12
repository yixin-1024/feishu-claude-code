#!/usr/bin/env python3
"""cc-lark 运行时 MCP server（stdio，零依赖）。

定位：把"只有常驻 bot 能干的运行时动作"暴露成 Claude Code 可调的 MCP 工具。
本进程是**每个 turn 由 claude 拉起的短命前端**——turn 结束随 claude 一起被 killpg
也无所谓，因为它**不持有任何状态**：它只把一次工具调用翻译成对常驻 bot 本机
control API（默认 127.0.0.1:9982 /wake）的一次鉴权请求，真正的"几分钟后唤醒"由 bot 的
APScheduler 持久兑现（见 scheduler.schedule_wake）。

→ 这正是"MCP server 必须 host 在常驻 bot 里"的落地：调度状态在 bot，stdio 这层
只是 typed 前端，模型不用自己拼 crontab / 也不会把 thread / @ 发错。

协议：MCP over stdio = **换行分隔 JSON**（newline-delimited JSON-RPC 2.0，一行一条）。
⚠️ 不是 LSP 的 Content-Length 帧——实测 Claude Code 2.1.196 只认换行帧，用
Content-Length 写回会让 client 的 initialize 等不到响应、30s 握手超时、一个工具都
不注册（读侧 _read_framed_message 兼容两种，写侧必须换行帧，见 _write_framed_message）。
实现 initialize / notifications/initialized / tools/list / tools/call / ping。
**stdout 只许写 JSON-RPC**，所有日志走 stderr。

当前工具：
  wake_me_in(minutes, note) —— 安排 N 分钟后在「当前这条 Lark 话题」里自动开一个
  新 turn，把 note 作为 prompt 续上。本轮调用后应当 END THE TURN、不要在原地干等。

会话上下文（chat / thread / 归属人 / profile / 回复锚点 / 回调端口）由 bot 在
spawn claude 时通过 --mcp-config 的 env 块注入到本进程环境里（CC_LARK_*），模型
无需也无法传错。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

SERVER_NAME = "cc-lark"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2025-06-18"

WAKE_MIN_MINUTES = 1
WAKE_MAX_MINUTES = 1440  # 24h 上限；更久的等待场景请用 bg-job


def _log(msg: str) -> None:
    """日志只能走 stderr —— stdout 被 JSON-RPC 独占。"""
    try:
        print(f"[cc-mcp] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def _control_base() -> str:
    # canonical 变量优先。后两个 alias 只为兼容尚未重启、仍注入旧 wake_context 的 bot；
    # 新版 dispatcher 始终提供 CC_LARK_CONTROL_PORT，绝不会把控制请求发向 ngrok 端口。
    port = (
        os.environ.get("CC_LARK_CONTROL_PORT")
        or os.environ.get("CC_LARK_HTTP_PORT")
        or os.environ.get("CC_LARK_CALLBACK_PORT")
        or "9982"
    ).strip() or "9982"
    return f"http://127.0.0.1:{port}"


def _control_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = (os.environ.get("CC_LARK_CONTROL_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _allow(flag: str, default: str = "1") -> bool:
    """能力开关：env 里显式 0/false/no/off 才关，否则默认开。关掉的能力对应工具**不注册**
    （对 agent 完全隐形，不是调用时才拒），这是把 agent 自主性显式收口的闸门。"""
    return (os.environ.get(flag, default) or default).strip().lower() not in ("0", "false", "no", "off")


# 三个独立闸门（bot 经 --mcp-config 的 env 块注入；未设=开）：
#   CC_LARK_ALLOW_DISPATCH —— 主动派子 agent（dispatch_task + read_thread）
#   CC_LARK_ALLOW_WAKE     —— 定时/等事件自我唤醒（wake_me_in）
#   CC_LARK_ALLOW_CRON     —— 重复定时任务（schedule_cron + list_crons）
_ALLOW_DISPATCH = _allow("CC_LARK_ALLOW_DISPATCH")
_ALLOW_WAKE = _allow("CC_LARK_ALLOW_WAKE")
_ALLOW_CRON = _allow("CC_LARK_ALLOW_CRON")


def _post_json(path: str, payload: dict, timeout: int = 35) -> dict:
    """POST 一个 JSON 给常驻 bot 的本机端点，返回解析后的 dict。任何异常向上抛。"""
    req = urllib.request.Request(
        f"{_control_base()}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=_control_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


# ── 工具定义 ──────────────────────────────────────────────────

WAKE_TOOL = {
    "name": "wake_me_in",
    "description": (
        "Schedule the cc-lark bot to AUTOMATICALLY wake a fresh turn in THIS Lark "
        "thread after `minutes` minutes, carrying `note` as the prompt. Use this when "
        "you need to wait for something (CI/deploy/rate-limit reset/just a delay) and "
        "want to check back later WITHOUT blocking the current turn. After calling it, "
        "you should END THE TURN — do NOT sleep or busy-wait (the runtime kills idle "
        "turns). The woken turn starts a FRESH session in the same thread, so write "
        "`note` self-contained: what you were doing + exactly what to check/do on wake. "
        "Thread/recipient/profile context is supplied automatically — you do NOT pass it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "minutes": {
                "type": "integer",
                "minimum": WAKE_MIN_MINUTES,
                "maximum": WAKE_MAX_MINUTES,
                "description": f"Minutes from now to wake ({WAKE_MIN_MINUTES}–{WAKE_MAX_MINUTES}).",
            },
            "note": {
                "type": "string",
                "description": "Self-contained instruction for the woken turn (becomes its prompt).",
            },
        },
        "required": ["minutes", "note"],
    },
}

DISPATCH_TASK_TOOL = {
    "name": "dispatch_task",
    "description": (
        "Fan out an INDEPENDENT sub-task to a fresh cc-lark Claude session running in a "
        "NEW thread in the current Lark group, then return immediately (fire-and-forget) "
        "with its thread_id. This is the generic multi-agent dispatch primitive: use it to "
        "parallelize a goal across several worker agents, each with its own clean context. "
        "The sub-agent runs fully autonomously; you do NOT block waiting for it — poll its "
        "progress/result later with read_thread(thread_id). Write `prompt` SELF-CONTAINED "
        "(working dir, scope, acceptance criteria, any 'do not touch prod' guards) — the "
        "worker has none of your context. HARD LIMIT: the bot enforces a PER-GROUP concurrency "
        "cap (default 7; excess dispatches in the same group are rejected) — dispatch in waves "
        "and wait for the prior wave before launching the next. Target group/recipient are supplied automatically. "
        "AUTO-REPORT: each sub-agent posts a completion line back to YOUR thread when it "
        "finishes (even if it crashes), and once the WHOLE wave is done you are automatically "
        "woken with each sub-agent's ACTUAL RESULT inlined in the wake message — so you may "
        "dispatch a wave and simply END THE TURN; you'll be brought back with all results in "
        "hand to aggregate, no read_thread needed (read_thread is only for full detail). "
        "CROSS-AGENT: by default the worker runs the SAME backend as you (e.g. Claude). Pass "
        "`agent` to run the worker on a DIFFERENT agent/backend loaded in this bot — e.g. "
        "agent=\"gpt\" runs it on the codex(GPT) bot, letting Claude delegate a sub-task to GPT "
        "(or \"gemini\"/\"mimo\", or an exact profile name). The target agent's bot must be a "
        "member of this group; if it isn't the dispatch returns a clear error. The auto-report "
        "and wake still come back to YOU regardless of which agent ran the worker."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Self-contained task brief for the worker sub-agent (becomes its prompt).",
            },
            "title": {
                "type": "string",
                "description": "Optional short thread title (defaults to the prompt's first line).",
            },
            "agent": {
                "type": "string",
                "description": (
                    "Optional target agent/backend for the worker (CROSS-AGENT dispatch). "
                    "Accepts a family alias — \"gpt\"/\"codex\" (GPT), \"claude\", "
                    "\"gemini\"/\"opencode\", \"mimo\" — or an exact loaded profile name. "
                    "Omit to run the worker on your own backend (default)."
                ),
            },
        },
        "required": ["prompt"],
    },
}

READ_THREAD_TOOL = {
    "name": "read_thread",
    "description": (
        "Read all messages of a Lark thread (e.g. one returned by dispatch_task) as a "
        "compact transcript, to supervise or collect a sub-agent's progress/result. "
        "Poll this periodically after dispatching; if the worker isn't done yet you'll see "
        "partial output. Returns sender + timestamp + text per message."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "thread_id": {
                "type": "string",
                "description": "The thread id (omt_…) returned by a prior dispatch_task.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Max messages to read (default 50).",
            },
        },
        "required": ["thread_id"],
    },
}

SCHEDULE_CRON_TOOL = {
    "name": "schedule_cron",
    "description": (
        "Create a RECURRING scheduled task (e.g. 'every day at 9am do X'). At each "
        "scheduled time the cc-lark bot opens a fresh turn in a NEW thread in the current "
        "group and runs `prompt`. This is the persistent 'cron' kind — it survives bot "
        "restarts (written to the bot's task config). For a ONE-OFF 'come back in N minutes' "
        "use wake_me_in instead. `cron` is a standard 5-field expression: minute hour "
        "day-of-month month day-of-week (timezone Asia/Shanghai), e.g. '0 9 * * *' = daily "
        "09:00, '*/30 * * * *' = every 30 min, '0 9 * * 1' = Mondays 09:00. Write `prompt` "
        "SELF-CONTAINED (each run is a fresh session). Group/recipient are supplied "
        "automatically. Returns the task name + next run time; use list_crons to review."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "cron": {"type": "string", "description": "5-field cron: 'minute hour dom month dow' (Asia/Shanghai)."},
            "prompt": {"type": "string", "description": "Self-contained instruction run at each scheduled time."},
            "title": {"type": "string", "description": "Optional short title for the recurring topic."},
        },
        "required": ["cron", "prompt"],
    },
}

LIST_CRONS_TOOL = {
    "name": "list_crons",
    "description": (
        "List scheduled jobs registered in the cc-lark bot for THIS chat (recurring crons "
        "you created via schedule_cron, plus any one-off wakes), with their next run time."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}

# 按闸门装配 tools/list —— 关掉的能力这里就不出现，agent 看都看不到。
TOOLS = []
if _ALLOW_WAKE:
    TOOLS.append(WAKE_TOOL)
if _ALLOW_DISPATCH:
    TOOLS += [DISPATCH_TASK_TOOL, READ_THREAD_TOOL]
if _ALLOW_CRON:
    TOOLS += [SCHEDULE_CRON_TOOL, LIST_CRONS_TOOL]


# ── 工具实现 ──────────────────────────────────────────────────

def _tool_wake_me_in(args: dict) -> dict:
    """返回 MCP tools/call 的 result（{content:[...], isError?}）。"""
    # 1) 入参校验
    minutes = args.get("minutes")
    note = args.get("note")
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return _err("`minutes` must be an integer.")
    if not (WAKE_MIN_MINUTES <= minutes <= WAKE_MAX_MINUTES):
        return _err(f"`minutes` must be between {WAKE_MIN_MINUTES} and {WAKE_MAX_MINUTES}.")
    if not isinstance(note, str) or not note.strip():
        return _err("`note` must be a non-empty string.")

    # 2) 会话上下文（bot 注入的环境变量）。缺 thread 说明这不是话题群，无法定向唤醒。
    chat_id = (os.environ.get("CC_LARK_CHAT_ID") or "").strip()
    thread_id = (os.environ.get("CC_LARK_THREAD_ID") or "").strip()
    anchor = (os.environ.get("CC_LARK_ANCHOR") or "").strip()
    user_id = (os.environ.get("CC_LARK_USER_ID") or "").strip()
    profile = (os.environ.get("CC_LARK_PROFILE") or "").strip()
    if not (chat_id and thread_id):
        return _err(
            "No Lark thread context available — wake_me_in only works inside a topic "
            "thread spawned by the cc-lark bot. (CC_LARK_CHAT_ID / CC_LARK_THREAD_ID unset.)"
        )

    # 3) 把调度请求投给常驻 bot（持久层）。本进程不持有调度状态。
    payload = {
        "profile": profile,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "anchor_message_id": anchor,
        "user_id": user_id,
        "minutes": minutes,
        "note": note.strip(),
    }
    try:
        body = _post_json("/wake", payload, timeout=10)
    except Exception as e:  # noqa: BLE001 — 任何失败都回成可见的工具错误，绝不抛进协议层
        _log(f"/wake POST failed: {type(e).__name__}: {e}")
        return _err(f"Failed to reach cc-lark scheduler: {type(e).__name__}: {e}")

    if not body.get("ok"):
        return _err(f"Scheduler rejected the wake: {body.get('error', 'unknown error')}")

    fire_at = body.get("fire_at_local") or f"~{minutes} min"
    return _ok(
        f"✅ Scheduled a wake in {minutes} min (fires {fire_at}) in this thread. "
        f"You can END THE TURN now — a fresh turn will continue with your note."
    )


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": f"⚠️ {text}"}], "isError": True}


def _tool_dispatch_task(args: dict) -> dict:
    """派一个独立子会话到当前群的新 thread（fan-out）。上下文取自 env。"""
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _err("`prompt` must be a non-empty string.")
    chat_id = (os.environ.get("CC_LARK_CHAT_ID") or "").strip()
    profile = (os.environ.get("CC_LARK_PROFILE") or "").strip()
    user_id = (os.environ.get("CC_LARK_USER_ID") or "").strip()
    if not chat_id:
        return _err(
            "No Lark group context — dispatch_task only works inside a cc-lark group "
            "session. (CC_LARK_CHAT_ID unset.)"
        )
    payload = {
        "profile": profile,
        "chat_id": chat_id,
        "user_id": user_id,
        "title": (args.get("title") or "").strip(),
        "prompt": prompt.strip(),
        # 跨 agent：可选目标后端（"gpt"/"gemini"/"mimo"/profile 名）；空=同 agent
        "agent": (args.get("agent") or "").strip(),
        # 父上下文：让 bot 在子会话结束后回报本 thread + 批次全完时唤醒我（主 agent）
        "parent_thread": (os.environ.get("CC_LARK_THREAD_ID") or "").strip(),
        "parent_anchor": (os.environ.get("CC_LARK_ANCHOR") or os.environ.get("CC_LARK_MESSAGE_ID") or "").strip(),
    }
    try:
        body = _post_json("/dispatch", payload)
    except Exception as e:  # noqa: BLE001
        _log(f"/dispatch POST failed: {type(e).__name__}: {e}")
        return _err(f"Failed to reach cc-lark dispatcher: {type(e).__name__}: {e}")
    if not body.get("ok"):
        return _err(f"Dispatch rejected: {body.get('error', 'unknown error')}")
    agent_note = ""
    if body.get("agent"):
        agent_note = f" on agent {body.get('agent')}[{body.get('agent_runner')}]"
    return _ok(
        f"✅ Dispatched a sub-agent{agent_note} in a new thread. thread_id={body.get('thread_id')} "
        f"(active {body.get('active_after')}/{body.get('cap')}). "
        f"It runs autonomously — poll its progress/result later with "
        f"read_thread(thread_id=\"{body.get('thread_id')}\")."
    )


def _tool_read_thread(args: dict) -> dict:
    """拉回某 thread 的消息 transcript（supervise / 取结果）。"""
    thread_id = (args.get("thread_id") or "").strip()
    if not thread_id:
        return _err("`thread_id` is required (use the value returned by dispatch_task).")
    try:
        limit = int(args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    profile = (os.environ.get("CC_LARK_PROFILE") or "").strip()
    try:
        body = _post_json("/read_thread", {"profile": profile, "thread_id": thread_id, "limit": limit})
    except Exception as e:  # noqa: BLE001
        _log(f"/read_thread POST failed: {type(e).__name__}: {e}")
        return _err(f"Failed to reach cc-lark: {type(e).__name__}: {e}")
    if not body.get("ok"):
        return _err(f"read_thread failed: {body.get('error', 'unknown error')}")
    return _ok(f"Thread {thread_id} — {body.get('count')} message(s):\n\n{body.get('transcript', '')}")


def _tool_schedule_cron(args: dict) -> dict:
    """新增一条重复定时任务。cron + prompt 来自 args；profile/chat/user 取自 env。"""
    cron = (args.get("cron") or "").strip()
    prompt = args.get("prompt")
    if not cron:
        return _err("`cron` is required (5-field: minute hour dom month dow).")
    if not isinstance(prompt, str) or not prompt.strip():
        return _err("`prompt` must be a non-empty string.")
    chat_id = (os.environ.get("CC_LARK_CHAT_ID") or "").strip()
    profile = (os.environ.get("CC_LARK_PROFILE") or "").strip()
    user_id = (os.environ.get("CC_LARK_USER_ID") or "").strip()
    if not chat_id:
        return _err("No Lark group context — schedule_cron only works inside a cc-lark group session.")
    payload = {
        "profile": profile, "chat_id": chat_id, "user_id": user_id,
        "cron": cron, "prompt": prompt.strip(), "title": (args.get("title") or "").strip(),
    }
    try:
        body = _post_json("/schedule_cron", payload)
    except Exception as e:  # noqa: BLE001
        _log(f"/schedule_cron POST failed: {type(e).__name__}: {e}")
        return _err(f"Failed to reach cc-lark scheduler: {type(e).__name__}: {e}")
    if not body.get("ok"):
        return _err(f"schedule_cron rejected: {body.get('error', 'unknown error')}")
    return _ok(
        f"✅ Recurring task created: {body.get('name')} — cron '{body.get('cron')}', "
        f"next run {body.get('next_run')}. It survives restarts. Use list_crons to review."
    )


def _tool_list_crons(args: dict) -> dict:
    # 只列本 chat 的任务：多群共用一个 bot 时不泄露别的群排了什么。
    chat_id = (os.environ.get("CC_LARK_CHAT_ID") or "").strip()
    try:
        body = _post_json("/list_crons", {"chat_id": chat_id})
    except Exception as e:  # noqa: BLE001
        _log(f"/list_crons POST failed: {type(e).__name__}: {e}")
        return _err(f"Failed to reach cc-lark: {type(e).__name__}: {e}")
    if not body.get("ok"):
        return _err(f"list_crons failed: {body.get('error', 'unknown error')}")
    jobs = body.get("jobs", [])
    if not jobs:
        return _ok("No scheduled jobs.")
    lines = [
        f"- {j.get('name')}　next={j.get('next_run')}" + ("　(agent)" if j.get("agent_created") else "")
        for j in jobs
    ]
    return _ok("Scheduled jobs:\n" + "\n".join(lines))


# 只登记开着的能力（防御纵深：闸门关掉的工具即便被硬调也 Unknown tool 拒绝）。
_HANDLERS = {}
if _ALLOW_WAKE:
    _HANDLERS["wake_me_in"] = _tool_wake_me_in
if _ALLOW_DISPATCH:
    _HANDLERS["dispatch_task"] = _tool_dispatch_task
    _HANDLERS["read_thread"] = _tool_read_thread
if _ALLOW_CRON:
    _HANDLERS["schedule_cron"] = _tool_schedule_cron
    _HANDLERS["list_crons"] = _tool_list_crons


# ── JSON-RPC over stdio ───────────────────────────────────────

def _handle(msg: dict):
    """处理一条 JSON-RPC 消息；返回要回写的 dict，或 None（通知 / 无需回应）。"""
    mid = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    # 通知（无 id）：initialized 等，吞掉不回。
    if mid is None:
        return None

    if method == "initialize":
        proto = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return _result(mid, {
            "protocolVersion": proto,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "ping":
        return _result(mid, {})

    if method == "tools/list":
        return _result(mid, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = _HANDLERS.get(name)
        if fn is None:
            return _error(mid, -32602, f"Unknown tool: {name}")
        try:
            return _result(mid, fn(args))
        except Exception as e:  # noqa: BLE001
            _log(f"tool {name} raised: {type(e).__name__}: {e}")
            return _result(mid, _err(f"Internal error in {name}: {type(e).__name__}: {e}"))

    return _error(mid, -32601, f"Method not found: {method}")


def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _read_framed_message():
    """Read one MCP stdio message.

    Claude Code uses Content-Length framing. For local smoke tests we also accept
    a single newline-delimited JSON object if the first byte is "{".
    """
    first = sys.stdin.buffer.peek(1)[:1]
    if not first:
        return None
    if first == b"{":
        line = sys.stdin.buffer.readline()
        return json.loads(line.decode("utf-8"))

    headers: dict[str, str] = {}
    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            return None
        if raw in (b"\r\n", b"\n"):
            break
        key, _, value = raw.decode("ascii", errors="replace").partition(":")
        if key:
            headers[key.strip().lower()] = value.strip()

    try:
        length = int(headers.get("content-length", "0"))
    except ValueError:
        raise ValueError("bad Content-Length")
    if length <= 0:
        raise ValueError("missing Content-Length")
    body = sys.stdin.buffer.read(length)
    if len(body) != length:
        raise EOFError("short MCP frame")
    return json.loads(body.decode("utf-8"))


def _write_framed_message(out: dict) -> None:
    # MCP stdio transport = **换行分隔 JSON**（每条一行、不含内嵌换行），不是 LSP 的
    # Content-Length 帧。实测 Claude Code 2.1.196 发的是换行分隔、也只认换行分隔的
    # 响应——之前用 Content-Length 写回，client 的 initialize 等不到能解析的响应，
    # 30s 握手超时、工具一个都不注册（mcp-logs-cc-lark 里就是 timeout）。一行一条即可。
    body = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(body + b"\n")
    sys.stdout.buffer.flush()


def main() -> None:
    _log(f"start (callback={_control_base()} thread={os.environ.get('CC_LARK_THREAD_ID','-')[:14]})")
    while True:
        try:
            msg = _read_framed_message()
        except Exception as e:  # noqa: BLE001 — 协议循环绝不能崩
            _log(f"bad MCP frame: {type(e).__name__}: {e}")
            continue
        if msg is None:
            break
        try:
            out = _handle(msg)
        except Exception as e:  # noqa: BLE001 — 协议循环绝不能崩
            _log(f"handler crashed: {type(e).__name__}: {e}")
            out = _error(msg.get("id"), -32603, "internal error") if msg.get("id") is not None else None
        if out is not None:
            _write_framed_message(out)
    _log("stdin closed, exiting")


if __name__ == "__main__":
    main()
