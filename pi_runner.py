"""本地调用 pi（earendil-works/pi 的编程 agent，CLI 叫 `pi`）的统一入口。

    pi --mode json --session-dir <PI_SESSION_DIR> [--session <会话文件>]
       --model google/<id> [--thinking <level>] [--tools read,grep,find,ls]
       [--append-system-prompt <临时文件>] -e pi_extensions/cc_lark.ts
       [--skill ~/.claude/skills] [--skill <cwd>/.claude/skills]
    （prompt 从 stdin 进：管道进来的 stdin 会拼在首条 prompt 前，没有 argv 长度上限）

装法：`sudo npm install -g @earendil-works/pi-coding-agent`（1.5.2 起包名从
@mariozechner 迁到 @earendil-works）。

线格式（JSON 模式，1.5.2 实测）：第一行是会话头 {type:"session", id, cwd}，之后是
agent_start / turn_start / message_start / message_update（assistantMessageEvent 是
text_delta / thinking_delta / toolcall_* 增量）/ message_end（权威消息：content、usage、
cost、stopReason、errorMessage）/ tool_execution_start|update|end / turn_end /
agent_end / agent_settled。stdout 只有 JSONL，告警和日志在 stderr。
⚠️ 模型出错（stopReason=error/aborted）时 JSON 模式退出码仍是 0，要自己看 message_end。

会话：pi 的会话是一个 jsonl 文件，id 在会话头里。默认按 cwd 分目录存，换了 cwd 再用
`--session <id>` 会在终端里问「Fork this session into current directory? [y/N]」，
stdin 不是终端就当 N 直接退出。所以这里用 `--session-dir` 钉一个扁平目录，续轮时按
id 找到文件、用 `--session <文件路径>` 打开（跨 cwd 也直接续上，实测）。文件不在了
（换机器 / 被清）就退回新会话。

模型：**只接 Gemini**（用户 11-20 定：Gemini 4 全家 Neon / Hydrogen / Carbon / Argon +
3.8 Flash，见 PI_GEMINI_MODELS）。pi 自带的 Google 模型表里只有 Argon 和 3.8 Flash，
Neon / Hydrogen / Carbon 会被当「自定义 id」照抄 Argon 的元数据（价格、档位都不对），
所以扩展每轮用 registerProvider("google", {models}) 把 Google 的模型表整张换成这 5 款。
凭据：GEMINI_API_KEY；本机没单独配时借 agy 那把（AGY_API_KEY，PI_GEMINI_KEY_FROM 可改）。

本地模型（11-22 加，mini2 上的 Ollama 跑 Qwen3.6 35B-A3B 去审查版）：配了 PI_LOCAL_MODELS 的
机器上，扩展再注册一个 OpenAI 兼容的 provider（默认叫 ollama，模型写成 ollama/<id>），见
local_provider_config。思考开关走 reasoning_effort（Ollama 实测 none=不想、low~high=想；
Qwen 只有开 / 关，没有深浅），所以档位是 none / low / medium / high。Ollama 不花钱，footer 不显示花费；
服务器上同一套开关接的是收费的 OpenAI 兼容服务，PI_LOCAL_COST 给价目、PI_LOCAL_EXTRA_BODY 并进请求体
（Venice 用它关掉自带的系统提示），见 local_provider_config。可以同时挂几家（11-22 起 Lebrel 为主 +
Venice：PI_LOCAL_MORE_PROVIDERS + PI_LOCAL_<名字>_<项>，见 bot_config.pi_local_env），/model 面板
一家一个按钮、正文报余额（local_balance_lines）。

推理强度：`--thinking`。Gemini 只有 low / medium / high，Neon / Hydrogen 多一档 xhigh
（11-20 直接打 Gemini API 实测：这两款收 thinkingLevel=xhigh，Carbon 回 400）。
none / minimal 落到 low，max 落到该模型的最高档。⚠️ pi 1.5.2 的 Google 适配层把档位写死成
minimal/low/medium/high，配 xhigh 会直接报 Unsupported Google thinking level mapping；所以
模型表里 xhigh 先映射成 high 让 pi 放行，扩展在 before_provider_request 里再把这两款的
thinkingLevel 改写成 XHIGH。

MCP：pi 没有 --mcp-config，全局 ~/.pi/agent/mcp.json 又是所有会话共用的（并发话题
的 CC_LARK_* 会串）。所以每轮把 cc-lark 运行时 MCP 配置（Claude 同款 JSON）放进
环境变量 CC_LARK_PI_MCP_CONFIG，同一个扩展在会话里 registerMcpServer
（exposure=direct）。pi 会把 server 名里的 `-` 换成 `_`，工具名是
`mcp__cc_lark__<tool>`，提示词里有一段转译。

权限：pi 没有审批机制，工具默认全开。profile 关了 PI_DANGEROUS_SKIP 且本轮不是
bypassPermissions 时只给只读工具（read,grep,find,ls），MCP 工具不受影响。

Claude Code 的规则 / 记忆 / skill：pi 自己会沿 cwd 往上加载 AGENTS.md / CLAUDE.md、
扫 ~/.agents/skills；~/.claude/skills 和项目 .claude/skills 用 --skill 带进去；全局
~/.claude/CLAUDE.md 和记忆索引拼进系统提示（见 claude_context.py）。
CC_LARK_PI_CLAUDE_CONTEXT=0 关掉。

思考过程上卡片（11-24 加）：DeepSeek 这类模型把「我下一步要干嘛」全写在思考（reasoning）里，
正文只在最后一条消息出现（实测一轮 62 次工具调用、34 段思考、正文只有 2 段）。Claude / Codex
会在工具之间写叙述，卡片和收尾的「🔍 过程」靠的就是它；pi 这边以前不转思考，于是跑完只剩结论。
现在每段思考结束（thinking_end）时压成一行「💭 …」（最多 THINKING_PREVIEW_CHARS 字）走
on_text_chunk 上卡片，不进返回的正文。CC_LARK_PI_SHOW_THINKING=0 关掉。

计费：走 Gemini API key。每条 assistant 消息自带 usage + cost（美元，pi 按
PI_GEMINI_MODELS 里的价目表算，Gemini 4 的价格没有官方页可查，是估算），footer 显示
上下文占用 + 本轮花费。
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import tempfile
from typing import Callable, Optional

from bot_config import (
    pi_local_env,
    pi_local_models,
    pi_local_provider,
    pi_local_providers,
    pi_local_ref,
    resolve_claude_wall_clock_limit,
)
from claude_context import CLAUDE_SKILLS_DIR, build_claude_context_brief
from claude_runner import (
    _fire_callback,
    _has_children,
    cc_lark_mcp_config,
    is_fatal_error_text,
)

IDLE_TIMEOUT = 600  # 无输出且无子进程 → 视为挂死
STUCK_CHILD_TIMEOUT = int(os.getenv("STUCK_CHILD_TIMEOUT_SEC", "3600"))
_CHECK_INTERVAL = 30

# 基础三档；Neon / Hydrogen 多一档 xhigh（见 PI_GEMINI_MODELS）
PI_EFFORT_LEVELS = ("low", "medium", "high")
# 本地模型多一档 none（关掉思考）
PI_LOCAL_EFFORT_LEVELS = ("none",) + PI_EFFORT_LEVELS

# 本地 provider 的默认值（PI_LOCAL_* 环境变量可改）。上下文窗口要和本地服务实际给的一致：
# Ollama 的 OpenAI 接口不收 num_ctx，得在 Modelfile 里写 PARAMETER num_ctx。
LOCAL_BASE_URL = "http://127.0.0.1:11434/v1"
LOCAL_CONTEXT_WINDOW = 65_536
LOCAL_MAX_TOKENS = 16_384

DEFAULT_MODEL = "google/gemini-3.8-flash"
DEFAULT_SESSION_DIR = os.path.expanduser("~/.feishu-claude/pi-sessions")
EXTENSION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_extensions", "cc_lark.ts")
READ_ONLY_TOOLS = "read,grep,find,ls"

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_CONTEXT_WINDOW = 1_048_576

# pi 能用的全部模型（id → 显示名, (输入, 输出, 缓存命中) 美元/百万 token, 最大输出, 有没有 xhigh）。
# 3.8 Flash / Argon 的价格和输出上限照抄 pi 1.5.2 自带的表；Neon / Hydrogen / Carbon 没有官方
# 价目页，按 11-20 网上报道估（Neon $3/$15、Hydrogen $1.5/$7.5、Carbon 同 Argon 的首发价 $2/$10，
# 12-01 起 Carbon / Argon 据称涨到 $4/$20），输出上限按 Argon 的 32k 保守给。
# ⚠️ 名单和 bot_config.PI_MODELS 必须一致（tests/test_pi_runner.py 有断言）。
PI_GEMINI_MODELS: dict[str, tuple[str, tuple[float, float, float], int, bool]] = {
    "gemini-3.8-flash": ("Gemini 3.8 Flash", (0.75, 3.75, 0.075), 65_536, False),
    "gemini-4-neon": ("Gemini 4 Neon", (3.0, 15.0, 0.15), 32_768, True),
    "gemini-4-hydrogen": ("Gemini 4 Hydrogen", (1.5, 7.5, 0.15), 32_768, True),
    "gemini-4-carbon": ("Gemini 4 Carbon", (2.0, 10.0, 0.1), 32_768, False),
    "gemini-4-argon": ("Gemini 4 Argon", (2.0, 10.0, 0.1), 32_768, False),
}

# 给 /model 用的静态清单（别名, 显示名）；别名在 commands.MODEL_ALIASES 里解析
PI_MODEL_BUTTONS: list[tuple[str, str]] = [
    ("pi-neon", "✴️ Gemini 4 Neon"),
    ("pi-hydrogen", "💧 Gemini 4 Hydrogen"),
    ("pi-carbon", "🪨 Gemini 4 Carbon"),
    ("pi-argon", "🔆 Gemini 4 Argon"),
    ("pi-flash", "🚀 Gemini 3.8 Flash"),
]

# pi 工具名 → 卡片认识的名字（dispatcher._format_tool 按名字挑图标和主参数）
_TOOL_DISPLAY = {"find": "glob", "ls": "list_dir"}

# 每段思考在卡片上最多显示多少字（11-24 实测 DeepSeek 一段思考中位数约 390 字、p90 约 1600 字）
THINKING_PREVIEW_CHARS = 200


def resolve_pi_bin(configured: Optional[str] = None) -> str:
    if configured:
        return os.path.expanduser(configured)
    found = shutil.which("pi")
    if found:
        return found
    for cand in ("/usr/bin/pi", "/usr/local/bin/pi", "/opt/homebrew/bin/pi", "~/.local/bin/pi"):
        path = os.path.expanduser(cand)
        if os.path.exists(path):
            return path
    return "pi"


def _bare_model(model: Optional[str]) -> str:
    low = (model or "").strip().lower()
    return low[len("google/"):] if low.startswith("google/") else low


def is_local_model(model: Optional[str]) -> bool:
    return pi_local_ref(model) is not None


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def _local_int(provider: Optional[str], key: str, default: int) -> int:
    raw = pi_local_env(provider, key)
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def effort_levels_for_model(model: Optional[str]) -> tuple[str, ...]:
    """该模型能用的推理档位：low / medium / high，Neon / Hydrogen 多一档 xhigh，本地模型多一档 none。"""
    if is_local_model(model):
        return PI_LOCAL_EFFORT_LEVELS
    meta = PI_GEMINI_MODELS.get(_bare_model(model))
    return PI_EFFORT_LEVELS + (("xhigh",) if meta and meta[3] else ())


def normalize_thinking(effort: Optional[str], model: Optional[str] = None) -> Optional[str]:
    """cc-lark 的 effort → pi 的 --thinking；空 / auto / default = 不传（跟 pi 自己的默认）。

    收敛到该模型真有的档位：none / off / minimal → low（Gemini 关不掉思考，pi 也会这么抬）；
    xhigh / max → 有 xhigh 的模型给 xhigh，其余给 high。不知道模型时 xhigh / max 原样留 xhigh。
    """
    e = (effort or "").strip().lower()
    if not e or e in ("auto", "default"):
        return None
    if is_local_model(model):
        # 本地模型：none / off → pi 的 off（发 reasoning_effort=none 关掉思考），xhigh / max 封顶 high
        if e in ("none", "off"):
            return "off"
        if e == "minimal":
            return "low"
        if e in PI_EFFORT_LEVELS:
            return e
        if e in ("xhigh", "max"):
            return "high"
        raise ValueError(f"invalid pi thinking level {e!r}; expected one of none/low/medium/high")
    if e in ("none", "off", "minimal"):
        return "low"
    if e in PI_EFFORT_LEVELS:
        return e
    if e in ("xhigh", "max"):
        if model is None or "xhigh" in effort_levels_for_model(model):
            return "xhigh"
        return "high"
    raise ValueError(f"invalid pi thinking level {e!r}; expected one of low/medium/high/xhigh/max")


def provider_config(api_key_env: str = "GEMINI_API_KEY") -> dict:
    """扩展拿去 registerProvider("google", …) 的配置：Google 的模型表整张换成 PI_GEMINI_MODELS。"""
    models = []
    for mid, (name, (cin, cout, cread), max_out, xhigh) in PI_GEMINI_MODELS.items():
        models.append({
            "id": mid,
            "name": name,
            "api": "google-generative-ai",
            "baseUrl": GEMINI_BASE_URL,
            "reasoning": True,
            "input": ["text", "image"],
            "cost": {"input": cin, "output": cout, "cacheRead": cread, "cacheWrite": 0},
            "contextWindow": _CONTEXT_WINDOW,
            "maxTokens": max_out,
            # xhigh 先映射成 high（pi 只放行四个 Google 值），扩展发请求前改写成 XHIGH
            "thinkingLevelMap": {
                "off": None, "minimal": None, "low": "low", "medium": "medium", "high": "high",
                "xhigh": "high" if xhigh else None, "max": None,
            },
        })
    return {
        "provider": "google",
        "xhighModels": [mid for mid, meta in PI_GEMINI_MODELS.items() if meta[3]],
        "config": {
            "name": "Google",
            "baseUrl": GEMINI_BASE_URL,
            "api": "google-generative-ai",
            "apiKey": f"${api_key_env}",
            "models": models,
        },
    }


def local_provider_config(provider: Optional[str] = None) -> Optional[dict]:
    """本机配了 PI_LOCAL_MODELS 时，扩展拿去注册的本地 OpenAI 兼容 provider；没配返回 None。

    provider：哪一家（默认主 provider）；PI_LOCAL_MORE_PROVIDERS 里的那几家读
    PI_LOCAL_<名字>_<项>，见 bot_config.pi_local_env。

    PI_LOCAL_BASE_URL（默认本机 Ollama）、PI_LOCAL_API_KEY_ENV（要鉴权的服务填装 key 的环境
    变量名，Ollama 不用）、PI_LOCAL_CONTEXT_WINDOW（默认 64K，和 Modelfile 的 num_ctx 对齐）、
    PI_LOCAL_MAX_TOKENS（单条回复上限，默认 16K）、PI_LOCAL_VISION=0 关掉图片输入。

    收费的 OpenAI 兼容服务（11-22 起服务器上的 Venice）另有两项：
    PI_LOCAL_COST=输入,输出[,缓存命中[,缓存写入]]（美元/百万 token），配了 footer 才显示花费；
    PI_LOCAL_EXTRA_BODY=JSON 对象，扩展在请求发出前并进请求体（Venice 用它关掉平台自带的
    系统提示：{"venice_parameters":{"include_venice_system_prompt":false}}）。
    """
    prov = (provider or pi_local_provider()).strip().lower()
    ids = pi_local_models(prov)
    if not ids:
        return None
    base_url = (pi_local_env(prov, "BASE_URL") or LOCAL_BASE_URL).rstrip("/")
    key_env = pi_local_env(prov, "API_KEY_ENV")
    window = _local_int(prov, "CONTEXT_WINDOW", LOCAL_CONTEXT_WINDOW)
    max_out = min(_local_int(prov, "MAX_TOKENS", LOCAL_MAX_TOKENS), window)
    inputs = ["text"] if pi_local_env(prov, "VISION") == "0" else ["text", "image"]
    # Ollama 的 /v1/chat/completions 收 reasoning_effort：none 关掉思考，其余都是开
    compat = {"thinkingFormat": "openai", "supportsReasoningEffort": True}
    cost = _local_cost(prov)
    models = [{
        "id": mid,
        "name": mid,
        "api": "openai-completions",
        "baseUrl": base_url,
        "reasoning": True,
        "input": inputs,
        "cost": dict(cost),
        "contextWindow": window,
        "maxTokens": max_out,
        "compat": compat,
        "thinkingLevelMap": {
            "off": "none", "minimal": "low", "low": "low", "medium": "medium", "high": "high",
            "xhigh": None, "max": None,
        },
    } for mid in ids]
    out = {
        "provider": prov,
        "config": {
            "name": prov.capitalize() if prov != "ollama" else "Local",
            "baseUrl": base_url,
            "api": "openai-completions",
            # 不要鉴权的服务（Ollama）也得给个非空 key，pi 才会把模型当成可用
            "apiKey": f"${key_env}" if key_env else "local",
            "compat": compat,
            "models": models,
        },
    }
    extra = _local_extra_body(prov)
    if extra:
        out["extraBody"] = extra
    return out


def local_provider_configs() -> list[dict]:
    """本机配了的全部本地 provider（主 provider 在前）。"""
    return [cfg for cfg in (local_provider_config(p) for p in pi_local_providers()) if cfg]


def _local_cost(provider: Optional[str] = None) -> dict:
    """PI_LOCAL_COST=输入,输出[,缓存命中[,缓存写入]]（美元/百万 token）→ pi 模型表的 cost；没配或写错都是 0。"""
    keys = ("input", "output", "cacheRead", "cacheWrite")
    cost = dict.fromkeys(keys, 0.0)
    raw = pi_local_env(provider, "COST")
    if not raw:
        return cost
    try:
        vals = [float(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        print(f"[run_pi] PI_LOCAL_COST 不是数字列表，按 0 算：{raw!r}", flush=True)
        return cost
    if len(vals) > len(keys) or any(v < 0 for v in vals):
        print(f"[run_pi] PI_LOCAL_COST 最多 4 个非负数，按 0 算：{raw!r}", flush=True)
        return cost
    cost.update(zip(keys, vals))
    return cost


def _local_extra_body(provider: Optional[str] = None) -> Optional[dict]:
    """PI_LOCAL_EXTRA_BODY（JSON 对象）：扩展在请求发出前并进本地 provider 的请求体。"""
    raw = pi_local_env(provider, "EXTRA_BODY")
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except ValueError:
        val = None
    if not isinstance(val, dict) or not val:
        print(f"[run_pi] PI_LOCAL_EXTRA_BODY 要是非空 JSON 对象，忽略：{raw[:200]!r}", flush=True)
        return None
    return val


def provider_configs() -> list[dict]:
    """扩展每轮要注册的全部 provider：Google（换成 PI_GEMINI_MODELS）+ 本机配了的本地 provider。"""
    return [provider_config()] + local_provider_configs()


def _gemini_key_available() -> bool:
    src = os.getenv("PI_GEMINI_KEY_FROM", "AGY_API_KEY")
    return bool(os.getenv("GEMINI_API_KEY") or (src and os.getenv(src)))


def local_model_label(provider: str, model_id: str) -> str:
    """本地模型的按钮字：PI_LOCAL[_<名字>]_LABEL（配一款模型时用）；没配就是 🖥️ <id>，挂了几家再带上家名。"""
    label = pi_local_env(provider, "LABEL")
    if label and len(pi_local_models(provider)) == 1:
        return label
    if len(pi_local_providers()) > 1:
        return f"🖥️ {provider.capitalize()} · {model_id}"
    return f"🖥️ {model_id}"


def model_buttons() -> list[tuple[str, str]]:
    """/model 面板的 (命令参数, 按钮字)：本地模型在前；本机没有 Gemini key 就不摆 Gemini。

    PI_GEMINI_BUTTONS=0：有本地模型时不摆 Gemini（11-22 服务器上那把 Gemini key 没钱了，点了只会 402）。"""
    buttons = [
        (f"{prov}/{mid}", local_model_label(prov, mid))
        for prov in pi_local_providers() for mid in pi_local_models(prov)
    ]
    show_gemini = _gemini_key_available() and os.getenv("PI_GEMINI_BUTTONS", "1").strip() != "0"
    if show_gemini or not buttons:
        buttons += PI_MODEL_BUTTONS
    return buttons


# ── 收费本地 provider 的余额（/model 面板上显示，11-22 加）────────────────────────
# 按接口地址认是哪家：
#   Venice：GET <base>/api_keys/rate_limits（推理 key 就能查）→ data.balances.USD
#   Lebrel：GET https://lebrel.ai/api/overview → balanceMicrousd，要带 account:read 权限的 key
#           （推理 key 只有 inference:write，回 403）；PI_LOCAL_<名字>_BALANCE_KEY_ENV 指向那把只读 key。
#           另外 /api/drop/policy 是公开的：现在是不是免费时段、打几折。
# 查不到时，配了 PI_LOCAL_<名字>_BALANCE_URL 就给个后台链接。
_LEBREL_SITE = "https://lebrel.ai"


def _http_json(url: str, key: str = "", timeout: float = 5.0) -> Optional[dict]:
    import urllib.request
    headers = {"Accept": "application/json", "User-Agent": "cc-lark"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — 网络 / 401 / 403 / 非 JSON 一律当查不到
        return None


def _money(usd: float) -> str:
    return f"${usd:,.2f}"


def _lebrel_phase(now_ms: Optional[float] = None) -> str:
    """Lebrel 当前价位（公开接口）：免费时段 / 原价几折；查不到返回空串。"""
    import time as _time
    data = _http_json(f"{_LEBREL_SITE}/api/drop/policy")
    if not data:
        return ""
    phase = data.get("phase") or {}
    if phase.get("free"):
        until = phase.get("freeUntil")
        if isinstance(until, (int, float)):
            # 北京时间（固定 UTC+8）
            hhmm = _time.strftime("%H:%M", _time.gmtime(until / 1000 + 8 * 3600))
            return f"现在免费（到 {hhmm}）"
        return "现在免费"
    pct = (data.get("price") or {}).get("percent")
    if isinstance(pct, (int, float)):
        return "现在原价" if pct >= 100 else f"现在原价 {pct / 10:g} 折"
    return ""


def local_balance_line(provider: str) -> str:
    """某家本地 provider 的余额一行（不收费 / 认不出是哪家的返回空串）。"""
    base = (pi_local_env(provider, "BASE_URL") or "").rstrip("/")
    key_env = pi_local_env(provider, "BALANCE_KEY_ENV") or pi_local_env(provider, "API_KEY_ENV")
    key = os.getenv(key_env, "") if key_env else ""
    link = pi_local_env(provider, "BALANCE_URL")
    name = provider.capitalize()
    host = base.split("://", 1)[-1].split("/", 1)[0].lower()
    balance = ""
    extra = ""
    if host.endswith("venice.ai"):
        data = _http_json(f"{base}/api_keys/rate_limits", key) if key else None
        usd = (((data or {}).get("data") or {}).get("balances") or {}).get("USD")
        if isinstance(usd, (int, float)):
            balance = _money(float(usd))
    elif host.endswith("lebrel.ai"):
        data = _http_json(f"{_LEBREL_SITE}/api/overview", key) if key else None
        micro = (data or {}).get("balanceMicrousd")
        if isinstance(micro, (int, float)):
            balance = _money(micro / 1_000_000)
        extra = _lebrel_phase()
    else:
        return ""
    if not balance:
        balance = f"[后台查看]({link})" if link else "查不到"
    return f"💰 {name} 余额 {balance}" + (f" · {extra}" if extra else "")


def local_balance_lines() -> list[str]:
    """全部本地 provider 的余额（并发查，每家最多等 5 秒）。"""
    from concurrent.futures import ThreadPoolExecutor
    providers = pi_local_providers()
    if not providers:
        return []
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        lines = list(pool.map(local_balance_line, providers))
    return [line for line in lines if line]


def default_effort(profile_name: Optional[str] = None, model: Optional[str] = None) -> Optional[str]:
    """profile 级默认强度：<PROFILE>_PI_EFFORT 优先，再退到 PI_EFFORT；非法值当没配。"""
    prof = (profile_name or "").strip().upper()
    raw = (os.getenv(f"{prof}_PI_EFFORT") if prof else None) or os.getenv("PI_EFFORT") or ""
    try:
        return normalize_thinking(raw, model)
    except ValueError:
        return None


def default_model(profile_name: Optional[str] = None) -> str:
    """没 /model 覆盖时用的模型：<PROFILE>_PI_MODEL > PI_MODEL > google/gemini-3.8-flash。

    PI_MODEL 可以是本地模型（ollama/<id> 或裸 id，得在 PI_LOCAL_MODELS 里）。"""
    prof = (profile_name or "").strip().upper()
    raw = (os.getenv(f"{prof}_PI_MODEL") if prof else None) or os.getenv("PI_MODEL") or ""
    bare = _bare_model(raw)
    if bare in PI_GEMINI_MODELS:
        return f"google/{bare}"
    ref = pi_local_ref(raw)
    return f"{ref[0]}/{ref[1]}" if ref else DEFAULT_MODEL


def resolve_model(model: Optional[str], profile_name: Optional[str] = None) -> str:
    """会话里记的模型 → 传给 pi 的 provider/id。

    别名（pi-flash …）在这里解析；不是 pi 的模型（话题没 /model 覆盖时会话里记的是
    profile 默认，比如 Claude bot 的 opus[1m]）一律换成 pi 的默认模型。
    """
    fallback = default_model(profile_name)
    raw = (model or "").strip()
    if not raw:
        return fallback
    from bot_config import normalize_model_for_runner
    return normalize_model_for_runner(raw, "pi", fallback=fallback) or fallback


def session_dir() -> str:
    return os.path.expanduser(os.getenv("PI_SESSION_DIR") or DEFAULT_SESSION_DIR)


def find_session_file(session_id: Optional[str], sessions: Optional[str] = None) -> Optional[str]:
    """按会话 id 找 pi 的会话文件（`<时间戳>_<id>.jsonl`）；找不到返回 None。"""
    sid = (session_id or "").strip()
    if not sid or not re.fullmatch(r"[0-9A-Za-z-]{8,64}", sid):
        return None
    hits = sorted(glob.glob(os.path.join(sessions or session_dir(), f"*_{sid}.jsonl")))
    return hits[-1] if hits else None


def session_cost(session_id: Optional[str], sessions: Optional[str] = None) -> Optional[float]:
    """会话累计花费（美元）：会话文件里每条 assistant 消息的 usage.cost.total 求和。"""
    path = find_session_file(session_id, sessions)
    if not path:
        return None
    total = 0.0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line:
                    continue
                try:
                    msg = (json.loads(line).get("message") or {})
                except json.JSONDecodeError:
                    continue
                if msg.get("role") == "assistant":
                    cost = ((msg.get("usage") or {}).get("cost") or {}).get("total")
                    if isinstance(cost, (int, float)):
                        total += float(cost)
    except OSError:
        return None
    return total


def context_window_for(model: Optional[str] = None) -> int:
    """这几款 Gemini 都是 1M 窗口；本地模型按 PI_LOCAL_CONTEXT_WINDOW；PI_CONTEXT_WINDOW 全局覆盖。"""
    override = (os.getenv("PI_CONTEXT_WINDOW") or "").strip()
    if override.isdigit() and int(override) > 0:
        return int(override)
    ref = pi_local_ref(model)
    if ref:
        return _local_int(ref[0], "CONTEXT_WINDOW", LOCAL_CONTEXT_WINDOW)
    return _CONTEXT_WINDOW


def _with_claude_context(
    append_system_prompt: Optional[str], cwd: Optional[str], claude_home: Optional[str] = None,
) -> str:
    base = append_system_prompt or ""
    if os.getenv("CC_LARK_PI_CLAUDE_CONTEXT", "1") == "0":
        return base
    try:
        brief = build_claude_context_brief(
            cwd,
            skills_note=(
                "~/.claude/skills 里的 skill 已经用 --skill 带进来了（~/.agents/skills 里的 lark-* 那批"
                "pi 本来就会扫），都在你的 skill 列表里，按名字正常用。"
            ),
            include_project_rules=False,
            claude_home=claude_home,
        )
    except Exception as exc:  # noqa: BLE001 — 接不上 Claude 的上下文不该拖垮这一轮
        print(f"[run_pi] Claude 上下文跳过: {type(exc).__name__}: {exc}", flush=True)
        return base
    if not brief:
        return base
    return f"{base}\n\n{brief}" if base else brief


def _skill_args(cwd: Optional[str]) -> list[str]:
    if os.getenv("CC_LARK_PI_CLAUDE_CONTEXT", "1") == "0":
        return []
    args: list[str] = []
    dirs = [CLAUDE_SKILLS_DIR]
    if cwd:
        dirs.append(os.path.join(os.path.expanduser(cwd), ".claude", "skills"))
    for d in dirs:
        if os.path.isdir(d):
            args += ["--skill", d]
    return args


def _message_text(message: dict) -> str:
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def _show_thinking() -> bool:
    return os.getenv("CC_LARK_PI_SHOW_THINKING", "1") != "0"


def _thinking_line(content) -> str:
    """一段思考 → 卡片上的一行「💭 …」：压掉换行、截到 THINKING_PREVIEW_CHARS，
    截断后落单的 ` / ** 补齐，免得把后面整张卡片渲染成代码或粗体。"""
    if isinstance(content, dict):
        content = content.get("thinking") or content.get("text") or ""
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if not text:
        return ""
    if len(text) > THINKING_PREVIEW_CHARS:
        text = text[:THINKING_PREVIEW_CHARS].rstrip() + "…"
    if text.count("**") % 2:
        text += "**"
    if text.replace("**", "").count("*") % 2:
        text = text.replace("*", "")
    if text.count("`") % 2:
        text += "`"
    return f"💭 {text}"


def _tool_name(name: str) -> str:
    return _TOOL_DISPLAY.get(name, name)


def _tool_done_input(name: str, args: dict, event: dict) -> dict:
    """工具跑完再报一次同名同参的事件，卡片把那一行改成 ✅ / ❌。"""
    inp = dict(args or {})
    failed = bool(event.get("isError"))
    if name == "bash":
        structured = (event.get("result") or {}).get("structuredContent") or {}
        inp["status"] = "error" if failed and "exit_code" not in structured else "completed"
        if "exit_code" in structured:
            inp["exit_code"] = structured.get("exit_code")
    else:
        # 卡片的通用完成标记（_ 开头的键不进工具主参数，不会被当成另一个工具）
        inp["_agy_state"] = "ERROR" if failed else "DONE"
    return inp


def _context_usage(usage: dict) -> dict:
    """pi 的 usage → footer 认的 Anthropic 风格字段（input 不含缓存命中部分，和 Claude 一致）。"""
    return {
        "input_tokens": int(usage.get("input") or 0),
        "cache_read_input_tokens": int(usage.get("cacheRead") or 0),
        "cache_creation_input_tokens": int(usage.get("cacheWrite") or 0),
        "output_tokens": int(usage.get("output") or 0),
    }


class _ResumeFailed(Exception):
    pass


async def run_pi(
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
    pi_bin: Optional[str] = None,
    agent_dir: Optional[str] = None,
    dangerously_skip_permissions: bool = True,
    idle_timeout_sec: int = IDLE_TIMEOUT,
    sessions_dir: Optional[str] = None,
    claude_home: Optional[str] = None,
) -> tuple[str, Optional[str], bool]:
    """返回 (full_text, session_id, used_fresh_session_fallback)。

    claude_home：从哪套 Claude Code 配置取规则 / 记忆（research 是 ~/.claude-research）。"""
    del on_status  # 那是 PTY 限流告警通道，pi 的重试只记日志
    profile_name = (extra_env or {}).get("CC_LARK_PROFILE")
    resolved_model = resolve_model(model, profile_name)
    try:
        thinking = normalize_thinking(effort, resolved_model)
    except ValueError:
        # 派活 / 定时任务可能带别家后端才有的档位（codex 的 ultra）：别为这个整轮失败
        print(f"[run_pi] 忽略 pi 不认识的推理强度 {effort!r}", flush=True)
        thinking = None
    thinking = thinking or default_effort(profile_name, resolved_model)
    trust_all = bool(dangerously_skip_permissions) or (permission_mode or "").lower() in (
        "bypasspermissions", "bypass_permissions",
    )
    idle_limit = idle_timeout_sec if idle_timeout_sec > 0 else IDLE_TIMEOUT
    sessions = os.path.expanduser(sessions_dir) if sessions_dir else session_dir()
    os.makedirs(sessions, exist_ok=True)

    env = os.environ.copy()
    env["CC_LARK_MIRROR_OFF"] = "1"
    env.setdefault("PI_SKIP_VERSION_CHECK", "1")
    env.setdefault("PI_TELEMETRY", "0")
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    if agent_dir:
        env["PI_CODING_AGENT_DIR"] = os.path.expanduser(agent_dir)
    if not env.get("GEMINI_API_KEY"):
        src = os.getenv("PI_GEMINI_KEY_FROM", "AGY_API_KEY")
        if src and env.get(src):
            env["GEMINI_API_KEY"] = env[src]
    # 扩展把 Google 的模型表换成 PI_GEMINI_MODELS（只接这几款 Gemini、价格和档位对得上），
    # 本机配了本地模型再加一个 OpenAI 兼容的 provider
    env["CC_LARK_PI_PROVIDER"] = json.dumps(provider_configs(), ensure_ascii=False)
    mcp_cfg = cc_lark_mcp_config(extra_env, log_tag="run_pi")
    if mcp_cfg:
        env["CC_LARK_PI_MCP_CONFIG"] = json.dumps(mcp_cfg, ensure_ascii=False)
    else:
        env.pop("CC_LARK_PI_MCP_CONFIG", None)

    system_prompt = _with_claude_context(append_system_prompt, cwd, claude_home)
    tmp_dir = tempfile.mkdtemp(prefix="cc-lark-pi-")
    prompt_file = ""
    if system_prompt:
        prompt_file = os.path.join(tmp_dir, "append_system_prompt.md")
        fd = os.open(prompt_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system_prompt)

    async def _run_once(session_file: Optional[str]) -> tuple[str, Optional[str], Optional[int], str]:
        cmd = [resolve_pi_bin(pi_bin), "--mode", "json", "--session-dir", sessions]
        if session_file:
            cmd += ["--session", session_file]
        if resolved_model:
            cmd += ["--model", resolved_model]
        if thinking:
            cmd += ["--thinking", thinking]
        if not trust_all:
            cmd += ["--tools", READ_ONLY_TOOLS]
        if prompt_file:
            cmd += ["--append-system-prompt", prompt_file]
        if os.path.isfile(EXTENSION_PATH):
            cmd += ["-e", EXTENSION_PATH]
        cmd += _skill_args(cwd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.path.expanduser("~"),
            env=env,
            # agent_end 会把本轮全部消息再吐一遍，长任务一行能到几 MB
            limit=256 * 1024 * 1024,
            start_new_session=True,
        )
        await _fire_callback(on_process_start, proc)
        stderr_task = asyncio.ensure_future(proc.stderr.read())
        try:
            proc.stdin.write(message.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass

        full_text = ""
        last_text = ""
        new_session_id: Optional[str] = None
        text_started_in_message = False
        show_thinking = _show_thinking()
        # 上一次推给卡片的是「💭」行（已自带结尾空行）：紧跟的正文别再补空行
        thought_shown_last = False
        tool_args: dict[str, tuple[str, dict]] = {}
        last_started_tool = ""
        last_usage: dict = {}
        turn_tokens = 0
        turn_cost = 0.0
        error_msg = ""
        idle_seconds = 0
        loop = asyncio.get_event_loop()
        start_time = loop.time()
        wall_clock_limit = resolve_claude_wall_clock_limit(extra_env)

        try:
            while True:
                if wall_clock_limit > 0 and loop.time() - start_time >= wall_clock_limit:
                    proc.kill()
                    await proc.wait()
                    raise RuntimeError(
                        f"pi 单轮执行超过 wall-clock 最终上限（{int(wall_clock_limit)}秒），已终止进程。"
                    )
                try:
                    raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=_CHECK_INTERVAL)
                    idle_seconds = 0
                except asyncio.TimeoutError:
                    idle_seconds += _CHECK_INTERVAL
                    has_kids = _has_children(proc.pid)
                    threshold = STUCK_CHILD_TIMEOUT if has_kids else idle_limit
                    if idle_seconds >= threshold:
                        proc.kill()
                        await proc.wait()
                        if has_kids:
                            raise RuntimeError(
                                f"pi 执行超时（{threshold}秒有子进程但 pi 端无任何新输出），已终止进程。"
                                f"常见原因：tail -f / watch / npm run dev 等永不退出的阻塞命令。"
                            )
                        raise RuntimeError(f"pi 执行超时（{threshold}秒无输出且无活跃子进程），已终止进程")
                    continue
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = data.get("type")

                if etype == "session":
                    new_session_id = data.get("id") or new_session_id

                elif etype == "message_start":
                    if (data.get("message") or {}).get("role") == "assistant":
                        text_started_in_message = False

                elif etype == "message_update":
                    ev = data.get("assistantMessageEvent") or {}
                    if ev.get("type") == "text_delta":
                        chunk = ev.get("delta") or ""
                        if not chunk:
                            continue
                        # 工具调用前后是两条 assistant 消息，中间补个空行，别把两段话粘在一起
                        if not text_started_in_message and full_text and not full_text.endswith("\n"):
                            chunk = "\n\n" + chunk
                        text_started_in_message = True
                        full_text += chunk
                        if thought_shown_last:
                            chunk = chunk.lstrip("\n")
                            thought_shown_last = False
                        await _fire_callback(on_text_chunk, chunk)
                    elif ev.get("type") == "thinking_end" and show_thinking:
                        line = _thinking_line(ev.get("content"))
                        if line:
                            # 只上卡片（成为收尾「🔍 过程」的一部分），不进返回的正文
                            prefix = "\n\n" if (full_text and not thought_shown_last) else ""
                            thought_shown_last = True
                            await _fire_callback(on_text_chunk, prefix + line + "\n\n")

                elif etype == "message_end":
                    msg = data.get("message") or {}
                    if msg.get("role") != "assistant":
                        continue
                    text = _message_text(msg).strip()
                    if text:
                        last_text = text
                    usage = msg.get("usage") or {}
                    if usage:
                        last_usage = _context_usage(usage)
                        turn_tokens += int(usage.get("totalTokens") or 0)
                        cost = (usage.get("cost") or {}).get("total")
                        if isinstance(cost, (int, float)):
                            turn_cost += float(cost)
                    stop = str(msg.get("stopReason") or "")
                    if stop in ("error", "aborted"):
                        error_msg = str(msg.get("errorMessage") or stop)
                    elif stop:
                        error_msg = ""

                elif etype == "tool_execution_start":
                    name = str(data.get("toolName") or "tool")
                    args = data.get("args") if isinstance(data.get("args"), dict) else {}
                    last_started_tool = str(data.get("toolCallId") or "")
                    tool_args[last_started_tool] = (name, args)
                    await _fire_callback(on_tool_use, _tool_name(name), dict(args))

                elif etype == "tool_execution_end":
                    tid = str(data.get("toolCallId") or "")
                    name, args = tool_args.pop(tid, (str(data.get("toolName") or "tool"), data.get("args") or {}))
                    # 卡片只会把「同一工具的后续状态」并进最后一行。pi 会并行跑工具，别的工具
                    # 已经起了一行时再报完成就成了重复的一行，所以只给最后起的那个补 ✅。
                    if tid == last_started_tool:
                        await _fire_callback(on_tool_use, _tool_name(name), _tool_done_input(name, args, data))

                elif etype == "auto_retry_start":
                    print(
                        f"[run_pi] 上游出错自动重试 {data.get('attempt')}/{data.get('maxAttempts')}: "
                        f"{str(data.get('errorMessage') or '')[:200]}",
                        flush=True,
                    )

                elif etype == "compaction_end" and data.get("errorMessage"):
                    print(f"[run_pi] 压缩上下文失败: {str(data.get('errorMessage'))[:200]}", flush=True)
        except BaseException:
            stderr_task.cancel()
            try:
                if proc.returncode is None:
                    proc.kill()
            except ProcessLookupError:
                pass
            raise

        await proc.wait()
        stderr_text = (await stderr_task).decode("utf-8", errors="replace").strip()
        if stderr_text:
            print(f"[run_pi] stderr: {stderr_text[-500:]}", flush=True)
        if session_file and not new_session_id and "No session found" in stderr_text:
            raise _ResumeFailed(stderr_text[-200:])

        if last_usage:
            usage_out = dict(last_usage)
            usage_out["_context_window"] = context_window_for(resolved_model)
            if turn_tokens:
                usage_out["_turn_tokens"] = turn_tokens
            if turn_cost:
                usage_out["_turn_cost_usd"] = round(turn_cost, 6)
            await _fire_callback(on_usage, usage_out)

        if error_msg:
            exc = RuntimeError(f"pi 执行出错：{error_msg[:800]}")
            if new_session_id:
                exc.cc_session_id = new_session_id
                exc.cc_retryable_resume = not is_fatal_error_text(error_msg)
            raise exc
        return (last_text or full_text).strip(), new_session_id, proc.returncode, stderr_text

    used_fresh_session_fallback = False
    try:
        session_file = find_session_file(session_id, sessions) if session_id else None
        if session_id and not session_file:
            print(f"[run_pi] 会话文件不在了，开新会话; sid={session_id}", flush=True)
            used_fresh_session_fallback = True
        try:
            final_text, new_session_id, returncode, stderr_text = await _run_once(session_file)
        except _ResumeFailed as exc:
            print(f"[run_pi] resume failed, retrying with fresh session; sid={session_id} err={exc}", flush=True)
            final_text, new_session_id, returncode, stderr_text = await _run_once(None)
            used_fresh_session_fallback = True
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if returncode not in (0, None):
        if final_text:
            return final_text, new_session_id, used_fresh_session_fallback
        exc = RuntimeError(f"pi exited with code {returncode}: {stderr_text[-500:] or 'no stderr'}")
        if returncode > 0 and new_session_id and not is_fatal_error_text(stderr_text):
            exc.cc_session_id = new_session_id
            exc.cc_retryable_resume = True
        raise exc
    return final_text, new_session_id, used_fresh_session_fallback


def format_cost(usd: Optional[float]) -> str:
    if not isinstance(usd, (int, float)):
        return ""
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"
