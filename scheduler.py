"""
定时任务调度：cron → 在指定话题群创建新话题 → /spawn 起独立 session 处理。

每条任务在 scheduled_tasks.yaml 里定义：
    - name: my_daily_report
      profile: default
      cron: "50 17 * * *"          # 五段 cron（分 时 日 月 周），timezone 见下
      timezone: "Asia/Shanghai"     # 可选，默认 Asia/Shanghai
      chat_id: oc_xxx               # 在哪个话题群发顶楼消息（必须支持 thread）
      user_id: ou_xxx               # 任务归属人；顶楼消息会 @ 这个人拉订阅
      topic_title: "📅 每日报"
      topic_body: "🧵 接管：整理今天日报"
      # prompt 二选一：内联 prompt（短的） 或 prompt_file（长的，相对 yaml 同目录）
      prompt_file: prompts/my_daily_report.md
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

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

# ── 后台 lock reaper：兜底清 stale lock，让链能自愈 ───────────
# 背景：每个定时 task prompt STEP 0 里都有 `mkdir $LOCK` 的并发互斥。正常路径下
# session 跑完会 rmdir，链就续上下一轮；但 PTY runner 早退 / Claude CLI 启动卡死 /
# session crash 等异常路径下，lock 留在那直到下次 cron fire 才被新 session 自检清掉
# （每个 prompt STEP 0 里都有 AGE > 3600 即过期的逻辑）。问题是：如果没新 fire 来
# （比如 reflection 是日级 cron，错过 04:00 就要等明天），链彻底断在那。
# reaper 解决"没新 session 来时也定期清"的兜底问题。
def _lock_globs() -> list[str]:
    """要兜底扫描的 stale-lock glob 列表。部署相关的绝对路径放 env
    CC_LARK_LOCK_GLOBS（冒号分隔），不进代码库；未设置则只兜底扫本仓目录。"""
    env = os.environ.get("CC_LARK_LOCK_GLOBS", "").strip()
    if env:
        return [g.strip() for g in env.split(os.pathsep) if g.strip()]
    return [os.path.join(os.path.dirname(os.path.abspath(__file__)), ".*.lock")]
REAPER_STALE_MINUTES = 60   # 跟 prompt STEP 0 的 3600s 阈值对齐 — 任何 task 都不该跑超过 60min
REAPER_INTERVAL_SECONDS = 300  # 每 5 分钟扫一次


def _reap_stale_locks(stale_minutes: int = REAPER_STALE_MINUTES) -> int:
    """扫已知 lock 目录，rmdir mtime 超过 stale_minutes 的 lock。返回清掉的数量。"""
    now = time.time()
    cleaned = 0
    for pattern in _lock_globs():
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
# job_id -> chat_id：list_crons 按 chat 过滤的作用域表。
# wake job 在 schedule_wake 里登记；yaml 任务（含 agent cron）在注册 job 时登记。
_JOB_CHAT_SCOPE: dict[str, str] = {}
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


def schedule_wake(
    *,
    profile: str,
    chat_id: str,
    thread_id: str,
    anchor_message_id: str,
    user_id: str,
    minutes: int,
    note: str,
) -> dict:
    """安排 N 分钟后在 (chat_id:thread_id) 这条话题里自动开一个新 turn 续上 note。

    这是 cc_mcp_server 的 wake_me_in 工具的持久兑现端：MCP 那层只是把请求 POST
    到 bot 的 /wake，真正"过 N 分钟再唤醒"由这里挂到常驻 BackgroundScheduler 上
    （一次性 DateTrigger）。fire 时复用 spawn_fn(=handle_spawn) 在该话题强开新 session。

    依赖 start_scheduler 已经把 scheduler/bots/bot_loop/spawn_fn 放进 _STATE。
    返回 {"ok": True, "fire_at_local": "...", "job_id": "..."} 或 {"ok": False, "error": ...}。
    """
    sched = _STATE["scheduler"]
    bots = _STATE["bots"]
    bot_loop = _STATE["bot_loop"]
    spawn_fn = _STATE["spawn_fn"]
    if sched is None or bots is None or bot_loop is None or spawn_fn is None:
        return {"ok": False, "error": "scheduler 未启动"}

    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return {"ok": False, "error": "minutes 必须是整数"}
    if not (1 <= minutes <= 1440):
        return {"ok": False, "error": "minutes 必须在 1..1440 之间"}
    if not (chat_id and thread_id):
        return {"ok": False, "error": "缺少 chat_id / thread_id"}
    if not (note and note.strip()):
        return {"ok": False, "error": "note 不能为空"}

    # profile → bot：显式优先；单 bot 时可省略。
    bot = bots.get(profile) if profile else None
    if bot is None:
        if len(bots) == 1:
            bot = next(iter(bots.values()))
        else:
            return {"ok": False, "error": f"profile {profile!r} 未加载（多 bot 必须指定）"}

    user = user_id or bot.store.find_primary_user() or ""
    if not user:
        return {"ok": False, "error": "无法确定归属人 user_id"}
    # 回复锚点缺失时退回 thread_id 本身（handle_spawn 能把 om_ 自动转 omt_）。
    anchor = anchor_message_id or thread_id

    tz = ZoneInfo("Asia/Shanghai")
    run_date = datetime.now(tz) + timedelta(minutes=minutes)
    job_id = f"wake-{thread_id[-8:]}-{uuid.uuid4().hex[:6]}"
    _JOB_CHAT_SCOPE[job_id] = chat_id  # list_crons 按 chat 过滤用

    wake_prompt = (
        f"[⏰ 自动唤醒] 你在大约 {minutes} 分钟前给自己排了这次唤醒。\n"
        f"你当时留下的待办 / 要检查的事：\n{note.strip()}\n\n"
        f"请据此继续；需要更多上下文可读本话题历史或相关文件。处理完按正常方式回复即可。"
    )

    # fire 时走 send-as-user @bot（dispatcher.wake_thread_as_user）唤醒：经 WS 入站路径、
    # 忙时排队不丢、且 resume 本话题 session。比 handle_spawn(reject-if-busy) 稳。
    # 懒 import 避免 scheduler↔dispatcher 顶层循环依赖；失败兜底回退 spawn_fn(handle_spawn)。
    def _fire():
        async def _do():
            try:
                from dispatcher import wake_thread_as_user
                ok = await wake_thread_as_user(bot, anchor, wake_prompt)
                if ok:
                    return
                print(f"[scheduler/wake] ⚠️ {job_id} send-as-user 未成功，回退 handle_spawn", flush=True)
            except Exception as e:
                print(f"[scheduler/wake] ⚠️ {job_id} wake_thread_as_user 异常 {type(e).__name__}: {e}，回退 handle_spawn", flush=True)
            try:
                await spawn_fn(bot, user_id=user, chat_id_raw=chat_id,
                               thread_id=thread_id, anchor_message_id=anchor, prompt=wake_prompt)
            except Exception as e:
                print(f"[scheduler/wake] ❌ {job_id} 回退 spawn 也失败: {type(e).__name__}: {e}", flush=True)
        try:
            asyncio.run_coroutine_threadsafe(_do(), bot_loop)
            print(f"[scheduler/wake] 🔔 fire {job_id} → chat={chat_id[:12]}... thread={thread_id[:12]}...", flush=True)
        except Exception as e:
            print(f"[scheduler/wake] ❌ {job_id} submit 失败: {type(e).__name__}: {e}", flush=True)
        finally:
            _JOB_CHAT_SCOPE.pop(job_id, None)  # 一次性 job 跑完即出作用域表

    try:
        sched.add_job(
            _fire,
            trigger=DateTrigger(run_date=run_date),
            id=job_id,
            name=job_id,
            # 系统睡眠跨过 fire 时间也补跑（醒来后 wake-nudger 会拉起）；一次性，跑完自删。
            misfire_grace_time=None,
            coalesce=True,
            max_instances=1,
        )
    except Exception as e:
        return {"ok": False, "error": f"add_job 失败: {type(e).__name__}: {e}"}

    fire_local = run_date.strftime("%m/%d %H:%M")
    print(
        f"[scheduler/wake] ✅ 已排 {job_id} → {fire_local} (+{minutes}min) "
        f"profile={bot.profile.name} thread={thread_id[:12]}...",
        flush=True,
    )
    return {"ok": True, "fire_at_local": fire_local, "job_id": job_id}


def schedule_cron(
    *,
    profile: str,
    chat_id: str,
    user_id: str,
    cron: str,
    prompt: str,
    title: str = "",
    timezone: str = "Asia/Shanghai",
) -> dict:
    """新增一条**重复**定时任务（真·"每天几点干个啥"）—— cc_mcp_server 的 schedule_cron 后端。

    安全落盘：prompt 写进独立 data/agent_crons/<name>.md（避开多行 prompt 的 YAML 转义雷），
    再用 yaml.safe_dump 生成一条 entry **追加**到 scheduled_tasks.yaml（只 append、不 load+dump，
    保留既有注释和 ${VAR}），最后调 reload_tasks() 即时生效。重启后仍在（落了盘）。
    复用全套现成 cron 管线（quota 预检 / 建话题 / spawn / 睡眠安全调度）。

    返回 {"ok": True, "name", "cron", "next_run", "prompt_file"} 或 {"ok": False, "error"}。
    """
    sched = _STATE["scheduler"]
    bots = _STATE["bots"]
    config_path = _STATE["config_path"]
    if sched is None or bots is None or not config_path:
        return {"ok": False, "error": "scheduler 未启动（需要 scheduled_tasks.yaml 至少一条任务才会起）"}

    cron = (cron or "").strip()
    if not cron:
        return {"ok": False, "error": "cron 不能为空（五段：分 时 日 月 周）"}
    if not (prompt and prompt.strip()):
        return {"ok": False, "error": "prompt 不能为空"}
    if not chat_id:
        return {"ok": False, "error": "缺少 chat_id"}

    bot = bots.get(profile) if profile else None
    if bot is None and len(bots) == 1:
        bot = next(iter(bots.values()))
    if bot is None:
        return {"ok": False, "error": f"profile {profile!r} 未加载（多 bot 必须指定）"}
    prof = bot.profile.name
    user = user_id or bot.store.find_primary_user() or ""
    if not user:
        return {"ok": False, "error": "无法确定归属人 user_id"}

    # 先校验 cron 合法（非法就别落盘）
    try:
        CronTrigger.from_crontab(cron, timezone=timezone)
    except Exception as e:
        return {"ok": False, "error": f"cron 非法: {e}"}

    # uuid 后缀：同一秒批量创建两条也不会撞 job id
    name = f"agent_cron_{int(time.time())}_{uuid.uuid4().hex[:4]}"
    base_dir = os.path.dirname(os.path.abspath(config_path))
    rel_prompt = os.path.join("data", "agent_crons", f"{name}.md")
    abs_prompt = os.path.join(base_dir, rel_prompt)
    try:
        os.makedirs(os.path.dirname(abs_prompt), exist_ok=True)
        with open(abs_prompt, "w", encoding="utf-8") as f:
            f.write(prompt.strip() + "\n")
    except OSError as e:
        return {"ok": False, "error": f"写 prompt 文件失败: {type(e).__name__}: {e}"}

    entry = {
        "name": name,
        "profile": prof,
        "cron": cron,
        "timezone": timezone,
        "chat_id": chat_id,
        "user_id": user,
        "topic_title": (title.strip() or f"⏰ 定时任务")[:60],
        "topic_body": "🧵 定时任务（agent 创建）",
        "prompt_file": rel_prompt,
    }
    # yaml.safe_dump 负责所有转义/引号；append-only 保留既有内容与注释。
    chunk = yaml.safe_dump([entry], allow_unicode=True, sort_keys=False, default_flow_style=False)
    try:
        existing = ""
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                existing = f.read()
        sep = "" if (not existing or existing.endswith("\n")) else "\n"
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(existing + sep + chunk)
    except OSError as e:
        # 落盘失败：清理已写的 prompt 文件，避免留垃圾
        try:
            os.remove(abs_prompt)
        except OSError:
            pass
        return {"ok": False, "error": f"写 scheduled_tasks.yaml 失败: {type(e).__name__}: {e}"}

    res = reload_tasks()
    if not res.get("ok"):
        return {"ok": False, "error": f"reload 失败: {res.get('error')}"}

    job = sched.get_job(name)
    nxt = str(job.next_run_time) if (job and job.next_run_time) else "?"
    print(f"[scheduler/cron] ✅ 新增 {name} profile={prof} cron='{cron}' → next {nxt}", flush=True)
    return {"ok": True, "name": name, "cron": cron, "next_run": nxt, "prompt_file": rel_prompt}


def list_crons(chat_id: str = "") -> dict:
    """列出 scheduler 里的 job（含 agent 创建的重复 cron + 一次性 wake）。

    chat_id 非空时只返回该 chat 作用域内的 job（多群共用 bot 时不把 A 群排的
    任务名泄露给 B 群的 agent）；归属未知的 job 一并隐藏。chat_id 为空 = 全量
    （本机运维路径，如 http /list_crons 不带参）。"""
    sched = _STATE["scheduler"]
    if sched is None:
        return {"ok": False, "error": "scheduler 未启动"}
    jobs = []
    for j in sched.get_jobs():
        if chat_id and _JOB_CHAT_SCOPE.get(str(j.id)) != chat_id:
            continue
        jobs.append({
            "name": j.id,
            "next_run": str(j.next_run_time) if j.next_run_time else None,
            "agent_created": str(j.id).startswith("agent_cron_"),
        })
    return {"ok": True, "jobs": jobs}


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
        _JOB_CHAT_SCOPE.pop(name, None)
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
        _JOB_CHAT_SCOPE[task.name] = task.chat_id
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
    # yaml 为空/不存在也照常启动：scheduler 还是 wake_me_in / schedule_cron 的
    # 持久兑现层，没配 cron 任务不等于不需要它（此前空配置直接 return None，
    # 导致新部署里 runtime MCP 的 wake/cron 能力整体不可用）。
    tasks = _load_tasks(config_path)
    if not tasks:
        print(f"[scheduler] 未配置定时任务（{config_path}），空载启动（wake/cron 仍可用）", flush=True)

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
        _JOB_CHAT_SCOPE[task.name] = task.chat_id

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


def _quota_skip_reason(q: dict) -> tuple[str, list[tuple[str, int | None]]] | None:
    """根据 fetch_quota_headers() 返回判断是否要跳过派单。

    返回 None = 用量正常可派；否则返回 (整体说明, [(窗口标签, reset_ts), ...])。
    判据：status 字段非 "allowed"（Anthropic 直接告诉我们已耗尽），或
    utilization >= 0.98（再派一次极可能立刻撞 limit、Claude CLI 会注入
    synthetic rate_limit 事件，相当于派出去就死）。
    """
    if not q.get("ok"):
        return None  # 拿不到 quota：不阻塞，让 spawn 正常跑
    out: list[tuple[str, int | None]] = []
    for label, util_key, reset_key, status_key in [
        ("5h", "u5h", "r5h", "s5h"),
        ("7d", "u7d", "r7d", "s7d"),
    ]:
        util = q.get(util_key)
        status = q.get(status_key)
        reset_ts = q.get(reset_key)
        bad_status = bool(status) and status != "allowed" and status != "unknown"
        near_full = util is not None and util >= 0.98
        if bad_status or near_full:
            out.append((label, reset_ts))
    if not out:
        return None
    # 文案：列出每个窗口和它的 reset 时间
    from datetime import datetime
    lines: list[str] = []
    for label, ts in out:
        if ts:
            try:
                dt = datetime.fromtimestamp(int(ts))
                diff = dt - datetime.now()
                hh = int(diff.total_seconds() // 3600)
                mm = int((diff.total_seconds() % 3600) // 60)
                if diff.total_seconds() > 0:
                    lines.append(f"{label} 重置：{dt.strftime('%m/%d %H:%M')}（{hh}h{mm}m 后）")
                else:
                    lines.append(f"{label} 已过重置时间，下次 poll 会刷新")
            except Exception:
                lines.append(f"{label} 重置：{ts}")
        else:
            lines.append(f"{label} 已耗尽（无 reset 时间）")
    return ("Claude Max 用量已达上限", lines)


def _make_async_fire(task: ScheduledTask, bot, spawn_fn: SpawnFn):
    """生成无参 async coroutine。fire_task_now 直接 await，sync wrapper 投到 bot_loop。"""
    async def _fire():
        tag = f"[scheduler/{task.name}]"
        try:
            # ── 派单前预检 quota：用量耗尽时派出去也是死（Claude CLI 会立刻
            # 注入 synthetic rate_limit 事件让 PTY runner 退出），不如直接跳过、
            # 在群里发一条说明，等下次 cron 再试。
            try:
                from commands import fetch_quota_headers
                q = await asyncio.to_thread(fetch_quota_headers)
            except Exception as e:
                print(f"{tag} ⚠️ quota 预检失败 {type(e).__name__}: {e}，继续派单", flush=True)
                q = {"ok": False}
            skip = _quota_skip_reason(q)
            if skip is not None:
                reason, detail_lines = skip
                print(f"{tag} ⏸️ 跳过：{reason} | {' / '.join(detail_lines)}", flush=True)
                body = "原因：" + reason + "\n" + "\n".join(detail_lines)
                try:
                    await bot.feishu.send_post_to_chat(
                        chat_id=task.chat_id,
                        title=f"⏸️ 跳过本轮 · {task.topic_title}",
                        body_text=body,
                        mention_open_id=task.user_id,
                    )
                except Exception as e:
                    print(f"{tag} ⚠️ 发跳过通报失败: {type(e).__name__}: {e}", flush=True)
                return

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
