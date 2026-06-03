"""per-profile claude env 覆盖（多供应商路由）测试。

覆盖两件事：
  1. bot_config._parse_claude_env_text 能吃 JSON {"env":{...}} / 扁平 JSON / dotenv
  2. run_claude(extra_env=...) 真把 env 注入 spawn 子进程，且 ANTHROPIC_MODEL
     覆盖命令行 --model（否则 claude-opus-4-x 发去 deepseek 会 404）
"""
import asyncio
import os
import sys

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")
os.environ["CLAUDE_RUNNER"] = "print"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_config
from bot_config import _parse_claude_env_text
from claude_runner import run_claude


DEEPSEEK = {
    "ANTHROPIC_AUTH_TOKEN": "sk-test",
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_MODEL": "deepseek-v4-pro",
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
}


# ── parser ────────────────────────────────────────────────────
def test_parse_json_env_block():
    text = '{"env": {"ANTHROPIC_BASE_URL": "https://x", "A": 1}}'
    assert _parse_claude_env_text(text) == {"ANTHROPIC_BASE_URL": "https://x", "A": "1"}


def test_parse_flat_json():
    assert _parse_claude_env_text('{"A": "1", "B": "2"}') == {"A": "1", "B": "2"}


def test_parse_dotenv():
    text = "ANTHROPIC_BASE_URL=https://x\nANTHROPIC_MODEL=foo\n# comment\n"
    assert _parse_claude_env_text(text) == {
        "ANTHROPIC_BASE_URL": "https://x",
        "ANTHROPIC_MODEL": "foo",
    }


def test_parse_empty():
    assert _parse_claude_env_text("") == {}
    assert _parse_claude_env_text("   \n  ") == {}


# ── loader: 缺失文件不抛 ───────────────────────────────────────
def test_load_missing_file_returns_empty():
    from bot_config import Profile
    p = Profile(name="x", app_id="a", app_secret="b", platform="lark",
                domain="d", default_cwd="/tmp", claude_env_file="/no/such/file.env")
    assert bot_config.load_claude_extra_env(p) == {}


def test_load_no_override_returns_empty():
    from bot_config import Profile
    p = Profile(name="x", app_id="a", app_secret="b", platform="lark",
                domain="d", default_cwd="/tmp")
    assert bot_config.load_claude_extra_env(p) == {}


# ── 注入 spawn ────────────────────────────────────────────────
class _FakeStdin:
    def __init__(self): self.buffer = b""; self.closed = False
    def write(self, d): self.buffer += d
    async def drain(self): return None
    def close(self): self.closed = True


class _FakeStdout:
    def __init__(self, lines): self._lines = list(lines); self._i = 0
    async def readline(self):
        if self._i >= len(self._lines): return b""
        line = self._lines[self._i]; self._i += 1; return line


class _FakeStderr:
    async def read(self): return b""


class _FakeProc:
    def __init__(self, lines):
        self.stdin = _FakeStdin(); self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr(); self.returncode = 0
    async def wait(self): return 0
    def kill(self): pass


def test_extra_env_injected_and_model_overridden(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args)
        captured["env"] = kwargs.get("env")
        return _FakeProc([
            b'{"type":"system","session_id":"sid_1"}\n',
            b'{"type":"result","session_id":"sid_1","result":"ok"}\n',
        ])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    text, sid, _ = asyncio.run(run_claude(
        "hi", model="claude-opus-4-6", extra_env=DEEPSEEK))

    assert text == "ok"
    env = captured["env"]
    # 供应商 env 注入到子进程
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test"
    assert env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
    # 父进程 PATH 等仍在（只是 update，不是替换）
    assert "PATH" in env
    # --model 被 ANTHROPIC_MODEL 覆盖成 deepseek，而不是传入的 claude-opus-4-6
    cmd = captured["cmd"]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "deepseek-v4-pro"
    assert "claude-opus-4-6" not in cmd


def test_no_extra_env_keeps_original_model(monkeypatch):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["cmd"] = list(args); captured["env"] = kwargs.get("env")
        return _FakeProc([
            b'{"type":"result","session_id":"s","result":"ok"}\n',
        ])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(run_claude("hi", model="claude-opus-4-6"))
    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-6"
    assert "ANTHROPIC_BASE_URL" not in (captured["env"].keys() - os.environ.keys())
