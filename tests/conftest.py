"""
全局 test fixtures。
确保所有测试使用临时目录存储 sessions，不污染 ~/.feishu-claude/sessions.json。
"""

import os
import sys
from pathlib import Path

import pytest

# 让 tests/ 下的测试能 import 主工程模块
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

import session_store as _ss


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    """自动隔离: 将 SESSIONS_DIR 指向临时目录"""
    monkeypatch.setattr(_ss, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(_ss, "LEGACY_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    # wake_me_in 的落盘也隔离到临时目录，别让测试往仓库 data/pending_wakes.json 写假记录
    # （否则下次 bot 启动会把它们当真唤醒去 fire）。
    monkeypatch.setenv("CC_LARK_WAKE_STORE", str(tmp_path / "pending_wakes.json"))
    # Telegram 会话缓冲同理：别让测试往 ~/.feishu-claude/tg/*.jsonl 里灌假消息
    # （那份文件是真上下文，会被下次启动读回去）。
    monkeypatch.setenv("CC_TG_BUFFER_DIR", str(tmp_path / "tg"))
    # 重启续跑的落盘同理：测试里跑的假 run 绝不能留在 data/pending_resumes.json，
    # 否则下次 bot 启动会真的去把"上次没跑完的活"投回群里。
    monkeypatch.setenv("CC_LARK_RESUME_STORE", str(tmp_path / "pending_resumes.json"))
    monkeypatch.setenv("CC_LARK_TASK_ROUTES", str(tmp_path / "task_routes.json"))
    monkeypatch.setenv("CC_LARK_TASK_RESULTS_DIR", str(tmp_path / "task_results"))
    # 会话移交简报同理：测试里的假移交不该在仓库 data/handovers/ 里堆文件。
    monkeypatch.setenv("CC_LARK_HANDOVER_DIR", str(tmp_path / "handovers"))
    # agy 账号快照 / keychain 同理，而且更凶：~/.gemini/accounts 里是**真凭证**，
    # keychain 那条 gemini/antigravity 就是本机 agy 的登录态。测试一律读不到真号、
    # 也写不动 keychain（要验证写入路径的用例自己 monkeypatch 回去）。
    import agy_account_switcher as _aas
    monkeypatch.setattr(_aas, "ACCOUNTS_DIR", str(tmp_path / "agy_accounts"))
    monkeypatch.setattr(_aas, "_read_keychain_raw", lambda: None)
    monkeypatch.setattr(_aas, "_write_keychain_raw",
                        lambda raw: (False, "test: keychain 写入已被隔离挡住"))
    monkeypatch.setattr(_aas, "_delete_keychain",
                        lambda: (False, "test: keychain 删除已被隔离挡住"))

    # 钉死凭证的 env（CLAUDE_CODE_OAUTH_TOKEN 等）同理：财务机的 .env 里真钉着一个
    # setup-token，而 pytest 会话里 bot_config 一被 import 就 load_dotenv(override=True)
    # 把它灌进 os.environ —— 于是"账户池被架空"的生产状态漏进测试，账户池相关用例
    # 在那台机器上集体变红。测试要验 pinned 行为的自己 monkeypatch.setenv 打开。
    for _pin_key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(_pin_key, raising=False)

    import resume_store as _rs
    _rs._ATTEMPTS.clear()
