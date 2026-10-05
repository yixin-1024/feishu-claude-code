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

from agent_alias import alias_doc as _alias_doc

_ALIAS_DOC = _alias_doc()
import os
import sys
import urllib.error
import urllib.request
# 置 1 = 派出去的子任务不回报、批次跑完也不唤醒发起方（语音对讲用）。
_NO_REPORT_BACK = (os.environ.get("CC_LARK_NO_REPORT_BACK") or "").strip() in ("1", "true", "yes")

try:
    # 定时任务的删/停/改：纯 stdlib、跟本文件同目录。缺失时只是少 4 个工具，
    # 不能让整个 MCP server 起不来（那会连 wake/dispatch 一起赔进去）。
    import cron_store
except Exception:  # noqa: BLE001
    cron_store = None

try:
    # 工作域路由表（「派到哪个群」）：同样纯 stdlib、同目录。没有它就只是少了
    # workspace 参数（派发退回"当前群"的原行为），不能拖垮整个 server。
    import workspaces as _workspaces
except Exception:  # noqa: BLE001
    _workspaces = None


def _workspace_catalog() -> str:
    """本机配置了哪些工作域（渲染进工具说明）。没配就返回空串 → 不暴露该参数。"""
    if _workspaces is None:
        return ""
    try:
        return _workspaces.catalog_doc()
    except Exception:  # noqa: BLE001
        return ""


_WS_CATALOG = _workspace_catalog()

# 有工作域表才把 workspace 参数 + 路由说明挂出去；没配置时工具说明与从前逐字一致。
_WS_DOC = (
    "WORKSPACE ROUTING (choose the RIGHT Lark group): each configured workspace is one "
    "project = one Lark group = one working directory. Pass `workspace` to run the "
    "sub-task in THAT project's group/dir; omit it to keep the task in the CURRENT group. "
    "**When the user names a project or system, you MUST route to its workspace instead of "
    "defaulting to the current group** —— 用户说「去 KYT 查一下」「SPX 那边改个接口」就传对应的 "
    "workspace，别把活留在当前群（当前群往往是另一摊活、另一个目录）。Configured workspaces:\n"
    + _WS_CATALOG + "\n"
) if _WS_CATALOG else ""

# 参数说明里只列名字（各自是干什么的已经在工具 description 的清单里写过一遍了，
# 再抄一遍纯粹是把同样的几百 token 收两次费）。
_WS_NAMES = " | ".join(_workspaces.names()) if (_workspaces and _WS_CATALOG) else ""

_WS_PARAM = {
    "type": "string",
    "description": (
        "Optional target WORKSPACE (project) — routes this task to that project's Lark "
        "group and working directory. Case/spacing-insensitive, aliases accepted (see the "
        "workspace list in this tool's description). Omit to use the CURRENT group "
        "(previous behaviour). Configured: " + _WS_NAMES
    ),
} if _WS_CATALOG else None

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


def _control_token() -> str:
    """获取 control API 的 Bearer token。

    env 优先（Claude 后端会把 CC_LARK_CONTROL_TOKEN 注入子进程 env，MCP server 随
    Claude 继承拿到）。**codex 后端不会把该 env 透传给它 spawn 的 MCP server**（且
    runner 出于避免 token 落进命令行/ps 的考虑，本就把它从 --mcp-config 的 env 块里
    剔除了），于是 codex 起的这个进程 env 里没有 token → 调 control API 返回 401，
    dispatch/wake/cron 全挂。这里退回读常驻 bot 落盘的 0600 token 文件（同机同用户，
    与 http_server.load_or_create_control_token 同一份 secret），让两种后端都能鉴权，
    且 token 永不出现在命令行里。
    """
    token = (os.environ.get("CC_LARK_CONTROL_TOKEN") or "").strip()
    if token:
        return token
    token_path = os.path.expanduser(
        (os.environ.get("CC_LARK_CONTROL_TOKEN_FILE") or "~/.feishu-claude/control-token").strip()
    )
    try:
        with open(token_path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _control_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = _control_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _allow(flag: str, default: str = "1") -> bool:
    """能力开关：env 里显式 0/false/no/off 才关，否则默认开。关掉的能力对应工具**不注册**
    （对 agent 完全隐形，不是调用时才拒），这是把 agent 自主性显式收口的闸门。"""
    return (os.environ.get(flag, default) or default).strip().lower() not in ("0", "false", "no", "off")


# 三个独立闸门（bot 经 --mcp-config 的 env 块注入；未设=开）：
#   CC_LARK_ALLOW_DISPATCH —— 主动派子 agent / 移交整项任务（dispatch_task +
#                            handover + read_thread + append/steer）
#   CC_LARK_ALLOW_WAKE     —— 定时/等事件自我唤醒（wake_me_in）
#   CC_LARK_ALLOW_CRON     —— 重复定时任务（schedule_cron + list_crons）
_ALLOW_DISPATCH = _allow("CC_LARK_ALLOW_DISPATCH")
_ALLOW_WAKE = _allow("CC_LARK_ALLOW_WAKE")
_ALLOW_CRON = _allow("CC_LARK_ALLOW_CRON")


def _read_body(resp_or_err) -> str:
    """把响应体读成文本；读挂了也不抛（拿不到就当空 body）。"""
    try:
        return (resp_or_err.read() or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _decode_body(where: str, raw: str, status: int = 0) -> dict:
    """把一次 control API 响应压成 dict，**永不抛异常**。

    status=0 表示 2xx；非 0 = 4xx/5xx。bot 侧 `_mcp_respond` 对 ok=false 一律回
    HTTP 400 **并在 body 里带真实原因**，而 urllib 把 4xx 抛成 HTTPError 丢掉 body
    —— 模型只看得到 "HTTP Error 400: Bad Request"，只能反复瞎试（实测连吃 3 个 400）。
    所以这里把 body 解出来：能解析成对象就原样返回（`ok` 强制置 false），让调用方
    照常走既有的 `body.get("ok")` 分支把真实原因透出去；解析不了就退化成一条带
    状态码 + body 片段的可读 error。"""
    try:
        data = json.loads(raw or "{}")
    except Exception:  # noqa: BLE001
        data = None
    snippet = (raw or "").strip()[:400] or "(empty body)"
    if not isinstance(data, dict):
        head = f"HTTP {status} " if status else ""
        return {"ok": False, "error": f"{head}bad response from {where}: {snippet}"}
    if not status:
        return data
    reason = str(data.get("error") or "").strip() or snippet
    return {**data, "ok": False, "error": f"{reason} [HTTP {status} {where}]"}


def _post_json(path: str, payload: dict, timeout: int = 35) -> dict:
    """POST 一个 JSON 给常驻 bot 的本机端点，返回解析后的 dict。

    4xx/5xx **不抛异常**：bot 在 body 里回的 {"ok": false, "error": ...} 被原样带回，
    调用方的 `if not body.get("ok")` 分支就能拿到真实原因（所有调用方同等受益）。
    body 不是 JSON / 读不出来也只退化成可读的 ok=false，绝不抛解析异常。
    连不上 / 超时这类真·传输失败仍向上抛，由调用方的 except 兜底。"""
    req = urllib.request.Request(
        f"{_control_base()}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=_control_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _decode_body(path, _read_body(resp))
    except urllib.error.HTTPError as e:
        return _decode_body(path, _read_body(e), e.code)


def _get_json(path: str, timeout: int = 35) -> dict:
    """GET 一个本机 control 端点（/reload 是 GET）。错误处理同 _post_json。"""
    req = urllib.request.Request(
        f"{_control_base()}{path}", headers=_control_headers(), method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _decode_body(path, _read_body(resp))
    except urllib.error.HTTPError as e:
        return _decode_body(path, _read_body(e), e.code)


def _reload_tasks() -> dict:
    """让常驻 scheduler 重读 scheduled_tasks.yaml 并全量重建 job。

    这是「改完免重启」的关键：/reload 是**早就存在**的端点，所以删/停/改全部能在
    不动 bot 进程的前提下即时生效（bot 侧一行代码都不用加）。"""
    return _get_json("/reload")


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
        "Thread/recipient/profile context is supplied automatically — you do NOT pass it. "
        "A visible notice stating the exact wake time is AUTO-POSTED to this thread, so "
        "the user can see a wake is already queued (and won't manually re-wake you) — you "
        "do NOT need to announce the schedule yourself."
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

CANCEL_WAKE_TOOL = {
    "name": "cancel_wake",
    "description": (
        "Cancel a scheduled wake in THIS Lark thread (or by job_id). "
        "Use this when a prior wake_me_in is no longer needed — for example, "
        "because the user re-engaged and addressed the task early, the waiting condition "
        "(CI/deploy) finished sooner than expected, or the user asked to abort the waiting. "
        "If no arguments are passed, it automatically cancels all pending wakes for the current thread. "
        "Returns details of the cancelled wake(s)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "thread_id": {
                "type": "string",
                "description": (
                    "Optional Lark thread id (omt_…). Defaults to the current thread."
                ),
            },
            "job_id": {
                "type": "string",
                "description": (
                    "Optional specific wake job id (wake-…). If omitted, cancels all pending "
                    "wakes for the thread."
                ),
            },
        },
    },
}

DISPATCH_TASK_TOOL = {
    "name": "dispatch_task",
    "description": (
        "Fan out an INDEPENDENT sub-task to a fresh cc-lark Claude session running in a "
        "NEW thread in a Lark group (the current one by default — see WORKSPACE ROUTING "
        "below to send it to another project's group), then return immediately (fire-and-forget) "
        "with its thread_id. This is the generic multi-agent dispatch primitive: use it to "
        "parallelize a goal across several worker agents, each with its own clean context. "
        "The sub-agent runs fully autonomously; you do NOT block waiting for it — poll its "
        "progress/result later with read_thread(thread_id). Write `prompt` SELF-CONTAINED "
        "(working dir, scope, acceptance criteria, any 'do not touch prod' guards) — the "
        "worker has none of your context. HARD LIMIT: the bot enforces a PER-GROUP concurrency "
        "cap (default 7; excess dispatches in the same group are rejected) — dispatch in waves "
        "and wait for the prior wave before launching the next. The recipient is supplied automatically. "
        + _WS_DOC +
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
        "and wake still come back to YOU regardless of which agent ran the worker. "
        "MODEL/EFFORT: the worker starts a FRESH session and does NOT inherit the /model or "
        "/effort of this thread — it runs on the bot's default model. Pass `model` / `effort` "
        "to pick per-worker, e.g. model=\"opus\" for the heavy implementation worker and "
        "model=\"fable\" for a second opinion, or model=\"haiku\" for cheap grunt work."
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
                    "Accepts a family alias — " + _ALIAS_DOC + " — or an exact loaded "
                    "profile name. Omit to run the worker on your own backend (default). "
                    "When the user names an agent out loud (\"派给 agy\", \"让 GPT 做\", "
                    "\"用 antigravity\"), pass it here instead of silently falling back to "
                    "the default."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional model for THIS worker — an alias ('fable', 'opus', 'sonnet', "
                    "'haiku', 'opusplan', 'codex', 'gemini', ...) or a full model id. Must "
                    "belong to the target agent's backend (don't pass 'fable' with "
                    "agent=\"gpt\"). Omit to use that bot's default model."
                ),
            },
            "effort": {
                "type": "string",
                "description": (
                    "Optional reasoning effort for THIS worker: low / medium / high / xhigh / "
                    "max (codex also: ultra). Omit to use the bot's default."
                ),
            },
        },
        "required": ["prompt"],
    },
}

if _WS_PARAM:
    DISPATCH_TASK_TOOL["inputSchema"]["properties"]["workspace"] = dict(_WS_PARAM)

HANDOVER_TOOL = {
    "name": "handover",
    "description": (
        "HAND OFF THIS WHOLE TASK — with its ownership — to a FRESH agent session in a new "
        "thread, then finish your turn for good. Use this when YOUR OWN context has grown too "
        "large / cluttered to keep working in (or when the task should continue on a different "
        "agent): you manually COMPRESS the state into a structured brief (goal / completed / "
        "remaining / gotchas / key files) and the successor picks the work up from there with a "
        "clean context. "
        "DIFFERENCE FROM dispatch_task — this is the key point: dispatch_task delegates a "
        "SUB-task and the worker reports back to you (completion line + auto-wake with results), "
        "so you must stick around to aggregate. A handover transfers OWNERSHIP: there is NO "
        "report back, NO completion notice and NO wake — the successor talks to the user "
        "DIRECTLY in its own thread and owns the task to the end, while you simply stop. Any "
        "pending wake_me_in on your thread is cancelled automatically so you cannot come back "
        "and fight the successor over the same work. "
        "WRITE THE BRIEF FOR SOMEONE WHO HAS NONE OF YOUR CONTEXT: absolute paths, exact "
        "commands, decisions already made and WHY, dead ends already ruled out, what is verified "
        "vs assumed. It is persisted to a file so the successor can re-read it later, and it is "
        "also posted in the new thread so the user can see what was handed over. Keep it "
        "compressed (a briefing, not a context dump) — oversized briefs are rejected. "
        "CROSS-AGENT: pass `agent` to hand the task to a DIFFERENT backend — e.g. agent=\"gpt\" "
        "hands it to the codex(GPT) bot, or \"gemini\"/\"mimo\"/an exact profile name. The "
        "target agent's bot must be a member of this group. "
        "After a successful handover: tell the user in one line that the task moved (include the "
        "returned thread_id) and END YOUR TURN — do not keep working on it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "What the task is trying to achieve and how it will be judged done "
                    "(acceptance criteria). Self-contained — no references to 'the above'."
                ),
            },
            "completed": {
                "type": "string",
                "description": (
                    "What is already DONE (and where the evidence is: files changed, tests "
                    "passing, things deployed). Write '（无，刚开始）' if genuinely nothing yet."
                ),
            },
            "remaining": {
                "type": "string",
                "description": (
                    "What is still LEFT, in the order it should be tackled — the actual work "
                    "the successor is taking over."
                ),
            },
            "notes": {
                "type": "string",
                "description": (
                    "Optional but strongly recommended: gotchas, constraints, decisions already "
                    "made and why, approaches already ruled out, 'do not touch prod' guards, "
                    "credentials/env quirks."
                ),
            },
            "files": {
                "type": "string",
                "description": (
                    "Optional: key absolute file paths, working directory, commands to run "
                    "(build/test/deploy), and artifacts produced so far."
                ),
            },
            "title": {
                "type": "string",
                "description": "Optional short thread title (defaults to the first line of `goal`).",
            },
            "agent": {
                "type": "string",
                "description": (
                    "Optional target agent/backend to hand the task to (CROSS-AGENT handover). "
                    "Family alias — " + _ALIAS_DOC + " — or an exact loaded profile name. "
                    "Omit to hand it to a fresh session of your own backend."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Optional model for the successor — alias ('opus', 'fable', 'sonnet', "
                    "'haiku', ...) or full id. Must belong to the target agent's backend. Omit "
                    "for that bot's default."
                ),
            },
            "effort": {
                "type": "string",
                "description": (
                    "Optional reasoning effort for the successor: low / medium / high / xhigh / "
                    "max (codex also: ultra)."
                ),
            },
        },
        "required": ["goal", "completed", "remaining"],
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

APPEND_TASK_TOOL = {
    "name": "append_to_task",
    "description": (
        "Append a follow-up instruction to a running (or idle) sub-task session in a thread, "
        "WITHOUT interrupting whatever it is currently doing. The message is QUEUED: it runs "
        "after the sub-agent finishes its current turn, resuming the SAME session so full "
        "context/progress is preserved. Use this to add a next step, extra requirement, or "
        "clarification while letting the current step complete. `thread_id` is the omt_… "
        "returned by dispatch_task. For steering a task that has gone off-course and should be "
        "REDIRECTED NOW (stop the current work first), use steer_task instead. Target "
        "group/recipient are supplied automatically. Execution stays with the original "
        "task's bot, including cross-agent tasks; unresolved ownership is rejected."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "thread_id": {
                "type": "string",
                "description": "The thread id (omt_…) of the sub-task, returned by dispatch_task.",
            },
            "message": {
                "type": "string",
                "description": "The follow-up instruction to append (runs after the current turn).",
            },
        },
        "required": ["thread_id", "message"],
    },
}

STEER_TASK_TOOL = {
    "name": "steer_task",
    "description": (
        "STOP a sub-task's current run immediately and redirect it with a NEW instruction "
        "(real-time steering). Use when the sub-agent has gone off-course / picked the wrong "
        "approach and you want to interrupt and correct it NOW rather than wait for it to "
        "finish. The interrupted turn's progress card is preserved (marked stopped), then the "
        "SAME session is resumed with your new instruction so prior context/work carries over "
        "(the new instruction is prefixed with a note that the previous step was interrupted). "
        "`thread_id` is the omt_… returned by dispatch_task. To add a step WITHOUT interrupting "
        "the current work, use append_to_task instead. Target group/recipient supplied automatically."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "thread_id": {
                "type": "string",
                "description": "The thread id (omt_…) of the sub-task, returned by dispatch_task.",
            },
            "message": {
                "type": "string",
                "description": "The new instruction to redirect the sub-agent with (after stopping its current run).",
            },
        },
        "required": ["thread_id", "message"],
    },
}

SCHEDULE_CRON_TOOL = {
    "name": "schedule_cron",
    "description": (
        "Create a RECURRING scheduled task (e.g. 'every day at 9am do X'). At each "
        "scheduled time the cc-lark bot opens a fresh turn in a NEW thread in the current "
        "group and runs `prompt`. This is the persistent 'cron' kind — it survives bot "
        "restarts (written to the bot's task config). For a ONE-OFF 'come back in N minutes' "
        "use wake_me_in instead. `cron` is a 5-field expression: minute hour "
        "day-of-month month day-of-week (timezone Asia/Shanghai), e.g. '0 9 * * *' = daily "
        "09:00, '*/30 * * * *' = every 30 min, '0 9 * * mon' = Mondays 09:00, "
        "'0 9 * * mon-fri' = weekdays 09:00. ⚠️ Day-of-week NUMBERS are NOT standard "
        "crontab here: 0=Monday, 1=Tuesday … 6=Sunday (7 is rejected), so '1' means "
        "Tuesday. Prefer the names mon/tue/wed/thu/fri/sat/sun, and check next_run in the "
        "reply. Write `prompt` "
        "SELF-CONTAINED (each run is a fresh session). The recipient is supplied "
        "automatically. "
        + (
            "`workspace` picks WHICH GROUP the task recurs in (same routing table as "
            "dispatch_task; omit = current group). NOTE: a cron created into another "
            "workspace belongs to THAT group — list_crons / pause / cancel only see the "
            "群 they are called from, so manage it from there. "
            if _WS_CATALOG else ""
        )
        + "Because each run is a fresh session it does NOT inherit any /model "
        "or /effort set in this thread — pass `model` / `effort` if the task needs a "
        "specific model or reasoning depth. Returns the task name + next run time; use "
        "list_crons to review."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "cron": {"type": "string", "description": "5-field cron: 'minute hour dom month dow' (Asia/Shanghai). dow: prefer names (mon, mon-fri); numbers are 0=Mon … 6=Sun, NOT standard crontab."},
            "prompt": {"type": "string", "description": "Self-contained instruction run at each scheduled time."},
            "title": {"type": "string", "description": "Optional short title for the recurring topic."},
            "model": {
                "type": "string",
                "description": (
                    "Optional model for each run — an alias ('opus', 'sonnet', 'haiku', "
                    "'codex', ...) or a full model id. Omit to use the bot's default."
                ),
            },
            "effort": {
                "type": "string",
                "description": (
                    "Optional reasoning effort for each run: low / medium / high / xhigh / "
                    "max (codex also: ultra). Omit to use the bot's default."
                ),
            },
        },
        "required": ["cron", "prompt"],
    },
}

if _WS_PARAM:
    SCHEDULE_CRON_TOOL["inputSchema"]["properties"]["workspace"] = dict(_WS_PARAM)

LIST_CRONS_TOOL = {
    "name": "list_crons",
    "description": (
        "List scheduled jobs registered in the cc-lark bot for THIS chat (recurring crons "
        "you created via schedule_cron, plus any one-off wakes), with their next run time."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}

_NAME_PROP = {
    "type": "string",
    "description": "Exact task name as shown by list_crons (e.g. 'agent_cron_1784525729_4f9a').",
}

CANCEL_CRON_TOOL = {
    "name": "cancel_cron",
    "description": (
        "PERMANENTLY delete a recurring scheduled task by name. Takes effect immediately "
        "(no bot restart). Only tasks belonging to THIS chat can be cancelled. The removed "
        "entry is archived under data/agent_crons/removed/ so it can be restored by hand. "
        "To stop a task only temporarily, use pause_cron instead."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"name": _NAME_PROP},
        "required": ["name"],
    },
}

PAUSE_CRON_TOOL = {
    "name": "pause_cron",
    "description": (
        "Temporarily stop a recurring task without deleting it — it stops firing right away "
        "(no bot restart) and stays paused across restarts. Resume later with resume_cron. "
        "Paused tasks are shown by list_crons under a 'paused' section. Only tasks belonging "
        "to THIS chat can be paused."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"name": _NAME_PROP},
        "required": ["name"],
    },
}

RESUME_CRON_TOOL = {
    "name": "resume_cron",
    "description": (
        "Re-activate a task previously paused with pause_cron. It starts firing again "
        "immediately (no bot restart), on its original cron schedule."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"name": _NAME_PROP},
        "required": ["name"],
    },
}

UPDATE_CRON_TOOL = {
    "name": "update_cron",
    "description": (
        "Edit an existing recurring task in place — change its schedule, its prompt, its "
        "topic title, or the model/effort it runs with. Only the fields you pass are "
        "touched; everything else (and the surrounding config file) is left untouched. "
        "Takes effect immediately (no bot restart). If the new cron is invalid the change "
        "is rolled back and an error is returned. Works on paused tasks too. Only tasks "
        "belonging to THIS chat can be edited."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": _NAME_PROP,
            "cron": {"type": "string", "description": "New 5-field cron: 'minute hour dom month dow' (Asia/Shanghai). dow: prefer names (mon, mon-fri); numbers are 0=Mon … 6=Sun, NOT standard crontab."},
            "prompt": {"type": "string", "description": "New self-contained instruction to run at each scheduled time (replaces the old one)."},
            "title": {"type": "string", "description": "New topic title for each run."},
            "model": {
                "type": "string",
                "description": (
                    "New model for each run ('opus', 'sonnet', 'haiku', 'codex', ... or a "
                    "full model id). Pass an empty string to clear the override and fall "
                    "back to the bot default."
                ),
            },
            "effort": {
                "type": "string",
                "description": (
                    "New reasoning effort: low / medium / high / xhigh / max. Pass an empty "
                    "string to clear the override."
                ),
            },
        },
        "required": ["name"],
    },
}

TASK_RESULT_TOOL = {
    "name": "get_task_result",
    "description": "Read authoritative running/completed/failed/cancelled status and result of a task dispatched by this caller. Use thread_id from dispatch_task. Does not start or resume any task.",
    "inputSchema": {"type": "object", "properties": {
        "thread_id": {"type": "string"}}, "required": ["thread_id"]},
}

# 按闸门装配 tools/list —— 关掉的能力这里就不出现，agent 看都看不到。
TOOLS = []
if _ALLOW_WAKE:
    TOOLS.append(WAKE_TOOL)
    TOOLS.append(CANCEL_WAKE_TOOL)
if _ALLOW_DISPATCH:
    TOOLS += [DISPATCH_TASK_TOOL, HANDOVER_TOOL, READ_THREAD_TOOL,
              APPEND_TASK_TOOL, STEER_TASK_TOOL, TASK_RESULT_TOOL]
if _ALLOW_CRON:
    TOOLS += [SCHEDULE_CRON_TOOL, LIST_CRONS_TOOL]
    # 删/停/改靠本地改 yaml + /reload 实现，没有 cron_store 就整组不暴露
    if cron_store is not None:
        TOOLS += [CANCEL_CRON_TOOL, PAUSE_CRON_TOOL, RESUME_CRON_TOOL, UPDATE_CRON_TOOL]


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
        f"A visible notice with this wake time was posted to the thread, so no need to "
        f"announce it yourself. You can END THE TURN now — a fresh turn will continue "
        f"with your note."
    )


def _tool_cancel_wake(args: dict) -> dict:
    """取消当前话题或指定 job_id 的定时唤醒任务。"""
    job_id = (args.get("job_id") or "").strip() or None
    thread_id = (args.get("thread_id") or "").strip() or None

    # 如果两者都没传，默认使用当前话题
    if not thread_id and not job_id:
        thread_id = (os.environ.get("CC_LARK_THREAD_ID") or "").strip() or None

    chat_id = (os.environ.get("CC_LARK_CHAT_ID") or "").strip() or None
    profile = (os.environ.get("CC_LARK_PROFILE") or "").strip() or None

    if not thread_id and not job_id:
        return _err(
            "No Lark thread context available. Pass `thread_id` or `job_id` explicitly."
        )

    payload = {
        "profile": profile,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "job_id": job_id,
    }
    try:
        body = _post_json("/wake/cancel", payload, timeout=10)
    except Exception as e:
        _log(f"/wake/cancel POST failed: {type(e).__name__}: {e}")
        return _err(f"Failed to reach cc-lark scheduler: {type(e).__name__}: {e}")

    if not body.get("ok"):
        return _err(f"Scheduler rejected the cancel request: {body.get('error', 'unknown error')}")

    count = body.get("count", 0)
    if count == 0:
        return _ok("ℹ️ No pending wake found for the specified criteria.")

    cancelled = body.get("cancelled", [])
    lines = [f"🛑 Successfully cancelled {count} scheduled wake(s):"]
    for c in cancelled:
        jid = c.get("job_id", "")
        note = c.get("note", "")
        fire_at = c.get("fire_at", "")
        lines.append(f"- [{jid}] fire_at={fire_at}, note={note!r}")

    return _ok("\n".join(lines))


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": f"⚠️ {text}"}], "isError": True}


def _resolve_workspace(args: dict) -> tuple[str, dict | None]:
    """把 `workspace` 参数预检一遍。返回 (名字, 错误 result)。

    真正的解析在 bot 侧（http_server 才是权威：chat_id / cwd 都以路由表为准，
    不信客户端传来的值）。这里只是**提前失败**——名字打错时当场把可选清单回给模型，
    而不是让它吃一个 HTTP 400 再猜。
    """
    spec = (args.get("workspace") or args.get("target") or "").strip()
    if not spec:
        return "", None
    if _workspaces is None:
        return "", _err("本机没有工作域路由表，无法按项目分流；去掉 workspace 参数即派在当前群。")
    try:
        ws, err = _workspaces.resolve(spec)
    except Exception as e:  # noqa: BLE001
        return "", _err(f"工作域解析失败: {type(e).__name__}: {e}")
    if ws is None:
        return "", _err(err)
    return ws.name, None


def _stale_routing_warning(workspace: str, body: dict) -> str:
    """bot 还在跑旧代码时，workspace 会被它整个忽略——活其实落在了当前群。

    识破方式：新版 bot 一定会在响应里**回显** workspace；旧版没有这个字段。
    这里不报 isError（活真的已经派出去了，报错只会引诱模型再派一遍），而是在回执
    最前面顶一行醒目警告，让模型如实转告用户去 /restart。
    """
    if not workspace or body.get("workspace") == workspace:
        return ""
    return (
        f"⚠️ 路由未生效：目标工作域 «{workspace}» 被 bot 忽略了，任务实际落在了**当前群/当前目录**。"
        f"原因几乎一定是常驻 bot 还在跑旧代码 —— 让用户发 /restart 后再重试一次，"
        f"并且**别假装派对了群**。\n"
    )


def _tool_dispatch_task(args: dict) -> dict:
    """派一个独立子会话到目标群的新 thread（fan-out）。

    目标群默认是**当前群**（env 里的 CC_LARK_CHAT_ID，原行为）；传了 workspace 就
    改派到那个工作域的群、并把子会话钉在该工作域的目录里（bot 侧按路由表落）。
    """
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _err("`prompt` must be a non-empty string.")
    workspace, ws_err = _resolve_workspace(args)
    if ws_err:
        return ws_err
    chat_id = (os.environ.get("CC_LARK_CHAT_ID") or "").strip()
    profile = (os.environ.get("CC_LARK_PROFILE") or "").strip()
    user_id = (os.environ.get("CC_LARK_USER_ID") or "").strip()
    if not chat_id and not workspace:
        return _err(
            "No Lark group context — dispatch_task only works inside a cc-lark group "
            "session. (CC_LARK_CHAT_ID unset.)"
        )
    payload = {
        "profile": profile,
        # 调用方自己所在的群。目标群由 bot 侧按 workspace 定；不传 workspace 时就是这个。
        "chat_id": chat_id,
        "workspace": workspace,
        "user_id": user_id,
        "title": (args.get("title") or "").strip(),
        "prompt": prompt.strip(),
        # 跨 agent：可选目标后端（"gpt"/"gemini"/"mimo"/profile 名）；空=同 agent
        "agent": (args.get("agent") or "").strip(),
        # 子会话是全新 session，不继承本 thread 的 /model /effort；空=目标 bot 默认
        "model": (args.get("model") or "").strip(),
        "effort": (args.get("effort") or "").strip(),
        # 父上下文：让 bot 在子会话结束后回报本 thread + 批次全完时唤醒我（主 agent）
        # 父上下文 = 回报闭环的开关（bot 侧 `if parent_thread and parent_anchor` 才登记）。
        # 语音对讲这类「派完就挂电话」的场景要把它关掉：否则活明明派去了别的工作域，
        # 子任务回报和批次汇总却全灌回发起方的话题里。同 handover 的取舍。
        "parent_thread": "" if _NO_REPORT_BACK else (os.environ.get("CC_LARK_THREAD_ID") or "").strip(),
        "parent_anchor": "" if _NO_REPORT_BACK else (
            os.environ.get("CC_LARK_ANCHOR") or os.environ.get("CC_LARK_MESSAGE_ID") or "").strip(),
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
    agent_note += "".join(
        f" ({k}={body.get(k)})" for k in ("model", "effort") if body.get(k)
    )
    stale = _stale_routing_warning(workspace, body)
    where = ""
    if body.get("workspace"):
        where = f" in workspace «{body['workspace']}»"
        if body.get("cwd"):
            where += f" ({body['cwd']})"
    result = _ok(
        stale +
        f"✅ Dispatched a sub-agent{agent_note}{where} in a new thread. thread_id={body.get('thread_id')} "
        f"(active {body.get('active_after')}/{body.get('cap')} in that group). "
        f"It runs autonomously — poll its progress/result later with "
        f"read_thread(thread_id=\"{body.get('thread_id')}\")."
    )
    result["structuredContent"] = {k: body.get(k) for k in (
        "thread_id", "chat_id", "agent", "workspace", "anchor_message_id")}
    try:
        import task_results
        available = task_results.get(body.get("thread_id"), profile=profile,
                                     user_id=user_id).get("status") != "unavailable"
    except Exception:
        available = False
    result["structuredContent"]["callback_available"] = available
    if not available:
        result["content"][0]["text"] += (
            "\nCompletion callback is not available for this task. "
            "Do not promise an automatic voice notification; the cc-lark bot may need /restart.")
    return result


def _tool_get_task_result(args: dict) -> dict:
    import task_results
    try:
        record = task_results.get(args.get("thread_id"),
                                  profile=os.environ.get("CC_LARK_PROFILE", ""),
                                  user_id=os.environ.get("CC_LARK_USER_ID", ""))
    except (ValueError, PermissionError) as e:
        return _err(str(e))
    except Exception:
        return _err("Task result store is unavailable; do not infer task completion.")
    return {**_ok(json.dumps(record, ensure_ascii=False)), "structuredContent": record}


def _tool_handover(args: dict) -> dict:
    """把整项任务移交给一个全新会话（可跨 agent），移交方随后收工。

    与 _tool_dispatch_task 的差别在**语义**而非管道：这里不传父上下文（parent_thread /
    parent_anchor），所以 bot 侧不登记回报闭环——接手方对用户负责，不回报移交方。
    """
    def _field(name: str):
        # 模型也爱把 completed / remaining 写成 list；原样转给 bot 侧统一归一。
        value = args.get(name)
        return value.strip() if isinstance(value, str) else value

    brief = {k: _field(k) for k in ("goal", "completed", "remaining", "notes", "files")}
    missing = [k for k in ("goal", "completed", "remaining") if not brief.get(k)]
    if missing:
        return _err(
            f"handover requires a real brief — missing: {', '.join(missing)}. "
            "The successor has NONE of your context: state the goal + acceptance criteria, "
            "what is already done (with evidence), and what is left to do."
        )
    chat_id = (os.environ.get("CC_LARK_CHAT_ID") or "").strip()
    profile = (os.environ.get("CC_LARK_PROFILE") or "").strip()
    user_id = (os.environ.get("CC_LARK_USER_ID") or "").strip()
    if not chat_id:
        return _err(
            "No Lark group context — handover only works inside a cc-lark group session. "
            "(CC_LARK_CHAT_ID unset.)"
        )
    payload = {
        "profile": profile,
        "chat_id": chat_id,
        "user_id": user_id,
        "title": (args.get("title") or "").strip(),
        "brief": brief,
        # 跨 agent：可选目标后端（"gpt"/"gemini"/"mimo"/profile 名）；空=同 agent 的新会话
        "agent": (args.get("agent") or "").strip(),
        "model": (args.get("model") or "").strip(),
        "effort": (args.get("effort") or "").strip(),
        # 移交方所在话题：给 bot 用来取消本话题待触发的唤醒 + 贴一条移交标记。
        # **不是** parent_thread —— 移交没有回报闭环，这点正是它与 dispatch_task 的分界。
        "from_thread": (os.environ.get("CC_LARK_THREAD_ID") or "").strip(),
        "from_anchor": (os.environ.get("CC_LARK_ANCHOR") or os.environ.get("CC_LARK_MESSAGE_ID") or "").strip(),
    }
    try:
        body = _post_json("/handover_task", payload)
    except Exception as e:  # noqa: BLE001
        _log(f"/handover_task POST failed: {type(e).__name__}: {e}")
        return _err(f"Failed to reach cc-lark dispatcher: {type(e).__name__}: {e}")
    if not body.get("ok"):
        return _err(f"Handover rejected: {body.get('error', 'unknown error')}")
    where = f"{body.get('agent')}[{body.get('agent_runner')}]"
    where += "".join(f" ({k}={body.get(k)})" for k in ("model", "effort") if body.get(k))
    extra = ""
    if body.get("cancelled_wakes"):
        extra += f" Cancelled {body['cancelled_wakes']} pending wake(s) on your thread."
    if body.get("brief_path"):
        extra += " Brief persisted for the successor to re-read."
    return _ok(
        f"✅ Handed this task over to {where} in a new thread. "
        f"thread_id={body.get('thread_id')} (active {body.get('active_after')}/{body.get('cap')}).{extra} "
        f"A notice with the new thread id was posted to this thread, so just tell the user in "
        f"one line that the task moved there — then END YOUR TURN. The successor owns it now "
        f"and will NOT report back to you; do not keep working on it or wait for it."
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


def _post_steer(args: dict, *, stop_first: bool, verb: str) -> dict:
    """append_to_task / steer_task 的共享后端：POST /steer 给常驻 bot。
    stop_first=False=追加不打断；True=停当前 run 再按新指令续跑。"""
    thread_id = (args.get("thread_id") or "").strip()
    message = args.get("message")
    if not thread_id:
        return _err("`thread_id` is required (the omt_… returned by dispatch_task).")
    if not isinstance(message, str) or not message.strip():
        return _err("`message` must be a non-empty string.")
    chat_id = (os.environ.get("CC_LARK_CHAT_ID") or "").strip()
    profile = (os.environ.get("CC_LARK_PROFILE") or "").strip()
    user_id = (os.environ.get("CC_LARK_USER_ID") or "").strip()
    if not chat_id:
        return _err(f"No Lark group context — {verb} only works inside a cc-lark group session.")
    payload = {
        "profile": profile, "chat_id": chat_id, "user_id": user_id,
        "thread_id": thread_id, "instruction": message.strip(), "stop_first": stop_first,
    }
    try:
        body = _post_json("/steer", payload)
    except Exception as e:  # noqa: BLE001
        _log(f"/steer POST failed: {type(e).__name__}: {e}")
        return _err(f"Failed to reach cc-lark: {type(e).__name__}: {e}")
    if not body.get("ok"):
        return _err(f"{verb} rejected: {body.get('error', 'unknown error')}")
    if stop_first:
        detail = "stopped its current run and redirected it" if body.get("stopped") else \
                 "no run was active — it will run the new instruction directly"
    else:
        detail = "queued after the current run" if body.get("queued") else \
                 "no run was active — it will run directly"
    return _ok(
        f"✅ {verb} delivered to thread {body.get('thread_id') or thread_id} "
        f"by task owner {body.get('agent') or '(unspecified)'} ({detail}). "
        f"Poll read_thread(thread_id=\"{thread_id}\") to see how it continues."
    )


def _tool_append_to_task(args: dict) -> dict:
    """给某 thread 的子会话追加一条指令，不打断当前 run（排在其后，resume 同一会话）。"""
    return _post_steer(args, stop_first=False, verb="append_to_task")


def _tool_steer_task(args: dict) -> dict:
    """停掉某 thread 子会话的当前 run，再按新指令 resume 续跑（实时纠偏）。"""
    return _post_steer(args, stop_first=True, verb="steer_task")


def _tool_schedule_cron(args: dict) -> dict:
    """新增一条重复定时任务。cron + prompt 来自 args；profile/chat/user 取自 env。"""
    cron = (args.get("cron") or "").strip()
    prompt = args.get("prompt")
    if not cron:
        return _err("`cron` is required (5-field: minute hour dom month dow).")
    if not isinstance(prompt, str) or not prompt.strip():
        return _err("`prompt` must be a non-empty string.")
    workspace, ws_err = _resolve_workspace(args)
    if ws_err:
        return ws_err
    chat_id = (os.environ.get("CC_LARK_CHAT_ID") or "").strip()
    profile = (os.environ.get("CC_LARK_PROFILE") or "").strip()
    user_id = (os.environ.get("CC_LARK_USER_ID") or "").strip()
    if not chat_id and not workspace:
        return _err("No Lark group context — schedule_cron only works inside a cc-lark group session.")
    payload = {
        "profile": profile, "chat_id": chat_id, "user_id": user_id,
        # 空 = 在当前群循环；给了就按路由表改到那个工作域的群（bot 侧权威解析）
        "workspace": workspace,
        "cron": cron, "prompt": prompt.strip(), "title": (args.get("title") or "").strip(),
        "model": (args.get("model") or "").strip(),
        "effort": (args.get("effort") or "").strip(),
    }
    try:
        body = _post_json("/schedule_cron", payload)
    except Exception as e:  # noqa: BLE001
        _log(f"/schedule_cron POST failed: {type(e).__name__}: {e}")
        return _err(f"Failed to reach cc-lark scheduler: {type(e).__name__}: {e}")
    if not body.get("ok"):
        return _err(f"schedule_cron rejected: {body.get('error', 'unknown error')}")
    over = "".join(
        f", {k}={body.get(k)}" for k in ("model", "effort") if body.get(k)
    )
    stale = _stale_routing_warning(workspace, body)
    where = f" in workspace «{workspace}»" if workspace else ""
    hint = (" It belongs to THAT group — list/pause/cancel it from there."
            if workspace else " Use list_crons to review.")
    return _ok(
        stale +
        f"✅ Recurring task created{where}: {body.get('name')} — cron '{body.get('cron')}'{over}, "
        f"next run {body.get('next_run')}. It survives restarts.{hint}"
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
    sections = []
    if jobs:
        sections.append("Scheduled jobs:\n" + "\n".join(
            f"- {j.get('name')}　next={j.get('next_run')}"
            + ("　(agent)" if j.get("agent_created") else "")
            for j in jobs
        ))
    # 暂停中的任务已从 scheduler 摘掉，只有本地 sidecar 知道，得在这边补上。
    paused = []
    if cron_store is not None:
        try:
            paused = cron_store.list_paused(chat_id)
        except Exception as e:  # noqa: BLE001 —— 列不出来也不该让 list_crons 整个失败
            _log(f"list_paused failed: {type(e).__name__}: {e}")
    if paused:
        sections.append("Paused (not firing — resume_cron to re-activate):\n" + "\n".join(
            f"- {p['name']}　cron='{p['cron']}'"
            + (f"　{p['title']}" if p.get("title") else "")
            for p in paused
        ))
    if not sections:
        return _ok("No scheduled jobs.")
    return _ok("\n\n".join(sections))


def _cron_chat() -> str:
    return (os.environ.get("CC_LARK_CHAT_ID") or "").strip()


def _cron_mutate(op, name: str, **kwargs) -> dict:
    """删/停/改的公共外壳：校验入参 → 调 cron_store → 统一错误呈现。

    真正的落盘与生效在 cron_store：改 scheduled_tasks.yaml 后调既有的 /reload，
    常驻 bot 无需重启。"""
    if cron_store is None:
        return _err("cron management is unavailable (cron_store module missing).")
    if not isinstance(name, str) or not name.strip():
        return _err("`name` must be a non-empty task name (see list_crons).")
    chat_id = _cron_chat()
    if not chat_id:
        return _err("No Lark group context — cron management only works inside a cc-lark group session.")
    try:
        res = op(name.strip(), chat_id=chat_id, reload_fn=_reload_tasks, **kwargs)
    except Exception as e:  # noqa: BLE001
        _log(f"cron mutate {op.__name__} failed: {type(e).__name__}: {e}")
        return _err(f"{op.__name__} failed: {type(e).__name__}: {e}")
    if not res.get("ok"):
        return _err(res.get("error", "unknown error"))
    return res


def _tool_cancel_cron(args: dict) -> dict:
    res = _cron_mutate(cron_store.cancel if cron_store else None, args.get("name"))
    if res.get("isError"):
        return res
    return _ok(
        f"✅ Deleted recurring task {res['name']} — effective now, no restart needed. "
        f"(Archived to {res.get('stash')} in case you need it back.)"
    )


def _tool_pause_cron(args: dict) -> dict:
    res = _cron_mutate(cron_store.pause if cron_store else None, args.get("name"))
    if res.get("isError"):
        return res
    return _ok(
        f"⏸️ Paused {res['name']} — it stops firing now (and stays paused across "
        f"restarts). Use resume_cron to re-activate."
    )


def _tool_resume_cron(args: dict) -> dict:
    res = _cron_mutate(cron_store.resume if cron_store else None, args.get("name"))
    if res.get("isError"):
        return res
    return _ok(f"▶️ Resumed {res['name']} — firing again on its original schedule.")


def _tool_update_cron(args: dict) -> dict:
    # 只把「显式传了的」字段往下送：没传 = 不动，传空串 = 清掉（model/effort）。
    fields = {k: args[k] for k in ("cron", "prompt", "title", "model", "effort")
              if isinstance(args.get(k), str)}
    if not fields:
        return _err("Pass at least one of: cron / prompt / title / model / effort.")
    res = _cron_mutate(cron_store.update if cron_store else None, args.get("name"), **fields)
    if res.get("isError"):
        return res
    changed = ", ".join(res.get("changed") or fields.keys())
    if res.get("paused"):
        return _ok(f"✅ Updated paused task {res['name']} ({changed}). It stays paused — resume_cron to run it.")
    return _ok(f"✅ Updated {res['name']} ({changed}) — effective now, no restart needed.")


# 只登记开着的能力（防御纵深：闸门关掉的工具即便被硬调也 Unknown tool 拒绝）。
_HANDLERS = {}
if _ALLOW_WAKE:
    _HANDLERS["wake_me_in"] = _tool_wake_me_in
    _HANDLERS["cancel_wake"] = _tool_cancel_wake
if _ALLOW_DISPATCH:
    _HANDLERS["dispatch_task"] = _tool_dispatch_task
    _HANDLERS["get_task_result"] = _tool_get_task_result
    _HANDLERS["handover"] = _tool_handover
    _HANDLERS["read_thread"] = _tool_read_thread
    _HANDLERS["append_to_task"] = _tool_append_to_task
    _HANDLERS["steer_task"] = _tool_steer_task
if _ALLOW_CRON:
    _HANDLERS["schedule_cron"] = _tool_schedule_cron
    _HANDLERS["list_crons"] = _tool_list_crons
    if cron_store is not None:
        _HANDLERS["cancel_cron"] = _tool_cancel_cron
        _HANDLERS["pause_cron"] = _tool_pause_cron
        _HANDLERS["resume_cron"] = _tool_resume_cron
        _HANDLERS["update_cron"] = _tool_update_cron


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
