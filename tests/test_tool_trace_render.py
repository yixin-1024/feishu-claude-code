"""卡片上的工具轨迹渲染：入参要看得见，多条要留得住。

2026-09-03 修的两个问题：
  1. 非 Claude 后端（agy 等）的工具名没映射，卡片只写 `⚙️ run_command`，
     执行了什么命令 / 读了哪个文件全看不到。
  2. `on_tool_use` 的覆盖逻辑只对"先报名字再补入参"的 Claude 成立，
     一次带全参数上报的后端每来一个新工具都会覆盖上一行 → 卡片永远只剩 1 条。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dispatcher import (
    _TOOL_HISTORY_SHOWN,
    _format_usage_footer,
    _format_tool,
    _short_arg,
    _tool_identity,
    _tool_primary_arg,
)


def _simulate(events):
    """复刻 dispatcher.on_tool_use 里的历史维护逻辑，返回卡片上会显示的行。"""
    tool_history: list[str] = []
    tool_idents: list[tuple[str, str]] = []
    for name, inp in events:
        line = _format_tool(name, inp)
        ident = _tool_identity(name, inp)
        if tool_idents and tool_idents[-1][0] == name.lower() and (
            not tool_idents[-1][1] or tool_idents[-1][1] == ident
        ):
            tool_history[-1] = line
            tool_idents[-1] = (name.lower(), ident)
        else:
            tool_history.append(line)
            tool_idents.append((name.lower(), ident))
    return tool_history[-_TOOL_HISTORY_SHOWN:]


def test_agy_tools_show_real_arguments():
    """agy 的工具名 + 它自己的参数字段名（CommandLine / AbsolutePath / …）都要认。"""
    assert _format_tool("run_command", {"CommandLine": "npm test -- --watch=false"}) == (
        "🔧 **执行命令：** `npm test -- --watch=false`"
    )
    assert _format_tool("view_file", {"AbsolutePath": "/tmp/x/app.tsx"}) == (
        "📄 **读取：** `/tmp/x/app.tsx`"
    )
    assert _format_tool("write_to_file", {"TargetFile": "/tmp/x/out.txt"}) == (
        "✏️ **写入：** `/tmp/x/out.txt`"
    )
    assert _format_tool("replace_file_content", {"TargetFile": "/tmp/x/a.py"}) == (
        "✂️ **编辑：** `/tmp/x/a.py`"
    )
    assert _format_tool("list_dir", {"DirectoryPath": "/tmp/x"}) == "📁 **列目录：** `/tmp/x`"
    grep = _format_tool("grep_search", {"Query": "TODO", "SearchPath": "/tmp/x"})
    assert "`TODO`" in grep and "/tmp/x" in grep


def test_completion_state_marks_line():
    """DONE / ERROR 由 agy_runner 塞 _agy_state 进来，前缀换成 ✅ / ❌。"""
    done = _format_tool("run_command", {"CommandLine": "ls", "_agy_state": "DONE"})
    err = _format_tool("run_command", {"CommandLine": "ls", "_agy_state": "ERROR"})
    assert done.startswith("✅") and "`ls`" in done
    assert err.startswith("❌")


def test_unknown_tool_still_shows_primary_arg():
    """没映射的工具也别只剩名字——兜底把主参数带出来。"""
    assert _format_tool("some_new_tool", {"Query": "找点东西"}) == (
        "⚙️ **some_new_tool** `找点东西`"
    )
    assert _format_tool("no_arg_tool", {}) == "⚙️ **no_arg_tool**"


def test_claude_two_phase_report_overwrites_placeholder():
    """Claude 先报名字（inp 空）再补入参，两条要合成一行。"""
    lines = _simulate([("Bash", {}), ("Bash", {"command": "pytest -q"})])
    assert lines == ["🔧 **执行命令：** `pytest -q`"]


def test_bash_start_then_completed_collapses():
    """同一条命令的开始→完成也合成一行（不重复占两行）。"""
    lines = _simulate([
        ("Bash", {"command": "pytest -q"}),
        ("Bash", {"command": "pytest -q", "status": "completed", "exit_code": 0}),
    ])
    assert lines == ["✅ **执行命令：** `pytest -q`"]


def test_single_shot_backend_keeps_every_tool():
    """回归：agy 这种一次带全参数上报的后端，每个工具都要各占一行。

    老逻辑下这里只会剩最后 1 行 —— 这就是用户看到「总是看不到 agent 在做什么」的根因。
    """
    lines = _simulate([
        ("run_command", {"CommandLine": "ls -la"}),
        ("run_command", {"CommandLine": "ls -la", "_agy_state": "DONE"}),
        ("view_file", {"AbsolutePath": "/tmp/a.py"}),
        ("view_file", {"AbsolutePath": "/tmp/a.py", "_agy_state": "DONE"}),
        ("grep_search", {"Query": "TODO", "SearchPath": "/tmp"}),
        ("run_command", {"CommandLine": "npm run build"}),
    ])
    assert len(lines) == 4
    assert lines[0].startswith("✅") and "ls -la" in lines[0]
    assert lines[1].startswith("✅") and "/tmp/a.py" in lines[1]
    assert "TODO" in lines[2]
    assert "npm run build" in lines[3]


def test_history_depth_is_ten():
    """一轮里连跑十几个命令时，卡片至少留住最近 10 条。"""
    assert _TOOL_HISTORY_SHOWN == 10
    events = [("run_command", {"CommandLine": f"echo {i}"}) for i in range(15)]
    lines = _simulate(events)
    assert len(lines) == 10
    assert "echo 14" in lines[-1] and "echo 5" in lines[0]


def test_short_arg_shortens_home_and_long_text():
    home = os.path.expanduser("~")
    assert _short_arg(f"{home}/repo/a.py") == "~/repo/a.py"
    long_cmd = "x" * 200
    out = _short_arg(long_cmd)
    assert len(out) <= 96 and "…" in out
    # 多行命令压成一行，别把卡片顶开
    assert "\n" not in _short_arg("line1\nline2")


def test_primary_arg_skips_internal_marks():
    """_agy_state 是 runner 的内部标记，不能被当成主参数（否则 identity 变了会多占一行）。"""
    assert _tool_primary_arg({"_agy_state": "DONE"}) == ""
    assert _tool_identity("x", {"_agy_state": "DONE"}) == _tool_identity("x", {})


def test_other_backends_arg_fields_are_covered():
    """字段名各家不一样，都得渲出内容——空壳「读取：``」是回归信号。"""
    # grok：target_file
    assert _format_tool("read_file", {"target_file": "probe.txt"}) == "📄 **读取：** `probe.txt`"
    # opencode / mimo：把入参压进 command，且自带 status
    oc = _format_tool("read", {"command": "src/app.tsx", "status": "completed"})
    assert oc == "✅ **读取：** `src/app.tsx`"
    # codex：bash + status/exit_code（保留 exit 码细节）
    bad = _format_tool("bash", {"command": "pytest", "status": "completed", "exit_code": 1})
    assert bad.startswith("⚠️") and "exit 1" in bad
    # opencode 的非 bash 工具失败态
    assert _format_tool("edit", {"command": "a.py", "status": "error"}).startswith("❌")


def test_usage_footer_separates_turn_spend_from_context():
    """上下文百分比只反映上下文；一轮里重复发请求的累计消耗单独显示。"""
    usage = {
        "input_tokens": 5526, "output_tokens": 1,
        "cache_read_input_tokens": 16263, "_context_window": 1_000_000,
        "_turn_tokens": 168656,
    }
    line = _format_usage_footer(usage, "gemini-3.8-flash")
    assert "上下文 21.8k / 1M (2.2%)" in line
    assert "本轮消耗 168.7k" in line
    # 没有累计消耗（或不超过上下文）时不画这一段，别给 claude/codex 添噪音
    plain = _format_usage_footer(
        {"input_tokens": 5000, "output_tokens": 100, "_context_window": 200_000}, "sonnet")
    assert "本轮消耗" not in plain
