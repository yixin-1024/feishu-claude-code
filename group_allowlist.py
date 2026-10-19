"""群白名单 + 群 workspace 映射的唯一写入口（`/group add` 和外部 API 的 `/api/v1/chats` 共用）。

两件事必须一起做才算「加群」：
  · 内存：`profile.allowed_group_chat_ids` / `profile.chat_default_cwd` —— 实时生效，不用重启；
  · `.env`：`<PREFIX>_ALLOWED_GROUP_CHAT_IDS` 追加、`<PREFIX>_CHAT_CWD_<chat_id>` 覆盖 —— 重启后仍在。

`.env` 写入走模块锁 + tmp/replace 原子落盘并保留原权限（0600）：外部 API 在 HTTP 线程里、
`/group add` 在 bot loop 里，两边同时加群不能互相覆盖，也不能写出半截文件。
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from typing import Optional, Tuple

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# Lark 群 id：oc_ + 字母数字。它会原样拼进 .env 的变量名，换行/等号/空格一律不收。
CHAT_ID_RE = re.compile(r"^oc_[A-Za-z0-9]{1,64}$")

_ENV_LOCK = threading.Lock()


def default_env_path() -> str:
    return os.getenv("CC_LARK_ENV_FILE", "").strip() or os.path.join(REPO_ROOT, ".env")


def persist_chat_to_env(
    profile_name: str, chat_id: str, cwd: Optional[str], env_path: str = "",
) -> Tuple[bool, str]:
    """把 chat_id 追加到 .env 的 <PREFIX>_ALLOWED_GROUP_CHAT_IDS；
    若 cwd 非空，再追加/覆盖 <PREFIX>_CHAT_CWD_<chat_id>=<cwd>。
    返回 (是否改动, .env 路径 或 ❌ 开头的错误说明)。
    """
    if not CHAT_ID_RE.match(chat_id or ""):
        return False, f"❌ chat_id 非法: {chat_id!r}"
    if cwd is not None and ("\n" in cwd or "\r" in cwd):
        return False, "❌ cwd 不能含换行"
    env_path = env_path or default_env_path()
    if not os.path.isfile(env_path):
        return False, f"❌ .env 不存在: {env_path}"

    prefix = profile_name.upper()
    allow_key = f"{prefix}_ALLOWED_GROUP_CHAT_IDS"
    cwd_key = f"{prefix}_CHAT_CWD_{chat_id}"

    with _ENV_LOCK:
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
            real = os.path.realpath(env_path)
            mode = os.stat(real).st_mode & 0o777
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real) or ".", prefix=".env.tmp.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write("\n".join(out) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, mode)
                os.replace(tmp, real)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    return changed, env_path


def add_chat_to_profile(
    profile, chat_id: str, cwd: Optional[str], env_path: str = "",
) -> Tuple[bool, str]:
    """内存白名单 + 群 cwd 映射实时生效，再落 .env。返回 persist_chat_to_env 的结果。

    .env 落盘失败（文件不存在等）时内存改动仍保留——和 `/group add` 一贯的语义一致：
    本次运行立即可用，调用方根据返回的 ❌ 提示决定要不要报错。
    """
    if not CHAT_ID_RE.match(chat_id or ""):
        return False, f"❌ chat_id 非法: {chat_id!r}"
    profile.allowed_group_chat_ids.add(chat_id)
    if cwd:
        profile.chat_default_cwd[chat_id] = cwd
    return persist_chat_to_env(profile.name, chat_id, cwd, env_path)
