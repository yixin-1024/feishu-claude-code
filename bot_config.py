import os
import shutil
from dotenv import load_dotenv

load_dotenv()

FEISHU_APP_ID = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET = os.environ["FEISHU_APP_SECRET"]

# 平台选择：feishu（中国版，默认）或 lark（国际版）
_DOMAIN_MAP = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
LARK_PLATFORM = os.getenv("LARK_PLATFORM", "feishu").lower()
if LARK_PLATFORM not in _DOMAIN_MAP:
    raise ValueError(f"LARK_PLATFORM 必须是 feishu 或 lark，当前: {LARK_PLATFORM}")
LARK_DOMAIN = _DOMAIN_MAP[LARK_PLATFORM]

CLAUDE_CLI = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"

DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-opus-4-6")
DEFAULT_CWD = os.path.expanduser(os.getenv("DEFAULT_CWD", "~"))
PERMISSION_MODE = os.getenv("PERMISSION_MODE", "bypassPermissions")

# 访问控制
# ALLOWED_OPEN_IDS: 允许使用 bot 的用户 open_id 列表（逗号分隔），为空则允许所有人（不推荐）
ALLOWED_OPEN_IDS: set[str] = {
    s.strip() for s in os.getenv("ALLOWED_OPEN_IDS", "").split(",") if s.strip()
}
# ALLOWED_GROUP_CHAT_IDS: 群聊白名单（逗号分隔的 chat_id，oc_ 开头），为空则禁用所有群聊
# 私聊始终允许（仍受 ALLOWED_OPEN_IDS 约束）
ALLOWED_GROUP_CHAT_IDS: set[str] = {
    s.strip() for s in os.getenv("ALLOWED_GROUP_CHAT_IDS", "").split(",") if s.strip()
}

SESSIONS_DIR = os.path.expanduser("~/.feishu-claude")

# 卡片按钮回调 HTTP 端口（需 ngrok 暴露）
CALLBACK_PORT = int(os.getenv("CALLBACK_PORT", "9981"))

# 流式卡片更新：每积累多少字符推送一次
STREAM_CHUNK_SIZE = int(os.getenv("STREAM_CHUNK_SIZE", "20"))
