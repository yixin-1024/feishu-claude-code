"""cc_web_gateway 单测用的假 claude CLI（stream-json 输出）。

行为由 prompt 里的 MODE:<name> 决定（网关给子进程的 env 是白名单拼的，没法靠 env 传开关）：
    默认        → 回 "OK"
    MODE:slow   → 每 0.15s 吐一个 text_delta，共 20 个
    MODE:silent → 吐完 init 就一声不吭睡 60s（测 idle 超时）
    MODE:chatty → 不停吐无关事件（测 wall-clock 超时）
    MODE:tools  → 一次 Bash(rg-tool) 工具调用 + 结果，再回文本
    MODE:recall → 回 "PREV=<上一轮 prompt>"（测 resume）
    MODE:echoenv→ 回 RG_RUN_TOKEN 的值（测脱敏）
    MODE:echorelay→ 回 ANTHROPIC_AUTH_TOKEN 的值（测中转站 token 脱敏）
    MODE:apierror → result is_error（API 500）
会话存在 <cwd>/.fake_sessions/<sid>.jsonl；--resume 不存在的 sid → 和真 CLI 一样报
"No conversation found with session ID"。每次调用把 argv / env / prompt 追加到
<cwd>/.fake_calls.jsonl 供断言。
"""

import json
import os
import sys
import time
import uuid


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def text_delta(t):
    out({"type": "stream_event", "event": {"type": "content_block_delta",
                                            "delta": {"type": "text_delta", "text": t}}})


def result(text, sid, **extra):
    evt = {"type": "result", "subtype": "success", "is_error": False, "result": text,
           "session_id": sid, "num_turns": 1, "total_cost_usd": 0.0012, "duration_ms": 42,
           "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 5}}
    evt.update(extra)
    out(evt)


def main():
    argv = sys.argv[1:]
    prompt = sys.stdin.read()
    cwd = os.getcwd()
    resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
    with open(os.path.join(cwd, ".fake_calls.jsonl"), "a") as f:
        f.write(json.dumps({"argv": argv, "env": dict(os.environ), "prompt": prompt,
                            "pid": os.getpid()}) + "\n")
    sdir = os.path.join(cwd, ".fake_sessions")
    os.makedirs(sdir, exist_ok=True)
    if resume:
        if not os.path.exists(os.path.join(sdir, resume + ".jsonl")):
            out({"type": "result", "subtype": "error_during_execution", "is_error": True,
                 "result": "", "duration_ms": 0, "session_id": str(uuid.uuid4()),
                 "errors": [f"No conversation found with session ID: {resume}"]})
            sys.exit(1)
        sid = resume
    else:
        sid = str(uuid.uuid4())
    spath = os.path.join(sdir, sid + ".jsonl")
    prev = []
    if os.path.exists(spath):
        with open(spath) as f:
            prev = [json.loads(x)["prompt"] for x in f if x.strip()]
    with open(spath, "a") as f:
        f.write(json.dumps({"prompt": prompt.strip()}) + "\n")

    out({"type": "system", "subtype": "init", "session_id": sid, "model": "fake-model",
         "tools": ["Bash", "Read"]})

    if "MODE:silent" in prompt:
        time.sleep(60)
        return
    if "MODE:chatty" in prompt:
        while True:
            out({"type": "stream_event", "event": {"type": "ping"}})
            time.sleep(0.05)
    if "MODE:slow" in prompt:
        for i in range(20):
            text_delta(f"t{i} ")
            time.sleep(0.15)
        result("slow done", sid)
        return
    if "MODE:tools" in prompt:
        out({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash",
             "input": {"command": "rg-tool customer get --id 42", "description": "x"}}]}})
        out({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": False,
             "content": [{"type": "text", "text": "{\"id\": 42, \"name\": \"Test Co\"}"}]}]}})
        text_delta("Customer is Test Co")
        result("Customer is Test Co", sid)
        return
    if "MODE:recall" in prompt:
        msg = "PREV=" + (prev[-1] if prev else "<none>")
        text_delta(msg)
        result(msg, sid)
        return
    if "MODE:echorelay" in prompt:
        msg = "relay is " + os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        text_delta(msg)
        result(msg, sid)
        return
    if "MODE:echoenv" in prompt:
        msg = "token is " + os.environ.get("RG_RUN_TOKEN", "")
        text_delta(msg)
        result(msg, sid)
        return
    if "MODE:apierror" in prompt:
        out({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 500,
             "result": "API Error: 500 internal", "session_id": sid})
        return
    text_delta("O")
    text_delta("K")
    result("OK", sid)


if __name__ == "__main__":
    main()
