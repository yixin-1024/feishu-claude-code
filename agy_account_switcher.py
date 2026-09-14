"""agy（Antigravity CLI）多 Google 账号快速切换：save / use / list / remove。

和 Claude 那套（`account_switcher.py`）是同一个思路的轻量版，但底层差别很大：

凭证真源
========
agy 的登录态**全部**在 macOS login keychain 的一条记录里：

    service = "gemini"   account = "antigravity"

值是 zalando/go-keyring 的编码格式 ``go-keyring-base64:<base64(JSON)>``，解出来是::

    {"token": {"access_token", "token_type", "refresh_token", "expiry"},
     "auth_method": "consumer",
     "id_token": "<Google JWT，payload 里有 email>"}

其余磁盘状态都**不是**账号态，切号不用动（2026-09-12 实测确认）：
``~/.gemini/antigravity-cli/settings.json`` 只有 model / trustedWorkspaces，
``~/.gemini/google_accounts.json`` 是 gemini-cli 的（active=null），
``cache/onboarding.json`` / ``installation_id`` 是设备级的。
所以这里不需要 Claude 那种「keychain + ~/.claude.json identity 必须一起换」的双写。

快照
====
``save`` 把解码后的 JSON 原样 stash 到 ``~/.gemini/accounts/<name>.json``（0600），
外加 ``_meta``（email / saved_at / schema_version）。``use`` 写回 keychain 前会先
把**当前** keychain 回收进它对应的快照（agy 会自己 refresh access_token，不回收
的话快照会越来越旧）。

注意
====
- agy CLI 没有 login/logout 子命令，换号只能进 TUI 走 OAuth；本模块负责的是
  「换完之后把两份凭证都留住，以后一条命令来回切」。
- 正在跑的 agy 进程持有的是 spawn 时的 token，切号不影响它；但它若在切号后
  自行 refresh 并写回 keychain，会把 keychain 覆盖回旧账号——切号尽量在没有
  agy 任务在跑的时候做，``use()`` 会读回校验并在不一致时报错。

CLI::

    python3 agy_account_switcher.py list
    python3 agy_account_switcher.py save [name] [--force]
    python3 agy_account_switcher.py use <name>
    python3 agy_account_switcher.py current
    python3 agy_account_switcher.py logout          # 清 keychain，好让 agy 重新登另一个号
    python3 agy_account_switcher.py remove <name>
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Optional

KEYCHAIN_SERVICE = "gemini"
KEYCHAIN_ACCOUNT = "antigravity"
_GO_KEYRING_PREFIX = "go-keyring-base64:"

ACCOUNTS_DIR = os.path.expanduser("~/.gemini/accounts")
_SCHEMA_VERSION = 1
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SECURITY_TIMEOUT = 8


# ── keychain 读写 ────────────────────────────────────────────────


def keychain_supported() -> bool:
    return sys.platform == "darwin"


def _read_keychain_raw() -> Optional[str]:
    """读 keychain 里 agy 当前的原始凭证串（含 go-keyring 前缀）。没有则 None。"""
    if not keychain_supported():
        return None
    try:
        r = subprocess.run(
            ["security", "find-generic-password",
             "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=_SECURITY_TIMEOUT,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def _write_keychain_raw(raw: str) -> tuple[bool, str]:
    """覆写 keychain 条目。走 `security -i` 交互模式，token 不进进程命令行。"""
    if not keychain_supported():
        return False, "agy 账号切换目前只支持 macOS keychain"
    if '"' in raw or "\n" in raw or "\\" in raw:
        return False, "credential blob 含引号/换行，拒绝写入（格式异常）"
    script = (
        f'add-generic-password -U -s {KEYCHAIN_SERVICE} '
        f'-a {KEYCHAIN_ACCOUNT} -w "{raw}"\n'
    )
    try:
        r = subprocess.run(
            ["security", "-i"], input=script,
            capture_output=True, text=True, timeout=_SECURITY_TIMEOUT,
        )
    except Exception as e:
        return False, f"exec security: {e}"
    err = (r.stderr or "").strip()
    # security -i 即使子命令失败也可能 rc=0，用 stderr 兜底判定
    if r.returncode != 0 or ("error" in err.lower() and "password" in err.lower()):
        return False, err or (r.stdout or "security add-generic-password failed").strip()
    return True, ""


# ── blob 编解码 / 元信息 ──────────────────────────────────────────


def decode_raw(raw: str) -> Optional[dict]:
    """把 keychain 原始串解成 dict。非 go-keyring 格式时按裸 JSON 试一次。"""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith(_GO_KEYRING_PREFIX):
        s = s[len(_GO_KEYRING_PREFIX):]
        try:
            s = base64.b64decode(s).decode("utf-8")
        except Exception:
            return None
    try:
        d = json.loads(s)
    except json.JSONDecodeError:
        return None
    return d if isinstance(d, dict) else None


def encode_raw(blob: dict) -> str:
    """dict → keychain 原始串（go-keyring base64 格式，与 agy 自己写的一致）。"""
    payload = json.dumps(blob, separators=(",", ":"))
    return _GO_KEYRING_PREFIX + base64.b64encode(payload.encode("utf-8")).decode("ascii")


def _strip_meta(blob: dict) -> dict:
    return {k: v for k, v in blob.items() if k != "_meta"}


def blob_email(blob: Optional[dict]) -> str:
    """从 id_token（Google JWT）本地解出 email，不联网。取不到返回 ""。"""
    if not blob:
        return ""
    meta = blob.get("_meta") or {}
    tok = blob.get("id_token") or ""
    if not tok:
        return str(meta.get("email") or "")
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return str(meta.get("email") or "")
    return str(claims.get("email") or meta.get("email") or "")


def blob_fingerprint(blob: Optional[dict]) -> str:
    """refresh_token 前 24 位——refresh_token 长期不变，比 access_token 更适合认账户。"""
    if not blob:
        return ""
    rt = ((blob.get("token") or {}).get("refresh_token")) or ""
    return rt[:24]


def blob_expiry_ts(blob: Optional[dict]) -> Optional[float]:
    """access_token 过期时间（epoch 秒）。解析不出返回 None。"""
    if not blob:
        return None
    raw = ((blob.get("token") or {}).get("expiry")) or ""
    if not raw:
        return None
    s = str(raw).replace("Z", "+00:00")
    # Go 的 RFC3339 纳秒精度，Python 只吃 6 位小数
    s = re.sub(r"\.(\d{6})\d+", r".\1", s)
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _fmt_expiry(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    delta = ts - time.time()
    local = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    if delta <= 0:
        return f"{local}（已过期，agy 会自动续）"
    return f"{local}（{int(delta // 60)} min 后）"


# ── 快照文件 ─────────────────────────────────────────────────────


def _validate_name(name: str) -> Optional[str]:
    if not name:
        return "name required"
    if not _VALID_NAME_RE.match(name):
        return "name 只能是 [A-Za-z0-9._-]"
    return None


def _account_path(name: str) -> str:
    return os.path.join(ACCOUNTS_DIR, f"{name}.json")


def list_account_files() -> list[str]:
    try:
        return sorted(
            f[:-5] for f in os.listdir(ACCOUNTS_DIR)
            if f.endswith(".json") and not f.startswith(".")
        )
    except OSError:
        return []


def load_account(name: str) -> Optional[dict]:
    try:
        with open(_account_path(name), "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return d if isinstance(d, dict) else None


def current_blob() -> Optional[dict]:
    raw = _read_keychain_raw()
    return decode_raw(raw) if raw else None


def current_email() -> str:
    return blob_email(current_blob())


def current_account_name() -> Optional[str]:
    """当前 keychain 对应哪个快照：先比 refresh_token 指纹，再退回比 email。"""
    cur = current_blob()
    if not cur:
        return None
    fp, mail = blob_fingerprint(cur), blob_email(cur)
    by_email = None
    for n in list_account_files():
        snap = load_account(n)
        if not snap:
            continue
        if fp and blob_fingerprint(snap) == fp:
            return n
        if mail and blob_email(snap) == mail and by_email is None:
            by_email = n
    return by_email


def _default_name_from_email(email: str) -> str:
    local = (email or "").split("@")[0]
    name = re.sub(r"[^A-Za-z0-9._-]", "-", local).strip("-._")
    return name or "default"


def save_current_account(name: Optional[str] = None, *, overwrite: bool = True,
                         guard_email: bool = True) -> tuple[bool, str]:
    """把当前 keychain 凭证 stash 成快照。name 省略时用 email 的用户名部分。

    guard_email=True：目标档已绑 A 邮箱、当前 keychain 是 B → 拒绝覆盖（防串号）。
    """
    cur = current_blob()
    if not cur:
        if not keychain_supported():
            return False, "agy 账号切换目前只支持 macOS keychain"
        return False, "keychain 里没有 agy 凭证（先跑一次 `agy` 登录）"
    if not (cur.get("token") or {}).get("refresh_token"):
        return False, "当前 agy 凭证没有 refresh_token（可能是 API key 模式，不用切号）"

    email = blob_email(cur)
    name = (name or "").strip() or _default_name_from_email(email)
    err = _validate_name(name)
    if err:
        return False, err

    target = _account_path(name)
    if os.path.exists(target):
        if not overwrite:
            return False, f"`{name}` 已存在（overwrite=True 才覆盖）"
        if guard_email:
            old_email = blob_email(load_account(name))
            if old_email and email and old_email != email:
                return False, (
                    f"拒绝覆盖：档 `{name}` 已绑 {old_email}，"
                    f"当前 keychain 是 {email} —— 换个名字存，或加 --force"
                )

    payload = _strip_meta(cur)
    payload["_meta"] = {
        "schema_version": _SCHEMA_VERSION,
        "saved_at": int(time.time()),
        "email": email,
    }
    try:
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        os.chmod(ACCOUNTS_DIR, 0o700)
        tmp = target + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, target)
    except OSError as e:
        return False, f"写 {target} 失败：{e}"
    return True, f"saved `{name}` — {email or '(email 未知)'}"


def resync_current() -> tuple[str, Optional[str]]:
    """把 keychain 当前凭证回收进它对应的快照（捕获 agy 自己刷的新 token）。

    返回 ("noop"|"resynced"|"skip"|"error", name_or_reason)。best-effort，不抛。
    """
    try:
        cur = current_blob()
        if not cur:
            return ("noop", None)
        name = current_account_name()
        if not name:
            return ("noop", None)
        snap = load_account(name)
        if snap and json.dumps(_strip_meta(snap), sort_keys=True) == \
                json.dumps(_strip_meta(cur), sort_keys=True):
            return ("noop", name)
        ok, msg = save_current_account(name, overwrite=True, guard_email=True)
        return ("resynced", name) if ok else ("skip", msg)
    except Exception as e:  # pragma: no cover - 纯保险
        return ("error", str(e))


def use_account(name: str) -> tuple[bool, str]:
    """切到已保存的 agy 账号：回收当前 → 写 keychain → 读回校验。"""
    err = _validate_name(name)
    if err:
        return False, err
    snap = load_account(name)
    if not snap:
        avail = "、".join(f"`{n}`" for n in list_account_files()) or "（无）"
        return False, f"没有已保存的 agy 账号 `{name}`；已有：{avail}"
    if not (snap.get("token") or {}).get("refresh_token"):
        return False, f"`{name}` 快照里没有 refresh_token，无法使用"

    cur_name = current_account_name()
    if cur_name == name:
        # 即使已经是它，也顺手回收一次，保证快照不落后
        resync_current()
        return True, f"already using `{name}`（{blob_email(snap) or '?'}）"

    resync_state, resync_info = resync_current()

    ok, msg = _write_keychain_raw(encode_raw(_strip_meta(snap)))
    if not ok:
        return False, f"写 keychain 失败：{msg}"

    back = current_blob()
    if blob_fingerprint(back) != blob_fingerprint(snap):
        return False, (
            "写完读回不一致——keychain 可能被正在运行的 agy 覆盖了，"
            "等它跑完再切一次"
        )

    extra = ""
    if resync_state == "skip":
        extra = f"（⚠️ 旧账号快照未回收：{resync_info}）"
    elif resync_state == "resynced":
        extra = f"（已先回收 `{resync_info}` 的最新 token）"
    return True, f"switched to `{name}` — {blob_email(snap) or '?'}{extra}"


def _delete_keychain() -> tuple[bool, str]:
    """删掉 keychain 里的 agy 凭证条目（同名条目可能不止一条，删到 find 为空）。"""
    if not keychain_supported():
        return False, "agy 账号切换目前只支持 macOS keychain"
    for _ in range(5):
        if _read_keychain_raw() is None:
            return True, ""
        try:
            r = subprocess.run(
                ["security", "delete-generic-password",
                 "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
                capture_output=True, text=True, timeout=_SECURITY_TIMEOUT,
            )
        except Exception as e:
            return False, f"exec security: {e}"
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "delete-generic-password failed").strip()
    return (_read_keychain_raw() is None), "keychain 条目删不干净"


def logout(*, require_saved: bool = True) -> tuple[bool, str]:
    """清空 keychain 里的 agy 凭证 —— 这是换号的唯一入口。

    agy CLI 没有 /login /logout（2026-09-12 实测 `/help` 里没有），下次启动发现
    keychain 空了就会重新走 Google OAuth，这时登另一个号即可。

    require_saved=True：当前号没存成快照就拒绝清（清了就找不回来了）。
    """
    cur = current_blob()
    if not cur:
        return True, "keychain 里本来就没有 agy 凭证，直接跑 `agy` 就会走登录流程"
    name = current_account_name()
    if require_saved and not name:
        return False, (
            f"当前号 {blob_email(cur) or '(未知)'} 还没保存过，拒绝清除 —— "
            f"先 save 一份再 logout（真要丢弃就用 --force）"
        )
    if name:
        resync_current()
    ok, msg = _delete_keychain()
    if not ok:
        return False, f"清除 keychain 失败：{msg}"
    saved_hint = f"（已保存为 `{name}`，之后 `use {name}` 切回来）" if name else ""
    return True, (
        f"已登出 {blob_email(cur) or '(未知)'}{saved_hint} —— "
        f"现在到终端跑一次 `agy`，它会重新走 Google 登录，登你的另一个号；"
        f"登完再 save 一个名字就能来回切了"
    )


def remove_account(name: str) -> tuple[bool, str]:
    err = _validate_name(name)
    if err:
        return False, err
    target = _account_path(name)
    if not os.path.exists(target):
        return False, f"没有已保存的 agy 账号 `{name}`"
    try:
        os.remove(target)
    except OSError as e:
        return False, f"删除失败：{e}"
    return True, f"removed `{name}`"


def list_accounts_summary() -> list[dict]:
    active = current_account_name()
    out = []
    for n in list_account_files():
        snap = load_account(n) or {}
        meta = snap.get("_meta") or {}
        out.append({
            "name": n,
            "active": n == active,
            "email": blob_email(snap),
            "auth_method": snap.get("auth_method") or "",
            "expiry_ts": blob_expiry_ts(snap),
            "saved_at": meta.get("saved_at"),
        })
    return out


def render_accounts_text() -> str:
    """给 Lark 卡片用的纯文本渲染。"""
    rows = list_accounts_summary()
    cur = current_blob()
    cur_email = blob_email(cur)
    lines = ["👤 **agy（Antigravity）账号**"]
    if cur:
        name = current_account_name()
        tag = f"`{name}`" if name else "`未保存`"
        lines.append(f"当前：{cur_email or '?'} → {tag}")
        lines.append(f"access_token 到期：{_fmt_expiry(blob_expiry_ts(cur))}")
    else:
        lines.append("当前：keychain 里没有 agy 凭证（先跑一次 `agy` 登录）")
    lines.append("")
    if not rows:
        lines.append("还没有保存任何账号 —— 发 `/switch save <名字>` 把当前号存下来。")
        return "\n".join(lines)
    lines.append("已保存：")
    for r in rows:
        mark = "● " if r["active"] else "○ "
        saved = (datetime.fromtimestamp(r["saved_at"]).strftime("%m-%d %H:%M")
                 if r.get("saved_at") else "?")
        lines.append(f"{mark}`{r['name']}` — {r['email'] or '?'} · 存于 {saved}")
    lines.append("")
    lines.append("`/switch <名字>` 切换 · `/switch save [名字]` 保存当前号 · "
                 "`/switch logout` 清凭证好去登新号")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────


def _cli_main(argv: list[str]) -> int:
    cmd = (argv[0] if argv else "list").lower()
    rest = argv[1:]
    force = "--force" in rest
    rest = [a for a in rest if not a.startswith("--")]

    if cmd in ("list", "ls"):
        print(render_accounts_text())
        return 0
    if cmd == "current":
        name = current_account_name()
        print(f"{current_email() or '(none)'}  ->  {name or '(unsaved)'}")
        return 0 if current_blob() else 1
    if cmd == "save":
        ok, msg = save_current_account(rest[0] if rest else None,
                                       guard_email=not force)
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1
    if cmd == "use":
        if not rest:
            print("usage: agy_account_switcher.py use <name>")
            return 2
        ok, msg = use_account(rest[0])
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1
    if cmd == "logout":
        ok, msg = logout(require_saved=not force)
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1
    if cmd in ("remove", "rm"):
        if not rest:
            print("usage: agy_account_switcher.py remove <name>")
            return 2
        ok, msg = remove_account(rest[0])
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli_main(sys.argv[1:]))
