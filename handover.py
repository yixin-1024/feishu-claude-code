#!/usr/bin/env python3
"""
CLI → 飞书/Lark Bot 会话移交工具。

用法:
  python3 handover.py "对话中的独特文本" [profile_name]

profile_name 可选；省略时 bot 会选第一个已配置的 profile（多 profile 场景下
明确指定可避免发错）。通过内容指纹在所有 ~/.claude/projects/ 下的 .jsonl 中
搜索，匹配到的文件就是当前会话。
"""

import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

from dotenv import dotenv_values

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
_REPO_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _setting(name: str, default: str = "") -> str:
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    try:
        return str(dotenv_values(_REPO_ENV).get(name) or default).strip()
    except Exception:
        return default


def _control_token() -> str:
    token = _setting("CC_LARK_CONTROL_TOKEN")
    if token:
        return token
    token_file = os.path.expanduser(
        _setting("CC_LARK_CONTROL_TOKEN_FILE", "~/.feishu-claude/control-token")
    )
    try:
        with open(token_file, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _handover_url() -> str:
    callback_port = int(_setting("CALLBACK_PORT", "9981"))
    port = _setting("CONTROL_PORT", str(callback_port + 1))
    return f"http://127.0.0.1:{port}/handover"


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
        print("Usage: handover.py <fingerprint> [profile]", file=sys.stderr)
        sys.exit(1)

    found = _find_session(sys.argv[1])
    if not found:
        print("ERROR: 未找到匹配的 session，换一段更独特的文本试试")
        sys.exit(1)

    session_id, cwd = found
    query = {
        "session_id": session_id,
        "cwd": cwd,
    }
    # 只有显式设了 CLAUDE_MODEL 才带模型；否则接管后跟随 profile 默认模型
    _model = os.environ.get("CLAUDE_MODEL", "").strip()
    if _model:
        query["model"] = _model
    if len(sys.argv) >= 3:
        query["profile"] = sys.argv[2]
    params = urllib.parse.urlencode(query)

    token = _control_token()
    if not token:
        print("ERROR: 找不到 cc-lark control token；请先启动新版 bot", file=sys.stderr)
        sys.exit(1)

    req = urllib.request.Request(
        f"{_handover_url()}?{params}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
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
