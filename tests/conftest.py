"""
全局 test fixtures。
确保所有测试使用临时目录存储 sessions，不污染 ~/.feishu-claude/sessions.json。
"""

import os
import tempfile

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

import session_store as _ss


@pytest.fixture(autouse=True)
def _isolate_sessions(tmp_path, monkeypatch):
    """自动隔离: 将 SESSIONS_DIR / SESSIONS_FILE 指向临时目录"""
    monkeypatch.setattr(_ss, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(_ss, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
