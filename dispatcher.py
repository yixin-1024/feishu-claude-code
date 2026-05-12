"""消息分发 + 业务核心。

从 WS 收到一条 Lark 消息，到向 Claude 发请求、把流式输出推回飞书，
全部逻辑都在这里。也包括卡片按钮回调、/spawn、/handover、菜单命令等业务路径。

不直接持有 bot_loop / bots 的全局；通过 configure() 注入，避免与 main.py
循环依赖。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import traceback
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger, P2CardActionTriggerResponse, CallBackToast,
)

from bot_config import Profile
from bot_instance import BotInstance
from claude_runner import run_claude
from commands import parse_command, handle_command
from feishu_post import parse_post_content, extract_post_image_keys
from lark_prompts import render_lark_prompt
from log_util import log
from run_control import ActiveRun, stop_run
from thread_context import build_thread_context
from trinity_dispatch import maybe_handle_trinity, TrinityContext


# ── 注入点 ────────────────────────────────────────────────────

_bot_loop: Optional[asyncio.AbstractEventLoop] = None
_bots: dict[str, BotInstance] = {}


def configure(
    *,
    bot_loop: asyncio.AbstractEventLoop,
    bots: dict[str, BotInstance],
) -> None:
    """main.py 启动时调一次。注入跨 profile 共享的 loop 和 bot 注册表。"""
    global _bot_loop, _bots
    _bot_loop = bot_loop
    _bots = bots


# ── 工具：从 lark event 解析消息字段 ────────────────────────

def extract_chat_info(event: P2ImMessageReceiveV1) -> tuple[str, str, bool, str, str]:
    """Returns: (user_id, chat_id, is_group, raw_chat_id, thread_id)"""
    sender = event.event.sender
    user_id = sender.sender_id.open_id

    message = event.event.message
    chat_type = message.chat_type
    chat_id_raw = message.chat_id
    thread_id = getattr(message, "thread_id", "") or ""

    is_group = (chat_type == "group")
    if is_group:
        chat_id = f"{chat_id_raw}:{thread_id}" if thread_id else chat_id_raw
    else:
        chat_id = user_id

    return user_id, chat_id, is_group, chat_id_raw, thread_id


# ── /stop 命令处理 ───────────────────────────────────────────

async def _announce_stopped_run(bot: BotInstance, active_run: ActiveRun):
    try:
        await bot.feishu.update_card(active_run.card_msg_id, "⏹ 已停止当前任务")
    except Exception as exc:
        log(bot.profile.name, "stop", "warn", f"update stopped card failed: {exc}")


async def _handle_stop_command(bot: BotInstance, sender_open_id: str, chat_id: str) -> str:
    active_run = bot.active_runs.get_run(sender_open_id, chat_id)
    if active_run is None:
        return "当前没有正在运行的任务"
    if active_run.stop_requested:
        return "正在停止当前任务，请稍候"
    stopped = await stop_run(
        bot.active_runs,
        sender_open_id,
        chat_id,
        on_stopped=lambda run: _announce_stopped_run(bot, run),
    )
    if not stopped:
        return "当前没有正在运行的任务"
    return "已发送停止请求"


# ── 命令菜单（锁外即时响应）──────────────────────────────────

_COMMAND_MENU_GROUPS = [
    ("**会话**", [
        {"text": "🆕 新会话",      "value": {"action": "run_cmd", "cmd": "/new"}},
        {"text": "📋 新会话(规划)", "value": {"action": "run_cmd", "cmd": "/new plan"}},
        {"text": "📂 恢复会话",    "value": {"action": "run_cmd", "cmd": "/resume"}},
        {"text": "⏹ 停止任务",     "value": {"action": "run_cmd", "cmd": "/stop"}},
    ]),
    ("**配置**", [
        {"text": "🔄 切模型",      "value": {"action": "run_cmd", "cmd": "/model"}},
        {"text": "⚙️ 切模式",      "value": {"action": "run_cmd", "cmd": "/mode"}},
        {"text": "📁 工作空间",    "value": {"action": "run_cmd", "cmd": "/ws"}},
    ]),
    ("**查看**", [
        {"text": "📊 状态",        "value": {"action": "run_cmd", "cmd": "/status"}},
        {"text": "📈 用量",        "value": {"action": "run_cmd", "cmd": "/usage"}},
        {"text": "🛠 Skills",      "value": {"action": "run_cmd", "cmd": "/skills"}},
        {"text": "🔌 MCP",         "value": {"action": "run_cmd", "cmd": "/mcp"}},
        {"text": "📄 目录",        "value": {"action": "run_cmd", "cmd": "/ls"}},
        {"text": "❓ 帮助",        "value": {"action": "run_cmd", "cmd": "/help"}},
    ]),
]


async def _show_command_menu(bot: BotInstance, user_id: str, chat_id: str, is_group: bool, msg_id: str):
    """显示分组命令菜单，不走队列锁"""
    elements = []
    for title, buttons in _COMMAND_MENU_GROUPS:
        elements.append({"tag": "markdown", "content": title})
        columns = []
        for btn in buttons:
            value = {**btn["value"], "cid": chat_id, "profile": bot.profile.name}
            columns.append({
                "tag": "column",
                "width": "auto",
                "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": btn["text"]},
                    "type": "default",
                    "size": "small",
                    "name": f"menu_{btn['value']['cmd'].replace('/', '').replace(' ', '_')}",
                    "value": value,
                    "behaviors": [{"type": "callback", "value": value}],
                }],
            })
        elements.append({"tag": "column_set", "flex_mode": "flow", "columns": columns})
    try:
        if is_group:
            card_id = await bot.feishu.reply_card(msg_id, content="⚡ 快捷命令", loading=False)
        else:
            card_id = await bot.feishu.send_card_to_user(user_id, content="⚡ 快捷命令", loading=False)
        await bot.feishu.update_card_elements(card_id, elements)
    except Exception as e:
        log(bot.profile.name, "menu", "error", f"命令菜单发送失败: {e}")


# ── 核心消息处理 ─────────────────────────────────────────────

async def handle_message_async(bot: BotInstance, event: P2ImMessageReceiveV1):
    """异步处理一条飞书消息"""
    msg = event.event.message
    tag = bot.profile.name
    log(tag, "msg", "info", f"收到消息 type={msg.message_type} chat={msg.chat_type}")

    user_id, chat_id, is_group, raw_chat_id, thread_id = extract_chat_info(event)
    log(tag, "msg", "info",
        f"user={user_id[:8]}... chat={raw_chat_id[:10]}... "
        f"thread={thread_id[:10] if thread_id else '-'} is_group={is_group}")

    # ── Trinity 三省体系入口（必须在 allowed_open_ids 检查之前）─────
    # trinity bot 间通信时 sender 是另一个 bot 的 open_id，不会在用户白名单里。
    # maybe_handle_trinity 内部会做角色识别 + transition 校验。
    trinity_ctx: Optional[TrinityContext] = None
    if bot.profile.is_trinity:
        decision = await maybe_handle_trinity(
            bot.profile, user_id, raw_chat_id, thread_id, msg.message_id,
        )
        if decision.reject_reason:
            try:
                await bot.feishu.reply_text(msg.message_id, decision.reject_reason)
            except Exception as e:
                log(tag, "trinity", "warn", f"拒绝回复失败: {e}")
            return
        if decision.handled and decision.context is None:
            # 静默忽略（非授权发件人）
            return
        trinity_ctx = decision.context
        # trinity 路径通过后跳过常规白名单（同体系 bot 互发不受这两个限制）

    # 访问控制：群聊白名单 + 用户 allowlist（静默忽略，避免泄露 bot 存在）
    if not bot.profile.is_trinity:
        if is_group and raw_chat_id not in bot.profile.allowed_group_chat_ids:
            log(tag, "acl", "info", f"群不在白名单 chat={raw_chat_id[:10]}...")
            return
        if bot.profile.allowed_open_ids and user_id not in bot.profile.allowed_open_ids:
            log(tag, "acl", "info", f"user={user_id} 不在 allowlist")
            return

    # /stop 和 / 在锁外处理
    if msg.message_type == "text":
        try:
            _text = json.loads(msg.content).get("text", "").strip()
        except Exception:
            _text = ""
        if is_group:
            for m in (getattr(msg, 'mentions', None) or []):
                k = getattr(m, 'key', '')
                if k:
                    _text = _text.replace(k, '').strip()

        if _text.lower() == "/stop" or _text.strip().endswith("/stop"):
            reply = await _handle_stop_command(bot, user_id, chat_id)
            if is_group:
                await bot.feishu.reply_card(msg.message_id, content=reply, loading=False)
            else:
                await bot.feishu.send_card_to_user(user_id, content=reply, loading=False)
            return

        if _text == "/":
            await _show_command_menu(bot, user_id, chat_id, is_group, msg.message_id)
            return

    # 群聊只响应 @机器人 的消息
    if is_group:
        mentions = getattr(msg, 'mentions', None) or []
        if not mentions:
            return
        bot_open_id = await bot.feishu.get_bot_open_id()
        if bot_open_id:
            mentioned_bot = any(
                getattr(getattr(m, 'id', None), 'open_id', '') == bot_open_id
                for m in mentions
            )
            if not mentioned_bot:
                return

    lock = bot._ensure_chat_lock(chat_id)

    if lock.locked():
        try:
            await bot.feishu.reply_text(msg.message_id, "📬 前面还有任务在跑，排队中（/stop 可打断）")
        except Exception:
            pass

    async with lock:
        try:
            await _process_message(
                bot, user_id, chat_id, is_group, thread_id, msg,
                trinity_ctx=trinity_ctx,
            )
        except Exception as e:
            log(tag, "msg", "error", f"消息处理异常: {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()


async def _run_and_display(
    bot: BotInstance,
    user_id: str, chat_id: str, is_group: bool,
    text: str, card_msg_id: str, session, notify_msg_id: str,
    preview_text: str = "",
    append_system_prompt: str = "",
):
    """调用 Claude 并流式展示结果。消息处理和按钮回复共用此函数。"""
    active_run = bot.active_runs.start_run(user_id, chat_id, card_msg_id)

    accumulated = ""
    tool_history: list[str] = []
    ask_options: list[tuple[str, str]] = []
    plan_exited = False
    final_usage: dict = {}
    last_push_time = 0.0
    push_failures = 0
    _PUSH_INTERVAL = 0.4
    _MAX_STREAM_DISPLAY = 2500

    start_ts = time.time()
    last_output_ts = start_ts
    current_tool: tuple[str, float] | None = None

    def _fmt_duration(seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        m, sec = divmod(s, 60)
        if m < 60:
            return f"{m}m{sec:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"

    async def push(content: str):
        nonlocal push_failures
        if push_failures >= 3:
            return
        try:
            await bot.feishu.update_card(card_msg_id, content)
            push_failures = 0
        except Exception as push_err:
            push_failures += 1
            log(bot.profile.name, "stream", "warn",
                f"push 失败 ({push_failures}/3): {push_err}")

    def _build_display() -> str:
        parts = []
        if tool_history:
            parts.append("\n".join(tool_history[-5:]))
        if accumulated:
            if parts:
                parts.append("")
            d = accumulated
            if len(d) > _MAX_STREAM_DISPLAY:
                d = "...\n\n" + d[-_MAX_STREAM_DISPLAY:]
            parts.append(d)
        body = "\n".join(parts) if parts else "⏳ 思考中..."

        now = time.time()
        footer = [f"⏱ {_fmt_duration(now - start_ts)}"]
        if current_tool:
            tname, t_started = current_tool
            footer.append(f"🔧 {tname} {_fmt_duration(now - t_started)}")
        idle = now - last_output_ts
        if idle >= 30:
            footer.append(f"⚠️ 无输出 {_fmt_duration(idle)}")
        return f"{body}\n\n`{' · '.join(footer)}`"

    async def on_tool_use(name: str, inp: dict):
        nonlocal accumulated, last_push_time, plan_exited, current_tool, last_output_ts
        if name.lower() == "exitplanmode":
            plan_exited = True
            return
        if name.lower() == "enterplanmode":
            if session.permission_mode != "plan":
                log(bot.profile.name, "plan", "info", "EnterPlanMode 检测到，切换为 plan")
                await bot.store.set_permission_mode(user_id, chat_id, "plan")
            return
        if name.lower() == "enterworktree" and inp:
            wt_name = inp.get("name", "")
            if wt_name:
                log(bot.profile.name, "worktree", "info", f"进入: {wt_name}")
            return
        if name.lower() == "exitworktree":
            log(bot.profile.name, "worktree", "info", "退出")
            return
        if name.lower() == "askuserquestion":
            question = inp.get("question", inp.get("text", ""))
            if question:
                accumulated += f"\n\n❓ **等待回复：**\n{question}"
                detected = _extract_options(question)
                if detected:
                    ask_options.clear()
                    ask_options.extend(detected)
                await push(_build_display())
                return
        tool_line = _format_tool(name, inp)
        if inp and tool_history:
            tool_history[-1] = tool_line
        else:
            tool_history.append(tool_line)
        current_tool = (name, time.time())
        last_output_ts = time.time()
        await push(_build_display())
        last_push_time = time.time()

    async def on_text_chunk(chunk: str):
        nonlocal accumulated, last_push_time, current_tool, last_output_ts
        if current_tool:
            current_tool = None
        accumulated += chunk
        last_output_ts = time.time()
        now = time.time()
        if now - last_push_time >= _PUSH_INTERVAL:
            await push(_build_display())
            last_push_time = now

    def on_usage(usage: dict):
        final_usage.update(usage)

    async def _heartbeat():
        nonlocal last_push_time
        try:
            while True:
                await asyncio.sleep(1.0)
                if time.time() - last_push_time >= 1.5:
                    await push(_build_display())
                    last_push_time = time.time()
        except asyncio.CancelledError:
            pass

    heartbeat_task = asyncio.create_task(_heartbeat())

    claude_msg = text
    # 外层 try/finally 让 active_run 的生命周期对齐 lock —— 后处理（卡片 patch、发✅、
    # 写 session）期间 lock 仍然 held，active_run 也必须仍可被 /stop 找到，否则会出现
    # "队列说在跑、/stop 说没在跑" 的死区。
    try:
        try:
            log(bot.profile.name, "claude", "info", "开始调用...")
            full_text, new_session_id, used_fresh_session_fallback = await run_claude(
                message=claude_msg,
                session_id=session.session_id,
                model=session.model,
                cwd=session.cwd,
                permission_mode=session.permission_mode,
                on_text_chunk=on_text_chunk,
                on_tool_use=on_tool_use,
                on_process_start=lambda proc: bot.active_runs.attach_process(user_id, chat_id, proc),
                on_usage=on_usage,
                append_system_prompt=append_system_prompt or None,
            )
            log(bot.profile.name, "claude", "info", f"完成, session={new_session_id}")
        except Exception as e:
            if active_run.stop_requested:
                return
            log(bot.profile.name, "claude", "error", f"运行失败: {type(e).__name__}: {e}")
            traceback.print_exc()
            try:
                await bot.feishu.update_card(card_msg_id, f"❌ Claude 执行出错：{type(e).__name__}: {e}")
            except Exception:
                pass
            return
        finally:
            heartbeat_task.cancel()

        final = full_text or accumulated or "（无输出）"
        if used_fresh_session_fallback:
            final = (
                "⚠️ 检测到工作目录已变化，旧会话无法继续。"
                "本次已自动切换到新 session。\n\n" + final
            )
        footer = _format_usage_footer(final_usage, session.model)
        if footer:
            final = f"{final}\n\n{footer}"
        options = _extract_options(final) or ask_options
        card_patched = False
        try:
            if options:
                buttons = [
                    {"text": display, "value": {"reply": value, "cid": chat_id, "profile": bot.profile.name}}
                    for display, value in options
                ]
                short = all(len(b["text"]) <= 10 for b in buttons)
                await bot.feishu.update_card_with_buttons(card_msg_id, final, buttons, flow=short)
            else:
                await bot.feishu.update_card(card_msg_id, final)
            card_patched = True
        except Exception as e:
            log(bot.profile.name, "card", "error", f"卡片更新失败，回退发文本: {e}")
            try:
                if is_group and notify_msg_id:
                    await bot.feishu.reply_card(notify_msg_id, content=final, loading=False)
                else:
                    await bot.feishu.send_text_to_user(user_id, final)
            except Exception as fallback_err:
                log(bot.profile.name, "card", "error", f"文本回退也失败: {fallback_err}")

        if card_patched:
            try:
                if is_group and notify_msg_id:
                    await bot.feishu.reply_text(notify_msg_id, "✅")
                else:
                    await bot.feishu.send_text_to_user(user_id, "✅")
            except Exception:
                pass

        if new_session_id:
            await bot.store.on_claude_response(
                user_id, chat_id, new_session_id, preview_text or text,
            )

        if plan_exited and session.permission_mode == "plan":
            log(bot.profile.name, "plan", "info", "ExitPlanMode 检测到，切换为 bypassPermissions")
            await bot.store.set_permission_mode(user_id, chat_id, "bypassPermissions")
            try:
                notice = "🚀 已退出规划模式，发送任意消息开始执行。"
                if is_group and notify_msg_id:
                    await bot.feishu.reply_text(notify_msg_id, notice)
                else:
                    await bot.feishu.send_text_to_user(user_id, notice)
            except Exception:
                pass
    finally:
        bot.active_runs.clear_run(user_id, chat_id, active_run)


async def _process_message(
    bot: BotInstance, user_id: str, chat_id: str, is_group: bool, thread_id: str, msg,
    trinity_ctx: Optional[TrinityContext] = None,
):
    """实际处理消息的逻辑，在 per-chat lock 保护下执行"""
    tag = bot.profile.name
    log(tag, "process", "info",
        f"user={user_id[:8]}... chat={chat_id[:10]}... "
        f"thread={thread_id[:10] if thread_id else '-'} is_group={is_group}")
    text = ""
    preview_text = ""

    if msg.message_type == "text":
        try:
            text = json.loads(msg.content).get("text", "").strip()
        except Exception:
            return
        if not text:
            return

        if is_group:
            mentions = getattr(msg, 'mentions', None) or []
            for mention in mentions:
                key = getattr(mention, 'key', '')
                if key:
                    text = text.replace(key, '').strip()
            if not text and not thread_id:
                return

        preview_text = text
        log(tag, "text", "info", f"{text[:50] if text else '(空)'}")

    elif msg.message_type == "image":
        try:
            image_key = json.loads(msg.content).get("image_key", "")
            if not image_key:
                return
            img_path = await bot.feishu.download_image(msg.message_id, image_key)
            text = f"[用户发送了一张图片，路径：{img_path}，请读取并分析这张图片，直接回复用中文]"
            preview_text = "[图片]"
        except Exception as e:
            log(tag, "image", "error", f"下载图片失败: {e}")
            if is_group:
                try:
                    await bot.feishu.reply_card(msg.message_id, content=f"❌ 下载图片失败：{e}", loading=False)
                except Exception:
                    pass
            else:
                await bot.feishu.send_text_to_user(user_id, f"❌ 下载图片失败：{e}")
            return

    elif msg.message_type == "file":
        try:
            content_obj = json.loads(msg.content)
            file_key = content_obj.get("file_key", "")
            file_name = content_obj.get("file_name", "") or "file"
            if not file_key:
                return
            fpath = await bot.feishu.download_file(
                msg.message_id, file_key, msg_type="file", file_name=file_name,
            )
            text = (
                f"[用户发送了文件：{file_name}，本地路径：{fpath}。"
                f"请根据需要读取该文件并分析，用中文回复。]"
            )
            preview_text = f"[文件 {file_name}]"
        except Exception as e:
            log(tag, "file", "error", f"下载文件失败: {e}")
            if is_group:
                try:
                    await bot.feishu.reply_card(msg.message_id, content=f"❌ 下载文件失败：{e}", loading=False)
                except Exception:
                    pass
            else:
                await bot.feishu.send_text_to_user(user_id, f"❌ 下载文件失败：{e}")
            return

    elif msg.message_type == "post":
        post_text = parse_post_content(msg.content).strip()
        image_keys = extract_post_image_keys(msg.content)

        if is_group:
            for mention in (getattr(msg, 'mentions', None) or []):
                key = getattr(mention, 'key', '')
                if key:
                    post_text = post_text.replace(key, '').strip()

        img_paths: list[str] = []
        for ik in image_keys:
            try:
                path = await bot.feishu.download_image(msg.message_id, ik)
                img_paths.append(path)
            except Exception as e:
                log(tag, "post", "warn", f"下载 post 图片失败 key={ik[:8]}...: {e}")

        if not post_text and not img_paths:
            log(tag, "post", "info", "空内容，忽略")
            return

        if img_paths:
            paths_list = "\n".join(f"  - {p}" for p in img_paths)
            caption = post_text or "（无文字说明）"
            text = (
                f"[用户发送了富文本消息，含 {len(img_paths)} 张图片]\n"
                f"文字内容：{caption}\n"
                f"图片路径：\n{paths_list}\n"
                f"请读取并分析这些图片，结合文字回复（中文）。"
            )
            preview_text = post_text[:40] if post_text else f"[富文本 + {len(img_paths)} 图]"
        else:
            text = post_text
            preview_text = post_text

        log(tag, "post", "info", f"text_len={len(post_text)} imgs={len(img_paths)}")

    else:
        return

    # ── 斜杠命令 ──────────────────────────────────────────────
    parsed = parse_command(text)
    if parsed:
        cmd, args = parsed
        log(tag, "cmd", "info", f"执行 {cmd}")
        reply = await handle_command(cmd, args, user_id, chat_id, bot.store)
        if reply is not None:
            if isinstance(reply, dict):
                reply_text, reply_buttons = reply["text"], reply.get("buttons", [])
            else:
                reply_text, reply_buttons = reply, []

            for btn in reply_buttons:
                val = btn.get("value")
                if isinstance(val, dict):
                    val.setdefault("profile", bot.profile.name)

            if reply_buttons:
                if is_group:
                    card_id = await bot.feishu.reply_card(msg.message_id, content=reply_text, loading=False)
                else:
                    card_id = await bot.feishu.send_card_to_user(user_id, content=reply_text, loading=False)
                try:
                    short = all(len(b["text"]) <= 12 for b in reply_buttons)
                    await bot.feishu.update_card_with_buttons(card_id, reply_text, reply_buttons, flow=short)
                except Exception as btn_err:
                    log(tag, "btn", "warn", f"按钮渲染失败: {btn_err}")
            else:
                if is_group:
                    await bot.feishu.reply_card(msg.message_id, content=reply_text, loading=False)
                else:
                    await bot.feishu.send_card_to_user(user_id, content=reply_text, loading=False)
            return

    # ── 普通消息 → 调用 Claude ──────────────────────────────
    session = await bot.store.get_current(user_id, chat_id)
    log(tag, "claude", "info", f"session={session.session_id} model={session.model}")

    if thread_id:
        try:
            last_seen = await bot.store.get_last_seen(user_id, chat_id)
            context_block, ctx_paths = await build_thread_context(
                bot.feishu, thread_id, last_seen, msg.message_id,
            )
            if context_block:
                log(tag, "thread", "info",
                    f"注入上下文 last_seen={last_seen[:12] if last_seen else '-'}, "
                    f"附件={len(ctx_paths)}")
                if text.strip():
                    text = f"{context_block}\n\n【用户刚刚 @ 你并说】\n{text}"
                else:
                    text = f"{context_block}\n\n【用户刚刚 @ 你，没有新正文，请基于上方内容回复】"
            await bot.store.set_last_seen(user_id, chat_id, msg.message_id)
        except Exception as e:
            log(tag, "thread", "warn", f"构建上下文失败（继续处理当前消息）: {e}")

    if not text.strip():
        try:
            await bot.feishu.reply_card(
                msg.message_id,
                content="ℹ️ 只 @ 了我但没有正文，也没有新的话题消息可读。请补一句说明你想做什么。",
                loading=False,
            )
        except Exception:
            pass
        return

    try:
        if is_group:
            card_msg_id = await bot.feishu.reply_card(msg.message_id, loading=True)
        else:
            card_msg_id = await bot.feishu.send_card_to_user(user_id, loading=True)
    except Exception as e:
        log(tag, "card", "error", f"发送占位卡片失败: {e}")
        if is_group:
            try:
                await bot.feishu.reply_card(msg.message_id, content=f"❌ 发送消息失败：{e}", loading=False)
            except Exception:
                pass
        else:
            await bot.feishu.send_text_to_user(user_id, f"❌ 发送消息失败：{e}")
        return

    raw_chat_id = chat_id.split(":", 1)[0] if ":" in chat_id else chat_id
    lark_sys = build_lark_system_prompt(
        bot.profile, raw_chat_id, thread_id, msg.message_id, is_group,
        asker_open_id=user_id, trinity_ctx=trinity_ctx,
    )

    await _run_and_display(
        bot,
        user_id, chat_id, is_group, text, card_msg_id, session, msg.message_id,
        preview_text=preview_text,
        append_system_prompt=lark_sys,
    )


# ── Lark 系统提示词（薄包装到 lark_prompts.render_lark_prompt） ──

def _context_window_for(model: str) -> int:
    m = (model or "").lower()
    if "[1m]" in m or "1m" in m:
        return 1_000_000
    return 200_000


def build_lark_system_prompt(
    profile: Profile,
    raw_chat_id: str,
    thread_id: str,
    user_message_id: str,
    is_group: bool,
    asker_open_id: str = "",
    trinity_ctx: Optional[TrinityContext] = None,
) -> str:
    """构造注入到 Claude 的 Lark 语境系统提示。模板见 prompts/。"""
    ticket_state = trinity_ctx.new_state.value if trinity_ctx else None
    ticket_id = trinity_ctx.ticket.ticket_id if trinity_ctx else ""
    ticket_history = trinity_ctx.history_text if trinity_ctx else ""
    return render_lark_prompt(
        profile,
        raw_chat_id=raw_chat_id,
        thread_id=thread_id,
        user_message_id=user_message_id,
        is_group=is_group,
        asker_open_id=asker_open_id,
        ticket_state=ticket_state,
        ticket_id=ticket_id,
        ticket_history=ticket_history,
    )


def _format_usage_footer(usage: dict, model: str) -> str:
    if not usage:
        return ""
    input_tok = int(usage.get("input_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_create = int(usage.get("cache_creation_input_tokens", 0) or 0)
    output_tok = int(usage.get("output_tokens", 0) or 0)
    total_context = input_tok + cache_read + cache_create + output_tok
    if total_context <= 0:
        return ""
    window = int(usage.get("_context_window") or 0) or _context_window_for(model)
    pct = total_context / window * 100

    def fmt(n: int) -> str:
        if n >= 1_000_000:
            s = f"{n/1_000_000:.1f}".rstrip("0").rstrip(".")
            return f"{s}M"
        if n >= 1000:
            s = f"{n/1000:.1f}".rstrip("0").rstrip(".")
            return f"{s}k"
        return str(n)

    return f"— 📊 上下文 {fmt(total_context)} / {fmt(window)} ({pct:.1f}%)"


def _extract_options(text: str) -> list[tuple[str, str]]:
    """从文本中提取选项，适配 Claude Code 原生输出格式。"""
    lines = text.strip().split('\n')

    option_lines = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            if option_lines:
                break
            continue
        m = re.match(r'^(\d+|[a-zA-Z])[.）\)、]\s*(.+)', line)
        if m:
            option_lines.append((m.group(1), m.group(2).strip()))
        elif option_lines:
            break
        else:
            break
    option_lines.reverse()
    if len(option_lines) >= 2:
        return [
            (f"{key}. {desc}" if len(desc) <= 18 else f"{key}. {desc[:16]}..", key)
            for key, desc in option_lines
        ]

    tail = "\n".join(lines[-3:]) if len(lines) >= 3 else text
    if re.search(r'\by\b.*\bn\b|Y/N|yes.*no|是/否|确认/取消', tail, re.IGNORECASE):
        return [("Yes", "yes"), ("No", "no")]

    return []


def _format_tool(name: str, inp: dict) -> str:
    n = name.lower()
    if n == "bash":
        cmd = inp.get("command", "")
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        return f"🔧 **执行命令：** `{cmd}`" if cmd else f"🔧 **执行命令...**"
    elif n in ("read_file", "read"):
        return f"📄 **读取：** `{inp.get('file_path', inp.get('path', ''))}`"
    elif n in ("write_file", "write"):
        return f"✏️ **写入：** `{inp.get('file_path', inp.get('path', ''))}`"
    elif n in ("edit_file", "edit"):
        return f"✂️ **编辑：** `{inp.get('file_path', inp.get('path', ''))}`"
    elif n in ("glob",):
        return f"🔍 **搜索文件：** `{inp.get('pattern', '')}`"
    elif n in ("grep",):
        return f"🔎 **搜索内容：** `{inp.get('pattern', '')}`"
    elif n == "task":
        return f"🤖 **子任务：** {inp.get('description', inp.get('prompt', '')[:40])}"
    elif n == "webfetch":
        return f"🌐 **抓取网页...**"
    elif n == "websearch":
        return f"🔍 **搜索：** {inp.get('query', '')}"
    else:
        return f"⚙️ **{name}**"


# ── 卡片按钮点击处理 ─────────────────────────────────────────

def _resolve_bot_from_value(value: dict) -> Optional[BotInstance]:
    """从按钮 value.profile 取 BotInstance；兼容老卡片（无 profile 字段）时取第一个 profile。"""
    name = value.get("profile") if isinstance(value, dict) else None
    if name and name in _bots:
        return _bots[name]
    if len(_bots) == 1:
        return next(iter(_bots.values()))
    return None


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """用户点击卡片按钮（SDK 长连接回调路径）"""
    # 通知 runtime 刷新最近活动时间
    try:
        from runtime import touch_event
        touch_event()
    except Exception:
        pass

    event = data.event
    user_id = event.operator.open_id
    value = event.action.value or {}
    action_type = value.get("action", "")
    chat_id = value.get("cid", user_id)
    clicked_msg_id = event.context.open_message_id if event.context else None

    bot = _resolve_bot_from_value(value)
    if bot is None:
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "warning"
        toast.content = "按钮已过期，请重新操作"
        resp.toast = toast
        return resp

    if action_type == "set_mode":
        mode = value.get("mode", "")
        if mode:
            asyncio.run_coroutine_threadsafe(
                handle_set_mode(bot, user_id, chat_id, mode, clicked_msg_id), _bot_loop)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "success"
        toast.content = f"已切换: {mode}"
        resp.toast = toast
        return resp

    if action_type == "run_cmd":
        cmd_text = value.get("cmd", "")
        if cmd_text:
            asyncio.run_coroutine_threadsafe(
                handle_menu_command(bot, user_id, chat_id, cmd_text, clicked_msg_id), _bot_loop)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = cmd_text
        resp.toast = toast
        return resp

    if action_type == "resume_session":
        sid = value.get("sid", "")
        if sid:
            asyncio.run_coroutine_threadsafe(
                handle_resume_session(bot, user_id, chat_id, sid, clicked_msg_id), _bot_loop)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = "正在恢复..."
        resp.toast = toast
        return resp

    reply_text = value.get("reply", "")
    if reply_text:
        asyncio.run_coroutine_threadsafe(
            handle_button_reply(bot, user_id, chat_id, reply_text, clicked_msg_id), _bot_loop)

    resp = P2CardActionTriggerResponse()
    toast = CallBackToast()
    toast.type = "info"
    toast.content = f"已发送: {reply_text}"
    resp.toast = toast
    return resp


async def handle_menu_command(bot: BotInstance, user_id: str, chat_id: str, cmd_text: str, card_msg_id: str):
    is_group = (chat_id != user_id)
    parsed = parse_command(cmd_text)
    if not parsed:
        return
    cmd, args = parsed

    if cmd == "stop":
        reply_text = await _handle_stop_command(bot, user_id, chat_id)
        if card_msg_id:
            try:
                await bot.feishu.update_card(card_msg_id, reply_text)
            except Exception:
                pass
        return

    reply = await handle_command(cmd, args, user_id, chat_id, bot.store)
    if reply is None:
        return

    if isinstance(reply, dict):
        reply_text, reply_buttons = reply["text"], reply.get("buttons", [])
    else:
        reply_text, reply_buttons = reply, []

    for btn in reply_buttons:
        val = btn.get("value")
        if isinstance(val, dict):
            val.setdefault("profile", bot.profile.name)

    if card_msg_id:
        try:
            if reply_buttons:
                short = all(len(b["text"]) <= 12 for b in reply_buttons)
                await bot.feishu.update_card_with_buttons(card_msg_id, reply_text, reply_buttons, flow=short)
            else:
                await bot.feishu.update_card(card_msg_id, reply_text)
        except Exception as e:
            log(bot.profile.name, "menu", "error", f"菜单命令卡片更新失败: {e}")


async def handle_resume_session(bot: BotInstance, user_id: str, chat_id: str, session_id: str, card_msg_id: str):
    sid, old_title = await bot.store.resume_session(user_id, chat_id, session_id)
    if not sid:
        log(bot.profile.name, "resume", "warn", f"未找到 session: {session_id[:8]}")
        return
    log(bot.profile.name, "resume", "info", f"已恢复 session: {sid[:8]}")
    if card_msg_id:
        try:
            name = bot.store.get_summary(user_id, sid) or f"#{sid[:8]}"
            text = f"✅ 已恢复会话「{name}」，继续对话吧。"
            if old_title:
                text += f"\n上个会话：「{old_title}」"
            await bot.feishu.update_card(card_msg_id, text)
        except Exception:
            pass


async def handle_set_mode(bot: BotInstance, user_id: str, chat_id: str, mode: str, card_msg_id: str):
    from commands import VALID_MODES
    await bot.store.set_permission_mode(user_id, chat_id, mode)
    desc = VALID_MODES.get(mode, "")
    log(bot.profile.name, "mode", "info", f"user={user_id[:8]}... mode={mode}")
    if card_msg_id:
        try:
            await bot.feishu.update_card(card_msg_id, f"✅ 已切换为 **{mode}**\n{desc}")
        except Exception:
            pass


async def handle_button_reply(bot: BotInstance, user_id: str, chat_id: str, text: str, clicked_msg_id: str):
    is_group = (chat_id != user_id)
    lock = bot._ensure_chat_lock(chat_id)

    if lock.locked() and clicked_msg_id:
        try:
            await bot.feishu.reply_text(clicked_msg_id, "📬 前面还有任务在跑，排队中（/stop 可打断）")
        except Exception:
            pass

    async with lock:
        try:
            session = await bot.store.get_current(user_id, chat_id)
            try:
                if is_group and clicked_msg_id:
                    card_msg_id = await bot.feishu.reply_card(clicked_msg_id, loading=True)
                else:
                    card_msg_id = await bot.feishu.send_card_to_user(user_id, loading=True)
            except Exception as e:
                log(bot.profile.name, "btn", "error", f"按钮回复占位卡片失败: {e}")
                return
            raw_chat_id, _, btn_thread_id = chat_id.partition(":")
            lark_sys = build_lark_system_prompt(
                bot.profile, raw_chat_id, btn_thread_id, clicked_msg_id or "", is_group,
                asker_open_id=user_id,
            )
            await _run_and_display(
                bot,
                user_id, chat_id, is_group, text,
                card_msg_id, session, clicked_msg_id or "",
                append_system_prompt=lark_sys,
            )
        except Exception as e:
            log(bot.profile.name, "btn", "error", f"按钮回复处理异常: {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stdout)


# ── CLI Handover ─────────────────────────────────────────────

async def handle_handover(
    session_id: str, cwd: str, model: str,
    profile_name: str = "", target_user: str = "", target_chat: str = "",
) -> dict:
    """处理来自 CLI 的 handover 请求。profile_name 为空时自动选第一个。"""
    bot: Optional[BotInstance] = None
    if profile_name:
        bot = _bots.get(profile_name)
    if bot is None:
        if not _bots:
            return {"ok": False, "error": "no bot profile loaded"}
        bot = next(iter(_bots.values()))

    user_id = target_user or bot.store.find_primary_user()
    if not user_id:
        return {"ok": False, "error": f"no user found in profile {bot.profile.name}, pass user_id param"}

    chat_id = target_chat or user_id

    result = await bot.store.handover_session(user_id, chat_id, session_id, cwd=cwd, model=model)

    cur = await bot.store.get_current_raw(user_id, chat_id)
    display_cwd = cur.get("cwd", "~")
    display_model = cur.get("model", "unknown")
    display_mode = cur.get("permission_mode", "bypassPermissions")
    old_summary = result.get("old_summary", "")
    old_note = f"\n上个会话：「{old_summary}」" if old_summary else ""

    notify_text = (
        f"**CLI 会话已接入**（profile `{bot.profile.name}`）\n"
        f"Session: `{session_id[:12]}...`\n"
        f"目录: `{display_cwd}`\n"
        f"模型: `{display_model}`\n"
        f"模式: `{display_mode}`{old_note}\n\n"
        f"直接发消息即可继续对话。"
    )

    try:
        await bot.feishu.send_card_to_user(user_id, content=notify_text, loading=False)
    except Exception as e:
        log(bot.profile.name, "handover", "warn", f"推送通知失败: {e}")

    log(bot.profile.name, "handover", "info",
        f"session={session_id[:8]}... cwd={display_cwd}")
    return {"ok": True, "profile": bot.profile.name, "user_id": user_id, "session_id": session_id}


# ── /spawn：在指定 (user, chat:thread) 起一条全新 session ────

async def handle_spawn(
    bot: BotInstance,
    user_id: str,
    chat_id_raw: str,
    thread_id: str,
    anchor_message_id: str,
    prompt: str,
):
    """在 (user, chat_id_raw:thread_id) 这一格强制开新 session 跑 prompt。

    设计场景：大群里的"调度 session"读完上下文后，用 lark-cli 在会话群创建新话题，
    再 curl 这个端点把后续工作派给独立 session。和 WS 路径不冲突——这是绕过 WS 的
    内部触发入口。
    """
    tag = bot.profile.name

    # 兜底：dispatcher Claude 偶尔会把 anchor 的 message_id (om_xxx) 当成 thread_id 传过来。
    # 这样起的 session chat_key 是 oc_xxx:om_xxx，跟用户后续在该话题 @ bot 时构造的
    # oc_xxx:omt_xxx 不匹配，session 就丢了。识别出 om_xxx 形式 → 主动 mget 转成真 thread_id。
    if thread_id.startswith("om_") and not thread_id.startswith("omt_"):
        try:
            actual = await bot.feishu.get_message_thread_id(thread_id)
        except Exception as e:
            actual = ""
            log(tag, "spawn", "warn", f"mget thread_id 失败 {thread_id[:14]}...: {e}")
        if actual and actual.startswith("omt_"):
            log(tag, "spawn", "info",
                f"自动修正 thread_id: {thread_id[:14]}... → {actual[:14]}...")
            thread_id = actual
        else:
            log(tag, "spawn", "warn",
                f"拒绝：thread_id={thread_id[:14]}... 既非 omt_ 也无法 mget 成 omt_")
            try:
                await bot.feishu.reply_text(
                    anchor_message_id,
                    f"⚠️ /spawn 收到的 thread_id 不是 omt_ 形式且无法转换（{thread_id[:14]}...）。"
                    f"请检查派单代码是否把 message_id 误传成了 thread_id。",
                )
            except Exception:
                pass
            return

    chat_id = f"{chat_id_raw}:{thread_id}"

    lock = bot._ensure_chat_lock(chat_id)
    if lock.locked():
        try:
            await bot.feishu.reply_text(
                anchor_message_id,
                "⚠️ 这条话题里已有任务在跑，spawn 已忽略",
            )
        except Exception:
            pass
        log(tag, "spawn", "warn",
            f"拒绝：目标话题忙 chat={chat_id_raw[:10]}... thread={thread_id[:10]}...")
        return

    async with lock:
        try:
            await bot.store.new_session(user_id, chat_id)
            session = await bot.store.get_current(user_id, chat_id)

            try:
                card_msg_id = await bot.feishu.reply_card(anchor_message_id, loading=True)
            except Exception as e:
                log(tag, "spawn", "error", f"占位卡片发送失败: {e}")
                try:
                    await bot.feishu.reply_text(anchor_message_id, f"❌ spawn 失败：{e}")
                except Exception:
                    pass
                return

            lark_sys = build_lark_system_prompt(
                bot.profile, chat_id_raw, thread_id, anchor_message_id, is_group=True,
                asker_open_id=user_id,
            )

            log(tag, "spawn", "info",
                f"user={user_id[:8]}... chat={chat_id_raw[:10]}... "
                f"thread={thread_id[:10]}... anchor={anchor_message_id[:12]}... "
                f"prompt_len={len(prompt)}")

            await _run_and_display(
                bot,
                user_id, chat_id, True, prompt,
                card_msg_id, session, anchor_message_id,
                preview_text=prompt[:40],
                append_system_prompt=lark_sys,
            )
        except Exception as e:
            log(tag, "spawn", "error", f"异常: {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()
