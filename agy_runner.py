"""本地调用 Google Antigravity CLI（`agy`）的统一入口。

    agy -p <prompt> --output-format stream-json
        [--conversation <id>] [--model <m> --effort <low|medium|high>]
        [--dangerously-skip-permissions | --mode accept-edits|plan]
        --add-dir <cwd> --print-timeout <go-duration>

背景：Gemini CLI 在 2026-06-18 对免费/AI Pro/Ultra 停服，Google 把终端 agent
换成了闭源 Go 实现的 Antigravity CLI（`agy`）。它是 Gemini 3.x 官方协同优化的
harness，所以这里接的是 agy 而不是老的 `gemini`。

线格式：`--output-format stream-json` 是 agy 自己的 NDJSON（**不是** Anthropic
messages 流），三类事件：
    {"event":"init",        "conversation_id":..., "init":{model,cwd,tools,...}}
    {"event":"step_update", "step_update":{step_index,step_type,state,...}}
    {"event":"result",      "result":{conversation_id,status,response,usage}}
其中 step_type=agent_response 的 `text_delta` 是**增量**正文（ACTIVE 一段、DONE
再补一段，直接拼接即可）；step_type=tool 的 tool_info.{name,parameters} 是工具调用。

会话：conversation_id 稳定，续轮 `--conversation <id>`。传了不存在的 id 时 agy
只在 stderr 打一句 `warning: conversation ... not found` 然后**开新会话**（不报错），
所以这里不需要 grok 那套"resume 哑失败再来一次"的兜底——新 id 会从 init 事件回来。

模型：`agy models` 给的是带档位后缀的 id（gemini-3.8-flash-high/medium/low）；
裸名 `gemini-3.8-flash` 也认，但**必须同时给 `--effort`**，否则 agy 直接报
"invalid model selection"。_resolve_model_effort 负责把两种写法归一。

鉴权：两条互斥通道，由 AGY_MODEL_PROVIDER 单一开关切换，切换时 HOME 与
API key 必须一起跟着走，不能只切一半：
  • provider 非空（如 "gemini"）= API key 通道，settings.json 写
    `modelProvider: "gemini"` + 注入 GEMINI_API_KEY 直连 Gemini API；
  • provider 为空 = OAuth（消费级订阅额度），此时不该注入 GEMINI_API_KEY：
    两个开关本就该一起走，注入了也用不上，还让「现在到底走哪条通道」变得
    难以判断（排查时会以为是 API key 模式）。
⚠️ 免费档 Gemini API key 不给香港/大陆出口服务，走 API key 通道且出口在港区/
大陆时会报 400 User location is not supported。但反过来**不成立**：见下面
run_agy 尾部的长注释，OAuth 通道也会随机报同一句 400，那跟 key、跟地区都无关。
机器出口本身在受支持地区（当前全局节点=日本）时 `agy_proxy` 留空直连即可；
出口回到港区/大陆时再给它单独指一个受支持地区的代理，会以
HTTPS_PROXY/HTTP_PROXY/ALL_PROXY 注入，同时用 NO_PROXY 把 127.0.0.1 排除掉
——不然 cc_mcp_server 回连 bot 的 control API 会被代理吃掉。

Skills：agy 的 skill 就是 Anthropic 那套 SKILL.md（frontmatter name+description +
scripts/references 子目录），全局根目录是 `~/.gemini/config/skills/`。本机把它软链到
了 `~/.claude/skills`，所以 Claude Code 的 skill 对 agy 后端**直接可用**（代价：
skill 索引让每轮基线 input 从 ~9k 涨到 ~20k token）。

MCP：agy 的 MCP 子进程**继承父进程 env**（已实测：run_command 能看到父进程
的 CC_LARK_* 变量，call_mcp_tool 调 cc-lark 的 list_crons 能真拿到 bot 的
定时任务），所以 cc_mcp_server 只要注册一次，per-turn 的 CC_LARK_* 放进本
进程 env 即可，不用像 codex 那样每轮改写配置。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from typing import Callable, Optional
import uuid

from bot_config import PERMISSION_MODE, resolve_claude_wall_clock_limit
from claude_runner import _fire_callback, _has_children, is_fatal_error_text

IDLE_TIMEOUT = 300  # 无输出且无子进程 → 视为挂死
STUCK_CHILD_TIMEOUT = 900  # 有子进程但 agy 端持续无输出（tail -f / npm run dev 类）
_CHECK_INTERVAL = 30

# agy 的 --effort 只有三档（比 Claude 少 none/minimal/xhigh/max）
AGY_EFFORT_LEVELS = ("low", "medium", "high")
_EFFORT_FALLBACK = {
    "none": "low", "minimal": "low",
    "xhigh": "high", "max": "high",
}
DEFAULT_EFFORT = "high"

# agy 里**不接受 `--effort`** 的模型（`agy models` 显示它们没有 -high/-medium/-low
# 档位变体）。给它们传 --effort 会直接报
#   `invalid model selection (... --effort "high"): --effort is not supported for model ...`
# 而且 **exit code 仍是 0**，所以不特判就会变成一次静默失败。
# 与 bot_config.AGY_THIRD_PARTY_MODELS 是同一批模型（那边管"能不能选"，
# 这边管"选了之后要不要带档位"）。
_MODELS_WITHOUT_EFFORT = frozenset({
    "claude-opus-4-6-thinking",
    "claude-sonnet-4-6",
    "gpt-oss-120b-medium",
})

def model_ignores_effort(model: Optional[str]) -> bool:
    """该模型是否不接受 `--effort`（传了会被 agy 判成 invalid model selection）。

    接受别名（agy-opus）与 google/ 前缀，统一解析后再判定，好让 commands 的
    /effort 与 runner 的实参拼装用同一套口径。
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    if m.startswith("google/"):
        m = m[len("google/"):]
    try:
        from commands import MODEL_ALIASES
        m = MODEL_ALIASES.get(m, m).lower()
    except Exception:
        pass
    if m.startswith("google/"):
        m = m[len("google/"):]
    return m in _MODELS_WITHOUT_EFFORT


# agy 自己的配置目录（和桌面版 Antigravity 共用 ~/.gemini）
# ── bot 专用 HOME ──────────────────────────────────────────────────────
# agy 判断走 OAuth 还是 API key，只看全局 settings.json 里的 modelProvider
# （官方 changelog 明说没有 flag / env 可以覆盖）。而 ensure_model_provider()
# 每轮都要把 modelProvider 写回去，于是 bot 一跑，用户在终端手动登录的 OAuth
# 会话就被打回 API key 模式。agy 完整遵守 $HOME（实测：设了 HOME 之后它会在
# 新目录下重建 .gemini/antigravity-cli 整套结构，真 HOME 一个字节不动），所以
# 给 bot 单开一个 HOME 就能彻底隔离：
#   用户手动 `agy`  → ~/.gemini            → OAuth（订阅额度）
#   bot 跑 agy      → AGY_BOT_HOME/.gemini → API key
# 设 AGY_BOT_HOME= （空）可回退到与用户共用 ~/.gemini 的旧行为。
# ⚠️ 致命组合：AGY_BOT_HOME 启用 + AGY_MODEL_PROVIDER 为空。OAuth 凭证是绑
# HOME 的（实测：换了 HOME 就是 "You are not logged in"，keychain 不共享），
# 于是 bot 每个任务都会卡在「等浏览器授权」60s 再失败。想让 bot 也走 OAuth
# （用订阅额度而不是 API key 计费），必须把 AGY_BOT_HOME 留空共用 ~/.gemini。
AGY_BOT_HOME = os.path.expanduser(
    os.getenv("AGY_BOT_HOME", "~/.feishu-claude/agy-home").strip()
) if os.getenv("AGY_BOT_HOME", "~/.feishu-claude/agy-home").strip() else ""
_AGY_GEMINI_ROOT = (
    os.path.join(AGY_BOT_HOME, ".gemini") if AGY_BOT_HOME
    else os.path.expanduser("~/.gemini")
)
AGY_HOME = os.path.join(_AGY_GEMINI_ROOT, "antigravity-cli")
AGY_SETTINGS_PATH = os.path.join(AGY_HOME, "settings.json")
AGY_MCP_CONFIG_PATH = os.path.join(_AGY_GEMINI_ROOT, "config", "mcp_config.json")
# skill 索引：用户那边是 ~/.gemini/config/skills -> ~/.claude/skills 的软链，
# bot 的 HOME 也照着链一份，不然 agy 后端会丢掉全部 skill。
AGY_SKILLS_LINK = os.path.join(_AGY_GEMINI_ROOT, "config", "skills")
AGY_SKILLS_SRC = os.path.expanduser("~/.claude/skills")

# print 模式的默认上限是 5 分钟，对 bot 场景太短——真正的看门狗是下面的
# idle/stuck 检测，这里给个足够大的值别让 agy 自己先掐断。
DEFAULT_PRINT_TIMEOUT = "24h"

# Gemini 3.x 全系 1M 上下文；dispatcher 的 _context_window_for 不认 gemini 名字，
# 所以由 runner 自己把窗口塞进 usage。
_GEMINI_CONTEXT_WINDOW = 1_000_000

# agy 内置第三方模型的上下文窗口。agy 的 stream-json 只给 token 计数、**不给窗口
# 大小**（init 事件只有 cwd/model/permission_mode/tools），所以只能在这里维护。
# ⚠️ 这几个数字取自各家公开规格，agy 侧没有接口可核实；若日后发现 footer 的
# 「上下文 x/y」与实际撞限不符，以实测为准来改这里。
_MODEL_CONTEXT_WINDOWS = {
    "claude-opus-4-6-thinking": 200_000,
    "claude-sonnet-4-6": 200_000,
    "gpt-oss-120b-medium": 128_000,
}


def _context_window_for(model: Optional[str]) -> int:
    return _MODEL_CONTEXT_WINDOWS.get(
        (model or "").strip().lower(), _GEMINI_CONTEXT_WINDOW
    )

# argv 上限（macOS ARG_MAX 1MB）。prompt 只能走 argv（agy 的 print 模式不读
# stdin），超长时从中间截断，保住头尾。
_MAX_PROMPT_BYTES = 700_000

_mcp_registered = False


def resolve_agy_bin(configured: Optional[str] = None) -> str:
    if configured:
        return os.path.expanduser(configured)
    found = shutil.which("agy")
    if found:
        return found
    home_bin = os.path.expanduser("~/.local/bin/agy")
    if os.path.exists(home_bin):
        return home_bin
    return "agy"


def ensure_bot_home() -> bool:
    """幂等地把 bot 专用 HOME 备好（目录 + skills 软链）。返回是否发生写入。

    未启用隔离（AGY_BOT_HOME 为空）时直接返回 False，不碰用户的 ~/.gemini。
    """
    if not AGY_BOT_HOME:
        return False
    touched = False
    for d in (AGY_HOME, os.path.dirname(AGY_MCP_CONFIG_PATH)):
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            touched = True
    # skills：只在源存在、且目标还不是软链时补一次
    if os.path.isdir(AGY_SKILLS_SRC) and not os.path.islink(AGY_SKILLS_LINK):
        if not os.path.exists(AGY_SKILLS_LINK):
            try:
                os.symlink(AGY_SKILLS_SRC, AGY_SKILLS_LINK)
                touched = True
            except OSError:
                pass
    return touched


def ensure_model_provider(provider: str = "gemini", path: str = AGY_SETTINGS_PATH) -> bool:
    """把 modelProvider 写进 agy 的 settings.json。返回是否发生写入。

    provider="gemini" = 用 GEMINI_API_KEY 直连 Gemini API（不走 OAuth）。
    空 provider 表示"不管"，交回用户/OAuth 默认。
    """
    provider = (provider or "").strip()
    if not provider:
        return False
    path = os.path.expanduser(path)
    data: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except (OSError, json.JSONDecodeError):
            data = {}
    if data.get("modelProvider") == provider:
        return False
    data["modelProvider"] = provider
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return True


def ensure_cc_lark_mcp(path: str = AGY_MCP_CONFIG_PATH) -> bool:
    """幂等地把 cc_mcp_server 注册进 agy 的 mcp_config.json。返回是否发生写入。

    不写 env：agy 的 MCP 子进程继承父进程环境，per-turn 的 CC_LARK_* 由
    run_agy 放进 agy 进程 env 即可（写死在配置里会在并发话题间串台）。
    已存在同名 server 就不动（尊重用户手改）。
    """
    cc_server = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cc_mcp_server.py")
    if not os.path.isfile(cc_server):
        return False
    path = os.path.expanduser(path)
    data: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except (OSError, json.JSONDecodeError):
            data = {}
    servers = data.setdefault("mcpServers", {})
    if "cc-lark" in servers:
        return False
    servers["cc-lark"] = {
        "command": sys.executable,
        "args": [cc_server],
        "disabled": False,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return True


def _normalize_effort(effort: Optional[str]) -> Optional[str]:
    """把 cc-lark 的七档 effort 收敛到 agy 的三档；空值返回 None。"""
    e = (effort or "").strip().lower()
    if not e:
        return None
    e = _EFFORT_FALLBACK.get(e, e)
    if e not in AGY_EFFORT_LEVELS:
        raise ValueError(
            f"invalid agy effort {effort!r}; expected one of {list(AGY_EFFORT_LEVELS)}"
        )
    return e


def _resolve_model_effort(
    model: Optional[str], effort: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """归一 (model, effort)。

    - `gemini-3.8-flash-high` 这种带档位后缀的 id：原样传，不再给 --effort
      （agy 会报 "model already encodes effort" 之外的组合冲突）。
    - `gemini-3.8-flash` 这种裸名：**必须**配 --effort，没给就用默认（high）。
    - 非 agy 模型（如 opus[1m] / sonnet / fable / gpt 等误传）：自动安全回退为
      AGY_DEFAULT_MODEL 或 gemini-3.8-flash，绝不往 agy CLI 传非法模型。
    """
    m = (model or "").strip()
    e = _normalize_effort(effort)
    if not m:
        return None, e

    # 别名与格式归一：如 google/gemini-3.8-flash → gemini-3.8-flash
    if m.lower().startswith("google/"):
        m = m[len("google/"):]
    try:
        from commands import MODEL_ALIASES
        m = MODEL_ALIASES.get(m.lower(), m)
    except Exception:
        pass
    if m.lower().startswith("google/"):
        m = m[len("google/"):]

    # agy 官方仅支持 Gemini 系列模型；若被外部注入了 Claude / OpenAI 等异构模型则自动拦截
    from bot_config import is_model_compatible_with_runner
    if not is_model_compatible_with_runner(m, "agy"):
        fallback = (os.getenv("AGY_DEFAULT_MODEL") or "gemini-3.8-flash").strip()
        print(
            f"[run_agy] 警告：模型 {m!r} 不是有效的 agy 模型"
            f"（支持 Gemini 系列 + {sorted(_MODELS_WITHOUT_EFFORT)}），"
            f"已自动回退为 {fallback}",
            flush=True,
        )
        m = fallback

    # Claude / GPT-OSS 这些第三方模型自带固定推理深度，传 --effort 直接报错
    if model_ignores_effort(m):
        return m, None

    for level in AGY_EFFORT_LEVELS:
        if m.endswith(f"-{level}"):
            return m, None
    default = (os.getenv("AGY_EFFORT") or DEFAULT_EFFORT).strip().lower()
    if default not in AGY_EFFORT_LEVELS:
        default = DEFAULT_EFFORT
    return m, e or default


def _build_permission_args(
    permission_mode: Optional[str], dangerous_skip: bool
) -> list[str]:
    """cc-lark 的权限模式 → agy 的 --mode / --dangerously-skip-permissions。"""
    mode = (permission_mode or PERMISSION_MODE or "").strip()
    if mode == "plan":
        return ["--mode", "plan"]
    if mode in {"acceptEdits", "accept-edits"}:
        return ["--mode", "accept-edits"]
    if mode == "bypassPermissions" or dangerous_skip:
        return ["--dangerously-skip-permissions"]
    # agy 的 print 模式没有"逐个问"的通道——没人值守时只能自动放行，
    # 否则 ask_permission 会一直挂到 print-timeout。
    return ["--dangerously-skip-permissions"]


def _truncate_prompt(prompt: str) -> str:
    raw = prompt.encode("utf-8")
    if len(raw) <= _MAX_PROMPT_BYTES:
        return prompt
    half = _MAX_PROMPT_BYTES // 2
    head = raw[:half].decode("utf-8", errors="ignore")
    tail = raw[-half:].decode("utf-8", errors="ignore")
    dropped = len(raw) - _MAX_PROMPT_BYTES
    return f"{head}\n\n…（中间省略 {dropped} 字节，超出 agy argv 上限）…\n\n{tail}"


def _find_recent_agy_log(after_timestamp: float) -> Optional[str]:
    """兜底寻找在 after_timestamp 之后最近更新的 agy 运行日志。"""
    log_dir = os.path.join(AGY_HOME, "log")
    if not os.path.isdir(log_dir):
        return None
    try:
        candidates = []
        for name in os.listdir(log_dir):
            if name.endswith(".log"):
                path = os.path.join(log_dir, name)
                try:
                    mtime = os.path.getmtime(path)
                    if mtime >= after_timestamp - 2.0:
                        candidates.append((mtime, path))
                except OSError:
                    continue
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]
    except Exception:
        return None


# agy 启动早期（keyring 尚未加载完）必然会刷的假错误。它们出现在日志最前面，
# 但随后 OAuth 就认证成功了，所以绝不能当成本轮失败的原因。
_AGY_STARTUP_NOISE = (
    "not logged into antigravity",
    "error getting token source",
    "failed to get load code assist response",
    "admin controls not applicable",
)


def _is_agy_startup_noise(line: str) -> bool:
    low = (line or "").lower()
    return any(n in low for n in _AGY_STARTUP_NOISE)


def _extract_agy_log_error(
    log_file: Optional[str] = None,
    after_ts: float = 0.0,
    stderr_text: str = "",
) -> str:
    """从 agy 日志或 stderr 中提取底层的真实错误（HTTP 503/429/529/400、Status、Message 等）。"""
    target_file = log_file if (log_file and os.path.isfile(log_file)) else None
    if not target_file and after_ts > 0:
        target_file = _find_recent_agy_log(after_ts)

    log_content = ""
    if target_file:
        try:
            with open(target_file, "r", encoding="utf-8", errors="replace") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 200 * 1024), os.SEEK_SET)
                log_content = f.read()
        except Exception:
            pass

    combined_text = f"{log_content}\n{stderr_text}" if log_content else stderr_text
    if not combined_text.strip():
        return ""

    # 1. 优先提取 calling model 的结构化错误：Error 503/429/529/400
    m1 = re.search(
        r"calling model:\s*Error\s*(\d+),\s*Message:\s*(.*?),\s*Status:\s*([A-Z_]+)",
        combined_text,
    )
    if m1:
        code, msg, status = m1.groups()
        clean_msg = " ".join(msg.split()).strip()
        return f"Error {code} ({status}): {clean_msg}"

    # 2. 匹配 errorreport.go:xxx] agent executor error: ...
    #    ⚠️ 这里必须优先取 `agent executor error:`（执行期的真错），不能直接
    #    re.search 第一条 errorreport 行：agy 启动时 keyring 还没加载完，会先刷
    #    好几条 `error getting token source: You are not logged into Antigravity.`
    #    然后才认证成功（日志随后就有 "OAuth: authenticated successfully"）。
    #    取第一条会稳定抓到这句**假错误**，把上游的 400 User location 误报成
    #    「没登录」，既误导用户去重新登录，也让上层的错误分类全部失准。
    #    （2026-09-10 线上故障就是这么把排查带偏的。）
    executor_errs = re.findall(
        r"errorreport\.go:\d+\]\s*agent executor error:\s*(.*)", combined_text
    )
    if not executor_errs:
        executor_errs = [
            line
            for line in re.findall(r"errorreport\.go:\d+\]\s*(.*)", combined_text)
            if not _is_agy_startup_noise(line)
        ]
    if executor_errs:
        line = executor_errs[0].strip()
        line = re.sub(r",\s*Details:.*", "", line).strip()
        if line:
            return " ".join(line.split())

    # 3. 匹配常见 HTTP 状态码错误
    m3 = re.search(
        r"(Error\s*(?:529|503|500|502|504|429|400|401|403|404|408)\b[^\n]*)",
        combined_text,
    )
    if m3:
        line = m3.group(1).strip()
        line = re.sub(r",\s*Details:.*", "", line).strip()
        return " ".join(line.split())

    # 4. 匹配网络层连接重置/超时/流中断
    for pattern in (
        r"Received a RST_STREAM[^\n]*",
        r"transport: error while reading[^\n]*",
        r"connection reset by peer",
        r"context deadline exceeded",
        r"broken pipe",
        r"(?:error sending request:[^\n]*unexpected EOF[^\n]*|unexpected EOF\b[^\n]*)",
    ):
        m_net = re.search(pattern, combined_text, re.IGNORECASE)
        if m_net:
            return m_net.group(0).strip()

    # 5. 从 stderr_text 中筛选有效行兜底
    if stderr_text and stderr_text.strip():
        lines = [
            l.strip()
            for l in stderr_text.strip().splitlines()
            if l.strip() and not l.lower().startswith("warning:")
        ]
        if lines:
            return lines[-1]

    return ""


def _usage_from_result(
    data: dict, last_step: Optional[dict] = None, model: Optional[str] = None
) -> dict:
    """agy 的 usage → footer 认的字段名（cache_read_tokens → cache_read_input_tokens）。

    ⚠️ **`result.usage` 是本轮所有内部模型请求的累加**，不是上下文占用。实测一轮
    7 次工具调用：8 个 agent_response 步各自 input=20310/4238/4471/…，result 的
    input_tokens=54235 正好等于这 8 个之和，cache_read_tokens=113975 也是之和。
    直接把它当上下文喂给卡片 footer，会显示成「上下文 528.7k / 1M」这种离谱数字
    （用户 2026-09-03 报的就是这个）。

    所以上下文取**最后一步**的 usage（step_update.usage，单步 input 不含
    cache_read，`total_tokens = input + output`，故上下文 ≈ input + cache_read
    + output，与 Anthropic 语义一致）；累加值另外挂 `_turn_tokens`，footer 拿它
    单独显示"本轮消耗"，不参与上下文百分比。
    """
    raw = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    ctx = last_step if isinstance(last_step, dict) and last_step else raw
    usage = {
        "input_tokens": ctx.get("input_tokens", 0),
        "output_tokens": ctx.get("output_tokens", 0),
        "cache_read_input_tokens": ctx.get("cache_read_tokens", 0),
        "_context_window": _context_window_for(model),
    }
    thinking = ctx.get("thinking_tokens") or 0
    if thinking:
        usage["thinking_tokens"] = thinking
    turn_total = sum(
        int(raw.get(k) or 0) for k in ("input_tokens", "cache_read_tokens", "output_tokens")
    )
    if turn_total:
        usage["_turn_tokens"] = turn_total
    return {k: v for k, v in usage.items() if isinstance(v, (int, float))}


async def _run_agy_once(
    message: str,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    cwd: Optional[str] = None,
    permission_mode: Optional[str] = None,
    on_text_chunk: Optional[Callable[[str], None]] = None,
    on_tool_use: Optional[Callable[[str, dict], None]] = None,
    on_process_start: Optional[Callable[[asyncio.subprocess.Process], None]] = None,
    on_usage: Optional[Callable[[dict], None]] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
    append_system_prompt: Optional[str] = None,
    extra_env: Optional[dict] = None,
    agy_bin: Optional[str] = None,
    model_provider: str = "gemini",
    api_key: Optional[str] = None,
    api_key_env: Optional[str] = None,
    proxy: Optional[str] = None,
    print_timeout: str = DEFAULT_PRINT_TIMEOUT,
    dangerously_skip_permissions: bool = True,
    idle_timeout_sec: int = IDLE_TIMEOUT,
) -> tuple[str, Optional[str], bool]:
    """返回 (full_text, conversation_id, used_fresh_session_fallback)。

    used_fresh_session_fallback 恒为 False：agy 遇到不存在的 conversation 会
    自己开新会话，不需要 runner 再补一次。
    """
    del on_status  # agy 没有独立状态事件通道，正文/工具事件已够用

    global _mcp_registered
    if (
        not _mcp_registered
        and (extra_env or {}).get("CC_LARK_THREAD_ID")
        and os.getenv("CC_LARK_WAKE_MCP", "1") != "0"
    ):
        try:
            ensure_cc_lark_mcp()
        except OSError as exc:
            print(f"[run_agy] cc-mcp 注册跳过: {type(exc).__name__}: {exc}", flush=True)
        _mcp_registered = True

    try:
        ensure_bot_home()
        ensure_model_provider(model_provider)
    except OSError as exc:
        print(f"[run_agy] settings.json 写入跳过: {type(exc).__name__}: {exc}", flush=True)

    resolved_model, resolved_effort = _resolve_model_effort(model, effort)
    idle_limit = idle_timeout_sec if idle_timeout_sec > 0 else IDLE_TIMEOUT

    prompt = f"{append_system_prompt}\n\n{message}" if append_system_prompt else message
    prompt = _truncate_prompt(prompt)

    work_dir = cwd or os.path.expanduser("~")
    cmd = [
        resolve_agy_bin(agy_bin),
        "--output-format", "stream-json",
        "--print-timeout", print_timeout or DEFAULT_PRINT_TIMEOUT,
        # ⚠️ 必须显式把 cwd 加进 workspace：只靠进程 cwd 的话 agy 会把 run_command
        # 扔进 ~/.gemini/antigravity-cli/brain/<id>/scratch 里跑，而且读工作目录外
        # 的文件会被 permission check 拒掉。加了 --add-dir 之后 pwd 才是 cwd。
        "--add-dir", work_dir,
    ]
    cmd += _build_permission_args(permission_mode, dangerously_skip_permissions)
    if session_id:
        cmd += ["--conversation", session_id]
    if resolved_model:
        cmd += ["--model", resolved_model]
    if resolved_effort:
        cmd += ["--effort", resolved_effort]

    start_wall_time = time.time()
    log_dir = os.path.join(AGY_HOME, "log")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        pass
    run_log_file = os.path.join(
        log_dir,
        f"run-{int(start_wall_time)}-{uuid.uuid4().hex[:6]}.log",
    )
    cmd += ["--log-file", run_log_file]

    # -p 吃紧随其后的那个 token，所以 prompt 必须是最后一段
    cmd += ["-p", prompt]

    env = os.environ.copy()
    # 别让 bot spawn 出来的 agent 被 session-mirror hook 镜像回 Lark
    env["CC_LARK_MIRROR_OFF"] = "1"
    # 走 bot 专用 HOME，别动用户手动登录的 ~/.gemini（见文件头 AGY_BOT_HOME）。
    # 但 provider 为空 = 要走 OAuth，而 OAuth 凭证绑 HOME、keychain 不共享，
    # 隔离 HOME 里必然未登录 → 会卡 60s 等授权再失败。这种组合直接退回真 HOME。
    if AGY_BOT_HOME:
        if (model_provider or "").strip():
            env["HOME"] = AGY_BOT_HOME
        else:
            print(
                "[run_agy] AGY_MODEL_PROVIDER 为空（走 OAuth），忽略 AGY_BOT_HOME "
                "隔离并共用 ~/.gemini —— 隔离 HOME 里没有 OAuth 凭证",
                flush=True,
            )
    # API key 只在 provider 非空（= 真要走 API key 通道）时注入，与上面
    # AGY_BOT_HOME 那支保持同一条闸门：provider 为空就是走 OAuth，此时注入
    # GEMINI_API_KEY 用不上，只会让排查时误以为在跑 API key 模式。
    # （注：注入 key 本身并不会导致 400 User location —— 2026-09-10 实测带与
    # 不带 key 的失败率同为约五成，那个 400 的成因见文件尾部长注释。）
    if api_key:
        if (model_provider or "").strip():
            env[(api_key_env or "GEMINI_API_KEY").strip() or "GEMINI_API_KEY"] = api_key
        else:
            print(
                "[run_agy] AGY_MODEL_PROVIDER 为空（走 OAuth），忽略 AGY_API_KEY "
                "—— 注入 API key 会被 executor 抢用并触发 400 User location",
                flush=True,
            )
    if proxy:
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            env[key] = proxy
        # cc_mcp_server 要回连 bot 的 control API（127.0.0.1:9982），别走代理
        no_proxy = "127.0.0.1,localhost,::1"
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
    if extra_env:
        env.update(extra_env)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir,
        env=env,
        limit=10 * 1024 * 1024,
        # 与其它后端一致：独立进程组，/stop 时 killpg 不会误伤 main.py
        start_new_session=True,
    )
    await _fire_callback(on_process_start, proc)

    full_text = ""
    new_session_id = session_id
    error_detail = ""
    last_step_usage: dict = {}
    reported_tools: dict[int, str] = {}  # step_index → 已上报过的最后状态
    current_turn_has_error_step = False
    last_agent_response_done = False
    is_stale_history_error = False

    idle_seconds = 0
    loop = asyncio.get_event_loop()
    start_time = loop.time()
    wall_clock_limit = resolve_claude_wall_clock_limit(extra_env)

    while True:
        if wall_clock_limit > 0 and loop.time() - start_time >= wall_clock_limit:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"agy 单轮执行超过 wall-clock 最终上限（{int(wall_clock_limit)}秒），已终止进程。"
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
                        f"agy 执行超时（{threshold}秒有子进程但 agy 端无任何新输出），已终止进程。"
                        f"常见原因：tail -f / watch / npm run dev 等永不退出的阻塞命令。"
                    )
                raise RuntimeError(
                    f"agy 执行超时（{threshold}秒无输出且无活跃子进程），已终止进程"
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

        event = data.get("event")

        if event == "init":
            sid = data.get("conversation_id")
            if sid:
                new_session_id = sid

        elif event == "step_update":
            step = data.get("step_update") or {}
            sid = step.get("conversation_id")
            if sid:
                new_session_id = sid
            step_type = step.get("step_type")
            state = str(step.get("state") or "").upper()

            if state == "ERROR":
                current_turn_has_error_step = True

            if step_type == "agent_response":
                if state == "DONE":
                    last_agent_response_done = True
                # 每个 agent_response 步都带自己那次请求的 usage —— 最后一步的
                # 就是本轮真实的上下文占用（result.usage 是全部步骤的累加，见
                # _usage_from_result 的说明）。
                step_usage = step.get("usage")
                if isinstance(step_usage, dict) and step_usage:
                    last_step_usage = step_usage
                chunk = step.get("text_delta") or ""
                if chunk:
                    full_text += chunk
                    await _fire_callback(on_text_chunk, chunk)

            elif step_type == "tool":
                info = step.get("tool_info") or {}
                name = step.get("tool_name") or info.get("name") or ""
                idx = step.get("step_index")
                state = str(step.get("state") or "").upper()
                # 每个工具上报两次：ACTIVE（开始，带真实入参）+ DONE/ERROR（收尾，
                # 多带一个 _agy_state 让卡片把 🔧 换成 ✅/❌）。同一 step_index 的
                # 同一状态只报一次——agy 会重复推同状态的 step_update。
                if name and state in ("ACTIVE", "DONE", "ERROR") and reported_tools.get(idx) != state:
                    reported_tools[idx] = state
                    raw = info.get("parameters")
                    params = dict(raw) if isinstance(raw, dict) else {}
                    if state != "ACTIVE":
                        params["_agy_state"] = state
                    await _fire_callback(on_tool_use, name, params)

        elif event == "result":
            result = data.get("result") or {}
            sid = result.get("conversation_id")
            if sid:
                new_session_id = sid
            status = str(result.get("status") or "").upper()
            resp_str = str(result.get("response") or "")
            err_str = str(result.get("error") or "")
            # 只有在非 SUCCESS、或 error 字段显式报错、或 response 纯粹是一句简短的错误模版时，才视为中断/崩溃。
            # 避免用户询问该报错时，模型的正常技术分析回答（status=SUCCESS 且长文本）因正文中提及该报错短语而被误伤。
            resp_clean = resp_str.strip().lower()
            is_stream_interrupted = (
                "stream was interrupted" in err_str.lower()
                or (
                    "stream was interrupted" in resp_clean
                    and (status != "SUCCESS" or len(resp_clean) < 150)
                )
            )
            is_exec_terminated = (
                "execution terminated due to error" in err_str.lower()
                or (
                    "execution terminated due to error" in resp_clean
                    and (status != "SUCCESS" or len(resp_clean) < 150)
                )
            )

            # 判定是否为 agy CLI LastRunErrorDetails 扫描整个历史 steps 造成的旧错误污染 (stale error contamination)
            is_stale_template = any(
                tpl in (err_str or resp_str).lower()
                for tpl in (
                    "stream was interrupted",
                    "execution terminated due to error",
                    "user location is not supported",
                )
            )
            is_stale_history_error = (
                is_stale_template
                and not current_turn_has_error_step
                and (last_agent_response_done or bool(resp_str.strip()))
            )

            if (status and status != "SUCCESS") or is_stream_interrupted or is_exec_terminated:
                error_detail = (
                    result.get("error") or result.get("response") or status
                )
            final_text = result.get("response") or ""
            if final_text:
                full_text = final_text
            usage = _usage_from_result(result, last_step_usage, resolved_model)
            if usage:
                await _fire_callback(on_usage, usage)

    stderr_output = await proc.stderr.read()
    await proc.wait()
    stderr_text = stderr_output.decode("utf-8", errors="replace").strip()

    # 尝试从专属运行日志或 stderr 中提取底层的真实报错
    raw_error = _extract_agy_log_error(
        run_log_file,
        after_ts=start_wall_time,
        stderr_text=stderr_text,
    )

    # 历史错误污染防护：若报错属于已知的 agy CLI 历史倒序扫描污染模版，
    # 且本轮没有任何 step 报错、回答已完整生成（last_agent_response_done 或 full_text/resp_str 非空），
    # 则该错误纯属历史幽灵干扰（即使退出码非 0，只要回答完整且无 step 报错，通常只是 LSP shutdown timeout），予以忽略并正常采纳回答。
    if error_detail and is_stale_history_error:
        clean_text = (full_text or resp_str).strip()
        if clean_text:
            print(
                f"[run_agy] 忽略 agy 历史步骤旧错误污染: status={status}, error={err_str}, "
                f"本轮回答已正常完成 (len={len(clean_text)}) 且 exit_code={proc.returncode}",
                flush=True,
            )
            error_detail = ""
            if not full_text:
                full_text = resp_str

    if error_detail or (proc.returncode != 0 and not full_text):
        if not error_detail and proc.returncode != 0:
            error_detail = f"agy exited with code {proc.returncode}: {stderr_text or 'no stderr'}"

        if raw_error and raw_error not in str(error_detail):
            error_detail = f"{error_detail} (上游原始错误: {raw_error})"
        elif "stream was interrupted" in str(error_detail).lower() and not raw_error:
            error_detail = f"{error_detail} (上游连接流式中断，常见于网络抖动、模型服务端超时或会话历史过长)"

        exc = RuntimeError(f"agy 执行出错：{error_detail}")
        exc.cc_session_id = new_session_id
        exc.cc_retryable_resume = not is_fatal_error_text(str(error_detail))
        if run_log_file and os.path.exists(run_log_file):
            print(f"[run_agy] 出错日志已保留: {run_log_file}", flush=True)
        raise exc

    if proc.returncode != 0:
        if run_log_file and os.path.exists(run_log_file):
            try:
                os.unlink(run_log_file)
            except OSError:
                pass
        return full_text.strip(), new_session_id, False

    # 成功执行且无错误，清理当前轮的独立临时日志
    if run_log_file and os.path.exists(run_log_file):
        try:
            os.unlink(run_log_file)
        except OSError:
            pass

    return full_text.strip(), new_session_id, False


# ── Antigravity 后端的间歇性抽风 ────────────────────────────────────────
# agy 打的是 `daily-cloudcode-pa.googleapis.com`（Google 内部 daily 环境），
# 会**随机**对约半数请求回 400 FAILED_PRECONDITION
# `User location is not supported for the API use.`
# 这句文案具有强误导性：2026-09-10 实测同一时刻、同一出口（日本 IEPL，IP 稳定
# 不变）——
#   · 直连 Gemini API 的 generateContent 探针 6/6 全通  → 出口地区没问题
#   · agy /usage 显示 Weekly 89% / 5h 82%              → 额度没问题
#   · 日志里 OAuth 明确 authenticated 成功              → 鉴权没问题
#   · 换节点（日本Z02，另一 IP）同样约半数失败          → 不是单个 IP 被拉黑
# 也就是说它既不是地区、也不是额度或鉴权问题，是上游自己在抖，本机无法根治。
#
# 它是**独立随机事件**，所以正确的应对是「立刻重试」而不是退避等待：dispatcher
# 那套 10s/30s/60s 的 stall 冷却是为限流设计的，对这里既没用又白等 100 秒，而且
# 只有 3 次预算（半数失败率下有 ~12% 概率整轮打死，用户看到的就是这个）。
# 这里在 runner 内部就地快速重试，不消耗 dispatcher 的 stall 预算。
_AGY_FLAKY_MARKERS = (
    "user location is not supported",
)
_AGY_FLAKY_RETRY_MAX_DEFAULT = 4
_AGY_FLAKY_RETRY_DELAY = 2.0


def is_flaky_upstream_error(text: str) -> bool:
    """是否属于「上游随机抽风、原地重试大概率就好」的错误。"""
    low = (text or "").lower()
    return any(m in low for m in _AGY_FLAKY_MARKERS)


async def run_agy(**kwargs):
    """_run_agy_once + 针对上游随机抽风的就地快速重试。

    只对 is_flaky_upstream_error 命中的错误重试，其余错误（认证 / 额度 / 参数
    错等）原样抛出，交回 dispatcher 走它自己的续跑或错误卡逻辑。
    """
    max_retry = _env_int_agy("AGY_FLAKY_RETRY_MAX", _AGY_FLAKY_RETRY_MAX_DEFAULT)
    attempt = 0
    while True:
        try:
            return await _run_agy_once(**kwargs)
        except RuntimeError as exc:
            if attempt >= max_retry or not is_flaky_upstream_error(str(exc)):
                raise
            attempt += 1
            # 上一次可能已经建好 conversation 并写入了用户消息，续用同一个会话
            # 重试，避免丢掉本轮之前的上下文（新开会话等于把历史扔了）。
            resumed = getattr(exc, "cc_session_id", None)
            if resumed:
                kwargs["session_id"] = resumed
            print(
                f"[run_agy] 上游随机抽风（400 User location，非真地区问题），"
                f"就地重试 {attempt}/{max_retry}",
                flush=True,
            )
            await asyncio.sleep(_AGY_FLAKY_RETRY_DELAY)


def _env_int_agy(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default
