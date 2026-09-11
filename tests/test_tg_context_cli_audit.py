"""Telegram 会话缓冲（tg_context）+ agent 手脚（tg-cli）的审计用例。

为什么这两个东西值得单独审：
  · Telegram Bot API **没有任何读历史的接口**。"被 @ 时自动读上下文"这件事
    100% 依赖 bot 自己把每条消息落成 jsonl。这份文件坏了 = 上下文静默消失，
    甚至（见下面几条 xfail）整个进程起不来 —— 比报错更难发现。
  · tg-cli 是 agent 唯一的发图/发文件/读上下文入口，跑在别人的 python3 上，
    只有标准库，没有单测网。

本文件只读审计：不改产品代码。**不碰真 Telegram API**（假 HTTP 服务器 +
CC_TG_API_ROOT），缓冲目录由 conftest 的 CC_TG_BUFFER_DIR 指到 tmp_path。

标 `xfail(strict=True)` 的用例 = 已确认的真 bug：现在必然失败，修好后会
变成 XPASS（strict 下报错），提醒把标记摘掉。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tg_context
from tg_context import TgBuffer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TG_CLI = os.path.join(REPO, "tg-cli")


# ══════════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════════

def _rec(buf, mid, text, *, thread="c-100", chat="-100", uid="777", name="Yixin",
         bot=False, ts=1700000000000, mtype="text", content=None, mentions=None):
    buf.record(
        message_id=mid, chat_id=chat, thread_id=thread, user_id=uid, name=name,
        is_bot=bot, msg_type=mtype,
        content=content if content is not None
        else json.dumps({"text": text}, ensure_ascii=False),
        ts_ms=ts, mentions=mentions or [],
    )


def _texts(msgs):
    return [json.loads(m.body.content).get("text") for m in msgs]


def _msg_line(mid, text, *, chat="-100", thread="c-100", uid="777", name="Yixin",
              ts=1700000000000, bot=False):
    return json.dumps({
        "op": "msg", "mid": mid, "chat": chat, "thread": thread, "uid": uid,
        "name": name, "bot": bot, "type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "ts": ts, "reply_to": "", "mentions": [],
    }, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════
# 1. 持久化正确性：跨"重启"回放
# ══════════════════════════════════════════════════════════════

def test_replay_keeps_content_order_names_and_shape():
    """重启后：内容 / 顺序 / 姓名表 / 鸭子类型字段全部一致。"""
    buf = TgBuffer("r1")
    _rec(buf, "-100:1", "第一句")
    _rec(buf, "-100:2", "第二句", uid="888", name="Charlie")
    _rec(buf, "-100:3", "bot 说的", uid="42", name="spxbot", bot=True)
    _rec(buf, "-200:9", "另一个群", thread="c-200", chat="-200", uid="999", name="Ann")

    before = buf.thread_messages("c-100")
    again = TgBuffer("r1")
    after = again.thread_messages("c-100")

    assert [m.message_id for m in after] == [m.message_id for m in before]
    assert _texts(after) == ["第一句", "第二句", "bot 说的"]
    assert [m.sender.sender_type for m in after] == ["user", "user", "app"]
    assert [m.create_time for m in after] == ["1700000000000"] * 3
    assert again.names(["777", "888", "42", "999"]) == {
        "777": "Yixin", "888": "Charlie", "42": "spxbot", "999": "Ann",
    }
    # 群之间不串台
    assert _texts(again.thread_messages("c-200")) == ["另一个群"]
    assert again.chat_of("-200:9") == "-200"


def test_update_text_final_answer_survives_replay():
    """卡片终稿（update_text）在回放后仍然是终稿，不是流式中间帧。"""
    buf = TgBuffer("r2")
    _rec(buf, "-100:1", "问题")
    _rec(buf, "-100:2", "⏳ 思考中...", uid="42", name="spxbot", bot=True)
    buf.update_text("-100:2", "答案：42")
    buf.update_text("-100:2", "答案：42（补充一句）")

    again = TgBuffer("r2")
    assert _texts(again.thread_messages("c-100")) == ["问题", "答案：42（补充一句）"]
    # 未知 mid 的 patch 是静默 no-op，不落盘、不新增记录
    lines_before = open(buf.path, encoding="utf-8").read().count("\n")
    buf.update_text("-100:不存在", "x")
    assert open(buf.path, encoding="utf-8").read().count("\n") == lines_before
    assert len(TgBuffer("r2").thread_messages("c-100")) == 2


def test_trim_leaves_no_dangling_index_before_or_after_replay(monkeypatch):
    """裁剪后索引不残留悬空引用，重启回放同样收敛到最后 N 条。"""
    monkeypatch.setattr(tg_context, "MAX_PER_THREAD", 3)
    buf = TgBuffer("r3")
    for i in range(8):
        _rec(buf, f"-100:{i}", f"m{i}")

    for lookup in (buf, TgBuffer("r3")):
        assert _texts(lookup.thread_messages("c-100")) == ["m5", "m6", "m7"]
        for gone in range(5):
            assert lookup.thread_of(f"-100:{gone}") == ""
            assert lookup.chat_of(f"-100:{gone}") == ""
            assert lookup.has(f"-100:{gone}") is False
        assert lookup.has("-100:7") is True
        # 索引大小 == 活着的记录数（没有摘不掉的僵尸键）
        assert len(lookup._index) == 3


def test_replay_tolerates_broken_empty_and_huge_lines(tmp_path):
    """坏行 / 半行 / 空行 / 超大行混进 jsonl 不能炸，好行照样读出来。"""
    buf = TgBuffer("r4")
    _rec(buf, "-100:1", "好行")
    huge = "z" * 3_000_000
    with open(buf.path, "a", encoding="utf-8") as f:
        f.write("\n")                                   # 空行
        f.write("{不是 json\n")                          # 坏行
        f.write("   \n")                                # 全空白
        f.write(_msg_line("-100:2", huge) + "\n")       # 超大行（3MB）
        f.write('{"op":"msg","mid":"-100:3","chat":"-1')  # 半行（无换行结尾）

    again = TgBuffer("r4")
    msgs = again.thread_messages("c-100")
    assert [m.message_id for m in msgs] == ["-100:1", "-100:2"]
    assert _texts(msgs)[1] == huge


@pytest.mark.parametrize("junk", ["[]", "null", "123", '"str"'])
def test_replay_tolerates_valid_json_non_object_lines(junk):
    """`[]` / `null` / `123` 这种"是合法 JSON、但不是 dict"的行不该炸。

    实际：TgBuffer.__init__ 抛 AttributeError（'list' object has no attribute
    'get'），BotInstance → main.py:124 没有 try，**整个 cc-lark 进程起不来**。
    """
    buf = TgBuffer("r5")
    _rec(buf, "-100:1", "好行")
    with open(buf.path, "a", encoding="utf-8") as f:
        f.write(junk + "\n")
    assert _texts(TgBuffer("r5").thread_messages("c-100")) == ["好行"]


def test_replay_tolerates_torn_multibyte_tail(tmp_path):
    """被 kill -9 撕裂在汉字中间的尾行不该让缓冲永久打不开。

    实际：`open(..., encoding='utf-8').readlines()` 抛 UnicodeDecodeError
    （ValueError 的子类，不是 OSError），_load 的 except OSError 接不住 →
    每次启动都炸，且没有任何自愈（compact 在崩溃点之后才跑）。
    """
    buf = TgBuffer("r6")
    _rec(buf, "-100:1", "好行")
    with open(buf.path, "ab") as f:
        # 写到"断"字的第 2 个字节就断电了
        f.write(b'{"op":"msg","mid":"-100:2","content":"' + "断".encode()[:2])
    assert _texts(TgBuffer("r6").thread_messages("c-100")) == ["好行"]


def test_enospc_partial_write_bricks_the_next_boot(capsys):
    """磁盘满写到一半 → _append_line 吞掉 OSError（对），文件尾巴留了半个汉字；
    下一次 TgBuffer(...) 直接 UnicodeDecodeError（错），而且每次启动都炸。"""
    buf = TgBuffer("enospc")
    _rec(buf, "-100:1", "写得进去的一句")

    real_open = open

    class _HalfWriter:
        def __init__(self, path):
            self._f = real_open(path, "ab")

        def write(self, text):
            raw = text.encode("utf-8")
            cut = len(raw) // 2
            while cut < len(raw) and (raw[cut] & 0xC0) != 0x80:
                cut += 1                      # 停在多字节字符的续字节上
            self._f.write(raw[:cut])
            self._f.flush()
            raise OSError(28, "No space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._f.close()
            return False

    # 用独立的 MonkeyPatch 上下文：monkeypatch fixture 是**整个用例共享**的，
    # 在它上面 undo() 会连 conftest 的 CC_TG_BUFFER_DIR 隔离一起撤掉，缓冲路径
    # 会掉回真实的 ~/.feishu-claude/tg。
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tg_context, "open",
                   lambda path, *a, **kw: _HalfWriter(path), raising=False)
        _rec(buf, "-100:2", "磁盘满的时候正在写的一句话")
    assert "缓冲写盘失败" in capsys.readouterr().out
    with open(buf.path, "rb") as f:
        raw = f.read()
    with pytest.raises(UnicodeDecodeError):     # 前提：尾行确实断在多字节字符中间
        raw.decode("utf-8")

    assert _texts(TgBuffer("enospc").thread_messages("c-100")) == ["写得进去的一句"]


def test_compact_keeps_every_live_record(tmp_path, capsys):
    """文件超 COMPACT_BYTES → 压实，活记录一条不少，patch 已固化。"""
    buf_path = os.path.join(tg_context.buffer_dir(), "big.jsonl")
    os.makedirs(os.path.dirname(buf_path), exist_ok=True)
    pad = "x" * 2000
    n = 3000
    with open(buf_path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(_msg_line(f"-100:{i}", f"m{i} {pad}", ts=1700000000000 + i) + "\n")
        f.write(json.dumps({"op": "text", "mid": f"-100:{n - 1}",
                            "content": "终稿"}, ensure_ascii=False) + "\n")
    assert os.path.getsize(buf_path) > tg_context.COMPACT_BYTES

    buf = TgBuffer("big")
    live = buf.thread_messages("c-100", limit=10 ** 6)
    assert len(live) == tg_context.MAX_PER_THREAD
    assert "缓冲已压实" in capsys.readouterr().out
    # 压实后文件行数 == 活记录数，且再读一次内容完全一致
    assert sum(1 for _ in open(buf_path, encoding="utf-8")) == len(live)
    again = TgBuffer("big").thread_messages("c-100", limit=10 ** 6)
    assert [m.message_id for m in again] == [m.message_id for m in live]
    assert _texts(again)[-1] == "终稿"
    assert again[0].message_id == f"-100:{n - tg_context.MAX_PER_THREAD}"


def test_quiet_chats_survive_a_noisy_neighbour_and_compaction(monkeypatch, capsys):
    """安静的群不能被吵闹的群挤掉。

    老实现有个**全局**行预算（REPLAY_TAIL_LINES）叠加**每群**上限
    （MAX_PER_THREAD）：群 A 安静下来后，别的群刷够行数就把 A 整个挤出回放窗口，
    紧接着一次压实把它从磁盘上永久删掉 —— A 的 last_seen 从此指向一条不存在的
    消息，上下文静默清零。现在改成全量回放（内存有每群上限兜着），这条必须成立。
    """
    monkeypatch.setattr(tg_context, "COMPACT_BYTES", 1024)
    path = os.path.join(tg_context.buffer_dir(), "evict.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_msg_line("-100:1", "安静群里唯一的一句", chat="-100",
                          thread="c-100") + "\n")
        for i in range(200):                      # 另一个群把它挤出窗口
            f.write(_msg_line(f"-200:{i}", f"吵闹 {i}", chat="-200",
                              thread="c-200") + "\n")

    buf = TgBuffer("evict")
    assert _texts(buf.thread_messages("c-100")) == ["安静群里唯一的一句"]
    assert "缓冲已压实" in capsys.readouterr().out
    on_disk = open(path, encoding="utf-8").read()
    assert "安静群里唯一的一句" in on_disk               # 压实也留着它
    assert buf.thread_of("-100:1") == "c-100"
    # 吵闹群自己仍然受每群上限约束
    assert len(buf.thread_messages("c-200", limit=10 ** 6)) <= tg_context.MAX_PER_THREAD


def test_unwritable_dir_only_costs_context(monkeypatch, capsys):
    """目录只读：内存里照样有，进程不能挂（消息处理不能因为写不进缓冲而失败）。"""
    ro = os.path.join(tg_context.buffer_dir(), "ro")
    os.makedirs(ro, exist_ok=True)
    os.chmod(ro, 0o500)
    try:
        monkeypatch.setenv("CC_TG_BUFFER_DIR", ro)
        buf = TgBuffer("p_ro")
        _rec(buf, "-100:1", "内存里还是有的")
        assert _texts(buf.thread_messages("c-100")) == ["内存里还是有的"]
        assert "缓冲写盘失败" in capsys.readouterr().out
        assert not os.path.exists(buf.path)
    finally:
        os.chmod(ro, 0o700)


def test_disk_full_mid_write_is_swallowed(monkeypatch, capsys):
    """磁盘满（写到一半 ENOSPC）只丢上下文，不冒泡到消息处理。"""
    class _Full(io.StringIO):
        def write(self, _s):
            raise OSError(28, "No space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    buf = TgBuffer("p_full")
    monkeypatch.setattr(tg_context, "open", lambda *a, **kw: _Full(), raising=False)
    _rec(buf, "-100:1", "写不进去")
    assert _texts(buf.thread_messages("c-100")) == ["写不进去"]
    assert "缓冲写盘失败" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════
# 2. 并发写（轮询线程 + bot_loop 线程）
# ══════════════════════════════════════════════════════════════

def test_concurrent_record_never_loses_or_tears_lines(monkeypatch):
    """真 threading 几千次 record：不丢记录、不出半行 JSON、单个写者内部保序。"""
    monkeypatch.setattr(tg_context, "MAX_PER_THREAD", 10 ** 6)
    monkeypatch.setattr(tg_context, "COMPACT_BYTES", 10 ** 12)
    buf = TgBuffer("conc")
    n = 1200
    fat = "字" * 6000        # 单行 ≈18KB，越过 8KB 的 io buffer 边界

    def writer(tag, big):
        for i in range(n):
            _rec(buf, f"{tag}:{i}", (fat if big else f"{tag}-{i}"),
                 ts=1700000000000 + i)

    threads = [
        threading.Thread(target=writer, args=("A", False)),
        threading.Thread(target=writer, args=("B", True)),
        threading.Thread(target=writer, args=("C", False)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    mem = [m.message_id for m in buf.thread_messages("c-100", limit=10 ** 6)]
    assert len(mem) == 3 * n

    bad, ops = 0, {"msg": 0, "text": 0}
    with open(buf.path, encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                bad += 1
                continue
            ops[obj.get("op", "msg")] += 1
    assert bad == 0, "append + 单行 json.dumps 被撕成半行"
    assert ops["msg"] == 3 * n

    disk = [m.message_id for m in TgBuffer("conc").thread_messages("c-100", limit=10 ** 6)]
    assert sorted(disk) == sorted(mem)
    for tag in "ABC":
        want = [f"{tag}:{i}" for i in range(n)]
        assert [m for m in mem if m.startswith(tag)] == want
        # 同一个写者的相对顺序在盘上也必须稳定（跨写者的交错顺序无所谓）
        assert [m for m in disk if m.startswith(tag)] == want


def test_concurrent_update_text_is_thread_safe(monkeypatch):
    """record 与 update_text 并发跑同一批 mid：终稿不丢、不串。"""
    monkeypatch.setattr(tg_context, "MAX_PER_THREAD", 10 ** 6)
    buf = TgBuffer("conc2")
    n = 800
    for i in range(n):
        _rec(buf, f"-100:{i}", "⏳ 思考中...", uid="42", bot=True)

    errors: list[BaseException] = []

    def patcher(lo, hi):
        try:
            for i in range(lo, hi):
                buf.update_text(f"-100:{i}", f"终稿{i}")
        except BaseException as e:       # noqa: BLE001
            errors.append(e)

    half = n // 2
    ts = [threading.Thread(target=patcher, args=(0, half)),
          threading.Thread(target=patcher, args=(half, n)),
          threading.Thread(target=lambda: [_rec(buf, f"-100:new{i}", f"新{i}")
                                           for i in range(n)])]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors

    assert _texts(buf.thread_messages("c-100", limit=10 ** 6))[:n] == [
        f"终稿{i}" for i in range(n)]
    reloaded = _texts(TgBuffer("conc2").thread_messages("c-100", limit=10 ** 6))
    assert reloaded[:n] == [f"终稿{i}" for i in range(n)]


def test_patch_written_before_its_record_is_not_lost(monkeypatch):
    """确定性复现"落盘顺序被反转"：卡住 record 的写盘，让 update_text 抢先落盘。

    实际：文件里 `op:text` 在 `op:msg` 之前 → 回放时 _patch_text 找不到记录直接
    return，patch 被丢弃，重启后上下文里 bot 的终稿变回 "⏳ 思考中..."。
    """
    buf = TgBuffer("race")
    gate, release = threading.Event(), threading.Event()
    real_makedirs = tg_context.os.makedirs
    t = threading.Thread(
        target=lambda: _rec(buf, "-100:9", "⏳ 思考中...", uid="42", bot=True))

    def slow_makedirs(*a, **kw):
        # 只卡住写线程；主线程照常写，确定性地让 patch 行先落盘
        if threading.current_thread() is t and not release.is_set():
            gate.set()
            release.wait(5)
        return real_makedirs(*a, **kw)

    monkeypatch.setattr(tg_context.os, "makedirs", slow_makedirs)
    t.start()
    assert gate.wait(5)                 # 内存已插入、msg 行还没落盘
    buf.update_text("-100:9", "最终结论")
    release.set()
    t.join(5)

    assert _texts(buf.thread_messages("c-100")) == ["最终结论"]      # 内存对
    assert _texts(TgBuffer("race").thread_messages("c-100")) == ["最终结论"]


# ══════════════════════════════════════════════════════════════
# 3. 和 thread_context 的联动
# ══════════════════════════════════════════════════════════════

class _StubClient:
    """FeishuClient 的最小替身：只提供 thread_context 会碰的四个入口。"""

    def __init__(self, buf, app_id="8899"):
        self.buffer = buf
        self._app_id = app_id
        self.downloads: list[tuple] = []

    async def list_thread_messages(self, thread_id, limit=200):
        return self.buffer.thread_messages(thread_id, limit=limit)

    async def batch_resolve_names(self, open_ids):
        return self.buffer.names(list(open_ids or []))

    async def download_file(self, message_id, file_key, msg_type="file", file_name=""):
        self.downloads.append((message_id, file_key, msg_type, file_name))
        return f"/tmp/tg-dl/{file_key}"

    def get_card_text(self, message_id):
        return ""


def _build(buf, last_seen, current, app_id="8899"):
    import asyncio

    import thread_context
    client = _StubClient(buf, app_id)
    ctx, paths, err = asyncio.run(
        thread_context.build_thread_context(client, "c-100", last_seen, current))
    return client, ctx, paths, err


def test_thread_context_reads_tg_buffer_end_to_end():
    buf = TgBuffer("tc1")
    _rec(buf, "-100:1", "早就看过的")
    _rec(buf, "-100:2", "上次处理到这")
    _rec(buf, "-100:3", "未读的问题")
    _rec(buf, "-100:4", "我的回答", uid="8899", name="spxbot", bot=True)
    _rec(buf, "-100:5", "", mtype="image",
         content=json.dumps({"image_key": "AgACphoto"}))
    _rec(buf, "-100:6", "", mtype="file",
         content=json.dumps({"file_key": "BQAdoc", "file_name": "报告 v2.pdf"}))
    _rec(buf, "-100:7", "当前这条（要被排除）")

    client, ctx, paths, err = _build(buf, "-100:2", "-100:7")
    assert err is None
    assert ctx.startswith("【话题新增 · 4 条（距上次处理后）】")
    assert "早就看过的" not in ctx and "上次处理到这" not in ctx   # last_seen 之前
    assert "当前这条" not in ctx                                  # 当前那条被排除
    assert "[1] Yixin (11-15" in ctx                              # 姓名来自缓冲姓名表
    assert "[2] bot(自己) (11-15" in ctx                          # 自己的消息标 bot(自己)
    assert "[文件: 报告 v2.pdf]" in ctx
    # 附件真的走了 download_file（打桩，不下载）
    assert client.downloads == [
        ("-100:5", "AgACphoto", "image", ""),
        ("-100:6", "BQAdoc", "file", "报告 v2.pdf"),
    ]
    assert paths == ["/tmp/tg-dl/AgACphoto", "/tmp/tg-dl/BQAdoc"]


def test_thread_context_timestamps_come_from_ms_string():
    """create_time 必须是毫秒字符串，_fmt_time 才认得（给秒就会显示 1970）。"""
    import thread_context
    buf = TgBuffer("tc2")
    _rec(buf, "-100:1", "锚", ts=1700000000000)
    _rec(buf, "-100:2", "看这条", ts=1767225600000)      # 2026-01-01 08:00 CST
    msg = buf.thread_messages("c-100")[1]
    assert msg.create_time == "1767225600000" and isinstance(msg.create_time, str)
    assert thread_context._fmt_time(msg.create_time) == time.strftime(
        "%m-%d %H:%M", time.localtime(1767225600))
    _, ctx, _, _ = _build(buf, "-100:1", "")
    assert thread_context._fmt_time(msg.create_time) in ctx


def test_evicted_last_seen_yields_silent_empty_context(monkeypatch):
    """last_seen 被裁掉/被压实删掉 → 上下文静默为空且 error=None，
    调用方分不清"真没历史"和"历史被自己扔了"。"""
    monkeypatch.setattr(tg_context, "MAX_PER_THREAD", 3)
    buf = TgBuffer("tc3")
    for i in range(6):
        _rec(buf, f"-100:{i}", f"m{i}")
    _, ctx, paths, err = _build(buf, "-100:0", "-100:5")      # -100:0 已被裁掉
    assert ctx == "" and paths == [] and err is None


def test_bot_label_needs_app_id_match():
    """_app_id 还没被 getMe 填上（启动早期）时，bot 自己的消息只会退成
    bot_<末6>，不会误标成别人 —— 记录一下这个已知形态。"""
    buf = TgBuffer("tc4")
    _rec(buf, "-100:1", "锚")
    _rec(buf, "-100:2", "我的回答", uid="8899", name="spxbot", bot=True)
    _, ctx, _, _ = _build(buf, "-100:1", "", app_id="")
    assert "bot_8899" in ctx and "bot(自己)" not in ctx


def test_mention_placeholder_is_not_persisted():
    """缓冲只存 mention 的 id/name，丢了 key（`@bot` 那个字面量）：
    实时路径把 @bot 剥掉了，重放到上下文里的历史消息却剥不掉。"""
    from feishu_post import strip_lark_mentions

    class _LiveMention:
        def __init__(self, mid, key, name):
            self.id, self.key, self.name = mid, key, name

    live = [_LiveMention("8899", "@spx_bot", "spx_bot")]
    text = "@spx_bot 帮我看看这个"
    assert strip_lark_mentions(text, live) == "帮我看看这个"

    buf = TgBuffer("tc5")
    _rec(buf, "-100:1", text, mentions=[{"id": "8899", "name": "spx_bot"}])
    replayed = buf.thread_messages("c-100")[0]
    assert [m.id for m in replayed.mentions] == ["8899"]
    assert [m.key for m in replayed.mentions] == [""]          # key 没了
    assert strip_lark_mentions(
        json.loads(replayed.body.content)["text"], replayed.mentions) == text


# ══════════════════════════════════════════════════════════════
# 4. tg-cli 端到端（假 Bot API，绝不碰真 Telegram）
# ══════════════════════════════════════════════════════════════

class _FakeTelegram:
    """够 tg-cli 用的假 Bot API：记下每个请求的原始 body，好验 multipart 形状。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.next_error: dict | None = None
        self._srv = None

    def start(self) -> str:
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                token, _, method = self.path.lstrip("/").partition("/")
                ctype = self.headers.get("Content-Type", "")
                payload = {}
                if ctype.startswith("application/json"):
                    payload = json.loads(raw or b"{}")
                api.requests.append({
                    "method": method, "token": token.removeprefix("bot"),
                    "ctype": ctype, "raw": raw, "json": payload,
                })
                if api.next_error is not None:
                    body, api.next_error = api.next_error, None
                    status = 400
                else:
                    chat = str(payload.get("chat_id") or api._multipart_chat(raw))
                    status = 200
                    body = {"ok": True, "result": {
                        "message_id": 4242,
                        "chat": {"id": int(chat) if chat.lstrip("-").isdigit() else chat},
                        "date": 1700000000,
                    }}
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._srv.server_port}"

    @staticmethod
    def _multipart_chat(raw: bytes) -> str:
        marker = b'name="chat_id"\r\n\r\n'
        if marker not in raw:
            return "0"
        return raw.split(marker, 1)[1].split(b"\r\n", 1)[0].decode()

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()

    def last(self, method: str) -> dict:
        hits = [r for r in self.requests if r["method"] == method]
        assert hits, f"没有收到 {method} 请求：{[r['method'] for r in self.requests]}"
        return hits[-1]


@pytest.fixture
def fake_tg():
    api = _FakeTelegram()
    url = api.start()
    api.url = url
    yield api
    api.stop()


@pytest.fixture
def cli(tmp_path, fake_tg, monkeypatch):
    """把 tg-cli 原样复制到 tmp（BASE_DIR 跟着走），配一份假 .env。

    复制是为了**隔离掉仓库真 .env 里的真 token** —— resolve_token 找不到
    <PROFILE>_BOT_TOKEN 时会回退 TG_BOT_TOKEN，仓库里那个是真的。
    """
    home = tmp_path / "cli"
    home.mkdir()
    script = home / "tg-cli"
    script.write_bytes(open(TG_CLI, "rb").read())
    script.chmod(0o755)
    (home / ".env").write_text('AUDIT_BOT_TOKEN="111:AAfake-token"\n', encoding="utf-8")

    def run(*args, stdin=None, env=None, profile="audit"):
        e = dict(os.environ)
        e["CC_TG_API_ROOT"] = fake_tg.url
        # bot_config 在 import 时 load_dotenv 过，仓库 .env 里的真 TG_BOT_TOKEN
        # 已经在 os.environ 里；子进程里必须清掉，否则"token 缺失"这条根本测不出来
        for k in [k for k in e if k.endswith("_BOT_TOKEN")]:
            e.pop(k, None)
        e.pop("CC_LARK_CHAT_ID", None)
        e.pop("CC_LARK_MESSAGE_ID", None)
        e.pop("CC_LARK_THREAD_ID", None)
        e.update(env or {})
        cmd = [sys.executable, str(script)]
        if profile:
            cmd += ["--profile", profile]
        return subprocess.run(cmd + list(args), input=stdin, capture_output=True,
                              text=True, timeout=60, env=e)

    run.home = home
    return run


def _field(part) -> str:
    """multipart 里的普通字段没有 charset 头，按 form-data 惯例（也是 Telegram
    的口径）当 UTF-8 解。"""
    return part.get_payload(decode=True).decode("utf-8")


def _multipart_parts(req: dict) -> dict:
    """用独立的 email 解析器验 multipart 合法性 → {字段名: part}。"""
    blob = b"Content-Type: " + req["ctype"].encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + req["raw"]
    msg = BytesParser(policy=policy.default).parsebytes(blob)
    assert msg.is_multipart(), "multipart 拼装非法：解析器认不出分段"
    out = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        assert name, f"缺 name 的分段: {part.items()}"
        out[name] = part
    return out


def test_send_text_shape_and_reply_key_split(cli, fake_tg):
    r = cli("send", "--text", "跑完了 ✅", env={
        "CC_LARK_MESSAGE_ID": "-1001234567890:987",
    })
    assert r.returncode == 0, r.stderr
    req = fake_tg.last("sendMessage")
    assert req["token"] == "111:AAfake-token"          # 现场从 .env 读，没走 env
    assert req["json"] == {
        "chat_id": "-1001234567890",                   # 复合 key 里拆出来的负号 chat
        "text": "跑完了 ✅",
        "reply_parameters": {"message_id": 987, "allow_sending_without_reply": True},
        "link_preview_options": {"is_disabled": True},
    }
    assert json.loads(r.stdout) == {"ok": True, "chat_id": -1001234567890,
                                    "message_id": 4242}


def test_send_text_from_stdin_and_explicit_dash(cli, fake_tg):
    for args in (("send", "--text"), ("send", "--text", "-")):
        r = cli(*args, stdin="从 stdin 读的正文\n", env={"CC_LARK_CHAT_ID": "-100"})
        assert r.returncode == 0, r.stderr
        assert fake_tg.last("sendMessage")["json"]["text"] == "从 stdin 读的正文\n"


def test_send_text_truncates_at_4096_silently(cli, fake_tg):
    r = cli("send", "--text", "字" * 5000, env={"CC_LARK_CHAT_ID": "-100"})
    assert r.returncode == 0, r.stderr
    sent = fake_tg.last("sendMessage")["json"]["text"]
    assert len(sent) == 4096                    # 静默截断，多出来的 904 字没了
    assert "截断" not in r.stdout + r.stderr    # 也不提示


def test_send_image_multipart_is_wellformed_with_cjk_and_space_name(cli, fake_tg, tmp_path):
    img = tmp_path / "对 比 图.png"
    blob = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40
    img.write_bytes(blob)
    r = cli("send", "--image", str(img), "--caption", "左边旧的，右边新的",
            env={"CC_LARK_MESSAGE_ID": "-100:55"})
    assert r.returncode == 0, r.stderr

    req = fake_tg.last("sendPhoto")
    parts = _multipart_parts(req)
    assert set(parts) == {"chat_id", "caption", "reply_parameters", "photo"}
    assert _field(parts["chat_id"]) == "-100"
    assert _field(parts["caption"]) == "左边旧的，右边新的"
    # 复合字段按 Bot API 要求做 JSON 编码
    assert json.loads(_field(parts["reply_parameters"])) == {
        "message_id": 55, "allow_sending_without_reply": True}
    assert parts["photo"].get_payload(decode=True) == blob     # 文件字节零污染
    assert 'filename="对 比 图.png"'.encode() in req["raw"]     # 中文+空格文件名原样带 UTF-8
    assert b"Content-Type: image/png" in req["raw"]


def test_send_file_uses_sendDocument_and_octet_stream_fallback(cli, fake_tg, tmp_path):
    doc = tmp_path / "报告.weirdext"
    doc.write_bytes(b"hello" * 100)
    r = cli("send", "--file", str(doc), env={"CC_LARK_CHAT_ID": "-100"})
    assert r.returncode == 0, r.stderr
    req = fake_tg.last("sendDocument")
    parts = _multipart_parts(req)
    assert set(parts) == {"chat_id", "document"}          # caption=None 不发空字段
    assert b"Content-Type: application/octet-stream" in req["raw"]
    assert parts["document"].get_payload(decode=True) == b"hello" * 100


def test_long_caption_is_truncated_to_telegram_limit(cli, fake_tg, tmp_path):
    """Bot API 的 caption 上限是 1024；超了返回 400 MESSAGE_CAPTION_TOO_LONG，
    整张图发不出去。实际：tg-cli 原样发 2000 字。"""
    img = tmp_path / "a.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    r = cli("send", "--image", str(img), "--caption", "长" * 2000,
            env={"CC_LARK_CHAT_ID": "-100"})
    assert r.returncode == 0, r.stderr
    caption = _field(_multipart_parts(fake_tg.last("sendPhoto"))["caption"])
    assert len(caption) <= 1024


def test_filename_with_quote_does_not_break_the_header(cli, fake_tg, tmp_path):
    doc = tmp_path / 'a"b.txt'
    doc.write_bytes(b"x")
    r = cli("send", "--file", str(doc), env={"CC_LARK_CHAT_ID": "-100"})
    assert r.returncode == 0, r.stderr
    parts = _multipart_parts(fake_tg.last("sendDocument"))
    name = parts["document"].get_filename()
    # 取舍：**清洗成 `_`** 而不是按 RFC 转义 —— 转义在不同服务端的解析差异很大，
    # 而这个字段只影响 Telegram 上显示的文件名，内容和后缀都不受影响。
    assert name == "a_b.txt"
    assert '"' not in name and name.endswith(".txt")


def test_missing_file_and_missing_target_die_cleanly(cli, fake_tg, tmp_path):
    r = cli("send", "--image", str(tmp_path / "没有这个.png"),
            env={"CC_LARK_CHAT_ID": "-100"})
    assert r.returncode == 1
    assert "文件不存在" in r.stderr and "Traceback" not in r.stderr
    assert not fake_tg.requests

    r = cli("send", "--text", "hi")                      # 没有 chat 也没有锚点
    assert r.returncode == 1 and "拿不到 chat id" in r.stderr

    r = cli("send", env={"CC_LARK_CHAT_ID": "-100"})
    assert r.returncode == 1 and "需要 --text / --image / --file 之一" in r.stderr

    r = cli("send", "--text", "   ", env={"CC_LARK_CHAT_ID": "-100"})
    assert r.returncode == 1 and "--text 为空" in r.stderr


def test_missing_token_error_names_the_env_file(cli, fake_tg):
    (cli.home / ".env").write_text("# 什么都没有\n", encoding="utf-8")
    r = cli("send", "--text", "hi", env={"CC_LARK_CHAT_ID": "-100"})
    assert r.returncode == 1
    assert "找不到 AUDIT_BOT_TOKEN" in r.stderr and str(cli.home) in r.stderr
    assert not fake_tg.requests


def test_api_error_is_reported_not_traced(cli, fake_tg):
    fake_tg.next_error = {"ok": False, "error_code": 403,
                          "description": "Forbidden: bot was blocked by the user"}
    r = cli("send", "--text", "hi", env={"CC_LARK_CHAT_ID": "-100"})
    assert r.returncode == 1
    assert "[403]" in r.stderr and "bot was blocked" in r.stderr
    assert "Traceback" not in r.stderr


def test_reply_to_accepts_a_composite_key_with_negative_chat(cli, fake_tg):
    r"""Telegram 群 id 一律是负数，所以复合锚点必然以 `-` 开头。argparse 只
    放行"看起来像负数"的值（^-\d+$），`-1001234:55` 不匹配 → 报
    `expected one argument`。`--chat -1001234567890` 反而没事（纯负数）。
    绕法是 `--reply-to=-1001234:55`。"""
    r = cli("send", "--text", "hi", "--reply-to", "-1001234:55",
            env={"CC_LARK_CHAT_ID": "-1001234"})
    assert r.returncode == 0, r.stderr


def test_non_numeric_reply_anchor_dies_cleanly(cli, fake_tg):
    r = cli("send", "--text", "hi", "--reply-to=-100:om_xxx",
            env={"CC_LARK_CHAT_ID": "-100"})
    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 1


def test_network_failure_dies_cleanly(cli, fake_tg):
    """假服务器关掉 = 连接被拒，等价于线上 DNS/超时/断网。"""
    fake_tg.stop()
    r = cli("send", "--text", "hi", env={"CC_LARK_CHAT_ID": "-100"})
    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 1


def test_context_filters_by_chat_thread_and_applies_patches(cli, tmp_path, monkeypatch):
    """context 子命令：只列当前 chat/thread，op:text 的终稿要覆盖上去。"""
    buf_dir = tmp_path / "buf"
    buf_dir.mkdir()
    path = buf_dir / "audit.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(_msg_line("-100:1", "群里第一句") + "\n")
        f.write(_msg_line("-100:2", "⏳ 思考中...", uid="42", name="spxbot",
                          bot=True) + "\n")
        f.write(json.dumps({"op": "text", "mid": "-100:2",
                            "content": "终稿在这"}, ensure_ascii=False) + "\n")
        f.write(_msg_line("-100:3", "子会话里的", thread="t80") + "\n")
        f.write(_msg_line("-200:1", "别的群", chat="-200", thread="c-200") + "\n")

    env = {"CC_TG_BUFFER_DIR": str(buf_dir), "CC_LARK_CHAT_ID": "-100"}
    r = cli("context", "--limit", "10", env=env)
    assert r.returncode == 0, r.stderr
    assert "群里第一句" in r.stdout
    assert "终稿在这" in r.stdout and "思考中" not in r.stdout
    assert "spxbot(bot)" in r.stdout
    assert "别的群" not in r.stdout                    # chat 过滤生效
    assert "子会话里的" in r.stdout                    # 没给 --thread 时同 chat 全收

    r = cli("context", "--thread", "t80", env=env)
    assert r.stdout.count("[") >= 1 and "群里第一句" not in r.stdout
    assert "子会话里的" in r.stdout

    r = cli("context", "--limit", "1", env=env)
    assert r.stdout.strip().startswith("[1]") and len(r.stdout.strip().splitlines()) == 1

    r = cli("context", env={**env, "CC_LARK_CHAT_ID": "-999"})
    assert "(缓冲里还没有这个会话的消息)" in r.stdout

    r = cli("context", env={"CC_TG_BUFFER_DIR": str(tmp_path / "empty")})
    assert r.returncode == 1 and "还没有会话缓冲文件" in r.stderr


def test_context_survives_broken_and_half_lines(cli, tmp_path):
    buf_dir = tmp_path / "buf2"
    buf_dir.mkdir()
    with open(buf_dir / "audit.jsonl", "w", encoding="utf-8") as f:
        f.write("\n{坏行\n")
        f.write(_msg_line("-100:1", "好行") + "\n")
        f.write('{"op":"msg","mid":"-100:2"')     # 半行
    r = cli("context", env={"CC_TG_BUFFER_DIR": str(buf_dir), "CC_LARK_CHAT_ID": "-100"})
    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 0 and "好行" in r.stdout


def test_context_survives_valid_json_non_object_lines(cli, tmp_path):
    buf_dir = tmp_path / "buf3"
    buf_dir.mkdir()
    with open(buf_dir / "audit.jsonl", "w", encoding="utf-8") as f:
        f.write(_msg_line("-100:1", "好行") + "\n")
        f.write("[]\n")
    r = cli("context", env={"CC_TG_BUFFER_DIR": str(buf_dir), "CC_LARK_CHAT_ID": "-100"})
    assert "Traceback" not in r.stderr, r.stderr
    assert r.returncode == 0 and "好行" in r.stdout


def test_buffer_dir_is_isolated_to_tmp(tmp_path):
    """自证：本文件所有用例都在 tmp 里跑，没往 ~/.feishu-claude/tg 写真数据。"""
    assert tg_context.buffer_dir() == str(tmp_path / "tg")   # conftest 的 autouse 隔离
    assert tg_context.buffer_dir() != os.path.expanduser("~/.feishu-claude/tg")


# ══════════════════════════════════════════════════════════════
# 5. .env 里带负号的 key（每个群一个工作目录）
# ══════════════════════════════════════════════════════════════

def test_negative_chat_id_env_key_is_parsed_and_scanned(monkeypatch, tmp_path):
    """`TG_CHAT_CWD_-1001234567890=...`：dotenv 要读得进 os.environ，
    _load_profile 要扫得到（"每个群一个工作目录"全靠它）。"""
    from dotenv import dotenv_values, load_dotenv

    import bot_config

    env_file = tmp_path / ".env"
    env_file.write_text(
        "AUDITTG_PLATFORM=telegram\n"
        "AUDITTG_BOT_TOKEN=111:AAfake\n"
        "AUDITTG_CHAT_CWD_-1001234567890=/tmp/spx\n"
        "AUDITTG_CHAT_CWD_777=~/plain\n",
        encoding="utf-8",
    )
    parsed = dotenv_values(stream=io.StringIO(env_file.read_text(encoding="utf-8")))
    assert parsed["AUDITTG_CHAT_CWD_-1001234567890"] == "/tmp/spx"   # 负号 key 不被吞

    for k in list(os.environ):
        if k.startswith("AUDITTG_"):
            monkeypatch.delenv(k, raising=False)
    load_dotenv(env_file, override=True)
    try:
        assert os.environ["AUDITTG_CHAT_CWD_-1001234567890"] == "/tmp/spx"
        profile = bot_config._load_profile("audittg")
        assert profile.is_telegram and profile.app_id == "111"
        assert profile.chat_default_cwd["-1001234567890"] == "/tmp/spx"
        assert profile.chat_default_cwd["777"] == os.path.expanduser("~/plain")
    finally:
        for k in list(os.environ):
            if k.startswith("AUDITTG_"):
                del os.environ[k]
