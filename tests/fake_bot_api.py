"""一个够用的 Telegram Bot API 模拟器（本机 HTTP），给端到端联调用。

为什么需要它：Telegram 的 bot 只能被**真人**先搭话，机主睡了、机器又是合盖无屏
状态（UI 自动化打不开 Telegram 客户端），但"真 socket + 真轮询线程 + 真 JSON +
真 Claude"这条链路必须在上线前跑通一遍。于是把 Bot API 那一端换成本机的假服务：
`CC_TG_API_ROOT=http://127.0.0.1:<port>` 一指过去，telegram_client / tg-cli
的代码路径完全不变。

Telegram 自己的怪癖（HTML entity 解析、flood control 的真实阈值）另外用真 API
校验（`sendMessage(chat_id=1)` 会先解析 entities 再报 chat not found）。

支持：getMe / getUpdates(长轮询 + offset 语义) / sendMessage / editMessageText /
editMessageReplyMarkup / answerCallbackQuery / setMyCommands / getMyCommands /
getFile / sendPhoto / sendDocument / deleteWebhook。
可注入故障：`fail_once("sendMessage", 429, retry_after=2)`。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


# 进程内全局递增的 id 池。Telegram 的 message_id 在同一 chat 里不会重复，而
# dispatcher 的重复事件表（_seen_messages）是模块级 + 带 TTL 的：两个测试各起一个
# emulator、都从 5000 开始数，第二个测试的消息就会被当成 WS 重投丢掉（症状是"什么
# 都没发生"，非常难查）。所以 id 池放模块级。
_ID_POOL = [5000, 1000]   # [message_id, update_id]


def _next_id(slot: int) -> int:
    _ID_POOL[slot] += 1
    return _ID_POOL[slot]


def _reply_to(raw) -> Optional[int]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, dict):
        try:
            return int(raw.get("message_id"))
        except (TypeError, ValueError):
            return None
    return None


class FakeBotAPI:
    def __init__(self, *, bot_id: int = 8976773826, username: str = "SPX_STG_bot",
                 privacy_off: bool = True):
        self.bot_id = bot_id
        self.username = username
        self.privacy_off = privacy_off
        self._lock = threading.RLock()
        self._pending: list[dict] = []      # 待投递的 update
        self._confirmed_upto = 0            # offset 语义：已确认的最大 update_id
        self.sent: list[dict] = []          # 发出去的消息（含 edit）
        self.messages: dict[tuple, dict] = {}   # (chat_id, message_id) → 当前状态
        self.commands: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self._failures: dict[str, list[dict]] = {}
        self.files: dict[str, str] = {}     # file_id → 本地路径（getFile 用）
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = 0

    # ── 生命周期 ──
    def start(self) -> str:
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # 静音
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type", "")
                method = self.path.rsplit("/", 1)[-1]
                if ctype.startswith("application/json"):
                    try:
                        payload = json.loads(raw or b"{}")
                    except ValueError:
                        payload = {}
                else:
                    payload = api._parse_multipart(raw, ctype)
                status, body = api.handle(method, payload)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                # 文件下载：/file/bot<token>/<path>
                if "/file/bot" in self.path:
                    local = api.files.get(self.path.rsplit("/", 1)[-1], "")
                    blob = b""
                    if local:
                        try:
                            with open(local, "rb") as f:
                                blob = f.read()
                        except OSError:
                            blob = b""
                    self.send_response(200 if blob else 404)
                    self.send_header("Content-Length", str(len(blob)))
                    self.end_headers()
                    self.wfile.write(blob)
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="fake-botapi")
        self._thread.start()
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @staticmethod
    def _parse_multipart(raw: bytes, ctype: str) -> dict:
        """只解析出字段名和文件名，够验证上传形状了。"""
        out: dict = {}
        marker = "boundary="
        if marker not in ctype:
            return out
        boundary = ctype.split(marker, 1)[1].strip().strip('"')
        for part in raw.split(f"--{boundary}".encode()):
            if b"Content-Disposition" not in part:
                continue
            head, _, body = part.partition(b"\r\n\r\n")
            head_text = head.decode("utf-8", "replace")
            name = ""
            filename = ""
            for chunk in head_text.split(";"):
                chunk = chunk.strip()
                if chunk.startswith("name="):
                    name = chunk[5:].strip('"')
                elif chunk.startswith("filename="):
                    raw_name = chunk.split("=", 1)[1]
                    # 这一段后面还粘着 "\r\nContent-Type: image/png"，先切到行尾
                    raw_name = raw_name.split("\r\n", 1)[0].strip()
                    filename = raw_name.strip('"')
            if not name:
                continue
            if filename:
                out[name] = {"filename": filename, "size": len(body.rstrip(b"\r\n--"))}
            else:
                out[name] = body.rstrip(b"\r\n").decode("utf-8", "replace")
        return out

    # ── 故障注入 ──
    def fail_once(self, method: str, code: int, description: str = "injected",
                  retry_after: float = 0.0) -> None:
        with self._lock:
            self._failures.setdefault(method, []).append({
                "code": code, "description": description, "retry_after": retry_after,
            })

    # ── 用户侧动作：往 bot 投一条消息 / 点一个按钮 ──
    def user_message(self, **msg) -> dict:
        """构造一条用户消息 update 并排进队列。返回这条 message。"""
        with self._lock:
            message = {
                "message_id": msg.pop("message_id", _next_id(0)),
                "date": int(msg.pop("date", time.time())),
                "chat": msg.pop("chat", {"id": -100777, "type": "supergroup"}),
                "from": msg.pop("from_user", {"id": 777, "first_name": "Yixin"}),
                **msg,
            }
            self._pending.append({"update_id": _next_id(1), "message": message})
            return message

    def user_click(self, chat_id, message_id, data: str) -> None:
        with self._lock:
            update_id = _next_id(1)
            self._pending.append({
                "update_id": update_id,
                "callback_query": {
                    "id": f"cq{update_id}",
                    "data": data,
                    "from": {"id": 777, "first_name": "Yixin"},
                    "message": {
                        "message_id": int(message_id),
                        "chat": {"id": int(chat_id), "type": "supergroup"},
                    },
                },
            })

    # ── 查询 ──
    def texts_of(self, chat_id) -> list[str]:
        """某个 chat 里 bot 现在实际显示的消息文本（按 message_id 排序）。"""
        with self._lock:
            rows = [(k[1], v) for k, v in self.messages.items() if str(k[0]) == str(chat_id)]
        return [v["text"] for _mid, v in sorted(rows)]

    def method_calls(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]

    # ── 请求分发 ──
    def handle(self, method: str, payload: dict) -> tuple[int, dict]:
        with self._lock:
            self.calls.append((method, payload))
            queued = self._failures.get(method)
            if queued:
                f = queued.pop(0)
                body = {"ok": False, "error_code": f["code"],
                        "description": f["description"]}
                if f["retry_after"]:
                    body["parameters"] = {"retry_after": f["retry_after"]}
                return 400 if f["code"] < 500 else 500, body

        if method == "getMe":
            return 200, {"ok": True, "result": {
                "id": self.bot_id, "is_bot": True, "first_name": "spx stg",
                "username": self.username,
                "can_read_all_group_messages": self.privacy_off,
            }}

        if method == "getUpdates":
            return 200, {"ok": True, "result": self._get_updates(payload)}

        if method in ("sendMessage", "sendPhoto", "sendDocument"):
            return 200, {"ok": True, "result": self._send(method, payload)}

        if method == "editMessageText":
            return self._edit_text(payload)

        if method == "editMessageReplyMarkup":
            key = (str(payload.get("chat_id")), int(payload.get("message_id") or 0))
            with self._lock:
                msg = self.messages.get(key)
                if msg is None:
                    return 400, {"ok": False, "error_code": 400,
                                 "description": "Bad Request: message to edit not found"}
                msg["reply_markup"] = payload.get("reply_markup")
            return 200, {"ok": True, "result": True}

        if method == "answerCallbackQuery":
            return 200, {"ok": True, "result": True}

        if method == "setMyCommands":
            self.commands = payload.get("commands") or []
            return 200, {"ok": True, "result": True}

        if method == "getMyCommands":
            return 200, {"ok": True, "result": self.commands}

        if method == "getFile":
            fid = payload.get("file_id", "")
            if fid not in self.files:
                return 400, {"ok": False, "error_code": 400,
                             "description": "Bad Request: file not found"}
            return 200, {"ok": True, "result": {"file_id": fid, "file_path": fid}}

        if method == "deleteWebhook":
            return 200, {"ok": True, "result": True}

        return 400, {"ok": False, "error_code": 404,
                     "description": f"Not Found: method not found ({method})"}

    def _get_updates(self, payload: dict) -> list[dict]:
        """长轮询 + offset：offset 之前的 update 视为已确认，直接丢弃。"""
        offset = payload.get("offset")
        timeout = float(payload.get("timeout") or 0)
        if offset is not None:
            with self._lock:
                self._confirmed_upto = max(self._confirmed_upto, int(offset) - 1)
                self._pending = [
                    u for u in self._pending if u["update_id"] > self._confirmed_upto
                ]
        deadline = time.time() + min(timeout, 3.0)   # 联调用，别真等 25s
        while True:
            with self._lock:
                if self._pending:
                    batch = list(self._pending)
                    return batch
            if time.time() >= deadline:
                return []
            time.sleep(0.05)

    def _send(self, method: str, payload: dict) -> dict:
        with self._lock:
            mid = _next_id(0)
            chat_id = str(payload.get("chat_id"))
            text = payload.get("text") or payload.get("caption") or ""
            entry = {
                "message_id": mid, "chat_id": chat_id, "text": text,
                "parse_mode": payload.get("parse_mode"),
                "reply_markup": payload.get("reply_markup"),
                # JSON 请求里是 dict；multipart（tg-cli 上传）里是 JSON 字符串
                "reply_to": _reply_to(payload.get("reply_parameters")),
                "method": method,
                "attachment": payload.get("photo") or payload.get("document"),
            }
            self.messages[(chat_id, mid)] = entry
            self.sent.append(entry)
            return {"message_id": mid, "chat": {"id": int(chat_id)},
                    "date": int(time.time())}

    def _edit_text(self, payload: dict) -> tuple[int, dict]:
        key = (str(payload.get("chat_id")), int(payload.get("message_id") or 0))
        new_text = payload.get("text") or ""
        with self._lock:
            msg = self.messages.get(key)
            if msg is None:
                return 400, {"ok": False, "error_code": 400,
                             "description": "Bad Request: message to edit not found"}
            if msg["text"] == new_text:
                return 400, {"ok": False, "error_code": 400,
                             "description": "Bad Request: message is not modified"}
            msg["text"] = new_text
            msg["parse_mode"] = payload.get("parse_mode")
            if "reply_markup" in payload:
                msg["reply_markup"] = payload.get("reply_markup")
            self.sent.append(dict(msg, method="editMessageText"))
        return 200, {"ok": True, "result": {"message_id": key[1],
                                            "chat": {"id": int(key[0])}}}
