"""
飞书/Lark × Claude Code Bot — 多 profile 版本。

每个 profile 对应一个独立的 Lark/Feishu 应用（app_id/app_secret/brand 独立），
运行在各自的 WebSocket 长连接和 SessionStore 上；多个 profile 共用同一个
事件循环、卡片回调 HTTP 端口、ngrok 隧道、看门狗和摘要后台线程。

启动：python main.py
"""

import asyncio
import json
import re
import sys
import os
import threading
import time
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler

# 确保项目目录在 sys.path 最前面
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lark_oapi as lark
import lark_oapi.ws.client as _lark_ws_mod  # noqa: E402
from lark_oapi.api.im.v1.model import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger, P2CardActionTriggerResponse, CallBackToast,
)


# lark_oapi.ws.client 有一个模块级全局 `loop`，所有 ws.Client 实例共用它，
# 所以原生不支持「同进程跑多个 bot」。把它替换成代理对象，按调用线程的
# 默认事件循环动态分发 —— 每个 WS 线程先 set_event_loop(new_loop) 拿到自己的
# 事件循环，然后 lark_oapi 内部所有 `loop.*` 调用都会落到正确的那个 loop。
class _PerThreadLoopProxy:
    def __getattr__(self, attr):
        return getattr(asyncio.get_event_loop(), attr)


_lark_ws_mod.loop = _PerThreadLoopProxy()

import bot_config as config
from bot_config import Profile
from feishu_client import FeishuClient
from feishu_post import parse_post_content, extract_post_image_keys
from session_store import SessionStore, generate_summary, _write_custom_title
from commands import parse_command, handle_command
from claude_runner import run_claude
from run_control import ActiveRun, ActiveRunRegistry, stop_run
from thread_context import build_thread_context

# ── 看门狗：定时重启防止 WebSocket 假死 ──────────────────────
# launchd 只能检测进程退出，检测不到"假死"（进程在但 WS 不响应）。
# watchdog 在指定时长后主动退出，由 launchd KeepAlive 拉起，作为保险。

MAX_UPTIME = 6 * 3600   # 最长运行 6 小时后主动重启
_start_time = time.time()
_last_event = time.time()


def _watchdog():
    """后台线程，定期检查进程健康。超时退出让 launchctl 拉起。"""
    while True:
        time.sleep(300)  # 每 5 分钟检查
        uptime = time.time() - _start_time
        if uptime > MAX_UPTIME:
            print(f"[watchdog] 运行 {uptime/3600:.1f}h，定时重启刷新连接", flush=True)
            # 用非零退出码，确保即使 plist 是 SuccessfulExit-only 模式也能被拉起
            os._exit(1)
        # 常态不打日志，避免刷屏。需要巡检时用 cc-lark status 查看进程信息。


# ── 全局单例（跨 profile 共享）──────────────────────────────

# 独立的 asyncio 事件循环，启动时即就绪，不依赖 lark SDK 的首条消息
_bot_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()


def _start_bot_loop():
    asyncio.set_event_loop(_bot_loop)
    _bot_loop.run_forever()


threading.Thread(target=_start_bot_loop, daemon=True, name="bot-loop").start()


# ── BotInstance：一个 profile 的运行时包 ───────────────────

class BotInstance:
    """一个 profile 的全部状态：Lark SDK client、FeishuClient、session store、
    per-chat 锁、运行注册表。消息处理函数把 self 当作上下文。"""

    _MAX_CHAT_LOCKS = 200

    def __init__(self, profile: Profile):
        self.profile = profile
        self.lark_client = (
            lark.Client.builder()
            .app_id(profile.app_id)
            .app_secret(profile.app_secret)
            .domain(profile.domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        self.feishu = FeishuClient(
            self.lark_client,
            app_id=profile.app_id,
            app_secret=profile.app_secret,
            domain=profile.domain,
        )
        self.store = SessionStore(
            profile=profile.name,
            default_cwd=profile.default_cwd,
        )
        self.active_runs = ActiveRunRegistry()
        # per-chat 消息队列锁，保证同一群组的消息串行处理，允许不同群组并发处理
        self.chat_locks: dict[str, asyncio.Lock] = {}

    # ── 锁管理 ───────────────────────────────────────────────
    def _ensure_chat_lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self.chat_locks:
            if len(self.chat_locks) >= self._MAX_CHAT_LOCKS:
                idle = [k for k, v in self.chat_locks.items() if not v.locked()]
                for k in idle[:len(idle) // 2]:
                    del self.chat_locks[k]
            self.chat_locks[chat_id] = asyncio.Lock()
        return self.chat_locks[chat_id]


# profile_name → BotInstance
_bots: dict[str, BotInstance] = {}


# ── 消息工具函数 ─────────────────────────────────────────────

def extract_chat_info(event: P2ImMessageReceiveV1) -> tuple[str, str, bool, str, str]:
    """
    Returns:
        (user_id, chat_id, is_group, raw_chat_id, thread_id)
    """
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
        print(f"[{bot.profile.name}][warn] update stopped card failed: {exc}", flush=True)


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
        print(f"[{bot.profile.name}][error] 命令菜单发送失败: {e}", flush=True)


# ── 核心消息处理 ─────────────────────────────────────────────

async def handle_message_async(bot: BotInstance, event: P2ImMessageReceiveV1):
    """异步处理一条飞书消息"""
    msg = event.event.message
    tag = bot.profile.name
    print(f"[{tag}][收到消息] type={msg.message_type} chat={msg.chat_type}", flush=True)

    user_id, chat_id, is_group, raw_chat_id, thread_id = extract_chat_info(event)
    print(
        f"[{tag}][Chat Info] user={user_id[:8]}... chat={raw_chat_id[:10]}... "
        f"thread={thread_id[:10] if thread_id else '-'} is_group={is_group}",
        flush=True,
    )

    # 访问控制：群聊白名单 + 用户 allowlist（静默忽略，避免泄露 bot 存在）
    if is_group and raw_chat_id not in bot.profile.allowed_group_chat_ids:
        print(f"[{tag}][拒绝] 群不在白名单 chat={raw_chat_id[:10]}...", flush=True)
        return
    if bot.profile.allowed_open_ids and user_id not in bot.profile.allowed_open_ids:
        print(f"[{tag}][拒绝] user={user_id} 不在 allowlist", flush=True)
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
            await _process_message(bot, user_id, chat_id, is_group, thread_id, msg)
        except Exception as e:
            print(f"[{tag}][error] 消息处理异常: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()


async def _run_and_display(
    bot: BotInstance,
    user_id: str, chat_id: str, is_group: bool,
    text: str, card_msg_id: str, session, notify_msg_id: str,
    preview_text: str = "",
    append_system_prompt: str = "",
):
    """调用 Claude 并流式展示结果，检测选项时附加按钮。消息处理和按钮回复共用此函数。"""
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
            print(f"[{bot.profile.name}][warn] push 失败 ({push_failures}/3): {push_err}", flush=True)

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
                print(f"[{bot.profile.name}][Plan] EnterPlanMode 检测到，切换为 plan", flush=True)
                await bot.store.set_permission_mode(user_id, chat_id, "plan")
            return
        if name.lower() == "enterworktree" and inp:
            wt_name = inp.get("name", "")
            if wt_name:
                print(f"[{bot.profile.name}][Worktree] 进入: {wt_name}", flush=True)
            return
        if name.lower() == "exitworktree":
            print(f"[{bot.profile.name}][Worktree] 退出", flush=True)
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
    try:
        print(f"[{bot.profile.name}][run_claude] 开始调用...", flush=True)
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
        print(f"[{bot.profile.name}][run_claude] 完成, session={new_session_id}", flush=True)
    except Exception as e:
        if active_run.stop_requested:
            return
        print(f"[{bot.profile.name}][error] Claude 运行失败: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        try:
            await bot.feishu.update_card(card_msg_id, f"❌ Claude 执行出错：{type(e).__name__}: {e}")
        except Exception:
            pass
        return
    finally:
        heartbeat_task.cancel()
        bot.active_runs.clear_run(user_id, chat_id, active_run)

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
        print(f"[{bot.profile.name}][error] 卡片更新失败，回退发文本: {e}", flush=True)
        try:
            if is_group and notify_msg_id:
                await bot.feishu.reply_card(notify_msg_id, content=final, loading=False)
            else:
                await bot.feishu.send_text_to_user(user_id, final)
        except Exception as fallback_err:
            print(f"[{bot.profile.name}][error] 文本回退也失败: {fallback_err}", flush=True)

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
        print(f"[{bot.profile.name}][Plan] ExitPlanMode 检测到，切换为 bypassPermissions", flush=True)
        await bot.store.set_permission_mode(user_id, chat_id, "bypassPermissions")
        try:
            notice = "🚀 已退出规划模式，发送任意消息开始执行。"
            if is_group and notify_msg_id:
                await bot.feishu.reply_text(notify_msg_id, notice)
            else:
                await bot.feishu.send_text_to_user(user_id, notice)
        except Exception:
            pass


async def _process_message(bot: BotInstance, user_id: str, chat_id: str, is_group: bool, thread_id: str, msg):
    """实际处理消息的逻辑，在 per-chat lock 保护下执行"""
    tag = bot.profile.name
    print(
        f"[{tag}][处理消息] user={user_id[:8]}... chat={chat_id[:10]}... "
        f"thread={thread_id[:10] if thread_id else '-'} is_group={is_group}",
        flush=True,
    )
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
        print(f"[{tag}][文本] {text[:50] if text else '(空)'}", flush=True)

    elif msg.message_type == "image":
        try:
            image_key = json.loads(msg.content).get("image_key", "")
            if not image_key:
                return
            img_path = await bot.feishu.download_image(msg.message_id, image_key)
            text = f"[用户发送了一张图片，路径：{img_path}，请读取并分析这张图片，直接回复用中文]"
            preview_text = "[图片]"
        except Exception as e:
            print(f"[{tag}][error] 下载图片失败: {e}")
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
            print(f"[{tag}][error] 下载文件失败: {e}")
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
                print(f"[{tag}][error] 下载 post 图片失败 key={ik[:8]}...: {e}", flush=True)

        if not post_text and not img_paths:
            print(f"[{tag}][post] 空内容，忽略", flush=True)
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

        print(f"[{tag}][post] text_len={len(post_text)} imgs={len(img_paths)}", flush=True)

    else:
        return

    # ── 斜杠命令 ──────────────────────────────────────────────
    parsed = parse_command(text)
    if parsed:
        cmd, args = parsed
        print(f"[{tag}][cmd] 执行命令 {cmd}", flush=True)
        reply = await handle_command(cmd, args, user_id, chat_id, bot.store)
        if reply is not None:
            if isinstance(reply, dict):
                reply_text, reply_buttons = reply["text"], reply.get("buttons", [])
            else:
                reply_text, reply_buttons = reply, []

            # 给每个按钮带上 profile，用于后续回调路由
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
                    print(f"[{tag}][按钮] 失败: {btn_err}", flush=True)
            else:
                if is_group:
                    await bot.feishu.reply_card(msg.message_id, content=reply_text, loading=False)
                else:
                    await bot.feishu.send_card_to_user(user_id, content=reply_text, loading=False)
            return

    # ── 普通消息 → 调用 Claude ──────────────────────────────
    session = await bot.store.get_current(user_id, chat_id)
    print(f"[{tag}][Claude] session={session.session_id} model={session.model}", flush=True)

    if thread_id:
        try:
            last_seen = await bot.store.get_last_seen(user_id, chat_id)
            context_block, ctx_paths = await build_thread_context(
                bot.feishu, thread_id, last_seen, msg.message_id,
            )
            if context_block:
                print(
                    f"[{tag}][thread] 注入上下文 last_seen={last_seen[:12] if last_seen else '-'}, "
                    f"附件={len(ctx_paths)}",
                    flush=True,
                )
                if text.strip():
                    text = f"{context_block}\n\n【用户刚刚 @ 你并说】\n{text}"
                else:
                    text = f"{context_block}\n\n【用户刚刚 @ 你，没有新正文，请基于上方内容回复】"
            await bot.store.set_last_seen(user_id, chat_id, msg.message_id)
        except Exception as e:
            print(f"[{tag}][thread] 构建上下文失败（继续处理当前消息）: {e}", flush=True)

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
        print(f"[{tag}][error] 发送占位卡片失败: {e}", flush=True)
        if is_group:
            try:
                await bot.feishu.reply_card(msg.message_id, content=f"❌ 发送消息失败：{e}", loading=False)
            except Exception:
                pass
        else:
            await bot.feishu.send_text_to_user(user_id, f"❌ 发送消息失败：{e}")
        return

    raw_chat_id = chat_id.split(":", 1)[0] if ":" in chat_id else chat_id
    lark_sys = _build_lark_system_prompt(bot.profile, raw_chat_id, thread_id, msg.message_id, is_group)

    await _run_and_display(
        bot,
        user_id, chat_id, is_group, text, card_msg_id, session, msg.message_id,
        preview_text=preview_text,
        append_system_prompt=lark_sys,
    )


def _context_window_for(model: str) -> int:
    m = (model or "").lower()
    if "[1m]" in m or "1m" in m:
        return 1_000_000
    return 200_000


def _build_lark_system_prompt(
    profile: Profile,
    raw_chat_id: str,
    thread_id: str,
    user_message_id: str,
    is_group: bool,
) -> str:
    """构造注入到 Claude 的 Lark 语境系统提示，含 profile 名（决定 lark-cli --profile）。"""
    brand = profile.brand_label
    cli_profile = profile.lark_cli_profile or profile.name

    location_lines = [f"- chat_id: {raw_chat_id}"]
    if thread_id:
        location_lines.append(f"- thread_id: {thread_id}（话题群 / topic thread）")
    location_lines.append(f"- 用户刚发的消息 id: {user_message_id}")
    location_lines.append(f"- 场景: {'群聊' if is_group else '私聊'}")
    location_lines.append(f"- 平台: {brand}（domain: {profile.domain}）")
    location_lines.append(f"- 对应 lark-cli profile: **{cli_profile}**")

    reply_flag = "--reply-in-thread " if thread_id else ""
    profile_flag = f"--profile {cli_profile} "
    reply_cmd_text = (
        f'lark-cli {profile_flag}im +messages-reply --as bot --message-id {user_message_id} '
        f'{reply_flag}--text "<文本>"'
    )
    reply_cmd_image = (
        f'cd <文件所在目录> && lark-cli {profile_flag}im +messages-reply --as bot '
        f'--message-id {user_message_id} {reply_flag}--image <相对路径>'
    )
    reply_cmd_file = (
        f'cd <文件所在目录> && lark-cli {profile_flag}im +messages-reply --as bot '
        f'--message-id {user_message_id} {reply_flag}--file <相对路径>'
    )
    create_doc = (
        f'lark-cli {profile_flag}docs +create --as user '
        f'--title "<简短标题>" --markdown "<完整内容>"'
    )

    return f"""你正在通过{brand}与用户对话。你输出的文本由后台 bot 渲染成卡片发到用户的聊天里。除此之外，你可以主动调用 `lark-cli` 往当前会话发送图片、文件、文档链接。

【当前会话信息】
{chr(10).join(location_lines)}

【⚠️ 多账号注意】
本机 lark-cli 配置了多个 profile（不同租户 / 不同 bot 账号）。本次对话绑定到 profile **{cli_profile}**（{brand}）。**每一条 lark-cli 命令都必须显式加 `--profile {cli_profile}`**，否则会发到错的租户里。不要依赖当前默认 profile。

【何时主动调用 lark-cli】

1. 用户让你"发/截图/把X发过来/发文件"等 → 用 lark-cli 把文件/图片发到评论区：
   ```
   {reply_cmd_image}
   {reply_cmd_file}
   ```
   ⚠️ lark-cli 要求相对路径，**必须先 `cd` 到文件目录，再用文件名调用**，不能直接用绝对路径。

2. 你的回复内容偏长（估计超 40 行或 2000 字），比如大段审计报告、SQL 结果、长列表、多文件分析总结 → **先创建文档，再把链接回给用户**：
   ```
   {create_doc}
   ```
   拿到 doc_url 后，你只在文字回复里写一两句摘要 + 链接。**不要把长内容铺满卡片**。

3. 代码片段（< 30 行）、简短回答、状态更新 → 直接在文字里回复即可，不需要 lark-cli。

【额外提示】
- 如果要发文本消息到评论区（不是作为你当前回复的一部分），用：`{reply_cmd_text}`
- lark-cli 调用是你主动发送一条新消息，和你当前这条回复是独立的。
- 用户可能说中文或英文，保持和用户相同语言回复。

【⚠️ 运行环境约束（重要）】
你运行在一次性 bot 进程里（`claude --print`），没有持久 runtime，也没有定时器。
- **不要调用 `ScheduleWakeup`**：在本环境里它不会被执行，也不会真的唤醒你。
- **不要向用户承诺"X 分钟后自动继续 / 自动检查 / 自动唤醒"**：后台没人接这种信号，会变成空头支票。需要后续跟进就明确告诉用户"请再发一条消息（比如『继续』）触发下一轮"。
- **禁止运行阻塞式长驻命令**：`tail -f`、`tail -F`、`watch`、`journalctl -f`、`kubectl logs -f`、`npm run dev`、`nc -l`、交互式 REPL 等不会自己退出的命令会把 bot 卡住。**单轮有 20 分钟 wall-clock 硬上限**，超了会被强杀、本轮所有进度丢失。
  - 看日志用一次性快照：`tail -n 200 <file>` / `grep` / `sed -n '1,200p'`。
  - 等服务就绪用**带超时**的轮询：`curl --max-time 5 ...`、`timeout 10 <cmd>`，不要 `-f/-F` 盯流。
  - 调用别人封装的 `make` 目标/脚本前，先看清内部有没有 `-f / --follow / watch / tail -F` —— 从表面看很正常、实际死循环的坑主要出在这里（例：`make deploy-logs` 内部是 `tail -F`）。
"""


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

def _resolve_bot_from_value(value: dict) -> BotInstance | None:
    """从按钮 value.profile 取 BotInstance；兼容老卡片（无 profile 字段）时取第一个 profile。"""
    name = value.get("profile") if isinstance(value, dict) else None
    if name and name in _bots:
        return _bots[name]
    # 老卡片 fallback：只有一个 profile 时用它，否则放弃
    if len(_bots) == 1:
        return next(iter(_bots.values()))
    return None


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """用户点击卡片按钮（SDK 回调路径，长连接模式下使用）"""
    global _last_event
    _last_event = time.time()

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
            asyncio.run_coroutine_threadsafe(_handle_set_mode(bot, user_id, chat_id, mode, clicked_msg_id), _bot_loop)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "success"
        toast.content = f"已切换: {mode}"
        resp.toast = toast
        return resp

    if action_type == "run_cmd":
        cmd_text = value.get("cmd", "")
        if cmd_text:
            asyncio.run_coroutine_threadsafe(_handle_menu_command(bot, user_id, chat_id, cmd_text, clicked_msg_id), _bot_loop)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = cmd_text
        resp.toast = toast
        return resp

    if action_type == "resume_session":
        sid = value.get("sid", "")
        if sid:
            asyncio.run_coroutine_threadsafe(_handle_resume_session(bot, user_id, chat_id, sid, clicked_msg_id), _bot_loop)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = "正在恢复..."
        resp.toast = toast
        return resp

    reply_text = value.get("reply", "")
    if reply_text:
        asyncio.run_coroutine_threadsafe(_handle_button_reply(bot, user_id, chat_id, reply_text, clicked_msg_id), _bot_loop)

    resp = P2CardActionTriggerResponse()
    toast = CallBackToast()
    toast.type = "info"
    toast.content = f"已发送: {reply_text}"
    resp.toast = toast
    return resp


async def _handle_menu_command(bot: BotInstance, user_id: str, chat_id: str, cmd_text: str, card_msg_id: str):
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
            print(f"[{bot.profile.name}][error] 菜单命令卡片更新失败: {e}", flush=True)


async def _handle_resume_session(bot: BotInstance, user_id: str, chat_id: str, session_id: str, card_msg_id: str):
    sid, old_title = await bot.store.resume_session(user_id, chat_id, session_id)
    if not sid:
        print(f"[{bot.profile.name}][resume] 未找到 session: {session_id[:8]}", flush=True)
        return
    print(f"[{bot.profile.name}][resume] 已恢复 session: {sid[:8]}", flush=True)
    if card_msg_id:
        try:
            name = bot.store.get_summary(user_id, sid) or f"#{sid[:8]}"
            text = f"✅ 已恢复会话「{name}」，继续对话吧。"
            if old_title:
                text += f"\n上个会话：「{old_title}」"
            await bot.feishu.update_card(card_msg_id, text)
        except Exception:
            pass


async def _handle_set_mode(bot: BotInstance, user_id: str, chat_id: str, mode: str, card_msg_id: str):
    from commands import VALID_MODES
    await bot.store.set_permission_mode(user_id, chat_id, mode)
    desc = VALID_MODES.get(mode, "")
    print(f"[{bot.profile.name}][模式切换] user={user_id[:8]}... mode={mode}", flush=True)
    if card_msg_id:
        try:
            await bot.feishu.update_card(card_msg_id, f"✅ 已切换为 **{mode}**\n{desc}")
        except Exception:
            pass


async def _handle_button_reply(bot: BotInstance, user_id: str, chat_id: str, text: str, clicked_msg_id: str):
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
                print(f"[{bot.profile.name}][error] 按钮回复占位卡片失败: {e}", flush=True)
                return
            raw_chat_id, _, btn_thread_id = chat_id.partition(":")
            lark_sys = _build_lark_system_prompt(
                bot.profile, raw_chat_id, btn_thread_id, clicked_msg_id or "", is_group,
            )
            await _run_and_display(
                bot,
                user_id, chat_id, is_group, text,
                card_msg_id, session, clicked_msg_id or "",
                append_system_prompt=lark_sys,
            )
        except Exception as e:
            print(f"[{bot.profile.name}][error] 按钮回复处理异常: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc(file=sys.stdout)


# ── CLI Handover ─────────────────────────────────────────────

async def _handle_handover(
    session_id: str, cwd: str, model: str,
    profile_name: str = "", target_user: str = "", target_chat: str = "",
) -> dict:
    """处理来自 CLI 的 handover 请求。profile_name 为空时自动选第一个。"""
    bot: BotInstance | None = None
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
        print(f"[{bot.profile.name}][handover] 推送通知失败: {e}", flush=True)

    print(f"[{bot.profile.name}][handover] session={session_id[:8]}... cwd={display_cwd}", flush=True)
    return {"ok": True, "profile": bot.profile.name, "user_id": user_id, "session_id": session_id}


# ── 卡片回调 HTTP 服务 ───────────────────────────────────────

class _CardCallbackHandler(BaseHTTPRequestHandler):
    """处理飞书/Lark 卡片按钮点击的 HTTP 回调"""

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            self._respond(400, {"error": "bad json"})
            return

        if data.get("type") == "url_verification":
            self._respond(200, {"challenge": data.get("challenge", "")})
            return

        event = data.get("event", {})
        operator = event.get("operator", {})
        user_id = operator.get("open_id", "")
        action = event.get("action", {})
        value = action.get("value", {}) or {}
        context = event.get("context", {})

        action_type = value.get("action", "")
        chat_id = value.get("cid", user_id)
        clicked_msg_id = context.get("open_message_id", "")

        bot = _resolve_bot_from_value(value)
        if bot is None:
            self._respond(200, {"toast": {"type": "warning", "content": "按钮已过期"}})
            return

        print(f"[{bot.profile.name}][HTTP回调] user={user_id[:8]}... action={action_type or 'reply'}", flush=True)

        if action_type == "set_mode":
            mode = value.get("mode", "")
            if mode:
                asyncio.run_coroutine_threadsafe(
                    _handle_set_mode(bot, user_id, chat_id, mode, clicked_msg_id),
                    _bot_loop,
                )
            self._respond(200, {"toast": {"type": "success", "content": f"已切换: {mode}"}})
        elif action_type == "run_cmd":
            cmd_text = value.get("cmd", "")
            if cmd_text:
                asyncio.run_coroutine_threadsafe(
                    _handle_menu_command(bot, user_id, chat_id, cmd_text, clicked_msg_id),
                    _bot_loop,
                )
            self._respond(200, {"toast": {"type": "info", "content": cmd_text}})
        elif action_type == "resume_session":
            sid = value.get("sid", "")
            if sid:
                asyncio.run_coroutine_threadsafe(
                    _handle_resume_session(bot, user_id, chat_id, sid, clicked_msg_id),
                    _bot_loop,
                )
            self._respond(200, {"toast": {"type": "info", "content": "正在恢复..."}})
        else:
            reply_text = value.get("reply", "")
            if reply_text:
                asyncio.run_coroutine_threadsafe(
                    _handle_button_reply(bot, user_id, chat_id, reply_text, clicked_msg_id),
                    _bot_loop,
                )
            self._respond(200, {"toast": {"type": "info", "content": f"已发送: {reply_text}"}})

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)

        if parsed.path == "/handover":
            params = parse_qs(parsed.query)
            session_id = params.get("session_id", [""])[0]
            cwd = params.get("cwd", [""])[0]
            model = params.get("model", [""])[0]
            profile_name = params.get("profile", [""])[0]
            target_user = params.get("user_id", [""])[0]
            target_chat = params.get("chat_id", [""])[0]

            if not session_id:
                self._respond(400, {"error": "session_id required"})
                return

            try:
                future = asyncio.run_coroutine_threadsafe(
                    _handle_handover(session_id, cwd, model, profile_name, target_user, target_chat),
                    _bot_loop,
                )
                result = future.result(timeout=15)
                self._respond(200, result)
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


# ── 后台定时摘要生成 ─────────────────────────────────────────

def _bg_summary_thread():
    """扫描所有 profile 下未摘要的会话，逐个生成摘要"""
    time.sleep(60)
    while True:
        try:
            for bot in _bots.values():
                unsummarized = bot.store.get_all_unsummarized()
                if not unsummarized:
                    continue
                print(f"[{bot.profile.name}][摘要] 发现 {len(unsummarized)} 个未摘要会话", flush=True)
                count = 0
                for user_id, sid in unsummarized[:5]:
                    try:
                        summary = generate_summary(sid)
                        if summary:
                            bot.store._data.setdefault(user_id, {}).setdefault("summaries", {})[sid] = summary
                            _write_custom_title(sid, summary)
                            count += 1
                            print(f"[{bot.profile.name}][摘要] #{sid[:8]} → {summary}", flush=True)
                    except Exception as e:
                        print(f"[{bot.profile.name}][摘要] #{sid[:8]} 失败: {e}", flush=True)
                    time.sleep(5)
                if count:
                    bot.store._save()
        except Exception as e:
            print(f"[摘要] 定时任务异常: {e}", flush=True)
        time.sleep(600)


def _start_callback_server(port):
    server = HTTPServer(('0.0.0.0', port), _CardCallbackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()


def _start_ngrok(port):
    """启动 ngrok 隧道，返回公网 URL"""
    import subprocess
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
            tunnels = json.loads(r.read())
            for t in tunnels.get("tunnels", []):
                if t.get("proto") == "https":
                    return t["public_url"]
    except Exception:
        pass

    try:
        ngrok_domain = os.environ.get("NGROK_DOMAIN", "")
        ngrok_cmd = ["ngrok", "http", "--url", ngrok_domain, str(port)] if ngrok_domain else ["ngrok", "http", str(port)]
        subprocess.Popen(
            ngrok_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=5) as r:
            tunnels = json.loads(r.read())
            for t in tunnels.get("tunnels", []):
                if t.get("proto") == "https":
                    return t["public_url"]
    except Exception as e:
        print(f"   [warn] ngrok 启动失败: {e}", flush=True)
    return None


# ── 启动每个 profile 的 WebSocket 客户端 ──────────────────────

def _start_profile_ws(bot: BotInstance):
    """为一个 profile 启动独立的 WebSocket 客户端（跑在单独线程里，阻塞调用）。"""
    def _on_message(data: P2ImMessageReceiveV1) -> None:
        global _last_event
        _last_event = time.time()
        asyncio.run_coroutine_threadsafe(handle_message_async(bot, data), _bot_loop)

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(_on_message)
        .register_p2_card_action_trigger(on_card_action)
        .register_p2_im_message_message_read_v1(lambda _e: None)
        .build()
    )

    ws_client = lark.ws.Client(
        bot.profile.app_id,
        bot.profile.app_secret,
        event_handler=handler,
        domain=bot.profile.domain,
        log_level=lark.LogLevel.INFO,
    )

    def _run():
        # 给这条 WS 线程分配独立的 asyncio 事件循环；lark_oapi 内部的
        # 模块级 `loop` 代理会据此分发，不同 profile 的 ws.Client 互不打架。
        asyncio.set_event_loop(asyncio.new_event_loop())
        ws_client.start()

    t = threading.Thread(
        target=_run,
        daemon=True,
        name=f"ws-{bot.profile.name}",
    )
    t.start()
    print(f"   [{bot.profile.name}] WS 客户端已启动 ({bot.profile.brand_label} · {bot.profile.domain})")


# ── 启动 ──────────────────────────────────────────────────────

def main():
    print("🚀 飞书/Lark Claude Bot 启动中...")
    print(f"   已加载 {len(config.PROFILES)} 个 profile")
    for p in config.PROFILES:
        allow_desc = f"{len(p.allowed_open_ids)} 人" if p.allowed_open_ids else "⚠️ 所有人"
        group_desc = (
            f"{len(p.allowed_group_chat_ids)} 群" if p.allowed_group_chat_ids else "禁用"
        )
        print(
            f"   · {p.name:<8} {p.brand_label}  "
            f"app={p.app_id}  cwd={p.default_cwd}  "
            f"allow={allow_desc}  groups={group_desc}  "
            f"lark-cli profile={p.lark_cli_profile}"
        )
    print(f"   默认模型    : {config.DEFAULT_MODEL}")
    print(f"   权限模式    : {config.PERMISSION_MODE}")

    # 构建 BotInstance
    for p in config.PROFILES:
        _bots[p.name] = BotInstance(p)

    # 卡片回调 HTTP 服务 + ngrok 隧道（所有 profile 共用一个端口）
    cb_port = config.CALLBACK_PORT
    _start_callback_server(cb_port)
    ngrok_url = _start_ngrok(cb_port)
    if ngrok_url:
        print(f"   卡片回调    : {ngrok_url}/callback")
    else:
        print(f"   卡片回调    : http://localhost:{cb_port}/callback (需启动 ngrok)")

    # 启动后台线程
    threading.Thread(target=_watchdog, daemon=True).start()
    threading.Thread(target=_bg_summary_thread, daemon=True).start()

    # 每个 profile 起一个 WS 客户端（单独线程，阻塞等事件）
    print("✅ 连接 WebSocket 长连接（自动重连）...")
    for bot in _bots.values():
        _start_profile_ws(bot)

    # 主线程保持运行，让 _bot_loop 和 WS 线程持续工作
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n⛔ 退出")


if __name__ == "__main__":
    main()
