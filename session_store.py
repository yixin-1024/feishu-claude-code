import asyncio
import json
import os
import subprocess
import ssl
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

from bot_config import SESSIONS_DIR, DEFAULT_MODEL, DEFAULT_CWD, PERMISSION_MODE, DEFAULT_RUNNER

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def scan_cli_sessions(limit: int = 30) -> list[dict]:
    """
    扫描 ~/.claude/projects/ 下所有 session .jsonl 文件。
    返回列表，每项：{session_id, started_at, cwd, preview, source="terminal"}
    按最近修改时间倒序，最多返回 limit 条。
    """
    results = []
    if not os.path.isdir(CLAUDE_PROJECTS_DIR):
        return results

    for project_dir in os.listdir(CLAUDE_PROJECTS_DIR):
        project_path = os.path.join(CLAUDE_PROJECTS_DIR, project_dir)
        if not os.path.isdir(project_path):
            continue
        for fname in os.listdir(project_path):
            if not fname.endswith(".jsonl"):
                continue
            session_id = fname[:-6]  # 去掉 .jsonl
            fpath = os.path.join(project_path, fname)
            mtime = os.path.getmtime(fpath)
            results.append((mtime, session_id, fpath))

    # 按最近修改时间倒序
    results.sort(key=lambda x: x[0], reverse=True)
    results = results[:limit]

    sessions = []
    for mtime, session_id, fpath in results:
        info = _parse_session_file(fpath, session_id, mtime)
        sessions.append(info)
    return sessions


import re

def _clean_preview(text: str) -> str:
    """清洗 preview 文本，去掉系统注入内容"""
    # 去掉 [环境：...] 前缀
    text = re.sub(r'^\[环境：[^\]]*\]\s*', '', text)
    # 去掉 <local-command-caveat>...</local-command-caveat> 及其后的系统文本
    text = re.sub(r'<local-command-caveat>.*?</local-command-caveat>\s*', '', text, flags=re.DOTALL)
    # 去掉 <system-reminder>...</system-reminder>
    text = re.sub(r'<system-reminder>.*?</system-reminder>\s*', '', text, flags=re.DOTALL)
    # 去掉其他 XML-like 系统标签
    text = re.sub(r'<[a-z_-]+>.*?</[a-z_-]+>\s*', '', text, flags=re.DOTALL)
    return text.strip()


def _parse_session_file(fpath: str, session_id: str, mtime: float) -> dict:
    """从 .jsonl 文件提取首条用户消息（作为 preview）、cwd、时间戳"""
    preview = ""
    cwd = ""
    started_at = datetime.fromtimestamp(mtime).isoformat()

    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "user":
                    continue
                # 取 cwd
                if not cwd and d.get("cwd"):
                    cwd = d["cwd"]
                # 取 timestamp
                if d.get("timestamp"):
                    started_at = d["timestamp"][:19].replace("T", " ")
                # 取用户消息文本
                msg = d.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content if b.get("type") == "text"
                    ).strip()
                else:
                    text = str(content).strip()
                if text:
                    text = _clean_preview(text)
                    if text:
                        preview = text[:50]
                        break
    except OSError:
        pass

    return {
        "session_id": session_id,
        "started_at": started_at,
        "cwd": cwd,
        "preview": preview,
        "source": "terminal",
    }

def _find_session_file(session_id: str) -> Optional[str]:
    """在 ~/.claude/projects/ 下找到 session 对应的 .jsonl 文件"""
    if not os.path.isdir(CLAUDE_PROJECTS_DIR):
        return None
    for project_dir in os.listdir(CLAUDE_PROJECTS_DIR):
        project_path = os.path.join(CLAUDE_PROJECTS_DIR, project_dir)
        if not os.path.isdir(project_path):
            continue
        fpath = os.path.join(project_path, f"{session_id}.jsonl")
        if os.path.isfile(fpath):
            return fpath
    return None


def _extract_conversation_context(fpath: str, max_chars: int = 2000) -> str:
    """从 .jsonl 提取前几轮对话文本，用于生成摘要"""
    parts = []
    total = 0
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if d.get("type") not in ("user", "assistant"):
                    continue
                if d.get("isMeta"):
                    continue
                msg = d.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content
                        if b.get("type") == "text"
                    ).strip()
                else:
                    text = str(content).strip()
                if not text:
                    continue
                text = _clean_preview(text)
                if not text:
                    continue
                role = "用户" if d["type"] == "user" else "助手"
                part = f"{role}: {text}"
                parts.append(part)
                total += len(part)
                if total >= max_chars:
                    break
    except OSError:
        pass
    return "\n".join(parts)


def _get_api_token() -> Optional[str]:
    """获取 Claude API token，先试 credentials 文件，再试 keychain"""
    try:
        creds_path = os.path.expanduser("~/.claude/.credentials.json")
        if os.path.isfile(creds_path):
            with open(creds_path) as f:
                creds = json.load(f)
            return creds["claudeAiOauth"]["accessToken"]
        from account_switcher import decode_security_stdout, ensure_keychain_intact
        ensure_keychain_intact()  # keychain 被外部进程写丢时自愈
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        creds = json.loads(decode_security_stdout(result.stdout))
        return creds["claudeAiOauth"]["accessToken"]
    except Exception:
        return None


def generate_summary(session_id: str, token: Optional[str] = None) -> str:
    """为指定 session 调用 haiku 生成一句话摘要"""
    fpath = _find_session_file(session_id)
    if not fpath:
        return ""
    context = _extract_conversation_context(fpath)
    if not context:
        return ""
    if token is None:
        token = _get_api_token()
    if not token:
        return ""

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 40,
        "messages": [{"role": "user", "content": (
            "用10-20个中文字总结这段对话的主题。"
            "直接返回摘要，不加引号不加标点。\n\n"
            + context[:2000]
        )}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            result = json.loads(resp.read())
            blocks = result.get("content", [])
            if blocks and blocks[0].get("type") == "text":
                return blocks[0]["text"].strip()
    except urllib.error.HTTPError as e:
        print(f"[摘要API] {session_id[:8]} HTTP {e.code}: {e.read().decode()[:100]}", flush=True)
    except Exception as e:
        print(f"[摘要API] {session_id[:8]} {type(e).__name__}: {e}", flush=True)
    return ""


def _write_custom_title(session_id: str, title: str):
    """将摘要作为 custom-title 写入 .jsonl，让 CLI 终端也能显示"""
    fpath = _find_session_file(session_id)
    if not fpath:
        return
    # 检查是否已有 custom-title 行，幂等
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "custom-title":
                    return  # 已存在，跳过
    except OSError:
        return
    # 追加 custom-title 行
    entry = json.dumps({
        "type": "custom-title",
        "customTitle": title,
        "sessionId": session_id,
    }, ensure_ascii=False)
    try:
        with open(fpath, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except OSError:
        pass


LEGACY_SESSIONS_FILE = os.path.join(SESSIONS_DIR, "sessions.json")


def _sessions_file_for(profile: str) -> str:
    """每个 profile 一份独立的 sessions json。"""
    return os.path.join(SESSIONS_DIR, f"sessions-{profile}.json")


class Session:
    def __init__(
        self,
        session_id: Optional[str],
        model: str,
        cwd: str,
        permission_mode: str,
        workspace: str = "",
        runner: str = "claude",
    ):
        self.session_id = session_id
        self.model = model
        self.cwd = cwd
        self.permission_mode = permission_mode
        self.workspace = workspace
        self.runner = runner


class SessionStore:
    def __init__(
        self,
        profile: str = "default",
        default_cwd: Optional[str] = None,
        chat_default_cwd: Optional[dict[str, str]] = None,
        default_runner: Optional[str] = None,
        default_model: Optional[str] = None,
    ):
        """
        每个 profile 独占一个 SessionStore 实例和一份 json 文件。
        default_cwd 给新用户的初始 cwd（覆盖全局 DEFAULT_CWD）。
        chat_default_cwd 给指定 chat_id 的初始 cwd（仅在新建 chat 数据时生效，
        不会覆盖 sessions.json 里已有的 chat）。键用 normalized chat_key：
        私聊用 "private"，群聊用 chat_id 原值。
        """
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        self.profile = profile
        self._default_cwd = default_cwd or DEFAULT_CWD
        self._default_runner = (default_runner or DEFAULT_RUNNER or "claude").strip().lower()
        if self._default_runner not in {"claude", "codex"}:
            self._default_runner = "claude"
        self._default_model = default_model or DEFAULT_MODEL
        self._chat_default_cwd = chat_default_cwd or {}
        self._sessions_file = _sessions_file_for(profile)
        self._save_lock = asyncio.Lock()  # 保护 _save() 的全局锁
        self._data: dict = self._load()
        self._dedup_all_histories()

    def _load(self) -> dict:
        # 优先读 profile 专属文件
        if os.path.exists(self._sessions_file):
            try:
                with open(self._sessions_file) as f:
                    return json.load(f)
            except Exception:
                pass
        # 向后兼容：profile=default 且旧的 sessions.json 存在时读一次，下次 save 会写新文件
        if self.profile == "default" and os.path.exists(LEGACY_SESSIONS_FILE):
            try:
                with open(LEGACY_SESSIONS_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(self):
        tmp = self._sessions_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self._sessions_file)  # 原子操作，崩溃时不会截断原文件

    async def _save_async(self):
        """异步保存，使用锁保护并发写入（原子写入）"""
        async with self._save_lock:
            tmp = self._sessions_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._sessions_file)

    async def _bg_generate_summary(self, user_id: str, session_id: str):
        """后台生成会话摘要，不阻塞消息流"""
        try:
            summary = await asyncio.to_thread(generate_summary, session_id)
            if summary:
                self._data.setdefault(user_id, {}).setdefault("summaries", {})[session_id] = summary
                await asyncio.to_thread(_write_custom_title, session_id, summary)
                await self._save_async()
        except Exception:
            pass

    def _dedup_all_histories(self):
        """启动时清理所有用户 history 中的重复 session_id"""
        changed = False
        for user in self._data.values():
            for chat_data in user.values():
                if not isinstance(chat_data, dict) or "history" not in chat_data:
                    continue
                history = chat_data.get("history", [])
                seen = set()
                cleaned = []
                # 倒序遍历，保留每个 session_id 最后出现的那条
                for h in reversed(history):
                    sid = h.get("session_id")
                    if sid and sid not in seen:
                        seen.add(sid)
                        cleaned.append(h)
                cleaned.reverse()
                if len(cleaned) != len(history):
                    chat_data["history"] = cleaned
                    changed = True
        if changed:
            self._save()

    def _user(self, user_id: str) -> dict:
        return self._data.setdefault(user_id, {})

    def _default_current(self, chat_key: Optional[str] = None) -> dict:
        # chat_key 在话题群里是 "oc_xxx:omt_yyy" 复合形式，env 里配置的 CHAT_CWD_<id>
        # 用的是裸 chat_id，所以先按完整 key 查，未命中再剥掉 thread 后缀查一次。
        cwd = self._default_cwd
        if chat_key:
            cwd = self._chat_default_cwd.get(chat_key)
            if cwd is None:
                raw_chat_id = chat_key.split(":", 1)[0]
                cwd = self._chat_default_cwd.get(raw_chat_id, self._default_cwd)
        return {
            "session_id": None,
            "runner": self._default_runner,
            "model": self._default_model,
            "cwd": cwd,
            "permission_mode": PERMISSION_MODE,
            "started_at": datetime.now().isoformat(),
            "preview": "",
            "workspace": "",
            # 话题群：上一次处理过的 thread 消息 id，用于增量拉取新评论
            "last_seen_message_id": "",
            "last_usage": {},
        }

    def _normalize_chat_key(self, user_id: str, chat_id: str) -> str:
        return "private" if chat_id == user_id else chat_id

    def _ensure_current_defaults(self, current: dict, chat_key: Optional[str] = None) -> bool:
        changed = False
        defaults = self._default_current(chat_key=chat_key)
        stored_runner = current.get("runner")
        if stored_runner != self._default_runner:
            current["runner"] = self._default_runner
            current["model"] = self._default_model
            current["session_id"] = None
            current["preview"] = ""
            current["started_at"] = datetime.now().isoformat()
            changed = True
        elif self._default_runner == "codex" and str(current.get("model") or "").startswith("claude-"):
            current["model"] = self._default_model
            current["session_id"] = None
            current["preview"] = ""
            current["started_at"] = datetime.now().isoformat()
            changed = True
        for key, value in defaults.items():
            if key not in current:
                current[key] = value
                changed = True
        return changed

    async def _ensure_chat_data(self, user_id: str, chat_id: str) -> dict:
        user = self._user(user_id)
        chat_key = self._normalize_chat_key(user_id, chat_id)
        changed = False

        if chat_key not in user:
            # 兼容旧结构：首次访问私聊时把顶层 current/history 迁入 private。
            if chat_key == "private" and isinstance(user.get("current"), dict):
                user[chat_key] = {
                    "current": user.pop("current"),
                    "history": user.pop("history", []),
                }
            else:
                user[chat_key] = {
                    "current": self._default_current(chat_key=chat_key),
                    "history": [],
                }
            changed = True

        chat_data = user[chat_key]
        if self._ensure_current_defaults(
            chat_data.setdefault("current", self._default_current(chat_key=chat_key)),
            chat_key=chat_key,
        ):
            changed = True
        if "history" not in chat_data:
            chat_data["history"] = []
            changed = True

        if changed:
            await self._save_async()

        return chat_data

    def get_summary(self, user_id: str, session_id: str) -> str:
        """获取缓存的摘要"""
        return self._user(user_id).get("summaries", {}).get(session_id, "")

    def get_all_unsummarized(self) -> list[tuple[str, str]]:
        """返回所有缺摘要的 (user_id, session_id) 列表"""
        results = []
        for user_id, user_data in self._data.items():
            summaries = user_data.get("summaries", {})
            for chat_key, chat_data in user_data.items():
                if not isinstance(chat_data, dict) or "history" not in chat_data:
                    continue
                cur_sid = chat_data.get("current", {}).get("session_id")
                if cur_sid and not summaries.get(cur_sid):
                    results.append((user_id, cur_sid))
                for h in chat_data.get("history", []):
                    sid = h.get("session_id", "")
                    if sid and not summaries.get(sid):
                        results.append((user_id, sid))
        return results

    async def batch_set_summaries(self, user_id: str, summaries: dict):
        """批量缓存摘要并保存"""
        user = self._user(user_id)
        user.setdefault("summaries", {}).update(summaries)
        await self._save_async()

    async def get_current(self, user_id: str, chat_id: str) -> Session:
        """Get current session config for a specific chat"""
        cur = await self.get_current_raw(user_id, chat_id)
        return Session(
            session_id=cur.get("session_id"),
            model=cur.get("model", self._default_model),
            cwd=cur.get("cwd", self._default_cwd),
            permission_mode=cur.get("permission_mode", PERMISSION_MODE),
            workspace=cur.get("workspace", ""),
            runner=cur.get("runner", self._default_runner),
        )

    async def on_claude_response(
        self,
        user_id: str,
        chat_id: str,
        new_session_id: str,
        first_message: str,
        usage: Optional[dict] = None,
    ):
        """Claude 回复后用返回的 session_id 更新状态"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        cur = chat_data["current"]
        old_id = cur.get("session_id")

        if old_id and old_id != new_session_id:
            # 归档旧 session（先去重，避免同一 session_id 重复出现）
            chat_data["history"] = [h for h in chat_data["history"] if h["session_id"] != old_id]
            chat_data["history"].append({
                "session_id": old_id,
                "runner": cur.get("runner", self._default_runner),
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat_data["history"] = chat_data["history"][-20:]
            cur["started_at"] = datetime.now().isoformat()
            # 异步生成摘要，不阻塞消息流
            summaries = self._data[user_id].get("summaries", {})
            if not summaries.get(old_id):
                asyncio.create_task(self._bg_generate_summary(user_id, old_id))

        cur["session_id"] = new_session_id
        if usage:
            cur["last_usage"] = usage
        if not cur.get("preview"):
            cur["preview"] = _clean_preview(first_message)[:40]
        await self._save_async()

    async def on_agent_response(
        self,
        user_id: str,
        chat_id: str,
        new_session_id: str,
        first_message: str,
        usage: Optional[dict] = None,
    ):
        """Agent 回复后用返回的 session/thread id 更新状态。"""
        await self.on_claude_response(user_id, chat_id, new_session_id, first_message, usage=usage)

    async def new_session(self, user_id: str, chat_id: str) -> str:
        """Start a new session for a specific chat, return old session title"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        cur = chat_data["current"]
        old_title = ""

        if cur.get("session_id"):
            old_id = cur["session_id"]
            # Archive current session (dedup first)
            chat_data["history"] = [h for h in chat_data.get("history", []) if h["session_id"] != old_id]
            chat_data["history"].append({
                "session_id": old_id,
                "runner": cur.get("runner", self._default_runner),
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat_data["history"] = chat_data["history"][-20:]

            # 摘要：有缓存就用，没有就后台生成（不阻塞 /new 响应）
            summaries = self._data[user_id].get("summaries", {})
            old_title = summaries.get(old_id, "")
            if not old_title:
                asyncio.create_task(self._bg_generate_summary(user_id, old_id))

        # Create new session
        chat_data["current"] = {
            "session_id": None,
            "runner": cur.get("runner", self._default_runner),
            "model": cur.get("model", self._default_model),
            "cwd": cur.get("cwd", self._default_cwd),
            "permission_mode": cur.get("permission_mode", PERMISSION_MODE),
            "started_at": datetime.now().isoformat(),
            "preview": "",
            "workspace": cur.get("workspace", ""),
            "last_seen_message_id": "",
            "last_usage": {},
        }
        await self._save_async()
        return old_title

    async def reset_current_to_defaults(self, user_id: str, chat_id: str) -> str:
        """Start a fresh session and reset current chat config to profile defaults."""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        cur = chat_data["current"]
        old_title = ""

        if cur.get("session_id"):
            old_id = cur["session_id"]
            chat_data["history"] = [
                h for h in chat_data.get("history", []) if h["session_id"] != old_id
            ]
            chat_data["history"].append({
                "session_id": old_id,
                "runner": cur.get("runner", self._default_runner),
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat_data["history"] = chat_data["history"][-20:]
            summaries = self._data[user_id].get("summaries", {})
            old_title = summaries.get(old_id, "")
            if not old_title:
                asyncio.create_task(self._bg_generate_summary(user_id, old_id))

        chat_key = self._normalize_chat_key(user_id, chat_id)
        chat_data["current"] = self._default_current(chat_key=chat_key)
        await self._save_async()
        return old_title

    async def set_model(self, user_id: str, chat_id: str, model: str):
        """Set model for a specific chat"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        cur = chat_data["current"]
        if cur.get("session_id"):
            chat_data["history"] = [
                h for h in chat_data.get("history", []) if h["session_id"] != cur["session_id"]
            ]
            chat_data["history"].append({
                "session_id": cur["session_id"],
                "runner": cur.get("runner", self._default_runner),
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat_data["history"] = chat_data["history"][-20:]
        cur["model"] = model
        cur["session_id"] = None
        cur["preview"] = ""
        cur["last_usage"] = {}
        cur["started_at"] = datetime.now().isoformat()
        await self._save_async()

    async def set_runner(self, user_id: str, chat_id: str, runner: str, model: str = ""):
        """Set agent runner for a specific chat and start a fresh session."""
        normalized = (runner or "").strip().lower().replace("_", "-")
        if normalized in {"claude-code", "claudecode"}:
            normalized = "claude"
        if normalized not in {"claude", "codex"}:
            raise ValueError("runner must be 'claude' or 'codex'")
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        cur = chat_data["current"]
        if cur.get("session_id"):
            chat_data["history"] = [
                h for h in chat_data.get("history", []) if h["session_id"] != cur["session_id"]
            ]
            chat_data["history"].append({
                "session_id": cur["session_id"],
                "runner": cur.get("runner", self._default_runner),
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat_data["history"] = chat_data["history"][-20:]
        cur["runner"] = normalized
        if model:
            cur["model"] = model
        cur["session_id"] = None
        cur["preview"] = ""
        cur["last_usage"] = {}
        cur["started_at"] = datetime.now().isoformat()
        await self._save_async()

    async def set_cwd(self, user_id: str, chat_id: str, cwd: str, workspace_name: Optional[str] = None):
        """Set working directory for a specific chat"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        chat_data["current"]["cwd"] = cwd
        chat_data["current"]["workspace"] = workspace_name or ""
        await self._save_async()

    async def set_permission_mode(self, user_id: str, chat_id: str, mode: str):
        """Set permission mode for a specific chat"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        chat_data["current"]["permission_mode"] = mode
        await self._save_async()

    async def resume_session(self, user_id: str, chat_id: str, index_or_id: str) -> tuple[Optional[str], str]:
        """按序号（1-based）或 session_id 恢复 session，返回 (session_id, old_title)"""
        if user_id not in self._data:
            return None, ""

        chat_key = self._normalize_chat_key(user_id, chat_id)
        if chat_key not in self._data[user_id]:
            return None, ""

        chat_data = await self._ensure_chat_data(user_id, chat_id)
        history = chat_data.get("history", [])

        try:
            idx = int(index_or_id) - 1
            if 0 <= idx < len(history):
                session_id = history[idx]["session_id"]
            else:
                return None, ""
        except ValueError:
            session_id = index_or_id

        # 归档 outgoing session（如果有且不是同一个）
        cur = chat_data["current"]
        old_id = cur.get("session_id")
        old_title = ""
        if old_id and old_id != session_id:
            chat_data["history"] = [h for h in chat_data["history"] if h["session_id"] != old_id]
            chat_data["history"].append({
                "session_id": old_id,
                "runner": cur.get("runner", self._default_runner),
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat_data["history"] = chat_data["history"][-20:]
            # 获取摘要：优先缓存，否则生成
            summaries = self._data[user_id].get("summaries", {})
            old_title = summaries.get(old_id, "")
            if not old_title:
                asyncio.create_task(self._bg_generate_summary(user_id, old_id))

        # 从 history 中找回原始 preview 和 started_at
        original_preview = ""
        original_started = ""
        for h in chat_data["history"]:
            if h["session_id"] == session_id:
                original_preview = h.get("preview", "")
                original_started = h.get("started_at", "")
                break
        for h in chat_data["history"]:
            if h["session_id"] == session_id:
                runner = h.get("runner")
                if runner and runner != cur.get("runner", self._default_runner):
                    return None, ""
                break
        cur["session_id"] = session_id
        cur["preview"] = original_preview
        cur["started_at"] = original_started or datetime.now().isoformat()
        await self._save_async()
        return session_id, old_title

    async def list_sessions(self, user_id: str, chat_id: str) -> list:
        """List all sessions for a specific chat"""
        if user_id not in self._data:
            return []

        chat_key = self._normalize_chat_key(user_id, chat_id)
        if chat_key not in self._data[user_id]:
            return []

        return list(reversed((await self._ensure_chat_data(user_id, chat_id)).get("history", [])))

    def list_workspaces(self, user_id: str) -> dict[str, str]:
        """List saved workspaces for a user"""
        return dict(sorted(self._user(user_id).get("workspaces", {}).items()))

    async def save_workspace(self, user_id: str, name: str, cwd: str):
        """Save or update a named workspace for a user"""
        user = self._user(user_id)
        user.setdefault("workspaces", {})[name] = cwd
        await self._save_async()

    async def delete_workspace(self, user_id: str, name: str) -> bool:
        """Delete a named workspace and clear active bindings that reference it"""
        user = self._user(user_id)
        workspaces = user.setdefault("workspaces", {})
        if name not in workspaces:
            return False

        del workspaces[name]
        for chat_data in user.values():
            if not isinstance(chat_data, dict) or "current" not in chat_data:
                continue
            if chat_data["current"].get("workspace") == name:
                chat_data["current"]["workspace"] = ""
        await self._save_async()
        return True

    async def bind_workspace(self, user_id: str, chat_id: str, name: str) -> Optional[str]:
        """Bind a saved workspace to the current chat"""
        path = self._user(user_id).get("workspaces", {}).get(name)
        if not path:
            return None
        await self.set_cwd(user_id, chat_id, path, workspace_name=name)
        return path

    async def handover_session(
        self, user_id: str, chat_id: str, session_id: str,
        cwd: str = "", model: str = "",
    ) -> dict:
        """CLI handover: 将指定 session_id 设为当前会话。
        返回 {"old_session_id", "old_summary"} 供通知使用。"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        cur = chat_data["current"]
        old_sid = cur.get("session_id")
        old_summary = ""

        if old_sid and old_sid != session_id:
            # 归档当前会话
            chat_data["history"] = [h for h in chat_data["history"] if h["session_id"] != old_sid]
            chat_data["history"].append({
                "session_id": old_sid,
                "runner": cur.get("runner", self._default_runner),
                "started_at": cur.get("started_at", ""),
                "preview": cur.get("preview", ""),
            })
            chat_data["history"] = chat_data["history"][-20:]
            summaries = self._data[user_id].get("summaries", {})
            old_summary = summaries.get(old_sid, "")
            if not old_summary:
                asyncio.create_task(self._bg_generate_summary(user_id, old_sid))

        cur["session_id"] = session_id
        cur["started_at"] = datetime.now().isoformat()
        if cwd:
            cur["cwd"] = cwd
        if model:
            cur["model"] = model
        # preview 留空，等飞书第一条消息时再更新
        cur["preview"] = ""
        await self._save_async()
        return {"old_session_id": old_sid or "", "old_summary": old_summary}

    def find_primary_user(self) -> Optional[str]:
        """找到主用户（第一个有 private chat 且 open_id 格式的用户）"""
        for uid in self._data:
            if uid.startswith("ou_") and "private" in self._data[uid]:
                return uid
        return None

    async def get_last_seen(self, user_id: str, chat_id: str) -> str:
        """获取话题群最后处理过的 message_id"""
        cur = await self.get_current_raw(user_id, chat_id)
        return cur.get("last_seen_message_id", "") or ""

    async def set_last_seen(self, user_id: str, chat_id: str, message_id: str):
        """更新话题群最后处理过的 message_id"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        chat_data["current"]["last_seen_message_id"] = message_id
        await self._save_async()

    async def get_current_raw(self, user_id: str, chat_id: str = None) -> dict:
        """Get raw current session data for a specific chat"""
        if chat_id is None:
            chat_id = user_id

        return (await self._ensure_chat_data(user_id, chat_id))["current"]
