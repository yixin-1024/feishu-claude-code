"""
飞书 API 异步封装。
流式方案：发送内联卡片消息 → 用 patch 逐步更新内容（比 cardkit 流式卡片更简单可靠）。
"""

import asyncio
import json
import os
import tempfile
import time
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1.model import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ListMessageRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)


def _sanitize_filename(name: str) -> str:
    """把不安全的字符替换成下划线，保留 CJK/字母数字/常见标点。截断到 100 字符。"""
    safe_chars = "-_.() "
    cleaned = "".join(c if c.isalnum() or c in safe_chars else "_" for c in name)
    cleaned = cleaned.strip()
    return cleaned[:100] or "file"


def _card_json(content: str, loading: bool = False) -> str:
    """
    生成卡片 JSON 字符串（Card JSON 2.0）

    飞书卡片 markdown 元素有长度限制（约 3000 字符），
    超过限制时自动分段为多个 markdown 元素。
    """
    elements = []
    if loading:
        elements.append({"tag": "markdown", "content": "⏳ 思考中..."})
    else:
        # 飞书 markdown 元素长度限制约 3000 字符，保守使用 2800
        MAX_CHUNK_SIZE = 2800

        if len(content) <= MAX_CHUNK_SIZE:
            # 内容不长，直接发送
            elements.append({"tag": "markdown", "content": content})
        else:
            # 内容过长，分段发送
            # 尝试按段落分割，避免在句子中间截断
            chunks = []
            current_chunk = ""

            # 按换行符分割
            lines = content.split('\n')

            for line in lines:
                # 如果单行就超过限制，强制截断
                if len(line) > MAX_CHUNK_SIZE:
                    # 先保存当前块
                    if current_chunk:
                        chunks.append(current_chunk)
                        current_chunk = ""

                    # 强制分割长行
                    for i in range(0, len(line), MAX_CHUNK_SIZE):
                        chunks.append(line[i:i + MAX_CHUNK_SIZE])
                    continue

                # 检查加上这行是否会超过限制
                if len(current_chunk) + len(line) + 1 > MAX_CHUNK_SIZE:
                    # 超过限制，保存当前块，开始新块
                    if current_chunk:
                        chunks.append(current_chunk)
                    current_chunk = line
                else:
                    # 未超过限制，追加到当前块
                    if current_chunk:
                        current_chunk += '\n' + line
                    else:
                        current_chunk = line

            # 保存最后一块
            if current_chunk:
                chunks.append(current_chunk)

            # 为每个块创建 markdown 元素
            for i, chunk in enumerate(chunks):
                # 第一块不加前缀，后续块加分段标记
                if i > 0:
                    chunk = f"**（续 {i}）**\n\n{chunk}"
                elements.append({"tag": "markdown", "content": chunk})

    return json.dumps({
        "schema": "2.0",
        "body": {"elements": elements},
    }, ensure_ascii=False)


class FeishuClient:
    def __init__(self, client: lark.Client, app_id: str = "", app_secret: str = "",
                 domain: str = "https://open.feishu.cn"):
        self.client = client
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain.rstrip("/")
        self._bot_open_id: Optional[str] = None
        # interactive 卡片的最终文本快照（message_id → markdown 内容）。
        # 飞书 im.v1.message.list 拿到的 interactive content 是初始快照（loading 状态），
        # update_card 流式 patch 上去的内容拿不到；thread_context 解析历史 bot 卡片时
        # 会 fallback 到这个 cache。仅内存，重启就丢；只对"刚发完卡片就要审"场景有效。
        self._card_text_cache: dict[str, str] = {}
        self._CARD_CACHE_MAX = 500  # 简单上限防止无界增长

    def _remember_card_text(self, message_id: str, content: str) -> None:
        """记下一张卡片的当前文本，给 thread_context 解析 bot 历史卡片用。"""
        if not message_id or not content:
            return
        # 朴素 FIFO 上限：超出就丢最早 1/4，避免无界内存增长
        if len(self._card_text_cache) >= self._CARD_CACHE_MAX:
            drop = max(1, self._CARD_CACHE_MAX // 4)
            for k in list(self._card_text_cache.keys())[:drop]:
                self._card_text_cache.pop(k, None)
        self._card_text_cache[message_id] = content

    def get_card_text(self, message_id: str) -> str:
        """查这条 interactive 消息 update_card 之后的最终文本（没有则空串）。"""
        return self._card_text_cache.get(message_id, "")

    async def get_bot_open_id(self) -> Optional[str]:
        """查询机器人自己的 open_id（首次调用时请求 /bot/v3/info 并缓存）。"""
        if self._bot_open_id:
            return self._bot_open_id

        def _fetch() -> Optional[str]:
            import ssl
            import urllib.request
            ctx = ssl.create_default_context()
            token_body = json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode()
            token_req = urllib.request.Request(
                f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal",
                data=token_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(token_req, context=ctx, timeout=10) as r:
                token = json.loads(r.read())["tenant_access_token"]
            info_req = urllib.request.Request(
                f"{self._domain}/open-apis/bot/v3/info",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(info_req, context=ctx, timeout=10) as r:
                data = json.loads(r.read())
            return (data.get("bot") or {}).get("open_id")

        try:
            self._bot_open_id = await asyncio.to_thread(_fetch)
        except Exception as e:
            print(f"[warn] 获取 bot open_id 失败: {e}", flush=True)
            return None
        return self._bot_open_id

    async def _retry_with_backoff(self, coro_func, max_retries: int = 3, initial_delay: float = 0.5):
        """
        执行异步操作，失败时指数退避重试。

        Args:
            coro_func: 返回 coroutine 的可调用对象
            max_retries: 最多重试次数（不包括首次尝试）
            initial_delay: 初始延迟秒数

        Returns:
            操作结果

        Raises:
            最后一次尝试的异常
        """
        delay = initial_delay
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                return await coro_func()
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    print(f"[retry] 第 {attempt + 1} 次失败，{delay:.1f}s 后重试: {e}", flush=True)
                    await asyncio.sleep(delay)
                    delay *= 2  # 指数退避
                else:
                    print(f"[retry] 已达最大重试次数 {max_retries + 1}，放弃", flush=True)

        raise last_error

    # ── 发送消息 ──────────────────────────────────────────────

    async def send_card_to_user(self, open_id: str, content: str = "", loading: bool = True) -> str:
        """向用户发送卡片消息，返回 message_id（带重试）"""
        async def _send():
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(open_id)
                    .msg_type("interactive")
                    .content(_card_json(content, loading=loading))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.acreate(req)
            if not resp.success():
                raise RuntimeError(f"发送卡片消息失败: {resp.code} {resp.msg}")
            return resp.data.message_id

        mid = await self._retry_with_backoff(_send, max_retries=3)
        if not loading:
            self._remember_card_text(mid, content)
        return mid

    async def reply_card(self, message_id: str, content: str = "", loading: bool = True) -> str:
        """回复用户消息（卡片形式），触发通知。返回回复消息的 message_id（带重试）"""
        async def _reply():
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(_card_json(content, loading=loading))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.areply(req)
            if not resp.success():
                raise RuntimeError(f"回复卡片消息失败: {resp.code} {resp.msg}")
            return resp.data.message_id

        mid = await self._retry_with_backoff(_reply, max_retries=3)
        if not loading:
            self._remember_card_text(mid, content)
        return mid

    async def update_card(self, message_id: str, content: str):
        """用 patch 更新已发送的卡片内容（带重试）"""
        async def _update():
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(_card_json(content, loading=False))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.apatch(req)
            if not resp.success():
                raise RuntimeError(f"patch 卡片失败: {resp.code} {resp.msg}")

        await self._retry_with_backoff(_update, max_retries=3)
        self._remember_card_text(message_id, content)

    async def download_image(self, message_id: str, image_key: str) -> str:
        """下载飞书图片到临时文件，返回本地路径（不阻塞事件循环）"""
        return await asyncio.to_thread(
            self._download_resource_sync, message_id, image_key, "image", ""
        )

    async def download_file(
        self, message_id: str, file_key: str, msg_type: str = "file", file_name: str = "",
    ) -> str:
        """下载飞书文件/音频/视频到临时文件，返回本地路径。

        msg_type: 原始消息类型（file/audio/media/sticker/image），决定 API 的 type 参数
        file_name: 原始文件名（用于保留后缀和可读性）
        """
        return await asyncio.to_thread(
            self._download_resource_sync, message_id, file_key, msg_type, file_name
        )

    def _download_resource_sync(
        self, message_id: str, resource_key: str, msg_type: str, file_name: str,
    ) -> str:
        """同步下载逻辑，在线程池中执行。统一处理 image/file/audio/media。"""
        import ssl
        import urllib.request
        import uuid

        # Feishu /resources 接口 type 只接受 image 或 file
        api_type = "image" if msg_type in ("image", "sticker") else "file"

        ctx = ssl.create_default_context()

        token_body = json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode()
        token_req = urllib.request.Request(
            f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal",
            data=token_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, context=ctx, timeout=10) as r:
            token = json.loads(r.read())["tenant_access_token"]

        url = f"{self._domain}/open-apis/im/v1/messages/{message_id}/resources/{resource_key}?type={api_type}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

        tmp_dir = tempfile.gettempdir()

        # 组装本地路径：优先用原始文件名保留可读性和后缀
        if file_name:
            safe_name = _sanitize_filename(file_name)
            tmp_path = os.path.join(tmp_dir, f"feishu-{uuid.uuid4().hex[:6]}-{safe_name}")
        else:
            # 无 file_name 时用 msg_type 作前缀；图片后缀根据 Content-Type 确定
            default_ext = ".jpg" if api_type == "image" else ".bin"
            tmp_path = os.path.join(
                tmp_dir, f"feishu-{msg_type}-{uuid.uuid4().hex[:8]}{default_ext}"
            )

        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            if api_type == "image" and not file_name:
                ct = r.headers.get("Content-Type", "")
                if "png" in ct:
                    tmp_path = tmp_path[:-4] + ".png"
                elif "gif" in ct:
                    tmp_path = tmp_path[:-4] + ".gif"
                elif "webp" in ct:
                    tmp_path = tmp_path[:-4] + ".webp"
            with open(tmp_path, "wb") as f:
                f.write(r.read())

        return tmp_path

    async def speech_to_text(self, audio_path: str, file_id: str = "") -> str:
        """调飞书语音识别 API 把语音文件（≤60s）转成文字。

        file_recognize 只接受 16kHz s16le 单声道裸 pcm，所以先用 ffmpeg 把
        Lark 语音消息的 opus 解码成 pcm 再上传。需要 app 具备
        speech_to_text:speech 权限。file_id 要求恰好 16 位字母数字下划线。
        """
        return await asyncio.to_thread(self._speech_to_text_sync, audio_path, file_id)

    @staticmethod
    def _find_ffmpeg() -> str:
        import shutil
        # launchd 环境的 PATH 往往没有 homebrew，按常见安装位置兜底
        found = shutil.which("ffmpeg")
        if found:
            return found
        for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
            if os.path.exists(cand):
                return cand
        raise RuntimeError("未找到 ffmpeg（语音转写需要它把 opus 解码成 pcm）")

    def _speech_to_text_sync(self, audio_path: str, file_id: str) -> str:
        import base64
        import re
        import ssl
        import subprocess
        import urllib.request
        import uuid

        proc = subprocess.run(
            [self._find_ffmpeg(), "-y", "-loglevel", "error",
             "-i", audio_path, "-f", "s16le", "-ar", "16000", "-ac", "1", "-"],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(f"ffmpeg 解码失败: {proc.stderr.decode(errors='replace')[:200]}")
        speech_b64 = base64.b64encode(proc.stdout).decode()

        ctx = ssl.create_default_context()

        token_body = json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode()
        token_req = urllib.request.Request(
            f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal",
            data=token_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, context=ctx, timeout=10) as r:
            token = json.loads(r.read())["tenant_access_token"]

        fid = re.sub(r"[^A-Za-z0-9_]", "", file_id or "")
        if len(fid) < 16:
            fid = (fid + uuid.uuid4().hex)[:16]
        else:
            fid = fid[:16]

        body = json.dumps({
            "speech": {"speech": speech_b64},
            "config": {
                "file_id": fid,
                "format": "pcm",
                "engine_type": "16k_auto",
            },
        }).encode()
        req = urllib.request.Request(
            f"{self._domain}/open-apis/speech_to_text/v1/speech/file_recognize",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            data = json.loads(r.read())

        if data.get("code") != 0:
            raise RuntimeError(f"语音识别失败 code={data.get('code')} msg={data.get('msg')}")
        return ((data.get("data") or {}).get("recognition_text") or "").strip()

    async def get_message_thread_id(self, message_id: str) -> str:
        """通过 GET /im/v1/messages/{id} 拿这条消息所在的 thread_id（话题群里专用）。
        用于 /spawn 兜底：dispatcher Claude 偶尔会把 anchor 的 message_id 当 thread_id 传过来，
        我们识别出 om_xxx 形式后调本方法换成真正的 omt_xxx。"""
        def _http() -> str:
            import ssl
            import urllib.request
            ctx = ssl.create_default_context()
            token_body = json.dumps({
                "app_id": self._app_id, "app_secret": self._app_secret
            }).encode()
            token_req = urllib.request.Request(
                f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal",
                data=token_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(token_req, context=ctx, timeout=30) as r:
                token = json.loads(r.read())["tenant_access_token"]
            req = urllib.request.Request(
                f"{self._domain}/open-apis/im/v1/messages/{message_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
                data = json.loads(r.read())
            items = (data.get("data") or {}).get("items") or []
            if items:
                return items[0].get("thread_id", "") or ""
            return ""

        return await asyncio.to_thread(_http)

    async def list_thread_messages(self, thread_id: str, limit: int = 200) -> list:
        """列出话题里的消息，按创建时间升序。返回 Message 对象列表。"""
        messages: list = []
        page_token = ""
        while len(messages) < limit:
            builder = (
                ListMessageRequest.builder()
                .container_id_type("thread")
                .container_id(thread_id)
                .sort_type("ByCreateTimeAsc")
                .page_size(min(50, limit - len(messages)))
            )
            if page_token:
                builder = builder.page_token(page_token)
            req = builder.build()
            resp = await self.client.im.v1.message.alist(req)
            if not resp.success():
                raise RuntimeError(f"list thread messages 失败: {resp.code} {resp.msg}")
            items = (resp.data.items or []) if resp.data else []
            messages.extend(items)
            has_more = bool(resp.data and resp.data.has_more)
            page_token = (resp.data.page_token or "") if resp.data else ""
            if not has_more or not page_token:
                break
        return messages

    async def update_card_with_buttons(self, message_id: str, content: str, buttons: list[dict],
                                      flow: bool = False):
        """更新卡片内容并附加操作按钮。flow=True 时横排自动换行，False 时竖排。"""
        base = json.loads(_card_json(content))
        btn_elements = []
        for i, btn in enumerate(buttons):
            btn_elements.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn["text"]},
                "type": "default",
                "size": "small",
                "name": f"btn_{i}",
                "value": btn["value"],
                "behaviors": [{"type": "callback", "value": btn["value"]}],
            })
        if flow and btn_elements:
            # 横排: column_set + flex_mode flow
            columns = [{"tag": "column", "width": "auto", "elements": [b]} for b in btn_elements]
            base["body"]["elements"].append({"tag": "column_set", "flex_mode": "flow", "columns": columns})
        else:
            # 竖排: 每个按钮独占一行
            base["body"]["elements"].extend(btn_elements)
        card_content = json.dumps(base, ensure_ascii=False)

        async def _update():
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(card_content)
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.apatch(req)
            if not resp.success():
                raise RuntimeError(f"patch 卡片失败: {resp.code} {resp.msg}")

        await self._retry_with_backoff(_update, max_retries=3)
        self._remember_card_text(message_id, content)

    async def update_card_elements(self, message_id: str, elements: list[dict]):
        """用自定义 elements 列表更新卡片（支持 markdown + button 混排）"""
        card_content = json.dumps({
            "schema": "2.0",
            "body": {"elements": elements},
        }, ensure_ascii=False)

        async def _update():
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(card_content)
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.apatch(req)
            if not resp.success():
                raise RuntimeError(f"patch 卡片失败: {resp.code} {resp.msg}")

        await self._retry_with_backoff(_update, max_retries=3)

        # 从 elements 抽出 markdown / div 内容拼成文本快照（按钮文字忽略）
        chunks = []
        for el in elements:
            if not isinstance(el, dict):
                continue
            tag = el.get("tag", "")
            if tag == "markdown":
                c = (el.get("content") or "").strip()
                if c:
                    chunks.append(c)
            elif tag == "div":
                c = ((el.get("text") or {}).get("content") or "").strip()
                if c:
                    chunks.append(c)
        if chunks:
            self._remember_card_text(message_id, "\n".join(chunks))

    async def reply_text(self, message_id: str, text: str) -> str:
        """回复纯文本消息（触发通知）"""
        async def _reply():
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.areply(req)
            if not resp.success():
                raise RuntimeError(f"回复文本消息失败: {resp.code} {resp.msg}")
            return resp.data.message_id

        return await self._retry_with_backoff(_reply, max_retries=2)

    async def reply_post(
        self, message_id: str, title: str, body_text: str,
    ) -> str:
        """post（富文本）格式 reply 一条已有消息。同 thread / reply 链下挂。

        用于 inbox case session：同一 case 后续派单都 reply 到首条顶楼下，
        话题群会自动把它聚到同一 thread，dispatcher 拿到的 thread_id 就一致，
        Claude session 自然延续。
        """
        line: list[dict] = [{"tag": "text", "text": body_text}]
        content_payload = {
            "zh_cn": {
                "title": title,
                "content": [line],
            }
        }

        async def _do():
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("post")
                    .content(json.dumps(content_payload, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.areply(req)
            if not resp.success():
                raise RuntimeError(f"reply post 失败: {resp.code} {resp.msg}")
            return resp.data.message_id

        return await self._retry_with_backoff(_do, max_retries=2)

    async def send_post_to_chat(
        self, chat_id: str, title: str, body_text: str, mention_open_id: str = "",
    ) -> str:
        """往群里发一条 post（富文本）顶楼消息，可选 @ 一个 user。

        话题群（dispatch group）里把它当 anchor 用：返回的 message_id 是 om_xxx，
        /spawn 服务端会自动 mget 转成 omt_xxx 作 thread_id。
        """
        line: list[dict] = []
        if mention_open_id:
            line.append({"tag": "at", "user_id": mention_open_id})
            line.append({"tag": "text", "text": " "})
        line.append({"tag": "text", "text": body_text})

        content_payload = {
            "zh_cn": {
                "title": title,
                "content": [line],
            }
        }
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("post")
                .content(json.dumps(content_payload, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.acreate(req)
        if not resp.success():
            raise RuntimeError(f"发送 post 消息失败: {resp.code} {resp.msg}")
        return resp.data.message_id

    async def send_text_to_user(self, open_id: str, text: str) -> str:
        """发送纯文本消息"""
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.acreate(req)
        if not resp.success():
            raise RuntimeError(f"发送文本消息失败: {resp.code} {resp.msg}")
        return resp.data.message_id
