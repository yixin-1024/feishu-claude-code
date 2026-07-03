"""卡片回调 + 控制端点的 HTTP 服务。

提供端点：
    POST /callback        Lark 卡片按钮回调（也可被 url_verification 探测）
    POST /spawn           （仅本机）派单进新 session
    GET  /spawn           （仅本机）派单（query string 版本）
    POST /trigger         （仅本机）手动触发已注册的 cron 任务
    GET  /trigger         （仅本机）同上
    POST /reload          （仅本机）热重载 scheduled_tasks.yaml
    GET  /reload          （仅本机）同上
    GET  /handover        （仅本机）CLI session 接管

设计：本模块**不 import** dispatcher / business logic，所有业务回调通过
`configure(...)` 注入，避免循环 import + 让单元测试只 mock callbacks 即可。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse, parse_qs

from bot_instance import BotInstance
from log_util import log


# ── 注入点 ────────────────────────────────────────────────────

@dataclass
class HttpHandlers:
    """业务回调集合。configure() 注入。"""
    handle_spawn: Callable[..., Awaitable[None]]
    handle_handover: Callable[..., Awaitable[dict]]
    handle_set_mode: Callable[..., Awaitable[None]]
    handle_menu_command: Callable[..., Awaitable[None]]
    handle_resume_session: Callable[..., Awaitable[None]]
    handle_button_reply: Callable[..., Awaitable[None]]
    fire_task: Callable[[str], Awaitable[None]]
    list_tasks: Callable[[], list]
    reload_tasks: Callable[[], dict]
    # 同步：把"N 分钟后唤醒本话题"挂到常驻 scheduler（cc_mcp_server 的 wake_me_in 后端）
    schedule_wake: Callable[..., dict]
    # async：通用多 agent 派发 / 监工（cc_mcp_server 的 dispatch_task / read_thread 后端）
    dispatch_task: Callable[..., Awaitable[dict]]
    read_thread: Callable[..., Awaitable[dict]]
    # 同步：重复定时任务（cc_mcp_server 的 schedule_cron / list_crons 后端）
    schedule_cron: Callable[..., dict]
    list_crons: Callable[..., dict]


_bot_loop: Optional[asyncio.AbstractEventLoop] = None
_bots: dict[str, BotInstance] = {}
_handlers: Optional[HttpHandlers] = None


def configure(
    *,
    bot_loop: asyncio.AbstractEventLoop,
    bots: dict[str, BotInstance],
    handlers: HttpHandlers,
) -> None:
    """主入口在启动时调一次，注入运行时状态 + 业务回调。"""
    global _bot_loop, _bots, _handlers
    _bot_loop = bot_loop
    _bots = bots
    _handlers = handlers


# ── 内部工具 ─────────────────────────────────────────────────

def _is_localhost(client_address) -> bool:
    """HTTP 客户端是否来自本机（防止 /spawn 被 ngrok 暴露公网调用）"""
    try:
        host = client_address[0]
    except Exception:
        return False
    return host in ("127.0.0.1", "::1", "localhost")


def _resolve_bot_from_value(value: dict) -> Optional[BotInstance]:
    """从按钮 value.profile 取 BotInstance；兼容老卡片（无 profile 字段）时取第一个 profile。"""
    name = value.get("profile") if isinstance(value, dict) else None
    if name and name in _bots:
        return _bots[name]
    # 老卡片 fallback：只有一个 profile 时用它，否则放弃
    if len(_bots) == 1:
        return next(iter(_bots.values()))
    return None


def _resolve_spawn_request(
    params: dict,
) -> tuple[Optional[BotInstance], str, dict, Optional[dict]]:
    """从扁平 dict 解析 spawn 参数。

    返回 (bot, user_id, kwargs, error_dict)。error_dict 非空 = 参数有误，回 HTTP 400。
    """
    chat_id_raw = (params.get("chat_id") or "").strip()
    thread_id = (params.get("thread_id") or "").strip()
    anchor_message_id = (params.get("anchor_message_id") or "").strip()
    prompt = params.get("prompt") or ""
    profile_name = (params.get("profile") or "").strip()
    user_id_in = (params.get("user_id") or "").strip()
    model = (params.get("model") or "").strip()

    missing = [
        n for n, v in (
            ("chat_id", chat_id_raw),
            ("thread_id", thread_id),
            ("anchor_message_id", anchor_message_id),
            ("prompt", prompt),
        ) if not v
    ]
    if missing:
        return None, "", {}, {
            "ok": False,
            "error": f"missing required params: {', '.join(missing)}",
        }

    bot: Optional[BotInstance] = None
    if profile_name:
        bot = _bots.get(profile_name)
        if bot is None:
            return None, "", {}, {
                "ok": False,
                "error": f"profile {profile_name!r} not loaded",
            }
    else:
        if len(_bots) == 1:
            bot = next(iter(_bots.values()))
        else:
            return None, "", {}, {
                "ok": False,
                "error": "profile required when multiple bots loaded",
            }

    user_id = user_id_in or bot.store.find_primary_user() or ""
    if not user_id:
        return None, "", {}, {
            "ok": False,
            "error": f"no user found in profile {bot.profile.name}, pass user_id",
        }

    return bot, user_id, {
        "chat_id_raw": chat_id_raw,
        "thread_id": thread_id,
        "anchor_message_id": anchor_message_id,
        "prompt": prompt,
        "model": model,
    }, None


def _submit(coro: Awaitable) -> None:
    """把 coroutine 提交到 bot_loop 跑（fire-and-forget）。"""
    if _bot_loop is None:
        raise RuntimeError("http_server not configured; call configure() first")
    asyncio.run_coroutine_threadsafe(coro, _bot_loop)


# ── HTTP handler ──────────────────────────────────────────────

class _CardCallbackHandler(BaseHTTPRequestHandler):
    """处理飞书/Lark 卡片按钮点击 + 本机控制端点。"""

    # ── POST ─────────────────────────────────────────────────
    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)

        if parsed.path == "/spawn":
            self._handle_spawn_post(body)
            return

        if parsed.path == "/trigger":
            self._handle_trigger(body, query_mode=False)
            return

        if parsed.path == "/reload":
            self._handle_reload()
            return

        if parsed.path == "/wake":
            self._handle_wake(body)
            return

        if parsed.path == "/dispatch":
            self._handle_dispatch(body)
            return

        if parsed.path == "/read_thread":
            self._handle_read_thread(body)
            return

        if parsed.path == "/schedule_cron":
            self._handle_schedule_cron(body)
            return

        if parsed.path == "/list_crons":
            self._handle_list_crons(body)
            return

        # 默认：卡片按钮回调
        try:
            data = json.loads(body)
        except Exception:
            self._respond(400, {"error": "bad json"})
            return

        if data.get("type") == "url_verification":
            self._respond(200, {"challenge": data.get("challenge", "")})
            return

        self._dispatch_card_action(data)

    # ── GET ──────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/spawn":
            self._handle_spawn_get(parsed.query)
            return
        if parsed.path == "/trigger":
            self._handle_trigger_get(parsed.query)
            return
        if parsed.path == "/reload":
            self._handle_reload()
            return
        if parsed.path == "/handover":
            self._handle_handover(parsed.query)
            return

        self._respond(404, {"error": "not found"})

    # ── 各端点 ───────────────────────────────────────────────

    def _handle_spawn_post(self, body: bytes):
        if not _is_localhost(self.client_address):
            self._respond(403, {"error": "spawn is localhost only"})
            return
        try:
            params = json.loads(body)
            if not isinstance(params, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:
            self._respond(400, {"error": f"bad json: {e}"})
            return
        self._fire_spawn(params)

    def _handle_spawn_get(self, query: str):
        if not _is_localhost(self.client_address):
            self._respond(403, {"error": "spawn is localhost only"})
            return
        raw = parse_qs(query)
        params = {k: (v[0] if v else "") for k, v in raw.items()}
        self._fire_spawn(params)

    def _fire_spawn(self, params: dict):
        bot, user_id, kwargs, err = _resolve_spawn_request(params)
        if err:
            self._respond(400, err)
            return
        _submit(_handlers.handle_spawn(bot, user_id=user_id, **kwargs))
        self._respond(200, {
            "ok": True,
            "profile": bot.profile.name,
            "user_id": user_id,
            "chat_id": f"{kwargs['chat_id_raw']}:{kwargs['thread_id']}",
        })

    def _handle_trigger(self, body: bytes, query_mode: bool):
        if not _is_localhost(self.client_address):
            self._respond(403, {"error": "trigger is localhost only"})
            return
        try:
            params = json.loads(body) if body else {}
        except Exception as e:
            self._respond(400, {"error": f"bad json: {e}"})
            return
        self._fire_trigger((params.get("name") or "").strip())

    def _handle_trigger_get(self, query: str):
        if not _is_localhost(self.client_address):
            self._respond(403, {"error": "trigger is localhost only"})
            return
        raw = parse_qs(query)
        name = (raw.get("name", [""])[0] or "").strip()
        if not name:
            # 不带参数 = 列出可用任务
            self._respond(200, {"ok": True, "available": _handlers.list_tasks()})
            return
        self._fire_trigger(name)

    def _fire_trigger(self, name: str):
        available = _handlers.list_tasks()
        if not name:
            self._respond(400, {"error": "name required", "available": available})
            return
        if name not in available:
            self._respond(404, {"error": f"task {name!r} not registered", "available": available})
            return
        _submit(_handlers.fire_task(name))
        self._respond(200, {"ok": True, "fired": name})

    def _handle_wake(self, body: bytes):
        """cc_mcp_server 的 wake_me_in → 在常驻 scheduler 挂一个一次性唤醒任务。

        仅本机：MCP server 跑在 bot spawn 的 claude 子进程里，必经 127.0.0.1。
        schedule_wake 是同步的（只 add_job，不阻塞），直接调即可。
        """
        if not _is_localhost(self.client_address):
            self._respond(403, {"ok": False, "error": "wake is localhost only"})
            return
        try:
            params = json.loads(body) if body else {}
            if not isinstance(params, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:
            self._respond(400, {"ok": False, "error": f"bad json: {e}"})
            return
        try:
            result = _handlers.schedule_wake(
                profile=(params.get("profile") or "").strip(),
                chat_id=(params.get("chat_id") or "").strip(),
                thread_id=(params.get("thread_id") or "").strip(),
                anchor_message_id=(params.get("anchor_message_id") or "").strip(),
                user_id=(params.get("user_id") or "").strip(),
                minutes=params.get("minutes"),
                note=params.get("note") or "",
            )
        except Exception as e:
            self._respond(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        self._respond(200 if result.get("ok") else 400, result)

    def _resolve_bot_by_profile(self, name: str) -> Optional[BotInstance]:
        name = (name or "").strip()
        if name and name in _bots:
            return _bots[name]
        if len(_bots) == 1:
            return next(iter(_bots.values()))
        return None

    def _run_on_loop(self, coro, timeout: float):
        """把 async handler 投到 bot_loop 跑并等结果（dispatch/read_thread 需要返回值）。"""
        fut = asyncio.run_coroutine_threadsafe(coro, _bot_loop)
        return fut.result(timeout=timeout)

    def _handle_dispatch(self, body: bytes):
        """cc_mcp_server 的 dispatch_task → 在目标群新开 thread 派独立子会话。"""
        if not _is_localhost(self.client_address):
            self._respond(403, {"ok": False, "error": "dispatch is localhost only"})
            return
        try:
            p = json.loads(body) if body else {}
            if not isinstance(p, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:
            self._respond(400, {"ok": False, "error": f"bad json: {e}"})
            return
        bot = self._resolve_bot_by_profile(p.get("profile") or "")
        if bot is None:
            self._respond(400, {"ok": False, "error": "profile 未加载（多 bot 必须指定 profile）"})
            return
        try:
            result = self._run_on_loop(
                _handlers.dispatch_task(
                    bot,
                    user_id=(p.get("user_id") or "").strip(),
                    group_chat_id=(p.get("chat_id") or p.get("group_chat_id") or "").strip(),
                    title=(p.get("title") or "").strip(),
                    prompt=p.get("prompt") or "",
                    parent_thread=(p.get("parent_thread") or "").strip(),
                    parent_anchor=(p.get("parent_anchor") or "").strip(),
                ),
                timeout=30,
            )
        except Exception as e:
            self._respond(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        self._respond(200 if result.get("ok") else 400, result)

    def _handle_read_thread(self, body: bytes):
        """cc_mcp_server 的 read_thread → 拉回某 thread 的消息 transcript。"""
        if not _is_localhost(self.client_address):
            self._respond(403, {"ok": False, "error": "read_thread is localhost only"})
            return
        try:
            p = json.loads(body) if body else {}
            if not isinstance(p, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:
            self._respond(400, {"ok": False, "error": f"bad json: {e}"})
            return
        bot = self._resolve_bot_by_profile(p.get("profile") or "")
        if bot is None:
            self._respond(400, {"ok": False, "error": "profile 未加载（多 bot 必须指定 profile）"})
            return
        try:
            limit = int(p.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        try:
            result = self._run_on_loop(
                _handlers.read_thread(bot, thread_id=(p.get("thread_id") or "").strip(), limit=limit),
                timeout=30,
            )
        except Exception as e:
            self._respond(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        self._respond(200 if result.get("ok") else 400, result)

    def _handle_schedule_cron(self, body: bytes):
        """cc_mcp_server 的 schedule_cron → 新增一条重复定时任务（写 yaml + reload）。同步。"""
        if not _is_localhost(self.client_address):
            self._respond(403, {"ok": False, "error": "schedule_cron is localhost only"})
            return
        try:
            p = json.loads(body) if body else {}
            if not isinstance(p, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:
            self._respond(400, {"ok": False, "error": f"bad json: {e}"})
            return
        try:
            result = _handlers.schedule_cron(
                profile=(p.get("profile") or "").strip(),
                chat_id=(p.get("chat_id") or "").strip(),
                user_id=(p.get("user_id") or "").strip(),
                cron=(p.get("cron") or "").strip(),
                prompt=p.get("prompt") or "",
                title=(p.get("title") or "").strip(),
            )
        except Exception as e:
            self._respond(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        self._respond(200 if result.get("ok") else 400, result)

    def _handle_list_crons(self, body: bytes = b""):
        """body 可带 {"chat_id": "oc_..."} 把结果限定到该 chat 的作用域（agent 路径必带）；
        不带 = 全量（本机运维 curl）。"""
        if not _is_localhost(self.client_address):
            self._respond(403, {"ok": False, "error": "list_crons is localhost only"})
            return
        chat_id = ""
        try:
            p = json.loads(body) if body else {}
            if isinstance(p, dict):
                chat_id = (p.get("chat_id") or "").strip()
        except Exception:
            pass  # body 非 JSON 就当全量
        try:
            result = _handlers.list_crons(chat_id)
        except Exception as e:
            self._respond(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return
        self._respond(200 if result.get("ok") else 400, result)

    def _handle_reload(self):
        if not _is_localhost(self.client_address):
            self._respond(403, {"error": "reload is localhost only"})
            return
        try:
            result = _handlers.reload_tasks()
            self._respond(200, result)
        except Exception as e:
            self._respond(500, {"error": f"{type(e).__name__}: {e}"})

    def _handle_handover(self, query: str):
        params = parse_qs(query)
        session_id = params.get("session_id", [""])[0]
        cwd = params.get("cwd", [""])[0]
        model = params.get("model", [""])[0]
        profile_name = params.get("profile", [""])[0]
        target_user = params.get("user_id", [""])[0]
        target_chat = params.get("chat_id", [""])[0]

        if not session_id:
            self._respond(400, {"error": "session_id required"})
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                _handlers.handle_handover(
                    session_id, cwd, model, profile_name, target_user, target_chat,
                ),
                _bot_loop,
            )
            result = future.result(timeout=15)
            self._respond(200, result)
        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _dispatch_card_action(self, data: dict):
        event = data.get("event", {})
        operator = event.get("operator", {})
        user_id = operator.get("open_id", "")
        action = event.get("action", {})
        value = action.get("value", {}) or {}
        context = event.get("context", {})

        action_type = value.get("action", "")
        chat_id = value.get("cid", user_id)
        clicked_msg_id = context.get("open_message_id", "")

        bot = _resolve_bot_from_value(value)
        if bot is None:
            self._respond(200, {"toast": {"type": "warning", "content": "按钮已过期"}})
            return

        log(bot.profile.name, "http", "info",
            f"卡片回调 user={user_id[:8]}... action={action_type or 'reply'}")

        if action_type == "set_mode":
            mode = value.get("mode", "")
            if mode:
                _submit(_handlers.handle_set_mode(bot, user_id, chat_id, mode, clicked_msg_id))
            self._respond(200, {"toast": {"type": "success", "content": f"已切换: {mode}"}})
        elif action_type == "run_cmd":
            cmd_text = value.get("cmd", "")
            if cmd_text:
                _submit(_handlers.handle_menu_command(bot, user_id, chat_id, cmd_text, clicked_msg_id))
            self._respond(200, {"toast": {"type": "info", "content": cmd_text}})
        elif action_type == "resume_session":
            sid = value.get("sid", "")
            if sid:
                _submit(_handlers.handle_resume_session(bot, user_id, chat_id, sid, clicked_msg_id))
            self._respond(200, {"toast": {"type": "info", "content": "正在恢复..."}})
        else:
            reply_text = value.get("reply", "")
            if reply_text:
                _submit(_handlers.handle_button_reply(bot, user_id, chat_id, reply_text, clicked_msg_id))
            self._respond(200, {"toast": {"type": "info", "content": f"已发送: {reply_text}"}})

    def _respond(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # 静默：默认 BaseHTTPRequestHandler 会把每次请求打到 stderr
        pass


# ── 启动 ─────────────────────────────────────────────────────

def start_callback_server(port: int) -> HTTPServer:
    """启动 HTTP server，跑在后台线程里。返回 server 句柄。"""
    if _handlers is None:
        raise RuntimeError("call http_server.configure(...) before start_callback_server")
    server = HTTPServer(('0.0.0.0', port), _CardCallbackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="http-callback")
    t.start()
    return server


def start_ngrok(port: int) -> Optional[str]:
    """启动 ngrok 隧道，返回公网 URL。已有隧道复用。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
            tunnels = json.loads(r.read())
            for t in tunnels.get("tunnels", []):
                if t.get("proto") == "https":
                    return t["public_url"]
    except Exception:
        pass

    try:
        ngrok_domain = os.environ.get("NGROK_DOMAIN", "")
        ngrok_cmd = (
            ["ngrok", "http", "--url", ngrok_domain, str(port)]
            if ngrok_domain else
            ["ngrok", "http", str(port)]
        )
        subprocess.Popen(
            ngrok_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=5) as r:
            tunnels = json.loads(r.read())
            for t in tunnels.get("tunnels", []):
                if t.get("proto") == "https":
                    return t["public_url"]
    except Exception as e:
        log("global", "ngrok", "warn", f"ngrok 启动失败: {e}")
    return None
