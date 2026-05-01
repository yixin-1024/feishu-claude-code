"""
定时任务调度：cron → 在指定话题群创建新话题 → /spawn 起独立 session 处理。

每条任务在 scheduled_tasks.yaml 里定义：
    - name: spx_daily_briefing
      profile: spx
      cron: "50 17 * * *"          # 五段 cron（分 时 日 月 周），timezone 见下
      timezone: "Asia/Shanghai"     # 可选，默认 Asia/Shanghai
      chat_id: oc_xxx               # 在哪个话题群发顶楼消息（必须支持 thread）
      user_id: ou_xxx               # 任务归属人；顶楼消息会 @ 这个人拉订阅
      topic_title: "📅 每日报"
      topic_body: "🧵 接管：整理今天日报"
      # prompt 二选一：内联 prompt（短的） 或 prompt_file（长的，相对 yaml 同目录）
      prompt_file: prompts/spx_daily_briefing.md
      # 或：
      # prompt: |
      #   多行 prompt，承接 session 的全部上下文都靠这段。

调度器只在主线程的 _bot_loop 上运行——和 WS 事件、HTTP 回调共用同一个 loop，
所以 _handle_spawn 可以直接用 asyncio 调，不需要 run_coroutine_threadsafe。
"""

from __future__ import annotations

import asyncio
import os
import traceback
from dataclasses import dataclass
from typing import Awaitable, Callable

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ── 类型契约：main.py 注入的派单入口 ─────────────────────────
# (bot, user_id, chat_id_raw, thread_id, anchor_message_id, prompt) -> coroutine
SpawnFn = Callable[..., Awaitable[None]]


@dataclass
class ScheduledTask:
    name: str
    profile: str
    cron: str
    timezone: str
    chat_id: str
    user_id: str
    topic_title: str
    topic_body: str
    prompt: str

    @classmethod
    def from_dict(cls, raw: dict, base_dir: str) -> "ScheduledTask":
        # prompt 与 prompt_file 二选一
        prompt_inline = raw.get("prompt")
        prompt_file = raw.get("prompt_file")
        if prompt_inline and prompt_file:
            raise ValueError(f"task {raw.get('name')!r}: prompt 和 prompt_file 只能写一个")
        if not prompt_inline and not prompt_file:
            raise ValueError(f"task {raw.get('name')!r}: 缺少 prompt 或 prompt_file")

        if prompt_file:
            # 相对路径以 yaml 同目录为根
            path = prompt_file if os.path.isabs(prompt_file) else os.path.join(base_dir, prompt_file)
            if not os.path.exists(path):
                raise FileNotFoundError(f"task {raw.get('name')!r}: prompt_file 不存在 {path!r}")
            with open(path, "r", encoding="utf-8") as f:
                prompt_text = f.read()
        else:
            prompt_text = str(prompt_inline)

        missing = [
            k for k in ("name", "profile", "cron", "chat_id", "user_id")
            if not raw.get(k)
        ]
        if missing:
            raise ValueError(f"task 缺少字段: {', '.join(missing)} (raw={raw!r})")
        return cls(
            name=str(raw["name"]),
            profile=str(raw["profile"]),
            cron=str(raw["cron"]),
            timezone=str(raw.get("timezone") or "Asia/Shanghai"),
            chat_id=str(raw["chat_id"]),
            user_id=str(raw["user_id"]),
            topic_title=str(raw.get("topic_title") or raw["name"]),
            topic_body=str(raw.get("topic_body") or "🧵 定时任务"),
            prompt=prompt_text,
        )


def _load_tasks(path: str) -> list[ScheduledTask]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    # 支持在 yaml 里写 ${SPX_DISPATCH_CHAT_ID} 这类引用，避免把 chat_id 直接落到代码库
    text = os.path.expandvars(text)
    raw = yaml.safe_load(text) or []
    if not isinstance(raw, list):
        raise ValueError(f"{path} 顶层必须是 list")
    base_dir = os.path.dirname(os.path.abspath(path))
    return [ScheduledTask.from_dict(item, base_dir) for item in raw]


_TASK_REGISTRY: dict[str, Callable[[], Awaitable[None]]] = {}
# 全局：start_scheduler 把构造好的 scheduler 和加载状态放这里，/reload 复用
_STATE: dict = {"scheduler": None, "config_path": None, "bots": None, "spawn_fn": None}


def list_tasks() -> list[str]:
    """返回所有已注册的任务名（手动触发查表用）。"""
    return sorted(_TASK_REGISTRY.keys())


async def fire_task_now(name: str) -> None:
    """手动触发一条任务（绕过 cron）。任务必须已通过 start_scheduler 注册。"""
    fn = _TASK_REGISTRY.get(name)
    if fn is None:
        raise KeyError(f"task {name!r} 不存在；已注册: {list_tasks()}")
    await fn()


def reload_tasks() -> dict:
    """重新读 yaml + prompt_file，原地刷新已注册的任务。
    返回 {"removed": [...], "added": [...], "errors": [...]}。
    保留 scheduler 实例，只 remove + add_job，不打断正在跑的任务。
    """
    sched = _STATE["scheduler"]
    if sched is None:
        raise RuntimeError("scheduler 未启动，不能 reload")

    config_path = _STATE["config_path"]
    bots = _STATE["bots"]
    spawn_fn = _STATE["spawn_fn"]

    try:
        new_tasks = _load_tasks(config_path)
    except Exception as e:
        return {"ok": False, "error": f"加载 yaml 失败: {type(e).__name__}: {e}"}

    old_names = set(_TASK_REGISTRY.keys())
    removed, added, errors = [], [], []

    # 清空旧 jobs
    for name in list(old_names):
        try:
            sched.remove_job(name)
        except Exception:
            pass
        removed.append(name)
    _TASK_REGISTRY.clear()

    # 重新注册
    for task in new_tasks:
        bot = bots.get(task.profile)
        if bot is None:
            errors.append(f"{task.name}: profile {task.profile!r} 未加载")
            continue
        try:
            trigger = CronTrigger.from_crontab(task.cron, timezone=task.timezone)
        except Exception as e:
            errors.append(f"{task.name}: cron 非法 {e}")
            continue
        job_fn = _make_job(task, bot, spawn_fn)
        _TASK_REGISTRY[task.name] = job_fn
        sched.add_job(
            job_fn, trigger=trigger, id=task.name, name=task.name,
            misfire_grace_time=300, coalesce=True, max_instances=1,
        )
        added.append(task.name)
        print(
            f"[scheduler/reload] ✅ {task.name} → cron='{task.cron}' tz={task.timezone}",
            flush=True,
        )

    return {"ok": True, "removed": removed, "added": added, "errors": errors}


def start_scheduler(
    config_path: str,
    bots: dict,                 # profile_name -> BotInstance
    loop: asyncio.AbstractEventLoop,
    spawn_fn: SpawnFn,
) -> AsyncIOScheduler | None:
    """加载 YAML 并启动 AsyncIOScheduler，绑定到 loop 上。

    返回 scheduler；调用方持有引用即可，daemon 线程已经在 loop 内运行。
    若 yaml 不存在或为空，返回 None。
    """
    tasks = _load_tasks(config_path)
    if not tasks:
        print(f"[scheduler] 未配置定时任务（{config_path}），跳过", flush=True)
        return None

    scheduler = AsyncIOScheduler(event_loop=loop)
    _TASK_REGISTRY.clear()

    for task in tasks:
        bot = bots.get(task.profile)
        if bot is None:
            print(f"[scheduler] ⚠️ 任务 {task.name!r} profile={task.profile!r} 未加载，跳过", flush=True)
            continue

        try:
            trigger = CronTrigger.from_crontab(task.cron, timezone=task.timezone)
        except Exception as e:
            print(f"[scheduler] ⚠️ 任务 {task.name!r} cron={task.cron!r} 非法: {e}", flush=True)
            continue

        job_fn = _make_job(task, bot, spawn_fn)
        _TASK_REGISTRY[task.name] = job_fn

        scheduler.add_job(
            job_fn,
            trigger=trigger,
            id=task.name,
            name=task.name,
            misfire_grace_time=300,   # 错过 5 分钟内仍补跑；超过就跳过
            coalesce=True,            # 多次错过合并成一次
            max_instances=1,          # 同任务并发只能 1 个
        )
        print(
            f"[scheduler] ✅ {task.name} → profile={task.profile} chat={task.chat_id[:12]}... "
            f"cron='{task.cron}' tz={task.timezone}",
            flush=True,
        )

    scheduler.start()
    _STATE["scheduler"] = scheduler
    _STATE["config_path"] = config_path
    _STATE["bots"] = bots
    _STATE["spawn_fn"] = spawn_fn
    print(f"[scheduler] 已启动，加载 {len(scheduler.get_jobs())} 个定时任务", flush=True)
    return scheduler


def _make_job(task: ScheduledTask, bot, spawn_fn: SpawnFn):
    """生成无参 coroutine，apscheduler 会在 loop 上 await 它。"""
    async def _fire():
        tag = f"[scheduler/{task.name}]"
        try:
            print(f"{tag} fire → 在 chat={task.chat_id[:12]}... 发顶楼", flush=True)
            anchor_msg_id = await bot.feishu.send_post_to_chat(
                chat_id=task.chat_id,
                title=task.topic_title,
                body_text=task.topic_body,
                mention_open_id=task.user_id,
            )
            print(f"{tag} anchor={anchor_msg_id[:14]}... → /spawn", flush=True)

            # /spawn 服务端会把 om_xxx 自动转成 omt_xxx
            await spawn_fn(
                bot,
                user_id=task.user_id,
                chat_id_raw=task.chat_id,
                thread_id=anchor_msg_id,
                anchor_message_id=anchor_msg_id,
                prompt=task.prompt,
            )
            print(f"{tag} done", flush=True)
        except Exception as e:
            print(f"{tag} ❌ 异常: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()

    return _fire
