"""外部群 → 本地 agent → 以机主身份回话题 的单轮编排。

和主 bot 的 dispatcher 是同一套顺序（本轮头 → 话题上下文 → 正文 → system prompt
→ run_agent → 回复），区别只在两头：
  · 入口没有 WebSocket 事件，是 watcher 轮询出来的一条 dict；
  · 出口不是 bot 卡片，是 lark-cli 用机主 user token 回到话题里。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from typing import Optional

from agent_runner import run_agent
from bot_config import PROFILES, Profile
from log_util import log

from external_watcher.lark_user_api import (
    EXTERNAL_WRITE_DENIED,
    LarkCliError,
    LarkUserApi,
    extract_image_keys,
    strip_resource_markup,
)
from external_watcher.state import WatcherState
from external_watcher.thread_context import build_thread_context
from lark_prompts import render_external_prompt

TAG = "ext-web"

# agent 判断"这条不该我答"时输出的哨兵。收到就整轮跳过、群里一个字都不发。
SKIP_SENTINEL = "-"


class ExternalSendBlocked(RuntimeError):
    """Lark 不允许在这个群里以用户身份发消息（230027）。

    这不是偶发失败——重试多少次都一样。抛给 watcher 去把这个群熔断，
    否则每来一条 @ 都要白跑一次 agent、烧一次额度、再失败一次。
    """


def _resolve_profile(name: str) -> Profile:
    for p in PROFILES:
        if p.name == name:
            return p
    if PROFILES:
        return PROFILES[0]
    return Profile(
        name=name, app_id="", app_secret="", platform="lark",
        domain="open.larksuite.com", default_cwd=os.getcwd(),
    )


def turn_header(message_id: str, asker_open_id: str) -> str:
    """跟主 bot 一致的【本轮】行：把每轮都变的 id 放正文头部而不是 system prompt，
    避免打断 prompt cache 前缀。"""
    parts = []
    if message_id:
        parts.append(f"消息 id: {message_id}")
    if asker_open_id:
        parts.append(f"提问者 open_id: {asker_open_id}")
    if not parts:
        return ""
    return "【本轮 · " + " · ".join(parts) + "】\n\n"


def strip_mentions(content: str, mentions: Optional[list]) -> str:
    """把 `@张三` 这类 mention 文本从正文里去掉（结构化 mentions 里有名字才去）。"""
    text = content or ""
    for men in mentions or []:
        name = str((men or {}).get("name") or "").strip()
        if name:
            text = text.replace(f"@{name}", " ")
    # lark-cli 偶尔会留下 @_user_N 占位
    text = re.sub(r"@_user_\d+", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


async def build_message_body(
    api: LarkUserApi, msg: dict, download_dir: str,
) -> tuple[str, list[str]]:
    """把一条群消息压成给 agent 的正文。图片下载到本地并按主 bot 的措辞交代路径。"""
    content = str(msg.get("content") or "")
    mentions = msg.get("mentions") or []
    mid = str(msg.get("message_id") or "")

    image_keys = extract_image_keys(content)
    text = strip_mentions(strip_resource_markup(content), mentions)

    img_paths: list[str] = []
    for n, key in enumerate(image_keys):
        try:
            path = await api.download_resource(
                mid, key, "image", os.path.join(download_dir, f"{mid}_cur_{n}")
            )
            img_paths.append(path)
        except Exception as e:
            log(TAG, "post", "warn", f"下载图片失败 key={key[:12]}…: {e}")

    if img_paths:
        paths_list = "\n".join(f"  - {p}" for p in img_paths)
        caption = text or "（无文字说明）"
        body = (
            f"[对方发送了富文本消息，含 {len(img_paths)} 张图片]\n"
            f"文字内容：{caption}\n"
            f"图片路径：\n{paths_list}\n"
            f"请读取并分析这些图片，结合文字回复。"
        )
        return body, img_paths

    return text, []


async def _notify_owner(api: LarkUserApi, owner_open_id: str, text: str) -> None:
    """出错时私聊机主。用 bot 身份发（bot 和机主在同一租户，私聊发得到），
    失败也只记日志——通报失败不该再把主流程带崩。"""
    if not owner_open_id:
        return
    try:
        await api._run([
            "im", "+messages-send",
            "--user-id", owner_open_id,
            "--as", "bot",
            "--text", text,
            "--format", "json",
        ])
    except Exception as e:
        log(TAG, "notify", "warn", f"给机主发失败通报也失败了: {e}")


async def handle_external_message(
    *,
    cfg,
    group,
    api: LarkUserApi,
    state: WatcherState,
    msg: dict,
    owner_open_id: str,
    owner_name: str,
    download_dir: str,
) -> None:
    """处理一条命中的外部群消息：跑 agent，然后以机主身份回到话题里。"""
    chat_id = str(msg.get("chat_id") or group.chat_id)
    thread_id = str(msg.get("thread_id") or "")
    message_id = str(msg.get("message_id") or "")
    sender = msg.get("sender") or {}
    sender_name = str(sender.get("name") or "对方")
    sender_id = str(sender.get("id") or "")

    profile = _resolve_profile(cfg.user_profile)
    runner = group.runner or profile.runner or "claude"
    cwd = group.cwd or profile.default_cwd or os.getcwd()

    body, _img_paths = await build_message_body(api, msg, download_dir)

    # ── 话题上下文（跟主 bot 同一套【话题新增 · N 条】格式）─────────
    thread_state = state.get_thread(chat_id, thread_id)
    context_block = ""
    ctx_err: Optional[str] = None
    if thread_id:
        context_block, _ctx_paths, ctx_err = await build_thread_context(
            api, chat_id, thread_id,
            last_seen=thread_state.last_seen,
            current_message_id=message_id,
            owner_open_id=owner_open_id,
            download_dir=download_dir,
        )
        if ctx_err:
            log(TAG, "thread", "warn", f"拉话题历史失败，仅用当前消息继续: {ctx_err}")

    if context_block:
        log(TAG, "thread", "info",
            f"注入上下文 last_seen={thread_state.last_seen[:12] or '-'}…")
        if body.strip():
            prompt = f"{context_block}\n\n【{sender_name} 刚刚 @ 了你，说】\n{body}"
        else:
            prompt = f"{context_block}\n\n【{sender_name} 刚刚 @ 了你，但没有新正文，请基于上方内容回复】"
    else:
        if not body.strip():
            log(TAG, "bridge", "info", "只 @ 了一下且没有可读上下文，跳过本轮")
            return
        prompt = body

    prompt = turn_header(message_id, sender_id) + prompt

    system_prompt = render_external_prompt(
        profile,
        chat_id=chat_id,
        thread_id=thread_id,
        group_name=group.name,
        owner_name=owner_name,
        cli_profile=cfg.user_profile,
        runner=runner,
        # 必须 @ 才唤醒的群 = 每条到手的消息都是冲你来的，不给"可以不回"的口子
        allow_skip=not group.require_mention,
    )

    # 只给回复锚点 / 提问者，**不给** CC_LARK_THREAD_ID —— 运行时 MCP（wake /
    # dispatch / cron）要往 bot 能发卡片的话题投递，外部群里 bot 根本不在，
    # 注入了只会让 agent 开空头支票。见 prompts/_runtime_mcp_other.md。
    wake_context = {
        "CC_LARK_PROFILE_NAME": profile.name,
        "CC_LARK_CLI_PROFILE": cfg.user_profile,
        "CC_LARK_CHAT_ID": chat_id,
        "CC_LARK_MESSAGE_ID": message_id,
        "CC_LARK_ANCHOR": message_id,
        "CC_LARK_USER_ID": sender_id,
        "CC_LARK_IS_GROUP": "1",
        "CC_LARK_EXTERNAL": "1",
    }

    # system prompt 变了就不能再 resume 老会话：旧指令已经落在对话历史里，
    # 换了 append_system_prompt 也撤不掉（实测哨兵指令删了之后 resume 出来的
    # 会话照旧吐 `-`）。指纹对不上直接开新会话，宁可丢一点上下文。
    prompt_sig = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
    resume_id = thread_state.session_id or None
    # 注意条件是 `!=` 而不是 `and 有旧指纹`：升级前存下来的会话没有指纹，
    # 而它们恰恰是最需要作废的那批（就是它们还揣着老规则）。
    if resume_id and thread_state.prompt_sig != prompt_sig:
        log(TAG, "bridge", "info",
            f"system prompt 已变更（{thread_state.prompt_sig or '无指纹/升级前'}"
            f"→{prompt_sig}），丢弃旧会话 {resume_id[:12]}… 开新会话")
        resume_id = None

    log(TAG, "bridge", "info",
        f"起跑 runner={runner} session={(resume_id or '')[:12] or '新'}… "
        f"thread={thread_id[:14] or '-'} from={sender_name} len={len(prompt)}")

    try:
        result_text, new_session_id, _fresh = await run_agent(
            profile=profile,
            runner=runner,
            message=prompt,
            session_id=resume_id,
            model=group.model or None,
            cwd=cwd,
            append_system_prompt=system_prompt,
            wake_context=wake_context,
        )
    except Exception as e:
        log(TAG, "bridge", "error", f"agent 执行异常: {e}")
        if getattr(cfg, "notify_owner_on_error", True):
            await _notify_owner(
                api, owner_open_id,
                f"⚠️ 外部群「{group.name or chat_id}」自动回复失败\n"
                f"提问者: {sender_name}\n消息: {message_id}\n错误: {e}",
            )
        return

    # session / last_seen 只在**跑成功**后推进：失败时保留旧水位线，
    # 下一条消息还能把这段 backlog 补进上下文。
    if new_session_id:
        state.update_thread(
            chat_id, thread_id, session_id=new_session_id, prompt_sig=prompt_sig,
        )
    if not ctx_err:
        state.update_thread(chat_id, thread_id, last_seen=message_id)
    state.save()

    reply = (result_text or "").strip()
    if not reply:
        log(TAG, "bridge", "warn", "agent 没有输出，群里不发任何东西")
        return
    if reply == SKIP_SENTINEL:
        log(TAG, "bridge", "info", "agent 判定本轮无需回复（哨兵 '-'），跳过")
        return

    try:
        sent_ids = await api.reply_markdown(message_id, reply, in_thread=bool(thread_id))
    except LarkCliError as e:
        log(TAG, "bridge", "error", f"以 user 身份回复失败: {e}")
        blocked = e.code == EXTERNAL_WRITE_DENIED or e.subtype == "user_unauthorized"
        if getattr(cfg, "notify_owner_on_error", True):
            if blocked:
                note = (
                    f"⛔ 外部群「{group.name or chat_id}」发送被 Lark 拒绝（230027）。\n"
                    f"Lark 不允许应用以用户身份往外部群写消息（读没问题）。\n"
                    f"已暂停该群的自动回复，避免继续空跑 agent；"
                    f"解决后重启 bot 恢复。\n\n"
                    f"这次生成的回答（{len(reply)} 字符）：\n{reply[:1500]}"
                )
            else:
                note = (
                    f"⚠️ 外部群「{group.name or chat_id}」回复发送失败：{e}\n"
                    f"（回答已生成，长度 {len(reply)}）"
                )
            await _notify_owner(api, owner_open_id, note)
        if blocked:
            raise ExternalSendBlocked(str(e)) from e
        return

    # 自己代发的消息记进 state：即使它恰好带上了 @机主，也绝不会被下一轮当成新触发。
    state.mark_seen(chat_id, sent_ids)
    state.mark_self_sent(sent_ids)
    state.save()
    log(TAG, "bridge", "info", f"已以 {owner_name} 身份回复 {len(sent_ids)} 条（{len(reply)} 字符）")
