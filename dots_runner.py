"""OpenAI Dots（chatgpt.com/dots 网页版）作为 cc-lark 后端：Lark 私聊 ⇄ 豆包 的传话筒。

这里没有本地 CLI，也没有我们自己的会话：「大脑」是 OpenAI 服务端常驻的那个 Dot，对话
记录也在服务端。cc-lark 只做两件事（用户 2026-09-30 定的口径：「Lark 只是一个通讯方式，
网页里他怎么回复，这边就怎么回复过来」）：

    进：用户在 Lark **私聊**里发的每一条 → 立刻打进 Dots 网页（不排队、不开卡片、不等回复）
    出：常驻转发器盯着 Dot 的对话，它每说一条就往私聊发一条卡片；附件（图片 / 文件）
        下载下来按原样发成 Lark 图片 / 文件消息

    Lark 私聊 ─► dispatcher._dots_handle_message ─► forward() ─► browser-harness ─► Dots 页面
    Dots 页面 ─► dots_driver(stream) ─► _relay_loop ─► Lark 私聊

几条约束：
  * **不发 cc-lark 的系统提示词**，只递用户的原话（图片/文件走网页上传入口）。
  * **群里的消息一律不管**（dispatcher 里直接丢掉）；cron / 派活这类程序化入口走 run_dots，
    也只是把话递过去，回复同样回私聊。
  * **豆包的回复不是流式**：网页上先显示「正在输入」，然后整条出现。所以没必要开卡片
    刷新等待，来一条转一条。
  * **一个 Dot 只有一条对话**：发送走一把 FIFO 锁（_SEND_LOCKS），保证豆包收到的顺序和
    用户发的一致（先到的消息哪怕要先下载图片，后到的也得排在它后面）。
  * 已转发水位线落盘在 data/dots_state.json，转发器重启后从这里接着转，不重不漏。

配置（全局 DOTS_*，可用 <PROFILE>_DOTS_* 按 profile 覆盖）：
  DOTS_BROWSER_HARNESS_BIN  默认 ~/.local/bin/browser-harness
  DOTS_URL_MATCH            标签页 URL 子串，默认 chatgpt.com/dots
  DOTS_CHROME_PROFILE       登录了 Dots 的 Chrome 资料目录（如 "Profile 38"）。没开 Dots 标签时 driver
                            会自己开，这个只是最后一道兜底用的；不配的话 driver 会自己记下来
  DOTS_ROOM_ID / DOTS_NAME  账号下有多个 Dot 时指定一个（默认取最近活跃的）
  DOTS_POLL_SEC=2.5  DOTS_IDLE_POLL_SEC=15  DOTS_ACTIVE_WINDOW_SEC=300   转发器轮询节奏
  DOTS_NOTIFY_OPEN_ID       还没人私聊过时，豆包主动发的消息转给谁（默认白名单里第一个）
  DOTS_FORWARD_HISTORY=0    run_dots 入口：1 = 把话题历史也一起发给 Dot
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import tempfile
import time
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_DRIVER_PATH = os.path.join(_HERE, "dots_driver.py")
_DEFAULT_STATE_PATH = os.path.join(_HERE, "data", "dots_state.json")


def _state_path() -> str:
    # 测试用 CC_LARK_DOTS_STATE 隔离，别往仓库 data/ 里写假水位线
    return os.getenv("CC_LARK_DOTS_STATE") or _DEFAULT_STATE_PATH


# send / status 这类一次性调用的上限（发消息含上传附件，给宽一点）
_ONESHOT_TIMEOUT_SEC = 120
# 同一个 Dot 的发送顺序锁（asyncio.Lock 按到达顺序唤醒 = FIFO）
_SEND_LOCKS: dict[str, asyncio.Lock] = {}
_STATE_LOCK = asyncio.Lock()


def dots_cfg(profile_name: str, key: str, default: str = "") -> str:
    """<PROFILE>_DOTS_<KEY> 优先，其次 DOTS_<KEY>。"""
    if profile_name:
        v = os.getenv(f"{profile_name.upper()}_DOTS_{key}")
        if v is not None and v.strip() != "":
            return v.strip()
    v = os.getenv(f"DOTS_{key}")
    return v.strip() if v is not None and v.strip() != "" else default


def _key_for(profile_name: str) -> str:
    # 键用配置而不是房间 id：房间 id 要跑一次 driver 才知道，锁必须在那之前拿
    return dots_cfg(profile_name, "ROOM_ID") or dots_cfg(profile_name, "NAME") or "default"


def _send_lock(profile_name: str) -> asyncio.Lock:
    key = _key_for(profile_name)
    lock = _SEND_LOCKS.get(key)
    if lock is None:
        lock = _SEND_LOCKS[key] = asyncio.Lock()
    return lock


def resolve_harness_bin(profile_name: str = "") -> str:
    configured = dots_cfg(profile_name, "BROWSER_HARNESS_BIN")
    if configured:
        return os.path.expanduser(configured)
    local = os.path.expanduser("~/.local/bin/browser-harness")
    return local if os.path.exists(local) else "browser-harness"


# ── Lark 消息 → 发给 Dot 的内容 ─────────────────────────────
_TURN_HEADER_RE = re.compile(r"^\s*【本轮 · [^】]*】\s*")
_WAKE_HINT_RE = re.compile(r"^【⏰ 待办唤醒提醒】.*?(?:\n\n|$)", re.S)
_AT_MARK = "【用户刚刚 @ 你并说】"
_AT_EMPTY = "【用户刚刚 @ 你，没有新正文，请基于上方内容回复】"

_IMG_RE = re.compile(r"^\[用户发送了一张图片，路径：(?P<path>.+?)，请读取并分析这张图片，直接回复用中文\]\s*$", re.S)
_FILE_RE = re.compile(
    r"^\[用户发送了文件：(?P<name>.*?)，本地路径：(?P<path>.+?)。请根据需要读取该文件并分析，用中文回复。\]"
    r"(?:\n用户对这个文件的说明：(?P<caption>.*))?\s*$",
    re.S,
)
_VOICE_RE = re.compile(r"^\[用户发送了一条语音消息（\d+s），以下为自动转写，[^\]]*\]\n(?P<text>.*)$", re.S)
_POST_RE = re.compile(
    r"^\[用户发送了富文本消息，含 \d+ 张图片\]\n文字内容：(?P<caption>.*?)\n图片路径：\n(?P<paths>(?:  - .+\n?)+)"
    r"请读取并分析这些图片，结合文字回复（中文）。\s*$",
    re.S,
)


def _unwrap_media(text: str) -> tuple[str, list[str]]:
    """把 dispatcher 给本地 agent 写的媒体占位句还原成「正文 + 本地文件」。"""
    m = _IMG_RE.match(text)
    if m:
        return "", [m.group("path").strip()]
    m = _FILE_RE.match(text)
    if m:
        return (m.group("caption") or "").strip(), [m.group("path").strip()]
    m = _VOICE_RE.match(text)
    if m:
        return m.group("text").strip(), []
    m = _POST_RE.match(text)
    if m:
        caption = m.group("caption").strip()
        if caption == "（无文字说明）":
            caption = ""
        paths = [ln.strip()[2:].strip() for ln in m.group("paths").splitlines() if ln.strip().startswith("- ")]
        return caption, paths
    return text, []


def build_dots_prompt(message: str, forward_history: bool = False) -> tuple[str, list[str]]:
    """从 dispatcher 拼好的 message 里取出「用户这次说的话」和要上传的本地文件。"""
    text = message or ""
    text = _TURN_HEADER_RE.sub("", text, count=1)
    text = _WAKE_HINT_RE.sub("", text, count=1)
    history = ""
    if _AT_MARK in text:
        history, _, text = text.rpartition(_AT_MARK)
    elif _AT_EMPTY in text:
        # 用户只 @ 了没写字：唯一能递的就是上面那段话题历史
        text = text.replace(_AT_EMPTY, "")
        history = ""
    text, files = _unwrap_media(text.strip())
    files = [p for p in files if os.path.isfile(p)]
    if forward_history and history.strip():
        text = f"{history.strip()}\n\n{text}".strip()
    return text.strip(), files


# ── 状态：已转发水位线 / 转给谁 ───────────────────────────
def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(data: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


async def _update_state(profile_name: str, **fields) -> dict:
    async with _STATE_LOCK:
        data = _load_state()
        cur = dict(data.get(profile_name) or {})
        cur.update({k: v for k, v in fields.items() if v is not None})
        cur["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        data[profile_name] = cur
        _save_state(data)
        return cur


def get_state(profile_name: str) -> dict:
    return dict(_load_state().get(profile_name) or {})


def render_dot_text(ev: dict) -> str:
    """豆包一条消息的正文；附件没取回来的在末尾注明（取回来的另发成图片/文件消息）。"""
    body = (ev.get("text") or "").strip()
    fails = [f"📎 {f.get('name') or '附件'}（没能取回：{f.get('error')}）"
             for f in ev.get("files") or [] if f.get("error")]
    return "\n\n".join(p for p in (body, "\n".join(fails)) if p)


# ── 跑一次 driver ────────────────────────────────────────────
def _wake_path(profile_name: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"cc-dots-wake-{profile_name or 'default'}")


def wake_relay(profile_name: str) -> None:
    """用户刚发了消息：让转发器立刻回到快轮询（它在慢轮询时最多要等 DOTS_IDLE_POLL_SEC）。"""
    try:
        with open(_wake_path(profile_name), "a"):
            pass
        os.utime(_wake_path(profile_name), None)
    except OSError:
        pass


def _driver_env(profile_name: str, action: str, **extra) -> dict:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([os.path.expanduser("~/.local/bin"), env.get("PATH", "")])
    env["DOTS_ACTION"] = action
    for key in ("URL_MATCH", "ROOM_ID", "NAME", "POLL_SEC", "IDLE_POLL_SEC", "ACTIVE_WINDOW_SEC", "CHROME_PROFILE"):
        v = dots_cfg(profile_name, key)
        if v:
            env[f"DOTS_{key}"] = v
        else:
            env.pop(f"DOTS_{key}", None)
    env["DOTS_WAKE_FILE"] = _wake_path(profile_name)
    for k, v in extra.items():
        env[k] = v
    return env


async def _spawn_driver(profile_name: str, env: dict) -> asyncio.subprocess.Process:
    with open(_DRIVER_PATH, "rb") as f:
        code = f.read()
    proc = await asyncio.create_subprocess_exec(
        resolve_harness_bin(profile_name),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        # 独立进程组：要收掉时 killpg，连 browser-harness 起的子进程一起
        start_new_session=True,
        limit=16 * 1024 * 1024,  # 豆包一条长回复就是一行 JSON，默认 64KB 行长不够
    )
    try:
        proc.stdin.write(code)
        await proc.stdin.drain()
        proc.stdin.close()
    except BaseException:
        # 被取消在喂代码的半路：子进程还卡在读 stdin，不杀掉它会一直挂着
        _kill(proc)
        raise
    return proc


def _kill(proc) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


def _parse_event(line: bytes) -> Optional[dict]:
    s = line.decode("utf-8", "replace").strip()
    if not s.startswith("{"):
        return None
    try:
        ev = json.loads(s)
    except ValueError:
        return None
    return ev if isinstance(ev, dict) and "ev" in ev else None


class DotsError(RuntimeError):
    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code
        # 不让 dispatcher 当成「上游中断」去 resume 续跑：再发一遍会让 Dot 收到重复消息
        self.cc_retryable_resume = False


async def run_driver_oneshot(profile_name: str, action: str, **extra_env) -> dict:
    """send / status：返回 room + done 合并后的 dict。出错抛 DotsError。"""
    proc = await _spawn_driver(profile_name, _driver_env(profile_name, action, **extra_env))
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_ONESHOT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        _kill(proc)
        raise DotsError("timeout", f"browser-harness {action} 超过 {_ONESHOT_TIMEOUT_SEC}s 没返回")
    result: dict = {}
    for line in out.splitlines():
        ev = _parse_event(line)
        if not ev:
            continue
        if ev["ev"] == "error":
            raise DotsError(ev.get("code") or "error", ev.get("msg") or "unknown")
        if ev["ev"] in ("room", "sent", "done"):
            result.update({k: v for k, v in ev.items() if k != "ev"})
            if ev["ev"] == "sent":
                result["sent"] = ev.get("id")
    if not result.get("reason"):
        tail = err.decode("utf-8", "replace").strip()[-400:]
        raise DotsError("driver", f"browser-harness 没给出结果（rc={proc.returncode}）：{tail}")
    return result


# ── 进：把话递给豆包 ─────────────────────────────────────────
async def _send_now(profile_name: str, text: str, files: list[str]) -> str:
    fd, prompt_path = tempfile.mkstemp(prefix="cc-dots-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text or "")
    try:
        res = await run_driver_oneshot(
            profile_name, "send", DOTS_PROMPT_FILE=prompt_path, DOTS_FILES="\n".join(files or []),
        )
    finally:
        try:
            os.unlink(prompt_path)
        except OSError:
            pass
    if not res.get("sent"):
        raise DotsError("send_unconfirmed", "没确认送达豆包")
    return res["sent"]


async def forward(bot, prepare, *, user_id: str = "") -> str:
    """Lark 私聊的一条消息 → 豆包。返回它在 Dots 里的消息 id；没内容返回 ""。

    prepare：async () -> (text, files)，在顺序锁里执行（下载图片也算在排队里），
    这样先到的消息哪怕要下载附件，也一定先于后到的消息送达。出错抛 DotsError。
    """
    name = bot.profile.name
    start_relay(bot)
    async with _send_lock(name):
        text, files = await prepare()
        if not (text or "").strip() and not files:
            return ""
        sent = await _send_now(name, text, files)
    if user_id:
        await _update_state(name, user_id=user_id)
    wake_relay(name)
    return sent


async def run_dots(
    message: str,
    session_id: Optional[str] = None,
    profile_name: str = "",
    wake_context: Optional[dict] = None,
    **_ignored,
) -> tuple[str, Optional[str], bool]:
    """程序化入口（cron / 派活 / 话题里的老路径）：只把话递给豆包，回复由转发器发到私聊。

    其余 runner 通用参数（model / effort / cwd / append_system_prompt / 各种回调）对 Dot 都
    没有意义：模型和工具是它自己的，系统提示词也不能发过去——故意吞掉。
    """
    del session_id
    forward_history = dots_cfg(profile_name, "FORWARD_HISTORY", "0") in ("1", "true", "yes", "on")
    text, files = build_dots_prompt(message, forward_history=forward_history)
    if not text and not files:
        return "（没有可以转给豆包的内容）", None, False
    async with _send_lock(profile_name):
        try:
            await _send_now(profile_name, text, files)
        except DotsError as e:
            raise DotsError(e.code, f"❌ 没发到豆包：{e}") from e
    wake_relay(profile_name)
    return "↪️ 已发给豆包，它的回复会直接发到私聊里。", None, False


# ── 出：豆包说的每一条都转回 Lark 私聊 ─────────────────────────
_RELAYS: dict[str, asyncio.Task] = {}


def start_relay(bot) -> Optional[asyncio.Task]:
    """在 bot 的事件循环里调用（main.py 用 run_coroutine_threadsafe 包一层）。幂等。"""
    name = bot.profile.name
    task = _RELAYS.get(name)
    if task and not task.done():
        return task
    task = asyncio.get_running_loop().create_task(_relay_loop(bot))
    _RELAYS[name] = task
    return task


def relay_running(profile_name: str) -> bool:
    task = _RELAYS.get(profile_name)
    return bool(task and not task.done())


def _target_open_id(bot) -> str:
    st = get_state(bot.profile.name)
    if st.get("user_id"):
        return st["user_id"]
    explicit = dots_cfg(bot.profile.name, "NOTIFY_OPEN_ID")
    if explicit:
        return explicit
    owners = sorted(getattr(bot.profile, "allowed_open_ids", None) or [])
    return owners[0] if owners else ""


def _lark_client(bot):
    client = getattr(bot.feishu, "client", None)
    if client is None:
        raise DotsError("no_client", "这个 bot 不是 Lark 通道，发不了图片/文件")
    return client


_IMAGE_MAX = 10 * 1024 * 1024  # Lark 图片上传上限；更大的按文件发
_FILE_TYPES = {".pdf": "pdf", ".doc": "doc", ".docx": "doc", ".xls": "xls", ".xlsx": "xls",
               ".ppt": "ppt", ".pptx": "ppt", ".mp4": "mp4", ".opus": "opus"}


async def _send_msg(bot, open_id: str, msg_type: str, content: dict) -> None:
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    req = (
        CreateMessageRequest.builder().receive_id_type("open_id")
        .request_body(CreateMessageRequestBody.builder().receive_id(open_id)
                      .msg_type(msg_type).content(json.dumps(content)).build())
        .build()
    )
    resp = await _lark_client(bot).im.v1.message.acreate(req)
    if not resp.success():
        raise DotsError("lark", f"发 {msg_type} 消息失败 code={resp.code} {resp.msg}")


async def _send_image(bot, open_id: str, path: str) -> None:
    from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

    with open(path, "rb") as fh:
        req = CreateImageRequest.builder().request_body(
            CreateImageRequestBody.builder().image_type("message").image(fh).build()).build()
        resp = await _lark_client(bot).im.v1.image.acreate(req)
    if not resp.success():
        raise DotsError("lark", f"上传图片失败 code={resp.code} {resp.msg}")
    await _send_msg(bot, open_id, "image", {"image_key": resp.data.image_key})


async def _send_file(bot, open_id: str, path: str, name: str) -> None:
    from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

    ftype = _FILE_TYPES.get(os.path.splitext(name or path)[1].lower(), "stream")
    with open(path, "rb") as fh:
        req = CreateFileRequest.builder().request_body(
            CreateFileRequestBody.builder().file_type(ftype).file_name(name or os.path.basename(path))
            .file(fh).build()).build()
        resp = await _lark_client(bot).im.v1.file.acreate(req)
    if not resp.success():
        raise DotsError("lark", f"上传文件失败 code={resp.code} {resp.msg}")
    await _send_msg(bot, open_id, "file", {"file_key": resp.data.file_key})


async def deliver(bot, ev: dict, progress: Optional[dict] = None) -> None:
    """把豆包的一条消息发到私聊：正文一张卡片，附件逐个发成图片 / 文件。失败抛异常（不推水位线）。

    progress：同一条消息重试时记着哪些部分已经发出去了（正文 / 第几个附件），
    重试只补没发的，不会把正文再发一遍。
    """
    progress = progress if progress is not None else {}
    done_files = progress.setdefault("files", set())
    open_id = _target_open_id(bot)
    if not open_id:
        raise DotsError("no_target", "不知道该转给谁：还没人私聊过这个 bot，也没配 DOTS_NOTIFY_OPEN_ID")
    text = render_dot_text(ev)
    if text and not progress.get("text"):
        await bot.feishu.send_card_to_user(open_id, content=text, loading=False)
        progress["text"] = True
    for i, f in enumerate(ev.get("files") or []):
        path = f.get("path")
        if i in done_files or f.get("error") or not path or not os.path.isfile(path):
            continue
        is_image = (f.get("mime") or "").startswith("image/") and os.path.getsize(path) <= _IMAGE_MAX
        if is_image:
            await _send_image(bot, open_id, path)
        else:
            await _send_file(bot, open_id, path, f.get("name") or os.path.basename(path))
        done_files.add(i)


def _cleanup_files(ev: dict) -> None:
    for f in ev.get("files") or []:
        if f.get("path"):
            try:
                os.unlink(f["path"])
            except OSError:
                pass


async def relay_once(bot) -> None:
    """起一个常驻的 stream driver，豆包每说一条就转一条；driver 退出（标签页关了 /
    daemon 断了）就抛 DotsError，由 _relay_loop 退避重启，从水位线接着转。"""
    name = bot.profile.name
    after = get_state(name).get("delivered_id") or ""
    proc = await _spawn_driver(name, _driver_env(name, "stream", DOTS_AFTER=after))
    error = None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            ev = _parse_event(line)
            if not ev:
                continue
            kind = ev["ev"]
            if kind == "room":
                await _update_state(name, room_id=ev.get("room"))
            elif kind == "msg":
                progress: dict = {}
                try:
                    for attempt in range(3):
                        try:
                            await deliver(bot, ev, progress)
                            break
                        except Exception as e:  # noqa: BLE001
                            if attempt == 2:
                                raise DotsError("deliver", f"转发豆包消息失败：{e}") from e
                            await asyncio.sleep(2 * (attempt + 1))
                finally:
                    # 发完 / 彻底失败都删本地副本；失败时水位线没推，重启后 driver 会重新下载
                    _cleanup_files(ev)
                await _update_state(name, delivered_id=ev.get("id"))
            elif kind == "cursor":
                # 这一批里豆包的消息都已经转完了（上面是顺序处理的），可以推到批尾
                await _update_state(name, delivered_id=ev.get("id"))
            elif kind == "error":
                error = ev
    finally:
        _kill(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
    if error:
        raise DotsError(error.get("code") or "error", error.get("msg") or "unknown")
    raise DotsError("driver_exit", f"转发器的 browser-harness 退出了（rc={proc.returncode}）")


async def _relay_loop(bot) -> None:
    name = bot.profile.name
    backoff = 5.0
    while True:
        started = time.monotonic()
        try:
            await relay_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", type(e).__name__)
            print(f"[dots] 转发器 {name}: {code} {e}", flush=True)
        # 跑了挺久才断的（正常重连）马上重来；一起来就挂的（标签页没开 / 登录失效）退避
        backoff = 5.0 if time.monotonic() - started > 120 else min(backoff * 2, 300.0)
        await asyncio.sleep(backoff)
