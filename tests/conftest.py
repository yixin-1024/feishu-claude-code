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
    # 会话移交简报同理：测试里的假移交不该在仓库 data/handovers/ 里堆文件。
    monkeypatch.setenv("CC_LARK_HANDOVER_DIR", str(tmp_path / "handovers"))
    import resume_store as _rs
    _rs._ATTEMPTS.clear()
