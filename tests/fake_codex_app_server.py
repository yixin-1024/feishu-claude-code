"""假的 `codex app-server`：按场景脚本回放录制下来的 JSON-RPC 通知。

测试把它当 codex 二进制拉起（真实子进程 + 真实管道），驱动 codex_runner 的
app-server 传输。fixture 是从真机 codex 0.164 录下来的 turn 帧（见
tests/fixtures/codex_app_server/），$THREAD / $TURN 占位符运行时替换。

环境变量：
  FAKE_CODEX_SCENARIO  场景 JSON 路径
  FAKE_CODEX_LOG       把收到的每条客户端消息写成 jsonl（测试断言用）

场景格式：
  {
    "resume_error": "...",            # thread/resume 回这个错误
    "reject_steer": true,             # turn/steer 一律报错（模拟 turn 刚好收尾）
    "turns": [                        # 第 i 次 turn/start 回放 turns[i]
      {"fixture": "turn_read_and_run.json"} | {"frames": [...]},
      ...
    ]
  }
  帧里可以夹控制项：
    {"__wait__": "interrupt"}   阻塞到收到 turn/interrupt，回 turn/completed(interrupted) 并结束这个 turn
    {"__wait__": "steer"}       阻塞到收到 turn/steer（回显 userMessage 后继续往下放）
    {"__server_request__": {"method": ..., "params": ...}}  发服务端请求并等回复
    {"__exit__": 3}             直接退出（模拟进程崩溃）
"""

import json
import os
import queue
import sys
import threading
import time

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "codex_app_server")
THREAD_ID = "019a0000-0000-7000-8000-00000000cafe"

scenario = json.load(open(os.environ["FAKE_CODEX_SCENARIO"]))
log_path = os.environ.get("FAKE_CODEX_LOG")
log = open(log_path, "a") if log_path else None
inbox: "queue.Queue[dict | None]" = queue.Queue()
out_lock = threading.Lock()


def send(msg: dict) -> None:
    with out_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def reader() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if log:
            log.write(json.dumps({"argv": sys.argv[1:], "msg": msg}, ensure_ascii=False) + "\n")
            log.flush()
        inbox.put(msg)
    inbox.put(None)


threading.Thread(target=reader, daemon=True).start()

state = {"thread": THREAD_ID, "turn": None, "turn_no": 0, "interrupted": False, "srv": 0, "seen": set()}


def frames_for(turn: dict) -> list:
    frames = turn.get("frames")
    if frames is None:
        frames = json.load(open(os.path.join(FIXTURES, turn["fixture"])))
    raw = json.dumps(frames)
    raw = raw.replace("$THREAD", state["thread"]).replace("$TURN", state["turn"])
    return json.loads(raw)


def emit(m: dict) -> None:
    send(m)
    time.sleep(0.002)


def user_message(client_id, text) -> list:
    item = {"type": "userMessage", "id": f"um-{client_id}", "clientId": client_id,
            "content": [{"type": "text", "text": text, "text_elements": []}]}
    base = {"threadId": state["thread"], "turnId": state["turn"]}
    return [
        {"method": "item/started", "params": {**base, "item": item, "startedAtMs": 1}},
        {"method": "item/completed", "params": {**base, "item": item, "completedAtMs": 1}},
    ]


def handle_turn_request(msg: dict) -> str:
    """turn 进行中收到的请求。返回处理掉的种类（steer / interrupt / ''）。"""
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "turn/steer":
        if scenario.get("reject_steer") or params.get("expectedTurnId") != state["turn"]:
            send({"id": rid, "error": {"code": -32600, "message": "expected turn is not active"}})
            return ""
        send({"id": rid, "result": {"turnId": state["turn"]}})
        text = "".join(i.get("text", "") for i in params.get("input") or [])
        for m in user_message(params.get("clientUserMessageId"), text):
            emit(m)
        state["seen"].add("steer")
        return "steer"
    if method == "turn/interrupt":
        send({"id": rid, "result": None})
        state["interrupted"] = True
        return "interrupt"
    if rid is not None and method:
        send({"id": rid, "error": {"code": -32600, "message": f"unexpected {method} during turn"}})
    return ""


def finish_interrupted() -> None:
    emit({"method": "turn/completed", "params": {"threadId": state["thread"], "turn": {
        "id": state["turn"], "items": [], "status": "interrupted", "error": None}}})


def run_turn(turn: dict) -> None:
    state["interrupted"] = False
    state["seen"] = set()
    for frame in frames_for(turn):
        # 先处理进行中到达的请求（steer / interrupt 可能随时来）
        while True:
            try:
                msg = inbox.get_nowait()
            except queue.Empty:
                break
            if msg is None:
                sys.exit(0)
            handle_turn_request(msg)
        if state["interrupted"]:
            finish_interrupted()
            return
        if "__wait__" in frame:
            want = frame["__wait__"]
            while want not in state["seen"] and not state["interrupted"]:
                msg = inbox.get()
                if msg is None:
                    sys.exit(0)
                handle_turn_request(msg)
            if state["interrupted"]:
                finish_interrupted()
                return
            continue
        if "__server_request__" in frame:
            state["srv"] += 1
            rid = f"srv-{state['srv']}"
            send({"id": rid, **frame["__server_request__"]})
            while True:
                msg = inbox.get()
                if msg is None:
                    sys.exit(0)
                if msg.get("id") == rid and "method" not in msg:
                    break
                handle_turn_request(msg)
            continue
        if "__exit__" in frame:
            sys.stdout.flush()
            os._exit(int(frame["__exit__"]))
        emit(frame)


while True:
    msg = inbox.get()
    if msg is None:
        break
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if rid is None:
        continue  # initialized 等通知
    if method == "initialize":
        send({"id": rid, "result": {"userAgent": "fake/0.164.0", "platformOs": "linux"}})
    elif method == "thread/start":
        send({"id": rid, "result": {"thread": {"id": state["thread"]}}})
    elif method == "thread/resume":
        if scenario.get("resume_error"):
            send({"id": rid, "error": {"code": -32600, "message": scenario["resume_error"]}})
        else:
            state["thread"] = params["threadId"]
            send({"id": rid, "result": {"thread": {"id": state["thread"]}}})
    elif method == "turn/start":
        turns = scenario.get("turns") or []
        idx = state["turn_no"]
        state["turn_no"] += 1
        state["turn"] = f"019a0000-0000-7000-8000-{idx:012d}"
        send({"id": rid, "result": {"turn": {"id": state["turn"], "items": [], "status": "inProgress"}}})
        if idx < len(turns):
            run_turn(turns[idx])
        else:
            emit({"method": "turn/completed", "params": {"threadId": state["thread"], "turn": {
                "id": state["turn"], "items": [], "status": "completed"}}})
        state["turn"] = None
    elif method in ("turn/steer", "turn/interrupt"):
        send({"id": rid, "error": {"code": -32600, "message": "no active turn"}})
    else:
        send({"id": rid, "error": {"code": -32601, "message": f"fake: {method} not implemented"}})
