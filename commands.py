"""
斜杠命令解析与处理。
返回要发送给用户的回复文本。
"""

import asyncio
import getpass
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime
from typing import Optional, Tuple

from bot_config import CLAUDE_CLI, DEFAULT_CWD
from session_store import SessionStore, scan_cli_sessions, generate_summary, _get_api_token, _write_custom_title, _find_session_file

PLUGINS_DIR = os.path.expanduser("~/.claude/plugins")


VALID_MODES = {
    "default": "每次工具调用需确认",
    "acceptEdits": "自动接受文件编辑，其余需确认",
    "plan": "只规划不执行工具",
    "bypassPermissions": "全部自动执行（无确认）",
    "dontAsk": "全部自动执行（静默）",
}

MODE_ALIASES = {
    "bypass": "bypassPermissions",
    "accept": "acceptEdits",
    "auto": "bypassPermissions",
}

MODEL_ALIASES = {
    "fable": "claude-fable-5[1m]",
    "opus": "claude-opus-4-8[1m]",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "codex-max": "gpt-5.1-codex-max",
    "codex": "gpt-5.1-codex",
    "gpt5": "gpt-5.1",
}

HELP_TEXT = """\
📖 **可用命令**

**Bot 管理：**
`/help` — 显示此帮助
`/stop` — 停止当前正在运行的任务
`/new` 或 `/clear` — 开始新 session
`/defaults` — 新开 session，并把当前 chat 参数重置为配置默认值
`/resume` — 查看历史 sessions / `/resume [序号]` 恢复
`/runner [codex|claude]` — 切换当前 chat 使用 Codex 或 Claude Code
`/model [名称]` — 切换当前 bot 后端支持的模型（也可填完整 ID）
`/mode [模式]` — 切换权限模式（default / plan / acceptEdits / bypassPermissions）
`/status` — 显示当前 session 信息
`/cd [路径]` — 切换工具执行的工作目录
`/ls [路径]` — 查看当前工作目录下的文件/目录
`/exec <命令>` — 在当前 cwd 执行 shell 命令（30s 超时）
`/workspace` 或 `/ws` — 保存/切换群组工作空间

**查看能力：**
`/skills` — 列出已安装的 Claude Skills
`/mcp` — 列出已配置的 MCP Servers
`/usage` — 查看当前 runner 的上下文/用量信息
`/accounts` — 查看所有 Claude Max 账户全景 + 智能切换状态

**审计：**
`/verify [关注点]` — 在话题群里开新 session，审上方整段对话（既审 bot 的回答也审代码改动）

**服务管理：**
`/restart` — 重启 cc-lark 服务（detached，不会自残）
`/group add <chat_id> [cwd]` — 把群加白名单并设默认 cwd（实时生效 + 持久化到 .env）


**Claude Skills（直接转发给 Claude 执行）：**
`/commit` — 提交代码
其他 `/xxx` — 自动转发给 Claude 处理

**MCP 工具：** 已配置的 MCP servers 自动可用，直接对话即可调用。

**发送任意普通消息即可与当前 runner 对话。**\
"""


def parse_command(text: str) -> Optional[Tuple[str, str]]:
    """
    尝试解析斜杠命令。
    返回 (command, args) 或 None（不是命令）。
    """
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return cmd, args


# Bot 自身处理的命令，其余 /xxx 转发给 Claude
BOT_COMMANDS = {
    "help", "h", "new", "clear", "resume", "runner", "model", "mode", "status", "cd", "ls",
    "exec", "workspace", "ws", "skills", "mcp", "usage", "accounts", "stop",
    "restart", "group", "defaults",
}

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
APP_PATH = "/Applications/cc-lark.app"


async def _build_session_list(user_id: str, chat_id: str, store: SessionStore, cli_all: list[dict] | None = None) -> list[dict]:
    """构建合并、去重、排序后的 session 列表（不含当前 session）。
    /resume 列表展示和 /resume N 选择都用这一个函数，保证索引一致。"""
    cur_sid = (await store.get_current_raw(user_id, chat_id)).get("session_id")

    if cli_all is None:
        cli_all = scan_cli_sessions(30)
    cli_preview_map = {s["session_id"]: s for s in cli_all}

    feishu_sessions = [
        {**s, "source": "feishu"} for s in await store.list_sessions(user_id, chat_id)
    ]
    for s in feishu_sessions:
        cli_info = cli_preview_map.get(s["session_id"])
        if cli_info and cli_info.get("preview"):
            s["preview"] = cli_info["preview"]

    feishu_ids = {s["session_id"] for s in feishu_sessions}
    cli_sessions = [
        s for s in cli_all
        if s["session_id"] not in feishu_ids and len(s.get("preview", "")) > 5
    ]
    all_sessions = feishu_sessions + cli_sessions

    seen = set()
    if cur_sid:
        seen.add(cur_sid)
    deduped = []
    for s in all_sessions:
        sid = s["session_id"]
        if sid not in seen:
            seen.add(sid)
            deduped.append(s)

    deduped.sort(key=lambda s: s.get("started_at", ""), reverse=True)
    return deduped[:15]


def _strip_md(text: str) -> str:
    """去除 markdown 格式 + 压成单行纯文本"""
    text = " ".join(text.split())
    while text.startswith("#"):
        text = text.lstrip("#").lstrip()
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = text.replace("<", "").replace(">", "")
    return text.strip()


async def _format_session_list(user_id: str, chat_id: str, store: SessionStore):
    """生成历史 sessions 列表，每个会话一个按钮。返回 dict(text, buttons) 或 str。"""
    from session_store import _clean_preview

    cur = await store.get_current_raw(user_id, chat_id)
    cur_sid = cur.get("session_id")

    cli_all = scan_cli_sessions(30)
    cli_preview_map = {s["session_id"]: s for s in cli_all}
    all_sessions = await _build_session_list(user_id, chat_id, store, cli_all=cli_all)

    if not cur_sid and not all_sessions:
        return "暂无历史 sessions。"

    # 收集已缓存的摘要，缺失的后台生成（不阻塞列表展示）
    summaries = {}
    missing = []
    all_sids = [cur_sid] if cur_sid else []
    all_sids += [s["session_id"] for s in all_sessions]
    for sid in all_sids:
        cached = store.get_summary(user_id, sid)
        if cached:
            summaries[sid] = cached
        else:
            missing.append(sid)
    if missing:
        for sid in missing[:5]:
            asyncio.create_task(store._bg_generate_summary(user_id, sid))

    def _desc(sid: str, preview_raw: str) -> str:
        s = summaries.get(sid, "")
        if s:
            s = _strip_md(s)
            return s if len(s) <= 30 else s[:28] + ".."
        p = _clean_preview(preview_raw or "")
        if not p:
            return "（无预览）"
        p = _strip_md(p)
        return p if len(p) <= 30 else p[:28] + ".."

    def _fmt_time(raw: str) -> str:
        t = raw[:16].replace("T", " ")
        if len(t) >= 16:
            t = t[5:16].replace("-", "/")
        return t

    # 当前 session 信息
    lines = []
    if cur_sid:
        cli_info = cli_preview_map.get(cur_sid)
        preview = (cli_info.get("preview") if cli_info and cli_info.get("preview")
                   else cur.get("preview") or "")
        lines.append(f"当前：{_desc(cur_sid, preview)} ({_fmt_time(cur.get('started_at', ''))})")

    lines.append(f"共 {len(all_sessions)} 个历史会话")

    # 每个历史会话一个按钮
    buttons = []
    for s in all_sessions[:10]:
        sid = s["session_id"]
        preview = s.get("preview", "")
        desc = _desc(sid, preview)
        time_str = _fmt_time(s.get("started_at", ""))
        buttons.append({
            "text": f"{desc} ({time_str})",
            "value": {"action": "resume_session", "sid": sid, "cid": chat_id},
        })

    if buttons:
        return {"text": "\n".join(lines), "buttons": buttons}
    return "\n".join(lines)


def _list_skills(chat_id: str = ""):
    """扫描 ~/.claude/plugins + ~/.claude/skills 目录，返回 dict(text, buttons) 或 str"""
    skills = []
    # 扫描 plugins (旧格式)
    if os.path.isdir(PLUGINS_DIR):
        for root, dirs, files in os.walk(PLUGINS_DIR):
            if os.path.basename(root) != "commands":
                continue
            for fname in files:
                if not fname.endswith(".md"):
                    continue
                name = fname[:-3]
                fpath = os.path.join(root, fname)
                desc = _read_skill_desc(fpath)
                skills.append((name, desc))

    # 扫描 skills (新格式)
    skills_dir = os.path.expanduser("~/.claude/skills")
    if os.path.isdir(skills_dir):
        for entry in os.listdir(skills_dir):
            skill_md = os.path.join(skills_dir, entry, "SKILL.md")
            if os.path.isfile(skill_md):
                desc = _read_skill_desc(skill_md)
                skills.append((entry, desc))

    if not skills:
        return "暂无已安装的 skills。"

    skills.sort(key=lambda x: x[0])
    # 去重
    seen = set()
    unique = []
    for name, desc in skills:
        if name not in seen:
            seen.add(name)
            unique.append((name, desc))

    buttons = [
        {"text": f"/{name}", "value": {"action": "reply", "reply": f"/{name}", "cid": chat_id}}
        for name, desc in unique[:15]
    ]
    return {
        "text": f"🛠 **可用 Skills** ({len(unique)} 个)",
        "buttons": buttons,
    }


def _read_skill_desc(fpath: str) -> str:
    """从 skill/command 的 md 文件中提取 description"""
    try:
        with open(fpath, encoding="utf-8") as f:
            in_frontmatter = False
            for line in f:
                line = line.strip()
                if line == "---" and not in_frontmatter:
                    in_frontmatter = True
                    continue
                if line == "---" and in_frontmatter:
                    break
                if in_frontmatter and line.startswith("description:"):
                    return line[len("description:"):].strip().strip('"')
    except OSError:
        pass
    return ""


def fetch_quota_headers() -> dict:
    """发轻量 API 请求拉 Claude Max 用量 headers。

    返回结构：
        {"ok": True,  "u5h": 0.31, "u7d": 0.33, "r5h": 1736900000, "r7d": ...,
         "s5h": "allowed", "s7d": "allowed"}
        {"ok": False, "error": "..."}

    quota_watcher 和 /usage / /status 都共用这一个入口。
    """
    if sys.platform != "darwin":
        return {"ok": False, "error": "目前只支持 macOS"}

    import urllib.request
    import urllib.error
    import ssl

    try:
        from account_switcher import decode_security_stdout, ensure_keychain_intact
        ensure_keychain_intact()  # /restart 周期里 keychain 被写丢时自愈
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        creds = json.loads(decode_security_stdout(result.stdout))
        token = creds["claudeAiOauth"]["accessToken"]
    except Exception as e:
        return {"ok": False, "error": f"读取凭证失败：{e}"}

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            headers = dict(resp.headers)
    except urllib.error.HTTPError as e:
        headers = dict(e.headers)
    except Exception as e:
        return {"ok": False, "error": f"获取用量失败：{e}"}

    def h(key):
        return headers.get(key) or headers.get(key.lower()) or headers.get(key.replace("-", "_"))

    def _to_float(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    def _to_int(v):
        try:
            return int(v) if v is not None else None
        except Exception:
            return None

    out = {
        "ok": True,
        "u5h": _to_float(h("anthropic-ratelimit-unified-5h-utilization")),
        "u7d": _to_float(h("anthropic-ratelimit-unified-7d-utilization")),
        "r5h": _to_int(h("anthropic-ratelimit-unified-5h-reset")),
        "r7d": _to_int(h("anthropic-ratelimit-unified-7d-reset")),
        "s5h": h("anthropic-ratelimit-unified-5h-status") or "unknown",
        "s7d": h("anthropic-ratelimit-unified-7d-status") or "unknown",
    }
    if out["u5h"] is None and out["u7d"] is None:
        return {"ok": False, "error": "响应中无用量 headers"}
    return out


def _fmt_pct_bar(val) -> str:
    if val is None:
        return "未知"
    pct = float(val) * 100
    bar_len = 20
    filled = round(pct / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    return f"{bar} {pct:.1f}%"


def _fmt_reset_ts(ts) -> str:
    if ts is None:
        return "未知"
    try:
        dt = datetime.fromtimestamp(int(ts))
        diff = dt - datetime.now()
        hours = int(diff.total_seconds() // 3600)
        minutes = int((diff.total_seconds() % 3600) // 60)
        return f"{dt.strftime('%m/%d %H:%M')}（{hours}h{minutes}m 后）"
    except Exception:
        return str(ts)


def _usage_single_account_lines(data: dict, account_label: Optional[str] = None) -> list[str]:
    """渲染单账户的 5h / 7d bar + reset 倒计时（共用片段）。"""
    title = "📊 **Claude Max 用量**"
    if account_label:
        title += f" — 当前 `{account_label}`"
    lines = [title, ""]
    lines.append(f"**5小时窗口**（状态：{data.get('s5h', '?')}）")
    lines.append(_fmt_pct_bar(data.get("u5h")))
    lines.append(f"重置时间：{_fmt_reset_ts(data.get('r5h'))}")
    lines.append("")
    lines.append(f"**7天窗口**（状态：{data.get('s7d', '?')}）")
    lines.append(_fmt_pct_bar(data.get("u7d")))
    lines.append(f"重置时间：{_fmt_reset_ts(data.get('r7d'))}")
    return lines


def _get_usage() -> str:
    """渲染 /usage 命令的完整输出。

    保存了多个账户时：顶部展示当前 active 账户的详尽 bar/重置，下方列出
    其他账户的一行简表（用量 / score / 是否可用），底部显示自动切换开关。
    没保存任何账户时退回单账户老视图。
    """
    accounts: dict = {}
    current: Optional[str] = None
    try:
        from account_switcher import probe_all, current_account_name, evaluate
        accounts = probe_all()
        current = current_account_name()
        for a in accounts.values():
            evaluate(a, current)
    except Exception:
        accounts = {}
        current = None

    # 没保存账户 → 老路径
    if not accounts:
        data = fetch_quota_headers()
        if not data.get("ok"):
            return f"❌ {data.get('error', '获取用量失败')}"
        return "\n".join(_usage_single_account_lines(data))

    # 顶部：当前 active 账户的详尽 bar
    cur = accounts.get(current) if current else None
    lines: list[str] = []
    if cur and not cur.probe_error and cur.u5h is not None:
        data = {
            "u5h": cur.u5h, "u7d": cur.u7d, "r5h": cur.r5h, "r7d": cur.r7d,
            "s5h": cur.s5h, "s7d": cur.s7d,
        }
        lines.extend(_usage_single_account_lines(data, account_label=cur.name))
    else:
        # current 探测失败 / 没识别 → 直接 fetch keychain 兜底
        data = fetch_quota_headers()
        if data.get("ok"):
            label = current or "未识别"
            lines.extend(_usage_single_account_lines(data, account_label=label))
        else:
            lines.append(f"⚠️ 当前账户用量获取失败：{data.get('error', '未知')}")

    # 其他账户的一行简表（按 score 降序，可用的先排）
    others = [a for a in accounts.values() if a.name != current]
    if others:
        # usable 在前；usable 段内按 score 降序；unusable 在后按 name
        others.sort(key=lambda a: (0 if a.usable else 1, -a.score, a.name))
        lines.append("")
        lines.append("**其他账户：**")
        best_other = max((a for a in others if a.usable), key=lambda a: a.score, default=None)
        cur_score = cur.score if cur else 0.0
        cur_usable = bool(cur and cur.usable)
        now_ts = time.time()
        for a in others:
            if a.probe_error:
                lines.append(f"  `{a.name}` — ⚠️ {a.probe_error}")
                continue
            u5 = f"{a.u5h*100:.0f}%" if a.u5h is not None else "?"
            u7 = f"{a.u7d*100:.0f}%" if a.u7d is not None else "?"
            mark = "✅" if a.usable else "❌"
            r5_part = ""
            if a.r5h:
                secs = max(0, a.r5h - now_ts)
                r5_part = f" (重置 {int(secs//3600)}h{int(secs%3600/60)}m)"
            tail = ""
            if a is best_other and cur_usable and (a.score - cur_score) >= 0.15:
                tail = "（推荐切换）"
            elif a is best_other and not cur_usable:
                tail = "（当前不可用，候选）"
            elif not a.usable and a.reasons:
                tail = f"（{a.reasons[0]}）"
            lines.append(
                f"  `{a.name}` · 5h `{u5}`{r5_part} · 7d `{u7}` · score `{a.score:.2f}` {mark}{tail}"
            )

    # 自动切换开关状态
    auto_on = os.getenv("ACCOUNT_AUTO_SWITCH", "0").strip().lower() in ("1", "true", "yes", "on")
    cooldown = int(os.getenv("ACCOUNT_SWITCH_COOLDOWN_SEC", "1800"))
    lines.append("")
    if auto_on:
        lines.append(f"自动切换：✅ 已启用（冷却 {cooldown // 60} min · `/accounts` 看全景）")
    else:
        lines.append("自动切换：⏸ 未启用（`.env` 设 `ACCOUNT_AUTO_SWITCH=1` 打开）")
    return "\n".join(lines)



def _list_mcp() -> str:
    """调用 claude mcp list 获取已配置的 MCP servers"""
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "mcp", "list"],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stdout.strip()
    except Exception as e:
        return f"❌ 获取 MCP 列表失败：{e}"

    if not output:
        return "暂无已配置的 MCP servers。\n\n用 `claude mcp add` 在终端添加。"

    return f"🔌 **已配置的 MCP Servers**\n\n{output}"


def _context_window_for(model: str) -> int:
    m = (model or "").lower()
    if "1m" in m:
        return 1_000_000
    if m.startswith("gpt-5") or "codex" in m:
        return 258_400
    return 200_000


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1000:
        return f"{n/1000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(n)


def _find_codex_session_file(session_id: str) -> str:
    if not session_id:
        return ""
    root = os.path.expanduser("~/.codex/sessions")
    if not os.path.isdir(root):
        return ""
    suffix = f"{session_id}.jsonl"
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(suffix):
                return os.path.join(dirpath, name)
    return ""


def _normalize_codex_usage(raw: dict) -> dict:
    input_tokens = int(raw.get("input_tokens", 0) or 0)
    cached = int(raw.get("cached_input_tokens", 0) or 0)
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": int(raw.get("output_tokens", 0) or 0),
        "reasoning_output_tokens": int(raw.get("reasoning_output_tokens", 0) or 0),
    }
    if cached:
        usage["_cached_input_tokens"] = cached
    return usage


def _read_last_usage(session_id: Optional[str], runner: str = "claude") -> dict:
    """从 runner session 文件倒着找最后一次 usage。"""
    if not session_id:
        return {}
    runner = (runner or "claude").lower()
    fpath = _find_codex_session_file(session_id) if runner == "codex" else _find_session_file(session_id)
    if not fpath:
        return {}
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return {}
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if runner == "codex":
            payload = d.get("payload") or {}
            if d.get("type") == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                usage = _normalize_codex_usage(info.get("last_token_usage") or {})
                window = int(info.get("model_context_window") or 0)
                if window:
                    usage["_context_window"] = window
                return usage
            if d.get("type") == "turn.completed" and isinstance(d.get("usage"), dict):
                return _normalize_codex_usage(d["usage"])
        else:
            if d.get("type") != "assistant":
                continue
            usage = (d.get("message") or {}).get("usage") or {}
            if usage:
                return usage
    return {}


def _format_context_line(session_id: Optional[str], model: str, runner: str = "claude", current_usage: Optional[dict] = None) -> str:
    usage = current_usage or _read_last_usage(session_id, runner=runner)
    if not usage:
        return "上下文: （暂无数据，发一条消息后可见）"
    total = (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0)
    )
    if total <= 0:
        return "上下文: （无）"
    window = int(usage.get("_context_window") or 0) or _context_window_for(model)
    pct = total / window * 100
    return f"上下文: `{_fmt_tokens(total)} / {_fmt_tokens(window)} ({pct:.1f}%)`"


def _format_codex_rate_line(session_id: Optional[str]) -> str:
    fpath = _find_codex_session_file(session_id or "")
    if not fpath:
        return ""
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""
    for raw in reversed(lines):
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        payload = d.get("payload") or {}
        if d.get("type") != "event_msg" or payload.get("type") != "token_count":
            continue
        rate_limits = payload.get("rate_limits") or {}
        primary = rate_limits.get("primary") or {}
        secondary = rate_limits.get("secondary") or {}
        parts = []
        if primary.get("used_percent") is not None:
            parts.append(f"5h {float(primary['used_percent']):.1f}%")
        if secondary.get("used_percent") is not None:
            parts.append(f"7d {float(secondary['used_percent']):.1f}%")
        if not parts:
            return ""
        return f"Codex 配额: `{' · '.join(parts)}`"
    return ""


def _runner_default_model(bot, runner: str) -> str:
    profile = getattr(bot, "profile", None)
    if profile and getattr(profile, "runner", "") == runner and getattr(profile, "default_model", ""):
        return profile.default_model
    return "gpt-5.5" if runner == "codex" else "claude-sonnet-4-6"


def _get_quota_compact() -> str:
    """获取 Claude Max 5h/7d 用量的紧凑一行版本，失败返回空串。"""
    data = fetch_quota_headers()
    if not data.get("ok"):
        return ""
    parts = []
    if data["u5h"] is not None:
        parts.append(f"5h {data['u5h']*100:.1f}%")
    if data["u7d"] is not None:
        parts.append(f"7d {data['u7d']*100:.1f}%")
    tail = ""
    if data["r5h"]:
        try:
            dt = datetime.fromtimestamp(data["r5h"])
            diff = dt - datetime.now()
            h_ = int(diff.total_seconds() // 3600)
            m_ = int((diff.total_seconds() % 3600) // 60)
            tail = f"（5h 重置 {h_}h{m_}m 后）"
        except Exception:
            pass
    return f"Claude 配额: `{' · '.join(parts)}` {tail}".strip()


def _get_accounts() -> str:
    """渲染 /accounts 命令——展示所有保存账户的 5h/7d 用量、score、可用性。"""
    try:
        from account_switcher import AccountSwitcher, probe_all, current_account_name
    except Exception as e:
        return f"❌ account_switcher 加载失败：{e}"
    try:
        accounts = probe_all()
        current = current_account_name()
    except Exception as e:
        return f"❌ 探测账户失败：{e}"
    sw = AccountSwitcher()  # 无 send_fn、纯渲染
    return sw.render_matrix(accounts, current)


_EXEC_TIMEOUT_SEC = 30
_EXEC_MAX_OUTPUT = 3000


async def _exec_shell(user_id: str, chat_id: str, store: SessionStore, args: str) -> str:
    if not args:
        return "⚠️ 用法：`/exec <shell命令>`，例如：`/exec git status`"
    cur = await store.get_current_raw(user_id, chat_id)
    cwd = cur.get("cwd", DEFAULT_CWD)
    if not os.path.isdir(cwd):
        return f"❌ 当前 cwd 不存在：`{cwd}`"

    try:
        proc = await asyncio.create_subprocess_shell(
            args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        return f"❌ 启动失败：{type(e).__name__}: {e}"

    timed_out = False
    try:
        out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=_EXEC_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass
        out_bytes = b""

    output = (out_bytes or b"").decode("utf-8", errors="replace")
    truncated = len(output) > _EXEC_MAX_OUTPUT
    if truncated:
        output = output[:_EXEC_MAX_OUTPUT] + f"\n...（输出超过 {_EXEC_MAX_OUTPUT} 字符已截断）"
    if not output.strip():
        output = "（无输出）"

    header = [
        f"💻 `$ {args}`",
        f"📂 cwd: `{cwd}`",
    ]
    if timed_out:
        header.append(f"⏱ 超时（>{_EXEC_TIMEOUT_SEC}s），已终止")
    else:
        header.append(f"返回码：`{proc.returncode}`")
    return "\n".join(header) + f"\n```\n{output}\n```"


async def _list_directory(user_id: str, chat_id: str, store: SessionStore, args: str) -> str:
    cur = await store.get_current_raw(user_id, chat_id)
    base_dir = cur.get("cwd", DEFAULT_CWD)
    raw_target = args.strip()

    if not raw_target:
        target = base_dir
        display_target = "."
    elif os.path.isabs(raw_target):
        target = os.path.expanduser(raw_target)
        display_target = target
    else:
        target = os.path.abspath(os.path.join(base_dir, os.path.expanduser(raw_target)))
        display_target = raw_target

    if not os.path.exists(target):
        return f"❌ 路径不存在：`{display_target}`\n当前工作目录：`{base_dir}`"

    if not os.path.isdir(target):
        return f"❌ 目标不是目录：`{display_target}`"

    try:
        entries = []
        with os.scandir(target) as it:
            for entry in it:
                suffix = "/" if entry.is_dir() else ""
                entries.append((not entry.is_dir(), entry.name.lower(), f"`{entry.name}{suffix}`"))
    except OSError as e:
        return f"❌ 读取目录失败：{e}"

    entries.sort()
    preview = [item[2] for item in entries[:50]]
    hidden_count = max(0, len(entries) - len(preview))

    lines = [
        "📁 **目录内容**",
        f"请求路径：`{display_target}`",
        f"绝对路径：`{target}`",
    ]
    if not preview:
        lines.append("（空目录）")
        return "\n".join(lines)

    lines.append("")
    lines.extend(preview)
    if hidden_count:
        lines.append("")
        lines.append(f"…… 还有 {hidden_count} 项未显示")
    return "\n".join(lines)


async def _format_workspace_list(user_id: str, chat_id: str, store: SessionStore):
    cur = await store.get_current_raw(user_id, chat_id)
    current_name = cur.get("workspace", "")
    current_cwd = cur.get("cwd", "~")
    workspaces = store.list_workspaces(user_id)

    lines = ["🗂 **工作空间**"]
    lines.append(f"当前：`{current_name or '（未命名）'}` → `{current_cwd}`")

    buttons = []
    if workspaces:
        for name, path in workspaces.items():
            marker = " ✓" if name == current_name else ""
            buttons.append({
                "text": f"📁 {name}{marker}",
                "value": {"action": "run_cmd", "cmd": f"/ws use {name}", "cid": chat_id},
            })

    if buttons:
        lines.append(f"已保存 {len(workspaces)} 个，点击切换：")
        return {"text": "\n".join(lines), "buttons": buttons}

    lines.append("还没有已保存的工作空间。")
    lines.append("`/ws save 名称 [路径]` 保存")
    return "\n".join(lines)


async def _handle_workspace_command(
    args: str,
    user_id: str,
    chat_id: str,
    store: SessionStore,
) -> str:
    if not args:
        return await _format_workspace_list(user_id, chat_id, store)

    try:
        parts = shlex.split(args)
    except ValueError as e:
        return f"❌ 参数解析失败：{e}"

    if not parts:
        return await _format_workspace_list(user_id, chat_id, store)

    action = parts[0].lower()

    if action in {"list", "ls"}:
        return await _format_workspace_list(user_id, chat_id, store)

    if action in {"save", "add"}:
        if len(parts) < 2:
            return "⚠️ 用法：`/ws save 名称 [路径]`"
        name = parts[1]
        path = (await store.get_current_raw(user_id, chat_id)).get("cwd", DEFAULT_CWD)
        if len(parts) >= 3:
            path = os.path.expanduser(parts[2])
        if not os.path.isdir(path):
            return f"❌ 路径不存在：`{path}`"
        await store.save_workspace(user_id, name, path)
        return f"✅ 已保存工作空间 `{name}` → `{path}`"

    if action == "use":
        if len(parts) != 2:
            return "⚠️ 用法：`/ws use 名称`"
        name = parts[1]
        path = await store.bind_workspace(user_id, chat_id, name)
        if not path:
            return f"❌ 未找到工作空间：`{name}`，先用 `/ws save {name} 路径` 保存。"
        return (
            f"✅ 当前群组已绑定工作空间 `{name}`\n"
            f"工作目录：`{path}`\n"
            "如需清空旧上下文，可继续发送 `/new`。"
        )

    if action == "set":
        if len(parts) != 2:
            return "⚠️ 用法：`/ws set 路径`"
        path = os.path.expanduser(parts[1])
        if not os.path.isdir(path):
            return f"❌ 路径不存在：`{path}`"
        old_name = (await store.get_current_raw(user_id, chat_id)).get("workspace", "")
        await store.set_cwd(user_id, chat_id, path)
        suffix = "，并解除原工作空间绑定" if old_name else ""
        return f"✅ 当前群组工作目录已切换为 `{path}`{suffix}"

    if action in {"remove", "delete", "rm"}:
        if len(parts) != 2:
            return "⚠️ 用法：`/ws remove 名称`"
        name = parts[1]
        if not await store.delete_workspace(user_id, name):
            return f"❌ 未找到工作空间：`{name}`"
        return f"✅ 已删除工作空间 `{name}`"

    return (
        f"❌ 未知子命令：`{action}`\n"
        "可用：`list`、`save`、`use`、`set`、`remove`"
    )


def _modify_env_add_chat_to_profile(
    profile_name: str, chat_id: str, cwd: Optional[str]
) -> Tuple[bool, str]:
    """
    把 chat_id 追加到 .env 的 <PREFIX>_ALLOWED_GROUP_CHAT_IDS；
    若 cwd 非空，再追加/覆盖 <PREFIX>_CHAT_CWD_<chat_id>=<cwd>。
    返回 (是否改动, 提示信息)。
    """
    env_path = os.path.join(REPO_ROOT, ".env")
    if not os.path.isfile(env_path):
        return False, f"❌ .env 不存在: {env_path}"

    prefix = profile_name.upper()
    allow_key = f"{prefix}_ALLOWED_GROUP_CHAT_IDS"
    cwd_key = f"{prefix}_CHAT_CWD_{chat_id}"

    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    changed = False
    allow_seen = False
    cwd_seen = False
    out: list[str] = []
    for ln in lines:
        stripped = ln.lstrip()
        if stripped.startswith(f"{allow_key}="):
            allow_seen = True
            head, _, val = ln.partition("=")
            items = [x.strip() for x in val.split(",") if x.strip()]
            if chat_id not in items:
                items.append(chat_id)
                ln = f"{head}={','.join(items)}"
                changed = True
            out.append(ln)
            continue
        if cwd is not None and stripped.startswith(f"{cwd_key}="):
            cwd_seen = True
            new_ln = f"{cwd_key}={cwd}"
            if new_ln != ln:
                changed = True
            out.append(new_ln)
            continue
        out.append(ln)

    if not allow_seen:
        out.append(f"{allow_key}={chat_id}")
        changed = True
    if cwd is not None and not cwd_seen:
        out.append(f"{cwd_key}={cwd}")
        changed = True

    if changed:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")

    return changed, env_path


async def _handle_group_command(args: str, bot) -> str:
    """/group add <chat_id> [cwd] — 加白名单 + 设默认 cwd"""
    if bot is None:
        return "❌ 内部错误：handler 缺少 bot 上下文"
    parts = args.split(None, 2)
    if not parts or parts[0].lower() != "add":
        return "⚠️ 用法：`/group add <chat_id> [cwd]`"
    if len(parts) < 2:
        return "⚠️ 用法：`/group add <chat_id> [cwd]`"

    chat_id = parts[1].strip()
    cwd_arg = parts[2].strip() if len(parts) >= 3 else ""
    cwd: Optional[str] = None
    if cwd_arg:
        cwd = os.path.expanduser(cwd_arg)
        if not os.path.isdir(cwd):
            return f"❌ cwd 不存在: `{cwd}`"

    if not chat_id.startswith("oc_"):
        return f"⚠️ chat_id 看起来不对（应以 `oc_` 开头）: `{chat_id}`"

    profile = bot.profile
    bot.profile.allowed_group_chat_ids.add(chat_id)
    if cwd:
        bot.profile.chat_default_cwd[chat_id] = cwd

    changed, info = _modify_env_add_chat_to_profile(profile.name, chat_id, cwd)

    lines = [
        f"✅ 已加白群 `{chat_id}` 到 profile **{profile.name}** —— 实时生效",
    ]
    if cwd:
        lines.append(f"   默认 cwd: `{cwd}`")
    if changed:
        lines.append(f"   .env 已更新: `{info}`")
    else:
        lines.append("   .env 无变化（已存在）")
    lines.append("ℹ️  立即可用，无需 `/restart`。")
    return "\n".join(lines)


def _trigger_restart() -> None:
    """
    在新进程组里调度"清残留 + open .app"重启，然后让当前 bot 退出。
    - start_new_session=True 让 sh 脱离 wrapper 进程组，trap cleanup 杀不到它。
    - bash 退出后 Mach-O launcher（LSUIElement agent）不会自动退；不先 kill
      掉它，`open .app` 会判定 app 在跑、不重新拉起。所以 sleep 3s 等 bot
      exit + bash cleanup 跑完，再 pkill 整个 .app 进程树，最后 open。
    """
    quoted_app = shlex.quote(APP_PATH)
    # 匹配带 trailing slash 路径，避免误伤其他同名进程
    match = f"{APP_PATH}/"
    script = (
        f"sleep 3; "
        f"pkill -TERM -f {shlex.quote(match)} 2>/dev/null; "
        f"sleep 1; "
        f"pkill -KILL -f {shlex.quote(match)} 2>/dev/null; "
        f"sleep 0.5; "
        f"open {quoted_app}"
    )
    subprocess.Popen(
        ["sh", "-c", script],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )

    def _die():
        os._exit(0)

    asyncio.get_event_loop().call_later(2.0, _die)


async def handle_command(
    cmd: str,
    args: str,
    user_id: str,
    chat_id: str,
    store: SessionStore,
    bot=None,
) -> Optional[str]:
    """处理命令，返回回复文本。返回 None 表示不是 bot 命令，应转发给 Claude。"""

    if cmd not in BOT_COMMANDS:
        return None  # 不认识的 /xxx → 转发给 Claude（如 /commit 等 skill）

    if cmd == "ws":
        cmd = "workspace"

    if cmd in ("help", "h"):
        return HELP_TEXT

    elif cmd in ("new", "clear"):
        # /new [mode] — 开新 session，可选指定模式
        new_mode = None
        if args:
            alias = MODE_ALIASES.get(args.lower(), args)
            if alias in VALID_MODES:
                new_mode = alias

        old_title = await store.new_session(user_id, chat_id)
        if new_mode:
            await store.set_permission_mode(user_id, chat_id, new_mode)

        cur = await store.get_current(user_id, chat_id)
        parts = []
        if old_title:
            parts.append(f"✅ 已开始新 session。\n上个会话：「{old_title}」")
        else:
            parts.append("✅ 已开始新 session。")
        parts.append(f"Runner：**{cur.runner}**")
        parts.append(f"模型：**{cur.model}**")
        parts.append(f"当前模式：**{cur.permission_mode}**")
        return {
            "text": "\n".join(parts),
            "buttons": [
                {"text": "📋 规划", "value": {"action": "set_mode", "mode": "plan", "cid": chat_id}},
                {"text": "✏️ 接受编辑", "value": {"action": "set_mode", "mode": "acceptEdits", "cid": chat_id}},
                {"text": "🚀 全自动", "value": {"action": "set_mode", "mode": "bypassPermissions", "cid": chat_id}},
                {"text": "🔒 需确认", "value": {"action": "set_mode", "mode": "default", "cid": chat_id}},
            ],
        }

    elif cmd == "defaults":
        old_title = await store.reset_current_to_defaults(user_id, chat_id)
        cur = await store.get_current(user_id, chat_id)
        lines = ["✅ 已重置为配置默认参数，并开始新 session。"]
        if old_title:
            lines.append(f"上个会话：「{old_title}」")
        lines.extend([
            f"Runner: `{cur.runner}`",
            f"模型: `{cur.model}`",
            f"权限模式: `{cur.permission_mode}`",
            f"工作目录: `{cur.cwd}`",
        ])
        return "\n".join(lines)

    elif cmd == "resume":
        if not args:
            return await _format_session_list(user_id, chat_id, store)
        # 如果是数字序号，先在合并列表中找到对应 session_id
        try:
            idx = int(args) - 1
            all_sessions = await _build_session_list(user_id, chat_id, store)
            if 0 <= idx < len(all_sessions):
                args = all_sessions[idx]["session_id"]
            else:
                return f"❌ 序号 {int(args)} 超出范围（共 {len(all_sessions)} 条）。"
        except ValueError:
            pass  # 直接用 session ID 字符串
        session_id, old_title = await store.resume_session(user_id, chat_id, args)
        if not session_id:
            return f"❌ 未找到 session：`{args}`，用 `/resume` 查看列表。"
        # 用摘要作为会话名，没有就用 ID 前缀
        name = store.get_summary(user_id, session_id) or f"#{session_id[:8]}"
        reply = f"✅ 已恢复会话「{name}」，继续对话吧。"
        if old_title:
            reply += f"\n上个会话：「{old_title}」"
        return reply

    elif cmd == "runner":
        cur = await store.get_current(user_id, chat_id)
        if not args:
            runner = (cur.runner or "claude").lower()
            return {
                "text": f"当前 runner：**{runner}**\n当前模型：**{cur.model}**",
                "buttons": [
                    {"text": "Codex", "value": {"action": "run_cmd", "cmd": "/runner codex", "cid": chat_id}},
                    {"text": "Claude Code", "value": {"action": "run_cmd", "cmd": "/runner claude", "cid": chat_id}},
                ],
            }
        requested = args.strip().lower().replace("_", "-")
        if requested in {"claude-code", "claudecode"}:
            requested = "claude"
        if requested not in {"codex", "claude"}:
            return "❌ 未知 runner：`{}`\n可选：`codex`、`claude`（Claude Code）".format(args)
        model = _runner_default_model(bot, requested)
        await store.set_runner(user_id, chat_id, requested, model=model)
        return f"✅ 已切换 runner 为 `{requested}`，模型 `{model}`。已开始新 session。"

    elif cmd == "model":
        if not args:
            cur = await store.get_current(user_id, chat_id)
            runner = (getattr(cur, "runner", "") or getattr(getattr(bot, "profile", None), "runner", "claude")).lower()
            if runner == "codex":
                buttons = [
                    {"text": "Codex Max", "value": {"action": "run_cmd", "cmd": "/model codex-max", "cid": chat_id}},
                    {"text": "Codex", "value": {"action": "run_cmd", "cmd": "/model codex", "cid": chat_id}},
                    {"text": "GPT-5.1", "value": {"action": "run_cmd", "cmd": "/model gpt5", "cid": chat_id}},
                ]
            else:
                buttons = [
                    {"text": "📖 Fable", "value": {"action": "run_cmd", "cmd": "/model fable", "cid": chat_id}},
                    {"text": "🧠 Opus", "value": {"action": "run_cmd", "cmd": "/model opus", "cid": chat_id}},
                    {"text": "⚡ Sonnet", "value": {"action": "run_cmd", "cmd": "/model sonnet", "cid": chat_id}},
                    {"text": "🐇 Haiku", "value": {"action": "run_cmd", "cmd": "/model haiku", "cid": chat_id}},
                ]
            return {
                "text": f"当前 runner：**{runner}**\n当前模型：**{cur.model}**",
                "buttons": buttons,
            }
        model = MODEL_ALIASES.get(args.lower(), args)
        await store.set_model(user_id, chat_id, model)
        return f"✅ 已切换模型为 `{model}`。已开始新 session。"

    elif cmd == "status":
        cur = await store.get_current_raw(user_id, chat_id)
        sid = cur.get("session_id") or "（新 session）"
        runner = cur.get("runner") or getattr(getattr(bot, "profile", None), "runner", "claude")
        model = cur.get("model", "未知")
        cwd = cur.get("cwd", "~")
        workspace = cur.get("workspace") or "（未绑定）"
        started = cur.get("started_at", "")[:16].replace("T", " ")
        mode = cur.get("permission_mode") or "bypassPermissions"

        runner = str(runner).lower()
        context_line = _format_context_line(
            cur.get("session_id"),
            model,
            runner=runner,
            current_usage=cur.get("last_usage") or None,
        )
        quota_line = (
            _format_codex_rate_line(cur.get("session_id"))
            if runner == "codex" else await asyncio.to_thread(_get_quota_compact)
        )

        lines = [
            "📊 **当前 Session 状态**",
            f"Session ID: `{sid}`",
            f"Runner: `{runner}`",
            f"模型: `{model}`",
            f"权限模式: `{mode}`",
            f"工作空间: `{workspace}`",
            f"工作目录: `{cwd}`",
            f"开始时间: {started}",
        ]
        if context_line:
            lines.append(context_line)
        if quota_line:
            lines.append(quota_line)
        return "\n".join(lines)

    elif cmd == "mode":
        if not args:
            cur = await store.get_current(user_id, chat_id)
            return {
                "text": f"当前模式：**{cur.permission_mode}**\n{VALID_MODES.get(cur.permission_mode, '')}",
                "buttons": [
                    {"text": "📋 规划", "value": {"action": "set_mode", "mode": "plan", "cid": chat_id}},
                    {"text": "✏️ 接受编辑", "value": {"action": "set_mode", "mode": "acceptEdits", "cid": chat_id}},
                    {"text": "🚀 全自动", "value": {"action": "set_mode", "mode": "bypassPermissions", "cid": chat_id}},
                    {"text": "🔒 需确认", "value": {"action": "set_mode", "mode": "default", "cid": chat_id}},
                ],
            }
        mode = MODE_ALIASES.get(args.lower(), args)
        if mode not in VALID_MODES:
            return f"❌ 未知模式：`{args}`\n可选：{', '.join(f'`{m}`' for m in VALID_MODES)}"
        await store.set_permission_mode(user_id, chat_id, mode)
        return f"✅ 已切换为 **{mode}** — {VALID_MODES[mode]}"

    elif cmd == "cd":
        if not args:
            return "⚠️ 用法：`/cd [路径]`"
        path = os.path.expanduser(args)
        if not os.path.isdir(path):
            return f"❌ 路径不存在：`{path}`"
        old_name = (await store.get_current_raw(user_id, chat_id)).get("workspace", "")
        await store.set_cwd(user_id, chat_id, path)
        suffix = "，并解除原工作空间绑定" if old_name else ""
        return f"✅ 工作目录已切换为 `{path}`{suffix}"

    elif cmd == "ls":
        return await _list_directory(user_id, chat_id, store, args)

    elif cmd == "exec":
        return await _exec_shell(user_id, chat_id, store, args)

    elif cmd == "workspace":
        return await _handle_workspace_command(args, user_id, chat_id, store)

    elif cmd == "skills":
        return _list_skills(chat_id)

    elif cmd == "mcp":
        return _list_mcp()

    elif cmd == "usage":
        cur = await store.get_current_raw(user_id, chat_id)
        runner = str(cur.get("runner") or getattr(getattr(bot, "profile", None), "runner", "claude")).lower()
        if runner == "codex":
            model = cur.get("model", "gpt-5.5")
            lines = ["📈 **Codex 用量**"]
            lines.append(_format_context_line(
                cur.get("session_id"),
                model,
                runner="codex",
                current_usage=cur.get("last_usage") or None,
            ))
            rate_line = _format_codex_rate_line(cur.get("session_id"))
            if rate_line:
                lines.append(rate_line)
            lines.append(f"Runner: `codex`")
            lines.append(f"模型: `{model}`")
            return "\n".join(lines)
        return _get_usage()

    elif cmd == "accounts":
        return await asyncio.to_thread(_get_accounts)

    elif cmd == "stop":
        return "⏹ /stop 命令在消息队列外处理，如果看到这条说明当前没有运行中的任务。"

    elif cmd == "restart":
        if not os.path.isdir(APP_PATH):
            return f"❌ 未找到 {APP_PATH}，先 `deploy/cc-lark install`"
        _trigger_restart()
        return "♻️ 服务重启中 — wrapper 退出 ~2s 后由 `open .app` 拉起，全部就绪约 5s。"

    elif cmd == "group":
        return await _handle_group_command(args, bot)

    else:
        return None  # fallback: 转发给 Claude
