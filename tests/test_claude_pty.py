"""PTY 后端的单元测试 + 端到端集成测试。

集成测试用一个"假 claude"——真起一个 python 子进程，在 PTY 里运行，行为模拟
真实 claude：吐 TUI banner、读 stdin、把若干 assistant/tool_use 事件 append 到
~/.claude/projects/<encoded-cwd>/<sid>.jsonl。然后验证 run_claude 能：
  - 正确发现新 session_id
  - 实时回调 on_text_chunk / on_tool_use
  - 拿到 usage
  - 检测 end_turn + 静默 → 自然结束
"""

import asyncio
import json
import os
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("FEISHU_APP_ID", "test_app_id")
os.environ.setdefault("FEISHU_APP_SECRET", "test_app_secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import claude_pty  # noqa: E402
from claude_pty import (  # noqa: E402
    _encode_cwd_to_project_dir,
    _extract_text_from_content,
    _extract_tool_uses,
    _snapshot_session_uuids,
)


# ────────────────── 纯函数单测 ──────────────────

def test_encode_cwd_replaces_slashes():
    assert _encode_cwd_to_project_dir("/foo/bar/baz").endswith("-foo-bar-baz")


def test_encode_cwd_handles_trailing_slash():
    a = _encode_cwd_to_project_dir("/foo/bar")
    b = _encode_cwd_to_project_dir("/foo/bar/")
    # 末尾斜杠规范化掉后应该一致
    assert a == b


def test_extract_text_from_string_content():
    assert _extract_text_from_content("hello") == "hello"


def test_extract_text_from_list_content():
    content = [
        {"type": "thinking", "thinking": "noise"},
        {"type": "text", "text": "Hello "},
        {"type": "tool_use", "name": "Bash", "input": {}},
        {"type": "text", "text": "world"},
    ]
    assert _extract_text_from_content(content) == "Hello world"


def test_extract_text_handles_garbage():
    assert _extract_text_from_content(None) == ""
    assert _extract_text_from_content(42) == ""
    assert _extract_text_from_content([{"type": "text"}]) == ""  # 没 text key


def test_extract_tool_uses():
    content = [
        {"type": "thinking", "thinking": ""},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        {"type": "text", "text": "doing"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
    ]
    assert _extract_tool_uses(content) == [
        ("Bash", {"command": "ls"}),
        ("Read", {"file_path": "/x"}),
    ]


def test_extract_tool_uses_empty():
    assert _extract_tool_uses([]) == []
    assert _extract_tool_uses(None) == []
    assert _extract_tool_uses([{"type": "text", "text": "hi"}]) == []


def test_snapshot_session_uuids(tmp_path):
    # 只认 36 长度的 UUID + .jsonl
    (tmp_path / "abcdef12-1234-1234-1234-abcdef123456.jsonl").write_text("")
    (tmp_path / "not-a-uuid.jsonl").write_text("")
    (tmp_path / "random.txt").write_text("")
    seen = _snapshot_session_uuids(str(tmp_path))
    assert seen == {"abcdef12-1234-1234-1234-abcdef123456"}


def test_snapshot_session_uuids_missing_dir():
    assert _snapshot_session_uuids("/nonexistent/dir/xyz") == set()


# ────────────────── 集成测试：真 PTY + 假 claude ──────────────────

def _make_fake_claude(
    tmp_path: Path,
    project_dir: Path,
    *,
    text: str = "Hello from fake",
    tool_calls: list[tuple[str, dict]] | None = None,
    delay_before_done: float = 0.4,
    crash_after: bool = False,
) -> Path:
    """生成一个伪 `claude` 脚本，行为：
      - 起来后吐 TUI banner（让 _wait_pty_ready 看到）
      - 读 stdin（接收用户消息，可忽略内容）
      - 给 PROJECT_DIR 里写一个新 jsonl，按顺序写：user → [tool_use lines...] → assistant text → end_turn
    通过 env 变量传配置。
    """
    tool_calls = tool_calls or []
    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, sys, time, uuid

        PROJECT_DIR = {str(project_dir)!r}
        TEXT = {text!r}
        TOOL_CALLS = {tool_calls!r}
        DELAY = {delay_before_done!r}
        CRASH = {crash_after!r}

        # 模拟 TUI 启动
        sys.stdout.write("\\x1b[?1049h")  # alt screen
        sys.stdout.write("fake claude ready\\n")
        sys.stdout.flush()

        # 读 stdin 一段时间，等用户输入
        # bracketed paste 会发 \\x1b[200~msg\\x1b[201~\\r
        # 我们这里只是把 fd 读到 EOF / 第一段输入就跑
        try:
            import select
            buf = b""
            deadline = time.time() + 5.0
            while time.time() < deadline:
                r, _, _ = select.select([sys.stdin.fileno()], [], [], 0.3)
                if r:
                    chunk = os.read(sys.stdin.fileno(), 4096)
                    if not chunk:
                        break
                    buf += chunk
                    if b"\\x1b[201~" in buf or b"\\r" in buf:
                        break
        except Exception:
            pass

        # 还原真 claude 行为：把 bracketed-paste 包装去掉，拿到原始 prompt
        paste_text = buf.decode("utf-8", errors="replace") \\
            .replace("\\x1b[200~", "").replace("\\x1b[201~", "").strip("\\r\\n")

        # 写 jsonl
        sid = (sys.argv[sys.argv.index("--session-id") + 1]
               if "--session-id" in sys.argv else str(uuid.uuid4()))
        path = os.path.join(PROJECT_DIR, sid + ".jsonl")
        os.makedirs(PROJECT_DIR, exist_ok=True)
        with open(path, "a", buffering=1, encoding="utf-8") as f:
            f.write(json.dumps({{"type": "system", "sessionId": sid}}) + "\\n")
            f.flush()
            # 模拟真 claude：收到用户输入后立刻把它落到 jsonl 一条 user 行。
            # PTY runner 的 jsonl 归属逻辑依赖这条行存在——少了它，runner 会
            # 等不到自己的 jsonl（content==None → return None）然后超时。
            f.write(json.dumps({{
                "type": "user",
                "message": {{"role": "user", "content": [{{"type": "text", "text": paste_text}}]}},
            }}) + "\\n")
            f.flush()
            # 模拟一些工具调用
            for name, inp in TOOL_CALLS:
                time.sleep(0.15)
                f.write(json.dumps({{
                    "type": "assistant",
                    "message": {{
                        "role": "assistant",
                        "content": [{{"type": "tool_use", "name": name, "input": inp}}],
                        "stop_reason": "tool_use",
                        "usage": {{"input_tokens": 5, "output_tokens": 2}},
                    }},
                }}) + "\\n")
                f.flush()
                # 再补一条 user 行模拟 tool_result（实际无关紧要）
                f.write(json.dumps({{
                    "type": "user",
                    "message": {{"role": "user", "content": [{{"type": "tool_result", "content": "ok"}}]}},
                }}) + "\\n")
                f.flush()
            # 最终 assistant：text + end_turn
            time.sleep(DELAY)
            f.write(json.dumps({{
                "type": "assistant",
                "message": {{
                    "role": "assistant",
                    "content": [{{"type": "text", "text": TEXT}}],
                    "stop_reason": "end_turn",
                    "usage": {{"input_tokens": 7, "output_tokens": 9, "iterations": [{{
                        "input_tokens": 7, "output_tokens": 9
                    }}]}},
                }},
            }}) + "\\n")
            f.flush()
        if CRASH:
            os._exit(1)
        # 真 claude 会一直等下一个 prompt；我们这里也死等让 runner 来杀
        time.sleep(60)
    """)
    fake = tmp_path / "fake-claude.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)
    return fake


@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_pty_runner_end_to_end(tmp_path, monkeypatch):
    """跑一遍真 PTY + 假 claude → 验证 session_id 发现 / text 回调 / tool_use 回调 / usage 回调。"""
    project_root = tmp_path / "projects"
    project_root.mkdir()

    cwd = tmp_path / "workdir"
    cwd.mkdir()

    encoded = str(cwd).replace("/", "-")
    project_dir = project_root / encoded

    fake = _make_fake_claude(
        tmp_path,
        project_dir,
        text="Final response 🙂",
        tool_calls=[("Bash", {"command": "ls -l"}), ("Read", {"file_path": "/tmp/x"})],
        delay_before_done=0.3,
    )

    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    # 关掉 has_children 的真实检查，避免 pgrep 在 CI 里被防火墙
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)

    chunks: list[str] = []
    tools: list[tuple[str, dict]] = []
    usage_calls: list[dict] = []

    async def on_text(c):
        chunks.append(c)

    async def on_tool(name, inp):
        tools.append((name, inp))

    def on_usage(u):
        usage_calls.append(u)

    text, sid, used_fallback = asyncio.run(
        claude_pty.run_claude(
            "hello there",
            cwd=str(cwd),
            permission_mode="bypassPermissions",
            on_text_chunk=on_text,
            on_tool_use=on_tool,
            on_usage=on_usage,
        )
    )

    assert text == "Final response 🙂"
    assert sid is not None and len(sid) == 36
    assert used_fallback is False
    assert chunks == ["Final response 🙂"]
    assert ("Bash", {"command": "ls -l"}) in tools
    assert ("Read", {"file_path": "/tmp/x"}) in tools
    assert usage_calls and usage_calls[-1]["input_tokens"] == 7


@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_pty_runner_resume_existing_session(tmp_path, monkeypatch):
    """已有 session_id 时，runner 应直接 tail 已存在的 jsonl，且只读 offset 之后的增量。"""
    project_root = tmp_path / "projects"
    cwd = tmp_path / "wd"
    cwd.mkdir()
    encoded = str(cwd).replace("/", "-")
    project_dir = project_root / encoded
    project_dir.mkdir(parents=True)

    sid = str(uuid.uuid4())
    jsonl = project_dir / f"{sid}.jsonl"
    # 预先写入一些"历史"事件——runner 应该忽略它们
    jsonl.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "old"}}) + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "OLD TEXT (must not surface)"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }) + "\n"
    )

    # 假 claude：接收输入后 append 新事件到同一 jsonl
    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, sys, time, select
        PATH = {str(jsonl)!r}
        sys.stdout.write("ready\\n"); sys.stdout.flush()
        deadline = time.time() + 4.0
        buf = b""
        while time.time() < deadline:
            r, _, _ = select.select([sys.stdin.fileno()], [], [], 0.3)
            if r:
                c = os.read(sys.stdin.fileno(), 4096)
                if not c: break
                buf += c
                if b"\\x1b[201~" in buf or b"\\r" in buf:
                    break
        time.sleep(0.3)
        with open(PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({{
                "type": "assistant",
                "message": {{
                    "content": [{{"type": "text", "text": "RESUMED REPLY"}}],
                    "stop_reason": "end_turn",
                    "usage": {{"input_tokens": 3, "output_tokens": 4}},
                }},
            }}) + "\\n")
        time.sleep(30)
    """)
    fake = tmp_path / "fake.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)

    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)

    chunks: list[str] = []

    async def on_text(c):
        chunks.append(c)

    text, returned_sid, used_fallback = asyncio.run(
        claude_pty.run_claude(
            "continue",
            session_id=sid,
            cwd=str(cwd),
            permission_mode="bypassPermissions",
            on_text_chunk=on_text,
        )
    )

    assert text == "RESUMED REPLY"
    assert returned_sid == sid
    assert used_fallback is False
    assert "OLD TEXT" not in "".join(chunks)


# ────────────────── 用量耗尽快速退出 ──────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_pty_runner_raises_on_api_error_message(tmp_path, monkeypatch):
    """Claude Max 用量耗尽时 Claude CLI 会注入 synthetic assistant 事件
    （isApiErrorMessage=true / apiErrorStatus=429 / error="rate_limit"），
    runner 必须立刻抛 RuntimeError，而不是等 IDLE_TIMEOUT/STUCK_CHILD_TIMEOUT
    超时退出（否则 dispatcher 卡片会一直转 5 分钟才报错）。
    """
    project_root = tmp_path / "projects"
    project_root.mkdir()
    cwd = tmp_path / "wd"
    cwd.mkdir()
    encoded = str(cwd).replace("/", "-")
    project_dir = project_root / encoded
    project_dir.mkdir(parents=True)

    # fake claude: 收到 stdin 后写 system + user + 一条 isApiErrorMessage=true 的
    # synthetic assistant，然后死等（模拟真实 CLI 不退出的行为）。
    err_text = "You've hit your session limit · resets 6:40pm (Asia/Shanghai)"
    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, sys, time, select, uuid
        PROJECT_DIR = {str(project_dir)!r}
        ERR_TEXT = {err_text!r}

        sys.stdout.write("\\x1b[?1049h"); sys.stdout.write("ready\\n"); sys.stdout.flush()
        buf = b""
        deadline = time.time() + 5.0
        while time.time() < deadline:
            r, _, _ = select.select([sys.stdin.fileno()], [], [], 0.3)
            if r:
                c = os.read(sys.stdin.fileno(), 4096)
                if not c: break
                buf += c
                if b"\\x1b[201~" in buf or b"\\r" in buf:
                    break

        paste_text = buf.decode("utf-8", errors="replace") \\
            .replace("\\x1b[200~", "").replace("\\x1b[201~", "").strip("\\r\\n")
        sid = (sys.argv[sys.argv.index("--session-id") + 1]
               if "--session-id" in sys.argv else str(uuid.uuid4()))
        path = os.path.join(PROJECT_DIR, sid + ".jsonl")
        os.makedirs(PROJECT_DIR, exist_ok=True)
        with open(path, "a", buffering=1, encoding="utf-8") as f:
            f.write(json.dumps({{"type": "system", "sessionId": sid}}) + "\\n")
            f.write(json.dumps({{
                "type": "user",
                "message": {{"role": "user", "content": [{{"type": "text", "text": paste_text}}]}},
            }}) + "\\n")
            f.flush()
            time.sleep(0.2)
            # 注入 synthetic rate_limit 事件——CLI 真实 schema 一致
            f.write(json.dumps({{
                "type": "assistant",
                "message": {{
                    "role": "assistant",
                    "model": "<synthetic>",
                    "content": [{{"type": "text", "text": ERR_TEXT}}],
                    "stop_reason": "stop_sequence",
                    "stop_sequence": "",
                    "usage": {{"input_tokens": 0, "output_tokens": 0}},
                }},
                "error": "rate_limit",
                "isApiErrorMessage": True,
                "apiErrorStatus": 429,
            }}) + "\\n")
            f.flush()
        # 模拟真 CLI：注入完不退出，挂在 prompt 上等用户。runner 必须主动 raise
        time.sleep(60)
    """)
    fake = tmp_path / "fake-claude-ratelimit.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)

    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)

    start = time.time()
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(
            claude_pty.run_claude(
                "hello",
                cwd=str(cwd),
                permission_mode="bypassPermissions",
            )
        )
    elapsed = time.time() - start
    # 必须远快于 NO_ASSISTANT_TIMEOUT=600s / IDLE_TIMEOUT=300s
    assert elapsed < 30, f"应在几秒内退出，实际 {elapsed:.1f}s"
    msg = str(exc_info.value)
    assert "用量已达上限" in msg or "rate_limit" in msg
    assert "session limit" in msg


# ────────────────── 大消息 bracketed-paste 完整性 ──────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_send_user_input_large_paste_not_truncated(tmp_path, monkeypatch):
    """thread context + 邮件全文场景：>8KB 多行 message 必须**整段**到达 fake
    claude，不能被 PTY MAX_INPUT (1024B) 截断。

    fake claude 读 stdin 直到看到 \\x1b[201~ 收尾符，把读到的 bytes 写一份到
    sidecar 文件——测试验证 sidecar 内容包含完整的 user payload。
    """
    project_root = tmp_path / "projects"
    project_root.mkdir()
    cwd = tmp_path / "wd"
    cwd.mkdir()

    encoded = str(cwd).replace("/", "-")
    project_dir = project_root / encoded
    project_dir.mkdir(parents=True)

    sidecar = tmp_path / "stdin-capture.bin"

    # 构造一个 ~8KB 的多行 message——比 macOS MAX_INPUT (1024B) 大得多，且
    # 内嵌换行以触发 bracketed-paste 分支
    payload_lines = [
        f"line-{i:04d}: {'x' * 80}" for i in range(96)
    ]
    big_message = "\n".join(payload_lines)
    assert len(big_message.encode()) > 8000

    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, sys, time, select, uuid
        PROJECT_DIR = {str(project_dir)!r}
        SIDECAR = {str(sidecar)!r}

        sys.stdout.write("ready\\n"); sys.stdout.flush()

        buf = b""
        deadline = time.time() + 8.0
        end_marker = b"\\x1b[201~"
        while time.time() < deadline:
            r, _, _ = select.select([sys.stdin.fileno()], [], [], 0.3)
            if r:
                c = os.read(sys.stdin.fileno(), 65536)
                if not c:
                    break
                buf += c
                if end_marker in buf:
                    break

        # 落盘 + 写 jsonl 触发 runner 收尾
        with open(SIDECAR, "wb") as f:
            f.write(buf)

        sid = (sys.argv[sys.argv.index("--session-id") + 1]
               if "--session-id" in sys.argv else str(uuid.uuid4()))
        path = os.path.join(PROJECT_DIR, sid + ".jsonl")
        os.makedirs(PROJECT_DIR, exist_ok=True)
        with open(path, "a", buffering=1, encoding="utf-8") as f:
            f.write(json.dumps({{"type": "system", "sessionId": sid}}) + "\\n")
            f.write(json.dumps({{
                "type": "assistant",
                "message": {{
                    "content": [{{"type": "text", "text": "ok"}}],
                    "stop_reason": "end_turn",
                    "usage": {{"input_tokens": 1, "output_tokens": 1}},
                }},
            }}) + "\\n")
        time.sleep(30)
    """)
    fake = tmp_path / "fake-claude-bigpaste.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)

    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)

    asyncio.run(
        claude_pty.run_claude(
            big_message,
            cwd=str(cwd),
            permission_mode="bypassPermissions",
        )
    )

    assert sidecar.exists(), "fake claude 没收到任何 stdin"
    captured = sidecar.read_bytes()
    assert b"\x1b[200~" in captured, "缺少 bracketed-paste 起始符"
    assert b"\x1b[201~" in captured, "缺少 bracketed-paste 结束符——超过 MAX_INPUT 的部分被丢了"
    # 整段 payload 应该完整出现在 paste 内（line-0000 ... line-0095 全部）
    assert b"line-0000:" in captured
    assert b"line-0095:" in captured
    # 内容长度至少和原始 payload 一样
    assert len(captured) >= len(big_message.encode())


@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_fresh_session_resends_full_input_when_first_paste_is_swallowed(tmp_path, monkeypatch):
    """如果 Claude TUI 吞掉第一次输入，runner 应清空输入框并重发整段消息。"""
    project_root = tmp_path / "projects"
    project_root.mkdir()
    cwd = tmp_path / "wd"
    cwd.mkdir()

    encoded = str(cwd).replace("/", "-")
    project_dir = project_root / encoded
    project_dir.mkdir(parents=True)
    sidecar = tmp_path / "stdin-capture.bin"

    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, select, sys, time, uuid
        PROJECT_DIR = {str(project_dir)!r}
        SIDECAR = {str(sidecar)!r}

        sys.stdout.write("\\x1b[?1049hfake claude ready\\n"); sys.stdout.flush()

        buf = b""
        saw_first_enter = False
        deadline = time.time() + 8.0
        while time.time() < deadline:
            r, _, _ = select.select([sys.stdin.fileno()], [], [], 0.3)
            if not r:
                continue
            chunk = os.read(sys.stdin.fileno(), 65536)
            if not chunk:
                break
            buf += chunk
            if b"\\n" not in buf and b"\\r" not in buf:
                continue
            if not saw_first_enter:
                # 第一次输入被 TUI 吞掉：不写 JSONL，继续等 runner 重发。
                saw_first_enter = True
                buf = b""
                continue
            break

        with open(SIDECAR, "wb") as f:
            f.write(buf)

        paste_text = buf.decode("utf-8", errors="replace") \\
            .replace("\\x15", "") \\
            .replace("\\x1b[200~", "").replace("\\x1b[201~", "").strip("\\r\\n")
        sid = (sys.argv[sys.argv.index("--session-id") + 1]
               if "--session-id" in sys.argv else str(uuid.uuid4()))
        path = os.path.join(PROJECT_DIR, sid + ".jsonl")
        with open(path, "a", buffering=1, encoding="utf-8") as f:
            f.write(json.dumps({{"type": "system", "sessionId": sid}}) + "\\n")
            f.write(json.dumps({{
                "type": "user",
                "message": {{"role": "user", "content": [{{"type": "text", "text": paste_text}}]}},
            }}) + "\\n")
            f.write(json.dumps({{
                "type": "assistant",
                "message": {{
                    "role": "assistant",
                    "content": [{{"type": "text", "text": "resend worked"}}],
                    "stop_reason": "end_turn",
                    "usage": {{"input_tokens": 1, "output_tokens": 1}},
                }},
            }}) + "\\n")
        time.sleep(30)
    """)
    fake = tmp_path / "fake-claude-resend.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)

    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)
    monkeypatch.setattr(claude_pty, "_RESUBMIT_INTERVAL", 0.5)
    monkeypatch.setattr(claude_pty, "_NEW_SESSION_WAIT", 5.0)
    monkeypatch.setattr(claude_pty, "_PTY_INIT_GRACE", 0.1)

    text, sid, used_fallback = asyncio.run(
        claude_pty.run_claude(
            "hello after swallowed paste",
            cwd=str(cwd),
            permission_mode="bypassPermissions",
        )
    )

    assert text == "resend worked"
    assert sid is not None
    assert used_fallback is False
    captured = sidecar.read_bytes()
    assert b"hello after swallowed paste" in captured


# ────────────────── slash 转义单测 ──────────────────

def test_escape_for_pty_passes_through_normal_text():
    from claude_pty import _escape_for_pty
    assert _escape_for_pty("hello") == "hello"
    assert _escape_for_pty("Bash a command") == "Bash a command"
    assert _escape_for_pty("") == ""


def test_escape_for_pty_prefixes_slash_commands():
    from claude_pty import _escape_for_pty
    # 防止 PTY claude 把 /pwd /xxx 当成内置 slash 命令
    assert _escape_for_pty("/pwd") == " /pwd"
    assert _escape_for_pty("/run something") == " /run something"


def test_escape_for_pty_prefixes_bang_and_hash():
    from claude_pty import _escape_for_pty
    # ! 进 shell 模式、# 写 memory，都屏蔽
    assert _escape_for_pty("!ls") == " !ls"
    assert _escape_for_pty("#memory note") == " #memory note"


def test_escape_for_pty_passes_through_compact():
    from claude_pty import _escape_for_pty
    # /compact 在白名单里：透传给 claude 内置，触发真正的 compact
    assert _escape_for_pty("/compact") == "/compact"
    assert _escape_for_pty("/compact ") == "/compact "
    assert _escape_for_pty("/compact focus on db schema") == "/compact focus on db schema"
    # 但 /compactfoo 不是 /compact，仍应转义
    assert _escape_for_pty("/compactfoo") == " /compactfoo"


# ────────────────── jsonl 归属匹配 (verify 模板回归) ──────────────────

def test_jsonl_owns_message_ignores_collapsed_blank_lines(tmp_path):
    """/verify 模板首行是 "# 任务...\\n\\n你是一个..."；Claude Code PTY 把 \\n\\n 压成
    单 \\n 写进 jsonl。expected_prefix 不归一化空白就永远匹配不上，超时报"new
    session jsonl never appeared"。这条用例锁住归一化路径。"""
    from claude_pty import _expected_match_prefix, _jsonl_owns_message

    message = "# 任务：审计上方这段对话\n\n你是一个**独立审计员**，从外部接入这个话题群。"
    prefix = _expected_match_prefix(message)
    assert prefix is not None
    # 归一化后 \n\n 变成单空格
    assert "  " not in prefix
    assert "\n" not in prefix

    # 模拟 PTY 压缩后 + 头部 [Image #N] 占位 的真实 jsonl 内容
    jsonl = tmp_path / "fake.jsonl"
    jsonl.write_text(
        '{"type":"user","message":{"role":"user","content":'
        '[{"type":"text","text":"[Image #1] [Image #2] # 任务：审计上方这段对话\\n你是一个**独立审计员**，从外部接入这个话题群。"}]}}\n'
    )
    assert _jsonl_owns_message(str(jsonl), prefix, only_candidate=True) is True

    # 别的并发 spawn 写的不应被认领
    other = tmp_path / "other.jsonl"
    other.write_text(
        '{"type":"user","message":{"role":"user","content":'
        '[{"type":"text","text":"完全不相干的另一个用户输入"}]}}\n'
    )
    assert _jsonl_owns_message(str(other), prefix, only_candidate=False) is False


def test_jsonl_owns_message_waits_when_content_unwritten(tmp_path):
    """2026-05-24 真实事故回归：并发 spawn 时不准走 only_candidate 捷径。

    场景：两个定时任务 A/B 在 17:45 同时 fire。
    patrol（A）spawn 后拍 before_uuids snapshot，optimize（B）的 jsonl 比 A 自己
    的先落盘（A 的 PTY init 慢半拍），此时 B 还在 PTY bracketed-paste，user 行
    没写出来。A 看到 new_uuids = {B.uuid}、only_candidate=True、content=None，
    旧逻辑直接 return True → A 把 B 的 jsonl 认作自己的，从此 tail 别人的输出，
    两个会话的卡片正文完全串台。

    修复后：有 expected_prefix 时 content==None 一律 return None，必须等
    content 落地再用 prefix 校验。
    """
    from claude_pty import _jsonl_owns_message

    # 文件存在但还没写任何 user 行（只有 permission-mode / file-history-snapshot 类
    # 系统事件，或者完全空文件）
    fresh = tmp_path / "racing.jsonl"
    fresh.write_text(
        '{"type":"permission-mode","mode":"acceptEdits"}\n'
        '{"type":"file-history-snapshot"}\n'
    )

    # 有 prefix + content 未写：必须等（None），不准因 only_candidate 而误认
    assert _jsonl_owns_message(str(fresh), "你是 **SPX E2E 巡逻主 agent**", only_candidate=True) is None
    assert _jsonl_owns_message(str(fresh), "你是 **SPX E2E 巡逻主 agent**", only_candidate=False) is None

    # 短消息（无 prefix）保留 only_candidate 兜底：单候选可放行，多候选等
    assert _jsonl_owns_message(str(fresh), None, only_candidate=True) is True
    assert _jsonl_owns_message(str(fresh), None, only_candidate=False) is None


# ────────────────── orphan-resume 修复回归 ──────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_pty_runner_orphan_resume_falls_back_to_fresh_session(tmp_path, monkeypatch):
    """复现 2026-05-17 13:43 的 bug：上一轮被 watchdog 杀，session JSONL 里留下
    孤儿 tool_result。claude --resume 检测到后自动注入一对 housekeeping 事件：
      USER  isMeta=True  "Continue from where you left off."
      ASSIST stop_sequence='' "No response requested."
    然后 claude 退出，没处理真实用户输入。

    修复前：dispatcher 把 "No response requested." 当成回复贴给用户。
    修复后：runner 过滤掉这对事件，full_text 为空 → 触发 fresh session 重试 →
            用户拿到真正的回答。
    """
    project_root = tmp_path / "projects"
    cwd = tmp_path / "wd"
    cwd.mkdir()
    encoded = str(cwd).replace("/", "-")
    project_dir = project_root / encoded
    project_dir.mkdir(parents=True)

    # 构造一个"被 watchdog 杀掉"的 session JSONL：tool_result 后没有 assistant 收尾
    broken_sid = str(uuid.uuid4())
    broken_jsonl = project_dir / f"{broken_sid}.jsonl"
    broken_jsonl.write_text(
        json.dumps({"type": "system", "sessionId": broken_sid}) + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/x"}}],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        }) + "\n"
        + json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "tool_result", "content": "file contents"}]},
        }) + "\n"
    )

    # 假 claude：看到 --resume <broken_sid> → 行为像真 claude 那样注入 housekeeping 后退出；
    #          看到没有 --resume → 行为正常，给真实回复。
    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, sys, time, select, uuid
        PROJECT_DIR = {str(project_dir)!r}
        BROKEN_SID = {broken_sid!r}
        BROKEN_PATH = {str(broken_jsonl)!r}

        is_resume = "--resume" in sys.argv

        sys.stdout.write("ready\\n"); sys.stdout.flush()

        # 等用户输入吃完——orphan-resume 模式下 claude 也会读 stdin，只是不处理
        buf = b""
        deadline = time.time() + 4.0
        while time.time() < deadline:
            r, _, _ = select.select([sys.stdin.fileno()], [], [], 0.3)
            if r:
                c = os.read(sys.stdin.fileno(), 65536)
                if not c: break
                buf += c
                if b"\\x1b[201~" in buf or b"\\r" in buf:
                    break

        if is_resume:
            # 模拟 Claude CLI 的 orphan-resume housekeeping：synthetic user + dummy assistant
            with open(BROKEN_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({{
                    "type": "user",
                    "isMeta": True,
                    "message": {{
                        "role": "user",
                        "content": [{{"type": "text", "text": "Continue from where you left off."}}],
                    }},
                }}) + "\\n")
                f.write(json.dumps({{
                    "type": "assistant",
                    "message": {{
                        "content": [{{"type": "text", "text": "No response requested."}}],
                        "stop_reason": "stop_sequence",
                        "stop_sequence": "",
                        "usage": {{"input_tokens": 1, "output_tokens": 4, "iterations": [{{
                            "input_tokens": 1, "output_tokens": 4
                        }}]}},
                    }},
                }}) + "\\n")
                # 真实用户输入也落 JSONL 但不响应
                f.write(json.dumps({{
                    "type": "user",
                    "message": {{"role": "user", "content": "继续，搞完了吗"}},
                }}) + "\\n")
            # claude --resume 退出
            os._exit(0)
        else:
            # 全新 session：给个像样的回复
            paste_text = buf.decode("utf-8", errors="replace") \\
                .replace("\\x1b[200~", "").replace("\\x1b[201~", "").strip("\\r\\n")
            sid = (sys.argv[sys.argv.index("--session-id") + 1]
                   if "--session-id" in sys.argv else str(uuid.uuid4()))
            path = os.path.join(PROJECT_DIR, sid + ".jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({{"type": "system", "sessionId": sid}}) + "\\n")
                # 模拟真 claude：先把用户输入回写一条 user 行，再吐 assistant
                f.write(json.dumps({{
                    "type": "user",
                    "message": {{"role": "user", "content": [{{"type": "text", "text": paste_text}}]}},
                }}) + "\\n")
                f.write(json.dumps({{
                    "type": "assistant",
                    "message": {{
                        "content": [{{"type": "text", "text": "Fresh session real reply ✅"}}],
                        "stop_reason": "end_turn",
                        "usage": {{"input_tokens": 3, "output_tokens": 6, "iterations": [{{
                            "input_tokens": 3, "output_tokens": 6
                        }}]}},
                    }},
                }}) + "\\n")
            time.sleep(30)
    """)
    fake = tmp_path / "fake-claude-orphan.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)

    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)

    chunks: list[str] = []

    async def on_text(c):
        chunks.append(c)

    text, returned_sid, used_fallback = asyncio.run(
        claude_pty.run_claude(
            "继续，搞完了吗",
            session_id=broken_sid,
            cwd=str(cwd),
            permission_mode="bypassPermissions",
            on_text_chunk=on_text,
        )
    )

    # 修复后预期：
    #   1. 用户绝不应看到 "No response requested." —— 那是 Claude CLI 的内部 housekeeping
    #   2. orphan-resume + 空 full_text 应触发 fresh session 重试
    #   3. 最终拿到的是 fresh session 的真实回复
    assert "No response requested" not in text, (
        f"修复未生效：housekeeping dummy 漏过去了。text={text!r}"
    )
    assert "No response requested" not in "".join(chunks)
    assert text == "Fresh session real reply ✅"
    assert used_fallback is True
    assert returned_sid != broken_sid


# ────────────────── watchdog-hung resume 死循环修复回归 ──────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_pty_runner_resume_hung_by_watchdog_falls_back_to_fresh_session(tmp_path, monkeypatch):
    """复现 2026-05-17 14:33 cc-lark 群里那次死循环：watchdog kill 一次后，
    旧 session_id 还留在 session_store 里，用户"继续执行"还用同一个 id 再 resume，
    立刻撞同一个 dirty conversation state、又被 kill。

    修复前：watchdog 抛的 RuntimeError 直接冒泡到 dispatcher，session_id 不变，
            下一次"继续执行"必撞同一个坑。
    修复后：run_claude 接住 watchdog 的 RuntimeError，resume 模式下自动 fresh
            fallback，new_session_id 切到新的，store 自动覆盖。
    """
    project_root = tmp_path / "projects"
    cwd = tmp_path / "wd"
    cwd.mkdir()
    encoded = str(cwd).replace("/", "-")
    project_dir = project_root / encoded
    project_dir.mkdir(parents=True)

    # 上一轮被 watchdog 杀掉留下的 broken session jsonl
    broken_sid = str(uuid.uuid4())
    broken_jsonl = project_dir / f"{broken_sid}.jsonl"
    broken_jsonl.write_text(
        json.dumps({"type": "system", "sessionId": broken_sid}) + "\n"
    )

    # 假 claude：
    #   resume 模式 → 吐 ready 吃完 stdin 后 select 阻塞，CPU=0、JSONL 不增长、
    #                 PTY tail 不增长 → watchdog 三信号全 0，必触发 hung
    #   fresh 模式  → 正常回复
    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, select, sys, time, uuid
        PROJECT_DIR = {str(project_dir)!r}
        is_resume = "--resume" in sys.argv

        sys.stdout.write("ready\\n"); sys.stdout.flush()

        # 吃 stdin（runner 会发 bracketed-paste + \\r）
        buf = b""
        deadline = time.time() + 3.0
        while time.time() < deadline:
            r, _, _ = select.select([sys.stdin.fileno()], [], [], 0.2)
            if r:
                c = os.read(sys.stdin.fileno(), 65536)
                if not c: break
                buf += c
                if b"\\x1b[201~" in buf or b"\\r" in buf:
                    break

        if is_resume:
            # 完全静默——select 阻塞 0 CPU；watchdog 会 kill 我
            select.select([], [], [], 60.0)
            os._exit(0)
        else:
            sid = (sys.argv[sys.argv.index("--session-id") + 1]
                   if "--session-id" in sys.argv else str(uuid.uuid4()))
            path = os.path.join(PROJECT_DIR, sid + ".jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({{"type": "system", "sessionId": sid}}) + "\\n")
                # 真 claude 接受输入后会先回写一条 user 行（runner 据此判定输入到位）
                f.write(json.dumps({{
                    "type": "user",
                    "message": {{"role": "user", "content": [{{"type": "text", "text": "继续执行"}}]}},
                }}) + "\\n")
                f.write(json.dumps({{
                    "type": "assistant",
                    "message": {{
                        "content": [{{"type": "text", "text": "Fresh after hung ✅"}}],
                        "stop_reason": "end_turn",
                        "usage": {{"input_tokens": 1, "output_tokens": 2, "iterations": [{{
                            "input_tokens": 1, "output_tokens": 2
                        }}]}},
                    }},
                }}) + "\\n")
            time.sleep(10)
    """)
    fake = tmp_path / "fake-claude-watchdog.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)

    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)
    # 缩短 watchdog 让测试在秒级触发。这个假 resume 永远静默、从不产出 assistant
    # → resume_first_response_pending 全程为真 → 走 WATCHDOG_RESUME_FIRST_STALL 阈值，
    # 必须一并 patch 小，否则要等 300s 才被抓（验证的是「真卡死 resume 仍走 fresh 兜底」）
    monkeypatch.setattr(claude_pty, "WATCHDOG_INTERVAL", 0.5)
    monkeypatch.setattr(claude_pty, "WATCHDOG_MAX_STALL", 1.5)
    monkeypatch.setattr(claude_pty, "WATCHDOG_RESUME_FIRST_STALL", 1.5)

    text, returned_sid, used_fallback = asyncio.run(
        claude_pty.run_claude(
            "继续执行",
            session_id=broken_sid,
            cwd=str(cwd),
            permission_mode="bypassPermissions",
        )
    )

    assert text == "Fresh after hung ✅", (
        f"watchdog hung 之后没走 fresh fallback，text={text!r}"
    )
    assert used_fallback is True
    assert returned_sid is not None and returned_sid != broken_sid, (
        f"应该是 fresh 起的新 sid，实际 returned_sid={returned_sid}"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_pty_runner_slow_resume_first_response_not_killed(tmp_path, monkeypatch):
    """正向回归：合法的「大上下文 resume 首响应等待期」不能被 watchdog 误杀。

    线上真因：--resume 几百 KB~MB 的 session 后，CLI 把整段对话发给 Anthropic、在等
    第一个 token，这段 CPU/PTY/JSONL 全 0 的纯静默单独就能 >90s。base 90s 阈值会把它
    当 hung 杀掉 → fresh 兜底开新会话、上下文全丢（「9 次 resume 撞穿被迫开新会话」）。

    修复后：resume 首响应期用更宽松的 WATCHDOG_RESUME_FIRST_STALL，静默熬过 base 阈值
    后第一条真实 assistant 落地 → 正常返回真实回复，不走 fresh 兜底、sid 不变。

    这里把 base=0.8s（会杀） < 静默 1.5s < resume=4.0s（放行）卡在中间，证明正是
    「resume 宽限阈值」在起作用。
    """
    project_root = tmp_path / "projects"
    cwd = tmp_path / "wd"
    cwd.mkdir()
    encoded = str(cwd).replace("/", "-")
    project_dir = project_root / encoded
    project_dir.mkdir(parents=True)

    # 上一轮留下的 resume session jsonl（首响应慢，但 session 本身是好的）
    resume_sid = str(uuid.uuid4())
    resume_jsonl = project_dir / f"{resume_sid}.jsonl"
    resume_jsonl.write_text(
        json.dumps({"type": "system", "sessionId": resume_sid}) + "\n"
    )

    # 假 claude：
    #   resume 模式 → 吃完 stdin 后静默 1.5s（模拟首 token 等待，0 CPU/0 JSONL/0 PTY），
    #                 超过 base 0.8s 但在 resume 4.0s 内，然后追加真实 user+assistant 后退出
    #   fresh 模式  → 写个可区分文本（一旦走到这里说明被误杀降级了，断言会失败）
    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, select, sys, time, uuid
        PROJECT_DIR = {str(project_dir)!r}
        argv = sys.argv
        is_resume = "--resume" in argv
        resume_sid = argv[argv.index("--resume") + 1] if is_resume else None

        sys.stdout.write("ready\\n"); sys.stdout.flush()

        buf = b""
        deadline = time.time() + 3.0
        while time.time() < deadline:
            r, _, _ = select.select([sys.stdin.fileno()], [], [], 0.2)
            if r:
                c = os.read(sys.stdin.fileno(), 65536)
                if not c: break
                buf += c
                if b"\\x1b[201~" in buf or b"\\r" in buf:
                    break

        if is_resume:
            # 首响应静默等待——三信号全 0，单独就能撞穿 base 阈值
            select.select([], [], [], 1.5)
            path = os.path.join(PROJECT_DIR, resume_sid + ".jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({{
                    "type": "user",
                    "message": {{"role": "user", "content": "继续执行"}},
                }}) + "\\n")
                f.write(json.dumps({{
                    "type": "assistant",
                    "message": {{
                        "content": [{{"type": "text", "text": "Slow resume survived ✅"}}],
                        "stop_reason": "end_turn",
                        "usage": {{"input_tokens": 1, "output_tokens": 2, "iterations": [{{
                            "input_tokens": 1, "output_tokens": 2
                        }}]}},
                    }},
                }}) + "\\n")
            time.sleep(10)
        else:
            sid = (sys.argv[sys.argv.index("--session-id") + 1]
                   if "--session-id" in sys.argv else str(uuid.uuid4()))
            path = os.path.join(PROJECT_DIR, sid + ".jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({{"type": "system", "sessionId": sid}}) + "\\n")
                f.write(json.dumps({{
                    "type": "user",
                    "message": {{"role": "user", "content": [{{"type": "text", "text": "继续执行"}}]}},
                }}) + "\\n")
                f.write(json.dumps({{
                    "type": "assistant",
                    "message": {{
                        "content": [{{"type": "text", "text": "WRONGLY FELL BACK TO FRESH"}}],
                        "stop_reason": "end_turn",
                        "usage": {{"input_tokens": 1, "output_tokens": 2, "iterations": [{{
                            "input_tokens": 1, "output_tokens": 2
                        }}]}},
                    }},
                }}) + "\\n")
            time.sleep(10)
    """)
    fake = tmp_path / "fake-claude-slow-resume.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)

    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)
    # base 0.8s 会杀，resume 宽限 4.0s 放行；静默 1.5s 卡在中间。
    # _DONE_QUIET_PERIOD 调小，assistant 落地后快速收尾，避免 base 阈值在收尾静默里误触
    monkeypatch.setattr(claude_pty, "WATCHDOG_INTERVAL", 0.3)
    monkeypatch.setattr(claude_pty, "WATCHDOG_MAX_STALL", 0.8)
    monkeypatch.setattr(claude_pty, "WATCHDOG_RESUME_FIRST_STALL", 4.0)
    monkeypatch.setattr(claude_pty, "_DONE_QUIET_PERIOD", 0.2)

    text, returned_sid, used_fallback = asyncio.run(
        claude_pty.run_claude(
            "继续执行",
            session_id=resume_sid,
            cwd=str(cwd),
            permission_mode="bypassPermissions",
        )
    )

    assert text == "Slow resume survived ✅", (
        f"慢 resume 首响应被误杀（或降级 fresh），text={text!r}"
    )
    assert used_fallback is False, "合法慢 resume 不应触发 fresh 兜底"
    assert returned_sid == resume_sid, (
        f"应沿用原 resume sid，实际 returned_sid={returned_sid}"
    )


# ────────────────── forced --session-id 防串台 (本次根因修复) ──────────────────

def test_jsonl_has_real_user_event(tmp_path):
    """forced-session 输入到位判定：真实 user 行（含纯图片）算数，
    system / 纯 tool_result / isMeta 收尾不算。"""
    from claude_pty import _jsonl_has_real_user_event

    # 只有 system 事件——输入还没到位
    only_sys = tmp_path / "sys.jsonl"
    only_sys.write_text(json.dumps({"type": "system", "sessionId": "x"}) + "\n")
    assert _jsonl_has_real_user_event(str(only_sys)) is False

    # 文本 user 行 → 到位
    text_user = tmp_path / "text.jsonl"
    text_user.write_text(
        json.dumps({"type": "system", "sessionId": "x"}) + "\n"
        + json.dumps({"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": "你好"}]}}) + "\n"
    )
    assert _jsonl_has_real_user_event(str(text_user)) is True

    # 纯图片 user 行（无 text 块）也算到位——带截图无正文的消息不能被误判没到位
    img_user = tmp_path / "img.jsonl"
    img_user.write_text(
        json.dumps({"type": "user", "message": {"role": "user",
            "content": [{"type": "image", "source": {}}]}}) + "\n"
    )
    assert _jsonl_has_real_user_event(str(img_user)) is True

    # isMeta 收尾 + 纯 tool_result 都不算
    meta_tool = tmp_path / "meta.jsonl"
    meta_tool.write_text(
        json.dumps({"type": "user", "isMeta": True, "message": {"role": "user",
            "content": [{"type": "text", "text": "Continue from where you left off."}]}}) + "\n"
        + json.dumps({"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "content": "ok"}]}}) + "\n"
    )
    assert _jsonl_has_real_user_event(str(meta_tool)) is False

    # 文件不存在 → False（不抛）
    assert _jsonl_has_real_user_event(str(tmp_path / "nope.jsonl")) is False


@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_fresh_session_adopts_cli_generated_session_id(tmp_path, monkeypatch):
    """fresh 默认不强制 --session-id，由 Claude CLI 自己生成 sid。

    runner 通过本轮 user 内容匹配接管实际生成的 JSONL，避免 Claude Code 新版
    交互式 TUI 不按 forced id 落盘时直接报 "new session jsonl never appeared"。
    """
    project_root = tmp_path / "projects"
    project_root.mkdir()
    cwd = tmp_path / "wd"
    cwd.mkdir()
    encoded = str(cwd).replace("/", "-")
    project_dir = project_root / encoded
    project_dir.mkdir(parents=True)
    sidecar = tmp_path / "argv.json"

    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, sys, time, select, uuid
        PROJECT_DIR = {str(project_dir)!r}
        SIDECAR = {str(sidecar)!r}
        json.dump(sys.argv, open(SIDECAR, "w"))
        sys.stdout.write("\\x1b[?1049hready\\n"); sys.stdout.flush()
        buf = b""; deadline = time.time() + 5.0
        while time.time() < deadline:
            r,_,_ = select.select([sys.stdin.fileno()], [], [], 0.2)
            if r:
                c = os.read(sys.stdin.fileno(), 65536)
                if not c: break
                buf += c
                if b"\\x1b[201~" in buf or b"\\r" in buf: break
        paste = buf.decode("utf-8", errors="replace").replace("\\x1b[200~","") \\
            .replace("\\x1b[201~","").strip("\\r\\n")
        sid = str(uuid.uuid4())
        path = os.path.join(PROJECT_DIR, sid + ".jsonl")
        with open(path, "a", buffering=1, encoding="utf-8") as f:
            f.write(json.dumps({{"type": "system", "sessionId": sid}}) + "\\n")
            f.write(json.dumps({{"type": "user", "message": {{"role": "user",
                "content": [{{"type": "text", "text": paste}}]}}}}) + "\\n")
            f.write(json.dumps({{"type": "assistant", "message": {{"role": "assistant",
                "content": [{{"type": "text", "text": "generated ok"}}], "stop_reason": "end_turn",
                "usage": {{"input_tokens": 1, "output_tokens": 1,
                    "iterations": [{{"input_tokens": 1, "output_tokens": 1}}]}}}}}}) + "\\n")
        time.sleep(30)
    """)
    fake = tmp_path / "fake-generated.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)

    monkeypatch.delenv("CLAUDE_PTY_FORCE_SESSION_ID", raising=False)
    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)

    text, sid, used_fallback = asyncio.run(
        claude_pty.run_claude(
            "hello generated session",
            cwd=str(cwd),
            permission_mode="bypassPermissions",
            effort="xhigh",
        )
    )

    argv = json.load(open(sidecar))
    assert "--session-id" not in argv
    assert argv[argv.index("--effort") + 1] == "xhigh"
    assert sid is not None and len(sid) == 36
    assert (project_dir / f"{sid}.jsonl").is_file()
    assert text == "generated ok"
    assert used_fallback is False


@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_fresh_session_force_session_id_env_still_supported(tmp_path, monkeypatch):
    """CLAUDE_PTY_FORCE_SESSION_ID=1 时仍可用 --session-id 钉死 session 文件名：
      - runner 给 claude 传了 --session-id <uuid>
      - 返回的 sid == 那个 uuid
      - jsonl 真的落在 <uuid>.jsonl（路径事前确定，零扫描）
    fake 把收到的 argv dump 到 sidecar 验证。"""
    project_root = tmp_path / "projects"
    project_root.mkdir()
    cwd = tmp_path / "wd"
    cwd.mkdir()
    encoded = str(cwd).replace("/", "-")
    project_dir = project_root / encoded
    project_dir.mkdir(parents=True)
    sidecar = tmp_path / "argv.json"

    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, sys, time, select, uuid
        PROJECT_DIR = {str(project_dir)!r}
        SIDECAR = {str(sidecar)!r}
        json.dump(sys.argv, open(SIDECAR, "w"))
        sys.stdout.write("\\x1b[?1049hready\\n"); sys.stdout.flush()
        buf = b""; deadline = time.time() + 5.0
        while time.time() < deadline:
            r,_,_ = select.select([sys.stdin.fileno()], [], [], 0.2)
            if r:
                c = os.read(sys.stdin.fileno(), 65536)
                if not c: break
                buf += c
                if b"\\x1b[201~" in buf or b"\\r" in buf: break
        # 真 claude 行为：用 --session-id 指定的 id 命名 jsonl
        sid = (sys.argv[sys.argv.index("--session-id") + 1]
               if "--session-id" in sys.argv else str(uuid.uuid4()))
        path = os.path.join(PROJECT_DIR, sid + ".jsonl")
        with open(path, "a", buffering=1, encoding="utf-8") as f:
            f.write(json.dumps({{"type": "system", "sessionId": sid}}) + "\\n")
            f.write(json.dumps({{"type": "user", "message": {{"role": "user",
                "content": [{{"type": "text", "text": "hi"}}]}}}}) + "\\n")
            f.write(json.dumps({{"type": "assistant", "message": {{"role": "assistant",
                "content": [{{"type": "text", "text": "ok"}}], "stop_reason": "end_turn",
                "usage": {{"input_tokens": 1, "output_tokens": 1,
                    "iterations": [{{"input_tokens": 1, "output_tokens": 1}}]}}}}}}) + "\\n")
        time.sleep(30)
    """)
    fake = tmp_path / "fake-forced.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)

    monkeypatch.setenv("CLAUDE_PTY_FORCE_SESSION_ID", "1")
    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)

    text, sid, used_fallback = asyncio.run(
        claude_pty.run_claude("hello", cwd=str(cwd), permission_mode="bypassPermissions")
    )

    assert sid is not None and len(sid) == 36
    assert used_fallback is False
    argv = json.load(open(sidecar))
    assert "--session-id" in argv, f"runner 没给 fresh run 传 --session-id：{argv}"
    forced = argv[argv.index("--session-id") + 1]
    assert sid == forced, f"返回 sid({sid}) != 强制 id({forced})"
    # 路径事前确定：jsonl 必须就叫 <forced>.jsonl
    assert (project_dir / f"{forced}.jsonl").is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_fresh_session_adopts_actual_jsonl_when_forced_id_ignored(tmp_path, monkeypatch):
    """回归 Claude Code 2.1.201 现场症状：

    runner 传了 --session-id，但交互式 TUI 没按 forced id 写 JSONL。只要本轮
    产生了可用 JSONL 且 user 内容匹配，就接管实际 sid，不报
    "new session jsonl never appeared"。
    """
    project_root = tmp_path / "projects"
    project_root.mkdir()
    cwd = tmp_path / "wd"
    cwd.mkdir()
    encoded = str(cwd).replace("/", "-")
    project_dir = project_root / encoded
    project_dir.mkdir(parents=True)
    sidecar = tmp_path / "argv.json"

    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, sys, time, select, uuid
        PROJECT_DIR = {str(project_dir)!r}
        SIDECAR = {str(sidecar)!r}
        json.dump(sys.argv, open(SIDECAR, "w"))
        sys.stdout.write("\\x1b[?1049hready\\n"); sys.stdout.flush()
        buf = b""; deadline = time.time() + 5.0
        while time.time() < deadline:
            r,_,_ = select.select([sys.stdin.fileno()], [], [], 0.2)
            if r:
                c = os.read(sys.stdin.fileno(), 65536)
                if not c: break
                buf += c
                if b"\\x1b[201~" in buf or b"\\r" in buf: break
        paste = buf.decode("utf-8", errors="replace").replace("\\x1b[200~","") \\
            .replace("\\x1b[201~","").strip("\\r\\n")
        forced = sys.argv[sys.argv.index("--session-id") + 1]
        sid = str(uuid.uuid4())
        assert sid != forced
        path = os.path.join(PROJECT_DIR, sid + ".jsonl")
        with open(path, "a", buffering=1, encoding="utf-8") as f:
            f.write(json.dumps({{"type": "system", "sessionId": sid}}) + "\\n")
            f.write(json.dumps({{"type": "user", "message": {{"role": "user",
                "content": [{{"type": "text", "text": paste}}]}}}}) + "\\n")
            f.write(json.dumps({{"type": "assistant", "message": {{"role": "assistant",
                "content": [{{"type": "text", "text": "actual ok"}}], "stop_reason": "end_turn",
                "usage": {{"input_tokens": 1, "output_tokens": 1,
                    "iterations": [{{"input_tokens": 1, "output_tokens": 1}}]}}}}}}) + "\\n")
        time.sleep(30)
    """)
    fake = tmp_path / "fake-ignore-forced.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)

    monkeypatch.setenv("CLAUDE_PTY_FORCE_SESSION_ID", "1")
    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)

    text, sid, used_fallback = asyncio.run(
        claude_pty.run_claude(
            "hello actual session", cwd=str(cwd), permission_mode="bypassPermissions"
        )
    )

    argv = json.load(open(sidecar))
    forced = argv[argv.index("--session-id") + 1]
    assert sid and sid != forced
    assert (project_dir / f"{sid}.jsonl").is_file()
    assert not (project_dir / f"{forced}.jsonl").exists()
    assert text == "actual ok"
    assert used_fallback is False


@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_concurrent_fresh_same_cwd_no_crosstalk(tmp_path, monkeypatch):
    """根因回归：同一个 cwd 里两个 fresh run 并发，各自的回复**绝不**串到对方卡片。

    旧逻辑 spawn 后去共享 project 目录里扫"本次产生的 jsonl"，并发 + 带图片/短消息
    时退化成"单候选默认接受"，A 会把 B 的 jsonl 认成自己的 → 卡片正文串台（闲鱼
    thread 显示成汽车之家那次）。现在 fresh 默认由 Claude 生成 sid，但必须等
    user 内容前缀匹配后才接管 JSONL；匹配不到宁可等待，也不串台。

    fake 把收到的 paste 原样 echo 回 assistant text，断言 A 的 chunks 只含 A 的
    marker、B 的只含 B 的。"""
    project_root = tmp_path / "projects"
    project_root.mkdir()
    cwd = tmp_path / "shared_wd"           # 关键：两个 run 共用同一个 cwd
    cwd.mkdir()
    encoded = str(cwd).replace("/", "-")
    project_dir = project_root / encoded
    project_dir.mkdir(parents=True)

    # echo fake：honors --session-id，把 paste 原样塞进 assistant text
    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, sys, time, select, uuid
        PROJECT_DIR = {str(project_dir)!r}
        sys.stdout.write("\\x1b[?1049hready\\n"); sys.stdout.flush()
        buf = b""; deadline = time.time() + 6.0
        while time.time() < deadline:
            r,_,_ = select.select([sys.stdin.fileno()], [], [], 0.2)
            if r:
                c = os.read(sys.stdin.fileno(), 65536)
                if not c: break
                buf += c
                if b"\\x1b[201~" in buf or b"\\r" in buf: break
        paste = buf.decode("utf-8", errors="replace").replace("\\x15","") \\
            .replace("\\x1b[200~","").replace("\\x1b[201~","").strip("\\r\\n")
        sid = (sys.argv[sys.argv.index("--session-id") + 1]
               if "--session-id" in sys.argv else str(uuid.uuid4()))
        path = os.path.join(PROJECT_DIR, sid + ".jsonl")
        os.makedirs(PROJECT_DIR, exist_ok=True)
        with open(path, "a", buffering=1, encoding="utf-8") as f:
            f.write(json.dumps({{"type": "system", "sessionId": sid}}) + "\\n")
            f.write(json.dumps({{"type": "user", "message": {{"role": "user",
                "content": [{{"type": "text", "text": paste}}]}}}}) + "\\n")
            time.sleep(0.2)
            f.write(json.dumps({{"type": "assistant", "message": {{"role": "assistant",
                "content": [{{"type": "text", "text": "ECHO::" + paste}}],
                "stop_reason": "end_turn", "usage": {{"input_tokens": 1, "output_tokens": 1,
                    "iterations": [{{"input_tokens": 1, "output_tokens": 1}}]}}}}}}) + "\\n")
        time.sleep(30)
    """)
    fake = tmp_path / "fake-echo.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)

    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)

    chunks_a: list[str] = []
    chunks_b: list[str] = []

    async def on_a(c):
        chunks_a.append(c)

    async def on_b(c):
        chunks_b.append(c)

    MARK_A = "ALPHA-marker-11111"
    MARK_B = "BETA-marker-22222"

    async def both():
        return await asyncio.gather(
            claude_pty.run_claude(
                f"first run unique {MARK_A}", cwd=str(cwd),
                permission_mode="bypassPermissions", on_text_chunk=on_a),
            claude_pty.run_claude(
                f"second run unique {MARK_B}", cwd=str(cwd),
                permission_mode="bypassPermissions", on_text_chunk=on_b),
        )

    (text_a, sid_a, fb_a), (text_b, sid_b, fb_b) = asyncio.run(both())

    # 各自拿到自己的 echo，绝不串台
    assert MARK_A in text_a and MARK_B not in text_a, f"A 串台了：{text_a!r}"
    assert MARK_B in text_b and MARK_A not in text_b, f"B 串台了：{text_b!r}"
    assert MARK_A not in "".join(chunks_b) and MARK_B not in "".join(chunks_a)
    # 两个 run 各自独立的 session 文件
    assert sid_a and sid_b and sid_a != sid_b
    assert fb_a is False and fb_b is False


# ────────────── resume 目标被 CLI 30 天清理删掉 的回归 ──────────────

@pytest.mark.skipif(sys.platform == "win32", reason="PTY only on POSIX")
def test_pty_runner_resume_missing_session_falls_back_to_fresh_session(tmp_path, monkeypatch):
    """Claude Code 默认 cleanupPeriodDays=30 会删掉旧 session 的 JSONL，而 cc-lark 的
    thread→session 绑定永不过期。老话题再来一条消息时，TUI 打
    "No conversation found with session ID: …" 后 rc=1 退出。

    修复前：tail_buffer 非空 → 撞不上"failed without stderr"那条兜底 → 直接抛
            "claude (pty) exited with code 1: <一坨 ANSI>"，该话题从此每条消息都挂。
    修复后：识别出这条永久错误 → 自动开新会话重跑 → 用户拿到真实回答。
    """
    project_root = tmp_path / "projects"
    cwd = tmp_path / "wd"
    cwd.mkdir()
    project_dir = project_root / str(cwd).replace("/", "-")
    project_dir.mkdir(parents=True)

    dead_sid = str(uuid.uuid4())  # 故意不建对应的 .jsonl —— 就是"已被清理"

    script = textwrap.dedent(f"""
        #!{sys.executable}
        import json, os, sys, time, select, uuid
        PROJECT_DIR = {str(project_dir)!r}

        if "--resume" in sys.argv:
            sid = sys.argv[sys.argv.index("--resume") + 1]
            # 真 TUI 的表现：明文原因夹在 ANSI 转义里，写 stdout 后非零退出
            sys.stdout.write("\\x1b[?25l\\x1b[38;5;137m")
            sys.stdout.write("No conversation found with session ID: " + sid)
            sys.stdout.write("\\x1b[39m\\x1b[?25h\\n")
            sys.stdout.flush()
            os._exit(1)

        sys.stdout.write("ready\\n"); sys.stdout.flush()
        buf = b""
        deadline = time.time() + 4.0
        while time.time() < deadline:
            r, _, _ = select.select([sys.stdin.fileno()], [], [], 0.3)
            if r:
                c = os.read(sys.stdin.fileno(), 65536)
                if not c: break
                buf += c
                if b"\\x1b[201~" in buf or b"\\r" in buf:
                    break
        paste_text = buf.decode("utf-8", errors="replace") \\
            .replace("\\x1b[200~", "").replace("\\x1b[201~", "").strip("\\r\\n")
        sid = (sys.argv[sys.argv.index("--session-id") + 1]
               if "--session-id" in sys.argv else str(uuid.uuid4()))
        with open(os.path.join(PROJECT_DIR, sid + ".jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({{"type": "system", "sessionId": sid}}) + "\\n")
            f.write(json.dumps({{
                "type": "user",
                "message": {{"role": "user", "content": [{{"type": "text", "text": paste_text}}]}},
            }}) + "\\n")
            f.write(json.dumps({{
                "type": "assistant",
                "message": {{
                    "content": [{{"type": "text", "text": "Fresh session real reply ✅"}}],
                    "stop_reason": "end_turn",
                    "usage": {{"input_tokens": 3, "output_tokens": 6, "iterations": [{{
                        "input_tokens": 3, "output_tokens": 6
                    }}]}},
                }},
            }}) + "\\n")
        time.sleep(30)
    """)
    fake = tmp_path / "fake-claude-session-gone.py"
    fake.write_text(script.lstrip())
    fake.chmod(0o755)

    monkeypatch.setattr(claude_pty, "CLAUDE_CLI", str(fake))
    monkeypatch.setattr(claude_pty, "CLAUDE_PROJECTS_DIR", str(project_root))
    monkeypatch.setattr(claude_pty, "_has_children", lambda _pid: False)

    text, returned_sid, used_fallback = asyncio.run(
        claude_pty.run_claude(
            "这笔有计入台账吗？",
            session_id=dead_sid,
            cwd=str(cwd),
            permission_mode="bypassPermissions",
        )
    )

    assert text == "Fresh session real reply ✅"
    assert used_fallback is True
    assert returned_sid != dead_sid
