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

调度跑在独立的 BackgroundScheduler 线程里，不依赖 bot_loop 的 asyncio timer。
job 触发时同步 wrapper 用 run_coroutine_threadsafe 把 _fire() 投回 bot_loop 跑实际业务。

为什么不用 AsyncIOScheduler：macOS 上 asyncio 的 timer 走 time.monotonic()，
系统睡眠期间不推进，长跑进程跨睡眠周期会错过 cron。BackgroundScheduler 本身
也基于 monotonic，但配合 misfire_grace_time=None + 每 60s 的 wake-nudger，
唤醒后会立即补跑 + 重算 next_run_time。
"""

from __future__ import annotations

import asyncio
import glob
import os
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Awaitable, Callable

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ── 后台 lock reaper：兜底清 stale lock，让链能自愈 ───────────
# 背景：每个定时 task prompt STEP 0 里都有 `mkdir $LOCK` 的并发互斥。正常路径下
# session 跑完会 rmdir，链就续上下一轮；但 PTY runner 早退 / Claude CLI 启动卡死 /
# session crash 等异常路径下，lock 留在那直到下次 cron fire 才被新 session 自检清掉
# （每个 prompt STEP 0 里都有 AGE > 3600 即过期的逻辑）。问题是：如果没新 fire 来
# （比如 reflection 是日级 cron，错过 04:00 就要等明天），链彻底断在那。
# reaper 解决"没新 session 来时也定期清"的兜底问题。
LOCK_GLOBS = [
    "/Users/user/Desktop/workspace/payment/spx/.*.lock",
    "/Users/user/Desktop/workspace/tools/feishu-claude-code/.*.lock",
]
REAPER_STALE_MINUTES = 60   # 跟 prompt STEP 0 的 3600s 阈值对齐 — 任何 task 都不该跑超过 60min
REAPER_INTERVAL_SECONDS = 300  # 每 5 分钟扫一次


def _reap_stale_locks(stale_minutes: int = REAPER_STALE_MINUTES) -> int:
    """扫已知 lock 目录，rmdir mtime 超过 stale_minutes 的 lock。返回清掉的数量。"""
    now = time.time()
    cleaned = 0
    for pattern in LOCK_GLOBS:
        for lock_path in glob.glob(pattern):
            if not os.path.isdir(lock_path):
                continue
            try:
                mtime = os.path.getmtime(lock_path)
            except FileNotFoundError:
                continue  # race: 已被其他人清
            age_min = (now - mtime) / 60
            if age_min <= stale_minutes:
                continue
            try:
                os.rmdir(lock_path)
                cleaned += 1
                print(
                    f"[reaper] cleaned stale lock {lock_path} (age {age_min:.1f}min)",
                    flush=True,
                )
            except FileNotFoundError:
                pass  # race
            except OSError as e:
                # 非空 dir（lock 协议升级后里面写了 pid/start_ts）或权限错 — log 不 crash
                print(f"[reaper] failed to clean {lock_path}: {e}", flush=True)
    return cleaned


def _reap_loop() -> None:
    """每 REAPER_INTERVAL_SECONDS 跑一次 reaper。daemon thread，跟 scheduler 同生命周期。"""
    while True:
        time.sleep(REAPER_INTERVAL_SECONDS)
        try:
            _reap_stale_locks()
        except Exception as e:
            print(f"[reaper] loop error: {e}", flush=True)

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


# name -> async _fire；供 fire_task_now（HTTP /trigger 路径）await
_TASK_REGISTRY: dict[str, Callable[[], Awaitable[None]]] = {}
# 全局：start_scheduler 把构造好的 scheduler 和加载状态放这里，/reload 复用
_STATE: dict = {
    "scheduler": None,
    "config_path": None,
    "bots": None,
    "bot_loop": None,
    "spawn_fn": None,
}


def list_tasks() -> list[str]:
    """返回所有已注册的任务名（手动触发查表用）。"""
    return sorted(_TASK_REGISTRY.keys())


async def fire_task_now(name: str) -> None:
    """手动触发一条任务（绕过 cron）。任务必须已通过 start_scheduler 注册。

    跑在 bot_loop 里（由 http_server 的 _submit 投入），可以直接 await async _fire。
    """
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
    bot_loop = _STATE["bot_loop"]
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
        async_fire = _make_async_fire(task, bot, spawn_fn)
        _TASK_REGISTRY[task.name] = async_fire
        sched.add_job(
            _make_sync_fire(task.name, async_fire, bot_loop),
            trigger=trigger, id=task.name, name=task.name,
            misfire_grace_time=None, coalesce=True, max_instances=1,
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
    bot_loop: asyncio.AbstractEventLoop,
    spawn_fn: SpawnFn,
) -> BackgroundScheduler | None:
    """加载 YAML 并启动 BackgroundScheduler（独立线程跑）。

    job 触发时同步 wrapper 用 run_coroutine_threadsafe 把 async _fire 投到 bot_loop —
    避免 scheduler 线程阻塞，同时让业务逻辑跑在已经持有 bot 状态的那个 loop 上。

    若 yaml 不存在或为空，返回 None。
    """
    tasks = _load_tasks(config_path)
    if not tasks:
        print(f"[scheduler] 未配置定时任务（{config_path}），跳过", flush=True)
        return None

    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
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

        async_fire = _make_async_fire(task, bot, spawn_fn)
        _TASK_REGISTRY[task.name] = async_fire

        scheduler.add_job(
            _make_sync_fire(task.name, async_fire, bot_loop),
            trigger=trigger,
            id=task.name,
            name=task.name,
            # None = 永远补跑迟到的任务。
            # 进程长跑 + 系统睡眠后唤醒，wall clock 可能已跨过多个 cron 时间点；
            # coalesce=True 会把多次合并为 1 次，所以即便睡了 3 天醒来也只补 1 次。
            misfire_grace_time=None,
            coalesce=True,
            max_instances=1,         # 同任务并发只能 1 个
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
    _STATE["bot_loop"] = bot_loop
    _STATE["spawn_fn"] = spawn_fn

    # 60s 一次的 wake-nudger：强制 scheduler 重算 next_run_time。
    # 哪怕 monotonic clock 因系统睡眠而 drift，wakeup() 会让 scheduler 立刻按
    # wall clock 重新检查所有 jobs，misfire_grace_time=None 的会被补跑。
    threading.Thread(
        target=_wake_nudge_loop, args=(scheduler,),
        daemon=True, name="sched-nudger",
    ).start()

    # 5min 一次的 stale-lock reaper：清掉 mtime > 60min 的 stale lock dir，让链自愈。
    threading.Thread(
        target=_reap_loop,
        daemon=True, name="sched-lock-reaper",
    ).start()

    # 启动时立即跑一次 reaper —— 让重启 cc-lark 时能立即清掉积压的 stale lock
    initial_cleaned = _reap_stale_locks()
    if initial_cleaned:
        print(f"[reaper] startup pass cleaned {initial_cleaned} stale lock(s)", flush=True)

    print(
        f"[scheduler] 已启动（BackgroundScheduler），加载 {len(scheduler.get_jobs())} 个定时任务，lock-reaper 已起",
        flush=True,
    )
    return scheduler


def _wake_nudge_loop(scheduler: BackgroundScheduler) -> None:
    """每 60s 调一次 scheduler.wakeup() 强制重算 next_run_time。

    APScheduler 内部用 threading.Event.wait(timeout) 等下次 fire，timeout 走的也是
    CLOCK_MONOTONIC，macOS 睡眠期间不推进——长 wait 会被冻结，醒来后还得继续等。
    这个 nudger 用短 sleep 把 scheduler 拉醒，让它按 wall clock 重新调度。
    """
    while True:
        time.sleep(60)
        try:
            scheduler.wakeup()
        except Exception:
            pass


def _make_async_fire(task: ScheduledTask, bot, spawn_fn: SpawnFn):
    """生成无参 async coroutine。fire_task_now 直接 await，sync wrapper 投到 bot_loop。"""
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


def _make_sync_fire(
    name: str,
    async_fire: Callable[[], Awaitable[None]],
    bot_loop: asyncio.AbstractEventLoop,
):
    """BackgroundScheduler 调的同步 job —— 把 async 业务投回 bot_loop，不阻塞 scheduler 线程。"""
    def _sync_fire():
        try:
            asyncio.run_coroutine_threadsafe(async_fire(), bot_loop)
        except Exception as e:
            print(f"[scheduler/{name}] ❌ submit 失败: {type(e).__name__}: {e}", flush=True)

    return _sync_fire
