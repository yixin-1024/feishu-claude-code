import asyncio
import copy
import json
import os
import subprocess
import ssl
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

from bot_config import (
    SESSIONS_DIR, DEFAULT_MODEL, DEFAULT_CWD, PERMISSION_MODE, DEFAULT_RUNNER,
    THREAD_SHARED_SESSION,
)

# 话题群共享 session 的哨兵用户桶：thread 复合 chat_id（"oc_xxx:omt_yyy"）
# 在共享模式下统一记到这个桶下，让同一话题里所有人共享同一个 session。
SHARED_THREAD_UID = "__thread__"

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

# 无缝衔接：claude_session_mirror 的守护进程把"被镜像的本机终端 session"对应的
# Lark thread 落成 ~/.claude/session_mirror/threads/<thread_id>.json。get_current
# 读它，把"在 Lark 这个 thread 里回复"绑到终端 session_id + cwd（--resume）。
_MIRROR_THREADS_DIR = os.path.expanduser("~/.claude/session_mirror/threads")


_MIRROR_ACTIVE_DIR = os.path.expanduser("~/.claude/session_mirror/active")


def _lookup_mirror_thread(thread_id: str) -> Optional[dict]:
    """查 thread_id 是否对应一个被镜像的本机终端 session。返回 {session_id,cwd,...}
    或 None。任何异常都吞掉，回落到原有行为。"""
    if not thread_id:
        return None
    try:
        with open(os.path.join(_MIRROR_THREADS_DIR, f"{thread_id}.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("session_id") else None
    except (OSError, json.JSONDecodeError):
        return None


def _release_mirror_session(session_id: str, thread_id: str) -> None:
    """交接完成：让镜像守护进程停止跟踪这个 session（删 active marker），并清掉
    一次性的 thread link。否则 bot resume 后自己回复，镜像会把同一 session 的新
    事件又推一遍，thread 里出现重复卡片。全 best-effort。"""
    for path in (
        os.path.join(_MIRROR_ACTIVE_DIR, f"{session_id}.json"),
        os.path.join(_MIRROR_THREADS_DIR, f"{thread_id}.json"),
    ):
        try:
            os.remove(path)
        except OSError:
            pass


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


def _token_if_valid(creds: dict) -> Optional[str]:
    """从 credentials dict 取未过期的 accessToken；过期或缺失返回 None"""
    oauth = creds.get("claudeAiOauth", {})
    token = oauth.get("accessToken")
    if not token:
        return None
    exp = oauth.get("expiresAt")
    # 留 60s 余量；无 expiresAt 时保守认为可用
    if exp and exp < int(datetime.now().timestamp() * 1000) + 60_000:
        return None
    return token


def _get_api_token() -> Optional[str]:
    """获取 Claude API token：优先用未过期的，文件过期则回落到 keychain（CLI 维护的新鲜副本）"""
    # 1) credentials 文件（可能是过期的陈旧残留，需校验 expiresAt）
    try:
        creds_path = os.path.expanduser("~/.claude/.credentials.json")
        if os.path.isfile(creds_path):
            with open(creds_path) as f:
                token = _token_if_valid(json.load(f))
            if token:
                return token
    except Exception:
        pass
    # 2) keychain（文件缺失或 token 过期时回落）
    # ⚠️ 必须走 account_switcher._read_keychain_blob：它按 `-a <用户名>` 精确取条目。
    # 新版 CLI 把凭证写在 acct=<macOS 用户名> 上，不带 -a 的 `security` 会随机命中
    # 同 service 名的历史死条目 → 拿到过期 token → 这里返回 None → 后台摘要 7 天 0 成功
    # 且不报错（/usage 的同款坑在 89b54bb 修过，这里当时漏了）。
    try:
        from account_switcher import _read_keychain_blob, ensure_keychain_intact
        ensure_keychain_intact()  # keychain 被外部进程写丢时自愈
        blob = _read_keychain_blob()
        if not blob:
            return None
        return _token_if_valid(json.loads(blob))
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
        effort: Optional[str] = None,
    ):
        self.session_id = session_id
        self.model = model
        self.cwd = cwd
        self.permission_mode = permission_mode
        self.workspace = workspace
        self.runner = runner
        self.effort = effort


class SessionStore:
    def __init__(
        self,
        profile: str = "default",
        default_cwd: Optional[str] = None,
        chat_default_cwd: Optional[dict[str, str]] = None,
        default_runner: Optional[str] = None,
        default_model: Optional[str] = None,
        shared_thread_sessions: Optional[bool] = None,
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
        self._shared_threads = (
            THREAD_SHARED_SESSION if shared_thread_sessions is None
            else shared_thread_sessions
        )
        self._default_cwd = default_cwd or DEFAULT_CWD
        self._default_runner = (default_runner or DEFAULT_RUNNER or "claude").strip().lower()
        if self._default_runner not in {"claude", "codex", "opencode", "mimo", "grok", "maka"}:
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

    def _effective_uid(self, user_id: str, chat_id: Optional[str]) -> str:
        """话题群共享模式下，thread 复合 chat_id（"oc_xxx:omt_yyy"）统一归到
        哨兵桶 SHARED_THREAD_UID —— 同一话题里所有人共享同一个 session。
        私聊（chat_id == user_id）和非话题群聊（无 ":"）保持按发送人分桶。
        幂等：传入哨兵 uid 再映射一次仍是哨兵。"""
        if self._shared_threads and chat_id and ":" in chat_id and chat_id != user_id:
            return SHARED_THREAD_UID
        return user_id

    def _adopt_thread_data(self, chat_key: str) -> Optional[dict]:
        """共享模式下首次访问某话题时，从旧的按用户分桶数据里收养最近的会话，
        让开启共享前已有的话题能无缝续上原 session。取 started_at 最新的一份，
        连带把相关摘要复制到哨兵桶（/resume 列表显示标题用）。"""
        best_uid = None
        best_data = None
        best_ts = ""
        for uid, user_data in self._data.items():
            if uid == SHARED_THREAD_UID or not isinstance(user_data, dict):
                continue
            chat_data = user_data.get(chat_key)
            if not isinstance(chat_data, dict) or not isinstance(chat_data.get("current"), dict):
                continue
            ts = str(chat_data["current"].get("started_at", "") or "")
            if best_data is None or ts > best_ts:
                best_uid, best_data, best_ts = uid, chat_data, ts
        if best_data is None:
            return None
        adopted = copy.deepcopy(best_data)
        src_summaries = self._data.get(best_uid, {}).get("summaries", {})
        if src_summaries:
            sids = {adopted.get("current", {}).get("session_id")}
            sids.update(h.get("session_id") for h in adopted.get("history", []))
            picked = {s: src_summaries[s] for s in sids if s and s in src_summaries}
            if picked:
                self._data.setdefault(SHARED_THREAD_UID, {}).setdefault(
                    "summaries", {}
                ).update(picked)
        return adopted

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
            # model 不再钉死在 session 里。实际用的模型 = model_override（用户用
            # /model 显式选的，缺省 None）or profile 默认（运行时实时取）。这样改
            # config 的默认模型 + 重启，新旧 session 都跟随，不用动代码。
            "model_override": None,
            # /effort 显式选择的话题级覆盖。None 表示继续跟随 profile/CLI 默认；
            # 默认值不落盘，避免配置变化后旧会话被钉死。
            "effort_override": None,
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
            # runner 跟 profile 配置不一致（多为改了配置）：回到 profile 默认 runner，
            # 并清掉为旧 runner 选的模型 override，让模型回落到 profile 默认。
            current["runner"] = self._default_runner
            current["model_override"] = None
            current["effort_override"] = None
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
        user_id = self._effective_uid(user_id, chat_id)
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
                # 共享话题桶首次访问：先尝试收养旧的按用户分桶数据（无缝续 session）
                adopted = (
                    self._adopt_thread_data(chat_key)
                    if user_id == SHARED_THREAD_UID else None
                )
                user[chat_key] = adopted or {
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

    def has_chat_record(self, user_id: str, chat_id: str) -> bool:
        """只读检查：这个 chat（含话题复合 key "oc_xxx:omt_yyy"）是否已有会话记录。
        不创建任何数据。扫所有桶——共享话题模式的哨兵桶和旧的按用户分桶都算，
        与 _adopt_thread_data 的收养语义保持一致。"""
        uid = self._effective_uid(user_id, chat_id)
        chat_key = self._normalize_chat_key(uid, chat_id)
        for user_data in self._data.values():
            if not isinstance(user_data, dict):
                continue
            chat_data = user_data.get(chat_key)
            if not isinstance(chat_data, dict):
                continue
            cur = chat_data.get("current") or {}
            if cur.get("session_id") or chat_data.get("history"):
                return True
        return False

    def get_summary(self, user_id: str, session_id: str) -> str:
        """获取缓存的摘要。共享话题模式下回落查哨兵桶（调用方常拿真实
        sender uid 来查，而共享话题的摘要存在 SHARED_THREAD_UID 下）。"""
        summary = self._user(user_id).get("summaries", {}).get(session_id, "")
        if not summary and self._shared_threads and user_id != SHARED_THREAD_UID:
            summary = (
                self._data.get(SHARED_THREAD_UID, {})
                .get("summaries", {})
                .get(session_id, "")
            )
        return summary

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

    @property
    def default_model(self) -> str:
        """profile 当前默认模型（来自 config，重启即生效的唯一真相源）"""
        return self._default_model

    async def get_current(self, user_id: str, chat_id: str) -> Session:
        """Get current session config for a specific chat"""
        cur = await self.get_current_raw(user_id, chat_id)
        # 无缝衔接：thread 还没绑定 session 时，若它对应一个被镜像的本机终端会话，
        # 就把它绑到那个 session_id + cwd——于是在 Lark 这个 thread 里回复会直接
        # --resume 终端会话。只绑一次（绑完落盘 session_id+cwd，之后正常续）。
        if not cur.get("session_id") and chat_id and ":" in chat_id:
            try:
                thread_id = chat_id.split(":", 1)[1]
                bound = _lookup_mirror_thread(thread_id)
                if bound:
                    cur["session_id"] = bound["session_id"]
                    if bound.get("cwd"):
                        cur["cwd"] = bound["cwd"]
                    if not cur.get("preview"):
                        cur["preview"] = (bound.get("preview") or "")[:40]
                    await self._save_async()
                    # 交接给 cc-lark 后，停掉镜像对该 session 的跟踪，避免重复卡片
                    _release_mirror_session(bound["session_id"], thread_id)
            except Exception:
                pass
        return Session(
            session_id=cur.get("session_id"),
            # 没有显式 override 就用 profile 默认（实时取，配置变了即生效）
            model=cur.get("model_override") or self._default_model,
            cwd=cur.get("cwd", self._default_cwd),
            permission_mode=cur.get("permission_mode", PERMISSION_MODE),
            workspace=cur.get("workspace", ""),
            runner=cur.get("runner", self._default_runner),
            effort=cur.get("effort_override"),
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
        user_id = self._effective_uid(user_id, chat_id)
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
        user_id = self._effective_uid(user_id, chat_id)
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
            # 沿用显式 override（若有）；没有就继续跟随 profile 默认
            "model_override": cur.get("model_override"),
            # effort 是话题配置，/new 只换 CLI session，继续沿用当前覆盖。
            "effort_override": cur.get("effort_override"),
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
        user_id = self._effective_uid(user_id, chat_id)
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
        # model 为空 → 清除 override，回落 profile 默认
        cur["model_override"] = model or None
        cur["session_id"] = None
        cur["preview"] = ""
        cur["last_usage"] = {}
        cur["started_at"] = datetime.now().isoformat()
        await self._save_async()

    async def set_model_override(self, user_id: str, chat_id: str, model: str):
        """仅改 model_override、不动 session（safeguards 自动降级续跑用；
        用户手动 /model 换模型仍走 set_model 开新会话）。"""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        chat_data["current"]["model_override"] = model or None
        await self._save_async()

    async def set_effort(self, user_id: str, chat_id: str, effort: str):
        """Set a per-conversation reasoning-effort override without changing session."""
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        value = (effort or "").strip().lower()
        chat_data["current"]["effort_override"] = value or None
        await self._save_async()

    async def set_runner(self, user_id: str, chat_id: str, runner: str, model: str = ""):
        """Set agent runner for a specific chat and start a fresh session."""
        normalized = (runner or "").strip().lower().replace("_", "-")
        if normalized in {"claude-code", "claudecode"}:
            normalized = "claude"
        if normalized in {"mimo-code", "mimocode"}:
            normalized = "mimo"
        if normalized not in {"claude", "codex", "opencode", "mimo", "grok", "maka"}:
            raise ValueError(
                "runner must be 'claude', 'codex', 'opencode', 'mimo', 'grok' or 'maka'"
            )
        chat_data = await self._ensure_chat_data(user_id, chat_id)
        cur = chat_data["current"]
        runner_changed = cur.get("runner", self._default_runner) != normalized
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
        if runner_changed:
            # 不同 runner 支持的 effort 档位不同（如 Codex 的 ultra），不可沿用。
            cur["effort_override"] = None
        # 切 runner 时带的模型作为显式 override（不同 runner 模型不通用）
        if model:
            cur["model_override"] = model
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
        user_id = self._effective_uid(user_id, chat_id)
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
        user_id = self._effective_uid(user_id, chat_id)
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
        user_id = self._effective_uid(user_id, chat_id)
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
            cur["model_override"] = model
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
