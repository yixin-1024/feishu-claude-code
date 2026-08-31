"""卡片回调 + 控制端点的 HTTP 服务。

提供端点：
    POST /callback        Lark 卡片按钮回调（公网 listener）
    /api/v1/*             外部事件触发 API（公网 listener，API key/HMAC 鉴权，见 external_api.py）
    POST /spawn           （本机 control listener）派单进新 session
    GET  /spawn           （仅本机）派单（query string 版本）
    POST /trigger         （仅本机）手动触发已注册的 cron 任务
    GET  /trigger         （仅本机）同上
    POST /reload          （仅本机）热重载 scheduled_tasks.yaml
    GET  /reload          （仅本机）同上
    GET  /handover        （仅本机）CLI session 接管

安全边界：公网 callback listener 与本机 control listener 是两个独立 HTTPServer。
ngrok / 反代只转发 callback 端口；control 端口仅绑定 127.0.0.1 且要求 Bearer token。
公网 listener 上除 `/callback` 只多出 `/api/v1/*`（外部事件触发），它自带 API key +
可选 HMAC 鉴权，未配置时整个前缀直接 503 —— control 端点永远不会在公网侧出现。

设计：本模块**不 import** dispatcher / business logic，所有业务回调通过
`configure(...)` 注入，避免循环 import + 让单元测试只 mock callbacks 即可。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse, parse_qs

import external_api
from bot_instance import BotInstance
from card_security import (
    card_action_allowed,
    card_context_matches,
    claim_event,
    verify_action_value,
)
from log_util import log


# ── 注入点 ────────────────────────────────────────────────────

@dataclass
class HttpHandlers:
    """业务回调集合。configure() 注入。"""
    handle_spawn: Callable[..., Awaitable[None]]
    handle_handover: Callable[..., Awaitable[dict]]
    handle_set_mode: Callable[..., Awaitable[None]]
    handle_menu_command: Callable[..., Awaitable[None]]
    handle_switch_usage: Callable[..., Awaitable[None]]
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
    # async：往已有 thread 的 session 实时插话（cc_mcp_server 的 append_to_task / steer_task 后端）
    steer_thread: Callable[..., Awaitable[dict]]
    # 同步：重复定时任务（cc_mcp_server 的 schedule_cron / list_crons 后端）
    schedule_cron: Callable[..., dict]
    list_crons: Callable[..., dict]


_bot_loop: Optional[asyncio.AbstractEventLoop] = None
_bots: dict[str, BotInstance] = {}
_handlers: Optional[HttpHandlers] = None
_control_token = ""

_CONTROL_TOKEN_FILE = os.path.expanduser(
    os.getenv("CC_LARK_CONTROL_TOKEN_FILE", "~/.feishu-claude/control-token")
)
_MAX_REQUEST_BODY = int(os.getenv("CC_LARK_HTTP_MAX_BODY", str(1024 * 1024)))
_REQUEST_READ_TIMEOUT_SEC = float(os.getenv("CC_LARK_HTTP_READ_TIMEOUT_SEC", "15"))


def configure(
    *,
    bot_loop: asyncio.AbstractEventLoop,
    bots: dict[str, BotInstance],
    handlers: HttpHandlers,
    control_token: str = "",
) -> None:
    """主入口在启动时调一次，注入运行时状态 + 业务回调。"""
    global _bot_loop, _bots, _handlers, _control_token
    _bot_loop = bot_loop
    _bots = bots
    _handlers = handlers
    _control_token = (control_token or os.getenv("CC_LARK_CONTROL_TOKEN") or "").strip()


def load_or_create_control_token(path: str = "") -> str:
    """返回 control plane token；未显式配置时安全落盘生成一个。

    token 文件固定 0600，既让 bot 重启后保持稳定，也让本机运维脚本能显式读取并
    通过 Authorization header 调用 control API。任何文件错误都退化为本进程随机
    token（仍然 fail-closed，不会因为落盘失败关闭鉴权）。
    """
    configured = (os.getenv("CC_LARK_CONTROL_TOKEN") or "").strip()
    if configured:
        return configured

    token_path = os.path.expanduser(path or _CONTROL_TOKEN_FILE)
    try:
        os.makedirs(os.path.dirname(token_path) or ".", mode=0o700, exist_ok=True)
        try:
            with open(token_path, encoding="utf-8") as f:
                existing = f.read().strip()
            if existing:
                os.chmod(token_path, 0o600)
                return existing
        except FileNotFoundError:
            pass

        token = secrets.token_urlsafe(48)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(token_path, flags, 0o600)
        except FileExistsError:
            with open(token_path, encoding="utf-8") as f:
                raced = f.read().strip()
            if raced:
                os.chmod(token_path, 0o600)
                return raced
            raise RuntimeError(f"control token file is empty: {token_path}")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.chmod(token_path, 0o600)
        return token
    except Exception as e:
        log("global", "http", "warn",
            f"control token 无法落盘，改用本进程临时 token: {type(e).__name__}: {e}")
        return secrets.token_urlsafe(48)


# ── 内部工具 ─────────────────────────────────────────────────

def _is_localhost(client_address) -> bool:
    """HTTP 客户端是否来自本机（control listener 的纵深校验）。"""
    try:
        host = client_address[0]
    except Exception:
        return False
    return host in ("127.0.0.1", "::1", "localhost")


def _extract_control_token(headers) -> str:
    auth = (headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # 兼容不方便设置 Authorization 的本机脚本；仍需持有同一个 secret。
    return (headers.get("X-CC-Lark-Token") or "").strip()


def _resolve_callback_bot(header: dict) -> Optional[BotInstance]:
    """Resolve an HTTP callback only from Lark's header.app_id, never action.value."""
    app_id = header.get("app_id", "") if isinstance(header, dict) else ""
    matches = [bot for bot in _bots.values() if bot.profile.app_id == app_id]
    return matches[0] if len(matches) == 1 else None


# 跨 agent 派发：dispatch_task 的 `agent` 参数 → 已加载的目标 bot。
# runner 家族别名让编排 agent 用直觉名字（"gpt"）而不用记内部 profile 名。
_AGENT_RUNNER_ALIASES = {
    "gpt": "codex", "codex": "codex", "openai": "codex", "chatgpt": "codex", "o1": "codex",
    "claude": "claude", "anthropic": "claude",
    "gemini": "opencode", "opencode": "opencode",
    "mimo": "mimo",
    "grok": "grok", "xai": "grok",
    "maka": "maka", "apache-maka": "maka",
}


def resolve_target_agent(
    bots: dict[str, BotInstance], spec: str, *, exclude: str = "",
) -> tuple[Optional[BotInstance], str]:
    """把 dispatch_task 的 `agent` 解析成目标 bot。返回 (bot, err)。

    解析顺序：① 精确 profile 名（区分/不区分大小写）→ ② runner 家族别名
    （gpt/codex→codex, claude→claude, gemini/opencode→opencode, mimo→mimo,
    grok/xai→grok, maka→maka），
    别名命中多个时优先选 != 调用方(exclude) 的那个。命中不到返回 (None, 错误说明+可选项)。
    """
    spec = (spec or "").strip()
    if not spec:
        return None, "empty agent spec"
    if spec in bots:
        return bots[spec], ""
    low = spec.lower()
    for name, b in bots.items():
        if name.lower() == low:
            return b, ""
    want = _AGENT_RUNNER_ALIASES.get(low)
    if want:
        cands = [b for b in bots.values() if b.profile.runner == want]
        pref = [b for b in cands if b.profile.name != exclude]
        chosen = pref or cands
        if chosen:
            return chosen[0], ""
    avail = ", ".join(f"{n}({b.profile.runner})" for n, b in bots.items()) or "（无）"
    return None, f"未找到 agent {spec!r}。已加载可选：{avail}"


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
    effort = (params.get("effort") or "").strip().lower()

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
        "effort": effort,
    }, None


def _submit(coro: Awaitable) -> None:
    """把 coroutine 提交到 bot_loop 跑（fire-and-forget）。"""
    if _bot_loop is None:
        raise RuntimeError("http_server not configured; call configure() first")
    asyncio.run_coroutine_threadsafe(coro, _bot_loop)


# ── HTTP handler ──────────────────────────────────────────────

class _CardCallbackHandler(BaseHTTPRequestHandler):
    """公网 callback handler；control 子类只复用业务处理方法。"""

    control_plane = False

    def setup(self):
        super().setup()
        self.connection.settimeout(_REQUEST_READ_TIMEOUT_SEC)

    # ── POST ─────────────────────────────────────────────────
    def do_POST(self):
        parsed = urlparse(self.path)

        if not self.control_plane:
            # `/` 是旧部署的 callback 兼容别名；其余公网路径一律 404，尤其不能再
            # 因 ngrok 转发的 socket peer=127.0.0.1 而落进 control handlers。
            # 例外只有外部事件触发 API：它自己做 API key / HMAC 鉴权。
            if parsed.path.startswith(external_api.API_PREFIX):
                body = self._read_body()
                if body is None:
                    return
                self._handle_external_api("POST", parsed.path, body)
                return
            if parsed.path not in ("/callback", "/"):
                self._respond(404, {"error": "not found"})
                return
            body = self._read_body()
            if body is None:
                return
            self._handle_card_callback(body)
            return

        if not self._authorize_control():
            return
        body = self._read_body()
        if body is None:
            return

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

        if parsed.path == "/steer":
            self._handle_steer(body)
            return

        if parsed.path == "/schedule_cron":
            self._handle_schedule_cron(body)
            return

        if parsed.path == "/list_crons":
            self._handle_list_crons(body)
            return

        self._respond(404, {"error": "not found"})

    def _read_body(self) -> Optional[bytes]:
        """在完成 path/auth 判定后读取受限、带超时的请求体。"""
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except (TypeError, ValueError):
            self._respond(400, {"error": "invalid Content-Length"})
            return None
        if length < 0 or length > _MAX_REQUEST_BODY:
            self._respond(413, {"error": "request body too large"})
            return None
        try:
            return self.rfile.read(length)
        except TimeoutError:
            try:
                self._respond(408, {"error": "request body timeout"})
            except OSError:
                pass
            return None

    def _handle_card_callback(self, body: bytes):
        """处理公网 Lark callback；只会由 callback listener 调用。"""
        try:
            data = json.loads(body)
        except Exception:
            self._respond(400, {"error": "bad json"})
            return

        if data.get("type") == "url_verification":
            configured = [
                bot.profile.verification_token
                for bot in _bots.values()
                if getattr(bot.profile, "verification_token", "")
            ]
            supplied = str(data.get("token") or "")
            if configured and not any(hmac.compare_digest(supplied, token) for token in configured):
                self._respond(403, {"error": "invalid verification token"})
                return
            self._respond(200, {"challenge": data.get("challenge", "")})
            return

        self._dispatch_card_action(data)

    # ── GET ──────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)

        if not self.control_plane:
            if parsed.path.startswith(external_api.API_PREFIX):
                self._handle_external_api("GET", parsed.path, b"")
                return
            self._respond(404, {"error": "not found"})
            return
        if not self._authorize_control():
            return

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

    def _handle_external_api(self, method: str, path: str, body: bytes):
        """外部事件触发 API：鉴权 + 业务判定全在 external_api，这里只搬 HTTP。"""
        peer = ""
        try:
            peer = self.client_address[0]
        except Exception:
            pass
        try:
            code, payload = external_api.handle(method, path, self.headers, body, peer)
        except Exception as e:
            log("global", "extapi", "error", f"{method} {path} 异常: {type(e).__name__}: {e}")
            self._respond(500, {"ok": False, "error": "internal error"})
            return
        self._respond(code, payload)

    def _authorize_control(self) -> bool:
        """control API 双重校验：loopback listener + constant-time token。"""
        if not _is_localhost(self.client_address):
            self._respond(403, {"error": "control API is localhost only"})
            return False
        if not _control_token:
            self._respond(503, {"error": "control API token is not configured"})
            return False
        supplied = _extract_control_token(self.headers)
        if not supplied or not hmac.compare_digest(supplied, _control_token):
            self._respond(401, {"error": "invalid control API token"})
            return False
        return True

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

    def _mcp_respond(self, endpoint: str, profile: str, result: dict):
        """MCP 运行时端点统一回包：非 ok 结果 warn 落主日志。此前 MCP 层的失败只进
        claude 的 mcp-logs stderr，cc-lark.log 看不到 MCP 健康度 → 无法审计"有没有触顶/丢活"。"""
        if not result.get("ok"):
            log(profile or "global", "mcp", "warn",
                f"{endpoint} 被拒: {str(result.get('error'))[:200]}")
        self._respond(200 if result.get("ok") else 400, result)

    def _mcp_error(self, endpoint: str, profile: str, exc: Exception):
        log(profile or "global", "mcp", "warn",
            f"{endpoint} 异常: {type(exc).__name__}: {exc}")
        self._respond(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

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
        _prof = (params.get("profile") or "").strip()
        try:
            result = _handlers.schedule_wake(
                profile=_prof,
                chat_id=(params.get("chat_id") or "").strip(),
                thread_id=(params.get("thread_id") or "").strip(),
                anchor_message_id=(params.get("anchor_message_id") or "").strip(),
                user_id=(params.get("user_id") or "").strip(),
                minutes=params.get("minutes"),
                note=params.get("note") or "",
            )
        except Exception as e:
            self._mcp_error("wake", _prof, e)
            return
        self._mcp_respond("wake", _prof, result)

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
        _prof = (p.get("profile") or "").strip()
        bot = self._resolve_bot_by_profile(_prof)
        if bot is None:
            self._mcp_respond("dispatch", _prof,
                              {"ok": False, "error": "profile 未加载（多 bot 必须指定 profile）"})
            return
        # 跨 agent 派发：agent 参数指定异后端目标 bot（如 "gpt"→codex）。缺省=同 bot。
        _agent = (p.get("agent") or "").strip()
        target_bot = None
        if _agent:
            target_bot, err = resolve_target_agent(_bots, _agent, exclude=bot.profile.name)
            if target_bot is None:
                self._mcp_respond("dispatch", _prof, {"ok": False, "error": err})
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
                    target_bot=target_bot,
                    # 子会话是全新 session，不继承派发方的 /model /effort —— 要指定
                    # 只能显式带上（别名解析 + 校验在 dispatcher 侧统一做）。
                    model=(p.get("model") or "").strip(),
                    effort=(p.get("effort") or "").strip(),
                ),
                timeout=30,
            )
        except Exception as e:
            self._mcp_error("dispatch", _prof, e)
            return
        self._mcp_respond("dispatch", _prof, result)

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
        _prof = (p.get("profile") or "").strip()
        bot = self._resolve_bot_by_profile(_prof)
        if bot is None:
            self._mcp_respond("read_thread", _prof,
                              {"ok": False, "error": "profile 未加载（多 bot 必须指定 profile）"})
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
            self._mcp_error("read_thread", _prof, e)
            return
        self._mcp_respond("read_thread", _prof, result)

    def _handle_steer(self, body: bytes):
        """cc_mcp_server 的 append_to_task / steer_task → 往某 thread 的 session 实时插话。

        stop_first=False=追加（不打断当前 run），True=停当前 run 再按新指令续跑。
        bot 侧只做锚点解析 + 排后台 task 后立即返回，故 30s timeout 足够。"""
        if not _is_localhost(self.client_address):
            self._respond(403, {"ok": False, "error": "steer is localhost only"})
            return
        try:
            p = json.loads(body) if body else {}
            if not isinstance(p, dict):
                raise ValueError("body must be a JSON object")
        except Exception as e:
            self._respond(400, {"ok": False, "error": f"bad json: {e}"})
            return
        _prof = (p.get("profile") or "").strip()
        bot = self._resolve_bot_by_profile(_prof)
        if bot is None:
            self._mcp_respond("steer", _prof,
                              {"ok": False, "error": "profile 未加载（多 bot 必须指定 profile）"})
            return
        try:
            result = self._run_on_loop(
                _handlers.steer_thread(
                    bot,
                    user_id=(p.get("user_id") or "").strip(),
                    group_chat_id=(p.get("chat_id") or p.get("group_chat_id") or "").strip(),
                    thread_id=(p.get("thread_id") or "").strip(),
                    instruction=p.get("instruction") or p.get("message") or "",
                    stop_first=bool(p.get("stop_first")),
                ),
                timeout=30,
            )
        except Exception as e:
            self._mcp_error("steer", _prof, e)
            return
        self._mcp_respond("steer", _prof, result)

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
        _prof = (p.get("profile") or "").strip()
        try:
            result = _handlers.schedule_cron(
                profile=_prof,
                chat_id=(p.get("chat_id") or "").strip(),
                user_id=(p.get("user_id") or "").strip(),
                cron=(p.get("cron") or "").strip(),
                prompt=p.get("prompt") or "",
                title=(p.get("title") or "").strip(),
                model=(p.get("model") or "").strip(),
                effort=(p.get("effort") or "").strip(),
            )
        except Exception as e:
            self._mcp_error("schedule_cron", _prof, e)
            return
        self._mcp_respond("schedule_cron", _prof, result)

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
            self._mcp_error("list_crons", "", e)
            return
        self._mcp_respond("list_crons", "", result)

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
        header = data.get("header", {})

        action_type = value.get("action", "")
        chat_id = value.get("cid", user_id)
        clicked_msg_id = context.get("open_message_id", "")
        callback_chat_id = context.get("open_chat_id", "")

        bot = _resolve_callback_bot(header)
        source_valid = bool(
            bot
            and data.get("schema") == "2.0"
            and header.get("event_type") == "card.action.trigger"
            and value.get("profile") == bot.profile.name
        )
        if not source_valid:
            self._respond(200, {"toast": {"type": "warning", "content": "按钮已过期"}})
            return
        assert bot is not None

        expected_token = getattr(bot.profile, "verification_token", "") or ""
        supplied_token = str(header.get("token") or "")
        if expected_token and not hmac.compare_digest(supplied_token, expected_token):
            log(bot.profile.name, "http", "warn", "拒绝卡片回调 reason=verification-token")
            self._respond(200, {
                "toast": {"type": "warning", "content": "按钮无效或已过期，请重新操作"},
            })
            return

        verified, reason = verify_action_value(
            value,
            bot.profile.app_secret,
            user_id=user_id,
            message_id=clicked_msg_id,
        )
        context_valid = card_context_matches(user_id, chat_id, callback_chat_id)
        if (
            not verified
            or not context_valid
            or not card_action_allowed(bot.profile, user_id, chat_id)
        ):
            log(bot.profile.name, "http", "warn",
                f"拒绝卡片回调 user={user_id[:8]}... reason={reason or 'acl'}")
            self._respond(200, {
                "toast": {"type": "warning", "content": "按钮无效或已过期，请重新操作"},
            })
            return

        event_id = str(header.get("event_id") or "")
        if not claim_event(bot.profile.name, event_id):
            self._respond(200, {
                "toast": {"type": "info", "content": "该操作已处理"},
            })
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
        elif action_type == "switch_usage":
            name = value.get("name", "")
            if name:
                _submit(_handlers.handle_switch_usage(bot, user_id, chat_id, name, clicked_msg_id))
            self._respond(200, {"toast": {"type": "info", "content": f"正在切换到 {name}…"}})
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


class _ControlRequestHandler(_CardCallbackHandler):
    """仅绑定 loopback 的、带 token 鉴权的 control API handler。"""

    control_plane = True


# ── 启动 ─────────────────────────────────────────────────────

def start_callback_server(port: int) -> ThreadingHTTPServer:
    """启动公网 callback listener；除 POST /callback（及旧 `/`）外全 404。"""
    if _handlers is None:
        raise RuntimeError("call http_server.configure(...) before start_callback_server")
    server = ThreadingHTTPServer(('0.0.0.0', port), _CardCallbackHandler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True, name="http-callback")
    t.start()
    return server


def start_control_server(port: int) -> ThreadingHTTPServer:
    """启动仅 loopback 可达、且强制 token 的 control listener。"""
    if _handlers is None:
        raise RuntimeError("call http_server.configure(...) before start_control_server")
    if not _control_token:
        raise RuntimeError("control token must be configured before start_control_server")
    server = ThreadingHTTPServer(('127.0.0.1', port), _ControlRequestHandler)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True, name="http-control")
    t.start()
    return server


def _matching_ngrok_tunnel(data: dict, port: int) -> Optional[str]:
    """只复用确实转发到 callback port 的 HTTPS tunnel。"""
    for tunnel in data.get("tunnels", []):
        if tunnel.get("proto") != "https":
            continue
        addr = str((tunnel.get("config") or {}).get("addr") or "").strip()
        parsed = urlparse(addr if "://" in addr else f"http://{addr}")
        if parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            continue
        try:
            target_port = parsed.port
        except ValueError:
            continue
        if target_port == port:
            return tunnel.get("public_url")
    return None


def start_ngrok(port: int) -> Optional[str]:
    """启动 ngrok 隧道，返回公网 URL。已有隧道复用。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
            tunnels = json.loads(r.read())
            existing = _matching_ngrok_tunnel(tunnels, port)
            if existing:
                return existing
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
            return _matching_ngrok_tunnel(tunnels, port)
    except Exception as e:
        log("global", "ngrok", "warn", f"ngrok 启动失败: {e}")
    return None
