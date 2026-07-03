"""
多 Claude Max 账户智能切换 + 内置账户管理（save / use / list / remove）。

工作原理
========

`save_current_account(name)` 把当前 keychain 凭证 + ~/.claude.json 的身份
（`oauthAccount` + `userID`）一起 stash 到 `~/.claude/accounts/<name>.json`，
里面明文有 accessToken。**我们直接用每个账户的 token 各发一次 1-token haiku
请求**，从响应 header 读取那个账户的
`anthropic-ratelimit-unified-5h-utilization` / `-7d-utilization` / `-5h-reset` /
`-7d-reset`，就拿到了"全景"——不用真切到 keychain 就能比较所有账户。

切换 (`use_account`) 必须同时换 keychain + ~/.claude.json.oauthAccount，否则
Claude CLI 启动会发现 token 关联账户 ≠ oauthAccount.accountUuid → 触发 re-login
把 keychain 写回旧账户（这是 2026-05-25 抓到的根因）。本模块取代了老的
`~/bin/claude-switch` bash 脚本，全 Python，CLI 用法见文件末尾 `__main__`。

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

    - 冷却 30 min（默认，可配）：上次切换后这段时间内不切第二次（仅约束"优化切换"，
      当前账户被硬筛淘汰时的紧急切换绕过冷却）
    - is_current 加 0.05 bonus，临界差距下不抖动
    （注：不再因"有正在跑的 claude 子进程"而推迟——Claude 支持 keychain 热切换，
     正在跑的子进程读的是 spawn 时的 token，不受影响，新 spawn 自动用新账户。）

接口::

    AccountSwitcher(send_fn, ...)
      .probe_all() -> dict[name, Account]
      .decide(accounts) -> Optional[str]   # 目标账户名 / None
      .maybe_switch() -> Optional[str]     # 完整流程：探测 + 决策 + 切换

state 持久化到 ~/.feishu-claude/account_switcher_state.json（仅冷却时间戳）。
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

# ── 调参 ──────────────────────────────────────────────────────────

ACCOUNTS_DIR = os.path.expanduser("~/.claude/accounts")
STATE_DIR = os.path.expanduser("~/.feishu-claude")
STATE_FILE = os.path.join(STATE_DIR, "account_switcher_state.json")

# Claude CLI 的"当前身份"快照，跟 keychain token 是两份独立 state。
# 切账户必须同步这俩，否则 CLI 启动会比对 oauthAccount.accountUuid vs token 关联的
# 真实账户，发现 mismatch 触发 re-login 把 keychain 写回旧账户——
# 表现为「切到 reg 之后又被打回 via」。
IDENTITY_PATH = os.path.expanduser("~/.claude.json")
# 需要跟着 keychain 一起搬的 ~/.claude.json top-level 字段
_IDENTITY_KEYS = ("oauthAccount", "userID")
# saved account 文件的 schema_version：v2 多了 _meta.identity，v1 = 纯 OAuth blob
_SCHEMA_VERSION = 2
# keychain 真正认的 top-level keys（写 keychain 前用 _strip_meta 过滤掉 _meta）
_KEYCHAIN_TOPLEVEL = ("claudeAiOauth", "mcpOAuth")

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
    """读 ~/.claude/accounts/<name>.json 全 dict（含 _meta）。"""
    path = os.path.join(ACCOUNTS_DIR, f"{name}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None
    except json.JSONDecodeError:
        return None


def _strip_meta(blob: dict) -> dict:
    """剥掉 _meta，只保留 keychain 真正认的 top-level keys。
    saved file 里 _meta.identity 是给 ~/.claude.json 用的，不应该塞进 keychain。"""
    return {k: v for k, v in blob.items() if k in _KEYCHAIN_TOPLEVEL}


def _account_identity(blob: dict) -> Optional[dict]:
    """返回 saved file 里的 identity dict（含 oauthAccount / userID），无则 None。"""
    meta = blob.get("_meta") or {}
    ident = meta.get("identity") or {}
    if not ident:
        return None
    return ident


def _identity_uuid(blob_or_ident: Optional[dict]) -> str:
    """取 oauthAccount.accountUuid。入参可以是整个 saved blob（identity 在 _meta 下）
    或已经抠出来的 identity dict。取不到返回 ""。"""
    if not blob_or_ident:
        return ""
    ident = blob_or_ident
    if "_meta" in blob_or_ident:  # 整个 saved blob
        ident = _account_identity(blob_or_ident) or {}
    oa = (ident or {}).get("oauthAccount") or {}
    return oa.get("accountUuid") or ""


def _read_identity() -> Optional[dict]:
    """从 ~/.claude.json 抠出 Claude CLI 关心的身份字段。"""
    try:
        with open(IDENTITY_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    out = {k: d.get(k) for k in _IDENTITY_KEYS if k in d}
    return out or None


def _patch_identity(ident: Optional[dict]) -> tuple[bool, str]:
    """把 ident (含 oauthAccount + userID) atomic 写回 ~/.claude.json。

    `~/.claude.json` 是 200KB+ 的 settings/cache 大文件，绝大部分字段必须保留——
    只覆盖 _IDENTITY_KEYS 里的几个字段。
    """
    if not ident:
        return True, "identity empty (no-op)"
    try:
        with open(IDENTITY_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"read {IDENTITY_PATH}: {e}"
    changed = False
    for k in _IDENTITY_KEYS:
        if k in ident and d.get(k) != ident[k]:
            d[k] = ident[k]
            changed = True
    if not changed:
        return True, "identity already in sync"
    try:
        tmp = IDENTITY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        os.replace(tmp, IDENTITY_PATH)
        return True, "identity patched"
    except OSError as e:
        return False, f"write {IDENTITY_PATH}: {e}"


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
    """读 macOS keychain 里当前 active 的凭证 blob 字符串。

    ⚠️ 必须先按 `-a <用户名>` 精确取：新版 Claude CLI 把凭证写在
    acct=<macOS 用户名> 的条目上；keychain 里可能还残留同 service 名但
    acct 不同的历史死条目（实测有 acct="unknown" 的 32 天过期尸体），
    不带 `-a` 时 `security` 返回任意一条，长期命中死条目 → /usage 永远 401。
    带 `-a` 找不到时才退回旧的无 `-a` 行为（兼容老 CLI 布局）。
    """
    user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
    for extra in (["-a", user], []):
        try:
            r = subprocess.run(
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials", *extra, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return decode_security_stdout(r.stdout)
        except Exception:
            return None
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
    """读 keychain → 找出它对应哪个 saved 账户名。

    匹配优先级：
      1. accessToken 指纹精确匹配——最强信号：keychain 里的活 token 就是该快照的 token。
      2. accountUuid 回退——keychain 刚被 Claude CLI 自行轮换过时，活 token 已不在任何
         快照里（指纹全 miss → 旧逻辑直接返回 None = 状态栏「未识别」）。这时改用
         ~/.claude.json 的 oauthAccount.accountUuid 跟各快照 _meta.identity 比对救场。
         多个快照命中同一 uuid（迁移/串号期）→ 优先 last_switch_to，否则取 mtime 最新。
    """
    active_blob = _read_keychain_blob()
    if not active_blob:
        return None
    names = list_account_files()
    # 1) 指纹精确匹配（健康路径，行为与旧版完全一致）
    active_fp = _token_fingerprint(active_blob)
    if active_fp:
        for name in names:
            blob = _load_account_blob(name)
            if blob and _token_fingerprint(blob) == active_fp:
                return name
    # 2) accountUuid 回退（CLI 轮换后指纹全 miss 时兜底，消除「未识别」）
    cur_uuid = _identity_uuid(_read_identity())
    if cur_uuid:
        matches = [
            name for name in names
            if _identity_uuid(_load_account_blob(name) or {}) == cur_uuid
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            last = (_load_state() or {}).get("last_switch_to")
            if last in matches:
                return last
            try:
                return max(matches, key=lambda n: os.path.getmtime(
                    os.path.join(ACCOUNTS_DIR, f"{n}.json")))
            except OSError:
                return matches[0]
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


# ── 跨进程凭证锁（与官方 Claude CLI 的 proper-lockfile 互通）──────────
#
# 官方 CLI 刷新 / 写 keychain 前会 `proper-lockfile.lock(~/.claude)`，落地成目录
# `~/.claude.lock`（靠 mkdir 原子性 + mtime 判 stale）。cc-lark 写 keychain 时用
# **同一把锁、同一路径**，就不会和正在刷新的 CLI 互相把对方的 token 轮废。
# 严格 best-effort：抢不到就照常继续（绝不阻塞 bot），只为缩小竞态窗口。
_CLAUDE_LOCK_DIR = os.path.expanduser("~/.claude") + ".lock"
# proper-lockfile 默认 stale=10s，活着的持有者每 ~5s 刷一次 mtime；取 12s 只回收真死锁。
_CLAUDE_LOCK_STALE_SEC = 12.0


@contextlib.contextmanager
def _claude_dir_lock(budget_sec: float = 2.0):
    """best-effort 抢 ~/.claude.lock。yield True=拿到锁 / False=没拿到但继续。"""
    acquired = False
    deadline = time.time() + budget_sec
    while True:
        try:
            os.mkdir(_CLAUDE_LOCK_DIR)
            acquired = True
            break
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(_CLAUDE_LOCK_DIR)
            except OSError:
                age = 0.0
            if age > _CLAUDE_LOCK_STALE_SEC:
                try:
                    os.rmdir(_CLAUDE_LOCK_DIR)
                    continue  # 回收死锁后立刻重试
                except OSError:
                    pass
            if time.time() >= deadline:
                break
            time.sleep(0.05)
        except OSError:
            break  # 父目录异常等 → 放弃锁，继续
    try:
        yield acquired
    finally:
        if acquired:
            try:
                os.rmdir(_CLAUDE_LOCK_DIR)
            except OSError:
                pass


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
                d = json.load(f)
        except Exception:
            continue
        if not d.get("claudeAiOauth", {}).get("accessToken"):
            continue
        # strip _meta（keychain 不认识，可能 confuse CLI）
        kc_blob = json.dumps(_strip_meta(d))
        ok, msg = _write_keychain_blob(kc_blob)
        if not ok:
            return ("error", msg)
        # 同步 identity 到 ~/.claude.json，避免 CLI 启动 mismatch 触发 re-login
        ident = _account_identity(d)
        if ident:
            ident_ok, ident_msg = _patch_identity(ident)
            if not ident_ok:
                # identity patch 失败不算致命——keychain 已恢复，CLI 顶多 re-login
                # 一次。返回 restored 但带上 warn 信息。
                return ("restored", f"{name} (identity patch warn: {ident_msg})")
        return ("restored", name)

    return ("no_active", None)


# ── 探测：用每个 token 各发一个 1-token 请求拿 headers ────────────


# ── OAuth refresh：用 refresh_token 续期 access_token ──────────────


_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()


def _refresh_account_inplace(name: str, *, force: bool = False,
                             sync_keychain: bool = False) -> tuple[bool, str]:
    """直接调 platform.claude.com OAuth endpoint 用 refresh_token 续期。
    成功后**原子写回** ~/.claude/accounts/<name>.json（含轮换后的新 refresh_token）。

    sync_keychain=True（仅当 name 是当前活跃账户时传）：刷新轮换 refresh_token 后，
    keychain 里那份立刻服务端作废——把新 blob 也写回 keychain，否则正在用该账户的
    Claude CLI 下次刷新就 invalid_grant。对齐官方「锁 ~/.claude → 刷 → 写 keychain」。

    Rolling refresh token：每次成功 refresh，旧 refresh_token 立刻在服务端作废，
    必须把响应里的新 refresh_token 写回去，否则下次就 invalid_grant。

    并发保护：同一 name 上锁，防止并发探测时两次 refresh 互相把对方刚换的 token
    作废。

    force=True：跳过 "本地 expiresAt 还远着就视为已 refresh" 的 fast-path。
    场景：访问 token 在本地看还没到期，但被 Anthropic 服务端单方面 revoke 了
    （e.g. 用户在另一设备 login 了同账户、被人工 revoke），probe 收到 401 需要
    强制 refresh 一次拿新 token；如果 refresh_token 也死了，让上层走重 login。
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

        # 已经被别的线程刷新过了？（force=True 时跳过这个 fast-path）
        if not force:
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

        if sync_keychain:
            # 当前活跃账户：keychain 旧 RT 已被这次轮换作废，必须同步新 blob 回 keychain。
            with _claude_dir_lock():
                ok_kc, msg_kc = _write_keychain_blob(json.dumps(_strip_meta(blob)))
            if not ok_kc:
                return True, f"ok (snapshot written; keychain sync warn: {msg_kc})"
        return True, "ok"


# 传输层抖动异常：到 api.anthropic.com 的 TLS 偶发被中途 reset
# ([SSL: UNEXPECTED_EOF_WHILE_READING] _ssl.c:1006) / 临时断连 / 超时。
# 只对这些重试；HTTPError（401/403/429 等真实应答）必须原样抛给上层处理。
_TRANSIENT_NET_EXC = (ssl.SSLError, urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError)


def urlopen_with_retry(req, *, context=None, timeout=10, retries=2, backoff=0.6):
    """对传输层闪断重试的 urlopen。

    - HTTPError（含 401/403/429）立即抛出，不重试——那是服务端真实应答。
    - 仅对 _TRANSIENT_NET_EXC（TLS reset / 断连 / 超时）重试 retries 次，线性退避。
    返回 urlopen 的响应对象（可直接用于 `with`）。
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            return urllib.request.urlopen(req, context=context, timeout=timeout)
        except urllib.error.HTTPError:
            raise  # 真实 HTTP 应答，不重试
        except _TRANSIENT_NET_EXC as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
    raise last_exc  # 理论不可达


def _probe_one(acc: Account, is_current: bool = False) -> Account:
    """同步阻塞探测单个账户。失败时填 probe_error，不抛。

    is_current=True（该账户 == 当前 keychain 活跃账户）：任何 refresh 都带
    sync_keychain，把轮换后的新 token 同步回 keychain，避免 cc-lark 在 CLI 背后
    把活账户的 token 轮废。"""
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
            ok, err = _refresh_account_inplace(acc.name, sync_keychain=is_current)
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

    def _send_probe(token: str) -> tuple[Optional[dict], Optional[int], Optional[str]]:
        """返回 (headers, http_code_if_error, err_msg_if_other)。"""
        req2 = urllib.request.Request(
            _API_URL,
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
            ctx2 = ssl.create_default_context()
            with urlopen_with_retry(req2, context=ctx2, timeout=_PROBE_TIMEOUT_SEC) as r:
                return dict(r.headers), None, None
        except urllib.error.HTTPError as e:
            return dict(e.headers or {}), e.code, None
        except Exception as e:
            return None, None, f"probe failed: {e}"

    headers, http_code, err = _send_probe(acc.access_token)
    if err is not None:
        acc.probe_error = err
        return acc
    if http_code in (401, 403):
        # access_token 还没到本地 expiresAt 但服务端已废（典型场景：用户在 Claude
        # CLI 里 /login 切了别的账户，旧 session 被 OAuth revoke）。强制 refresh
        # 一次，若 refresh_token 也死了让上层走重 login 提示。
        ok, refresh_err = _refresh_account_inplace(acc.name, force=True, sync_keychain=is_current)
        if not ok:
            acc.probe_error = f"auth {http_code} + refresh failed ({refresh_err}) — needs re-login"
            return acc
        refreshed = load_account(acc.name)
        if refreshed is None:
            acc.probe_error = f"auth {http_code}, refresh ok but reload failed"
            return acc
        acc.access_token = refreshed.access_token
        acc.expires_at_ms = refreshed.expires_at_ms
        headers, http_code, err = _send_probe(acc.access_token)
        if err is not None:
            acc.probe_error = err
            return acc
        if http_code in (401, 403):
            acc.probe_error = f"auth {http_code} even after force refresh — needs re-login"
            return acc
    # 429 等仍可能带着 rate-limit headers——继续解析（headers 已填）

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
    # 探测前先把当前账户从 keychain 回收进快照——Claude CLI 可能刚自行轮换过 token，
    # 不回收的话当前账户会拿快照里的旧 RT 去刷 → invalid_grant 400。best-effort。
    try:
        resync_current_from_keychain()
    except Exception as e:
        print(f"[switcher] probe 前 resync 异常: {e}", flush=True)

    current = current_account_name()
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
        futs = {ex.submit(_probe_one, a, a.name == current): a for a in accounts}
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


# ── 内置账户管理：save / use / list / remove（替换老的 ~/bin/claude-switch）──


_VALID_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9._-]+$")


def _validate_name(name: str) -> Optional[str]:
    if not name:
        return "name required"
    if not _VALID_NAME_RE.match(name):
        return "name must match [A-Za-z0-9._-]+"
    return None


def save_current_account(name: str, *, overwrite: bool = True,
                         guard_identity: bool = True) -> tuple[bool, str]:
    """把当前 keychain 凭证 + ~/.claude.json identity 一起 stash 到
    ~/.claude/accounts/<name>.json（schema v2）。

    guard_identity=True（默认）：若目标档已绑定某 accountUuid，而当前 keychain 是
    另一个账号，拒绝覆盖以防串号（历史上 `spx` 档被误存成 boss 就是这么来的）。
    手动确要改绑同名档时传 guard_identity=False。

    返回 (ok, message_or_summary)。
    """
    err = _validate_name(name)
    if err:
        return False, err
    blob_str = _read_keychain_blob()
    if not blob_str:
        return False, "keychain has no Claude credentials (run `claude /login` first)"
    try:
        kc = json.loads(blob_str)
    except json.JSONDecodeError as e:
        return False, f"keychain blob malformed: {e}"
    if not kc.get("claudeAiOauth", {}).get("accessToken"):
        return False, "keychain blob has no claudeAiOauth.accessToken"

    target = os.path.join(ACCOUNTS_DIR, f"{name}.json")
    if os.path.exists(target) and not overwrite:
        return False, f"{name} already saved (pass overwrite=True to replace)"

    # 身份守卫：目标档已绑 A 账号、当前 keychain 是 B 账号 → 拒绝（防串号）。
    if guard_identity and os.path.exists(target):
        existing_uuid = _identity_uuid(_load_account_blob(name) or {})
        incoming_uuid = _identity_uuid(_read_identity())
        if existing_uuid and incoming_uuid and existing_uuid != incoming_uuid:
            return False, (
                f"refuse: 档 '{name}' 已绑账号 {existing_uuid[:8]}…，"
                f"当前 keychain 是另一个账号 {incoming_uuid[:8]}… — "
                f"不覆盖以防串号（确要改绑传 guard_identity=False / CLI 加 --force）"
            )

    payload = {k: v for k, v in kc.items() if k in _KEYCHAIN_TOPLEVEL}
    ident = _read_identity()
    payload["_meta"] = {
        "schema_version": _SCHEMA_VERSION,
        "saved_at": int(time.time()),
        "identity": ident or None,
    }

    try:
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        os.chmod(ACCOUNTS_DIR, 0o700)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except OSError as e:
        return False, f"write {target}: {e}"

    oa = (ident or {}).get("oauthAccount") or {}
    summary_parts = []
    sub = kc["claudeAiOauth"].get("subscriptionType")
    tier = kc["claudeAiOauth"].get("rateLimitTier")
    if sub:
        summary_parts.append(sub)
    if tier:
        summary_parts.append(tier)
    if oa.get("emailAddress"):
        summary_parts.append(oa["emailAddress"])
    if not ident:
        summary_parts.append("⚠️ identity missing")
    return True, f"saved {name}: {' / '.join(summary_parts) or '(no metadata)'}"


def resync_current_from_keychain() -> tuple[str, Optional[str]]:
    """把 keychain 当前活 token + ~/.claude.json identity 回写进它对应的 saved 快照。

    目的：捕获 Claude CLI 在 spawn 期间自行轮换的 token，使快照始终镜像 keychain。
    不做这步，快照会与 keychain 脱节 → `current_account_name()` 指纹对不上（状态栏
    「未识别」）+ 下次拿快照里的旧 refresh_token 去刷 → invalid_grant 400。

    幂等：指纹已一致就不写盘。受身份守卫保护，绝不串号。best-effort，绝不抛。

    返回 (status, name)：
        ("noop", name|None)   无需回收 / 没有可识别的当前账户
        ("resynced", name)    已回收
        ("skip", reason)      守卫拦下 / save 失败（带原因）
        ("error", msg)        异常
    """
    try:
        name = current_account_name()
    except Exception as e:
        return ("error", f"current lookup: {e}")
    if not name:
        return ("noop", None)
    try:
        kc = _read_keychain_blob()
        snap = _load_account_blob(name)
        if kc and snap and _token_fingerprint(kc) == _token_fingerprint(snap):
            return ("noop", name)  # 已同步
    except Exception:
        pass
    ok, msg = save_current_account(name, overwrite=True, guard_identity=True)
    if not ok:
        return ("skip", msg)
    return ("resynced", name)


def use_account(name: str) -> tuple[bool, str]:
    """切换到 saved 账户。同步写 keychain + ~/.claude.json identity。

    取代老 `claude-switch use <name>`——根因在 identity 也得跟着切，否则 Claude CLI
    启动时 oauthAccount.accountUuid != token 关联账户 → 触发 re-login → 回滚 keychain。
    """
    err = _validate_name(name)
    if err:
        return False, err
    blob = _load_account_blob(name)
    if not blob:
        return False, f"no saved account '{name}' (use `account_switcher.py list`)"
    if not blob.get("claudeAiOauth", {}).get("accessToken"):
        return False, f"{name} has no claudeAiOauth.accessToken"

    # 1) 写 keychain（strip _meta）——进 ~/.claude.lock，避免和正在刷新的 CLI 抢写
    kc_blob = json.dumps(_strip_meta(blob))
    with _claude_dir_lock():
        ok, msg = _write_keychain_blob(kc_blob)
    if not ok:
        return False, f"keychain write failed: {msg}"

    # 2) 同步 identity 到 ~/.claude.json
    ident = _account_identity(blob)
    ident_warn = ""
    if ident:
        ident_ok, ident_msg = _patch_identity(ident)
        if not ident_ok:
            ident_warn = f" (identity patch warn: {ident_msg})"
    else:
        ident_warn = " (⚠️ identity missing — CLI may force re-login; save again from this account to fix)"

    sub = blob["claudeAiOauth"].get("subscriptionType") or ""
    tier = blob["claudeAiOauth"].get("rateLimitTier") or ""
    return True, f"switched to {name} ({sub}/{tier}){ident_warn}"


def remove_account(name: str) -> tuple[bool, str]:
    err = _validate_name(name)
    if err:
        return False, err
    target = os.path.join(ACCOUNTS_DIR, f"{name}.json")
    if not os.path.exists(target):
        return False, f"no saved account '{name}'"
    try:
        os.remove(target)
    except OSError as e:
        return False, f"remove {target}: {e}"
    return True, f"removed {name}"


def list_accounts_summary() -> list[dict]:
    """返回每个账户的简单摘要，用于 CLI list / 调试。"""
    out = []
    active = current_account_name()
    for n in list_account_files():
        blob = _load_account_blob(n) or {}
        oa = (_account_identity(blob) or {}).get("oauthAccount") or {}
        kc = blob.get("claudeAiOauth", {})
        out.append({
            "name": n,
            "active": n == active,
            "subscription": kc.get("subscriptionType") or "",
            "tier": kc.get("rateLimitTier") or "",
            "email": oa.get("emailAddress") or "",
            "has_identity": bool(_account_identity(blob)),
        })
    return out


def auto_stash_identity_for_current() -> tuple[str, Optional[str]]:
    """启动期补全：当前 keychain 指向哪个 saved 账户，如果它没存 identity，
    把当前 ~/.claude.json 的 identity 抠出来补进去。
    用户手动 login 切到 reg 干了一阵子，下次 cc-lark 启动就能自动把当时的
    reg identity stash 回 reg.json，不需要用户手动 save。

    返回 (status, name)：
        ("noop", None)        没匹配 / 已有 identity
        ("stashed", name)     补全成功
        ("error", msg)        异常
    """
    name = current_account_name()
    if not name:
        return ("noop", None)
    blob = _load_account_blob(name)
    if not blob:
        return ("noop", None)
    if _account_identity(blob):
        return ("noop", None)
    ident = _read_identity()
    if not ident or not (ident.get("oauthAccount") or {}).get("accountUuid"):
        return ("noop", None)
    # 写回（保留 keychain blob 原样 + 加 _meta.identity）
    payload = {k: v for k, v in blob.items() if k in _KEYCHAIN_TOPLEVEL}
    payload["_meta"] = {
        "schema_version": _SCHEMA_VERSION,
        "saved_at": int(time.time()),
        "identity": ident,
    }
    path = os.path.join(ACCOUNTS_DIR, f"{name}.json")
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        return ("error", f"write {path}: {e}")
    return ("stashed", name)


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


# ── 模块级默认 switcher + spawn 前按需切换 ─────────────────────────
#
# 背景：后台 quota_watcher 每 10min 才跑一次切换判定，对"当前账户刚烧穿、下一条
# 消息就想立刻切到好账户"不够跟手。这里暴露一个 spawn claude 前调用的入口——
# 让切换"按需触发"而不是等下一拍轮询。
#
# 关键约束：maybe_switch() 内部会 probe_all（给每个账户发探测请求），不能每条消息
# 都跑、否则给 spawn 加延迟。所以加探测节流：默认 45s 内最多探测一次。紧急情况
# （429 事件 force=True）跳过节流。真正的切换决策 / 冷却 / 紧急绕过仍全在
# maybe_switch() 内部，这里只管"要不要现在去问一次"。

_DEFAULT_SWITCHER: Optional["AccountSwitcher"] = None
_SPAWN_PROBE_THROTTLE_SEC = 45
_last_spawn_probe_at = 0.0
_spawn_probe_lock = threading.Lock()


def set_default_switcher(sw: Optional["AccountSwitcher"]) -> None:
    """runtime 启动时把配置好的 AccountSwitcher 注册成模块级默认实例，
    供 spawn 路径（claude_runner.run_claude）按需触发。"""
    global _DEFAULT_SWITCHER
    _DEFAULT_SWITCHER = sw


def maybe_switch_before_spawn(*, force: bool = False) -> Optional[str]:
    """spawn claude 前按需切换。带探测节流，避免连发消息把 probe 打爆给每次
    spawn 加延迟。无注册的 default switcher 时 no-op。返回切到的新账户名 / None。

    force=True：跳过节流（例如 PTY 抓到 rate_limit_error / 429 后立即触发）。

    这是同步阻塞调用（内部 probe_all 走网络）——调用方若在 asyncio loop 里，
    应丢到 executor 跑，别堵事件循环。"""
    global _last_spawn_probe_at
    sw = _DEFAULT_SWITCHER
    if sw is None or not getattr(sw, "enabled", False):
        return None
    now = time.time()
    if not force:
        with _spawn_probe_lock:
            if (now - _last_spawn_probe_at) < _SPAWN_PROBE_THROTTLE_SEC:
                return None
            _last_spawn_probe_at = now
    else:
        with _spawn_probe_lock:
            _last_spawn_probe_at = now
    try:
        return sw.maybe_switch()
    except Exception as e:
        print(f"[switcher] maybe_switch_before_spawn 异常: {e}", flush=True)
        return None


# ── Orchestrator ─────────────────────────────────────────────────


class AccountSwitcher:
    """完整流程：探测 → 决策 → 切换 → 通报。"""

    def __init__(
        self,
        send_fn: Optional[Callable[[str], None]] = None,
        *,
        cooldown_sec: int = _DEFAULT_COOLDOWN_SEC,
        enabled: bool = True,
    ):
        self.send_fn = send_fn
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
        # 先探测 + 决策，再决定要不要受冷却 / 活跃子进程约束。
        # 关键：gate 放到 decide 之后，才能区分「紧急切换」(当前账户已不可用) 和
        # 「优化切换」(候选只是略优)。否则当前账户限流/挂掉时，冷却 + 活跃子进程
        # 这俩 gate 会把救命的切换一起挡掉——这正是「一直不换」的根因之一。
        accounts = probe_all()
        if not accounts:
            return None
        current = current_account_name()
        target = decide(accounts, current)
        if target is None or target == current:
            return None

        cur_acc = accounts.get(current) if current else None
        # 紧急：当前账户不存在 / 被硬筛淘汰（限流满、blocked、token 失效）。keychain
        # 只影响下一个新 spawn 的 claude，正在跑的子进程不受影响，没理由再等——
        # 直接绕过冷却 + 活跃子进程 gate。
        emergency = (cur_acc is None) or (not cur_acc.usable)

        state = _load_state()
        if not emergency:
            # 优化切换：只尊重冷却防抖（避免临界差距下来回抖）。
            # 不再因"有正在跑的 claude 子进程"而推迟——Claude 支持 keychain 热切换，
            # 正在跑的子进程不受影响，新 spawn 的自动用新账户。
            last_switch = float(state.get("last_switch_at") or 0)
            if (last_switch + self.cooldown_sec) - time.time() > 0:
                return None

        # 5) 切换前先把当前账户的 identity stash 回 saved file（自动维持新鲜度）
        if current:
            stash_status, stash_msg = auto_stash_identity_for_current()
            if stash_status == "error":
                print(f"[switcher] identity auto-stash 失败: {stash_msg}", flush=True)

        # 6) 执行切换（内置 use_account：keychain + ~/.claude.json identity 一起切）
        cur_acc = accounts.get(current) if current else None
        tgt_acc = accounts[target]
        ok, msg = use_account(target)
        if not ok:
            self._notify(
                f"⚠️ 账户切换失败：{current or '(unknown)'} → {target}\n  原因：{msg}"
            )
            return None

        state["last_switch_at"] = time.time()
        state["last_switch_from"] = current or ""
        state["last_switch_to"] = target
        _save_state(state)

        # 7) 通报
        reason_lines = self._switch_reason_lines(cur_acc, tgt_acc)
        if "identity missing" in msg:
            reason_lines.append(f"  ⚠️ {msg.split('(', 1)[1].rstrip(')')}")
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


# ── CLI 入口（取代 ~/bin/claude-switch）──────────────────────────


def _cli_main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="claude-switch",
        description="Manage Claude Max accounts (keychain + ~/.claude.json identity).",
    )
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("save", help="Save current logged-in account as <name>")
    sp.add_argument("name")
    sp.add_argument("--no-overwrite", action="store_true")
    sp.add_argument("--force", action="store_true",
                    help="bypass identity guard (allow re-binding this slot to a different account)")

    sub.add_parser("resync", help="Re-capture current keychain token into its matching saved slot")

    su = sub.add_parser("use", help="Activate saved account <name>")
    su.add_argument("name")
    su.add_parser = sub.add_parser  # appease linters

    sub.add_parser("list", help="List saved accounts (* = active)")
    sub.add_parser("current", help="Show currently active saved account")
    sub.add_parser("path", help="Print accounts storage directory")

    sr = sub.add_parser("rm", help="Delete a saved account file")
    sr.add_argument("name")

    args = p.parse_args(argv)

    if args.cmd in (None, "help"):
        p.print_help()
        return 0

    if args.cmd == "save":
        ok, msg = save_current_account(
            args.name, overwrite=not args.no_overwrite,
            guard_identity=not args.force,
        )
        print(msg)
        return 0 if ok else 1

    if args.cmd == "resync":
        status, name = resync_current_from_keychain()
        print(f"{status}: {name}")
        return 0 if status in ("noop", "resynced") else 1

    if args.cmd == "use":
        ok, msg = use_account(args.name)
        print(msg)
        if ok:
            print("→ restart any running Claude Code session to pick up the new token.")
        return 0 if ok else 1

    if args.cmd == "list":
        rows = list_accounts_summary()
        if not rows:
            print("(no saved accounts — use `save <name>` to add one)")
            return 0
        for r in rows:
            mark = "*" if r["active"] else " "
            parts = [r["subscription"], r["tier"], r["email"]]
            tail = " / ".join(p_ for p_ in parts if p_)
            warn = "" if r["has_identity"] else "  ⚠️ no identity (save again when this is the active account)"
            print(f"{mark} {r['name']:<20} {tail}{warn}")
        return 0

    if args.cmd == "current":
        cur = current_account_name()
        if not cur:
            print("(no active credentials / unsaved account)")
            return 1
        print(cur)
        return 0

    if args.cmd == "path":
        print(ACCOUNTS_DIR)
        return 0

    if args.cmd == "rm":
        ok, msg = remove_account(args.name)
        print(msg)
        return 0 if ok else 1

    p.print_help()
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli_main(sys.argv[1:]))

