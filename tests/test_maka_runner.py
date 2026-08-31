import asyncio
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import maka_runner
from maka_runner import run_maka


class FakeProc:
    """maka run 不流式：communicate() 一把交出 stdout/stderr。"""

    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = 5150
        self.stdin_payload = None

    async def communicate(self, payload=None):
        self.stdin_payload = payload
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _patch_exec(monkeypatch, procs, captured):
    """procs 可以是单个 FakeProc，也可以是按调用顺序取的列表。"""
    queue = list(procs) if isinstance(procs, list) else [procs]
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(list(args))
        captured["cmd"] = list(args)
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        captured["calls"] = calls
        return queue[min(len(calls) - 1, len(queue) - 1)]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


def _fake_workspace(tmp_path, rows=()):
    """造一个带 session_metadata 的 runtime.sqlite，模拟 maka 的 workspace。"""
    db = tmp_path / "runtime.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "create table session_metadata (session_id text, created_at integer, "
        "name text, payload_json text)"
    )
    con.executemany("insert into session_metadata values (?,?,?,?)", rows)
    con.commit()
    con.close()
    return str(tmp_path)


def test_run_maka_returns_final_output_and_streams_once(monkeypatch, tmp_path):
    captured = {}
    _patch_exec(monkeypatch, FakeProc(stdout="banana-7788\n".encode()), captured)

    chunks, statuses = [], []
    text, sid, fallback = asyncio.run(
        run_maka(
            message="读 probe.txt",
            cwd="/tmp",
            workspace_root=str(tmp_path),  # 空 workspace：认领不到 session id
            on_text_chunk=chunks.append,
            on_status=lambda kind, detail: statuses.append((kind, detail)),
        )
    )

    assert text == "banana-7788"
    assert sid is None
    assert fallback is False
    # 无流式通道：整段正文只在收尾时一次性推给卡片
    assert chunks == ["banana-7788"]
    # ⚠️ 绝不能碰 on_status：那是 dispatcher 的「API 限流/过载」告警通道，只有
    # level="clear" 才消除，发进度提示会在收尾卡片上留一条永不消失的 🚦 假告警
    assert statuses == []


def test_run_maka_builds_expected_argv(monkeypatch, tmp_path):
    captured = {}
    proc = FakeProc(stdout=b"ok\n")
    _patch_exec(monkeypatch, proc, captured)

    asyncio.run(
        run_maka(
            message="hi",
            model="deepseek-v4-flash",
            connection="env-deepseek",
            cwd="/tmp",
            effort="high",
            append_system_prompt="LARK RULES",
            max_steps=12,
            workspace_root=str(tmp_path),
            api_key="sk-test",
            base_url="https://api.wowapi.ai/v1",
            idle_timeout_sec=900,
        )
    )

    cmd = captured["cmd"]
    assert cmd[1:3] == ["run", "-"]  # prompt 走 stdin
    assert cmd[cmd.index("--cwd") + 1] == "/tmp"
    assert cmd[cmd.index("--connection") + 1] == "env-deepseek"
    assert cmd[cmd.index("--model") + 1] == "deepseek-v4-flash"
    assert cmd[cmd.index("--thinking") + 1] == "high"
    assert cmd[cmd.index("--max-steps") + 1] == "12"
    # 无人值守：默认必须给 --yolo，否则任何越出沙箱的操作都会被非交互模式直接拒掉
    assert "--yolo" in cmd
    # maka 自己的 --timeout 要比我们的硬杀早到期；上限取 profile / 全局的更小者
    assert int(cmd[cmd.index("--timeout") + 1]) == 900 - maka_runner._GRACE_SEC
    # 首轮不带 --resume
    assert "--resume" not in cmd
    # append_system_prompt 拼在 prompt 前面，整体从 stdin 进去
    assert proc.stdin_payload.decode() == "LARK RULES\n\nhi"
    # bootstrap 用的凭证/端点按约定注入 deepseek 通道
    assert captured["env"]["DEEPSEEK_API_KEY"] == "sk-test"
    assert captured["env"]["DEEPSEEK_BASE_URL"] == "https://api.wowapi.ai/v1"
    assert captured["cwd"] == "/tmp"


def test_run_maka_resume_keeps_session_and_drops_model_flags(monkeypatch, tmp_path):
    captured = {}
    _patch_exec(monkeypatch, FakeProc(stdout=b"ok\n"), captured)

    _text, sid, _fallback = asyncio.run(
        run_maka(
            message="hi",
            session_id="sess-1",
            model="deepseek-v4-pro",
            connection="env-deepseek",
            cwd="/tmp",
            workspace_root=str(tmp_path),
        )
    )

    cmd = captured["cmd"]
    assert cmd[cmd.index("--resume") + 1] == "sess-1"
    # 续会话时连接/模型跟随 session 自己的设置，不能再传（maka 会以 session 为准）
    assert "--model" not in cmd and "--connection" not in cmd
    assert sid == "sess-1"


def test_run_maka_claims_only_the_session_created_this_turn(monkeypatch, tmp_path):
    """认领 = 跑前快照的差集 ∩ cwd。会话名在真实 bot 里全都一样，不能当判据。"""
    TMP = os.path.realpath("/tmp")
    ws = _fake_workspace(
        tmp_path,
        rows=[
            # 跑前就存在（同 cwd、同名字）—— 不能认领
            ("old-sid", 100, "同一个 Lark 前言开头", '{"cwd":"' + TMP + '"}'),
            # 本轮新增但 cwd 不对 —— 不能认领
            ("other-cwd-sid", 300, "同一个 Lark 前言开头", '{"cwd":"/private/var/other"}'),
            # 本轮新增 + cwd 对得上 —— 就是它
            ("new-sid", 200, "同一个 Lark 前言开头", '{"cwd":"' + TMP + '"}'),
        ],
    )
    captured = {}
    _patch_exec(monkeypatch, FakeProc(stdout=b"done\n"), captured)
    monkeypatch.setattr(
        maka_runner, "_session_ids_snapshot", lambda root: {"old-sid"}
    )

    _text, sid, _fallback = asyncio.run(
        run_maka(message="查一下磁盘", cwd="/tmp", workspace_root=ws)
    )
    assert sid == "new-sid"


def test_run_maka_refuses_to_claim_when_two_new_sessions_share_cwd(monkeypatch, tmp_path):
    """同目录并发：宁可下一轮开新会话，也不能把两个话题的上下文串到一起。"""
    TMP = os.path.realpath("/tmp")
    ws = _fake_workspace(
        tmp_path,
        rows=[
            ("mine-or-theirs-a", 200, "n", '{"cwd":"' + TMP + '"}'),
            ("mine-or-theirs-b", 201, "n", '{"cwd":"' + TMP + '"}'),
        ],
    )
    captured = {}
    _patch_exec(monkeypatch, FakeProc(stdout=b"done\n"), captured)
    monkeypatch.setattr(maka_runner, "_session_ids_snapshot", lambda root: set())

    _text, sid, _fallback = asyncio.run(
        run_maka(message="hi", cwd="/tmp", workspace_root=ws)
    )
    assert sid is None


def test_run_maka_resume_broken_falls_back_to_fresh(monkeypatch, tmp_path):
    captured = {}
    procs = [
        FakeProc(stderr=b"maka run: cannot resume externally isolated session x", returncode=1),
        FakeProc(stdout=b"fresh-ok\n"),
    ]
    calls = _patch_exec(monkeypatch, procs, captured)

    text, _sid, fallback = asyncio.run(
        run_maka(message="hi", session_id="stale-sid", cwd="/tmp", workspace_root=str(tmp_path))
    )

    assert text == "fresh-ok"
    assert fallback is True
    assert "--resume" in calls[0] and "--resume" not in calls[1]


def test_run_maka_setup_error_is_not_retryable(monkeypatch, tmp_path):
    captured = {}
    _patch_exec(
        monkeypatch,
        FakeProc(
            stderr=b"maka run: Task is not ready:\nmodel_empty_model_list: repair connection",
            returncode=1,
        ),
        captured,
    )

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(run_maka(message="hi", cwd="/tmp", workspace_root=str(tmp_path)))
    assert "未就绪" in str(exc.value)
    # 配置问题重跑也是同样结果，绝不能当成"可续跑"喂回 dispatcher
    assert getattr(exc.value, "cc_retryable_resume", None) is None


def test_run_maka_runtime_failure_is_retryable(monkeypatch, tmp_path):
    ws = _fake_workspace(
        tmp_path,
        rows=[("sid-9", 900, "hi", '{"cwd":"' + os.path.realpath("/tmp") + '"}')],
    )
    captured = {}
    _patch_exec(monkeypatch, FakeProc(stderr=b"maka run: provider stream closed", returncode=1), captured)
    monkeypatch.setattr(maka_runner, "_session_ids_snapshot", lambda root: set())

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(run_maka(message="hi", cwd="/tmp", workspace_root=ws))
    assert getattr(exc.value, "cc_session_id", None) == "sid-9"
    assert getattr(exc.value, "cc_retryable_resume", None) is True


def test_run_maka_strips_node_experimental_warning(monkeypatch, tmp_path):
    """node 的 SQLite 实验特性警告每轮都刷 stderr，不能被当成错误详情。"""
    captured = {}
    _patch_exec(
        monkeypatch,
        FakeProc(
            stdout=b"",
            stderr=(
                b"(node:1) ExperimentalWarning: SQLite is an experimental feature\n"
                b"(Use `node --trace-warnings ...` to show where the warning was created)\n"
            ),
            returncode=1,
        ),
        captured,
    )

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(run_maka(message="hi", cwd="/tmp", workspace_root=str(tmp_path)))
    assert "ExperimentalWarning" not in str(exc.value)
    assert "no stderr" in str(exc.value)


def test_run_maka_rejects_unknown_thinking(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(run_maka(message="hi", cwd="/tmp", effort="turbo", workspace_root=str(tmp_path)))


def test_thinking_and_permission_normalization():
    assert maka_runner._normalize_thinking("none") == "off"
    assert maka_runner._normalize_thinking("") is None
    assert maka_runner._normalize_thinking("XHIGH") == "xhigh"
    assert maka_runner._wants_full_access("bypassPermissions", False) is True
    assert maka_runner._wants_full_access("plan", True) is False
    assert maka_runner._wants_full_access("", True) is True


def test_workspace_root_default_matches_upstream(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    root = maka_runner.resolve_maka_workspace_root()
    assert root.endswith("/Library/Application Support/Maka/workspaces/default")
    assert maka_runner.resolve_maka_workspace_root("~/x") == os.path.expanduser("~/x")


def test_session_name_is_not_discriminating_in_the_real_bot():
    """留档为什么不能用会话名认领：cc-lark 把 Lark 前言拼在 prompt 最前面，
    而 maka 用拼好的 prompt 首行前 42 字符当名字 → 所有话题同名。"""
    preamble = "你正在通过Lark与用户对话。你输出的文本由后台 bot 渲染成卡片发到用户的聊天里。"
    a = maka_runner._session_name_for(f"{preamble}\n\n帮我查 A")
    b = maka_runner._session_name_for(f"{preamble}\n\n帮我查 B")
    assert a == b  # ← 正是这一点让名字失去区分度
    # 规则本身仍与上游一致：首行前 42 字符 + 空白折叠
    assert maka_runner._session_name_for("hello   world\nsecond line") == "hello world"
    assert maka_runner._session_name_for("x" * 60) == "x" * 42


def test_wall_clock_limit_takes_the_smaller_of_both(monkeypatch, tmp_path):
    """全局 CLAUDE_WALL_CLOCK_LIMIT_SEC 默认 3600，不能把 MAKA_IDLE_TIMEOUT_SEC 顶成死配置。"""
    captured = {}
    _patch_exec(monkeypatch, FakeProc(stdout=b"ok\n"), captured)
    monkeypatch.setattr(maka_runner, "resolve_claude_wall_clock_limit", lambda env: 3600.0)

    asyncio.run(run_maka(message="hi", cwd="/tmp", workspace_root=str(tmp_path),
                         idle_timeout_sec=600))
    cmd = captured["cmd"]
    assert int(cmd[cmd.index("--timeout") + 1]) == 600 - maka_runner._GRACE_SEC

    # profile 侧填 0（=不设限）时回落到全局上限
    asyncio.run(run_maka(message="hi", cwd="/tmp", workspace_root=str(tmp_path),
                         idle_timeout_sec=0))
    cmd = captured["cmd"]
    assert int(cmd[cmd.index("--timeout") + 1]) == 3600 - maka_runner._GRACE_SEC
