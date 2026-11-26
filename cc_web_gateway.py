"""cc-lark web gateway —— 给网页产品（RG Next 等）起 / 续 / 停 Claude Code 会话。

独立进程（systemd `cc-lark-web.service`），**不跟主 bot（main.py）同进程**：主 bot 是所有
Lark 会话的父进程，不能为了网页需求重启它。本模块只 import cc-lark 现有模块里的纯函数
（错误分类、子进程探测、凭证读取、日志），不改它们的行为。

接口（只绑 127.0.0.1，Bearer 共享密钥）——完整契约见 rg-next 仓 docs/ai/GATEWAY-API.md：

    POST /v1/runs                     起一轮（新会话或续会话）→ 202 {run_id, status}
    GET  /v1/runs/{id}/events?after=N SSE 事件流（落盘 jsonl，断线可从 after 续）
    GET  /v1/runs/{id}                状态 / session_id / 最终文本 / 用量
    POST /v1/runs/{id}/cancel         杀进程组
    GET  /v1/health
    GET  /v1/apps/{app}/runs?limit=N  运维只读：在跑 / 排队 / 最近完成的 run（不含正文）
    GET  /v1/apps/{app}/agent         只读：agent 规则版本号（CLAUDE.md + bin/ 的哈希）与文件清单

安全边界：
  · cwd / 模型白名单 / 工具白名单 / 权限模式只来自配置（env 文件），请求改不了。
  · Claude 以 `--restricted` 跑：文件工具锁在 cwd 内、不读 user/project settings、
    不带任何 MCP server（--strict-mcp-config），权限模式 dontAsk（白名单外一律拒，不弹）。
  · 子进程 env 是**显式白名单**拼出来的（不继承本进程 env，本进程 env 里有 cc-lark .env
    的全部密钥），请求只能带配置里允许的键（RG_RUN_TOKEN 等）。
  · 只续本网关为同一 app + conversation 建过的 session，防止拿任意 session id 去
    resume 别的项目（比如 bot 自己的会话）。
  · 日志不打印 prompt 全文和任何 token。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from log_util import log

# 复用 cc-lark runner 的纯函数：错误分类（和 bot 同一套判定）、子进程探测、凭证读取。
from claude_runner import (
    _classify_result_error,
    _extract_errors_text,
    _extract_text_content,
    _has_children,
    is_session_missing_error_text,
)
from bot_config import CLAUDE_CLI as _BOT_CLAUDE_CLI, CLAUDE_EFFORT_LEVELS

PROFILE_TAG = "web"
VERSION = "1"

# 绝不允许出现在网页 agent 工具白名单里的工具（写文件 / 派子 agent）
_FORBIDDEN_TOOLS = frozenset({
    "Edit", "Write", "MultiEdit", "NotebookEdit", "Task", "Agent", "Workflow",
})
_FORBIDDEN_PERMISSION_MODES = frozenset({"bypassPermissions", "acceptEdits", "auto"})

_DEFAULT_TOOLS = "Bash(rg-tool:*),Read,Grep,Glob,WebSearch,WebFetch"
# 一条拒绝规则：工具名，或 工具名(规则内容)；不许空白、嵌套括号混进 argv。
_DENY_SPEC_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\([^()\s]{1,200}\))?$")

# CC_WEB_CLAUDE_ENV_FILE 里只认这些键（中转站 / API key 的凭证和模型映射）。别的键一律丢掉，
# 所以这个文件改不了 PATH、HOME、代理之类，也带不进订阅 token。
_CLAUDE_ENV_FILE_KEYS = frozenset({
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
})
_CLAUDE_ENV_SECRET_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
_DEFAULT_ENV_KEYS = "RG_RUN_TOKEN,RG_API_BASE,RG_LOCALE"

_CONV_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_APP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_RUN_ID_RE = re.compile(r"^run_[0-9a-f]{32}$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_IDEM_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")
# <rg_context> / </rg_context> 的各种写法（大小写、空白、属性、全角括号不算）
_CTX_TAG_RE = re.compile(r"<\s*/?\s*rg_context\b[^>]*>", re.IGNORECASE)

# 子进程 env 从本进程继承的键（其余一律不给）。凭证另行注入。
_INHERIT_ENV_KEYS = (
    "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "SHELL",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
)
_BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timeout", "interrupted"})
ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_EVENTS = frozenset({"done", "error"})

_MAX_BODY_BYTES = 512 * 1024
_SUMMARY_CHARS = 300

# GET /v1/apps/{app}/runs（运维只读列表）
_LIST_DEFAULT_LIMIT = 50
_LIST_MAX_LIMIT = 200
_LIST_ERROR_CHARS = 200
# 列表只从 run 元数据里取这些键（白名单）；text / prompt / idempotency_key / env 一律不碰
_LIST_META_FIELDS = (
    "run_id", "app", "conversation_id", "status", "session_id", "resume_session_id",
    "session_reset", "model", "effort", "created_at", "started_at", "ended_at", "pid",
    "error", "usage",
)
_LIST_USAGE_KEYS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "total_cost_usd", "num_turns",
)

# agent 规则版本号：只哈希 system prompt 文件 + PATH_PREPEND 目录里的常规文件（不递归）
_AGENT_MAX_FILES = 200
_AGENT_MAX_FILE_BYTES = 8 * 1024 * 1024


# ── 配置 ──────────────────────────────────────────────────────

class ConfigError(ValueError):
    pass


def _env_int(env: dict, key: str, default: int) -> int:
    raw = str(env.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 不是整数") from exc


def _env_float(env: dict, key: str, default: float) -> float:
    raw = str(env.get(key, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 不是数字") from exc


def _csv(raw: str) -> list[str]:
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def _tool_base(spec: str) -> str:
    return spec.split("(", 1)[0].strip()


@dataclass
class AppConfig:
    name: str
    cwd: str
    model: str
    effort: str
    models: list[str]
    allowed_tools: list[str]
    permission_mode: str
    env_keys: list[str]
    path_prepend: list[str]
    system_prompt_file: str  # 空 = 用 cwd/CLAUDE.md（存在才带）
    # 每个 conversation 一个独立工作目录（state_dir/work/<app>/<hash>）。agent 的文件工具
    # （--restricted 锁在 cwd）和 rg-tool 落地的客户原件（./.files）因此按话题隔离——
    # 共用 app.cwd 时，别的用户的话题能 Glob/Read 到这里的原件（D4 安全审查实测）。
    isolate_cwd: bool = True
    # 拒绝规则（--disallowedTools，优先于白名单）。用途：Claude Code 自带一份「只读命令」名单
    # （ls / echo / pwd / id / cat …），这些在 dontAsk 下不经白名单也放行，还能和 rg-tool 用
    # ; && | 串成一条（RG R0 审计 §B.2）。逐条 Bash(<cmd>:*) 拒掉；复合命令里任一段命中整条拒。
    disallowed_tools: list[str] = field(default_factory=list)

    @property
    def builtin_tools(self) -> list[str]:
        seen: list[str] = []
        for spec in self.allowed_tools:
            base = _tool_base(spec)
            if base and base not in seen:
                seen.append(base)
        return seen


@dataclass
class GatewayConfig:
    token: str
    host: str
    port: int
    state_dir: str
    claude_cli: str
    max_concurrent: int
    max_queue: int
    idle_timeout: float
    stuck_child_timeout: float
    wall_clock: float
    check_interval: float
    max_prompt_chars: int
    max_context_chars: int
    max_env_value_chars: int
    retention_days: float
    heartbeat_sec: float
    apps: dict[str, AppConfig] = field(default_factory=dict)
    # 空 = claude 用默认的 ~/.claude（和主 bot 同一个）。配了 → 每个 claude 子进程带
    # CLAUDE_CONFIG_DIR=<这里>，会话记录（projects/…jsonl）只落这个目录：同机多个网关实例
    # （例：RG 测试 / 生产租户）的会话彼此、也和主 bot 分开。凭证仍走 env 注入（claude_credential_env）。
    claude_config_dir: str = ""
    # 空 = 凭证走 claude_credential_env()（主 bot 选中的订阅 token）。配了 → 每次起 claude 前重读这个
    # JSON（{"env": {...}} 或扁平 {...}），只取 _CLAUDE_ENV_FILE_KEYS 里的键注入子进程，**不再**注入订阅
    # 凭证；读不到 / 不合法 / 权限太宽 → 这一轮以 claude_env_unavailable 失败（fail-closed，不回落订阅）。
    # 换 token 只改文件，不用重启。用于 RG agent 独立机器走中转站。
    claude_env_file: str = ""

    @property
    def token_ok(self) -> bool:
        return len(self.token) >= 32


def _app_prefix(name: str) -> str:
    return "CC_WEB_APP_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_"


def load_config(env: Optional[dict] = None) -> GatewayConfig:
    env = dict(os.environ if env is None else env)
    host = (env.get("CC_WEB_GATEWAY_HOST") or "127.0.0.1").strip()
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ConfigError("CC_WEB_GATEWAY_HOST 只允许回环地址（127.0.0.1 / ::1）")
    default_model = (env.get("CC_WEB_DEFAULT_MODEL") or "sonnet").strip()
    default_effort = (env.get("CC_WEB_DEFAULT_EFFORT") or "medium").strip().lower()
    wall = _env_float(env, "CC_WEB_WALL_CLOCK_SEC", 900.0)
    cfg = GatewayConfig(
        token=(env.get("CC_WEB_GATEWAY_TOKEN") or "").strip(),
        host=host,
        port=_env_int(env, "CC_WEB_GATEWAY_PORT", 8096),
        state_dir=os.path.expanduser(
            env.get("CC_WEB_STATE_DIR") or "~/.local/state/cc-lark-web"),
        claude_cli=(env.get("CC_WEB_CLAUDE_CLI") or _BOT_CLAUDE_CLI).strip(),
        max_concurrent=max(1, _env_int(env, "CC_WEB_MAX_CONCURRENT", 3)),
        max_queue=max(0, _env_int(env, "CC_WEB_MAX_QUEUE", 20)),
        idle_timeout=_env_float(env, "CC_WEB_IDLE_TIMEOUT_SEC", 300.0),
        stuck_child_timeout=_env_float(env, "CC_WEB_STUCK_CHILD_TIMEOUT_SEC", wall or 900.0),
        wall_clock=wall,
        check_interval=max(0.05, _env_float(env, "CC_WEB_CHECK_INTERVAL_SEC", 5.0)),
        max_prompt_chars=_env_int(env, "CC_WEB_MAX_PROMPT_CHARS", 50000),
        max_context_chars=_env_int(env, "CC_WEB_MAX_CONTEXT_CHARS", 50000),
        max_env_value_chars=_env_int(env, "CC_WEB_MAX_ENV_VALUE_CHARS", 4096),
        retention_days=_env_float(env, "CC_WEB_RETENTION_DAYS", 14.0),
        heartbeat_sec=max(0.2, _env_float(env, "CC_WEB_HEARTBEAT_SEC", 15.0)),
        claude_config_dir=os.path.expanduser((env.get("CC_WEB_CLAUDE_CONFIG_DIR") or "").strip()),
        claude_env_file=os.path.expanduser((env.get("CC_WEB_CLAUDE_ENV_FILE") or "").strip()),
    )
    if cfg.claude_env_file and not os.path.isabs(cfg.claude_env_file):
        raise ConfigError("CC_WEB_CLAUDE_ENV_FILE 必须是绝对路径")
    if cfg.claude_config_dir and not os.path.isabs(cfg.claude_config_dir):
        raise ConfigError("CC_WEB_CLAUDE_CONFIG_DIR 必须是绝对路径")
    for name in _csv(env.get("CC_WEB_APPS") or ""):
        if not _APP_NAME_RE.match(name):
            raise ConfigError(f"app 名不合法：{name!r}")
        p = _app_prefix(name)
        cwd = (env.get(p + "CWD") or "").strip()
        if not cwd or not os.path.isabs(cwd):
            raise ConfigError(f"{p}CWD 必须是绝对路径")
        effort = (env.get(p + "EFFORT") or default_effort).strip().lower()
        if effort and effort not in CLAUDE_EFFORT_LEVELS:
            raise ConfigError(f"{p}EFFORT={effort!r} 不在 {CLAUDE_EFFORT_LEVELS}")
        tools = _csv(env.get(p + "ALLOWED_TOOLS") or _DEFAULT_TOOLS)
        bad = [t for t in tools if _tool_base(t) in _FORBIDDEN_TOOLS]
        if bad:
            raise ConfigError(f"{p}ALLOWED_TOOLS 含禁用工具：{bad}")
        disallowed = _csv(env.get(p + "DISALLOWED_TOOLS") or "")
        bad = [t for t in disallowed if not _DENY_SPEC_RE.match(t)]
        if bad:
            raise ConfigError(f"{p}DISALLOWED_TOOLS 含非法规则：{bad[:3]}")
        perm = (env.get(p + "PERMISSION_MODE") or "dontAsk").strip()
        if perm in _FORBIDDEN_PERMISSION_MODES:
            raise ConfigError(f"{p}PERMISSION_MODE 不允许 {perm}")
        model = (env.get(p + "MODEL") or default_model).strip()
        models = _csv(env.get(p + "MODELS") or "") or [model]
        if model not in models:
            models.insert(0, model)
        env_keys = _csv(env.get(p + "ENV_KEYS") or _DEFAULT_ENV_KEYS)
        for k in env_keys:
            if not _ENV_KEY_RE.match(k):
                raise ConfigError(f"{p}ENV_KEYS 含非法键 {k!r}")
        path_prepend = _csv(env.get(p + "PATH_PREPEND") or os.path.join(cwd, "bin"))
        cfg.apps[name] = AppConfig(
            name=name, cwd=cwd, model=model, effort=effort, models=models,
            allowed_tools=tools, permission_mode=perm, env_keys=env_keys,
            disallowed_tools=disallowed,
            path_prepend=path_prepend,
            system_prompt_file=(env.get(p + "SYSTEM_PROMPT_FILE") or "").strip(),
            isolate_cwd=(env.get(p + "ISOLATE_CWD") or "1").strip().lower()
            not in ("0", "false", "no", "off"),
        )
    return cfg


def read_env_file(path: str) -> dict[str, str]:
    """极简 KEY=VALUE 解析（systemd EnvironmentFile 同款子集），手动起服务时用。"""
    out: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip()] = v
    return out


# ── 凭证 ──────────────────────────────────────────────────────

def claude_credential_env() -> dict[str, str]:
    """和主 bot 同一份 Claude 凭证：env token 池里选中的那个 / .env 钉死的那个。

    只读：不探额度、不切号（切号是主 bot 的事，两个进程一起切会打架）。
    什么都没钉时返回 {}，claude 自己读 ~/.claude/.credentials.json。
    """
    try:
        from account_switcher import pinned_env_credential
        key, tok = pinned_env_credential()
        if key and tok:
            return {key: tok}
    except Exception as exc:  # noqa: BLE001
        log(PROFILE_TAG, "gateway", "warn", f"读凭证失败：{type(exc).__name__}")
    return {}


def load_claude_env_file(path: str) -> dict[str, str]:
    """CC_WEB_CLAUDE_ENV_FILE → 白名单内的 env。任何问题都抛 RunFailure（这一轮失败，不回落订阅凭证）。"""
    try:
        st = os.stat(path)
        if st.st_mode & 0o077:
            raise RunFailure("claude_env_unavailable", "claude env file must not be group/world accessible")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except RunFailure:
        raise
    except (OSError, ValueError) as exc:
        raise RunFailure("claude_env_unavailable", f"claude env file unreadable ({type(exc).__name__})")
    if isinstance(data, dict) and isinstance(data.get("env"), dict):
        data = data["env"]
    if not isinstance(data, dict):
        raise RunFailure("claude_env_unavailable", "claude env file must be a JSON object")
    out: dict[str, str] = {}
    for k, v in data.items():
        if k not in _CLAUDE_ENV_FILE_KEYS:
            continue
        if not isinstance(v, str) or not v.strip() or "\n" in v or "\r" in v:
            raise RunFailure("claude_env_unavailable", f"claude env file: bad value for {k}")
        out[k] = v.strip()
    if not any(out.get(k) for k in _CLAUDE_ENV_SECRET_KEYS):
        raise RunFailure("claude_env_unavailable",
                         "claude env file has no ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY")
    return out


# ── 摘要 / 脱敏 ───────────────────────────────────────────────

def _clip(text: str, n: int = _SUMMARY_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def summarize_tool_input(name: str, inp: Any) -> str:
    if not isinstance(inp, dict):
        return _clip(json.dumps(inp, ensure_ascii=False) if inp is not None else "")
    key = {
        "Bash": "command", "Read": "file_path", "Glob": "pattern",
        "WebFetch": "url", "WebSearch": "query",
    }.get(name)
    if key and inp.get(key):
        return _clip(str(inp[key]))
    if name == "Grep":
        pat = inp.get("pattern", "")
        where = inp.get("path") or inp.get("glob") or ""
        return _clip(f"{pat} @ {where}" if where else str(pat))
    return _clip(json.dumps(inp, ensure_ascii=False))


def summarize_tool_result(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text", "")))
            elif isinstance(c, dict):
                parts.append(f"[{c.get('type', 'item')}]")
        content = " ".join(parts)
    return _clip(str(content or ""))


class Redactor:
    """把本轮注入的敏感 env 值（RG_RUN_TOKEN 等）从任何对外事件里抹掉。"""

    def __init__(self, secrets_: list[str]):
        self._secrets = sorted({s for s in secrets_ if s and len(s) >= 6}, key=len, reverse=True)

    def __call__(self, obj: Any) -> Any:
        if not self._secrets:
            return obj
        if isinstance(obj, str):
            for s in self._secrets:
                if s in obj:
                    obj = obj.replace(s, "***")
            return obj
        if isinstance(obj, dict):
            return {k: self(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self(v) for v in obj]
        return obj


# ── 落盘 ──────────────────────────────────────────────────────

def _atomic_write_json(path: str, data: Any) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# ── Run ───────────────────────────────────────────────────────

class Run:
    """一次 Claude 调用。事件只追加，每条带递增 seq，同步落盘 runs/<id>.jsonl。"""

    _META_FIELDS = (
        "run_id", "app", "conversation_id", "status", "session_id", "resume_session_id",
        "session_reset", "text", "usage", "error", "model", "effort", "created_at",
        "started_at", "ended_at", "pid", "prompt_chars", "idempotency_key", "agent_version",
    )

    def __init__(self, gw: "Gateway", run_id: str, app: str, conversation_id: str):
        self.gw = gw
        self.run_id = run_id
        self.app = app
        self.conversation_id = conversation_id
        self.status = "queued"
        self.session_id: Optional[str] = None
        self.resume_session_id: Optional[str] = None
        self.session_reset = False
        self.text = ""
        self.usage: Optional[dict] = None
        self.error: Optional[dict] = None
        self.model = ""
        self.effort = ""
        self.created_at = _now()
        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None
        self.pid: Optional[int] = None
        self.prompt_chars = 0
        self.idempotency_key = ""
        self.agent_version: Optional[str] = None  # 起 Claude 进程那一刻的 agent 规则版本
        self.events: list[dict] = []
        self.cond = asyncio.Condition()
        self.cancel_requested = False
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.task: Optional[asyncio.Task] = None
        self.redact = Redactor([])

    # 路径
    @property
    def meta_path(self) -> str:
        return os.path.join(self.gw.runs_dir, f"{self.run_id}.json")

    @property
    def events_path(self) -> str:
        return os.path.join(self.gw.runs_dir, f"{self.run_id}.jsonl")

    @property
    def last_seq(self) -> int:
        return self.events[-1]["seq"] if self.events else 0

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def meta(self) -> dict:
        return {k: getattr(self, k) for k in self._META_FIELDS}

    def save_meta(self) -> None:
        _atomic_write_json(self.meta_path, self.meta())

    def public(self) -> dict:
        return {
            "run_id": self.run_id,
            "app": self.app,
            "conversation_id": self.conversation_id,
            "status": self.status,
            "session_id": self.session_id,
            "resume_session_id": self.resume_session_id,
            "session_reset": self.session_reset,
            "text": self.text,
            "usage": self.usage,
            "error": self.error,
            "model": self.model,
            "effort": self.effort,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "last_seq": self.last_seq,
            "agent_version": self.agent_version,
        }

    async def emit(self, etype: str, data: dict) -> dict:
        evt = {"seq": self.last_seq + 1, "type": etype, "ts": _iso(_now()),
               "data": self.redact(data)}
        line = json.dumps(evt, ensure_ascii=False)
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        async with self.cond:
            self.events.append(evt)
            self.cond.notify_all()
        return evt

    @classmethod
    def from_disk(cls, gw: "Gateway", meta: dict) -> "Run":
        r = cls(gw, meta["run_id"], meta.get("app", ""), meta.get("conversation_id", ""))
        for k in cls._META_FIELDS:
            if k in meta:
                setattr(r, k, meta[k])
        try:
            with open(r.events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r.events.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return r


class RunFailure(Exception):
    def __init__(self, code: str, message: str, status: str = "failed"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ── 运维列表（GET /v1/apps/{app}/runs）────────────────────────

def _slim_meta(meta: dict) -> dict:
    """只留列表要用的白名单键（缓存里也只存这些，正文 text 不进内存缓存）。"""
    return {k: meta.get(k) for k in _LIST_META_FIELDS}


def _ts(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return None
    return float(v)


def _span_ms(start: Optional[float], end: Optional[float]) -> Optional[int]:
    if start is None or end is None:
        return None
    return max(0, int(round((end - start) * 1000)))


def run_brief(meta: dict, now: float) -> dict:
    """一条 run 的运维摘要。逐键白名单拼出来，不透传 meta（不含 prompt / 输出 / env / token）。

    wait_ms：创建 → 开跑（排队中 = 到现在；排队时就被取消 = 到结束）。
    duration_ms：开跑 → 结束（在跑 = 到现在；没开跑过 = null）。
    """
    status = str(meta.get("status") or "")
    created, started, ended = (_ts(meta.get(k)) for k in ("created_at", "started_at",
                                                          "ended_at"))
    active = status in ACTIVE_STATUSES
    if started is not None:
        wait_ms = _span_ms(created, started)
        duration_ms = _span_ms(started, now if active else ended)
    else:
        wait_ms = _span_ms(created, now if active else ended)
        duration_ms = None
    err = meta.get("error")
    error = None
    if isinstance(err, dict):
        error = {"code": str(err.get("code") or ""),
                 "message": _clip(str(err.get("message") or ""), _LIST_ERROR_CHARS)}
    raw_usage = meta.get("usage")
    usage = None
    if isinstance(raw_usage, dict):
        usage = {k: raw_usage[k] for k in _LIST_USAGE_KEYS
                 if isinstance(raw_usage.get(k), (int, float))
                 and not isinstance(raw_usage.get(k), bool)} or None
    pid = meta.get("pid")
    return {
        "run_id": str(meta.get("run_id") or ""),
        "app": str(meta.get("app") or ""),
        "conversation_id": str(meta.get("conversation_id") or ""),
        "session_id": meta.get("session_id") or None,
        "resume_session_id": meta.get("resume_session_id") or None,
        "session_reset": bool(meta.get("session_reset")),
        "status": status,
        "model": str(meta.get("model") or ""),
        "effort": str(meta.get("effort") or ""),
        "created_at": _iso(created),
        "started_at": _iso(started),
        "ended_at": _iso(ended),
        "wait_ms": wait_ms,
        "duration_ms": duration_ms,
        "error": error,
        "usage": usage,
        # 只有在跑的 run 给 pid（运维要看 / 杀进程组时用）
        "pid": pid if status == "running" and isinstance(pid, int)
        and not isinstance(pid, bool) else None,
    }


# ── Gateway ───────────────────────────────────────────────────

class Gateway:
    def __init__(self, cfg: GatewayConfig):
        self.cfg = cfg
        self.runs: dict[str, Run] = {}
        self.active_by_conv: dict[str, str] = {}  # "app\0conv" → run_id
        self.sem = asyncio.Semaphore(cfg.max_concurrent)
        self.running = 0
        self.queued = 0
        self.started_at = _now()
        self.shutting_down = False
        os.makedirs(self.runs_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(cfg.state_dir, 0o700)
        except OSError:
            pass
        if cfg.claude_config_dir:
            os.makedirs(cfg.claude_config_dir, mode=0o700, exist_ok=True)
            os.chmod(cfg.claude_config_dir, 0o700)
        self.conversations: dict = _read_json(self.conv_path, {})
        self.sessions: dict = _read_json(self.sessions_path, {})
        self.idempotency: dict = _read_json(self.idem_path, {})
        # 运维列表的盘上元数据缓存：文件名 → ((ino, mtime_ns, size), slim meta | None)。
        # 只在工作线程里读写，_list_lock 串行化（同一时刻只有一次扫盘）。
        self._list_cache: dict[str, tuple[tuple[int, int, int], Optional[dict]]] = {}
        self._list_lock = threading.Lock()
        # agent 规则版本：app → (文件 stat 签名, info)；版本变了才重算哈希、才改 since
        self._agent_cache: dict[str, tuple[tuple, dict]] = {}
        self.agent_versions: dict = _read_json(self.agent_versions_path, {})

    # 路径
    @property
    def runs_dir(self) -> str:
        return os.path.join(self.cfg.state_dir, "runs")

    @property
    def conv_path(self) -> str:
        return os.path.join(self.cfg.state_dir, "conversations.json")

    @property
    def sessions_path(self) -> str:
        return os.path.join(self.cfg.state_dir, "sessions.json")

    @property
    def idem_path(self) -> str:
        return os.path.join(self.cfg.state_dir, "idempotency.json")

    @property
    def agent_versions_path(self) -> str:
        return os.path.join(self.cfg.state_dir, "agent_versions.json")

    @staticmethod
    def _conv_key(app: str, conv: str) -> str:
        return f"{app}\x00{conv}"

    @staticmethod
    def _conv_disk_key(app: str, conv: str) -> str:
        return f"{app}/{conv}"

    # ── 启动恢复 ──
    def recover(self) -> dict:
        """上次进程退出时还在 queued/running 的 run → interrupted；过期 run 清掉。"""
        interrupted, pruned = 0, 0
        cutoff = _now() - self.cfg.retention_days * 86400
        for fn in sorted(os.listdir(self.runs_dir)):
            if not fn.endswith(".json"):
                continue
            meta = _read_json(os.path.join(self.runs_dir, fn), None)
            if not isinstance(meta, dict) or not _RUN_ID_RE.match(str(meta.get("run_id", ""))):
                continue
            run = Run.from_disk(self, meta)
            if run.status in ACTIVE_STATUSES:
                _kill_orphan(run.pid)
                run.status = "interrupted"
                run.ended_at = _now()
                run.error = {"code": "interrupted",
                             "message": "gateway restarted while the run was in flight"}
                if not run.events or run.events[-1].get("type") not in TERMINAL_EVENTS:
                    evt = {"seq": run.last_seq + 1, "type": "error", "ts": _iso(_now()),
                           "data": dict(run.error)}
                    with open(run.events_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(evt, ensure_ascii=False) + "\n")
                    run.events.append(evt)
                run.save_meta()
                interrupted += 1
            elif (run.ended_at or run.created_at or 0) < cutoff:
                for p in (run.meta_path, run.events_path):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                pruned += 1
                continue
            # 不把历史 run 全量常驻内存：按需 load_run 读盘
        # 隔离工作目录（state_dir/work/<app>/<hash>）：久未使用的整个删掉（里面只有 rg-tool
        # 落地的原件缓存，会话本身在 ~/.claude 里）。
        work_root = os.path.join(self.cfg.state_dir, "work")
        for app_name in (os.listdir(work_root) if os.path.isdir(work_root) else []):
            app_dir = os.path.join(work_root, app_name)
            for h in (os.listdir(app_dir) if os.path.isdir(app_dir) else []):
                d = os.path.join(app_dir, h)
                try:
                    if os.path.getmtime(d) < cutoff:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
        now = _now()
        self.idempotency = {k: v for k, v in self.idempotency.items()
                            if now - float(v.get("at", 0)) < 86400}
        _atomic_write_json(self.idem_path, self.idempotency)
        return {"interrupted": interrupted, "pruned": pruned}

    def load_run(self, run_id: str) -> Optional[Run]:
        if not _RUN_ID_RE.match(run_id or ""):
            return None
        run = self.runs.get(run_id)
        if run is not None:
            return run
        meta = _read_json(os.path.join(self.runs_dir, f"{run_id}.json"), None)
        if not isinstance(meta, dict):
            return None
        run = Run.from_disk(self, meta)
        self.runs[run_id] = run
        return run

    # ── 会话映射 ──
    def _remember_session(self, run: Run, session_id: str) -> None:
        if not session_id:
            return
        key = self._conv_disk_key(run.app, run.conversation_id)
        now = _now()
        rec = self.conversations.get(key) or {}
        changed = rec.get("session_id") != session_id
        rec.update({"app": run.app, "conversation_id": run.conversation_id,
                    "session_id": session_id, "last_run_id": run.run_id, "updated_at": now})
        rec.setdefault("created_at", now)
        self.conversations[key] = rec
        _atomic_write_json(self.conv_path, self.conversations)
        if changed or session_id not in self.sessions:
            self.sessions[session_id] = {"app": run.app, "conversation_id": run.conversation_id,
                                         "created_at": now}
            _atomic_write_json(self.sessions_path, self.sessions)

    # ── agent 规则版本 ──
    @staticmethod
    def _agent_files(app: AppConfig) -> list[tuple[str, str]]:
        """(展示名, 绝对路径)：system prompt 文件 + PATH_PREPEND 目录里的常规文件（不递归、跳过隐藏）。"""
        def label(path: str) -> str:
            rel = os.path.relpath(path, app.cwd)
            return path if rel.startswith("..") else rel

        sp_file = app.system_prompt_file or os.path.join(app.cwd, "CLAUDE.md")
        out = [(label(sp_file), sp_file)]
        for d in app.path_prepend:
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                if e.name.startswith(".") or not e.is_file(follow_symlinks=True):
                    continue
                out.append((label(e.path), e.path))
                if len(out) >= _AGENT_MAX_FILES:
                    return out
        return out

    def agent_info(self, app: AppConfig) -> dict:
        """agent 规则版本号 = CLAUDE.md + bin/ 内容的哈希（不看 mtime）。

        rg-next 用它判断「规则变了，老话题要开新 session」：Claude 把 system prompt 定格在会话
        第一轮，CLAUDE.md 改了老会话吃不到。since = 网关第一次看到这个版本的时间（落盘，重启不变；
        第一次运行时取文件最新 mtime）。
        """
        files = self._agent_files(app)
        sig = []
        for name, path in files:
            try:
                st = os.stat(path)
                sig.append((name, st.st_ino, st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((name, None, None, None))
        sig_t = tuple(sig)
        cached = self._agent_cache.get(app.name)
        if cached and cached[0] == sig_t:
            return cached[1]
        h = hashlib.sha256(b"cc-web-agent-v1\n")
        listing, newest = [], 0.0
        for (name, path), (_, ino, mtime_ns, size) in zip(files, sig):
            if ino is None:
                digest = "missing"
            elif size > _AGENT_MAX_FILE_BYTES:
                digest = f"too-large:{size}"
            else:
                try:
                    with open(path, "rb") as f:
                        digest = hashlib.sha256(f.read()).hexdigest()
                except OSError:
                    digest = "unreadable"
                newest = max(newest, mtime_ns / 1e9)
            h.update(f"{name}\0{digest}\n".encode())
            listing.append({"path": name, "sha256": digest, "size": size})
        version = h.hexdigest()[:16]
        rec = self.agent_versions.get(app.name) or {}
        if rec.get("version") != version:
            since = _now() if rec.get("version") else (newest or _now())
            rec = {"version": version, "since": since}
            self.agent_versions[app.name] = rec
            try:
                _atomic_write_json(self.agent_versions_path, self.agent_versions)
            except OSError:
                pass
            if cached:
                log(PROFILE_TAG, "gateway", "info", f"app {app.name} agent rules → {version}")
        info = {"version": version, "since": _iso(float(rec["since"])), "files": listing}
        self._agent_cache[app.name] = (sig_t, info)
        return info

    def agent_brief(self, app: AppConfig) -> dict:
        info = self.agent_info(app)
        return {"version": info["version"], "since": info["since"], "files": len(info["files"])}

    # ── HTTP 入口（非 SSE）──
    def authorized(self, headers: dict) -> tuple[bool, int, dict]:
        if not self.cfg.token_ok:
            return False, 503, _err("gateway_not_configured",
                                    "CC_WEB_GATEWAY_TOKEN 未配置（或太短），网关拒绝服务")
        raw = headers.get("authorization", "")
        tok = raw[7:].strip() if raw[:7].lower() == "bearer " else ""
        # 常量时间比较；空 token 也走一遍 compare，避免时序上区分"没带"和"带错"
        ok = hmac.compare_digest(tok.encode() or b"-", self.cfg.token.encode())
        if not ok:
            return False, 401, _err("unauthorized", "missing or invalid bearer token")
        return True, 200, {}

    async def handle(self, method: str, path: str, query: dict, headers: dict,
                     body: bytes) -> tuple[int, dict]:
        ok, code, payload = self.authorized(headers)
        if not ok:
            return code, payload
        if path == "/v1/health":
            if method != "GET":
                return 405, _err("method_not_allowed", "use GET")
            return 200, self.health()
        if path == "/v1/runs":
            if method != "POST":
                return 405, _err("method_not_allowed", "use POST")
            return await self.create_run(headers, body)
        m = re.match(r"^/v1/runs/([^/]+)(/cancel)?$", path)
        if m:
            run = self.load_run(m.group(1))
            if run is None:
                return 404, _err("run_not_found", "no such run")
            if m.group(2):
                if method != "POST":
                    return 405, _err("method_not_allowed", "use POST")
                return await self.cancel_run(run)
            if method != "GET":
                return 405, _err("method_not_allowed", "use GET")
            return 200, run.public()
        m = re.match(r"^/v1/apps/([^/]+)/agent$", path)
        if m:
            if method != "GET":
                return 405, _err("method_not_allowed", "use GET")
            app = self.cfg.apps.get(m.group(1))
            if app is None:
                return 404, _err("app_not_found", "app is not configured on this gateway")
            return 200, {"app": app.name, **self.agent_info(app)}
        m = re.match(r"^/v1/apps/([^/]+)/runs$", path)
        if m:
            if method != "GET":
                return 405, _err("method_not_allowed", "use GET")
            return await self.list_app_runs(m.group(1), query)
        return 404, _err("not_found", "unknown endpoint")

    def health(self) -> dict:
        return {
            "ok": True,
            "version": VERSION,
            "uptime_sec": int(_now() - self.started_at),
            "running": self.running,
            "queued": self.queued,
            "max_concurrent": self.cfg.max_concurrent,
            "apps": sorted(self.cfg.apps),
            "claude_cli": os.path.basename(self.cfg.claude_cli),
            # 新增（只加字段）：每个 app 的 agent 规则版本号，见 agent_info()
            "agents": {name: self.agent_brief(a) for name, a in sorted(self.cfg.apps.items())},
        }

    # ── 运维只读列表 ──
    async def list_app_runs(self, app_name: str, query: dict) -> tuple[int, dict]:
        """GET /v1/apps/{app}/runs：在跑 / 排队 / 最近完成的 run 摘要（见 run_brief）。

        盘上 runs/*.json 是全集（含重启前的历史），本进程内存里的 run 覆盖盘上同 id 的
        记录（在跑的 run 以内存为准）。只读：不改任何状态、不把历史 run 塞进 self.runs。
        """
        if app_name not in self.cfg.apps:
            return 404, _err("app_not_found", "app is not configured on this gateway")
        raw = str(query.get("limit") or "").strip()
        if raw:
            try:
                limit = int(raw)
            except ValueError:
                return 400, _err("invalid_limit", "limit must be a non-negative integer")
            if limit < 0:
                return 400, _err("invalid_limit", "limit must be a non-negative integer")
        else:
            limit = _LIST_DEFAULT_LIMIT
        limit = min(limit, _LIST_MAX_LIMIT)

        metas = await asyncio.to_thread(self._scan_app_metas, app_name)
        by_id = {m["run_id"]: m for m in metas}
        for run in list(self.runs.values()):
            if run.app == app_name:
                by_id[run.run_id] = _slim_meta(run.meta())

        now = _now()
        running, queued, done = [], [], []
        for m in by_id.values():
            st = m.get("status")
            (running if st == "running" else queued if st == "queued" else done).append(m)
        # 在跑：开跑早的在前；排队：先来的在前（= 信号量 FIFO 顺序）；最近：结束晚的在前
        running.sort(key=lambda m: (_ts(m.get("started_at")) or _ts(m.get("created_at")) or 0,
                                    m["run_id"]))
        queued.sort(key=lambda m: (_ts(m.get("created_at")) or 0, m["run_id"]))
        done.sort(key=lambda m: (_ts(m.get("ended_at")) or _ts(m.get("created_at")) or 0,
                                 _ts(m.get("created_at")) or 0, m["run_id"]), reverse=True)
        return 200, {
            "app": app_name,
            "generated_at": _iso(now),
            "limit": limit,
            "running": [run_brief(m, now) for m in running],
            "queued": [run_brief(m, now) for m in queued],
            "recent": [run_brief(m, now) for m in done[:limit]],
            "recent_total": len(done),
        }

    def _scan_app_metas(self, app_name: str) -> list[dict]:
        """在工作线程里扫 runs/*.json，按 (inode, mtime_ns, size) 缓存解析结果（终态 run 不再变；
        save_meta 是 os.replace，每次写都换 inode）。"""
        out: list[dict] = []
        with self._list_lock:
            try:
                names = os.listdir(self.runs_dir)
            except OSError:
                return out
            seen: set[str] = set()
            for fn in names:
                if not fn.endswith(".json"):  # 跳过 .jsonl 事件文件和 .tmp.<pid>
                    continue
                path = os.path.join(self.runs_dir, fn)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                seen.add(fn)
                sig = (st.st_ino, st.st_mtime_ns, st.st_size)
                hit = self._list_cache.get(fn)
                if hit is not None and hit[0] == sig:
                    slim = hit[1]
                else:
                    meta = _read_json(path, None)
                    ok = isinstance(meta, dict) and _RUN_ID_RE.match(str(meta.get("run_id", "")))
                    slim = _slim_meta(meta) if ok else None
                    self._list_cache[fn] = (sig, slim)
                if slim is not None and slim.get("app") == app_name:
                    out.append(slim)
            for fn in [k for k in self._list_cache if k not in seen]:
                del self._list_cache[fn]
        return out

    async def create_run(self, headers: dict, body: bytes) -> tuple[int, dict]:
        try:
            req = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            return 400, _err("invalid_json", "body must be a JSON object")
        if not isinstance(req, dict):
            return 400, _err("invalid_json", "body must be a JSON object")

        app_name = req.get("app")
        app = self.cfg.apps.get(app_name) if isinstance(app_name, str) else None
        if app is None:
            return 400, _err("unknown_app", "app is not configured on this gateway")
        conv = req.get("conversation_id")
        if not isinstance(conv, str) or not _CONV_ID_RE.match(conv):
            return 400, _err("invalid_conversation_id",
                             "conversation_id must match [A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
        prompt = req.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return 400, _err("invalid_prompt", "prompt must be a non-empty string")
        if len(prompt) > self.cfg.max_prompt_chars:
            return 413, _err("prompt_too_long", f"prompt > {self.cfg.max_prompt_chars} chars")
        ctx = req.get("system_context")
        if ctx is not None and not isinstance(ctx, str):
            return 400, _err("invalid_system_context", "system_context must be a string")
        if ctx and len(ctx) > self.cfg.max_context_chars:
            return 413, _err("system_context_too_long",
                             f"system_context > {self.cfg.max_context_chars} chars")
        env_in = req.get("env") or {}
        if not isinstance(env_in, dict):
            return 400, _err("invalid_env", "env must be an object of strings")
        bad_keys = sorted(k for k in env_in if k not in app.env_keys)
        if bad_keys:
            return 400, _err("env_key_not_allowed",
                             f"env keys not allowed: {bad_keys}; allowed: {app.env_keys}")
        for k, v in env_in.items():
            if not isinstance(v, str) or len(v) > self.cfg.max_env_value_chars \
                    or "\x00" in v or "\n" in v:
                return 400, _err("invalid_env", f"env {k} must be a single-line string")
        model = req.get("model") or app.model
        if not isinstance(model, str) or model not in app.models:
            return 400, _err("model_not_allowed", f"model must be one of {app.models}")
        effort = req.get("effort") or app.effort
        if not isinstance(effort, str) or effort not in CLAUDE_EFFORT_LEVELS:
            return 400, _err("invalid_effort", f"effort must be one of {CLAUDE_EFFORT_LEVELS}")
        new_session = req.get("new_session", False)
        if not isinstance(new_session, bool):
            return 400, _err("invalid_new_session", "new_session must be a boolean")
        resume_sid = req.get("resume_session_id")
        if resume_sid is not None and new_session:
            return 400, _err("conflicting_session_options",
                             "resume_session_id and new_session are mutually exclusive")
        if resume_sid is not None:
            if not isinstance(resume_sid, str) or not _SESSION_ID_RE.match(resume_sid):
                return 400, _err("invalid_session_id", "resume_session_id must be a UUID")
            owner = self.sessions.get(resume_sid)
            if not owner or owner.get("app") != app.name \
                    or owner.get("conversation_id") != conv:
                return 409, _err("session_not_owned",
                                 "resume_session_id was not created by this gateway for this "
                                 "app + conversation_id")

        idem = (headers.get("idempotency-key") or "").strip()
        if idem and not _IDEM_RE.match(idem):
            return 400, _err("invalid_idempotency_key", "Idempotency-Key format invalid")
        idem_key = f"{app.name}/{idem}" if idem else ""
        if idem_key and idem_key in self.idempotency:
            prev = self.load_run(self.idempotency[idem_key].get("run_id", ""))
            if prev is not None:
                return 200, {"run_id": prev.run_id, "status": prev.status,
                             "conversation_id": prev.conversation_id, "idempotent_replay": True}

        ckey = self._conv_key(app.name, conv)
        active_id = self.active_by_conv.get(ckey)
        if active_id:
            active = self.runs.get(active_id)
            if active is not None and not active.finished:
                return 409, {**_err("conversation_busy",
                                    "this conversation already has a run in flight"),
                             "active_run_id": active_id}
        if self.queued >= self.cfg.max_queue and self.running >= self.cfg.max_concurrent:
            return 429, _err("queue_full", "too many runs queued; retry later")

        if resume_sid is None and not new_session:
            rec = self.conversations.get(self._conv_disk_key(app.name, conv)) or {}
            resume_sid = rec.get("session_id") or None

        run = Run(self, "run_" + uuid.uuid4().hex, app.name, conv)
        run.model, run.effort = model, effort
        run.resume_session_id = resume_sid
        run.prompt_chars = len(prompt)
        run.idempotency_key = idem
        run.redact = Redactor([v for k, v in env_in.items()] + self._claude_env_secret_values())
        self.runs[run.run_id] = run
        self.active_by_conv[ckey] = run.run_id
        run.save_meta()
        if idem_key:
            self.idempotency[idem_key] = {"run_id": run.run_id, "at": _now()}
            _atomic_write_json(self.idem_path, self.idempotency)
        await run.emit("status", {"state": "queued", "resume_session_id": resume_sid})
        self.queued += 1
        run.task = asyncio.create_task(
            self._execute(run, app, prompt, ctx or "", dict(env_in)))
        log(PROFILE_TAG, "gateway", "info",
            f"run {run.run_id} app={app.name} conv={conv} model={model} effort={effort} "
            f"resume={'yes' if resume_sid else 'no'} prompt_chars={len(prompt)} "
            f"ctx_chars={len(ctx or '')} env_keys={sorted(env_in)}")
        return 202, {"run_id": run.run_id, "status": run.status, "conversation_id": conv,
                     "resume_session_id": resume_sid,
                     "agent_version": self.agent_brief(app)["version"]}

    async def cancel_run(self, run: Run) -> tuple[int, dict]:
        if run.finished:
            return 200, {"run_id": run.run_id, "status": run.status, "already_finished": True}
        run.cancel_requested = True
        if run.proc is not None and run.proc.returncode is None:
            await _kill_group(run.proc)
        elif run.task is not None and run.status == "queued":
            run.task.cancel()
        # 等执行协程把终态写完，最多几秒
        for _ in range(100):
            if run.finished:
                break
            await asyncio.sleep(0.05)
        log(PROFILE_TAG, "gateway", "info", f"run {run.run_id} cancel → {run.status}")
        return 200, {"run_id": run.run_id, "status": run.status}

    # ── 执行 ──
    async def _execute(self, run: Run, app: AppConfig, prompt: str, ctx: str,
                       env_in: dict) -> None:
        acquired = False
        try:
            try:
                await self.sem.acquire()
                acquired = True
            finally:
                self.queued -= 1
            if run.cancel_requested:
                raise RunFailure("cancelled", "cancelled by client", "cancelled")
            self.running += 1
            run.status = "running"
            run.started_at = _now()
            run.save_meta()
            await run.emit("status", {"state": "running"})
            sid, text, usage, reset = await self._run_claude(run, app, prompt, ctx, env_in)
            run.session_id = sid or run.session_id
            run.text = run.redact(text)
            run.usage = usage
            run.session_reset = reset
            run.status = "succeeded"
            run.ended_at = _now()
            run.save_meta()
            await run.emit("done", {"session_id": run.session_id, "text": run.text,
                                    "usage": usage, "session_reset": reset})
        except asyncio.CancelledError:
            await self._fail(run, RunFailure("cancelled", "cancelled by client", "cancelled"))
        except RunFailure as exc:
            if run.cancel_requested and exc.code != "cancelled":
                exc = RunFailure("cancelled", "cancelled by client", "cancelled")
            await self._fail(run, exc)
        except Exception as exc:  # noqa: BLE001
            log(PROFILE_TAG, "gateway", "error",
                f"run {run.run_id} crashed: {type(exc).__name__}: {_clip(str(exc), 200)}")
            await self._fail(run, RunFailure("internal_error", f"{type(exc).__name__}"))
        finally:
            if acquired:
                self.running -= 1
                self.sem.release()
            ckey = self._conv_key(run.app, run.conversation_id)
            if self.active_by_conv.get(ckey) == run.run_id:
                self.active_by_conv.pop(ckey, None)
            run.proc = None

    async def _fail(self, run: Run, exc: RunFailure) -> None:
        if run.finished:
            return
        if self.shutting_down:
            exc = RunFailure("interrupted", "gateway stopped while the run was in flight",
                             "interrupted")
        run.status = exc.status
        run.error = run.redact({"code": exc.code, "message": exc.message})
        run.ended_at = _now()
        run.save_meta()
        await run.emit("error", {"code": exc.code, "message": exc.message,
                                 "session_id": run.session_id})
        log(PROFILE_TAG, "gateway", "warn",
            f"run {run.run_id} {exc.status}: {exc.code} {_clip(exc.message, 200)}")

    def build_cmd(self, app: AppConfig, model: str, effort: str,
                  resume_sid: Optional[str]) -> list[str]:
        cmd = [
            self.cfg.claude_cli,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            # 文件工具锁在 cwd、不读 user/project/local settings（hooks、全局权限放行）
            "--restricted",
            # 不带任何 MCP server（user 级配置里有浏览器 / Mac 控制类 MCP）
            "--strict-mcp-config",
            "--tools", ",".join(app.builtin_tools),
            "--allowedTools", *app.allowed_tools,
        ]
        if app.disallowed_tools:
            cmd += ["--disallowedTools", *app.disallowed_tools]
        cmd += [
            "--permission-mode", app.permission_mode,
            "--model", model,
        ]
        if effort:
            cmd += ["--effort", effort]
        sp_file = app.system_prompt_file or os.path.join(app.cwd, "CLAUDE.md")
        # --restricted 下 CLAUDE.md 不会自动加载，显式挂进 system prompt
        if os.path.isfile(sp_file):
            cmd += ["--append-system-prompt-file", sp_file]
        if resume_sid:
            cmd += ["--resume", resume_sid]
        return cmd

    def _claude_env_secret_values(self) -> list[str]:
        """中转站 token 也要从对外事件里抹掉（模型偶尔会把环境变量念出来）。读不到就算了：起 run 时会报错。"""
        if not self.cfg.claude_env_file:
            return []
        try:
            got = load_claude_env_file(self.cfg.claude_env_file)
        except RunFailure:
            return []
        return [got[k] for k in _CLAUDE_ENV_SECRET_KEYS if got.get(k)]

    def build_env(self, app: AppConfig, env_in: dict, effort: str) -> dict:
        env = {k: os.environ[k] for k in _INHERIT_ENV_KEYS if os.environ.get(k)}
        env.setdefault("HOME", os.path.expanduser("~"))
        env.setdefault("LANG", "C.UTF-8")
        env["PATH"] = ":".join([*app.path_prepend, _BASE_PATH,
                                os.path.expanduser("~/.local/bin")])
        if self.cfg.claude_env_file:
            # 中转站 / API key：只用这个文件里的白名单键，订阅凭证不进子进程
            env.update(load_claude_env_file(self.cfg.claude_env_file))
        else:
            env.update(claude_credential_env())
        if self.cfg.claude_config_dir:
            env["CLAUDE_CONFIG_DIR"] = self.cfg.claude_config_dir
        env["CC_LARK_MIRROR_OFF"] = "1"  # session-mirror hook 自我排除（restricted 下本就不跑 hook）
        env["DISABLE_AUTOUPDATER"] = "1"
        if effort:
            env["CLAUDE_CODE_EFFORT_LEVEL"] = effort
        env.update(env_in)
        return env

    @staticmethod
    def build_message(prompt: str, ctx: str) -> str:
        # 用户输入里伪造的上下文定界符一律抹掉（大小写 / 空格 / 属性变体都算），
        # 有没有 system_context 都抹——免得冒充服务端上下文
        prompt = _CTX_TAG_RE.sub("", prompt)
        if not ctx:
            return prompt
        # 服务端上下文里可能带客户资料（名字、文件名…）：里面的定界符同样不能提前闭合块
        ctx = _CTX_TAG_RE.sub(lambda m: m.group(0).replace("<", "&lt;"), ctx)
        return f"<rg_context>\n{ctx}\n</rg_context>\n\n{prompt}"

    def work_dir(self, app: AppConfig, conversation_id: str) -> str:
        """这一轮的 cwd：开了隔离就是这个 conversation 自己的目录（700，按需建）。"""
        if not app.isolate_cwd:
            return app.cwd
        h = hashlib.sha256(self._conv_disk_key(app.name, conversation_id).encode()).hexdigest()[:32]
        d = os.path.join(self.cfg.state_dir, "work", app.name, h)
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        os.utime(d)  # 在用的目录不会被启动清理当成过期
        return d

    async def _run_claude(self, run: Run, app: AppConfig, prompt: str, ctx: str,
                          env_in: dict) -> tuple[Optional[str], str, Optional[dict], bool]:
        if not os.path.isdir(app.cwd):
            raise RunFailure("app_cwd_missing", "configured app cwd does not exist")
        message = self.build_message(prompt, ctx)
        env = self.build_env(app, env_in, run.effort)
        resume = run.resume_session_id
        try:
            return (*await self._spawn_once(run, app, message, env, resume), False)
        except RunFailure as exc:
            if not (resume and exc.code == "session_missing") or run.cancel_requested:
                raise
        # resume 目标已被 Claude 清理（默认 30 天）：开新会话兜底，并告诉调用方上下文丢了
        log(PROFILE_TAG, "gateway", "warn",
            f"run {run.run_id} resume target gone; starting a fresh session")
        await run.emit("status", {"state": "session_reset", "reason": "session_missing",
                                  "lost_session_id": resume})
        run.session_reset = True
        return (*await self._spawn_once(run, app, message, env, None), True)

    async def _spawn_once(self, run: Run, app: AppConfig, message: str, env: dict,
                          resume_sid: Optional[str]
                          ) -> tuple[Optional[str], str, Optional[dict]]:
        cmd = self.build_cmd(app, run.model, run.effort, resume_sid)
        try:
            run.agent_version = self.agent_brief(app)["version"]
        except Exception:  # noqa: BLE001 — 版本号只是给调用方的提示，不能挡住 run
            run.agent_version = None
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.work_dir(app, run.conversation_id),
            env=env,
            limit=10 * 1024 * 1024,
            start_new_session=True,  # 自成进程组，cancel / 超时 killpg 一锅端
        )
        run.proc = proc
        run.pid = proc.pid
        run.save_meta()
        if run.cancel_requested:
            await _kill_group(proc)
        stderr_buf = bytearray()

        async def _drain_stderr() -> None:
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    return
                if len(stderr_buf) < 64 * 1024:
                    stderr_buf.extend(chunk[: 64 * 1024 - len(stderr_buf)])

        stderr_task = asyncio.create_task(_drain_stderr())
        try:
            proc.stdin.write(message.encode("utf-8") + b"\n")
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass

        session_id: Optional[str] = None
        full_text = ""
        final: Optional[tuple[str, Optional[dict]]] = None
        failure: Optional[RunFailure] = None
        tool_names: dict[str, str] = {}
        aborted = False
        loop = asyncio.get_running_loop()
        start = loop.time()
        last_output = start
        cfg = self.cfg
        try:
            while True:
                now = loop.time()
                if cfg.wall_clock > 0 and now - start >= cfg.wall_clock:
                    await _kill_group(proc)
                    failure = RunFailure(
                        "wall_clock_timeout",
                        f"run exceeded wall-clock limit ({int(cfg.wall_clock)}s)", "timeout")
                    break
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(),
                                                 timeout=cfg.check_interval)
                except asyncio.TimeoutError:
                    idle = loop.time() - last_output
                    has_kids = await loop.run_in_executor(None, _has_children, proc.pid)
                    limit = cfg.stuck_child_timeout if has_kids else cfg.idle_timeout
                    if limit > 0 and idle >= limit:
                        await _kill_group(proc)
                        failure = RunFailure(
                            "idle_timeout",
                            f"no output for {int(idle)}s"
                            + (" (child process stuck)" if has_kids else
                               " and no child process"), "timeout")
                        break
                    continue
                except ValueError:  # 单行超过 limit
                    continue
                if not raw:
                    break
                last_output = loop.time()
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                et = data.get("type")
                if et == "system":
                    sid = data.get("session_id")
                    if sid and sid != session_id:
                        session_id = sid
                        run.session_id = sid
                        run.save_meta()
                        self._remember_session(run, sid)
                        if data.get("subtype") == "init":
                            await run.emit("status", {"state": "session", "session_id": sid,
                                                      "model": data.get("model"),
                                                      "agent_version": run.agent_version})
                elif et == "stream_event":
                    evt = data.get("event") or {}
                    if evt.get("type") == "content_block_delta":
                        delta = evt.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            full_text += delta["text"]
                            await run.emit("text_delta", {"text": delta["text"]})
                elif et == "assistant":
                    for block in (data.get("message") or {}).get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = str(block.get("name") or "")
                            tid = str(block.get("id") or "")
                            tool_names[tid] = name
                            await run.emit("tool_use", {
                                "id": tid, "name": name,
                                "summary": summarize_tool_input(name, block.get("input")),
                            })
                elif et == "user":
                    content = (data.get("message") or {}).get("content")
                    for block in content if isinstance(content, list) else []:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tid = str(block.get("tool_use_id") or "")
                            await run.emit("tool_result", {
                                "id": tid, "name": tool_names.get(tid, ""),
                                "is_error": bool(block.get("is_error")),
                                "summary": summarize_tool_result(block.get("content")),
                            })
                elif et == "result":
                    sid = data.get("session_id")
                    if sid and sid != session_id:
                        session_id = sid
                        run.session_id = sid
                        self._remember_session(run, sid)
                    text = _extract_text_content(data.get("result", ""))
                    usage = _usage_from_result(data)
                    if usage:
                        await run.emit("usage", usage)
                    err = _classify_result_error(data, text or full_text)
                    if err is not None:
                        blob = f"{text or full_text}\n{_extract_errors_text(data)}"
                        if is_session_missing_error_text(blob):
                            failure = RunFailure("session_missing", "resume target not found")
                        else:
                            failure = RunFailure(_error_code(data, err[0]), _clip(err[0], 500))
                    else:
                        final = ((text or full_text).strip(), usage)
        except asyncio.CancelledError:
            # 任务被取消（进程关停）：同步 SIGKILL 整组，不在取消路径上再 await
            aborted = True
            _killpg_now(proc)
            stderr_task.cancel()
            raise
        finally:
            if proc.returncode is None and not aborted:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    await _kill_group(proc)
            if not aborted:
                try:
                    await asyncio.wait_for(stderr_task, timeout=5)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    stderr_task.cancel()
        stderr_text = stderr_buf.decode("utf-8", errors="replace").strip()

        if run.cancel_requested:
            raise RunFailure("cancelled", "cancelled by client", "cancelled")
        if failure is not None:
            raise failure
        if final is not None:
            return session_id, final[0], final[1]
        if resume_sid and is_session_missing_error_text(stderr_text):
            raise RunFailure("session_missing", "resume target not found")
        rc = proc.returncode
        if rc and rc < 0:
            raise RunFailure("killed", f"claude killed by signal {-rc}")
        raise RunFailure("claude_exit",
                         f"claude exited with code {rc} without a result: "
                         f"{_clip(stderr_text, 300) or 'no stderr'}")


def _usage_from_result(data: dict) -> Optional[dict]:
    usage = data.get("usage")
    out: dict = {}
    if isinstance(usage, dict):
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens"):
            if isinstance(usage.get(k), (int, float)):
                out[k] = usage[k]
    for k in ("total_cost_usd", "duration_ms", "duration_api_ms", "num_turns"):
        if isinstance(data.get(k), (int, float)):
            out[k] = data[k]
    model_usage = data.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        out["models"] = sorted(model_usage)
    return out or None


def _error_code(data: dict, msg: str) -> str:
    low = (msg or "").lower()
    if "用量已达上限" in msg or data.get("api_error_status") == 429 or "rate limit" in low:
        return "rate_limited"
    if "authenticat" in low or "oauth" in low:
        return "auth_error"
    return "claude_error"


def _err(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


async def _kill_group(proc: asyncio.subprocess.Process, grace: float = 3.0) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except asyncio.TimeoutError:
            pass


def _killpg_now(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_orphan(pid: Any) -> None:
    """上一代网关留下的 claude 进程组（systemd 正常会一起杀；手动跑时可能残留）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().replace(b"\x00", b" ")
    except OSError:
        return
    if b"claude" not in cmdline or b"--print" not in cmdline:
        return
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ── HTTP 层（asyncio 裸实现：只服务本机 rg-next server，不引第三方依赖）──

_REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
            404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
            413: "Payload Too Large", 429: "Too Many Requests",
            500: "Internal Server Error", 503: "Service Unavailable"}


async def _write_json(writer: asyncio.StreamWriter, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    head = (f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n").encode()
    writer.write(head + body)
    await writer.drain()


def _sse_frame(evt: dict) -> bytes:
    payload = json.dumps(evt["data"], ensure_ascii=False)
    return (f"id: {evt['seq']}\nevent: {evt['type']}\ndata: {payload}\n\n").encode("utf-8")


async def _serve_sse(gw: Gateway, run: Run, after: int,
                     writer: asyncio.StreamWriter) -> None:
    writer.write((
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/event-stream; charset=utf-8\r\n"
        "Cache-Control: no-store\r\n"
        "X-Accel-Buffering: no\r\n"
        "Connection: close\r\n\r\n"
        "retry: 2000\n\n").encode())
    await writer.drain()
    sent = after
    terminal_sent = False
    while True:
        pending = [e for e in run.events if e["seq"] > sent]
        for evt in pending:
            writer.write(_sse_frame(evt))
            sent = evt["seq"]
            if evt["type"] in TERMINAL_EVENTS:
                terminal_sent = True
        if pending:
            await writer.drain()
        if terminal_sent and sent >= run.last_seq:
            return
        if run.finished and run.task is None and sent >= run.last_seq:
            return  # 从盘上读回来的历史 run，没有更多事件
        async with run.cond:
            try:
                await asyncio.wait_for(
                    run.cond.wait_for(lambda: run.last_seq > sent),
                    timeout=gw.cfg.heartbeat_sec)
            except asyncio.TimeoutError:
                writer.write(b": ping\n\n")
                await writer.drain()


async def handle_connection(gw: Gateway, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
    try:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                asyncio.TimeoutError):
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            await _write_json(writer, 400, _err("bad_request", "malformed request line"))
            return
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > _MAX_BODY_BYTES:
            await _write_json(writer, 413, _err("body_too_large", "body too large"))
            return
        body = b""
        if length:
            try:
                body = await asyncio.wait_for(reader.readexactly(length), timeout=30)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                return
        parts = urlsplit(target)
        path = parts.path.rstrip("/") or "/"
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}

        m = re.match(r"^/v1/runs/([^/]+)/events$", path)
        if m:
            ok, code, payload = gw.authorized(headers)
            if not ok:
                await _write_json(writer, code, payload)
                return
            if method != "GET":
                await _write_json(writer, 405, _err("method_not_allowed", "use GET"))
                return
            run = gw.load_run(m.group(1))
            if run is None:
                await _write_json(writer, 404, _err("run_not_found", "no such run"))
                return
            raw_after = query.get("after") or headers.get("last-event-id") or "0"
            try:
                after = max(0, int(raw_after))
            except ValueError:
                await _write_json(writer, 400, _err("invalid_after", "after must be an int"))
                return
            await _serve_sse(gw, run, after, writer)
            return

        status, payload = await gw.handle(method, path, query, headers, body)
        await _write_json(writer, status, payload)
    except (ConnectionResetError, BrokenPipeError):
        pass
    except Exception as exc:  # noqa: BLE001
        log(PROFILE_TAG, "http", "error", f"handler crashed: {type(exc).__name__}: {exc}")
        try:
            await _write_json(writer, 500, _err("internal_error", type(exc).__name__))
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def start_server(gw: Gateway, host: Optional[str] = None,
                       port: Optional[int] = None) -> asyncio.base_events.Server:
    return await asyncio.start_server(
        lambda r, w: handle_connection(gw, r, w),
        host or gw.cfg.host, gw.cfg.port if port is None else port,
        limit=_MAX_BODY_BYTES + 64 * 1024,
    )


async def _amain(env_file: str) -> int:
    if env_file:
        for k, v in read_env_file(env_file).items():
            os.environ.setdefault(k, v)
    try:
        cfg = load_config()
    except ConfigError as exc:
        log(PROFILE_TAG, "gateway", "error", f"配置错误：{exc}")
        return 2
    gw = Gateway(cfg)
    rec = gw.recover()
    server = await start_server(gw)
    log(PROFILE_TAG, "gateway", "info",
        f"listening on {cfg.host}:{cfg.port} apps={sorted(cfg.apps)} "
        f"max_concurrent={cfg.max_concurrent} token={'set' if cfg.token_ok else 'MISSING'} "
        f"recovered={rec}")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log(PROFILE_TAG, "gateway", "info", "shutting down")
    server.close()
    gw.shutting_down = True
    inflight = [r for r in gw.runs.values() if not r.finished]
    for run in inflight:
        if run.proc is not None:
            await _kill_group(run.proc, grace=2.0)
        elif run.task is not None:
            run.task.cancel()
    for run in inflight:
        if run.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(run.task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="cc-lark web gateway")
    ap.add_argument("--env-file", default=os.getenv("CC_WEB_ENV_FILE", ""),
                    help="额外读一份 KEY=VALUE 配置（systemd 下用 EnvironmentFile，不需要）")
    args = ap.parse_args(argv)
    return asyncio.run(_amain(args.env_file))


if __name__ == "__main__":
    sys.exit(main())
