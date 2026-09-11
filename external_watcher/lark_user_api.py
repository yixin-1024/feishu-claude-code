"""lark-cli 用户身份的薄封装。

外部群里 bot 不是成员（也拉不进去），所有读写都得借机主本人的 user token 走
lark-cli。这里把 `lark-cli --profile X ... --as user --format json` 收口成几个
async 方法，统一超时、JSON 解析与错误形态，免得调用方到处拼命令行。

⚠️ 只有配了 user 授权的 profile 能用（本机目前只有 spx）。没授权时接口会返回
need_user_authorization，这里统一抛 LarkCliError，由调用方决定降级还是重试。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional

from log_util import log

TAG = "ext-web"

# 单次 lark-cli 调用的兜底超时。列消息 / 回消息都是秒级接口，卡住基本是网络问题；
# 与其挂死整个轮询循环，不如超时后让下一轮重试。
# Lark 拒绝"应用以用户身份在外部群里写"的错误码。读没事、写必挂，重试无意义。
EXTERNAL_WRITE_DENIED = 230027

DEFAULT_TIMEOUT = 45.0
DOWNLOAD_TIMEOUT = 120.0

# 一条 Lark 消息的正文上限远大于这个数，但超长正文在手机端体验极差，
# 且 --markdown 是走 argv 传的（macOS ARG_MAX ~1MB）。按这个粒度切片发送。
REPLY_CHUNK_CHARS = 3500

# post 消息被 lark-cli 渲染成 markdown，图片是 ![Image](img_v3_xxx) 形态。
_IMG_RE = re.compile(r"!\[[^\]]*\]\((img_[A-Za-z0-9_\-]+)\)")
# 附件（file_xxx）在 post 里渲染成普通链接。
_FILE_RE = re.compile(r"\[([^\]]*)\]\((file_[A-Za-z0-9_\-]+)\)")


class LarkCliError(RuntimeError):
    """lark-cli 调用失败（非 0 退出、ok=false、或输出不是 JSON）。

    code: Lark 的业务错误码（拿得到才有）。上层靠它区分"这次网络抖了"和
    "这个操作在这个群里永远不可能成功"（如 230027 外部群禁写）。
    """

    def __init__(self, message: str, code: int = 0, subtype: str = ""):
        super().__init__(message)
        self.code = code
        self.subtype = subtype


def extract_image_keys(content: str) -> list[str]:
    """从 lark-cli 渲染出的 post markdown 里抠出图片 key（保序去重）。"""
    seen: set[str] = set()
    out: list[str] = []
    for key in _IMG_RE.findall(content or ""):
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def extract_file_keys(content: str) -> list[tuple[str, str]]:
    """从 post markdown 里抠出附件 (显示名, file_key)，保序去重。"""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name, key in _FILE_RE.findall(content or ""):
        if key not in seen:
            seen.add(key)
            out.append((name or key, key))
    return out


def strip_resource_markup(content: str) -> str:
    """把图片 / 附件的 markdown 占位从正文里去掉，只留文字。"""
    text = _IMG_RE.sub("", content or "")
    text = _FILE_RE.sub("", text)
    return text.strip()


def unwrap_card(content: str) -> str:
    """lark-cli 把卡片正文渲染成 `<card>…</card>`，剥掉外壳（与主 bot 一致）。"""
    text = (content or "").strip()
    if text.startswith("<card>"):
        text = text[len("<card>"):]
        if text.rstrip().endswith("</card>"):
            text = text.rstrip()[: -len("</card>")]
    return text.strip()


def chunk_text(text: str, size: int = REPLY_CHUNK_CHARS) -> list[str]:
    """按行边界把长文本切成不超过 size 的片段（尽量不把一行劈开）。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for line in text.split("\n"):
        # 单行本身就超长：先把已攒的吐出去，再硬切这一行
        if len(line) > size:
            if buf:
                chunks.append("\n".join(buf))
                buf, buf_len = [], 0
            for i in range(0, len(line), size):
                chunks.append(line[i:i + size])
            continue
        if buf_len + len(line) + 1 > size and buf:
            chunks.append("\n".join(buf))
            buf, buf_len = [], 0
        buf.append(line)
        buf_len += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return [c for c in chunks if c.strip()]


class LarkUserApi:
    """按 profile 绑定的 lark-cli user 身份客户端。"""

    def __init__(self, profile: str, binary: str = "lark-cli"):
        self.profile = profile
        self.binary = binary

    async def _run(self, args: list[str], timeout: float = DEFAULT_TIMEOUT) -> dict:
        cmd = [self.binary, "--profile", self.profile] + args
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise LarkCliError(f"找不到 {self.binary}: {e}") from e

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise LarkCliError(f"lark-cli 超时（{timeout}s）: {' '.join(args[:3])}")

        out = (stdout or b"").decode("utf-8", errors="ignore").strip()
        err = (stderr or b"").decode("utf-8", errors="ignore").strip()

        # ⚠️ 失败时 lark-cli 把结构化错误 JSON 打到 **stderr**，stdout 是空的
        # （实测 rc=3 时 stdout 0 字节 / stderr 817 字节）。只看 stdout 会把
        # 「230027 user_unauthorized」这种明确原因糊成一句「输出不是 JSON」，
        # 排查时完全看不出真因。所以两路都试。
        data: Optional[dict] = None
        for raw in (out, err):
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            if isinstance(parsed, dict):
                data = parsed
                break

        if data is None:
            raise LarkCliError(
                f"lark-cli 输出不是 JSON (rc={proc.returncode}): {(out or err)[:300]}"
            )
        if not data.get("ok", False):
            detail = data.get("error") or data.get("_notice") or data
            code, subtype = 0, ""
            if isinstance(detail, dict):
                try:
                    code = int(detail.get("code") or 0)
                except (TypeError, ValueError):
                    code = 0
                subtype = str(detail.get("subtype") or "")
            raise LarkCliError(
                f"lark-cli 返回失败: {json.dumps(detail, ensure_ascii=False)[:300]}",
                code=code, subtype=subtype,
            )
        return data.get("data") or {}

    # ── 身份 ─────────────────────────────────────────────────
    async def whoami(self) -> tuple[str, str]:
        """返回 (open_id, 姓名)。`auth status` 不吃 --format，输出本身就是 JSON。"""
        proc = await asyncio.create_subprocess_exec(
            self.binary, "--profile", self.profile, "auth", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=DEFAULT_TIMEOUT)
        try:
            info = json.loads((stdout or b"").decode("utf-8", errors="ignore"))
        except Exception as e:
            raise LarkCliError(f"auth status 解析失败: {e}") from e
        user = ((info.get("identities") or {}).get("user") or {})
        if not user.get("available"):
            raise LarkCliError(
                f"profile {self.profile} 没有可用的 user 身份: {user.get('message', '?')}"
            )
        return str(user.get("openId") or ""), str(user.get("userName") or "")

    # ── 读 ───────────────────────────────────────────────────
    async def list_chat_messages(
        self, chat_id: str, page_size: int = 20, order: str = "desc",
    ) -> list[dict]:
        """列群消息。话题群里返回的是**话题根消息**，回复挂在 thread_replies 里。"""
        data = await self._run([
            "im", "+chat-messages-list",
            "--chat-id", chat_id,
            "--as", "user",
            "--order", order,
            "--page-size", str(page_size),
            "--no-reactions",
            "--format", "json",
        ])
        return list(data.get("messages") or [])

    async def list_thread_messages(
        self, thread_id: str, page_size: int = 50, order: str = "asc",
    ) -> list[dict]:
        """列一个话题内的全部消息（含根消息），按时间正序。"""
        data = await self._run([
            "im", "+threads-messages-list",
            "--thread", thread_id,
            "--as", "user",
            "--order", order,
            "--page-size", str(page_size),
            "--no-reactions",
            "--format", "json",
        ])
        return list(data.get("messages") or [])

    async def download_resource(
        self, message_id: str, file_key: str, kind: str, out_path: str,
    ) -> str:
        """下载图片 / 文件到 out_path（不含扩展名也行，lark-cli 会补）。返回真实落盘路径。"""
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        data = await self._run([
            "im", "+messages-resources-download",
            "--message-id", message_id,
            "--file-key", file_key,
            "--type", "image" if kind == "image" else "file",
            "--as", "user",
            "--output", out_path,
            "--format", "json",
        ], timeout=DOWNLOAD_TIMEOUT)
        saved = str(data.get("saved_path") or "")
        if not saved:
            raise LarkCliError(f"下载 {file_key[:12]}… 没返回 saved_path")
        return saved

    # ── 写 ───────────────────────────────────────────────────
    async def reply_markdown(
        self, message_id: str, text: str, in_thread: bool = True,
    ) -> list[str]:
        """以 user 身份回复。超长自动切片顺序发送，返回发出去的 message_id 列表。"""
        chunks = chunk_text(text)
        if not chunks:
            return []

        sent: list[str] = []
        anchor = message_id
        for idx, chunk in enumerate(chunks):
            args = [
                "im", "+messages-reply",
                "--as", "user",
                "--message-id", anchor,
                "--markdown", chunk,
            ]
            if in_thread:
                args.append("--reply-in-thread")
            args += ["--format", "json"]
            data = await self._run(args)
            mid = str(data.get("message_id") or "")
            if mid:
                sent.append(mid)
            # 后续分片继续挂在原锚点下，保持在同一话题流里；顺序由串行发送保证。
            if idx < len(chunks) - 1:
                await asyncio.sleep(0.3)
        return sent
