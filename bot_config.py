"""
多 profile 配置：每个 profile 是一个独立的 Lark/Feishu 机器人实例，
拥有自己的 app_id、app_secret、默认目录、用户白名单、群白名单以及
对应的 lark-cli profile 名。

.env 示例：
    PROFILES=work,personal

    WORK_APP_ID=cli_xxx
    WORK_APP_SECRET=xxx
    WORK_PLATFORM=lark
    WORK_DEFAULT_CWD=/Users/me/work-project
    WORK_ALLOWED_OPEN_IDS=ou_xxx
    WORK_ALLOWED_GROUP_CHAT_IDS=oc_xxx
    WORK_LARK_CLI_PROFILE=work

    PERSONAL_APP_ID=cli_yyy
    PERSONAL_APP_SECRET=yyy
    PERSONAL_PLATFORM=feishu
    PERSONAL_DEFAULT_CWD=/Users/me/personal-project
    PERSONAL_ALLOWED_OPEN_IDS=ou_yyy
    PERSONAL_ALLOWED_GROUP_CHAT_IDS=oc_yyy
    PERSONAL_LARK_CLI_PROFILE=personal

当 PROFILES 缺省时退回到旧版单实例行为（读 FEISHU_APP_ID / FEISHU_APP_SECRET）。
"""

import os
import shutil
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

_DOMAIN_MAP = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}


def _split_csv(value: str) -> set[str]:
    return {s.strip() for s in (value or "").split(",") if s.strip()}


@dataclass
class Profile:
    """一个 Lark/Feishu 机器人实例的完整配置。"""
    name: str
    app_id: str
    app_secret: str
    platform: str
    domain: str
    default_cwd: str
    allowed_open_ids: set[str] = field(default_factory=set)
    allowed_group_chat_ids: set[str] = field(default_factory=set)
    lark_cli_profile: str = ""  # 告诉 Claude 用 `lark-cli --profile <name>` 发消息

    @property
    def brand_label(self) -> str:
        return "飞书" if self.platform == "feishu" else "Lark"


def _load_profile(name: str) -> Profile:
    prefix = name.upper()

    def env(suffix: str, default: str = "") -> str:
        return os.getenv(f"{prefix}_{suffix}", default)

    app_id = env("APP_ID")
    app_secret = env("APP_SECRET")
    if not app_id or not app_secret:
        raise ValueError(
            f"profile {name!r} 缺少 {prefix}_APP_ID 或 {prefix}_APP_SECRET"
        )

    platform = env("PLATFORM", "feishu").lower()
    if platform not in _DOMAIN_MAP:
        raise ValueError(
            f"profile {name!r} 的 {prefix}_PLATFORM 必须是 feishu 或 lark，当前: {platform}"
        )

    default_cwd = os.path.expanduser(env("DEFAULT_CWD", os.path.expanduser("~")))

    return Profile(
        name=name,
        app_id=app_id,
        app_secret=app_secret,
        platform=platform,
        domain=_DOMAIN_MAP[platform],
        default_cwd=default_cwd,
        allowed_open_ids=_split_csv(env("ALLOWED_OPEN_IDS")),
        allowed_group_chat_ids=_split_csv(env("ALLOWED_GROUP_CHAT_IDS")),
        lark_cli_profile=env("LARK_CLI_PROFILE", name),
    )


def _load_legacy_profile() -> Optional[Profile]:
    """兼容旧版单实例 .env：FEISHU_APP_ID / FEISHU_APP_SECRET / LARK_PLATFORM / DEFAULT_CWD / ALLOWED_OPEN_IDS / ALLOWED_GROUP_CHAT_IDS"""
    app_id = os.getenv("FEISHU_APP_ID", "")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        return None
    platform = os.getenv("LARK_PLATFORM", "feishu").lower()
    if platform not in _DOMAIN_MAP:
        raise ValueError(f"LARK_PLATFORM 必须是 feishu 或 lark，当前: {platform}")
    legacy_name = os.getenv("LEGACY_PROFILE_NAME", "default")
    return Profile(
        name=legacy_name,
        app_id=app_id,
        app_secret=app_secret,
        platform=platform,
        domain=_DOMAIN_MAP[platform],
        default_cwd=os.path.expanduser(os.getenv("DEFAULT_CWD", "~")),
        allowed_open_ids=_split_csv(os.getenv("ALLOWED_OPEN_IDS", "")),
        allowed_group_chat_ids=_split_csv(os.getenv("ALLOWED_GROUP_CHAT_IDS", "")),
        lark_cli_profile=os.getenv("LARK_CLI_PROFILE", legacy_name),
    )


def _load_profiles() -> list[Profile]:
    names_env = os.getenv("PROFILES", "").strip()
    if names_env:
        names = [n.strip() for n in names_env.split(",") if n.strip()]
        if not names:
            raise ValueError("PROFILES 不能为空")
        return [_load_profile(n) for n in names]

    legacy = _load_legacy_profile()
    if legacy is None:
        raise RuntimeError(
            "未找到任何 profile 配置。"
            "请在 .env 里设置 PROFILES=xxx,yyy 并分别配置 XXX_APP_ID/APP_SECRET/...，"
            "或使用旧版 FEISHU_APP_ID/FEISHU_APP_SECRET。"
        )
    return [legacy]


PROFILES: list[Profile] = _load_profiles()

# 名字 → profile 索引，供 callback 路由使用
PROFILES_BY_NAME: dict[str, Profile] = {p.name: p for p in PROFILES}

# ── 共享配置 ───────────────────────────────────────────────────

CLAUDE_CLI = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-opus-4-6")
PERMISSION_MODE = os.getenv("PERMISSION_MODE", "bypassPermissions")

# session 数据目录（每个 profile 写一个独立 json 文件）
SESSIONS_DIR = os.path.expanduser("~/.feishu-claude")

# 卡片按钮回调 HTTP 端口（ngrok 暴露），所有 profile 共用同一个端口
CALLBACK_PORT = int(os.getenv("CALLBACK_PORT", "9981"))

# 流式卡片更新：每积累多少字符推送一次
STREAM_CHUNK_SIZE = int(os.getenv("STREAM_CHUNK_SIZE", "20"))

# ── 旧代码兼容：有些模块可能仍读这些名字，保留向后兼容 ─────────
# 取第一个 profile 作为"默认"值，仅用于没有 profile 上下文的场景
_primary = PROFILES[0]
FEISHU_APP_ID = _primary.app_id
FEISHU_APP_SECRET = _primary.app_secret
LARK_PLATFORM = _primary.platform
LARK_DOMAIN = _primary.domain
DEFAULT_CWD = _primary.default_cwd
ALLOWED_OPEN_IDS = _primary.allowed_open_ids
ALLOWED_GROUP_CHAT_IDS = _primary.allowed_group_chat_ids
