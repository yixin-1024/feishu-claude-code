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

from bot_config import Profile, normalize_effort, normalize_model
from bot_instance import BotInstance
from card_security import (
    card_action_allowed,
    card_context_matches,
    claim_event,
    verify_action_value,
)
from agent_runner import run_agent
from claude_runner import is_safeguards_error_text
from feishu_client import _err_desc
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

async def _acquire_card_lock(active_run: ActiveRun, timeout: float) -> bool:
    """限时抢卡片锁。抢不到返回 False，让调用方自己决定跳过还是硬写。

    /stop 和 /restart 是"救火通道"，绝不能被一次卡住的卡片请求反锁死——那会让
    整个重启流程停在「中断任务」阶段永远出不来。
    """
    try:
        await asyncio.wait_for(active_run.card_update_lock.acquire(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def _announce_stopped_run(bot: BotInstance, active_run: ActiveRun):
    # 保留停止前流式渲染出的进度（工具轨迹 + 部分回答），仅在末尾追加「已停止」
    # 标记，而不是整卡覆盖成一句"已停止"。与错误路径的"保留旧内容 + 追加"一致。
    body = (getattr(active_run, "last_body", "") or "").strip()
    if body and body != "⏳ 思考中...":
        content = f"{body}\n\n---\n\n⏹ **任务已被停止**（以上为停止前的进度）"
    else:
        content = "⏹ 已停止当前任务（尚无输出）"
    # 抢不到锁也照写：此时 stop_requested 已置位，push() 会自己 return，不存在
    # 被流式帧覆盖的竞态；持锁的那位多半是一次永远回不来的请求。
    locked = await _acquire_card_lock(active_run, _PUSH_TIMEOUT)
    if not locked:
        log(bot.profile.name, "stop", "warn", "卡片锁被占住，改为无锁写停止卡")
    try:
        try:
            await bot.feishu.update_card(active_run.card_msg_id, content)
        except Exception as exc:
            log(bot.profile.name, "stop", "warn", f"update stopped card failed: {exc}")
        try:
            await bot.feishu.finalize_streaming_card(active_run.card_msg_id)
        except Exception as exc:
            log(bot.profile.name, "stop", "warn", f"finalize stopped card failed: {exc}")
    finally:
        if locked:
            active_run.card_update_lock.release()


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
            # 锁被一次挂死的卡片请求占着也要能重启：限时抢，抢不到就无锁写。
            locked = await _acquire_card_lock(r, _RESTART_CARD_LOCK_TIMEOUT)
            if not locked:
                log(prof_name, "restart", "warn",
                    f"卡片锁被占住，改为无锁写中断卡 chat={r.chat_id[:12]}")
            try:
                try:
                    await asyncio.wait_for(
                        b.feishu.update_card(r.card_msg_id, RESTART_MSG),
                        timeout=1.5,
                    )
                except Exception as e:
                    log(prof_name, "restart", "warn",
                        f"update_card 失败 chat={r.chat_id[:12]}: {e}")
                finally:
                    try:
                        # CardKit 注册表会随进程重启丢失；必须在旧进程里关掉
                        # streaming，否则中断卡可能永久停在流式状态。
                        await asyncio.wait_for(
                            b.feishu.finalize_streaming_card(r.card_msg_id),
                            timeout=1.0,
                        )
                    except Exception as e:
                        log(prof_name, "restart", "warn",
                            f"finalize streaming card 失败 chat={r.chat_id[:12]}: {e}")
            finally:
                if locked:
                    r.card_update_lock.release()
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


_RESTART_NOTICE_TIMEOUT_SECONDS = 3.0
_RESTART_DRAIN_TIMEOUT_SECONDS = 4.0
_restart_in_progress = False
_restart_committed = False


def _active_run_count() -> int:
    """返回所有 profile 当前 active run 数量，供重启前即时回执使用。"""
    total = 0
    for b in list(_bots.values()):
        runs = getattr(getattr(b, "active_runs", None), "_runs", None)
        if runs is not None:
            try:
                total += len(runs)
            except TypeError:
                pass
    return total


async def _send_restart_notice(
    bot: BotInstance,
    user_id: str,
    is_group: bool,
    message_id: str,
    content: str,
    *,
    card_msg_id: str = "",
) -> bool:
    """在动任何 runner 之前发送重启提醒；失败可观测，但不阻断用户的重启意图。"""

    async def _send():
        if card_msg_id:
            await bot.feishu.update_card(card_msg_id, content)
        elif is_group and message_id:
            await bot.feishu.reply_text(message_id, content)
        elif user_id:
            await bot.feishu.send_text_to_user(user_id, content)
        else:
            raise RuntimeError("restart notice 缺少 message_id/user_id")

    try:
        await asyncio.wait_for(_send(), timeout=_RESTART_NOTICE_TIMEOUT_SECONDS)
        log(bot.profile.name, "restart", "info", "重启提醒已发送")
        return True
    except Exception as exc:
        anchor = message_id or card_msg_id or "-"
        log(bot.profile.name, "restart", "warn",
            f"重启提醒发送失败 anchor={anchor[:14]}，仍继续重启: {exc}")
        return False


async def _handle_restart_request(
    bot: BotInstance,
    user_id: str,
    is_group: bool,
    message_id: str,
    *,
    card_msg_id: str = "",
) -> bool:
    """统一编排 /restart：先通知，再立即中断任务，最后触发 supervisor 重拉。"""
    global _restart_in_progress, _restart_committed
    from commands import _trigger_restart, restart_strategy

    # 同一 event loop 内无 await 地 check-and-set，避免两个 /restart 重复 kickstart；
    # 同时阻止“active run 快照之后、进程真正退出之前”再启动新 runner。
    if _restart_in_progress:
        await _send_restart_notice(
            bot, user_id, is_group, message_id,
            "♻️ 重启请求已经在处理中，请稍候。",
            card_msg_id=card_msg_id,
        )
        return True
    _restart_in_progress = True

    async def _restart_step(awaitable):
        """Reset the restart gate if the orchestration task is cancelled."""
        global _restart_in_progress, _restart_committed
        try:
            return await awaitable
        except asyncio.CancelledError:
            _restart_in_progress = False
            _restart_committed = False
            log(bot.profile.name, "restart", "warn", "重启编排被取消，已重置全局闸门")
            raise

    affected = _active_run_count()
    task_note = (
        f"正在中断 {affected} 个未完成任务"
        if affected else "当前没有未完成任务"
    )
    await _restart_step(
        _send_restart_notice(
            bot, user_id, is_group, message_id,
            f"♻️ 收到，正在立即重启：{task_note}；约 3-10 秒恢复。",
            card_msg_id=card_msg_id,
        )
    )

    # supervisor 探测含同步 launchctl；放到线程里，至少不阻塞其它 profile 的
    # event loop。提醒已先发出，因此异常慢探测也不会再表现成“完全没回复”。
    try:
        strat = await _restart_step(asyncio.to_thread(restart_strategy))
    except Exception as exc:
        _restart_in_progress = False
        _restart_committed = False
        log(bot.profile.name, "restart", "error", f"探测 supervisor 失败: {exc}")
        await _restart_step(
            _send_restart_notice(
                bot, user_id, is_group, message_id,
                f"❌ 无法确认重启方式，已取消：{exc}",
                card_msg_id=card_msg_id,
            )
        )
        return False

    if strat == "bare":
        _restart_in_progress = False
        _restart_committed = False
        await _restart_step(
            _send_restart_notice(
                bot, user_id, is_group, message_id,
                "❌ 没找到 supervisor（非 launchd/systemd 任务、无 .app），"
                "直接退出会停服，已取消重启。",
                card_msg_id=card_msg_id,
            )
        )
        return False

    # 只有确认 supervisor 能重拉后才把请求升级为“已提交重启”。
    # 探测失败/bare 期间恰好完成的正常任务仍应该能够落卡并发送
    # 结果；否则重启最终取消了，用户的结果却已经被吞掉。
    _restart_committed = True

    try:
        await _restart_step(
            asyncio.wait_for(
                _handle_restart_command(bot),
                timeout=_RESTART_DRAIN_TIMEOUT_SECONDS,
            )
        )
    except asyncio.TimeoutError:
        log(bot.profile.name, "restart", "warn",
            f"中断 active runs 超过 {_RESTART_DRAIN_TIMEOUT_SECONDS:.0f}s，继续重启")
    except Exception as exc:
        log(bot.profile.name, "restart", "warn", f"中断 active runs 异常，继续重启: {exc}")

    try:
        _trigger_restart()
        return True
    except Exception as exc:
        _restart_in_progress = False
        _restart_committed = False
        log(bot.profile.name, "restart", "error", f"触发 supervisor 重启失败: {exc}")
        await _restart_step(
            _send_restart_notice(
                bot, user_id, is_group, message_id,
                f"❌ 触发重启失败：{exc}",
                card_msg_id=card_msg_id,
            )
        )
        return False


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
            cli_profile=bot.profile.lark_cli_profile or bot.profile.name,
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


def _env_int(name: str, default: int) -> int:
    """读 env 里的非负整数，脏值 / 空值一律回落 default（别让配置笔误炸掉一轮对话）。"""
    try:
        return max(0, int(str(os.environ.get(name, "")).strip()))
    except (TypeError, ValueError):
        return default


# ── 上游中断自动续跑预算 ───────────────────────────────────
# 上游中断（流被掐断 / 5xx / 连接关闭 / CLI 进程炸）比 hung 安全得多——崩溃前的
# session 干净，resume 同一会话就能接着跑，所以给独立且更宽的预算，冷却递增避免
# 上游正在抖动时连撞。次数可用 CC_LARK_STALL_RETRY_MAX 调（0 = 关掉自动续跑）。
_STALL_RETRY_MAX_DEFAULT = 3
_STALL_COOLDOWNS = (10, 30, 60)

# ── 卡片推送的看门狗 ───────────────────────────────────────
# 单帧（含 SDK 内部重试）的硬上限；超时就放弃这一帧并把锁还回去。必须大于
# 一次 SDK 请求的 timeout，否则正常网络下的慢请求会被误判。
_PUSH_TIMEOUT = 20.0
# 连续失败到这个数就静音一段时间，而不是永久关掉本 run 的卡片刷新。
_PUSH_FAILURE_LIMIT = 3
_PUSH_MUTE_SECONDS = 30.0
# /restart 抢卡片锁的上限：重启要快，抢不到就无锁写，别让一个 run 拖住全局。
_RESTART_CARD_LOCK_TIMEOUT = 1.5

# 上游中断后 resume 续跑用的提示：既补全被截断的回复，又明令别重复已做的副作用。
_STALL_RESUME_NUDGE = (
    "继续。上一轮回复在流式返回时被上游中断（API Error：连接中断 / 响应截断），"
    "请基于已经完成的工作补全最终回复；"
    "不要重复执行上一轮已经做过的写操作 / 命令 / 文件改动。"
)
# 写操作场景（开户 / 发卡 / 部署这类有外部副作用的）不能盲目续跑，但也不该直接
# 断在半路——改成「先核实再继续」，把幂等判断交给带完整上下文的同一个会话。
_STALL_RESUME_NUDGE_WRITE = (
    "继续。上一轮回复在流式返回时被上游中断（API Error：连接中断 / 响应截断）。"
    "本轮涉及写操作 / 外部接口调用：请先核实上一轮最后一步是否已经生效"
    "（查库 / 查接口返回 / 看已有记录），已经生效的绝对不要重复执行，"
    "只补做剩下的步骤，然后给出最终回复。"
)


# ── 模型 safeguards 拦截自动降级 ──────────────────────────────
# Fable 5 的 safeguards 拦截（"...safeguards flagged this message... Try
# rephrasing the request in a new session or change your model."）对同一模型
# 重试 / 续跑必然复现，重试无益。命中时不走 stall 重试，直接把本对话模型切到
# 降级模型（默认 opus[1m]，env CC_LARK_SAFEGUARDS_FALLBACK_MODEL 可改），
# resume 同一 session 接着跑，并显式通知用户「因为这个错误已切换模型」。
_SAFEGUARDS_FALLBACK_MODEL_DEFAULT = "opus[1m]"


def _safeguards_fallback_model() -> str:
    raw = os.environ.get("CC_LARK_SAFEGUARDS_FALLBACK_MODEL", "").strip()
    # 支持 /model 同款别名（opus / sonnet / …）
    return normalize_model(raw) or _SAFEGUARDS_FALLBACK_MODEL_DEFAULT


_SAFEGUARDS_RESUME_NUDGE = (
    "继续。上一轮消息被模型 safeguards 误拦截，已自动切换模型接管本会话；"
    "请基于已经完成的工作继续任务并给出最终回复，"
    "不要重复执行上一轮已经做过的写操作 / 命令 / 文件改动。"
)
_SAFEGUARDS_RESUME_NUDGE_WRITE = (
    "继续。上一轮消息被模型 safeguards 误拦截，已自动切换模型接管本会话。"
    "本轮涉及写操作 / 外部接口调用：请先核实上一轮最后一步是否已经生效"
    "（查库 / 查接口返回 / 看已有记录），已经生效的绝对不要重复执行，"
    "只补做剩下的步骤，然后给出最终回复。"
)


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
        {"text": "🧠 推理强度",    "value": {"action": "run_cmd", "cmd": "/effort"}},
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
            value = {
                **btn["value"],
                "cid": chat_id,
                "profile": bot.profile.name,
                "_cc_uid": user_id,
            }
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
        if (
            is_group
            and "*" not in bot.profile.allowed_group_chat_ids
            and raw_chat_id not in bot.profile.allowed_group_chat_ids
        ):
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

        # 人工 /stop 永远只表示取消，不能把同一条消息后面的文字隐式当成
        # "停止并续跑"指令。实时纠偏仍由显式 MCP steer_task 提供。
        # 精确匹配命令前缀，避免"怎么让你 /stop"这类普通句子误触发。
        _parsed_stop = parse_command(_text)
        if _parsed_stop and _parsed_stop[0] == "stop":
            if is_group and not await _is_current_bot_mentioned(bot, msg):
                return
            _instr = _parsed_stop[1]
            reply = await _handle_stop_command(bot, user_id, chat_id)
            if _instr:
                reply += "\n\nℹ️ `/stop` 后的文字不会自动执行；如需新任务，请另发一条消息。"
            if is_group:
                await bot.feishu.reply_card(msg.message_id, content=reply, loading=False)
            else:
                await bot.feishu.send_card_to_user(user_id, content=reply, loading=False)
            return

        _parsed_lock_free = parse_command(_text)
        if _parsed_lock_free and _parsed_lock_free[0] == "restart":
            if is_group and not await _is_current_bot_mentioned(bot, msg):
                return
            await _handle_restart_request(bot, user_id, is_group, msg.message_id)
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
    if _restart_in_progress:
        try:
            await bot.feishu.update_card(
                card_msg_id,
                "♻️ 服务正在重启，本条任务未执行；服务恢复后请重新发送。",
            )
        except Exception:
            pass
        try:
            await bot.feishu.finalize_streaming_card(card_msg_id)
        except Exception:
            pass
        return

    active_run = bot.active_runs.start_run(user_id, chat_id, card_msg_id)

    def _stopping() -> bool:
        # committed gate is raised once a supervisor is confirmed, before active
        # runs are drained. Re-check it after every potentially slow Lark request
        # so an old run cannot append a success/error notification after restart.
        return active_run.stop_requested or _restart_committed

    # cc-lark MCP 的会话上下文。透传给 run_agent → claude 的 extra_env，MCP
    # server 用这些默认值把 send_text / schedule_wakeup 定向到当前 Lark 话题。
    wake_context: Optional[dict] = None
    raw_chat_id, _, thread_id = chat_id.partition(":")
    if is_group:
        cli_profile = bot.profile.lark_cli_profile or bot.profile.name
        callback_port = str(os.getenv("CALLBACK_PORT", "9981"))
        control_port = str(os.getenv(
            "CC_LARK_CONTROL_PORT",
            os.getenv("CONTROL_PORT", str(int(callback_port) + 1)),
        ))
        wake_context = {
            "CC_LARK_PROFILE_NAME": bot.profile.name,
            "CC_LARK_CLI_PROFILE": cli_profile,
            "CC_LARK_CHAT_ID": raw_chat_id,
            "CC_LARK_THREAD_ID": thread_id,
            "CC_LARK_MESSAGE_ID": notify_msg_id or "",
            "CC_LARK_USER_ID": user_id or "",
            "CC_LARK_IS_GROUP": "1",
            "CC_LARK_CONTROL_PORT": control_port,
            "CC_LARK_CONTROL_TOKEN": os.getenv("CC_LARK_CONTROL_TOKEN", ""),
            # Backward-compatible alias for the first wake_context draft. It represented
            # the internal HTTP API, so after the split it must follow the control port.
            "CC_LARK_HTTP_PORT": control_port,
            "CC_LARK_PROFILE": bot.profile.name,
            "CC_LARK_ANCHOR": notify_msg_id or "",
            # Keep the real public callback port available for diagnostics only. New MCP
            # clients use CC_LARK_CONTROL_PORT and never POST control actions here.
            "CC_LARK_CALLBACK_PORT": callback_port,
        }

    accumulated = ""
    tool_history: list[str] = []
    ask_options: list[tuple[str, str]] = []
    plan_exited = False
    final_usage: dict = {}
    last_push_time = 0.0
    push_failures = 0
    push_muted_until = 0.0
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
        """推一帧卡片。三条硬约束，缺一个就会出现"任务还在跑、卡片永久定格"：

        1. 拿锁要有上限 —— 上一帧还挂在网络里时，直接跳过这一帧，绝不能把调用方
           （心跳 / on_text_chunk，后者跑在 runner 的读流循环里）一起堵死。
        2. 单帧要有上限 —— SDK 侧已配 timeout，这里再兜一层，保证锁一定还得回来。
        3. 连续失败只"静音"一段时间，不永久关掉 —— 网络抖 10 秒不该让接下来
           40 分钟的卡片全哑掉（老逻辑 push_failures>=3 后此 run 再不推送）。
        """
        nonlocal push_failures, push_muted_until
        if _stopping():
            return
        if push_failures >= _PUSH_FAILURE_LIMIT and time.time() < push_muted_until:
            return
        try:
            await asyncio.wait_for(
                active_run.card_update_lock.acquire(), timeout=_PUSH_TIMEOUT)
        except asyncio.TimeoutError:
            log(bot.profile.name, "stream", "warn",
                f"push 跳过：上一帧仍未返回（等锁 >{_PUSH_TIMEOUT:.0f}s）")
            return
        try:
            if _stopping():
                return
            await asyncio.wait_for(
                bot.feishu.update_card(card_msg_id, content), timeout=_PUSH_TIMEOUT)
            push_failures = 0
        except Exception as push_err:
            push_failures += 1
            if push_failures >= _PUSH_FAILURE_LIMIT:
                push_muted_until = time.time() + _PUSH_MUTE_SECONDS
            log(bot.profile.name, "stream", "warn",
                f"push 失败 ({push_failures}): {_err_desc(push_err)}"
                + (f"，静音 {_PUSH_MUTE_SECONDS:.0f}s 后重试"
                   if push_failures >= _PUSH_FAILURE_LIMIT else ""))
        finally:
            active_run.card_update_lock.release()

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
        # 快照正文（不含 footer），供 /stop 保留停止前进度用。收到停止后不再
        # 改写快照，避免终止竞态把“自动续跑”分隔等迟到内容混进停止卡。
        if not _stopping():
            active_run.last_body = body

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
        if _stopping():
            return
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
        if _stopping():
            return
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
        if _stopping():
            return
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
                if _stopping():
                    return
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
        # 上游中断续跑预算（次数 env 可调，冷却见模块常量 _STALL_COOLDOWNS）
        _stall_max = _env_int("CC_LARK_STALL_RETRY_MAX", _STALL_RETRY_MAX_DEFAULT)
        retry_count = 0
        stall_count = 0
        safeguards_switched = False
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
                        effort=getattr(session, "effort", None),
                        cwd=session.cwd,
                        permission_mode=session.permission_mode,
                        on_text_chunk=on_text_chunk,
                        on_tool_use=on_tool_use,
                        on_process_start=lambda proc: bot.active_runs.attach_process(user_id, chat_id, proc),
                        on_usage=on_usage,
                        on_status=on_status,
                        should_stop=_stopping,
                        append_system_prompt=append_system_prompt or None,
                        wake_context=wake_context,
                    )
                    # /stop 或 /restart 已接管最终卡片；runner 被 TERM 后即使带着
                    # partial output“成功”返回，也不能再把中断提示覆盖成结果 + ✅。
                    if active_run.stop_requested:
                        return
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
                    is_stall = (
                        isinstance(e, RuntimeError)
                        and getattr(e, "cc_retryable_resume", False) is True
                    )
                    # 用原始用户诉求判定（claude_msg 在续跑后已被换成「继续」提示，
                    # 拿它判会让第二次续跑误判成非写操作场景）
                    blacklisted = _is_write_op_context(text, tool_history)

                    # 上游中断 / 服务端瞬时错误：崩溃前的 session 干净可 --resume，
                    # 直接 resume 同一会话、用「继续」提示补全被截断的最终回复即可恢复
                    # （不像 hung 那样服务端 state 已脏、必须清 session 走 fresh）。
                    # 写操作场景不再直接放弃——有 session 可 resume 时用「先核实再继续」
                    # 的提示续跑（同一会话看得到自己已做过什么，比停在半路更可控）；
                    # 只有连 session 都没有、只能原样重发时才跳过，避免 double-write。
                    resumable = getattr(e, "cc_session_id", None) or session.session_id

                    # ── safeguards 拦截 → 不重试，自动降级模型续跑 ──
                    # 同一模型重发必然再被拦（is_fatal_error_text 已把它挡在 stall
                    # 重试外）。切到降级模型 resume 同一 session 接着跑；override 落
                    # 盘（不动 session），本对话后续轮次也用降级模型，避免复撞。
                    # 一轮只切一次：切完仍被拦（降级模型也拦）就交回用户。
                    is_safeguards = is_safeguards_error_text(str(e))
                    fallback_model = _safeguards_fallback_model()
                    if (
                        is_safeguards
                        and not safeguards_switched
                        and (session.model or "") != fallback_model
                        and (resumable or not blacklisted)
                    ):
                        safeguards_switched = True
                        old_model = session.model
                        session.model = fallback_model
                        if resumable:
                            session.session_id = resumable
                            claude_msg = (
                                _SAFEGUARDS_RESUME_NUDGE_WRITE if blacklisted
                                else _SAFEGUARDS_RESUME_NUDGE
                            )
                        else:
                            # 首轮就被拦、连 session 都没有：原样重发用户诉求
                            claude_msg = text
                        try:
                            await bot.store.set_model_override(
                                user_id, chat_id, fallback_model,
                            )
                        except Exception:
                            pass
                        log(bot.profile.name, "claude", "warn",
                            f"safeguards 拦截，不重试，模型 {old_model} → "
                            f"{fallback_model} 续跑 "
                            f"session={(resumable or 'fresh')[:8]} "
                            f"write_op={blacklisted}")
                        notice = (
                            f"⚠️ 本条消息被 `{old_model}` 的 safeguards 拦截"
                            f"（重试无益），已自动切换模型为 `{fallback_model}` "
                            f"继续本任务（本对话后续也用它，/model default 可恢复）。"
                        )
                        # 清掉被拦那轮流式出来的残片（可能含 CLI 的 "API Error…"
                        # 文本），让降级续跑后的卡片只显示恢复出来的完整回复。
                        accumulated = ""
                        await push(f"🔄 {notice}")
                        # 卡片会被后续流式内容覆盖，额外发一条独立消息显式告知
                        try:
                            if is_group and notify_msg_id:
                                await bot.feishu.reply_text(notify_msg_id, notice)
                            else:
                                await bot.feishu.send_text_to_user(user_id, notice)
                        except Exception:
                            pass
                        if active_run.stop_requested:
                            return
                        last_output_ts = time.time()
                        last_push_time = 0.0
                        start_ts = time.time()
                        current_tool = None
                        pty_warning = None
                        heartbeat_task = asyncio.create_task(_heartbeat())
                        continue

                    if is_stall and stall_count < _stall_max and (
                        resumable or not blacklisted
                    ):
                        stall_count += 1
                        if resumable:
                            session.session_id = resumable
                            claude_msg = (
                                _STALL_RESUME_NUDGE_WRITE if blacklisted
                                else _STALL_RESUME_NUDGE
                            )
                        else:
                            # 无 session 可 resume（首轮就断在建会话前）：原样重发用户诉求
                            claude_msg = text
                        _cds = _STALL_COOLDOWNS or (_COOLDOWN_SECONDS,)
                        cooldown = _cds[min(stall_count - 1, len(_cds) - 1)]
                        log(bot.profile.name, "claude", "warn",
                            f"上游中断，{cooldown}s 后自动续跑 "
                            f"({stall_count}/{_stall_max}) "
                            f"session={(resumable or 'fresh')[:8]} "
                            f"write_op={blacklisted}")
                        # 清掉被中断那轮流式出来的残片（可能含半截回答或 CLI 的
                        # "API Error…" 文本），让续跑后的卡片只显示恢复出来的完整回复。
                        accumulated = ""
                        await push(
                            f"🔄 上游响应中断，{cooldown}s 后自动续跑 "
                            f"({stall_count}/{_stall_max})..."
                        )
                        await asyncio.sleep(cooldown)
                        if active_run.stop_requested:
                            return
                        last_output_ts = time.time()
                        last_push_time = 0.0
                        start_ts = time.time()
                        current_tool = None
                        pty_warning = None
                        heartbeat_task = asyncio.create_task(_heartbeat())
                        continue

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
                        await push(
                            f"🔄 检测到 client hung，{_COOLDOWN_SECONDS}s 冷却后自动重试 "
                            f"({retry_count}/{_AUTO_RETRY_MAX})..."
                        )
                        await asyncio.sleep(_COOLDOWN_SECONDS)
                        if active_run.stop_requested:
                            return
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
                    if is_stall and stall_count >= _stall_max:
                        log(bot.profile.name, "claude", "warn",
                            f"上游中断已自动续跑 {stall_count} 次仍失败，交回用户")
                    elif is_stall and blacklisted and not resumable:
                        log(bot.profile.name, "claude", "warn",
                            "上游中断但无可 resume 的 session 且涉及写操作，跳过自动续跑")
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
            attempts = retry_count + stall_count
            err_brief = (
                f"❌ 自动重试 {attempts} 次后仍失败：{clean}"
                if attempts > 0
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
            async with active_run.card_update_lock:
                if _stopping():
                    return
                try:
                    # 终态（报错）卡同样走确认写，防止停在流式快照（见 update_card_final）。
                    await bot.feishu.update_card_final(card_msg_id, err_card)
                except Exception:
                    pass
                if _stopping():
                    return
                # 流式卡若开着，关掉它恢复交互（非流式/未登记则 no-op，且永不抛）
                await bot.feishu.finalize_streaming_card(card_msg_id)
                if _stopping():
                    return
                # 卡片是 in-place patch，不会触发 Lark 新消息通知。异常退出时额外发一条
                # 独立 ❌ 短消息，与成功路径下的独立 ✅ 对齐。
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

        # 收尾卡片 = 中间过程（去掉工具执行行）+ 分隔 + 最终产出。process 为空
        # （单段自包含回复）时退回只显示干净结论，不给简单回答强加过程区。
        process_text, result_text = _split_process_and_result(accumulated, full_text)
        final = result_text or "（无输出）"
        # 选项只从最终产出里认，别被过程叙述里的候选文本污染。
        options = _extract_options(result_text) or ask_options
        if process_text:
            if len(process_text) > _MAX_STREAM_DISPLAY:
                process_text = (
                    "…（过程较长，仅显示末段）\n\n"
                    + process_text[-_MAX_STREAM_DISPLAY:]
                )
            final = (
                f"🔍 **过程**\n\n{process_text}\n\n"
                f"---\n\n"
                f"📌 **最终结论**\n\n{result_text}"
            )
        if used_fresh_session_fallback:
            final = (
                "⚠️ 无法接续上一轮会话（resume 失败），已自动开新会话继续"
                "——之前的上下文没有带过来。\n\n" + final
            )
        footer = _format_usage_footer(final_usage, session.model)
        if footer:
            final = f"{final}\n\n{footer}"
        card_patched = False
        async with active_run.card_update_lock:
            if _stopping():
                return
            try:
                if options:
                    buttons = [
                        {"text": display, "value": {
                            "reply": value,
                            "cid": chat_id,
                            "profile": bot.profile.name,
                            "_cc_uid": user_id,
                        }}
                        for display, value in options
                    ]
                    short = all(len(b["text"]) <= 10 for b in buttons)
                    # 流式卡：update_card_with_buttons 内部会推最终文本 + 关流式 + 加按钮
                    await bot.feishu.update_card_with_buttons(card_msg_id, final, buttons, flow=short)
                else:
                    # 终态卡走确认写：抗飞书对紧邻两次 patch 的乱序/合并，防止卡片
                    # 停在带 `⏱ 计时` 的流式快照不翻完成态（见 update_card_final）。
                    await bot.feishu.update_card_final(card_msg_id, final)
                    if _stopping():
                        return
                    # 无按钮的流式卡推完最终文本后需手动关流式（非流式则 no-op）
                    await bot.feishu.finalize_streaming_card(card_msg_id)
                if _stopping():
                    return
                card_patched = True
            except Exception as e:
                if _stopping():
                    return
                log(bot.profile.name, "card", "error", f"卡片更新失败，回退发文本: {e}")
                # 卡片更新失败时流式卡可能仍开着，先收尾
                await bot.feishu.finalize_streaming_card(card_msg_id)
                if _stopping():
                    return
                try:
                    if is_group and notify_msg_id:
                        await bot.feishu.reply_card(notify_msg_id, content=final, loading=False)
                    else:
                        await bot.feishu.send_text_to_user(user_id, final)
                except Exception as fallback_err:
                    if _stopping():
                        return
                    log(bot.profile.name, "card", "error", f"文本回退也失败: {fallback_err}")
                    # 卡片 + 文本回退都失败（额度耗尽 / 渲染故障等）：结果落 outbox，绝不丢
                    saved = bot.feishu.save_outbox(
                        final, kind="result", error=str(fallback_err),
                        meta={"chat_id": chat_id, "user": user_id,
                              "card_msg_id": card_msg_id, "session": new_session_id or ""},
                    )
                    if saved:
                        log(bot.profile.name, "outbox", "warn", f"结果已落 outbox: {saved}")

            if card_patched and not _stopping():
                try:
                    if is_group and notify_msg_id:
                        await bot.feishu.reply_text(notify_msg_id, "✅")
                    else:
                        await bot.feishu.send_text_to_user(user_id, "✅")
                except Exception:
                    pass

        if _stopping():
            return

        if new_session_id:
            await bot.store.on_agent_response(
                user_id, chat_id, new_session_id, preview_text or text,
                usage=final_usage or None,
            )

        if _stopping():
            return

        if plan_exited and session.permission_mode == "plan":
            log(bot.profile.name, "plan", "info", "ExitPlanMode 检测到，切换为 bypassPermissions")
            await bot.store.set_permission_mode(user_id, chat_id, "bypassPermissions")
            if _stopping():
                return
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
        # 富文本或带参数的 /restart 不会命中 handle_message_async 的精确文本快路径，
        # 仍必须走同一套“先提醒、再中断、后重拉”编排，不能落到 commands 旧路径。
        if cmd == "restart":
            await _handle_restart_request(bot, user_id, is_group, msg.message_id)
            return
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
                    val["_cc_uid"] = user_id

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
                cli_profile=bot.profile.lark_cli_profile or bot.profile.name,
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


def _split_process_and_result(accumulated: str, result: str) -> tuple[str, str]:
    """把流式累积的 assistant 文字拆成「中间过程」和「最终产出」。

    `accumulated` 是整段跑下来所有 assistant 文字的拼接，尾部通常就是 `result`
    ——最终那条消息本身也是流式吐出来的，所以会重复出现在末尾。把尾部的 result
    抠掉，剩下的就是工具调用之间的叙述（中间过程）。工具执行轨迹（tool_history）
    从来没进 accumulated，所以天然不含"执行指令"那些 🔧 行。

    返回 (process, result)：process 为空表示这是一条自包含的单段回复，收尾时
    照旧只显示干净结论、不加过程区。"""
    proc = (accumulated or "").strip()
    res = (result or "").strip()
    if not proc:
        return "", res
    if not res or proc == res:
        return "", res or proc
    if proc.endswith(res):
        proc = proc[: len(proc) - len(res)].strip()
    return proc, res


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


def on_card_action(bot: BotInstance, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
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
    callback_chat_id = event.context.open_chat_id if event.context else ""

    header = data.header
    source_valid = bool(
        header
        and data.schema == "2.0"
        and header.event_type == "card.action.trigger"
        and header.app_id == bot.profile.app_id
        and value.get("profile") == bot.profile.name
    )
    if not source_valid:
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "warning"
        toast.content = "按钮已过期，请重新操作"
        resp.toast = toast
        return resp

    verified, reason = verify_action_value(
        value,
        bot.profile.app_secret,
        user_id=user_id,
        message_id=clicked_msg_id or "",
    )
    context_valid = card_context_matches(user_id, chat_id, callback_chat_id or "")
    if (
        not verified
        or not context_valid
        or not card_action_allowed(bot.profile, user_id, chat_id)
    ):
        log(bot.profile.name, "card", "warn",
            f"拒绝卡片动作 user={user_id[:8]}... reason={reason or 'acl/replay'}")
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "warning"
        toast.content = "按钮无效或已过期，请重新操作"
        resp.toast = toast
        return resp
    if not claim_event(bot.profile.name, header.event_id or ""):
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = "该操作已处理"
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

    if action_type == "switch_usage":
        name = value.get("name", "")
        if name:
            asyncio.run_coroutine_threadsafe(
                handle_switch_usage(bot, user_id, chat_id, name, clicked_msg_id), _bot_loop)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = f"正在切换到 {name}…"
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
        await _handle_restart_request(
            bot, user_id, is_group, "", card_msg_id=card_msg_id,
        )
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
            val["_cc_uid"] = user_id

    if card_msg_id:
        try:
            if reply_buttons:
                short = all(len(b["text"]) <= 12 for b in reply_buttons)
                await bot.feishu.update_card_with_buttons(card_msg_id, reply_text, reply_buttons, flow=short)
            else:
                await bot.feishu.update_card(card_msg_id, reply_text)
        except Exception as e:
            log(bot.profile.name, "menu", "error", f"菜单命令卡片更新失败: {e}")


async def handle_switch_usage(bot: BotInstance, user_id: str, chat_id: str, name: str, card_msg_id: str):
    """点击 /usage 里的账户按钮：切换账户后把当前卡片原地重渲染成最新 /usage。

    与直接跑 `/switch` 的区别——不把整张卡换成裸切换提示，而是切完立刻重出一份
    /usage（切换后的账户置顶 ● + 按钮保留），顶部只加一行切换结果 headline。
    """
    from commands import _get_usage, _switch_claude_account

    switch_result = await asyncio.to_thread(_switch_claude_account, name)
    headline = (switch_result or "").split("\n", 1)[0].strip()

    usage = await asyncio.to_thread(_get_usage, chat_id)
    if isinstance(usage, dict):
        usage_text, buttons = usage["text"], usage.get("buttons", [])
    else:
        usage_text, buttons = usage, []

    text = f"{headline}\n\n{usage_text}" if headline else usage_text

    for btn in buttons:
        val = btn.get("value")
        if isinstance(val, dict):
            val.setdefault("profile", bot.profile.name)
            val["_cc_uid"] = user_id

    if not card_msg_id:
        return
    try:
        if buttons:
            short = all(len(b["text"]) <= 12 for b in buttons)
            await bot.feishu.update_card_with_buttons(card_msg_id, text, buttons, flow=short)
        else:
            await bot.feishu.update_card(card_msg_id, text)
    except Exception as e:
        log(bot.profile.name, "switch_usage", "error", f"切换后刷新 /usage 卡片失败: {e}")


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
    effort: str = "",
    cwd: str = "",
    workspace: str = "",
) -> tuple[bool, str]:
    """在 (user, chat_id_raw:thread_id) 这一格强制开新 session 跑 prompt。

    设计场景：大群里的"调度 session"读完上下文后，用 lark-cli 在会话群创建新话题，
    再 curl 这个端点把后续工作派给独立 session。和 WS 路径不冲突——这是绕过 WS 的
    内部触发入口。

    cwd/workspace：**强制这条 session 的工作目录**。新话题的 cwd 默认来自
    `<PROFILE>_CHAT_CWD_<chat_id>` / DEFAULT_CWD，而外部触发 API 的 route 要求
    "群 → workspace" 由配置钉死，所以在这里显式落一次，不依赖 env 里那份映射。

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
            # 新话题的 effort_override 一律是 None，定时任务/派单要指定强度只能在这里落
            if effort:
                await bot.store.set_effort(user_id, chat_id, effort)
            if cwd:
                await bot.store.set_cwd(user_id, chat_id, cwd, workspace or None)
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
# 批次收口 debounce：一波里最后一个子会话结束后，等这么多秒再唤醒父 agent。
# 窗口内若父又派进新 dispatch（同一 parent_thread），则本次收口作废、并进同一波——
# 修掉"快任务/秒失败的子会话在父把整波 dispatch 落库前就把 pending 打到 0、导致
# 提前用半波结果唤醒父 + 父被唤醒多次"的 wave-split 竞态。
WAVE_DEBOUNCE_SEC = int(os.getenv("DISPATCH_WAVE_DEBOUNCE_SEC", "6") or "6")
_DISPATCH_TASKS: set = set()   # 持回报/唤醒等辅助 task 引用，防被 GC
# cap 的计数依据：每个子会话 task 从 create_task 起就进对应 chat 的集合、done 才出。
# 此前用 active_runs 计数有 TOCTOU 窗口——子会话从派发到真正 start_run 之间隔着
# 建话题/mget/发卡片好几个 await，一波快速连派会全部通过检查。
# 按 chat 分桶（此前是全局单一集合）：cap 是**每个群**独立的 —— 多群/多租户时一个
# 忙群不会把并发额度吃光饿死别的群。计数 = 目标群当前在跑/在启动的子会话数。
_DISPATCH_CHILDREN: dict[str, set] = {}


def _dispatch_active(chat_id: str) -> int:
    return len(_DISPATCH_CHILDREN.get(chat_id, ()))
# 父→子批次跟踪：parent_thread -> {bot, thread, anchor, chat, user, pending:int, children:[(title,thread)]}
# 子会话**结束后一定回报父 agent**靠这层工程化（不靠子 agent 自觉 @）：
#   · 每个子会话结束 → 往父 thread 贴一行完成/异常通知（bot 知道 turn 生命周期，子崩了也能报）
#   · 父的最后一个子会话结束 → 唤醒父 agent 一次（handle_spawn 进父 thread，带"去 read_thread 收 N 个结果"）
# 只在 dispatch_task 带了父上下文（parent_thread/parent_anchor）时启用；缺则退化成纯 fire-and-forget。
_DISPATCH_PARENTS: dict[str, dict] = {}


_BOT_OPEN_ID_CACHE: dict[str, str] = {}   # app_id -> bot 自身 open_id（@自己触发用）


def _format_dispatch_body(prompt: str, header: str = "") -> str:
    """新建子任务 thread 顶楼展示：状态 + 完整 worker prompt。

    header 可换掉默认那行状态说明（外部触发 API 用它标出 route / client / 来源），
    让群里一眼看出这条话题是谁派的。
    """
    return (
        f"{header.strip() or '（cc-lark 子任务已派发，正在独立处理…）'}\n\n"
        "【完整任务提示词】\n"
        f"{prompt.strip()}"
    )


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


async def _dispatch_wake_parent_debounced(ptid: str) -> None:
    """pending 归零后延迟 WAVE_DEBOUNCE_SEC 再收口，吸收"父还在把整波 dispatch 落库"
    的时间差。窗口内被 dispatch_task 取消（有新 dispatch 进同一 parent）则本次作废。

    只有一个挂起点（sleep）；醒来后到 pop 之间**无 await**，单线程事件循环下不会有新
    dispatch 插进来偷改状态 → 判定 + 摘出批次是原子的。"""
    try:
        await asyncio.sleep(WAVE_DEBOUNCE_SEC)
    except asyncio.CancelledError:
        return  # 窗口内来了新 dispatch，本次收口并进同一波，作废
    grp = _DISPATCH_PARENTS.get(ptid)
    if grp is None or grp.get("pending", 0) > 0:
        return  # 已被收口 / 又有在跑的了：交给后续 child 完成时再触发
    _DISPATCH_PARENTS.pop(ptid, None)
    await _dispatch_wake_parent(grp)


async def dispatch_task(
    bot: BotInstance, *, user_id: str, group_chat_id: str,
    title: str, prompt: str, cap: int = DISPATCH_CONCURRENCY_CAP,
    parent_thread: str = "", parent_anchor: str = "",
    target_bot: "BotInstance | None" = None,
    model: str = "", effort: str = "",
    cwd: str = "", workspace: str = "", body_header: str = "",
) -> dict:
    """在 group_chat_id 新开一条 thread 派一个独立 cc-lark 子会话跑 prompt。

    fire-and-forget：建好话题 + 启动子会话后立即返回 thread_id，子会话独立续跑，
    不阻塞本次调用。编排方用 read_thread(thread_id) 轮询拉结果。≤cap 并发保护。

    parent_thread/parent_anchor：派发方（主 agent）所在 thread + 其锚点消息。给了就启用
    "子会话结束→回报父 thread + 批次全完→唤醒父 agent"的工程化闭环（见 _DISPATCH_PARENTS）。

    target_bot：**跨 agent 派发**——子会话在 target_bot 名下跑（可为异后端 bot，如
    codex=GPT / opencode=Gemini / mimo），从而实现 claude 调 GPT 这类跨 agent 编排。
    缺省 = bot（同 agent 派发，原行为）。target_bot 必须是同进程已加载、且是本群成员的
    bot；建话题 + 跑子会话都用 target_bot，而回报父 thread + 唤醒父 agent 仍用 bot
    （派发方自己）——因为唤醒父 = resume 父自己的 session，必须父 bot @ 父自己。

    model/effort：**指定子会话的模型 / 推理强度**（支持 /model 的别名：fable / opus /
    sonnet / haiku / codex ...）。子会话是全新 thread + 全新 session，不继承派发方
    thread 里的 /model /effort，缺省就是目标 bot 的 profile 默认；要让一路跑 Opus、
    另一路跑 Fable 交叉验证，只能在这里显式指定。跨 agent 时 model 是给 **target_bot 的
    runner** 用的（别给 codex 传 fable），这里不按 runner 校验，错配由 runner 自己报错。

    cwd/workspace/body_header：外部事件触发 API（external_api）用——workspace 由
    route 配置钉死并强制落到子会话，body_header 换掉顶楼那行状态说明以标出触发来源。
    """
    if not group_chat_id:
        log(bot.profile.name, "dispatch", "warn", "派发被拒：缺少 group_chat_id")
        return {"ok": False, "error": "缺少 group_chat_id（派发目标群）"}
    if not (prompt and prompt.strip()):
        log(bot.profile.name, "dispatch", "warn",
            f"派发被拒：prompt 为空 chat={group_chat_id[:12]}...")
        return {"ok": False, "error": "prompt 不能为空"}
    # 先归一 model/effort 再建话题：非法值不该留下一条空话题在群里。
    try:
        model = normalize_model(model)
        effort = normalize_effort(effort, "dispatch_task")
    except ValueError as e:
        log(bot.profile.name, "dispatch", "warn", f"派发被拒：{e}")
        return {"ok": False, "error": str(e)}
    active = _dispatch_active(group_chat_id)
    if active >= cap:
        log(bot.profile.name, "dispatch", "warn",
            f"派发被拒：并发达上限 chat={group_chat_id[:12]}... active={active}/{cap}")
        return {"ok": False, "error": f"并发已达上限 {cap}（本群在跑/在启动的子会话 {active}），等已派的跑完再派下一波"}
    user = user_id or bot.store.find_primary_user() or ""
    if not user:
        log(bot.profile.name, "dispatch", "warn", "派发被拒：无法确定归属人 user_id")
        return {"ok": False, "error": "无法确定归属人 user_id"}

    # 跨 agent：子会话在 child_bot 名下跑；缺省同 bot（原行为）。
    child_bot = target_bot or bot
    cross = child_bot is not bot
    # open_id 是按 app 维度的——父的 open_id 在异 app 子 bot 里无效。跨 agent 时
    # 用子 bot 自己的 primary user 作 @/session 归属；拿不到就不 @（post 仍可发）。
    child_user = user if not cross else (child_bot.store.find_primary_user() or "")
    # 没显式指定就用**子 bot** profile 的 dispatch 默认模型（DISPATCH_MODEL）——
    # 让"主对话用便宜模型聊天、派出去的活用最强模型"成为配置保证，而不是靠派发方
    # 每次记得传 model。取 child_bot 的：model 属于跑它的那个后端。
    if not model:
        model = normalize_model(child_bot.profile.dispatch_model)

    topic_title = (title.strip() if (title and title.strip())
                   else prompt.strip().splitlines()[0])[:60]
    if cross:
        topic_title = f"[{child_bot.profile.runner}] {topic_title}"[:60]
    try:
        anchor = await child_bot.feishu.send_post_to_chat(
            chat_id=group_chat_id,
            title=f"🤖 {topic_title}",
            body_text=_format_dispatch_body(prompt, body_header),
            mention_open_id=child_user,
        )
    except Exception as e:
        hint = ""
        if cross:
            hint = (f"（跨 agent 派发：确认 {child_bot.profile.name}"
                    f"[{child_bot.profile.runner}] 这个 bot 已被拉进本群）")
        return {"ok": False, "error": f"建话题失败: {type(e).__name__}: {e}{hint}"}

    thread_id = anchor
    try:
        resolved = await child_bot.feishu.get_message_thread_id(anchor)
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
        # 新 dispatch 进来 → 作废上一次"疑似收口"的 debounce（把这一波并进来），
        # 修 wave-split：不让先跑完的子会话提前用半波结果唤醒父 agent。
        _timer = grp.pop("wake_timer", None)
        if _timer is not None and not _timer.done():
            _timer.cancel()

    # fire-and-forget：子会话独立跑，不阻塞派发返回。进目标群的 _DISPATCH_CHILDREN
    # 桶（per-chat cap 依据）兼防 GC。
    t = asyncio.create_task(handle_spawn(
        child_bot, user_id=(child_user or user), chat_id_raw=group_chat_id,
        thread_id=thread_id, anchor_message_id=anchor, prompt=prompt,
        model=model, effort=effort, cwd=cwd, workspace=workspace,
    ))
    _DISPATCH_CHILDREN.setdefault(group_chat_id, set()).add(t)

    def _on_child_done(fut, _title=topic_title, _thread=thread_id, _ptid=parent_thread,
                       _chat=group_chat_id):
        _bucket = _DISPATCH_CHILDREN.get(_chat)
        if _bucket is not None:
            _bucket.discard(fut)
            if not _bucket:
                _DISPATCH_CHILDREN.pop(_chat, None)
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
                # 不立即收口：挂一个 debounce，给父把整波剩下的 dispatch 落库的时间。
                # 窗口内来了新 dispatch 会 cancel 掉它（并进同一波），否则到点才唤醒父。
                _old = grp2.pop("wake_timer", None)
                if _old is not None and not _old.done():
                    _old.cancel()
                wt = asyncio.create_task(_dispatch_wake_parent_debounced(_ptid))
                grp2["wake_timer"] = wt
                _DISPATCH_TASKS.add(wt)
                wt.add_done_callback(_DISPATCH_TASKS.discard)

    t.add_done_callback(_on_child_done)

    over = "".join(f" {k}={v}" for k, v in (("model", model), ("effort", effort)) if v)
    log(bot.profile.name, "dispatch", "info",
        f"派子会话 chat={group_chat_id[:12]}... thread={thread_id[:14]}... "
        f"agent={child_bot.profile.name}[{child_bot.profile.runner}]"
        f"{'(cross)' if cross else ''}{over} "
        f"active={active + 1} parent={parent_thread[:12] + '...' if parent_thread else '-'}")
    return {"ok": True, "thread_id": thread_id, "anchor_message_id": anchor,
            "active_after": active + 1, "cap": cap,
            "agent": child_bot.profile.name, "agent_runner": child_bot.profile.runner,
            "model": model, "effort": effort}


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
    from thread_context import (
        _extract, _sender_label, _fmt_time, _needs_user_fetch, _fetch_card_texts_as_user,
    )
    # 子会话的结果几乎全在卡片里，而卡片正文 Lark 不回传：先吃 bot 自己的卡片缓存，
    # 缓存没有的（重启前发的 / 别的 bot 发的）借 user 身份捞回来，否则这里读到的
    # 全是占位提示——read_thread 的主用途就是收子会话结果，不能空手而归。
    need = [(m.message_id or "") for m in msgs if _needs_user_fetch(m, bot.feishu)]
    cli_profile = bot.profile.lark_cli_profile or bot.profile.name
    card_texts = (
        await _fetch_card_texts_as_user(need, cli_profile) if need and cli_profile else {}
    )
    lines: list[str] = []
    for i, m in enumerate(msgs, 1):
        try:
            text, _atts = _extract(m, bot.feishu, card_texts)
        except Exception:
            text = "[unparseable]"
        sender = _sender_label(m)
        ts = _fmt_time(m.create_time)
        lines.append(f"[{i}] {sender} {ts}: {text}".strip())
    transcript = "\n".join(lines) if lines else "(thread 暂无消息)"
    if len(transcript) > 8000:
        transcript = "…(前文截断)\n" + transcript[-8000:]
    return {"ok": True, "count": len(msgs), "transcript": transcript}


# ── 实时插话：追加 / 停止并改指令（append / steer）──────────────────────────
# 两个原语，都作用在一条已有 thread 的 session 上，都走 option B —— resume 现有
# 会话（get_current），不新开，尽力保留已经跑出来的上下文/进度（被打断那半轮已写进
# JSONL 的部分能带多少带多少；pty runner 的 session 认领策略可能让它不完整）：
#   · append —— 不打断当前 run，把新指令排到它后面（当前 run 跑完接着上下文执行）
#   · steer  —— 先停掉当前 run（保留进度卡），再按新指令 resume 续跑（实时纠偏）
# 这组原语只供 MCP 的 append_to_task / steer_task 使用（编排 agent 监工子会话
# 时实时调整方向）。人工 `/stop` 始终是纯取消。lock 天然给 append 提供"排队在后"语义。

async def _deliver_followup(
    bot: BotInstance, *, user_id: str, chat_id: str, thread_id: str,
    anchor_message_id: str, instruction: str, stop_first: bool,
    is_group: bool = True,
):
    """(可选)停当前 run → 抢 per-chat lock → resume 现有 session 跑 instruction。

    以后台 task 方式 fire-and-forget 调用。stop_first=True：先 stop_run（保留进度卡），
    等旧 run 释放 lock 再续跑；stop_first=False：直接抢 lock，天然排在当前 run 之后。
    始终 resume 现有 session（option B），不新开会话。"""
    tag = bot.profile.name
    if stop_first:
        try:
            await stop_run(
                bot.active_runs, user_id, chat_id,
                on_stopped=lambda run: _announce_stopped_run(bot, run),
            )
        except Exception as e:
            log(tag, "steer", "warn", f"停当前 run 失败（仍继续续跑）: {e}")

    lock = bot._ensure_chat_lock(chat_id)
    async with lock:
        try:
            session = await bot.store.get_current(user_id, chat_id)
            card_msg_id = await bot.feishu.reply_card(anchor_message_id, loading=True)
        except Exception as e:
            log(tag, "steer", "error", f"续跑前置失败: {type(e).__name__}: {e}")
            try:
                await bot.feishu.reply_text(
                    anchor_message_id, f"❌ 续跑失败：{type(e).__name__}: {e}")
            except Exception:
                pass
            return
        raw_chat_id = chat_id.split(":", 1)[0] if ":" in chat_id else chat_id
        lark_sys = build_lark_system_prompt(
            bot.profile, raw_chat_id, thread_id, anchor_message_id, is_group,
            asker_open_id=user_id, runner=session.runner,
        )
        run_text = instruction
        if stop_first:
            run_text = (
                "⚠️ 我刚打断了你上一步正在做的事（上一轮任务被显式中止）。"
                "请改按下面的新指令继续，不要重复已经做过的写操作 / 命令 / 文件改动：\n\n"
                + instruction
            )
        log(tag, "steer", "info",
            f"续跑 chat={raw_chat_id[:10]}... thread={(thread_id or '-')[:10]}... "
            f"stop_first={stop_first} len={len(instruction)}")
        try:
            await _run_and_display(
                bot, user_id, chat_id, is_group, run_text,
                card_msg_id, session, anchor_message_id,
                preview_text=instruction[:40], append_system_prompt=lark_sys,
            )
        except Exception as e:
            log(tag, "steer", "error", f"续跑执行异常: {type(e).__name__}: {e}")


async def steer_or_append_thread(
    bot: BotInstance, *, user_id: str, group_chat_id: str, thread_id: str,
    instruction: str, stop_first: bool,
) -> dict:
    """MCP append_to_task / steer_task 的 bot 侧实现：往某条已有 thread 的 session
    实时插话。stop_first=False=追加（不打断），True=停当前 run 再按新指令续跑。
    仅做锚点解析 + 排后台 task，立即返回（不阻塞等整轮跑完）。"""
    if not (group_chat_id and thread_id):
        return {"ok": False, "error": "缺少 group_chat_id / thread_id"}
    if not (instruction and instruction.strip()):
        return {"ok": False, "error": "instruction 不能为空"}
    # 兜底：dispatch_task 偶尔把 anchor 的 om_ 当 thread_id 传回 → 转成真 omt_
    if thread_id.startswith("om_") and not thread_id.startswith("omt_"):
        try:
            actual = await bot.feishu.get_message_thread_id(thread_id)
            if actual and actual.startswith("omt_"):
                thread_id = actual
        except Exception:
            pass
    user = user_id or bot.store.find_primary_user() or ""
    if not user:
        return {"ok": False, "error": "无法确定归属人 user_id"}
    chat_id = f"{group_chat_id}:{thread_id}"
    run = bot.active_runs.get_run(user, chat_id)
    running = run is not None and not run.stop_requested

    # 回复锚点：优先当前 run 的流式卡（一定在该 thread 内），否则拉 thread 里一条消息
    anchor = run.card_msg_id if run else ""
    if not anchor:
        try:
            msgs = await bot.feishu.list_thread_messages(thread_id, limit=1)
            if msgs:
                anchor = getattr(msgs[0], "message_id", "") or ""
        except Exception as e:
            return {"ok": False, "error": f"无法定位 thread 锚点: {type(e).__name__}: {e}"}
    if not anchor:
        return {"ok": False, "error": "thread 内没有可回复的消息作为锚点"}

    do_stop = stop_first and running
    if do_stop:
        head, title = "⏹ 已停止当前任务，正在按新指令续跑（保留已完成的上下文）", "🛠 停止并改指令"
    elif running:
        head, title = "📬 已把新指令追加到当前任务后面，跑完就接着上下文执行", "➕ 追加指令"
    else:
        head, title = "▶️ 该话题当前空闲，直接按新指令续跑", "➕ 追加指令"
    # 把「完整指令」发进 thread —— MCP 路径的指令来自编排 agent，人在群里看不到，
    # 必须像 dispatch_task 展示 worker prompt 那样把全文贴出来，否则只剩一句状态。
    body_text = f"{head}\n\n【完整指令】\n{instruction.strip()}"
    try:
        await bot.feishu.reply_post(anchor, title=title, body_text=body_text)
    except Exception:
        try:
            await bot.feishu.reply_text(anchor, body_text)  # 富文本失败退回纯文本
        except Exception:
            pass

    t = asyncio.create_task(_deliver_followup(
        bot, user_id=user, chat_id=chat_id, thread_id=thread_id,
        anchor_message_id=anchor, instruction=instruction.strip(),
        stop_first=do_stop, is_group=True,
    ))
    _DISPATCH_TASKS.add(t)
    t.add_done_callback(_DISPATCH_TASKS.discard)
    return {
        "ok": True, "thread_id": thread_id,
        "mode": "steer" if stop_first else "append",
        "stopped": do_stop, "queued": bool(running and not stop_first),
    }
