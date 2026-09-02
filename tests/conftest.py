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
