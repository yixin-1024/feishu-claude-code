"""
多 Claude Max 账户智能切换。

工作原理
========

`claude-switch`（用户本机的 bash 脚本）已经把每个账户的 OAuth 凭证存到
`~/.claude/accounts/<name>.json`，里面明文有 accessToken。**我们直接用每个
账户的 token 各发一次 1-token haiku 请求**，从响应 header 读取那个账户的
`anthropic-ratelimit-unified-5h-utilization` / `-7d-utilization` / `-5h-reset` /
`-7d-reset`，就拿到了"全景"——不用真切到 keychain 就能比较所有账户。

打分（越高越优先）::

    h7 = 1 - u7d                       # 7d headroom (主权重，7d 更稀缺)
    h5 = 1 - u5h                       # 5h headroom
    若 5h reset < 30 min: h5 = 1.0     # "快重置不心疼"——再用一会儿就 reset
    score = 0.65 * h7 + 0.30 * h5 + 0.05 * is_current   # is_current 防抖

硬筛（直接淘汰）::

    u7d >= 0.98                                            → unusable
    u5h >= 0.98 AND (r5h - now) > 5 min                    → unusable
    s5h == "blocked" 或 s7d == "blocked"                   → unusable
    expiresAt - now < 60s                                  → stale (跳过探测)
    探测请求 401/403                                       → unusable (token 失效)

切换触发（任一即切）::

    1. 当前 active 账户被硬筛淘汰
    2. score(best) - score(current) >= 0.15 AND u5h_current > 0.70
    3. Anthropic 给当前账户返回 blocked status

防抖::

    - 冷却 30 min（默认，可配）：上次切换后这段时间内不切第二次
    - 正在跑 claude 子进程时推迟（has_active_children_fn）
    - is_current 加 0.05 bonus，临界差距下不抖动

接口::

    AccountSwitcher(send_fn, has_active_children_fn, ...)
      .probe_all() -> dict[name, Account]
      .decide(accounts) -> Optional[str]   # 目标账户名 / None
      .maybe_switch() -> Optional[str]     # 完整流程：探测 + 决策 + 切换

state 持久化到 ~/.feishu-claude/account_switcher_state.json（仅冷却时间戳）。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

# ── 调参 ──────────────────────────────────────────────────────────

ACCOUNTS_DIR = os.path.expanduser("~/.claude/accounts")
STATE_DIR = os.path.expanduser("~/.feishu-claude")
STATE_FILE = os.path.join(STATE_DIR, "account_switcher_state.json")

# 探测 API endpoint
_API_URL = "https://api.anthropic.com/v1/messages"
_PROBE_MODEL = "claude-haiku-4-5-20251001"
_PROBE_TIMEOUT_SEC = 10

# OAuth refresh endpoint（从 claude.exe 二进制扒出来，client_id 通用）
_OAUTH_REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_OAUTH_REFRESH_TIMEOUT_SEC = 15
# Cloudflare 风控会拦未知客户端——保留这两个 header 让请求看起来像 CLI
_OAUTH_UA = "claude-cli/2.1.150 (external, cli)"
_OAUTH_BETA = "oauth-2025-04-20"

# 打分权重
_W_7D = 0.65
_W_5H = 0.30
_W_CURRENT_BONUS = 0.05

# 5h 视为"满 headroom"的 reset 临近阈值
_RESET_BONUS_WINDOW_SEC = 30 * 60

# 硬筛门限
_HARD_LIMIT_UTIL = 0.98
_SHORT_RESET_GRACE_SEC = 5 * 60

# 切换触发阈值
_SCORE_GAP_THRESHOLD = 0.15
_TIGHT_5H_THRESHOLD = 0.70

# 冷却（避免抖动）
_DEFAULT_COOLDOWN_SEC = 30 * 60

# Token 即将过期不再用于探测
_TOKEN_FRESH_GRACE_SEC = 60


# ── 数据结构 ──────────────────────────────────────────────────────


@dataclass
class Account:
    name: str
    access_token: str = ""
    expires_at_ms: int = 0  # ~/.claude/accounts/<name>.json 里的 expiresAt
    subscription: str = ""  # e.g. "team" / "max"
    tier: str = ""          # e.g. "default_claude_max_5x"
    # 探测结果
    u5h: Optional[float] = None
    u7d: Optional[float] = None
    r5h: Optional[int] = None
    r7d: Optional[int] = None
    s5h: str = "unknown"
    s7d: str = "unknown"
    probe_error: Optional[str] = None
    probed_at: float = 0.0

    # decide 阶段填的衍生字段
    is_current: bool = False
    score: float = 0.0
    usable: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        u5 = f"{self.u5h*100:.0f}%" if self.u5h is not None else "?"
        u7 = f"{self.u7d*100:.0f}%" if self.u7d is not None else "?"
        return f"{self.name}(5h {u5}/7d {u7})"


# ── 账户文件 / keychain 工具 ──────────────────────────────────────


def list_account_files() -> list[str]:
    """列出 ~/.claude/accounts/*.json，返回账户名（不含 .json）。"""
    if not os.path.isdir(ACCOUNTS_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(ACCOUNTS_DIR)):
        if fn.endswith(".json"):
            out.append(fn[:-5])
    return out


def _load_account_blob(name: str) -> Optional[dict]:
    path = os.path.join(ACCOUNTS_DIR, f"{name}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None
    except json.JSONDecodeError:
        return None


def load_account(name: str) -> Optional[Account]:
    blob = _load_account_blob(name)
    if not blob:
        return None
    oauth = blob.get("claudeAiOauth", {})
    tok = oauth.get("accessToken", "")
    if not tok:
        return None
    return Account(
        name=name,
        access_token=tok,
        expires_at_ms=int(oauth.get("expiresAt") or 0),
        subscription=oauth.get("subscriptionType", "") or "",
        tier=oauth.get("rateLimitTier", "") or "",
    )


def decode_security_stdout(raw: str) -> str:
    """`security find-generic-password -w` 在 blob 含非可打印字符时会把整段
    输出 hex 化（无 0x 前缀），否则原样输出。识别 hex-only + 偶数长度则反解，
    其它情况原样返回。

    合法 JSON 凭证一定含 `{` `"` `:` 等非 hex 字符，所以该判定不会误伤。
    """
    s = raw.strip()
    if s and len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s):
        try:
            return bytes.fromhex(s).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return s
    return s


def _read_keychain_blob() -> Optional[str]:
    """读 macOS keychain 里当前 active 的凭证 blob 字符串。"""
    try:
        r = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        return decode_security_stdout(r.stdout)
    except Exception:
        return None


def _token_fingerprint(blob_or_dict) -> str:
    """accessToken 前 24 位作为指纹——足够区分账户。"""
    try:
        if isinstance(blob_or_dict, str):
            d = json.loads(blob_or_dict)
        else:
            d = blob_or_dict
        t = d.get("claudeAiOauth", {}).get("accessToken", "")
        return t[:24]
    except Exception:
        return ""


def current_account_name() -> Optional[str]:
    """读 keychain → 跟 ~/.claude/accounts/*.json 比对，返回当前账户名。"""
    active_blob = _read_keychain_blob()
    if not active_blob:
        return None
    active_fp = _token_fingerprint(active_blob)
    if not active_fp:
        return None
    for name in list_account_files():
        blob = _load_account_blob(name)
        if blob and _token_fingerprint(blob) == active_fp:
            return name
    return None


def _write_keychain_blob(blob: str) -> tuple[bool, str]:
    """用 `security add-generic-password -U` 覆写 keychain 凭证项。返回 (ok, msg)。"""
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-U",
             "-s", "Claude Code-credentials",
             "-a", os.environ.get("USER") or os.path.basename(os.path.expanduser("~")),
             "-w", blob],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "security add-generic-password failed").strip()
        return True, ""
    except Exception as e:
        return False, f"exec failed: {e}"


def ensure_keychain_intact() -> tuple[str, Optional[str]]:
    """检测 keychain 凭证 blob 是否含完整的 `claudeAiOauth.accessToken`，缺失则
    从 saved 账户文件自动恢复。

    背景：cc-lark `/restart` 周期里观察到 keychain blob 被覆写为只剩
    `{"mcpOAuth": ...}`——`claudeAiOauth` top-level 整个写丢，导致 `/usage`
    `'claudeAiOauth'` KeyError、`current_account_name()` 返回 None。怀疑是
    Claude CLI 或 MCP 子系统在自己启动时 `add-generic-password -U` 用残缺
    内存态覆盖，但根因在外部进程不可控，本函数是自愈兜底。

    恢复优先级：
    1. `state.last_switch_to`（最近一次主动切换的目标账户）
    2. `~/.claude/accounts/*.json` 里 mtime 最新的（人工 `claude-switch save` 也算）

    返回 (status, name)：
        ("ok",        None)  keychain 完整，no-op
        ("restored",  name)  blob 缺失，已从 saved 文件写回
        ("no_active", None)  blob 缺失且没有可用 saved 文件
        ("error",     msg)   写回过程异常
    """
    blob = _read_keychain_blob()
    if blob:
        try:
            d = json.loads(blob)
            if d.get("claudeAiOauth", {}).get("accessToken"):
                return ("ok", None)
        except Exception:
            pass  # blob 解析失败也走恢复路径

    # 收集候选 saved 文件，按优先级排序
    candidates: list[str] = []
    try:
        last = _load_state().get("last_switch_to")
    except Exception:
        last = None
    available = list_account_files()
    if last and last in available:
        candidates.append(last)
    try:
        others = sorted(
            (n for n in available if n != last),
            key=lambda n: os.path.getmtime(os.path.join(ACCOUNTS_DIR, f"{n}.json")),
            reverse=True,
        )
        candidates.extend(others)
    except OSError:
        pass

    for name in candidates:
        path = os.path.join(ACCOUNTS_DIR, f"{name}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            d = json.loads(raw)
        except Exception:
            continue
        if not d.get("claudeAiOauth", {}).get("accessToken"):
            continue
        ok, msg = _write_keychain_blob(raw)
        if ok:
            return ("restored", name)
        return ("error", msg)

    return ("no_active", None)


# ── 探测：用每个 token 各发一个 1-token 请求拿 headers ────────────


# ── OAuth refresh：用 refresh_token 续期 access_token ──────────────


_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()


def _refresh_account_inplace(name: str) -> tuple[bool, str]:
    """直接调 platform.claude.com OAuth endpoint 用 refresh_token 续期。
    成功后**原子写回** ~/.claude/accounts/<name>.json（含轮换后的新 refresh_token）。

    Rolling refresh token：每次成功 refresh，旧 refresh_token 立刻在服务端作废，
    必须把响应里的新 refresh_token 写回去，否则下次就 invalid_grant。

    并发保护：同一 name 上锁，防止并发探测时两次 refresh 互相把对方刚换的 token
    作废。
    """
    import urllib.request
    import urllib.error

    with _REFRESH_LOCKS_GUARD:
        lock = _REFRESH_LOCKS.setdefault(name, threading.Lock())

    with lock:
        # 锁内重新加载——可能上一个持锁者已经刷过了
        blob = _load_account_blob(name)
        if not blob:
            return False, f"account file not found: {name}"
        oauth = blob.get("claudeAiOauth") or {}
        old_rt = oauth.get("refreshToken")
        if not old_rt:
            return False, "no refresh_token in account file"

        # 已经被别的线程刷新过了？
        exp = int(oauth.get("expiresAt") or 0)
        if exp and exp / 1000 - time.time() > _TOKEN_FRESH_GRACE_SEC:
            return True, "already refreshed by concurrent probe"

        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": old_rt,
            "client_id": _OAUTH_CLIENT_ID,
        }).encode()
        req = urllib.request.Request(
            _OAUTH_REFRESH_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _OAUTH_UA,
                "anthropic-beta": _OAUTH_BETA,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_OAUTH_REFRESH_TIMEOUT_SEC) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            # 400 invalid_grant → refresh_token 服务端已废，要重 OAuth login
            detail = ""
            try:
                detail = json.loads(e.read()).get("error_description") or ""
            except Exception:
                pass
            return False, f"HTTP {e.code}{(': ' + detail) if detail else ''}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

        new_at = resp.get("access_token")
        new_rt = resp.get("refresh_token")
        exp_in = resp.get("expires_in")
        if not (new_at and new_rt and exp_in):
            return False, f"malformed response, keys={list(resp.keys())}"

        blob["claudeAiOauth"]["accessToken"] = new_at
        blob["claudeAiOauth"]["refreshToken"] = new_rt
        blob["claudeAiOauth"]["expiresAt"] = int((time.time() + exp_in) * 1000)
        if "scope" in resp and isinstance(resp["scope"], str):
            blob["claudeAiOauth"]["scopes"] = resp["scope"].split(" ")

        path = os.path.join(ACCOUNTS_DIR, f"{name}.json")
        tmp = path + ".new"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blob, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True, "ok"


def _probe_one(acc: Account) -> Account:
    """同步阻塞探测单个账户。失败时填 probe_error，不抛。"""
    import urllib.request
    import urllib.error
    import ssl

    acc.probed_at = time.time()

    # Token 已过期或即将过期 → 直接调 OAuth refresh endpoint 续期，写回 json，
    # 然后继续走探测。Rolling refresh token：服务端每次成功 refresh 都会轮换
    # refresh_token，必须把响应里的新值写回 ~/.claude/accounts/<name>.json，
    # 否则下次就 invalid_grant。
    if acc.expires_at_ms:
        secs_left = acc.expires_at_ms / 1000 - time.time()
        if secs_left < _TOKEN_FRESH_GRACE_SEC:
            ok, err = _refresh_account_inplace(acc.name)
            if not ok:
                acc.probe_error = f"refresh failed: {err}"
                return acc
            refreshed = load_account(acc.name)
            if refreshed is None:
                acc.probe_error = "refresh ok but reload failed"
                return acc
            acc.access_token = refreshed.access_token
            acc.expires_at_ms = refreshed.expires_at_ms

    body = json.dumps({
        "model": _PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()

    req = urllib.request.Request(
        _API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {acc.access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    headers: dict
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=_PROBE_TIMEOUT_SEC) as resp:
            headers = dict(resp.headers)
    except urllib.error.HTTPError as e:
        headers = dict(e.headers or {})
        if e.code in (401, 403):
            acc.probe_error = f"auth {e.code}"
            return acc
        # 429 等仍可能带着 rate-limit headers——继续解析
    except Exception as e:
        acc.probe_error = f"probe failed: {e}"
        return acc

    def h(key):
        return headers.get(key) or headers.get(key.lower()) or headers.get(key.replace("-", "_"))

    def _f(v):
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    def _i(v):
        try:
            return int(v) if v is not None else None
        except Exception:
            return None

    acc.u5h = _f(h("anthropic-ratelimit-unified-5h-utilization"))
    acc.u7d = _f(h("anthropic-ratelimit-unified-7d-utilization"))
    acc.r5h = _i(h("anthropic-ratelimit-unified-5h-reset"))
    acc.r7d = _i(h("anthropic-ratelimit-unified-7d-reset"))
    acc.s5h = h("anthropic-ratelimit-unified-5h-status") or "unknown"
    acc.s7d = h("anthropic-ratelimit-unified-7d-status") or "unknown"

    if acc.u5h is None and acc.u7d is None and not acc.probe_error:
        acc.probe_error = "no rate-limit headers in response"

    return acc


def probe_all(parallel: int = 4) -> dict[str, Account]:
    """并行探测所有保存的账户。返回 {name: Account}。"""
    names = list_account_files()
    accounts: list[Account] = []
    for n in names:
        a = load_account(n)
        if a is not None:
            accounts.append(a)

    if not accounts:
        return {}

    out: dict[str, Account] = {}
    workers = max(1, min(parallel, len(accounts)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="acc-probe") as ex:
        futs = {ex.submit(_probe_one, a): a for a in accounts}
        for fut in as_completed(futs):
            try:
                acc = fut.result()
            except Exception as e:
                acc = futs[fut]
                acc.probe_error = f"probe crashed: {e}"
            out[acc.name] = acc
    return out


# ── 打分 / 硬筛 ──────────────────────────────────────────────────


def evaluate(acc: Account, current_name: Optional[str], now: Optional[float] = None) -> None:
    """填充 acc.score / acc.usable / acc.reasons / acc.is_current。"""
    now = now if now is not None else time.time()
    acc.is_current = (acc.name == current_name)
    acc.reasons = []

    # 探测失败 → 不可用
    if acc.probe_error or acc.u5h is None or acc.u7d is None:
        acc.usable = False
        acc.score = 0.0
        if acc.probe_error:
            acc.reasons.append(acc.probe_error)
        else:
            acc.reasons.append("missing utilization headers")
        return

    # 硬筛
    if acc.u7d >= _HARD_LIMIT_UTIL:
        acc.usable = False
        acc.reasons.append(f"7d {acc.u7d*100:.0f}% ≥ {_HARD_LIMIT_UTIL*100:.0f}% hard limit")
    secs_to_5h_reset = max(0, (acc.r5h or 0) - now) if acc.r5h else None
    if acc.u5h >= _HARD_LIMIT_UTIL and (secs_to_5h_reset is None or secs_to_5h_reset > _SHORT_RESET_GRACE_SEC):
        acc.usable = False
        acc.reasons.append(f"5h {acc.u5h*100:.0f}% ≥ {_HARD_LIMIT_UTIL*100:.0f}% (reset > 5 min)")
    if acc.s5h == "blocked":
        acc.usable = False
        acc.reasons.append("5h status=blocked")
    if acc.s7d == "blocked":
        acc.usable = False
        acc.reasons.append("7d status=blocked")

    if acc.reasons:
        acc.usable = False
        acc.score = 0.0
        return

    acc.usable = True

    # 打分
    h7 = 1.0 - acc.u7d
    h5 = 1.0 - acc.u5h
    # 5h 快重置：把 h5 视为满
    if secs_to_5h_reset is not None and secs_to_5h_reset < _RESET_BONUS_WINDOW_SEC:
        h5 = 1.0
        acc.reasons.append(f"5h resets in {int(secs_to_5h_reset/60)}m (bonus)")

    score = _W_7D * h7 + _W_5H * h5
    if acc.is_current:
        score += _W_CURRENT_BONUS
    acc.score = score


def decide(
    accounts: dict[str, Account],
    current_name: Optional[str],
    *,
    now: Optional[float] = None,
) -> Optional[str]:
    """决策：返回目标账户名 (≠ current_name) 或 None（保持现状）。"""
    if not accounts:
        return None
    now = now if now is not None else time.time()
    for acc in accounts.values():
        evaluate(acc, current_name, now=now)

    cur = accounts.get(current_name) if current_name else None

    # 找候选：在 usable 且非 current 的里挑分数最高的
    candidates = [a for a in accounts.values() if a.usable and a.name != current_name]
    if not candidates:
        return None
    best = max(candidates, key=lambda a: a.score)

    # 当前不存在或不可用 → 必须切
    if cur is None or not cur.usable:
        return best.name

    # 候选明显更优 + 当前 5h 用得有点紧 → 切
    if best.score - cur.score >= _SCORE_GAP_THRESHOLD and (cur.u5h or 0) >= _TIGHT_5H_THRESHOLD:
        return best.name

    # blocked status 已经在 usable=False 里处理了，这里不需要重复
    return None


# ── 切换执行（claude-switch use <name>）──────────────────────────


def _run_claude_switch_use(name: str) -> tuple[bool, str]:
    """调 claude-switch use <name>。返回 (success, message)。"""
    # 走绝对路径——cc-lark 起 .app 时 PATH 可能没有 ~/bin
    candidates = [
        os.path.expanduser("~/bin/claude-switch"),
        "/usr/local/bin/claude-switch",
        "/opt/homebrew/bin/claude-switch",
        "claude-switch",  # PATH 兜底
    ]
    cmd_path = next((c for c in candidates if c == "claude-switch" or os.path.isfile(c)), None)
    if cmd_path is None:
        return False, "claude-switch not found"
    try:
        r = subprocess.run(
            [cmd_path, "use", name],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return False, f"exec failed: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "unknown error").strip()
    return True, (r.stdout or "").strip()


# ── 状态持久化（仅冷却时间戳）─────────────────────────────────────


def _load_state() -> dict:
    if not os.path.isfile(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[switcher] save state 失败: {e}", flush=True)


# ── Orchestrator ─────────────────────────────────────────────────


class AccountSwitcher:
    """完整流程：探测 → 决策 → 切换 → 通报。"""

    def __init__(
        self,
        send_fn: Optional[Callable[[str], None]] = None,
        has_active_children_fn: Optional[Callable[[], bool]] = None,
        *,
        cooldown_sec: int = _DEFAULT_COOLDOWN_SEC,
        enabled: bool = True,
    ):
        self.send_fn = send_fn
        self.has_active_children_fn = has_active_children_fn
        self.cooldown_sec = cooldown_sec
        self.enabled = enabled
        self._lock = threading.Lock()

    # ── 公共 API ──

    def probe_all(self) -> dict[str, Account]:
        return probe_all()

    def current_name(self) -> Optional[str]:
        return current_account_name()

    def render_matrix(self, accounts: dict[str, Account], current: Optional[str]) -> str:
        """渲染 /accounts 命令展示用的矩阵。"""
        if not accounts:
            return "(无保存的账户。先 `claude-switch save <name>` 把当前账户保存进来。)"
        for a in accounts.values():
            evaluate(a, current)
        lines = ["📋 **Claude Max 账户全景**", ""]
        # 按 score 降序，但 current 放最前
        ordered = sorted(
            accounts.values(),
            key=lambda a: (0 if a.name == current else 1, -a.score),
        )
        for a in ordered:
            mark = "● " if a.name == current else "  "
            if a.probe_error:
                lines.append(f"{mark}**{a.name}** — ⚠️ {a.probe_error}")
                continue
            u5 = f"{a.u5h*100:.0f}%" if a.u5h is not None else "?"
            u7 = f"{a.u7d*100:.0f}%" if a.u7d is not None else "?"
            secs_5h = max(0, (a.r5h or 0) - time.time()) if a.r5h else 0
            r5 = f"{int(secs_5h/3600)}h{int(secs_5h%3600/60)}m" if secs_5h else "?"
            status = "✅" if a.usable else "❌"
            tail = ""
            if a.tier:
                tail = f" `{a.tier}`"
            lines.append(
                f"{mark}**{a.name}**{tail} · 5h `{u5}` (重置 {r5}) · 7d `{u7}` · score `{a.score:.2f}` {status}"
            )
            if a.reasons and not a.usable:
                lines.append(f"   └ {'; '.join(a.reasons)}")
        lines.append("")
        lines.append("● 当前 active · score 越高越优先 · 自动切换由 cc-lark 后台决定")
        return "\n".join(lines)

    def maybe_switch(self) -> Optional[str]:
        """完整流程。返回切到的新账户名（成功切换时），否则 None。

        线程安全：内部 lock，并发调用只跑一次。"""
        if not self.enabled:
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._maybe_switch_locked()
        finally:
            self._lock.release()

    def _maybe_switch_locked(self) -> Optional[str]:
        # 1) 冷却
        state = _load_state()
        last_switch = float(state.get("last_switch_at") or 0)
        cooldown_left = (last_switch + self.cooldown_sec) - time.time()
        if cooldown_left > 0:
            return None

        # 2) 有正在跑的 claude 子进程 → 推迟
        if self.has_active_children_fn is not None:
            try:
                if self.has_active_children_fn():
                    return None
            except Exception as e:
                # 探针炸了不应该挡切换决策——但保守起见推迟一轮
                print(f"[switcher] has_active_children_fn raised: {e}", flush=True)
                return None

        # 3) 探测
        accounts = probe_all()
        if not accounts:
            return None
        current = current_account_name()

        # 4) 决策
        target = decide(accounts, current)
        if target is None or target == current:
            return None

        # 5) 执行切换
        cur_acc = accounts.get(current) if current else None
        tgt_acc = accounts[target]
        ok, msg = _run_claude_switch_use(target)
        if not ok:
            self._notify(
                f"⚠️ 账户切换失败：{current or '(unknown)'} → {target}\n  原因：{msg}"
            )
            return None

        state["last_switch_at"] = time.time()
        state["last_switch_from"] = current or ""
        state["last_switch_to"] = target
        _save_state(state)

        # 6) 通报
        reason_lines = self._switch_reason_lines(cur_acc, tgt_acc)
        text = "🔁 **Claude 账户已自动切换**\n" + "\n".join(reason_lines)
        self._notify(text)
        return target

    # ── 内部 ──

    def _switch_reason_lines(self, cur: Optional[Account], tgt: Account) -> list[str]:
        lines = []
        if cur is not None:
            cur_label = f"{cur.name}"
            cur_u5 = f"{cur.u5h*100:.1f}%" if cur.u5h is not None else "?"
            cur_u7 = f"{cur.u7d*100:.1f}%" if cur.u7d is not None else "?"
            lines.append(f"从 `{cur_label}` → `{tgt.name}`")
            lines.append(f"  原账户：5h {cur_u5} / 7d {cur_u7}（score {cur.score:.2f}）")
        else:
            lines.append(f"切到 `{tgt.name}`（原 active 账户未识别）")
        tgt_u5 = f"{tgt.u5h*100:.1f}%" if tgt.u5h is not None else "?"
        tgt_u7 = f"{tgt.u7d*100:.1f}%" if tgt.u7d is not None else "?"
        lines.append(f"  新账户：5h {tgt_u5} / 7d {tgt_u7}（score {tgt.score:.2f}）")
        if cur and cur.reasons:
            lines.append(f"  触发：{'; '.join(cur.reasons[:2])}")
        elif cur and cur.u5h and cur.u5h > _TIGHT_5H_THRESHOLD:
            lines.append(f"  触发：当前 5h 已用 {cur.u5h*100:.0f}% > {_TIGHT_5H_THRESHOLD*100:.0f}% 阈值")
        return lines

    def _notify(self, text: str) -> None:
        if self.send_fn is None:
            print(f"[switcher] {text}", flush=True)
            return
        try:
            self.send_fn(text)
        except Exception as e:
            print(f"[switcher] notify failed: {e}\n{text}", flush=True)
