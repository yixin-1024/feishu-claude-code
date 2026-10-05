"""「做到一半就停」的通用治法：完成标记 + 自动续跑的共享文案与流式剥离。

codex（`codex exec`）和 agy（`agy -p`）都是「一次调用只跑一段有界的 agentic pass
就退出」的 CLI：模型做完一段活、吐一句「我正在… / 接下来…」，进程 rc=0 退出，
turn 就结束。runner 能看到的只有「进程正常退出 + result.status=SUCCESS」，于是把
半截活判成完成态——用户看到的就是"任务执行到一半自己停了"。

进程退出码区分不了「说完了」和「干完了」，唯一知道任务是否真完成的是模型自己，
所以这里给它一个只在【整件事真正全部完成】时才输出的完成标记，由各 runner 的续跑
循环判读：没吐标记就 resume 同一会话催它继续。

文案放在这里是为了单一真相源——两个后端的续跑提示必须逐字一致，否则同一套写法在
不同后端表现不同，排查时几乎发现不了。
"""

from __future__ import annotations

DONE_SENTINEL = "⟦CC_TASK_DONE⟧"


class IncompleteTaskError(RuntimeError):
    """A pass ended normally, but the task never confirmed completion.

    Keep the resumable session; do not flag this as a retryable upstream error,
    which would reset the continuation budget in the dispatcher.
    """

    def __init__(self, reason: str, session_id: str | None):
        super().__init__(reason)
        self.cc_session_id = session_id


def has_final_sentinel(text: str, sentinel: str) -> bool:
    """Only a standalone final line declares completion, not a quoted mention."""
    lines = text.rstrip().splitlines()
    return bool(sentinel and lines and lines[-1].strip() == sentinel)

CONTINUE_SYSTEM_HINT = (
    "【运行环境：自动续跑】你运行在一个会自动让你续跑的环境里——你这一轮回复结束后，"
    "只要任务还没真正全部完成，系统会自动把你唤醒继续，无需用户催促。因此：任务未完成时"
    "不要停下等待、不要只汇报进度或计划就收尾、不要问『要我继续吗』；请持续推进直到交付"
    "全部要求的产物。仅当【整件任务确实已全部完成】时，在你最终消息的最后单独一行原样输出"
    "完成标记：{sentinel}。任务尚未全部完成时，绝对不要输出该标记。"
    "\n⚠️ 既然环境已经会自动续跑你，就【不要】为了『回来接着干自己没干完的活』或『稍后回来自检/复核』"
    "去调 wake_me_in / schedule_cron 给自己排唤醒——那是多余的，会在任务早已干完后 fire 出"
    "『该自动唤醒已过期』的噪音。wake_me_in 只在你必须等一个【真实墙钟事件】（等 CI 跑完、等部署、"
    "等限流恢复、或用户明确要的定时提醒）时才用；『继续推进本任务』一律靠本轮内的自动续跑，别排 wake。"
    "\n(Environment auto-continues you: keep working across turns until everything is truly "
    "done; do NOT stop to report progress or ask to continue. Emit the exact marker "
    "{sentinel} on its own final line ONLY when the whole task is fully complete. Since you are "
    "auto-continued, do NOT call wake_me_in/schedule_cron just to resume your own unfinished work "
    "or self-check later — use wake_me_in only to await a real wall-clock event (CI, deploy, "
    "rate-limit recovery, or an explicit timed reminder).)"
)

CONTINUE_NUDGE = (
    "继续未完成的工作，直到全部要求的产物都交付完毕。若已全部完成，在最后单独一行输出 "
    "{sentinel}；若还没完成，就继续推进、不要输出该标记，也不要只汇报进度/计划就停下。"
)


def split_sentinel(buffer: str, sentinel: str) -> tuple[str, str, bool]:
    """把流式缓冲切成 `(可以外发的部分, 必须继续攒着的尾巴, 是否见到完成标记)`。

    完成标记会被模型拆成好几个 token 吐出来（`⟦CC_TA` + `SK_DONE⟧`），对单个
    delta 做 replace 拦不住——半截标记会漏到卡片上。所以凡是缓冲的尾巴**有可能**
    是标记的前缀，就先攒着不发，等下一个 delta 拼上再判。调用方在流结束时要把
    剩下的尾巴 flush 出去，否则最后几个字符会丢。
    """
    if not sentinel:
        return buffer, "", False
    saw = sentinel in buffer
    if saw:
        buffer = buffer.replace(sentinel, "")
    for n in range(min(len(sentinel) - 1, len(buffer)), 0, -1):
        if sentinel.startswith(buffer[-n:]):
            return buffer[: len(buffer) - n], buffer[-n:], saw
    return buffer, "", saw
