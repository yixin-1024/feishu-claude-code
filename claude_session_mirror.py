#!/usr/bin/env python3
"""
claude_session_mirror.py — 把本机 Claude Code 的终端/IDE 会话单向镜像到 Lark。

设计（见 [[project_claude_session_mirror]]）：
  • 真相源：~/.claude/projects/<encoded-cwd>/<session-id>.jsonl（append-only 事件流）。
  • 触发：UserPromptSubmit hook（session_mirror_hook.py）做"门"——按 cwd 路由 + 排除
    bot 自己（CC_LARK_MIRROR_OFF），命中就往 active/ 写一个 marker。
  • 本守护进程是"引擎"：轮询 marker → tail 对应 jsonl → 把 prompt / Claude 回复 /
    产出文件实时 patch 进一张 Lark 卡片（⏳ 工作中 → ✅ 完成）。
  • 一轮是否结束靠"文件静默 idle_finalize_sec"判定，**不依赖 Stop hook**（更雅观/实时）。
  • 用事件 uuid 去重，所以 offset 不必精确，重叠重读也不会重复推送。
  • 纯 stdlib + 原始 Lark HTTP（复用 .env 里的 SPX app 凭证），无第三方依赖。

用法：
  python3 claude_session_mirror.py                # 常驻轮询（launchd 跑这个）
  python3 claude_session_mirror.py --once         # 跑一遍就退出（测试用）
  python3 claude_session_mirror.py --dry-run      # 不真发 Lark，卡片打到 stderr（测试用）
  python3 claude_session_mirror.py --config PATH  # 指定 config（默认 ~/.claude/session_mirror.json）
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

HOME = os.path.expanduser("~")
DEFAULT_CONFIG = os.path.join(HOME, ".claude", "session_mirror.json")
BASE_DIR = os.path.join(HOME, ".claude", "session_mirror")
ACTIVE_DIR = os.path.join(BASE_DIR, "active")
STATE_DIR = os.path.join(BASE_DIR, "state")
# 无缝衔接：thread_id → {session_id, cwd} 映射，cc-lark 的 session_store.get_current
# 读它来把"在 Lark 这个 thread 里回复"绑到对应的本机终端 session（--resume）。
THREADS_DIR = os.path.join(BASE_DIR, "threads")

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# 同一张卡片两次 patch 的最小间隔（秒），避免触发 Lark 限流。
MIN_PATCH_INTERVAL = 1.2
# seen uuid 集合上限（防止无限增长）。
SEEN_UUIDS_CAP = 800


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[mirror {ts}] {msg}", file=sys.stderr, flush=True)


# ── 配置 / 凭证 ──────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("lark_domain", "https://open.larksuite.com")
    cfg.setdefault("poll_interval_sec", 1.5)
    cfg.setdefault("idle_finalize_sec", 8)
    cfg.setdefault("max_reply_chars", 2500)
    cfg.setdefault("max_prompt_chars", 320)
    cfg.setdefault("routes", [])
    cfg.setdefault("exclude_prefixes", [])
    cfg.setdefault("default_chat_id", "")
    # backfill：首次镜像一个 session 时，把它"注册之前"的历史轮次也补成卡片
    # （banner + 每轮一张已完成卡）。默认关。
    cfg.setdefault("backfill", False)
    cfg.setdefault("backfill_max_turns", 50)
    cfg.setdefault("backfill_send_interval", 0.25)
    return cfg


def _parse_env_file(path: str) -> dict:
    """极简 .env 解析：KEY=VALUE，忽略注释和空行，去掉包裹引号。"""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k.strip()] = v
    except OSError:
        pass
    return out


def resolve_app_creds(cfg: dict) -> tuple[str, str]:
    """先看 config 里直配的 app_id/app_secret，否则从 env_file 里按 *_env key 取。"""
    app_id = cfg.get("app_id", "")
    app_secret = cfg.get("app_secret", "")
    if app_id and app_secret:
        return app_id, app_secret
    env_file = cfg.get("env_file", "")
    if env_file:
        env = _parse_env_file(os.path.expanduser(env_file))
        app_id = env.get(cfg.get("app_id_env", "SPX_APP_ID"), app_id)
        app_secret = env.get(cfg.get("app_secret_env", "SPX_APP_SECRET"), app_secret)
    # 最后兜底：进程环境变量
    app_id = app_id or os.environ.get(cfg.get("app_id_env", "SPX_APP_ID"), "")
    app_secret = app_secret or os.environ.get(cfg.get("app_secret_env", "SPX_APP_SECRET"), "")
    return app_id, app_secret


# ── Lark HTTP（原始接口，stdlib urllib）─────────────────────────────────

class Lark:
    def __init__(self, domain: str, app_id: str, app_secret: str, dry_run: bool = False):
        self.domain = domain.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self.dry_run = dry_run
        self._token = ""
        self._token_exp = 0.0

    def _req(self, method: str, path: str, body: dict, auth: bool = True) -> dict:
        url = f"{self.domain}{path}"
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if auth:
            headers["Authorization"] = f"Bearer {self._tenant_token()}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"Lark {method} {path} HTTP {e.code}: {detail}") from None

    def _tenant_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_exp - 120:
            return self._token
        out = self._req(
            "POST", "/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self.app_id, "app_secret": self.app_secret}, auth=False,
        )
        if out.get("code") != 0:
            raise RuntimeError(f"get tenant_access_token failed: {out}")
        self._token = out["tenant_access_token"]
        self._token_exp = now + out.get("expire", 7200)
        return self._token

    def send_card(self, chat_id: str, card: dict) -> str:
        if self.dry_run:
            log(f"[dry-run] send_card -> {chat_id}\n{json.dumps(card, ensure_ascii=False)[:600]}")
            return f"dry_{int(time.time()*1000)}"
        out = self._req(
            "POST", "/open-apis/im/v1/messages?receive_id_type=chat_id",
            {"receive_id": chat_id, "msg_type": "interactive",
             "content": json.dumps(card, ensure_ascii=False)},
        )
        if out.get("code") != 0:
            raise RuntimeError(f"send_card failed: {out}")
        return out["data"]["message_id"]

    def reply_card(self, parent_message_id: str, card: dict, in_thread: bool = True) -> str:
        if self.dry_run:
            log(f"[dry-run] reply_card -> {parent_message_id} (thread={in_thread})")
            return f"dry_{int(time.time()*1000)}"
        out = self._req(
            "POST", f"/open-apis/im/v1/messages/{parent_message_id}/reply",
            {"msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False),
             "reply_in_thread": in_thread},
        )
        if out.get("code") != 0:
            raise RuntimeError(f"reply_card failed: {out}")
        return out["data"]["message_id"]

    def get_thread_id(self, message_id: str) -> str:
        """查一条消息的 thread_id（话题群里消息自带 thread_id）。"""
        if self.dry_run or not message_id:
            return ""
        try:
            url = f"{self.domain}/open-apis/im/v1/messages/{message_id}"
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self._tenant_token()}"}, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            items = (out.get("data") or {}).get("items") or []
            if items:
                return items[0].get("thread_id", "") or ""
        except Exception as e:  # noqa: BLE001
            log(f"get_thread_id failed: {e}")
        return ""

    def patch_card(self, message_id: str, card: dict) -> None:
        if self.dry_run:
            log(f"[dry-run] patch_card {message_id}\n{json.dumps(card, ensure_ascii=False)[:600]}")
            return
        out = self._req(
            "PATCH", f"/open-apis/im/v1/messages/{message_id}",
            {"content": json.dumps(card, ensure_ascii=False)},
        )
        if out.get("code") != 0:
            raise RuntimeError(f"patch_card failed: {out}")


# ── 卡片渲染 ─────────────────────────────────────────────────────────────

def _chunk_markdown(text: str, size: int = 2700) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(line) > size:
            if cur:
                chunks.append(cur); cur = ""
            for i in range(0, len(line), size):
                chunks.append(line[i:i + size])
            continue
        if len(cur) + len(line) + 1 > size:
            chunks.append(cur); cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def render_card(turn: dict, cfg: dict) -> dict:
    """每个语义段落用独立的 card 元素 + 一条 hr，靠卡片自身排版，不依赖 markdown 内换行
    （Lark 会折叠单 blob 里的连续换行，分元素才稳）。"""
    proj = os.path.basename(turn.get("cwd", "").rstrip("/")) or "?"
    title = turn.get("title") or ""
    title_part = f" · {title}" if title else ""
    prompt = turn.get("prompt", "").strip()
    if len(prompt) > cfg["max_prompt_chars"]:
        prompt = prompt[:cfg["max_prompt_chars"]] + " …"
    reply = turn.get("reply", "").strip()
    truncated = False
    if len(reply) > cfg["max_reply_chars"]:
        reply = reply[:cfg["max_reply_chars"]]
        truncated = True

    status = "✅ 完成" if turn.get("finalized") else "⏳ 工作中…"
    elements: list[dict] = [
        {"tag": "markdown", "content": f"🧑 **你** · `{proj}`{title_part}"}
    ]
    if prompt:
        quoted = "\n".join("> " + ln for ln in prompt.split("\n"))
        elements.append({"tag": "markdown", "content": quoted})
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": f"🤖 **Claude** · {status}"})

    if reply:
        reply_md = reply + ("\n\n*（回复较长已截断，完整见本机终端）*" if truncated else "")
        for c in _chunk_markdown(reply_md):
            elements.append({"tag": "markdown", "content": c})
    elif not turn.get("finalized"):
        elements.append({"tag": "markdown", "content": "*思考中…*"})

    arts = turn.get("artifacts", [])
    if arts:
        shown = arts[:8]
        names = ", ".join(f"`{os.path.basename(a)}`" for a in shown)
        more = f" (+{len(arts) - len(shown)})" if len(arts) > len(shown) else ""
        elements.append({"tag": "markdown", "content": f"📦 **产出**：{names}{more}"})

    sid = turn.get("session_id", "")
    elements.append({"tag": "markdown", "content": f"—— `cc-mirror` · session `{sid[:8]}`"})
    return {"schema": "2.0", "body": {"elements": elements}}


# ── 事件解析 ─────────────────────────────────────────────────────────────

def _ts_epoch(ev: dict) -> float:
    s = ev.get("timestamp")
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _user_text(ev: dict) -> str:
    """真实用户 prompt 的文本；若是 tool_result / 命令注入则返回空串。"""
    msg = ev.get("message", {}) or {}
    if msg.get("role") not in (None, "user"):
        return ""
    content = msg.get("content", "")
    if isinstance(content, list):
        # 含 tool_result 块的不是用户 prompt
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return ""
        text = " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    else:
        text = str(content).strip()
    return text


def _is_command_or_meta(text: str) -> bool:
    """跳过 slash 命令注入 / 系统注入这类非真实输入。"""
    if not text:
        return True
    t = text.lstrip()
    if t.startswith("<command-") or t.startswith("<local-command"):
        return True
    if t.startswith("Caveat:") or t.startswith("<system-reminder"):
        return True
    return False


def _assistant_blocks(ev: dict):
    msg = ev.get("message", {}) or {}
    content = msg.get("content", [])
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                yield b


# ── 镜像引擎 ─────────────────────────────────────────────────────────────

class Mirror:
    def __init__(self, cfg: dict, lark: Lark):
        self.cfg = cfg
        self.lark = lark
        os.makedirs(ACTIVE_DIR, exist_ok=True)
        os.makedirs(STATE_DIR, exist_ok=True)
        os.makedirs(THREADS_DIR, exist_ok=True)

    # ---- 路由 ----
    def resolve_chat_id(self, cwd: str) -> str:
        cwd = cwd or ""
        for pref in self.cfg.get("exclude_prefixes", []):
            if cwd.startswith(pref):
                return ""
        best, best_len = "", -1
        for r in self.cfg.get("routes", []):
            pref = r.get("prefix", "")
            if pref and cwd.startswith(pref) and len(pref) > best_len:
                best, best_len = r.get("chat_id", ""), len(pref)
        if best:
            return best
        return self.cfg.get("default_chat_id", "")

    # ---- state 读写 ----
    def _state_path(self, sid: str) -> str:
        return os.path.join(STATE_DIR, f"{sid}.json")

    def load_state(self, sid: str) -> dict:
        try:
            with open(self._state_path(sid), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def save_state(self, sid: str, st: dict) -> None:
        # seen_uuids 截断
        seen = st.get("seen_uuids", [])
        if len(seen) > SEEN_UUIDS_CAP:
            st["seen_uuids"] = seen[-SEEN_UUIDS_CAP:]
        tmp = self._state_path(sid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, self._state_path(sid))

    def _write_thread_link(self, thread_id, session_id, cwd, chat_id, preview) -> None:
        """落 thread_id → {session_id, cwd} 映射，供 cc-lark get_current 做无缝衔接。"""
        try:
            link = {
                "thread_id": thread_id, "session_id": session_id,
                "cwd": cwd, "chat_id": chat_id, "preview": preview,
            }
            tmp = os.path.join(THREADS_DIR, f".{thread_id}.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(link, f, ensure_ascii=False)
            os.replace(tmp, os.path.join(THREADS_DIR, f"{thread_id}.json"))
            log(f"thread link {thread_id[:12]} -> session {session_id[:8]} ({os.path.basename(cwd)})")
        except Exception as e:  # noqa: BLE001
            log(f"write_thread_link failed: {e}")

    def _parse_all_turns(self, tpath: str):
        """读整个 jsonl，按真实 user prompt 切成 turns。返回 (turns, title)。
        每个 turn：{prompt, reply, artifacts, uuids, ts}。"""
        turns, cur, title = [], None, ""
        try:
            with open(tpath, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("isSidechain"):
                        continue
                    etype = ev.get("type")
                    if etype == "ai-title":
                        t = ev.get("aiTitle")
                        if t:
                            title = t
                        continue
                    if etype == "user":
                        text = _user_text(ev)
                        if _is_command_or_meta(text):
                            continue
                        if cur:
                            turns.append(cur)
                        cur = {"prompt": text, "reply": "", "artifacts": [],
                               "uuids": [ev.get("uuid")], "ts": _ts_epoch(ev)}
                    elif etype == "assistant":
                        if not cur:
                            continue
                        for b in _assistant_blocks(ev):
                            bt = b.get("type")
                            if bt == "text":
                                cur["reply"] += b.get("text", "")
                            elif bt == "tool_use" and b.get("name") in WRITE_TOOLS:
                                fp = (b.get("input") or {}).get("file_path") \
                                    or (b.get("input") or {}).get("notebook_path")
                                if fp and fp not in cur["artifacts"]:
                                    cur["artifacts"].append(fp)
                        if ev.get("uuid"):
                            cur["uuids"].append(ev.get("uuid"))
            if cur:
                turns.append(cur)
        except OSError:
            pass
        return turns, title

    def _backfill(self, sid, marker, chat_id, st, tpath) -> None:
        """首次镜像该 session 时，把"注册之前"的历史轮次补成卡片：先发一个 banner 当
        thread 根，再每轮一张已完成卡（thread reply）。当前 in-flight 轮（ts>=min_ts）
        留给正常 live 逻辑。历史轮的 uuid 全记进 seen，避免后续 live 读到时重复推。"""
        turns, title = self._parse_all_turns(tpath)
        hist = [t for t in turns if t["ts"] < st["min_ts"]]
        if not hist:
            return
        cap = int(self.cfg.get("backfill_max_turns", 50) or 50)
        omitted = max(0, len(hist) - cap)
        hist = hist[-cap:]
        cwd = marker.get("cwd", "")
        proj = os.path.basename(cwd.rstrip("/")) or "?"
        title_part = f" · {title}" if title else ""
        note = f"（更早 {omitted} 轮已省略）" if omitted else ""
        banner = {"schema": "2.0", "body": {"elements": [
            {"tag": "markdown", "content": f"📜 **历史会话回放** · `{proj}`{title_part}"},
            {"tag": "markdown", "content": f"session `{sid[:8]}` · 共 {len(hist)} 轮{note}"},
        ]}}
        try:
            root = self.lark.send_card(chat_id, banner)
        except Exception as e:  # noqa: BLE001
            log(f"backfill banner failed: {e}")
            return
        st["root_message_id"] = root
        tid = self.lark.get_thread_id(root)
        if tid:
            st["thread_id"] = tid
            self._write_thread_link(tid, sid, cwd, chat_id, hist[-1]["prompt"][:40])
        seen = list(st.get("seen_uuids", []))
        interval = float(self.cfg.get("backfill_send_interval", 0.25) or 0)
        for t in hist:
            turn = {"session_id": sid, "cwd": cwd, "title": title,
                    "prompt": t["prompt"], "reply": t["reply"],
                    "artifacts": t["artifacts"], "finalized": True}
            try:
                self.lark.reply_card(root, render_card(turn, self.cfg), in_thread=True)
            except Exception as e:  # noqa: BLE001
                log(f"backfill turn failed: {e}")
            seen.extend(u for u in t["uuids"] if u)
            if interval:
                time.sleep(interval)
        st["seen_uuids"] = seen
        st["title"] = title
        st["backfilled"] = True
        log(f"backfilled session={sid[:8]} {len(hist)} turns (omitted {omitted}) -> {root}")

    # ---- 单个会话处理 ----
    def process_session(self, marker: dict) -> None:
        sid = marker.get("session_id")
        tpath = marker.get("transcript_path")
        chat_id = marker.get("chat_id")
        if not (sid and tpath and chat_id):
            return
        if not os.path.isfile(tpath):
            return

        st = self.load_state(sid)
        if not st:
            # 从头读，靠 min_ts 过滤掉注册前的历史轮次——不能用 filesize-REWIND 起步：
            # claude -p 这类突发会话会在守护进程首次轮询前就把整段写完，prompt 滚出窗口被漏掉。
            st = {
                "offset": 0,
                "min_ts": float(marker.get("registered_at", time.time())) - 5.0,
                "root_message_id": "",
                "seen_uuids": [],
                "turn": None,
                "cwd": marker.get("cwd", ""),
                "chat_id": chat_id,
            }
            if self.cfg.get("backfill"):
                try:
                    self._backfill(sid, marker, chat_id, st, tpath)
                except Exception as e:  # noqa: BLE001
                    log(f"backfill error: {e}")
        seen = set(st.get("seen_uuids", []))
        turn = st.get("turn")

        # 读新增内容（只消费到最后一个换行）
        new_lines, new_offset = self._read_new_lines(tpath, st["offset"])
        file_grew = new_offset > st["offset"]
        st["offset"] = new_offset

        any_event = False
        for line in new_lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            uid = ev.get("uuid")
            if uid and uid in seen:
                continue
            etype = ev.get("type")
            if etype == "ai-title":
                t = ev.get("aiTitle")
                if t:
                    st["title"] = t
                    if turn and not turn.get("finalized"):
                        turn["title"] = t
                        turn["dirty"] = True
                continue
            if etype not in ("user", "assistant"):
                continue
            if ev.get("isSidechain"):
                if uid:
                    seen.add(uid)
                continue

            if etype == "user":
                text = _user_text(ev)
                if _is_command_or_meta(text):
                    if uid:
                        seen.add(uid)
                    continue
                if _ts_epoch(ev) < st["min_ts"]:
                    if uid:
                        seen.add(uid)
                    continue
                # 新一轮：先结算上一轮
                if turn and not turn.get("finalized"):
                    self._finalize(turn)
                turn = self._start_turn(sid, marker, chat_id, st, text)
                any_event = True
            elif etype == "assistant":
                if not turn:
                    if uid:
                        seen.add(uid)
                    continue
                # 已结算的 turn 又收到本轮输出（长 pause 跨过 idle 后又恢复）→ 重新打开
                if turn.get("finalized"):
                    turn["finalized"] = False
                for b in _assistant_blocks(ev):
                    bt = b.get("type")
                    if bt == "text":
                        turn["reply"] += b.get("text", "")
                    elif bt == "tool_use" and b.get("name") in WRITE_TOOLS:
                        fp = (b.get("input") or {}).get("file_path") \
                            or (b.get("input") or {}).get("notebook_path")
                        if fp and fp not in turn["artifacts"]:
                            turn["artifacts"].append(fp)
                turn["dirty"] = True
                turn["last_event_ts"] = time.time()
                any_event = True
            if uid:
                seen.add(uid)

        now = time.time()
        # 流式 patch
        if turn and turn.get("dirty") and not turn.get("finalized"):
            if now - turn.get("last_patch_ts", 0) >= MIN_PATCH_INTERVAL:
                self._patch(turn)
                turn["dirty"] = False
                turn["last_patch_ts"] = now
        # 静默结算（不依赖 Stop hook）
        if turn and not turn.get("finalized"):
            last_act = max(turn.get("last_event_ts", 0),
                           os.path.getmtime(tpath) if os.path.isfile(tpath) else 0)
            if now - last_act > self.cfg["idle_finalize_sec"]:
                self._finalize(turn)

        st["seen_uuids"] = list(seen)
        st["turn"] = turn
        st["root_message_id"] = st.get("root_message_id", "")
        if any_event or file_grew:
            st["last_seen"] = now
        self.save_state(sid, st)

    def _read_new_lines(self, path: str, offset: int) -> tuple[list[str], int]:
        try:
            size = os.path.getsize(path)
            if size < offset:  # 文件被重写/截断
                offset = 0
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read()
            if not data:
                return [], offset
            # 只消费到最后一个换行，半行留到下次
            last_nl = data.rfind(b"\n")
            if last_nl == -1:
                return [], offset
            consumed = data[:last_nl + 1]
            new_offset = offset + len(consumed)
            text = consumed.decode("utf-8", "replace")
            return text.split("\n"), new_offset
        except OSError:
            return [], offset

    def _start_turn(self, sid, marker, chat_id, st, prompt_text) -> dict:
        turn = {
            "session_id": sid,
            "cwd": marker.get("cwd", ""),
            "title": st.get("title") or marker.get("title", ""),
            "chat_id": chat_id,
            "prompt": prompt_text,
            "reply": "",
            "artifacts": [],
            "finalized": False,
            "dirty": False,
            "card_message_id": "",
            "last_event_ts": time.time(),
            "last_patch_ts": 0.0,
        }
        card = render_card(turn, self.cfg)
        root = st.get("root_message_id", "")
        try:
            if not root:
                mid = self.lark.send_card(chat_id, card)
                st["root_message_id"] = mid
                # 无缝衔接：把这个 thread 绑到终端 session，让 Lark 里回复能 --resume
                tid = self.lark.get_thread_id(mid)
                if tid:
                    st["thread_id"] = tid
                    self._write_thread_link(tid, sid, marker.get("cwd", ""),
                                            chat_id, prompt_text[:40])
            else:
                mid = self.lark.reply_card(root, card, in_thread=True)
            turn["card_message_id"] = mid
            log(f"new turn session={sid[:8]} prompt={prompt_text[:40]!r} -> {mid}")
        except Exception as e:  # noqa: BLE001
            log(f"send card failed: {e}")
        turn["last_patch_ts"] = time.time()
        return turn

    def _patch(self, turn: dict) -> None:
        mid = turn.get("card_message_id")
        if not mid:
            return
        try:
            self.lark.patch_card(mid, render_card(turn, self.cfg))
        except Exception as e:  # noqa: BLE001
            log(f"patch failed: {e}")

    def _finalize(self, turn: dict) -> None:
        turn["finalized"] = True
        self._patch(turn)
        log(f"finalized session={turn.get('session_id','')[:8]} "
            f"reply_len={len(turn.get('reply',''))} arts={len(turn.get('artifacts',[]))}")

    # ---- 主循环 ----
    def list_markers(self) -> list[dict]:
        out = []
        try:
            names = os.listdir(ACTIVE_DIR)
        except OSError:
            return out
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(ACTIVE_DIR, name), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def tick(self) -> None:
        for marker in self.list_markers():
            try:
                self.process_session(marker)
            except Exception as e:  # noqa: BLE001
                log(f"process_session error: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not cfg.get("enabled", True):
        log("mirror disabled in config; exiting")
        return
    app_id, app_secret = resolve_app_creds(cfg)
    if not (app_id and app_secret) and not args.dry_run:
        log("missing app credentials; exiting")
        sys.exit(1)
    lark = Lark(cfg["lark_domain"], app_id, app_secret, dry_run=args.dry_run)
    mirror = Mirror(cfg, lark)
    log(f"started (once={args.once} dry_run={args.dry_run}) "
        f"routes={len(cfg.get('routes', []))} default={cfg.get('default_chat_id') or '-'}")

    if args.once:
        mirror.tick()
        return
    interval = cfg["poll_interval_sec"]
    while True:
        mirror.tick()
        time.sleep(interval)


if __name__ == "__main__":
    main()
