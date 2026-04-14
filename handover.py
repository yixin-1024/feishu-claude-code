#!/usr/bin/env python3
"""
CLI → 飞书 Bot 会话移交工具。

用法:
  python3 handover.py "对话中的独特文本"

通过内容指纹在所有 ~/.claude/projects/ 下的 .jsonl 中搜索，
匹配到的文件就是当前会话，然后调用飞书 Bot 的 handover 端点完成移交。
"""

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
HANDOVER_URL = "http://localhost:9981/handover"


def _find_session(fingerprint: str) -> tuple[str, str] | None:
    """在所有项目目录的 .jsonl 中搜索指纹文本。返回 (session_id, cwd) 或 None"""
    try:
        result = subprocess.run(
            ["grep", "-rl", "--include=*.jsonl", fingerprint, CLAUDE_PROJECTS_DIR],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None

    matches = [l.strip() for l in result.stdout.strip().splitlines() if l.strip().endswith(".jsonl")]
    if not matches:
        return None

    if len(matches) > 1:
        matches.sort(key=lambda f: os.path.getmtime(f), reverse=True)

    best = matches[0]
    session_id = os.path.basename(best).replace(".jsonl", "")
    # 从项目目录名还原 cwd: -Users-jonathan → /Users/jonathan
    project_name = os.path.basename(os.path.dirname(best))
    cwd = project_name.replace("-", "/")
    return session_id, cwd


def main():
    if len(sys.argv) < 2:
        print("Usage: handover.py <fingerprint>", file=sys.stderr)
        sys.exit(1)

    found = _find_session(sys.argv[1])
    if not found:
        print("ERROR: 未找到匹配的 session，换一段更独特的文本试试")
        sys.exit(1)

    session_id, cwd = found
    params = urllib.parse.urlencode({
        "session_id": session_id,
        "cwd": cwd,
        "model": os.environ.get("CLAUDE_MODEL", "claude-opus-4-6"),
    })

    try:
        with urllib.request.urlopen(f"{HANDOVER_URL}?{params}", timeout=10) as resp:
            result = json.loads(resp.read())
    except ConnectionRefusedError:
        print("ERROR: 飞书 Bot 未运行")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if result.get("ok"):
        print(session_id)
    else:
        print(f"ERROR: {result.get('error', 'unknown')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
