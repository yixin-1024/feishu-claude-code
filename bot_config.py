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

    # 可选：按 chat_id 覆盖默认 cwd（新群第一次接入时生效；
    # 已有 sessions.json 记录的群不受影响，仍走持久化值）
    WORK_CHAT_CWD_oc_aaa=/Users/me/spx
    WORK_CHAT_CWD_oc_bbb=/Users/me/tools/feishu-claude-code

    PERSONAL_APP_ID=cli_yyy
    PERSONAL_APP_SECRET=yyy
    PERSONAL_PLATFORM=feishu
    PERSONAL_DEFAULT_CWD=/Users/me/personal-project
    PERSONAL_ALLOWED_OPEN_IDS=ou_yyy
    PERSONAL_ALLOWED_GROUP_CHAT_IDS=oc_yyy
    PERSONAL_LARK_CLI_PROFILE=personal

当 PROFILES 缺省时退回到旧版单实例行为（读 FEISHU_APP_ID / FEISHU_APP_SECRET）。
"""

import io
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Optional

from dotenv import dotenv_values, load_dotenv

# override=True：让 .env 文件成为唯一事实源。
# 必须 override —— /restart 是以"当前运行进程"为父进程跑 `open .app` 拉起新实例，
# 新进程会继承老进程的环境变量；而老进程启动时 load_dotenv 已把 .env 的值灌进了
# os.environ。若用默认 override=False，下次重启时这些"上一代灌进来的旧值"会粘住，
# 改了 .env 也读不到（实测 <PROFILE>_CLAUDE_ENV_FILE 跨重启一直卡在旧文件）。
# override=True 强制每次启动都以磁盘上的 .env 为准，根治这个跨重启粘滞。
load_dotenv(override=not bool(os.getenv("PYTEST_CURRENT_TEST")))

# cc-lark 代码目录——用于解析相对路径的 claude env 覆盖文件
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
    # 执行后端：claude（默认）或 codex。按 profile 绑定，用于让不同 bot
    # 走不同 agent；后续可在 chat 维度继续覆盖。
    runner: str = "claude"
    default_model: str = ""
    # 可选：chat_id -> cwd 映射。新群初次进入时按此初始化 cwd（缺省回退到 default_cwd）。
    # 不影响 sessions.json 已记录的群——那些走持久化值，可被 /ws set / /ws use 继续覆盖。
    chat_default_cwd: dict[str, str] = field(default_factory=dict)
    allowed_open_ids: set[str] = field(default_factory=set)
    allowed_group_chat_ids: set[str] = field(default_factory=set)
    # Webhook callback source verification. Long-connection callbacks are already
    # authenticated by the SDK; HTTP callbacks should also set this per profile.
    verification_token: str = ""
    lark_cli_profile: str = ""  # 告诉 Claude 用 `lark-cli --profile <name>` 发消息
    # 可选：该 profile spawn claude 时额外注入的 env 覆盖文件（相对 cc-lark 目录或绝对路径）。
    # 用于把某个 bot 路由到不同的模型供应商（如 .env.deepseek 走 DeepSeek 的 Anthropic 兼容端点）。
    # 文件格式见 load_claude_extra_env：支持 JSON {"env":{...}} 或 dotenv KEY=VALUE。
    claude_env_file: str = ""
    # 可选：Claude Code 子后端。空=沿用全局 CLAUDE_RUNNER；pty=交互式 JSONL tail；
    # print=`claude --print --output-format stream-json`。用于单独绕开某个 profile
    # 的 PTY/JSONL 兼容性问题。
    claude_runner: str = ""
    codex_bin: str = ""
    codex_sandbox_mode: str = ""
    codex_approval_policy: str = ""
    codex_dangerous_bypass: int = 0
    codex_idle_timeout_sec: int = 3600
    # opencode runner 配置。opencode 走 provider/model 形式；裸 model 名按
    # opencode_provider 补前缀。api_key 注入到子进程的 opencode_api_key_env
    # 环境变量（google provider 默认读 GOOGLE_GENERATIVE_AI_API_KEY）。
    opencode_bin: str = ""
    opencode_provider: str = "google"
    opencode_api_key: str = ""
    opencode_api_key_env: str = "GOOGLE_GENERATIVE_AI_API_KEY"
    opencode_dangerous_skip: int = 1
    opencode_idle_timeout_sec: int = 300
    # mimo runner 配置（MiMo Code，小米 fork 自 opencode）。走 provider/model 形式，
    # 裸 model 名按 mimo_provider 补前缀（默认 quotio）。自定义 OpenAI 兼容 provider
    # 写在 ~/.config/mimocode/mimocode.json；若给定 mimo_base_url + mimo_api_key，
    # runner 启动时会幂等地把该 provider 合并进配置（已存在则不动）。
    mimo_bin: str = ""
    mimo_provider: str = "quotio"
    mimo_base_url: str = ""
    mimo_api_key: str = ""
    mimo_variant: str = ""  # 推理强度：high / max / minimal（provider 相关）
    mimo_dangerous_skip: int = 1
    mimo_idle_timeout_sec: int = 600
    # "会话群" chat_id：bot 在其它群被 @ 时（=调度 session），会被指引把任务派单到
    # 这个群的新话题里，由独立 session 承接处理。空字符串=禁用派单。
    dispatch_chat_id: str = ""

    # ── Trinity 三省体系 ─────────────────────────────────────────
    # 角色：yushitai / zhongshu / menxia / shangshu / ganhuode
    # 空 = 普通 bot（保留原 dispatch_chat_id 派单行为）
    role: str = ""
    # 朝廷群 chat_id：所有 trinity bot 在这个群里协作。等同于 dispatch_chat_id
    # 但语义更明确——为了让 trinity 模式和遗留派单模式可以共存。
    court_chat_id: str = ""
    # 当前 bot 自己的 open_id（lark-cli 发出去的 sender_id），用于其他 bot
    # 识别"是不是同体系内的 bot 在 @ 我"。
    bot_open_id: str = ""
    # 链路上 5 个 bot 的 open_id（每个 bot 都要知道全员，权限矩阵校验时用）
    yushitai_open_id: str = ""
    zhongshu_open_id: str = ""
    menxia_open_id: str = ""
    shangshu_open_id: str = ""
    ganhuode_open_id: str = ""
    # Boss（皇帝）本人的 open_id —— 只有 yushitai 实际需要，但放 Profile
    # 里方便 prompt 模板统一引用。
    boss_open_id: str = ""

    @property
    def brand_label(self) -> str:
        return "飞书" if self.platform == "feishu" else "Lark"

    @property
    def is_trinity(self) -> bool:
        """是否启用 trinity 三省体系。

        需要同时满足：
            1. 全局开关 ENABLE_TRINITY=true（加载时清空 role 字段实现"软关闭"）
            2. 本 profile 配置了 role
        """
        return bool(self.role)

    def bot_open_id_for(self, role: str) -> str:
        """根据角色名拿 open_id；未配置返回空串。"""
        return {
            "yushitai": self.yushitai_open_id,
            "zhongshu": self.zhongshu_open_id,
            "menxia": self.menxia_open_id,
            "shangshu": self.shangshu_open_id,
            "ganhuode": self.ganhuode_open_id,
        }.get(role, "")


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

    # 扫描 <PREFIX>_CHAT_CWD_<chat_id> 形式的环境变量（chat_id 大小写按用户写的原样）。
    chat_cwd_prefix = f"{prefix}_CHAT_CWD_"
    chat_default_cwd: dict[str, str] = {}
    for env_key, env_val in os.environ.items():
        if env_key.startswith(chat_cwd_prefix) and env_val:
            chat_id = env_key[len(chat_cwd_prefix):]
            if chat_id:
                chat_default_cwd[chat_id] = os.path.expanduser(env_val)

    role = env("ROLE").strip().lower()
    runner = env("RUNNER", "claude").strip().lower()
    if runner not in {"claude", "codex", "opencode", "mimo"}:
        raise ValueError(
            f"profile {name!r} 的 {prefix}_RUNNER 必须是 claude / codex / opencode / mimo，当前: {runner}"
        )
    claude_runner = env("CLAUDE_RUNNER").strip().lower()
    if claude_runner and claude_runner not in {"pty", "print"}:
        raise ValueError(
            f"profile {name!r} 的 {prefix}_CLAUDE_RUNNER 必须是 pty / print，当前: {claude_runner}"
        )
    try:
        codex_bypass = int(env("CODEX_DANGEROUS_BYPASS", os.getenv("CODEX_DANGEROUS_BYPASS", "2")) or "2")
    except ValueError:
        codex_bypass = 0
    codex_bypass = max(0, min(2, codex_bypass))
    try:
        codex_idle = int(env("CODEX_IDLE_TIMEOUT_SEC", os.getenv("CODEX_IDLE_TIMEOUT_SEC", "3600")) or "3600")
    except ValueError:
        codex_idle = 3600
    try:
        oc_skip = int(env("OPENCODE_DANGEROUS_SKIP", os.getenv("OPENCODE_DANGEROUS_SKIP", "1")) or "1")
    except ValueError:
        oc_skip = 1
    try:
        oc_idle = int(env("OPENCODE_IDLE_TIMEOUT_SEC", os.getenv("OPENCODE_IDLE_TIMEOUT_SEC", "300")) or "300")
    except ValueError:
        oc_idle = 300
    try:
        mimo_skip = int(env("MIMO_DANGEROUS_SKIP", os.getenv("MIMO_DANGEROUS_SKIP", "1")) or "1")
    except ValueError:
        mimo_skip = 1
    try:
        mimo_idle = int(env("MIMO_IDLE_TIMEOUT_SEC", os.getenv("MIMO_IDLE_TIMEOUT_SEC", "600")) or "600")
    except ValueError:
        mimo_idle = 600
    return Profile(
        name=name,
        app_id=app_id,
        app_secret=app_secret,
        platform=platform,
        domain=_DOMAIN_MAP[platform],
        default_cwd=default_cwd,
        runner=runner,
        default_model=env("DEFAULT_MODEL", os.getenv("DEFAULT_MODEL", "")),
        chat_default_cwd=chat_default_cwd,
        allowed_open_ids=_split_csv(env("ALLOWED_OPEN_IDS")),
        allowed_group_chat_ids=_split_csv(env("ALLOWED_GROUP_CHAT_IDS")),
        verification_token=env("VERIFICATION_TOKEN").strip(),
        lark_cli_profile=env("LARK_CLI_PROFILE", name),
        claude_env_file=env("CLAUDE_ENV_FILE").strip(),
        claude_runner=claude_runner,
        codex_bin=env("CODEX_BIN", os.getenv("CODEX_BIN", "")).strip(),
        codex_sandbox_mode=env("CODEX_SANDBOX_MODE", os.getenv("CODEX_SANDBOX_MODE", "")).strip(),
        codex_approval_policy=env("CODEX_APPROVAL_POLICY", os.getenv("CODEX_APPROVAL_POLICY", "")).strip(),
        codex_dangerous_bypass=codex_bypass,
        codex_idle_timeout_sec=max(0, codex_idle),
        opencode_bin=env("OPENCODE_BIN", os.getenv("OPENCODE_BIN", "")).strip(),
        opencode_provider=env("OPENCODE_PROVIDER", os.getenv("OPENCODE_PROVIDER", "google")).strip() or "google",
        opencode_api_key=env("OPENCODE_API_KEY", os.getenv("OPENCODE_API_KEY", "")).strip(),
        opencode_api_key_env=env("OPENCODE_API_KEY_ENV", os.getenv("OPENCODE_API_KEY_ENV", "GOOGLE_GENERATIVE_AI_API_KEY")).strip() or "GOOGLE_GENERATIVE_AI_API_KEY",
        opencode_dangerous_skip=max(0, min(1, oc_skip)),
        opencode_idle_timeout_sec=max(0, oc_idle),
        mimo_bin=env("MIMO_BIN", os.getenv("MIMO_BIN", "")).strip(),
        mimo_provider=env("MIMO_PROVIDER", os.getenv("MIMO_PROVIDER", "quotio")).strip() or "quotio",
        mimo_base_url=env("MIMO_BASE_URL", os.getenv("MIMO_BASE_URL", "")).strip(),
        mimo_api_key=env("MIMO_API_KEY", os.getenv("MIMO_API_KEY", "")).strip(),
        mimo_variant=env("MIMO_VARIANT", os.getenv("MIMO_VARIANT", "")).strip(),
        mimo_dangerous_skip=max(0, min(1, mimo_skip)),
        mimo_idle_timeout_sec=max(0, mimo_idle),
        dispatch_chat_id=env("DISPATCH_CHAT_ID").strip(),
        role=role,
        court_chat_id=env("COURT_CHAT_ID").strip(),
        bot_open_id=env("BOT_OPEN_ID").strip(),
        # 5 个角色 open_id 可以放在共享配置里（5 个 profile 都引用同一组），
        # 也可以单独覆盖。这里先读 profile 专属，没有再回落到全局。
        yushitai_open_id=env("YUSHITAI_OPEN_ID", os.getenv("YUSHITAI_OPEN_ID", "")).strip(),
        zhongshu_open_id=env("ZHONGSHU_OPEN_ID", os.getenv("ZHONGSHU_OPEN_ID", "")).strip(),
        menxia_open_id=env("MENXIA_OPEN_ID", os.getenv("MENXIA_OPEN_ID", "")).strip(),
        shangshu_open_id=env("SHANGSHU_OPEN_ID", os.getenv("SHANGSHU_OPEN_ID", "")).strip(),
        ganhuode_open_id=env("GANHUODE_OPEN_ID", os.getenv("GANHUODE_OPEN_ID", "")).strip(),
        boss_open_id=env("BOSS_OPEN_ID", os.getenv("BOSS_OPEN_ID", "")).strip(),
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
    try:
        codex_bypass = int(os.getenv("CODEX_DANGEROUS_BYPASS", "2") or "2")
    except ValueError:
        codex_bypass = 0
    try:
        codex_idle = int(os.getenv("CODEX_IDLE_TIMEOUT_SEC", "3600") or "3600")
    except ValueError:
        codex_idle = 3600
    try:
        oc_skip = int(os.getenv("OPENCODE_DANGEROUS_SKIP", "1") or "1")
    except ValueError:
        oc_skip = 1
    try:
        oc_idle = int(os.getenv("OPENCODE_IDLE_TIMEOUT_SEC", "300") or "300")
    except ValueError:
        oc_idle = 300
    try:
        mimo_skip = int(os.getenv("MIMO_DANGEROUS_SKIP", "1") or "1")
    except ValueError:
        mimo_skip = 1
    try:
        mimo_idle = int(os.getenv("MIMO_IDLE_TIMEOUT_SEC", "600") or "600")
    except ValueError:
        mimo_idle = 600
    return Profile(
        name=legacy_name,
        app_id=app_id,
        app_secret=app_secret,
        platform=platform,
        domain=_DOMAIN_MAP[platform],
        default_cwd=os.path.expanduser(os.getenv("DEFAULT_CWD", "~")),
        runner=os.getenv("RUNNER", "claude").strip().lower() or "claude",
        default_model=os.getenv("DEFAULT_MODEL", ""),
        allowed_open_ids=_split_csv(os.getenv("ALLOWED_OPEN_IDS", "")),
        allowed_group_chat_ids=_split_csv(os.getenv("ALLOWED_GROUP_CHAT_IDS", "")),
        verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN", "").strip(),
        lark_cli_profile=os.getenv("LARK_CLI_PROFILE", legacy_name),
        claude_env_file=os.getenv("CLAUDE_ENV_FILE", "").strip(),
        claude_runner=os.getenv("CLAUDE_RUNNER", "").strip().lower(),
        codex_bin=os.getenv("CODEX_BIN", "").strip(),
        codex_sandbox_mode=os.getenv("CODEX_SANDBOX_MODE", "").strip(),
        codex_approval_policy=os.getenv("CODEX_APPROVAL_POLICY", "").strip(),
        codex_dangerous_bypass=max(0, min(2, codex_bypass)),
        codex_idle_timeout_sec=max(0, codex_idle),
        opencode_bin=os.getenv("OPENCODE_BIN", "").strip(),
        opencode_provider=os.getenv("OPENCODE_PROVIDER", "google").strip() or "google",
        opencode_api_key=os.getenv("OPENCODE_API_KEY", "").strip(),
        opencode_api_key_env=os.getenv("OPENCODE_API_KEY_ENV", "GOOGLE_GENERATIVE_AI_API_KEY").strip() or "GOOGLE_GENERATIVE_AI_API_KEY",
        opencode_dangerous_skip=max(0, min(1, oc_skip)),
        opencode_idle_timeout_sec=max(0, oc_idle),
        mimo_bin=os.getenv("MIMO_BIN", "").strip(),
        mimo_provider=os.getenv("MIMO_PROVIDER", "quotio").strip() or "quotio",
        mimo_base_url=os.getenv("MIMO_BASE_URL", "").strip(),
        mimo_api_key=os.getenv("MIMO_API_KEY", "").strip(),
        mimo_variant=os.getenv("MIMO_VARIANT", "").strip(),
        mimo_dangerous_skip=max(0, min(1, mimo_skip)),
        mimo_idle_timeout_sec=max(0, mimo_idle),
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


# ── Trinity 三省体系开关 ─────────────────────────────────────
#
# 默认 OFF。设 ENABLE_TRINITY=true 才启用三省体系。即使 profile 配了 ROLE，
# 没有这个开关也不会走 trinity 路径——保持 100% 向后兼容。
#
# 接受的真值：true / 1 / yes / on（大小写不敏感）

_TRUTHY = {"true", "1", "yes", "on", "y"}
TRINITY_ENABLED: bool = os.getenv("ENABLE_TRINITY", "").strip().lower() in _TRUTHY

# ── 话题群共享 session ─────────────────────────────────────────
#
# 默认开启：同一话题群的同一个话题（thread）里，不管谁 @ bot，都续同一个
# claude/codex session（SessionStore 把 "oc_xxx:omt_yyy" 复合 chat_id 归到
# 哨兵用户桶；ActiveRunRegistry 同样按话题聚合，任何人都能 /stop 话题内任务）。
# 设 THREAD_SHARED_SESSION=0 恢复旧版"每人各自一个 session"行为。
THREAD_SHARED_SESSION: bool = (
    os.getenv("THREAD_SHARED_SESSION", "1").strip().lower() in _TRUTHY
)


# 关闭时把 trinity 相关属性置零，让 Profile.is_trinity / PROFILES_BY_ROLE 等
# 全部"看不见"角色配置，dispatcher/lark_prompts 走遗留路径。
if not TRINITY_ENABLED:
    for _p in PROFILES:
        _p.role = ""

# bot_open_id → profile 索引，供 trinity 路径识别"发件人是不是同体系内 bot"
# 只有配置了 bot_open_id 的 profile 才会进入这张表
PROFILES_BY_BOT_OPEN_ID: dict[str, Profile] = (
    {p.bot_open_id: p for p in PROFILES if p.bot_open_id}
    if TRINITY_ENABLED else {}
)

# role → profile 索引（只看 trinity profile）
PROFILES_BY_ROLE: dict[str, Profile] = (
    {p.role: p for p in PROFILES if p.role}
    if TRINITY_ENABLED else {}
)

# ── 共享配置 ───────────────────────────────────────────────────

CLAUDE_CLI = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-opus-4-8[1m]")
DEFAULT_RUNNER = os.getenv("RUNNER", "claude").strip().lower() or "claude"
PERMISSION_MODE = os.getenv("PERMISSION_MODE", "bypassPermissions")

# session 数据目录（每个 profile 写一个独立 json 文件）
SESSIONS_DIR = os.path.expanduser("~/.feishu-claude")

# 卡片按钮回调 HTTP 端口（ngrok 暴露），所有 profile 共用同一个端口。
# control API 必须使用独立 loopback 端口，不能和 callback/ngrok 共面。
CALLBACK_PORT = int(os.getenv("CALLBACK_PORT", "9981"))
CONTROL_PORT = int(os.getenv("CONTROL_PORT", str(CALLBACK_PORT + 1)))

# 流式卡片更新：每积累多少字符推送一次
STREAM_CHUNK_SIZE = int(os.getenv("STREAM_CHUNK_SIZE", "20"))


# ── 按 profile 注入 claude env 覆盖（多供应商路由）────────────────
#
# 某些 bot 可以走非 Anthropic 的模型供应商：在 .env 里给该 profile
# 配 <PREFIX>_CLAUDE_ENV_FILE=.env.deepseek，spawn claude 时这个文件里的 env
# 会 update 进子进程环境，从而覆盖 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN /
# ANTHROPIC_MODEL 等，把这一路 bot 整体路由到别的端点。

# claude_env_file 解析缓存：abspath -> (mtime, parsed_dict)
_CLAUDE_ENV_CACHE: dict[str, tuple[float, dict[str, str]]] = {}


def _parse_claude_env_text(text: str) -> dict[str, str]:
    """解析 claude env 覆盖文件文本。支持两种写法，value 一律转成 str：

    1) JSON —— 直接粘贴 settings.json 的 ``{"env": {...}}`` 块，或扁平 ``{"K":"V"}``
    2) dotenv —— 逐行 ``KEY=VALUE``
    """
    text = (text or "").strip()
    if not text:
        return {}
    # 先按 JSON 解析（兼容用户直接粘 settings.json 的 env 块）
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict):
        env_obj = obj["env"] if isinstance(obj.get("env"), dict) else obj
        return {str(k): str(v) for k, v in env_obj.items()}
    # 回退 dotenv（KEY=VALUE 逐行）
    vals = dotenv_values(stream=io.StringIO(text))
    return {str(k): str(v) for k, v in vals.items() if v is not None}


def load_claude_extra_env(profile: "Profile") -> dict[str, str]:
    """读取某 profile 配置的 claude env 覆盖文件，返回要注入 spawn 的环境变量。

    - 没配置 / 文件不存在 / 解析失败 → 返回 {}（绝不抛，避免拖垮 bot）。
    - 按 mtime 缓存：编辑覆盖文件后，下次 spawn 自动生效，无需重启 cc-lark。
    """
    rel = (getattr(profile, "claude_env_file", "") or "").strip()
    if not rel:
        return {}
    path = rel if os.path.isabs(rel) else os.path.join(_BASE_DIR, rel)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _CLAUDE_ENV_CACHE.pop(path, None)
        return {}
    cached = _CLAUDE_ENV_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            parsed = _parse_claude_env_text(f.read())
    except Exception:
        parsed = {}
    _CLAUDE_ENV_CACHE[path] = (mtime, parsed)
    return parsed


# ── cc-lark 运行时 MCP 能力闸门（支持 per-profile 覆盖）──────────
#
# 三个闸门决定 spawn 出的 claude 里注册哪几个 cc-lark 运行时工具：
#   CC_LARK_ALLOW_DISPATCH（dispatch_task + read_thread）
#   CC_LARK_ALLOW_WAKE    （wake_me_in）
#   CC_LARK_ALLOW_CRON    （schedule_cron + list_crons）
# 优先级：<PROFILE>_<FLAG>（该 profile 专属）> <FLAG>（全局）> 未设（=开，由
# cc_mcp_server._allow 默认放行）。profile 段大写、非字母数字转下划线，与 env
# 命名惯例一致（如 profile "spx" → SPX_CC_LARK_ALLOW_DISPATCH）。
# 这样"从不编排的 bot（regtank/seesaw 等）默认关掉 dispatch/cron，省掉每 turn
# 白背的工具描述 context"就能单独配，而不必全局一刀切。

_CC_LARK_GATE_FLAGS = ("CC_LARK_ALLOW_DISPATCH", "CC_LARK_ALLOW_WAKE", "CC_LARK_ALLOW_CRON")


def _profile_env_prefix(profile_name: str) -> str:
    """把 profile 名归一成 env 前缀：大写 + 非字母数字→下划线 + 尾随下划线。"""
    import re
    if not profile_name:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "_", profile_name).upper() + "_"


def resolve_cc_lark_gates(profile_name: str = "") -> dict[str, str]:
    """解析 cc-lark 运行时 MCP 三个能力闸门，支持 per-profile 覆盖。

    只返回**显式设了值**的闸门键（per-profile 优先，回退全局）；没设的键不放进
    返回值 → 交给 cc_mcp_server._allow 默认放行。给 runner 注入 --mcp-config 用。
    """
    prefix = _profile_env_prefix(profile_name)
    gates: dict[str, str] = {}
    for flag in _CC_LARK_GATE_FLAGS:
        val = os.getenv(prefix + flag) if prefix else None
        if val is None:
            val = os.getenv(flag)
        if val is not None:
            gates[flag] = val
    return gates


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
