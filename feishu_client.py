"""
飞书 API 异步封装。

默认流式方案：发送内联卡片消息 → 用 patch 逐步更新内容（简单可靠，且带
LARK_CARD_SCHEMA v1 降级兜底，是线上保命路径）。

可选流式方案（LARK_CARD_STREAMING=1 开启）：CardKit 流式卡片，客户端本地
打字机效果、增量推送更顺滑。按 message_id 在 FeishuClient 内部路由——只有
loading=True 的占位卡会建成 CardKit 流式卡，后续 update_card /
update_card_with_buttons 自动走 CardKit；flag 关闭时一切照旧走 PATCH。
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

import outbox


class FeishuApiError(RuntimeError):
    """飞书 API 返回非成功码。携带结构化 code，供 _retry_with_backoff 判定是否可重试。

    str() 输出沿用旧的 "<action>: <code> <msg>" 格式，对现有 except/日志零影响。
    """

    def __init__(self, action: str, code, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"{action}: {code} {msg}")


def _load_non_retryable_codes() -> frozenset:
    """不可重试的错误码集合。

    默认含 99991403（Lark 发送额度耗尽）——这类错误重试只会白烧 2~3 倍额度，
    应立即放弃并转 outbox。可用 LARK_NO_RETRY_CODES="99991403,230020" 覆盖。
    """
    raw = os.getenv("LARK_NO_RETRY_CODES", "").strip()
    if not raw:
        return frozenset({99991403})
    codes = set()
    for tok in raw.replace("，", ",").split(","):
        tok = tok.strip()
        if tok.isdigit():
            codes.add(int(tok))
    return frozenset(codes or {99991403})


NON_RETRYABLE_CODES = _load_non_retryable_codes()


def _streaming_enabled() -> bool:
    """是否启用 CardKit 流式卡片（默认否，走 PATCH）。"""
    return (os.getenv("LARK_CARD_STREAMING", "") or "").strip() in ("1", "true", "on", "yes")


def _sanitize_filename(name: str) -> str:
    """把不安全的字符替换成下划线，保留 CJK/字母数字/常见标点。截断到 100 字符。"""
    safe_chars = "-_.() "
    cleaned = "".join(c if c.isalnum() or c in safe_chars else "_" for c in name)
    cleaned = cleaned.strip()
    return cleaned[:100] or "file"


# ── 卡片 schema 兼容层 ────────────────────────────────────────
# 调用方一律按 Card JSON 2.0 构造卡片；当 Lark 的 2.0 渲染服务故障
# （报 230099 Failed to create card content / Server Internal Error）时，
# 设环境变量 LARK_CARD_SCHEMA=1.0 即可把卡片临时降级成 v1 schema 顶着，
# 等官方修好 2.0 再把开关切回 "2.0"（默认），业务代码零改动。

def _use_v1_card() -> bool:
    """是否把卡片降级成 v1 schema（默认否，用 2.0）"""
    return (os.getenv("LARK_CARD_SCHEMA", "2.0") or "2.0").strip() in ("1", "1.0", "v1")


def _downgrade_button(btn: dict) -> dict:
    """Card 2.0 button → v1 button（保留 value 供点击回调 action.value 读取）"""
    out = {
        "tag": "button",
        "text": btn.get("text", {"tag": "plain_text", "content": ""}),
        "type": btn.get("type", "default"),
    }
    if "size" in btn:
        out["size"] = btn["size"]
    if "value" in btn:
        out["value"] = btn["value"]
    return out


def _downgrade_element(el: dict) -> list:
    """把单个 2.0 元素降级成 0..n 个 v1 元素"""
    tag = el.get("tag")
    if tag == "markdown":
        return [{"tag": "div", "text": {"tag": "lark_md", "content": el.get("content", "")}}]
    if tag == "button":
        return [{"tag": "action", "actions": [_downgrade_button(el)]}]
    if tag == "column_set":
        # v1 无 flow 列布局：列里的 button 收拢成一个 action 行，其余元素逐个降级
        actions, others = [], []
        for col in el.get("columns", []):
            for sub in (col.get("elements") or []):
                if sub.get("tag") == "button":
                    actions.append(_downgrade_button(sub))
                else:
                    others.extend(_downgrade_element(sub))
        result = list(others)
        if actions:
            result.append({"tag": "action", "actions": actions})
        return result
    if tag == "hr":
        return [{"tag": "hr"}]
    return [el]  # div/lark_md/img 等 v1 也认，原样保留


def _serialize_card(card: dict) -> str:
    """序列化卡片 dict；LARK_CARD_SCHEMA=1.0 时把 2.0 卡片降级成 v1"""
    if _use_v1_card():
        elements = (card.get("body") or {}).get("elements", [])
        v1_elements = []
        for el in elements:
            v1_elements.extend(_downgrade_element(el))
        card = {"config": {"wide_screen_mode": True}, "elements": v1_elements}
    return json.dumps(card, ensure_ascii=False)


def _card_dict(content: str, loading: bool = False) -> dict:
    """
    构造卡片 Card JSON 2.0 dict（未序列化、未降级）。

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

    return {"schema": "2.0", "body": {"elements": elements}}


def _card_json(content: str, loading: bool = False) -> str:
    """生成卡片 JSON 字符串（默认 Card JSON 2.0；LARK_CARD_SCHEMA=1.0 时降级 v1）"""
    return _serialize_card(_card_dict(content, loading=loading))


class FeishuClient:
    def __init__(self, client: lark.Client, app_id: str = "", app_secret: str = "",
                 domain: str = "https://open.feishu.cn", label: str = ""):
        self.client = client
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = domain.rstrip("/")
        # bot/profile 标识，用于发送彻底失败时落 outbox 文件名
        self.label = label or (app_id[-6:] if app_id else "bot")
        self._bot_open_id: Optional[str] = None
        # CardKit 流式卡登记表：message_id → {"card_id", "seq", "element_id"}。
        # 仅 LARK_CARD_STREAMING=1 且 loading 占位卡才会登记；update_card 等据此
        # 决定走 CardKit 还是 PATCH。重启即丢，只服务进行中的 run。
        self._streaming_cards: dict[str, dict] = {}
        self._STREAMING_MAX = 200
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

    def save_outbox(self, content: str, *, kind: str = "result", error: str = "",
                    meta: Optional[dict] = None) -> Optional[str]:
        """发送彻底失败时，把内容落到本 bot 的 outbox 文件，避免结果丢失。永不抛异常。"""
        return outbox.record(self.label, content, kind=kind, error=error, meta=meta)

    # ── CardKit 流式卡登记 ────────────────────────────────────
    def _register_streaming(self, message_id: str, card_id: str, element_id: str) -> None:
        if not message_id or not card_id:
            return
        if len(self._streaming_cards) >= self._STREAMING_MAX:
            for k in list(self._streaming_cards.keys())[: self._STREAMING_MAX // 4 or 1]:
                self._streaming_cards.pop(k, None)
        self._streaming_cards[message_id] = {"card_id": card_id, "seq": 0, "element_id": element_id}

    def _next_seq(self, message_id: str) -> Optional[tuple[str, str, int]]:
        """取流式卡 (card_id, element_id, 递增后的 sequence)；非流式卡返回 None。"""
        st = self._streaming_cards.get(message_id)
        if not st:
            return None
        st["seq"] += 1
        return st["card_id"], st["element_id"], st["seq"]

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
                # 额度耗尽等不可重试码：重试纯属白烧额度，立即放弃
                code = getattr(e, "code", None)
                if code is not None and code in NON_RETRYABLE_CODES:
                    print(f"[retry] 错误码 {code} 不可重试（如额度耗尽），立即放弃: {e}", flush=True)
                    raise
                if attempt < max_retries:
                    print(f"[retry] 第 {attempt + 1} 次失败，{delay:.1f}s 后重试: {e}", flush=True)
                    await asyncio.sleep(delay)
                    delay *= 2  # 指数退避
                else:
                    print(f"[retry] 已达最大重试次数 {max_retries + 1}，放弃", flush=True)

        raise last_error

    # ── 发送消息 ──────────────────────────────────────────────

    async def send_card_to_user(self, open_id: str, content: str = "", loading: bool = True) -> str:
        """向用户发送卡片消息，返回 message_id（带重试）。

        LARK_CARD_STREAMING=1 且 loading 占位卡时，改建 CardKit 流式卡。
        """
        if loading and _streaming_enabled():
            try:
                return await self._create_streaming_card(open_id=open_id)
            except Exception as e:
                print(f"[cardkit] 创建流式卡失败，回退普通卡: {e}", flush=True)

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
                raise FeishuApiError("发送卡片消息失败", resp.code, resp.msg)
            return resp.data.message_id

        mid = await self._retry_with_backoff(_send, max_retries=3)
        if not loading:
            self._remember_card_text(mid, content)
        return mid

    async def reply_card(self, message_id: str, content: str = "", loading: bool = True) -> str:
        """回复用户消息（卡片形式），触发通知。返回回复消息的 message_id（带重试）。

        LARK_CARD_STREAMING=1 且 loading 占位卡时，改建 CardKit 流式卡。
        """
        if loading and _streaming_enabled():
            try:
                return await self._create_streaming_card(reply_to=message_id)
            except Exception as e:
                print(f"[cardkit] 创建流式卡失败，回退普通卡: {e}", flush=True)

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
                raise FeishuApiError("回复卡片消息失败", resp.code, resp.msg)
            return resp.data.message_id

        mid = await self._retry_with_backoff(_reply, max_retries=3)
        if not loading:
            self._remember_card_text(mid, content)
        return mid

    async def update_card(self, message_id: str, content: str):
        """更新已发送的卡片内容（带重试）。流式卡走 CardKit 增量推送，否则 PATCH。"""
        if message_id in self._streaming_cards:
            await self._stream_update_text(message_id, content)
            self._remember_card_text(message_id, content)
            return

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
                raise FeishuApiError("patch 卡片失败", resp.code, resp.msg)

        await self._retry_with_backoff(_update, max_retries=3)
        self._remember_card_text(message_id, content)

    # ── CardKit 流式卡片（LARK_CARD_STREAMING=1 时启用）──────────
    # 语义：建卡片实体 → 用消息 API 发"卡片引用" → card_element.content 传全量文本
    # + 递增 sequence（客户端本地打字机）→ card.settings 关流式恢复交互。
    # 参考 GabrielZhu123456/feishu-claude-code 的实现，适配本仓库的 message_id 路由。

    async def _create_streaming_card(self, open_id: str = "", reply_to: str = "",
                                     element_id: str = "md_stream") -> str:
        """建 CardKit 流式卡并发送/回复，登记后返回 message_id。"""
        from lark_oapi.api.cardkit.v1.model import CreateCardRequest, CreateCardRequestBody

        card_json = json.dumps({
            "schema": "2.0",
            "config": {
                "streaming_mode": True,
                "streaming_config": {
                    "print_frequency_ms": {"default": 30},
                    "print_step": {"default": 2},
                    "print_strategy": "fast",
                },
            },
            "body": {"elements": [
                {"tag": "markdown", "element_id": element_id, "content": "⏳ 思考中..."},
            ]},
        }, ensure_ascii=False)

        async def _create():
            req = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder().type("card_json").data(card_json).build()
                )
                .build()
            )
            resp = await self.client.cardkit.v1.card.acreate(req)
            if not resp.success():
                raise FeishuApiError("创建流式卡失败", resp.code, resp.msg)
            return resp.data.card_id

        card_id = await self._retry_with_backoff(_create, max_retries=2)
        card_ref = json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)

        if reply_to:
            msg_id = await self._retry_with_backoff(
                lambda: self._send_card_entity(card_ref, reply_to=reply_to), max_retries=2)
        else:
            msg_id = await self._retry_with_backoff(
                lambda: self._send_card_entity(card_ref, open_id=open_id), max_retries=2)

        self._register_streaming(msg_id, card_id, element_id)
        return msg_id

    async def _send_card_entity(self, card_ref: str, open_id: str = "", reply_to: str = "") -> str:
        """把已建好的卡片实体作为消息发出（主动发或回复）。"""
        if reply_to:
            req = (
                ReplyMessageRequest.builder()
                .message_id(reply_to)
                .request_body(
                    ReplyMessageRequestBody.builder().msg_type("interactive").content(card_ref).build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.areply(req)
            if not resp.success():
                raise FeishuApiError("回复流式卡实体失败", resp.code, resp.msg)
            return resp.data.message_id
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id).msg_type("interactive").content(card_ref).build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.acreate(req)
        if not resp.success():
            raise FeishuApiError("发送流式卡实体失败", resp.code, resp.msg)
        return resp.data.message_id

    async def _stream_update_text(self, message_id: str, content: str):
        """流式更新文本（全量内容 + 递增 sequence）。"""
        from lark_oapi.api.cardkit.v1.model import (
            ContentCardElementRequest, ContentCardElementRequestBody,
        )
        nxt = self._next_seq(message_id)
        if not nxt:
            return
        card_id, element_id, seq = nxt

        async def _update():
            req = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(element_id)
                .request_body(
                    ContentCardElementRequestBody.builder().content(content).sequence(seq).build()
                )
                .build()
            )
            resp = await self.client.cardkit.v1.card_element.acontent(req)
            if not resp.success():
                raise FeishuApiError("流式更新失败", resp.code, resp.msg)

        await self._retry_with_backoff(_update, max_retries=1)

    async def finalize_streaming_card(self, message_id: str, buttons: Optional[list[dict]] = None,
                                      flow: bool = False):
        """关闭流式模式（恢复交互/转发），可选追加按钮，然后注销登记。

        非流式卡或未登记的 message_id：no-op。永不抛异常——收尾失败不该影响主流程。
        """
        st = self._streaming_cards.get(message_id)
        if not st:
            return
        card_id = st["card_id"]
        try:
            from lark_oapi.api.cardkit.v1.model import SettingsCardRequest, SettingsCardRequestBody
            st["seq"] += 1
            seq = st["seq"]

            async def _finish():
                req = (
                    SettingsCardRequest.builder()
                    .card_id(card_id)
                    .request_body(
                        SettingsCardRequestBody.builder()
                        .settings(json.dumps({"streaming_mode": False}))
                        .sequence(seq)
                        .build()
                    )
                    .build()
                )
                resp = await self.client.cardkit.v1.card.asettings(req)
                if not resp.success():
                    raise FeishuApiError("关闭流式模式失败", resp.code, resp.msg)

            await self._retry_with_backoff(_finish, max_retries=2)

            if buttons:
                st["seq"] += 1
                await self._streaming_add_buttons(card_id, buttons, flow=flow, sequence=st["seq"])
        except Exception as e:
            print(f"[cardkit] finalize 失败 msg={message_id[:12]}: {e}", flush=True)
        finally:
            self._streaming_cards.pop(message_id, None)

    async def _streaming_add_buttons(self, card_id: str, buttons: list[dict], flow: bool = False,
                                     sequence: int = 1):
        """流式结束后给卡片追加按钮元素。sequence 为必填（缺则 99992402 校验失败）。"""
        from lark_oapi.api.cardkit.v1.model import (
            CreateCardElementRequest, CreateCardElementRequestBody,
        )
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
        if not btn_elements:
            return
        if len(btn_elements) == 1 and not flow:
            new_elements = [btn_elements[0]]
        else:
            new_elements = [{
                "tag": "column_set", "flex_mode": "flow",
                "columns": [{"tag": "column", "width": "auto", "elements": [b]} for b in btn_elements],
            }]
        # SDK 1.5.3：elements 是「元素列表的 JSON 字符串」，type=append 追加到卡片末尾
        elements_json = json.dumps(new_elements, ensure_ascii=False)

        async def _add():
            req = (
                CreateCardElementRequest.builder()
                .card_id(card_id)
                .request_body(
                    CreateCardElementRequestBody.builder()
                    .type("append")
                    .elements(elements_json)
                    .sequence(sequence)
                    .build()
                )
                .build()
            )
            resp = await self.client.cardkit.v1.card_element.acreate(req)
            if not resp.success():
                raise FeishuApiError("流式卡追加按钮失败", resp.code, resp.msg)

        await self._retry_with_backoff(_add, max_retries=2)

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
        """更新卡片内容并附加操作按钮。flow=True 时横排自动换行，False 时竖排。

        流式卡：先推最终全量文本，再关流式 + 用 CardKit 追加按钮（按钮必须等
        streaming_mode 关掉后才能交互），最后注销登记。
        """
        if message_id in self._streaming_cards:
            await self._stream_update_text(message_id, content)
            self._remember_card_text(message_id, content)
            await self.finalize_streaming_card(message_id, buttons=buttons, flow=flow)
            return

        base = _card_dict(content)
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
        card_content = _serialize_card(base)

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
                raise FeishuApiError("patch 卡片失败", resp.code, resp.msg)

        await self._retry_with_backoff(_update, max_retries=3)
        self._remember_card_text(message_id, content)

    async def update_card_elements(self, message_id: str, elements: list[dict]):
        """用自定义 elements 列表更新卡片（支持 markdown + button 混排）"""
        card_content = _serialize_card({
            "schema": "2.0",
            "body": {"elements": elements},
        })

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
                raise FeishuApiError("patch 卡片失败", resp.code, resp.msg)

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
                raise FeishuApiError("回复文本消息失败", resp.code, resp.msg)
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
                raise FeishuApiError("reply post 失败", resp.code, resp.msg)
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
            raise FeishuApiError("发送 post 消息失败", resp.code, resp.msg)
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
            raise FeishuApiError("发送文本消息失败", resp.code, resp.msg)
        return resp.data.message_id
