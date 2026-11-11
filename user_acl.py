"""操作者白名单：谁能在哪个群 @ bot、谁能私聊它、谁能改这些设置。

判定（`user_allowed`），群白名单（`<PREFIX>_ALLOWED_GROUP_CHAT_IDS`）照旧在它前面先判：

  1. 管理员（`<PREFIX>_ADMIN_OPEN_IDS`）永远放行——免得把自己关在门外。
  2. 群消息：这个群配了 `<PREFIX>_CHAT_USERS_<chat_id>=ou_a,ou_b` 就只认这份名单
     （**替代**全局名单；值为空 = 只有管理员）；值是 `*` 或没配 = 沿用全局名单。
  3. 私聊：`<PREFIX>_ALLOW_P2P=0` 关掉后只有管理员能私聊（默认开）；
     `<PREFIX>_P2P_USERS=ou_a,...` 非空时只认这份名单，空 = 沿用全局名单。
  4. 全局名单 `<PREFIX>_ALLOWED_OPEN_IDS`：空 = 不限人。

没权限的人一律静默忽略（dispatcher 里不回话，不暴露 bot 的存在）。

管理员没配时 `is_admin` 对所有人返回 True —— 保持老行为（过了白名单的人都能
`/group`、`/restart`），升级后不会突然谁都重启不了。

`/acl` 命令（只有管理员能用）在群里改本群名单、在任意处开关私聊，实时生效并落 .env。
`/acl clear` 写 `=*` 而不是删行：Mac 上 /restart 拉起的新进程继承老进程的环境变量，
删掉的行会"复活"；写成 `*` 靠 load_dotenv(override=True) 覆盖掉旧值。
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence, Tuple

import group_allowlist

OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9]{1,64}$")


# ── 判定 ─────────────────────────────────────────────────────────


def _ids(profile, attr: str) -> set[str]:
    return set(getattr(profile, attr, None) or set())


def admin_ids(profile) -> set[str]:
    return _ids(profile, "admin_open_ids")


def is_admin(profile, user_id: str) -> bool:
    """能不能用 /acl /group /restart。没配管理员 = 过了白名单的人都算（老行为）。"""
    admins = admin_ids(profile)
    if not admins:
        return True
    return bool(user_id) and user_id in admins


def chat_list(profile, raw_chat_id: str) -> Optional[set[str]]:
    """本群单独配的名单；None = 没配（沿用全局）。空集 = 只有管理员。"""
    lists = getattr(profile, "chat_allowed_open_ids", None) or {}
    if raw_chat_id in lists:
        return set(lists[raw_chat_id])
    return None


def p2p_enabled(profile) -> bool:
    return bool(getattr(profile, "allow_p2p", True))


def user_allowed(profile, user_id: str, *, is_group: bool, raw_chat_id: str = "") -> bool:
    """这个人能不能在这里触发 bot（群白名单由调用方先判）。"""
    if not user_id:
        return False
    if user_id in admin_ids(profile):
        return True
    if is_group:
        users = chat_list(profile, raw_chat_id)
        if users is not None:
            return user_id in users
    else:
        if not p2p_enabled(profile):
            return False
        p2p_users = _ids(profile, "p2p_allowed_open_ids")
        if p2p_users:
            return user_id in p2p_users
    glob = _ids(profile, "allowed_open_ids")
    return not glob or user_id in glob


def parse_chat_users(value: Optional[str]) -> Optional[set[str]]:
    """.env 里 `<PREFIX>_CHAT_USERS_<chat_id>` 的值 → 名单。`*` → None（不单独限制）。"""
    text = (value or "").strip()
    if text == "*":
        return None
    return {x.strip() for x in text.split(",") if x.strip()}


# ── 落盘 ─────────────────────────────────────────────────────────


def chat_users_key(profile_name: str, chat_id: str) -> str:
    return f"{profile_name.upper()}_CHAT_USERS_{chat_id}"


def set_chat_users(profile, chat_id: str, users: Optional[set[str]],
                   env_path: str = "") -> Tuple[bool, str]:
    """内存实时生效 + 落 .env。users=None = 去掉本群名单（写 `*`）。"""
    if not group_allowlist.CHAT_ID_RE.match(chat_id or ""):
        return False, f"❌ chat_id 非法: {chat_id!r}"
    for u in users or ():
        if not OPEN_ID_RE.match(u):
            return False, f"❌ open_id 非法: {u!r}"
    lists = getattr(profile, "chat_allowed_open_ids", None)
    if lists is None:
        lists = {}
        profile.chat_allowed_open_ids = lists
    if users is None:
        lists.pop(chat_id, None)
        value = "*"
    else:
        lists[chat_id] = set(users)
        value = ",".join(sorted(users))
    return group_allowlist.set_env_line(
        chat_users_key(profile.name, chat_id), value, env_path)


def set_p2p(profile, enabled: bool, env_path: str = "") -> Tuple[bool, str]:
    profile.allow_p2p = bool(enabled)
    return group_allowlist.set_env_line(
        f"{profile.name.upper()}_ALLOW_P2P", "1" if enabled else "0", env_path)


# ── /acl 命令 ────────────────────────────────────────────────────

USAGE = (
    "用法（在要设置的群里 @我 发）：\n"
    "`/acl` 看本群名单\n"
    "`/acl add @某人` 加人（可一次 @ 多个，也可写 ou_ 开头的 id）\n"
    "`/acl remove @某人` 删人\n"
    "`/acl clear` 去掉本群名单，恢复默认\n"
    "`/acl dm on|off` 开关私聊"
)


async def _names(bot, ids: Iterable[str], known: dict[str, str]) -> dict[str, str]:
    ids = list(ids)
    out = {i: known.get(i, "") for i in ids}
    missing = [i for i in ids if not out[i]]
    feishu = getattr(bot, "feishu", None)
    if missing and feishu is not None and hasattr(feishu, "batch_resolve_names"):
        try:
            got = await feishu.batch_resolve_names(missing)
            for i in missing:
                out[i] = got.get(i, "") or ""
        except Exception:  # noqa: BLE001 — 名字只是展示用，取不到就显示 id
            pass
    return out


def _label(uid: str, names: dict[str, str]) -> str:
    nm = names.get(uid, "")
    return f"{nm}（{uid[:10]}…）" if nm else f"`{uid}`"


def _fmt_global(profile) -> str:
    glob = _ids(profile, "allowed_open_ids")
    return f"全局名单 {len(glob)} 人" if glob else "不限人"


async def _show(bot, raw_chat_id: str, is_group: bool, known: dict[str, str]) -> str:
    profile = bot.profile
    lines = ["🔐 **操作者名单**"]
    if is_group:
        users = chat_list(profile, raw_chat_id)
        if users is None:
            lines.append(f"本群：没单独设置，沿用默认（{_fmt_global(profile)}）")
        elif not users:
            lines.append("本群：只有管理员能 @")
        else:
            names = await _names(bot, sorted(users), known)
            lines.append(f"本群：只有这 {len(users)} 人 + 管理员能 @")
            lines.extend(f"· {_label(u, names)}" for u in sorted(users))
    else:
        lists = getattr(profile, "chat_allowed_open_ids", None) or {}
        lines.append(f"单独设了名单的群：{len(lists)} 个")
    p2p_users = _ids(profile, "p2p_allowed_open_ids")
    if not p2p_enabled(profile):
        lines.append("私聊：关（只有管理员能私聊）")
    elif p2p_users:
        lines.append(f"私聊：开（只限 {len(p2p_users)} 人）")
    else:
        lines.append(f"私聊：开（{_fmt_global(profile)}）")
    admins = admin_ids(profile)
    if admins:
        names = await _names(bot, sorted(admins), known)
        lines.append("管理员：" + "、".join(names.get(a) or a[:10] + "…" for a in sorted(admins)))
    else:
        lines.append("管理员：未设置（能用 bot 的人都能改）")
    lines.append("")
    lines.append(USAGE)
    return "\n".join(lines)


async def handle_acl_command(
    bot,
    user_id: str,
    chat_id: str,
    args: str,
    mentions: Sequence[Tuple[str, str]] = (),
) -> str:
    """`/acl …`。chat_id 是会话键（群 `oc_x[:omt_y]` / 私聊 = user_id）；
    mentions 是消息里 @ 到的人 [(open_id, 名字)]，已去掉 bot 自己。"""
    profile = bot.profile
    if not is_admin(profile, user_id):
        return "⛔ 只有管理员能用 `/acl`。"
    raw_chat_id = (chat_id or "").split(":", 1)[0]
    is_group = raw_chat_id.startswith("oc_")
    known = {oid: name for oid, name in mentions if oid}

    parts = (args or "").split()
    sub = parts[0].lower() if parts else ""
    rest = parts[1:]

    if sub in ("", "show", "list", "ls"):
        return await _show(bot, raw_chat_id, is_group, known)

    if sub == "dm":
        flag = rest[0].lower() if rest else ""
        if flag not in ("on", "off"):
            return "⚠️ 用法：`/acl dm on` 或 `/acl dm off`"
        enabled = flag == "on"
        _, info = set_p2p(profile, enabled)
        msg = "✅ 私聊已打开" if enabled else "✅ 私聊已关闭（只有管理员还能私聊）"
        if info.startswith("❌"):
            msg += f"\n⚠️ .env 没写进去，重启后会丢：{info}"
        return msg

    if sub not in ("add", "remove", "rm", "del", "clear", "reset"):
        return f"⚠️ 不认识的子命令 `{sub}`\n\n{USAGE}"

    if not is_group:
        return "⚠️ 群名单要在那个群里 @我 发 `/acl …`。"
    if not group_allowlist.CHAT_ID_RE.match(raw_chat_id):
        return f"⚠️ 这个会话不支持设名单：`{raw_chat_id}`"

    if sub in ("clear", "reset"):
        _, info = set_chat_users(profile, raw_chat_id, None)
        msg = f"✅ 本群名单已去掉，恢复默认（{_fmt_global(profile)}）"
        if info.startswith("❌"):
            msg += f"\n⚠️ .env 没写进去，重启后会丢：{info}"
        return msg

    targets: list[str] = [oid for oid, _ in mentions if oid]
    bad: list[str] = []
    for tok in rest:
        if tok.startswith("ou_"):
            if OPEN_ID_RE.match(tok):
                targets.append(tok)
            else:
                bad.append(tok)
    targets = list(dict.fromkeys(targets))
    if bad:
        return "⚠️ 这些 id 不对：" + "、".join(f"`{b}`" for b in bad)
    if not targets:
        return "⚠️ 要 @ 到具体的人（或写 ou_ 开头的 id）"

    current = chat_list(profile, raw_chat_id)
    users = set(current or ())
    names = await _names(bot, targets, known)
    if sub == "add":
        users |= set(targets)
        verb = "加了"
    else:
        if current is None:
            return "⚠️ 本群还没设名单（现在是默认），没有人可删。"
        users -= set(targets)
        verb = "删了"
    _, info = set_chat_users(profile, raw_chat_id, users)

    lines = [f"✅ {verb}：" + "、".join(_label(t, names) for t in targets)]
    if users:
        lines.append(f"本群现在只有这 {len(users)} 人 + 管理员能 @")
    else:
        lines.append("本群名单空了：现在只有管理员能 @（`/acl clear` 恢复默认）")
    if current is None and sub == "add":
        lines.append("⚠️ 这是本群第一份名单：名单外的人从现在起 @ 我都不会理")
    if info.startswith("❌"):
        lines.append(f"⚠️ .env 没写进去，重启后会丢：{info}")
    return "\n".join(lines)


def mention_targets(mentions, bot_open_id: str) -> list[Tuple[str, str]]:
    """Lark 消息的 mentions → [(open_id, 名字)]，去掉 bot 自己和 @所有人。"""
    out: list[Tuple[str, str]] = []
    for m in mentions or []:
        oid = getattr(getattr(m, "id", None), "open_id", "") or ""
        if not oid or oid == bot_open_id or not OPEN_ID_RE.match(oid):
            continue
        out.append((oid, getattr(m, "name", "") or ""))
    return out
