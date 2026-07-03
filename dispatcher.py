"""消息分发 + 业务核心。

从 WS 收到一条 Lark 消息，到向 Claude 发请求、把流式输出推回飞书，
全部逻辑都在这里。也包括卡片按钮回调、/spawn、/handover、菜单命令等业务路径。

不直接持有 bot_loop / bots 的全局；通过 configure() 注入，避免与 main.py
循环依赖。
"""

from __future__ import annotations

import asyncio
import json
import os
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
from agent_runner import run_agent
from commands import parse_command, handle_command
from feishu_post import parse_post_content, extract_post_image_keys, strip_lark_mentions
from lark_prompts import render_lark_prompt
from passthrough import is_builtin_passthrough
from log_util import log
from run_control import ActiveRun, stop_run
from thread_context import build_thread_context
from trinity_dispatch import maybe_handle_trinity, TrinityContext
import inbox_watcher


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


# ── 事件去重 ─────────────────────────────────────────────────
# Lark WS 是 at-least-once：keepalive ping timeout / connection reset 后重连
# 服务端会把未 ack 的 receive_v1 再投一次。同一条 om_ 不能跑两遍 claude，
# 否则用户看到的就是"一条消息收到两次回复"（实际案例：2026-05-22 16:43，
# 16:43:52 WS 断开 → 16:43:59 同事件复推 → 两次完整翻译 + 一次"排队中"）。
#
# 实现：module-level dict (dedupe_key -> first_seen_ts)。所有 handler 都跑在
# 同一个 bot_loop 上（runtime.py:_on_message 用 run_coroutine_threadsafe 投递），
# 单线程访问，不需要锁。TTL 120s 足够覆盖 Lark 重连窗口，又不会无界增长。
_SEEN_MSG_TTL_SEC = 120
_seen_messages: dict[str, float] = {}


def _is_duplicate_event(message_id: str, profile_name: str = "") -> bool:
    """同一 profile 的同一 message_id 在 TTL 内重复送达返回 True。"""
    if not message_id:
        return False
    dedupe_key = f"{profile_name}:{message_id}" if profile_name else message_id
    now = time.time()
    if _seen_messages:
        expired = [k for k, t in _seen_messages.items() if now - t > _SEEN_MSG_TTL_SEC]
        for k in expired:
            _seen_messages.pop(k, None)
    if dedupe_key in _seen_messages:
        return True
    _seen_messages[dedupe_key] = now
    return False


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


async def _is_current_bot_mentioned(bot: BotInstance, msg) -> bool:
    """Return True when a group message explicitly mentions this bot."""
    mentions = getattr(msg, "mentions", None) or []
    if not mentions:
        return False
    bot_open_id = await bot.feishu.get_bot_open_id()
    if not bot_open_id:
        return False
    return any(
        getattr(getattr(m, "id", None), "open_id", "") == bot_open_id
        for m in mentions
    )


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


async def _handle_restart_command(originating_bot: BotInstance) -> int:
    """
    /restart：跨所有 bot 对每个 active run 调 stop_run（terminate PTY → 等子
    进程退出 → on_stopped 改卡片），保证我们的"♻️ 重启"中断消息不被并发的
    push() 流回覆盖。返回受影响数量。调用方负责回 ack + 触发 detach。
    """
    RESTART_MSG = "♻️ cc-lark 服务正在重启 — 本次任务被中断，~5s 后再发一遍。"

    async def _stop_one(b: BotInstance, prof_name: str, run):
        async def _announce(r):
            if not r.card_msg_id:
                return
            try:
                await b.feishu.update_card(r.card_msg_id, RESTART_MSG)
            except Exception as e:
                log(prof_name, "restart", "warn",
                    f"update_card 失败 chat={r.chat_id[:12]}: {e}")
        try:
            await stop_run(
                b.active_runs, run.user_id, run.chat_id,
                on_stopped=_announce, grace_seconds=1.5,
            )
        except Exception as e:
            log(prof_name, "restart", "warn",
                f"stop_run 失败 chat={run.chat_id[:12]}: {e}")

    tasks = []
    affected = 0
    for prof_name, b in list(_bots.items()):
        for run in list(b.active_runs._runs.values()):
            affected += 1
            tasks.append(_stop_one(b, prof_name, run))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    log(originating_bot.profile.name, "restart", "info",
        f"广播完毕（{affected} 个 active run），触发 detach + exit")
    return affected


# ── /verify：审计当前 thread ─────────────────────────────────

_VERIFY_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "prompts", "verify.md",
)
_verify_template_cache: Optional[str] = None


def _load_verify_template() -> str:
    """读 prompts/verify.md，简单内存缓存（模板基本不会改）"""
    global _verify_template_cache
    if _verify_template_cache is None:
        with open(_VERIFY_TEMPLATE_PATH, encoding="utf-8") as f:
            _verify_template_cache = f.read()
    return _verify_template_cache


def _thread_ctx_error_hint(err: str) -> str:
    """话题历史拉取失败时给用户的兜底提示。

    识别权限类错误（230027 / 缺 scope）给出可操作建议，否则原样带上原因。
    目的：避免新 bot 因读不到历史而"看起来很蠢"——明确告诉用户是读不到，
    不是没内容。
    """
    e = (err or "").lower()
    if "230027" in e or "scope" in e or "permission" in e:
        return (
            "⚠️ 我读不到这个话题的历史消息——本应用缺少群消息读取权限 "
            "`im:message.group_msg`。请到 Lark 开发者后台给本应用加上该权限并发布新版本。\n"
            "在那之前，请把要我做的事**直接贴在消息里**，我就能处理。"
        )
    return (
        f"⚠️ 我读不到这个话题的历史消息（拉取失败：{err}）。"
        "请把要我做的事直接贴在消息里。"
    )


async def _handle_verify_command(
    bot: BotInstance,
    user_id: str,
    chat_id: str,
    is_group: bool,
    thread_id: str,
    msg,
    args: str,
):
    """/verify [关注点] — 在话题群里开新 session 审上方整段对话。

    流程：
      1. 校验必须在话题群（有 thread_id），否则一句话报错。
      2. 拉整个 thread 的消息（build_thread_context 已经默认保留 bot 消息和附件）。
      3. new_session + 强制 bypassPermissions（验证完可直接动手改）。
      4. 拼 prompt = prompts/verify.md 替换 ${focus} ${history}。
      5. 走 _run_and_display 流式审计。
    """
    tag = bot.profile.name

    if not is_group or not thread_id:
        try:
            await bot.feishu.reply_card(
                msg.message_id,
                content="⚠️ `/verify` 只支持话题群里使用 —— 私聊 / 非话题群拉不到完整对话历史。",
                loading=False,
            )
        except Exception:
            pass
        return

    # 拉整个 thread（last_seen_message_id="" → 全量从头；current_message_id=本条 /verify
    # 本身会被跳过，避免审计指令进入审计对象）
    try:
        context_block, ctx_paths, ctx_err = await build_thread_context(
            bot.feishu, thread_id, "", msg.message_id,
        )
    except Exception as e:
        log(tag, "verify", "error", f"拉 thread 失败: {e}")
        try:
            await bot.feishu.reply_card(
                msg.message_id, content=f"❌ 拉 thread 失败：{e}", loading=False,
            )
        except Exception:
            pass
        return

    if ctx_err:
        log(tag, "verify", "error", f"拉 thread 历史失败（缺权限？）: {ctx_err}")
        try:
            await bot.feishu.reply_card(
                msg.message_id, content=_thread_ctx_error_hint(ctx_err), loading=False,
            )
        except Exception:
            pass
        return

    if not context_block:
        try:
            await bot.feishu.reply_card(
                msg.message_id,
                content="⚠️ 这个 thread 里没有可审计的历史消息。",
                loading=False,
            )
        except Exception:
            pass
        return

    # 新 session + bypassPermissions（验证后可直接动手）
    await bot.store.new_session(user_id, chat_id)
    await bot.store.set_permission_mode(user_id, chat_id, "bypassPermissions")
    session = await bot.store.get_current(user_id, chat_id)

    focus = args.strip() if args.strip() else "（无指定，全面审）"
    template = _load_verify_template()
    user_msg = template.replace("${focus}", focus).replace("${history}", context_block)

    log(tag, "verify", "info",
        f"thread={thread_id[:12]}... history_len={len(context_block)} "
        f"attachments={len(ctx_paths)} focus={focus[:30]!r}")

    try:
        card_msg_id = await bot.feishu.reply_card(msg.message_id, loading=True)
    except Exception as e:
        log(tag, "verify", "error", f"占位卡片失败: {e}")
        try:
            await bot.feishu.reply_text(msg.message_id, f"❌ 发送占位卡片失败：{e}")
        except Exception:
            pass
        return

    raw_chat_id = chat_id.split(":", 1)[0] if ":" in chat_id else chat_id
    lark_sys = build_lark_system_prompt(
        bot.profile, raw_chat_id, thread_id, msg.message_id, is_group=True,
        asker_open_id=user_id, runner=session.runner,
    )

    await _run_and_display(
        bot,
        user_id, chat_id, True, user_msg,
        card_msg_id, session, msg.message_id,
        preview_text="/verify",
        append_system_prompt=lark_sys,
    )


# ── Hung 自动重试黑名单 ────────────────────────────────────
# 命中即跳过 auto-retry：这些是写操作（开卡 / 开户 / 扣费类），上一轮可能已经
# commit 了一半，重试会 double-write。user text + tool_history 任一命中即黑名单。
# 部署相关的 skill / endpoint 标记放 env CC_LARK_WRITE_OP_MARKERS（逗号分隔），
# 不进代码库；未设置则为空（通用框架默认无写操作黑名单）。
def _write_op_markers() -> tuple[str, ...]:
    raw = os.environ.get("CC_LARK_WRITE_OP_MARKERS", "")
    return tuple(m.strip() for m in raw.split(",") if m.strip())


def _is_write_op_context(user_text: str, tool_history: list[str]) -> bool:
    blob = (user_text or "") + "\n" + "\n".join(tool_history or [])
    return any(m in blob for m in _write_op_markers())


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

    # ── WS at-least-once 去重：Lark WS 断线重连会把同一条 om_ 重投一次，
    # 直接当新消息处理会跑两遍 claude。在最前面（inbox / trinity / ACL / lock 之前）
    # 拦掉，重复事件完全无副作用。
    if _is_duplicate_event(msg.message_id, tag):
        log(tag, "msg", "info",
            f"重复事件忽略 mid={msg.message_id[:14]}... "
            f"(WS at-least-once redelivery)")
        return

    # ── Inbox 旁路 hook（非阻塞）：把源群消息喂给 inbox_watcher 做派单判定。
    # 故意放在 trinity / ACL 之前 — 源群里别的人发的消息可能不在 allowlist，
    # 但 inbox 要看到。inbox_watcher.observe 内部静默 / 非阻塞，不影响主路径。
    try:
        inbox_watcher.observe(bot, event)
    except Exception as e:
        log(tag, "inbox", "warn", f"inbox observe 失败（已忽略）: {e}")

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
            _text = strip_lark_mentions(_text, getattr(msg, 'mentions', None))

        # 精确匹配（此前 endswith 会让"怎么让你 /stop"这类普通句子误触发）
        if _text.strip().lower() == "/stop":
            if is_group and not await _is_current_bot_mentioned(bot, msg):
                return
            reply = await _handle_stop_command(bot, user_id, chat_id)
            if is_group:
                await bot.feishu.reply_card(msg.message_id, content=reply, loading=False)
            else:
                await bot.feishu.send_card_to_user(user_id, content=reply, loading=False)
            return

        if _text.strip().lower() == "/restart":
            if is_group and not await _is_current_bot_mentioned(bot, msg):
                return
            from commands import _trigger_restart, restart_strategy
            strat = restart_strategy()
            if strat == "bare":
                ack = ("❌ 没找到 supervisor（非 launchd 任务、无 .app），"
                       "直接退出会停服，已取消重启。")
                try:
                    if is_group:
                        await bot.feishu.reply_card(msg.message_id, content=ack, loading=False)
                    else:
                        await bot.feishu.send_card_to_user(user_id, content=ack, loading=False)
                except Exception:
                    pass
                return
            affected = await _handle_restart_command(bot)
            via = "launchd kickstart" if strat == "launchd" else "open .app"
            ack = (f"♻️ 服务重启中（通知了 {affected} 个进行中的会话）— "
                   f"{via}，~3-5s 后回来。")
            try:
                if is_group:
                    await bot.feishu.reply_card(msg.message_id, content=ack, loading=False)
                else:
                    await bot.feishu.send_card_to_user(user_id, content=ack, loading=False)
            except Exception:
                pass
            _trigger_restart()
            return

        if _text == "/":
            if is_group and not await _is_current_bot_mentioned(bot, msg):
                return
            await _show_command_menu(bot, user_id, chat_id, is_group, msg.message_id)
            return

    # 群聊只响应 @机器人 的消息。
    # 例外：语音消息没法 @ 人，在已有会话记录的话题 thread 里直接放行
    # （新 thread 首条消息仍要求文字 @，和 text 行为一致）。
    if is_group:
        if not await _is_current_bot_mentioned(bot, msg):
            if not (
                msg.message_type == "audio"
                and thread_id
                and bot.store.has_chat_record(user_id, chat_id)
            ):
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
            # _process_message 可能在建卡片之前就抛了（路由层 / store / 权限检查等），
            # 此时没有 card_msg_id / notify_msg_id 可改。直接 reply 用户原消息，让出错可见。
            try:
                err_text = f"❌ 异常退出：{type(e).__name__}: {e}"
                if is_group:
                    await bot.feishu.reply_text(msg.message_id, err_text)
                else:
                    await bot.feishu.send_text_to_user(user_id, err_text)
            except Exception:
                pass


async def _run_and_display(
    bot: BotInstance,
    user_id: str, chat_id: str, is_group: bool,
    text: str, card_msg_id: str, session, notify_msg_id: str,
    preview_text: str = "",
    append_system_prompt: str = "",
):
    """调用 Claude 并流式展示结果。消息处理和按钮回复共用此函数。"""
    active_run = bot.active_runs.start_run(user_id, chat_id, card_msg_id)

    # cc-lark MCP 的会话上下文。透传给 run_agent → claude 的 extra_env，MCP
    # server 用这些默认值把 send_text / schedule_wakeup 定向到当前 Lark 话题。
    wake_context: Optional[dict] = None
    raw_chat_id, _, thread_id = chat_id.partition(":")
    if is_group:
        cli_profile = bot.profile.lark_cli_profile or bot.profile.name
        wake_context = {
            "CC_LARK_PROFILE_NAME": bot.profile.name,
            "CC_LARK_CLI_PROFILE": cli_profile,
            "CC_LARK_CHAT_ID": raw_chat_id,
            "CC_LARK_THREAD_ID": thread_id,
            "CC_LARK_MESSAGE_ID": notify_msg_id or "",
            "CC_LARK_USER_ID": user_id or "",
            "CC_LARK_IS_GROUP": "1",
            "CC_LARK_HTTP_PORT": str(os.getenv("CALLBACK_PORT", "9981")),
            # Backward-compatible aliases for the first wake_context draft.
            "CC_LARK_PROFILE": bot.profile.name,
            "CC_LARK_ANCHOR": notify_msg_id or "",
            "CC_LARK_CALLBACK_PORT": str(os.getenv("CALLBACK_PORT", "9981")),
        }

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
    pty_warning: tuple[str, float] | None = None  # (label, since_ts) — PTY 抓到的 API 限流/过载提示

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
        if pty_warning:
            label, since = pty_warning
            footer.append(f"🚦 {label} {_fmt_duration(now - since)}")
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

    async def on_status(level: str, label: str):
        nonlocal pty_warning, last_push_time
        if level == "clear" or not label:
            if pty_warning is None:
                return
            pty_warning = None
        else:
            if pty_warning and pty_warning[0] == label:
                return  # 同一 label 不刷计时
            pty_warning = (label, time.time())
            log(bot.profile.name, "pty", "warn", f"PTY 抓到 API 异常: {label}")
        await push(_build_display())
        last_push_time = time.time()

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
        # ── Hung 自动重试 ───────────────────────────────────────
        # 只对 claude_pty 抛的 "Claude 客户端疑似 hung" RuntimeError 重试；
        # 其他错误（wall-clock、JSON、API key、orphan resume 等）一律不重试。
        # 写操作 skill 跳过 retry，防止 double-write。
        # max=1，硬编码；冷却 10s 让 TLS pool / claude 子进程释放。
        _AUTO_RETRY_MAX = 1
        _HUNG_MARKER = "客户端疑似 hung"
        _COOLDOWN_SECONDS = 10
        retry_count = 0
        last_exc: Optional[Exception] = None
        success = False
        full_text = ""
        new_session_id = ""
        used_fresh_session_fallback = False

        try:
            while True:
                try:
                    if retry_count == 0:
                        log(bot.profile.name, "agent", "info", "开始调用...")
                    else:
                        log(bot.profile.name, "agent", "info",
                            f"重试调用 ({retry_count}/{_AUTO_RETRY_MAX})...")
                    log(bot.profile.name, "agent", "info",
                        f"开始调用 runner={session.runner} model={session.model}")
                    full_text, new_session_id, used_fresh_session_fallback = await run_agent(
                        profile=bot.profile,
                        runner=session.runner,
                        message=claude_msg,
                        session_id=session.session_id,
                        model=session.model,
                        cwd=session.cwd,
                        permission_mode=session.permission_mode,
                        on_text_chunk=on_text_chunk,
                        on_tool_use=on_tool_use,
                        on_process_start=lambda proc: bot.active_runs.attach_process(user_id, chat_id, proc),
                        on_usage=on_usage,
                        on_status=on_status,
                        append_system_prompt=append_system_prompt or None,
                        wake_context=wake_context,
                    )
                    log(bot.profile.name, "agent", "info",
                        f"完成 runner={session.runner} session={new_session_id}")
                    success = True
                    break
                except Exception as e:
                    # 先杀心跳——无论后续 retry 还是 ❌，都要先让心跳停，再 update_card，
                    # 否则下一次心跳 push 会把 "🔄 重试中" 或 "❌" 覆盖回"进行中"画面。
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except (asyncio.CancelledError, Exception):
                        pass

                    if active_run.stop_requested:
                        return

                    is_hung = isinstance(e, RuntimeError) and _HUNG_MARKER in str(e)
                    blacklisted = _is_write_op_context(claude_msg, tool_history)
                    can_retry = (
                        is_hung
                        and retry_count < _AUTO_RETRY_MAX
                        and not blacklisted
                    )

                    if can_retry:
                        retry_count += 1
                        log(bot.profile.name, "claude", "warn",
                            f"hung 检测，{_COOLDOWN_SECONDS}s 后自动重试 "
                            f"({retry_count}/{_AUTO_RETRY_MAX})")
                        # 死循环防御：旧 session_id 在 Claude 服务端大概率已经 dirty
                        # （上轮 hung 时 API request 发到一半卡死），同一个 id 再 resume
                        # 必然撞同一个坑。强制清掉，下一轮 run_claude 走 fresh session。
                        # claude_pty 内部也有同样的 fresh-fallback 兜底，这里是双保险。
                        if session.session_id:
                            log(bot.profile.name, "claude", "warn",
                                f"清掉 hung session={session.session_id[:8]}，下一轮 fresh")
                            session.session_id = None
                        try:
                            await bot.feishu.update_card(
                                card_msg_id,
                                f"🔄 检测到 client hung，{_COOLDOWN_SECONDS}s 冷却后自动重试 "
                                f"({retry_count}/{_AUTO_RETRY_MAX})...",
                            )
                        except Exception:
                            pass
                        await asyncio.sleep(_COOLDOWN_SECONDS)
                        # 重置心跳计时（否则下一轮一启动就报"无输出 N min"）
                        last_output_ts = time.time()
                        last_push_time = 0.0
                        start_ts = time.time()
                        current_tool = None
                        pty_warning = None
                        heartbeat_task = asyncio.create_task(_heartbeat())
                        continue

                    if is_hung and blacklisted and retry_count == 0:
                        log(bot.profile.name, "claude", "warn",
                            "hung 但本轮涉及写操作 skill，跳过 auto-retry")
                    log(bot.profile.name, "claude", "error",
                        f"运行失败: {type(e).__name__}: {e}")
                    traceback.print_exc()
                    last_exc = e
                    break
        finally:
            # 成功/失败路径统一：先等心跳协程完全退出再动最终卡片。只 cancel 不 await
            # 的话，一个 in-flight 的心跳 push（HTTP 已发出）可能在最终内容之后落地，
            # 把 ✅/❌ 覆盖回"进行中"画面（错误路径此前修过同款竞态，这里补齐成功路径）。
            if not heartbeat_task.done():
                heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass

        if not success:
            # claude_pty 撞用量上限 / API 错误时，会把崩溃前的 session id 挂在异常上
            # （cc_session_id）。这个 session 的 JSONL 完整、可 --resume，必须存进 store，
            # 否则用户下一轮"继续"会 fresh 一个空 session，整段上下文丢失。
            # watchdog hung 的异常不带这个属性（其服务端 conversation state 已脏，
            # 故意不复用，见上方 hung-retry 分支清掉 session.session_id 的逻辑）。
            resumable_sid = getattr(last_exc, "cc_session_id", None)
            if resumable_sid:
                try:
                    await bot.store.on_agent_response(
                        user_id, chat_id, resumable_sid, preview_text or text,
                    )
                    log(bot.profile.name, "claude", "info",
                        f"崩溃前 session={resumable_sid[:8]} 已保存，下一轮『继续』可 resume")
                except Exception:
                    pass
            clean = _format_run_error(last_exc)
            err_brief = (
                f"❌ 自动重试 {retry_count} 次后仍失败：{clean}"
                if retry_count > 0
                else f"❌ Agent 执行出错：{clean}"
            )
            if resumable_sid:
                err_brief += "\n\n💾 上下文已保留，配额恢复后发『继续』即可接着上次进度跑。"
            # 出错前流式渲染出的内容（工具轨迹 + 部分回答）对用户仍有价值，
            # 不能整卡覆盖成错误信息——保留旧内容，把错误追加在末尾。
            partial_parts = []
            if tool_history:
                partial_parts.append("\n".join(tool_history[-5:]))
            if accumulated:
                d = accumulated
                if len(d) > _MAX_STREAM_DISPLAY:
                    d = "...\n\n" + d[-_MAX_STREAM_DISPLAY:]
                partial_parts.append(d)
            if partial_parts:
                err_card = (
                    "\n\n".join(partial_parts)
                    + "\n\n---\n\n⚠️ **任务没有执行完，中途报错了**（以上为出错前的进度）\n\n"
                    + err_brief
                )
            else:
                err_card = err_brief
            try:
                await bot.feishu.update_card(card_msg_id, err_card)
            except Exception:
                pass
            # 流式卡若开着，关掉它恢复交互（非流式/未登记则 no-op，且永不抛）
            await bot.feishu.finalize_streaming_card(card_msg_id)
            # 卡片是 in-place patch，不会触发 Lark 新消息通知。异常退出时额外发一条独立
            # ❌ 短消息，与成功路径下的独立 ✅ 对齐，让用户能在消息列表里直接看到出错。
            err_notify = "❌ 异常退出" + (
                f"（已自动重试 {retry_count} 次）" if retry_count > 0 else ""
            )
            try:
                if is_group and notify_msg_id:
                    await bot.feishu.reply_text(notify_msg_id, err_notify)
                else:
                    await bot.feishu.send_text_to_user(user_id, err_notify)
            except Exception:
                pass
            return

        final = full_text or accumulated or "（无输出）"
        if used_fresh_session_fallback:
            final = (
                "⚠️ 无法接续上一轮会话（resume 失败），已自动开新会话继续"
                "——之前的上下文没有带过来。\n\n" + final
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
                # 流式卡：update_card_with_buttons 内部会推最终文本 + 关流式 + 加按钮
                await bot.feishu.update_card_with_buttons(card_msg_id, final, buttons, flow=short)
            else:
                await bot.feishu.update_card(card_msg_id, final)
                # 无按钮的流式卡推完最终文本后需手动关流式（非流式则 no-op）
                await bot.feishu.finalize_streaming_card(card_msg_id)
            card_patched = True
        except Exception as e:
            log(bot.profile.name, "card", "error", f"卡片更新失败，回退发文本: {e}")
            # 卡片更新失败时流式卡可能仍开着，先收尾
            await bot.feishu.finalize_streaming_card(card_msg_id)
            try:
                if is_group and notify_msg_id:
                    await bot.feishu.reply_card(notify_msg_id, content=final, loading=False)
                else:
                    await bot.feishu.send_text_to_user(user_id, final)
            except Exception as fallback_err:
                log(bot.profile.name, "card", "error", f"文本回退也失败: {fallback_err}")
                # 卡片 + 文本回退都失败（额度耗尽 / 渲染故障等）：结果落 outbox，绝不丢
                saved = bot.feishu.save_outbox(
                    final, kind="result", error=str(fallback_err),
                    meta={"chat_id": chat_id, "user": user_id,
                          "card_msg_id": card_msg_id, "session": new_session_id or ""},
                )
                if saved:
                    log(bot.profile.name, "outbox", "warn", f"结果已落 outbox: {saved}")

        if card_patched:
            try:
                if is_group and notify_msg_id:
                    await bot.feishu.reply_text(notify_msg_id, "✅")
                else:
                    await bot.feishu.send_text_to_user(user_id, "✅")
            except Exception:
                pass

        if new_session_id:
            await bot.store.on_agent_response(
                user_id, chat_id, new_session_id, preview_text or text,
                usage=final_usage or None,
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

        # 把本轮最终响应文本作为返回值交回（成功路径）。dispatch_task 的子会话靠它把结果
        # **内联**进给主 agent 的唤醒消息——不必再 read_thread（而 read_thread 受 Lark
        # interactive 卡"初始快照"限制，本来也拿不到子的最终答案文本）。错误路径上面已 return（→None）。
        return full_text
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
            text = strip_lark_mentions(text, getattr(msg, 'mentions', None))
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

    elif msg.message_type == "audio":
        try:
            content_obj = json.loads(msg.content)
            file_key = content_obj.get("file_key", "")
            if not file_key:
                return
            duration_ms = int(content_obj.get("duration") or 0)
            audio_path = await bot.feishu.download_file(
                msg.message_id, file_key, msg_type="audio", file_name="voice.opus",
            )
            transcript = await bot.feishu.speech_to_text(audio_path, file_id=msg.message_id)
            if not transcript:
                raise RuntimeError("识别结果为空（可能没说话或环境噪音过大）")
            text = (
                f"[用户发送了一条语音消息（{duration_ms // 1000}s），以下为自动转写，"
                f"可能存在同音字/分词误差，请按口语理解]\n{transcript}"
            )
            preview_text = f"🎤 {transcript[:40]}"
            log(tag, "audio", "info", f"转写 {duration_ms}ms → {transcript[:50]}")
        except Exception as e:
            log(tag, "audio", "error", f"语音转写失败: {e}")
            if is_group:
                try:
                    await bot.feishu.reply_card(msg.message_id, content=f"❌ 语音转写失败：{e}", loading=False)
                except Exception:
                    pass
            else:
                await bot.feishu.send_text_to_user(user_id, f"❌ 语音转写失败：{e}")
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
            post_text = strip_lark_mentions(post_text, getattr(msg, 'mentions', None))

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
        if cmd == "verify":
            log(tag, "cmd", "info", f"执行 /verify args={args!r}")
            await _handle_verify_command(
                bot, user_id, chat_id, is_group, thread_id, msg, args,
            )
            return
        log(tag, "cmd", "info", f"执行 {cmd}")
        reply = await handle_command(cmd, args, user_id, chat_id, bot.store, bot=bot)
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

    # ── 普通消息 → 调用当前 profile 的 agent ─────────────────
    session = await bot.store.get_current(user_id, chat_id)
    log(tag, "agent", "info",
        f"runner={session.runner} session={session.session_id} model={session.model}")

    # 透传给 claude 内置的控制命令（如 /compact）：绝不能在前面拼话题上下文，否则
    # 消息不再以 /compact 开头，_escape_for_pty 不透传、claude 当普通文本，内置命令
    # 不触发 = 没效果。同时跳过 set_last_seen——这条命令并没有真正"读"话题内容，
    # 把未读上下文留给下一条真实消息。
    if thread_id and is_builtin_passthrough(text):
        log(tag, "thread", "info", "透传内置命令，跳过话题上下文注入")
    elif thread_id:
        try:
            last_seen = await bot.store.get_last_seen(user_id, chat_id)
            context_block, ctx_paths, ctx_err = await build_thread_context(
                bot.feishu, thread_id, last_seen, msg.message_id,
            )
            if ctx_err:
                # 拉历史失败（多半缺 im:message.group_msg 权限）。不再静默吞掉：
                # 结构化告警 + 告诉用户读不到历史。失败时【不推进 last_seen】，
                # 权限修好后下次仍能补全这段 backlog。
                log(tag, "thread", "warn", f"拉取话题历史失败（缺权限？）: {ctx_err}")
                hint = _thread_ctx_error_hint(ctx_err)
                if text.strip():
                    text = f"{hint}\n\n【用户说】\n{text}"
                else:
                    # 只 @ 没正文，本来就指望历史 → 直接回提示，不浪费一次 runner 调用
                    try:
                        await bot.feishu.reply_card(
                            msg.message_id, content=hint, loading=False,
                        )
                    except Exception:
                        pass
                    return
            else:
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
        asker_open_id=user_id, trinity_ctx=trinity_ctx, runner=session.runner,
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
    if m.startswith("gpt-5") or "codex" in m:
        return 258_400
    return 200_000


def build_lark_system_prompt(
    profile: Profile,
    raw_chat_id: str,
    thread_id: str,
    user_message_id: str,
    is_group: bool,
    asker_open_id: str = "",
    trinity_ctx: Optional[TrinityContext] = None,
    runner: str = "",
) -> str:
    """构造注入到 Claude 的 Lark 语境系统提示。模板见 prompts/。
    runner 决定运行时 MCP 段落的注入版本（非 claude 后端没有那些工具）。"""
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
        runner=runner,
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


# Claude CLI TUI 输出的 ANSI 控制序列（CSI / OSC / DEC private mode / 单字符）。
# PTY runner 抛 RuntimeError 时 detail 里会原样夹带这些，不清洗的话直接 dump 到
# Lark 卡片会变成"[22m [32m ▓▓ shift+tab"这种乱码淹没用户。
_ANSI_RE = re.compile(
    r"\x1b\[[?]?[0-9;]*[a-zA-Z]"           # CSI: ESC [ ... letter
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC: ESC ] ... BEL / ST
    r"|\x1b[=>]"                            # 简单单字符
)
_CTRL_CHARS = "".join(chr(c) for c in range(32) if c not in (9, 10))  # 留 \t \n


def _format_run_error(exc: Optional[BaseException]) -> str:
    """把 PTY runner / Claude 子进程的异常清洗成给用户看的短文本。

    - ANSI 控制序列 strip 掉（CSI / OSC / DEC private mode）
    - 控制字符 strip（保留 \\t \\n）
    - 折叠空白 / 多余空行
    - 截断到 400 字符，给真正有用的报错头部留位置
    - "new session jsonl never appeared" 加一句业务友好提示
    """
    if exc is None:
        return "（未知错误）"
    raw = f"{type(exc).__name__}: {exc}"
    s = _ANSI_RE.sub("", raw)
    s = s.translate({ord(c): None for c in _CTRL_CHARS})
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s).strip()
    if len(s) > 400:
        s = s[:400] + "…"
    if "new session jsonl never appeared" in s:
        s += (
            "\n💡 Claude Code TUI 已启动，但没有接收本轮输入或没有创建会话 JSONL。"
            "请重试；如果连续出现，先用备用 runner 承接或重启 bot。"
        )
    return s


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
        status = str(inp.get("status") or "").lower()
        exit_code = inp.get("exit_code")
        if status == "completed":
            prefix = "✅" if exit_code in (0, "0", None) else "⚠️"
            suffix = "" if exit_code in (0, "0", None) else f"（exit {exit_code}）"
            return f"{prefix} **执行命令：** `{cmd}`{suffix}" if cmd else f"{prefix} **执行命令完成**{suffix}"
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

    if cmd == "restart":
        from commands import _trigger_restart, restart_strategy
        strat = restart_strategy()
        if strat == "bare":
            if card_msg_id:
                try:
                    await bot.feishu.update_card(
                        card_msg_id,
                        "❌ 没找到 supervisor（非 launchd 任务、无 .app），"
                        "直接退出会停服，已取消重启。")
                except Exception:
                    pass
            return
        affected = await _handle_restart_command(bot)
        via = "launchd kickstart" if strat == "launchd" else "open .app"
        ack = (f"♻️ 服务重启中（通知了 {affected} 个进行中的会话）— "
               f"{via}，~3-5s 后回来。")
        if card_msg_id:
            try:
                await bot.feishu.update_card(card_msg_id, ack)
            except Exception:
                pass
        _trigger_restart()
        return

    reply = await handle_command(cmd, args, user_id, chat_id, bot.store, bot=bot)
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
                asker_open_id=user_id, runner=session.runner,
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
    display_model = cur.get("model_override") or bot.store.default_model
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
    model: str = "",
) -> tuple[bool, str]:
    """在 (user, chat_id_raw:thread_id) 这一格强制开新 session 跑 prompt。

    设计场景：大群里的"调度 session"读完上下文后，用 lark-cli 在会话群创建新话题，
    再 curl 这个端点把后续工作派给独立 session。和 WS 路径不冲突——这是绕过 WS 的
    内部触发入口。

    返回 (ok, text)：ok=True 时 text 是子会话最终响应文本；ok=False 时 text 是
    失败原因（忙被拒 / 卡片失败 / agent 出错等）。此前所有失败路径都裸 return None，
    dispatch_task 的 done-callback 无法区分"跑完了"和"根本没跑"，掉活会被记成 ✅。
    """
    tag = bot.profile.name

    # 兜底：dispatcher Claude 偶尔会把 anchor 的 message_id (om_xxx) 当成 thread_id 传过来。
    # 这样起的 session chat_key 是 oc_xxx:om_xxx，跟用户后续在该话题 @ bot 时构造的
    # oc_xxx:omt_xxx 不匹配，session 就丢了。识别出 om_xxx 形式 → 主动 mget 转成真 thread_id。
    if thread_id.startswith("om_") and not thread_id.startswith("omt_"):
        # 网络瞬断 / SSL handshake 超时会让单次 mget 失败 → 必须重试。
        # 退避：1s, 3s, 7s（总 11s 上限），3 次都失败再放弃。
        actual = ""
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                actual = await bot.feishu.get_message_thread_id(thread_id)
                if actual and actual.startswith("omt_"):
                    break
            except Exception as e:
                last_err = e
                log(tag, "spawn", "warn",
                    f"mget thread_id 失败 (try {attempt+1}/3) {thread_id[:14]}...: {e}")
            if attempt < 2:
                await asyncio.sleep(1 + attempt * 2)
        if actual and actual.startswith("omt_"):
            log(tag, "spawn", "info",
                f"自动修正 thread_id: {thread_id[:14]}... → {actual[:14]}...")
            thread_id = actual
        else:
            reason = f"mget 三次失败: {last_err}" if last_err else "mget 返回空 thread_id"
            log(tag, "spawn", "warn",
                f"拒绝：thread_id={thread_id[:14]}... — {reason}")
            try:
                await bot.feishu.reply_text(
                    anchor_message_id,
                    f"⚠️ /spawn 无法转换 thread_id（{thread_id[:14]}...）：{reason}。"
                    f"通常是网络瞬断；下个 cron 会重试。",
                )
            except Exception:
                pass
            return (False, f"thread_id 转换失败：{reason}")

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
        return (False, "目标话题已有任务在跑，spawn 被拒（忙）")

    async with lock:
        try:
            await bot.store.new_session(user_id, chat_id)
            if model:
                await bot.store.set_model(user_id, chat_id, model)
            session = await bot.store.get_current(user_id, chat_id)

            try:
                card_msg_id = await bot.feishu.reply_card(anchor_message_id, loading=True)
            except Exception as e:
                log(tag, "spawn", "error", f"占位卡片发送失败: {e}")
                try:
                    await bot.feishu.reply_text(anchor_message_id, f"❌ spawn 失败：{e}")
                except Exception:
                    pass
                return (False, f"占位卡片发送失败：{e}")

            lark_sys = build_lark_system_prompt(
                bot.profile, chat_id_raw, thread_id, anchor_message_id, is_group=True,
                asker_open_id=user_id, runner=session.runner,
            )

            log(tag, "spawn", "info",
                f"user={user_id[:8]}... chat={chat_id_raw[:10]}... "
                f"thread={thread_id[:10]}... anchor={anchor_message_id[:12]}... "
                f"prompt_len={len(prompt)}")

            # 返回子会话最终响应文本，供 dispatch_task 的 done-callback 内联进唤醒消息。
            # _run_and_display：成功返回 full_text，错误/被停止路径返回 None。
            res = await _run_and_display(
                bot,
                user_id, chat_id, True, prompt,
                card_msg_id, session, anchor_message_id,
                preview_text=prompt[:40],
                append_system_prompt=lark_sys,
            )
            if res is None:
                return (False, "agent 运行失败或被停止（详见子话题卡片）")
            return (True, res)
        except Exception as e:
            log(tag, "spawn", "error", f"异常: {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()
            return (False, f"内部异常：{type(e).__name__}: {e}")


# ── 通用多 agent 派发（dispatch_task / read_thread）────────────────────────
# 基于 Lark 的多 agent 派发+监工系统的可复用内核。把两条原语做进 bot 侧，
# 经 cc_mcp_server 的 dispatch_task / read_thread 工具暴露给任意编排 agent：
#   · dispatch_task —— 在目标群新开一条 thread 派一个独立 cc-lark 子会话（fan-out）
#   · read_thread   —— 拉回某子会话 thread 的全部消息（supervise / 取结果）
# 派发走内部 handle_spawn（不依赖 lark-cli user 身份 / WS 往返），子会话在新 thread
# 里独立跑；编排方拿 thread_id 后用 read_thread 轮询。保留实测出的物理硬约束：
# 并发 ≤ cap（实测 14 全卡死）。"不碰 prod"靠派发 prompt 自带，不在这层强制。
# 默认 7：与 prompts/default.md、cc_mcp_server 工具描述保持同一口径（实测 14 全卡死）。
DISPATCH_CONCURRENCY_CAP = int(os.getenv("DISPATCH_CONCURRENCY_CAP", "7") or "7")
_DISPATCH_TASKS: set = set()   # 持回报/唤醒等辅助 task 引用，防被 GC
# cap 的计数依据：每个子会话 task 从 create_task 起就进这个集合、done 才出。
# 此前用 active_runs 计数有 TOCTOU 窗口——子会话从派发到真正 start_run 之间隔着
# 建话题/mget/发卡片好几个 await，一波快速连派会全部通过检查。
_DISPATCH_CHILDREN: set = set()
# 父→子批次跟踪：parent_thread -> {bot, thread, anchor, chat, user, pending:int, children:[(title,thread)]}
# 子会话**结束后一定回报父 agent**靠这层工程化（不靠子 agent 自觉 @）：
#   · 每个子会话结束 → 往父 thread 贴一行完成/异常通知（bot 知道 turn 生命周期，子崩了也能报）
#   · 父的最后一个子会话结束 → 唤醒父 agent 一次（handle_spawn 进父 thread，带"去 read_thread 收 N 个结果"）
# 只在 dispatch_task 带了父上下文（parent_thread/parent_anchor）时启用；缺则退化成纯 fire-and-forget。
_DISPATCH_PARENTS: dict[str, dict] = {}


_BOT_OPEN_ID_CACHE: dict[str, str] = {}   # app_id -> bot 自身 open_id（@自己触发用）


async def _resolve_bot_open_id(bot: BotInstance) -> str:
    """拿 bot 自己的 open_id（用于 @ 自己触发 WS 唤醒）。profile 配了 BOT_OPEN_ID 优先；
    否则向 Lark 要 /bot/v3/info 自动发现并按 app_id 缓存。失败返回空串（调用方降级）。"""
    if getattr(bot.profile, "bot_open_id", ""):
        return bot.profile.bot_open_id
    app_id = bot.profile.app_id
    if app_id in _BOT_OPEN_ID_CACHE:
        return _BOT_OPEN_ID_CACHE[app_id]

    def _fetch() -> str:
        import urllib.request as _u
        dom = (bot.profile.domain or "https://open.larksuite.com").rstrip("/")
        req = _u.Request(
            f"{dom}/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": app_id, "app_secret": bot.profile.app_secret}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        tok = json.loads(_u.urlopen(req, timeout=10).read())["tenant_access_token"]
        req2 = _u.Request(f"{dom}/open-apis/bot/v3/info", headers={"Authorization": f"Bearer {tok}"})
        info = json.loads(_u.urlopen(req2, timeout=10).read())
        return ((info.get("bot") or {}).get("open_id") or "")

    try:
        oid = await asyncio.to_thread(_fetch)
    except Exception as e:
        log(bot.profile.name, "wake", "warn", f"取 bot open_id 失败: {type(e).__name__}: {e}")
        oid = ""
    if oid:
        _BOT_OPEN_ID_CACHE[app_id] = oid
    return oid


async def wake_thread_as_user(bot: BotInstance, anchor_msg_id: str, prompt: str) -> bool:
    """以 **user 身份** 在 anchor_msg_id 所在 thread 里 @bot 自己发一条 prompt。

    走的是正常 WS 入站路径（owner 发的 @bot 消息）——和 /spawn 不同：忙时 **排队** 而非
    "已忽略" 丢弃，且会 **resume 该 thread 的现有 session**（带上下文）。这才是稳的唤醒姿势。
    返回是否发送成功（失败仅 log，调用方不崩）。"""
    if not anchor_msg_id:
        log(bot.profile.name, "wake", "warn", "缺 anchor，无法 send-as-user 唤醒")
        return False
    bot_oid = await _resolve_bot_open_id(bot)
    if not bot_oid:
        log(bot.profile.name, "wake", "warn", "拿不到 bot open_id，无法 @ 自己唤醒")
        return False
    cli_profile = bot.profile.lark_cli_profile or bot.profile.name
    text = f'<at user_id="{bot_oid}"></at> {prompt}'
    cmd = [
        "lark-cli", "--profile", cli_profile, "im", "+messages-reply",
        "--as", "user", "--message-id", anchor_msg_id, "--reply-in-thread",
        "--text", text,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=25)
    except Exception as e:
        log(bot.profile.name, "wake", "warn", f"send-as-user 唤醒异常: {type(e).__name__}: {e}")
        return False
    if proc.returncode != 0:
        log(bot.profile.name, "wake", "warn",
            f"send-as-user 唤醒 lark-cli rc={proc.returncode} err={err.decode('utf-8','replace')[:200]}")
        return False
    return True


async def _dispatch_safe_reply(bot: BotInstance, anchor: str, text: str) -> None:
    """往父 thread 贴一行（best-effort，失败只 log）。"""
    try:
        await bot.feishu.reply_text(anchor, text)
    except Exception as e:
        log(bot.profile.name, "dispatch", "warn", f"回父 thread 失败: {type(e).__name__}: {e}")


async def _dispatch_wake_parent(grp: dict) -> None:
    """父的子任务批次全部完成 → 唤醒父 agent 一次去收口（监工闭环）。

    走 send-as-user @bot（wake_thread_as_user）而不是 handle_spawn——后者 reject-if-busy
    会被 "话题已有任务在跑，已忽略" 丢掉（实测闭环就栽在这）；前者经 WS 排队、且 resume
    父 session。**唤醒消息内联每个子任务的实际结果**（grp["results"] 里存的子会话最终响应
    文本），主 agent 醒来即拿到全部内容，无需再 read_thread。"""
    bot = grp["bot"]
    results = grp.get("results", [])
    blocks = []
    for title, thread, text, ok in results:
        head = f"━━ {title} {'✅' if ok else '⚠️ 未完成'}（thread {thread}）"
        body = (text or "").strip() or "（子会话无文本输出）"
        if len(body) > 1500:
            body = body[:1500] + f'\n…（截断，完整用 read_thread(thread_id="{thread}")）'
        blocks.append(f"{head}\n{body}")
    joined = "\n\n".join(blocks) or "（无结果）"
    prompt = (
        f"[🔔 子任务批次完成] 你派出的 {len(results)} 个子任务都跑完了，结果已**内联**在下面"
        f"（无需再 read_thread，除非要看完整细节）：\n\n{joined}\n\n"
        f"请核对 / 汇总后回复用户。如还需继续，可再 dispatch_task 派下一波。"
    )
    ok = await wake_thread_as_user(bot, grp.get("anchor", ""), prompt)
    if not ok:
        log(bot.profile.name, "dispatch", "warn", "批次完成唤醒父 agent 失败（send-as-user 未成功）")


async def dispatch_task(
    bot: BotInstance, *, user_id: str, group_chat_id: str,
    title: str, prompt: str, cap: int = DISPATCH_CONCURRENCY_CAP,
    parent_thread: str = "", parent_anchor: str = "",
) -> dict:
    """在 group_chat_id 新开一条 thread 派一个独立 cc-lark 子会话跑 prompt。

    fire-and-forget：建好话题 + 启动子会话后立即返回 thread_id，子会话独立续跑，
    不阻塞本次调用。编排方用 read_thread(thread_id) 轮询拉结果。≤cap 并发保护。

    parent_thread/parent_anchor：派发方（主 agent）所在 thread + 其锚点消息。给了就启用
    "子会话结束→回报父 thread + 批次全完→唤醒父 agent"的工程化闭环（见 _DISPATCH_PARENTS）。
    """
    if not group_chat_id:
        return {"ok": False, "error": "缺少 group_chat_id（派发目标群）"}
    if not (prompt and prompt.strip()):
        return {"ok": False, "error": "prompt 不能为空"}
    active = len(_DISPATCH_CHILDREN)
    if active >= cap:
        return {"ok": False, "error": f"并发已达上限 {cap}（在跑/在启动的子会话 {active}），等已派的跑完再派下一波"}
    user = user_id or bot.store.find_primary_user() or ""
    if not user:
        return {"ok": False, "error": "无法确定归属人 user_id"}

    topic_title = (title.strip() if (title and title.strip())
                   else prompt.strip().splitlines()[0])[:60]
    try:
        anchor = await bot.feishu.send_post_to_chat(
            chat_id=group_chat_id,
            title=f"🤖 {topic_title}",
            body_text="（cc-lark 子任务已派发，正在独立处理…）",
            mention_open_id=user,
        )
    except Exception as e:
        return {"ok": False, "error": f"建话题失败: {type(e).__name__}: {e}"}

    thread_id = anchor
    try:
        resolved = await bot.feishu.get_message_thread_id(anchor)
        if resolved:
            thread_id = resolved
    except Exception:
        pass

    # 父批次登记（带父上下文才启用回报闭环）
    if parent_thread and parent_anchor:
        grp = _DISPATCH_PARENTS.setdefault(parent_thread, {
            "bot": bot, "thread": parent_thread, "anchor": parent_anchor,
            "chat": group_chat_id, "user": user, "pending": 0, "results": [],
        })
        grp["pending"] += 1

    # fire-and-forget：子会话独立跑，不阻塞派发返回。进 _DISPATCH_CHILDREN 计数
    # （cap 依据）兼防 GC。
    t = asyncio.create_task(handle_spawn(
        bot, user_id=user, chat_id_raw=group_chat_id,
        thread_id=thread_id, anchor_message_id=anchor, prompt=prompt,
    ))
    _DISPATCH_CHILDREN.add(t)

    def _on_child_done(fut, _title=topic_title, _thread=thread_id, _ptid=parent_thread):
        _DISPATCH_CHILDREN.discard(fut)
        # handle_spawn 返回 (ok, text)：ok=False 时 text 是失败原因。
        # 掉活（忙被拒/卡片失败/agent 出错）必须报 ⚠️，不能装 ✅。
        exc = None
        ok = False
        result_text = ""
        fail_reason = ""
        try:
            exc = fut.exception()
        except Exception:
            exc = None
        if exc is not None:
            fail_reason = f"{type(exc).__name__}: {exc}"
        else:
            r = None
            try:
                r = fut.result()
            except Exception:
                pass
            if isinstance(r, tuple) and len(r) == 2:
                ok = bool(r[0])
                if ok:
                    result_text = r[1] or ""
                else:
                    fail_reason = r[1] or "子会话未跑完（原因未知）"
            else:  # 兼容旧契约（裸文本/None）
                ok = r is not None
                result_text = r or ""
                if not ok:
                    fail_reason = "子会话未返回结果"
        # 1) 每个子会话结束 → 回报父 thread（含结果摘要；工程化保证，子崩了也报）
        if parent_anchor:
            status = "✅ 完成" if ok else f"⚠️ 未完成（{fail_reason[:120]}）"
            line = f"🔔 子任务「{_title}」{status}　thread={_thread}"
            snippet = (result_text or "").strip().replace("\n", " ")
            if snippet:
                line += f"\n{snippet[:200]}"
            rt = asyncio.create_task(_dispatch_safe_reply(bot, parent_anchor, line))
            _DISPATCH_TASKS.add(rt)
            rt.add_done_callback(_DISPATCH_TASKS.discard)
        # 2) 批次全完 → 唤醒父 agent 一次收口（内联所有子结果；失败的内联原因）
        grp2 = _DISPATCH_PARENTS.get(_ptid) if _ptid else None
        if grp2 is not None:
            grp2["pending"] -= 1
            grp2.setdefault("results", []).append(
                (_title, _thread, result_text if ok else f"（未完成）{fail_reason}", ok))
            if grp2["pending"] <= 0:
                _DISPATCH_PARENTS.pop(_ptid, None)
                wt = asyncio.create_task(_dispatch_wake_parent(grp2))
                _DISPATCH_TASKS.add(wt)
                wt.add_done_callback(_DISPATCH_TASKS.discard)

    t.add_done_callback(_on_child_done)

    log(bot.profile.name, "dispatch", "info",
        f"派子会话 chat={group_chat_id[:12]}... thread={thread_id[:14]}... "
        f"active={active + 1} parent={parent_thread[:12] + '...' if parent_thread else '-'}")
    return {"ok": True, "thread_id": thread_id, "anchor_message_id": anchor,
            "active_after": active + 1, "cap": cap}


async def read_thread(bot: BotInstance, *, thread_id: str, limit: int = 50) -> dict:
    """读一条 thread 的全部消息，渲染成 `[seq] sender time: text` 紧凑 transcript。

    给编排 agent 拉回某个 dispatch_task 子会话的进展/结果。复用 thread_context 的
    消息抽取逻辑，不重造轮子。"""
    if not thread_id:
        return {"ok": False, "error": "缺少 thread_id"}
    try:
        msgs = await bot.feishu.list_thread_messages(thread_id, limit=limit)
    except Exception as e:
        return {"ok": False, "error": f"读 thread 失败: {type(e).__name__}: {e}"}
    from thread_context import _extract, _sender_label, _fmt_time
    lines: list[str] = []
    for i, m in enumerate(msgs, 1):
        try:
            text, _atts = _extract(m, None)
        except Exception:
            text = "[unparseable]"
        sender = _sender_label(m)
        ts = _fmt_time(m.create_time)
        lines.append(f"[{i}] {sender} {ts}: {text}".strip())
    transcript = "\n".join(lines) if lines else "(thread 暂无消息)"
    if len(transcript) > 8000:
        transcript = "…(前文截断)\n" + transcript[-8000:]
    return {"ok": True, "count": len(msgs), "transcript": transcript}
