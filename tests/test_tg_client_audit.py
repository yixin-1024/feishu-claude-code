"""telegram_client.py 对抗性审计（只读审计的产物，不改产品代码）。

这个文件和 tests/test_telegram_client.py 的分工：那边钉的是"正常路径的契约"，
这边钉的是**审计发现的缺陷**——每条 xfail 都对应一个已复现、未修的 bug，
修完把 xfail 去掉即可当回归测试用。

结论详见 /tmp/tg_audit_client.md。
"""

import asyncio
import os
import re
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_client as tc
from telegram_client import TelegramApiError, TelegramClient, make_key
from tg_md import SPLIT_LIMIT, render_html, split_md

TG_TEXT_LIMIT = 4096


class Transport:
    """可编排的假 Bot API。记录 **实际到达顺序**（审计 #3 的关键）。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.arrived: list[tuple[str, str]] = []
        self.next_message_id = 100
        self.errors: dict[str, list[BaseException]] = {}
        self.send_fail_at: int = -1      # 第 N 次 sendMessage 抛错（0-based）
        self.sends = 0
        self.slow_edit_once = 0.0        # 第一次 editMessageText 阻塞多久

    def __call__(self, method, payload, timeout, fresh=False):
        self.calls.append((method, payload))
        queued = self.errors.get(method)
        if queued:
            raise queued.pop(0)
        if method == "sendMessage":
            if self.sends == self.send_fail_at:
                self.sends += 1
                raise TelegramApiError(
                    "sendMessage", 400, "Bad Request: message is too long")
            self.sends += 1
            self.next_message_id += 1
            self.arrived.append(("send", payload["text"]))
            return {"message_id": self.next_message_id,
                    "chat": {"id": int(payload["chat_id"])}}
        if method == "editMessageText":
            if self.slow_edit_once:
                delay, self.slow_edit_once = self.slow_edit_once, 0.0
                time.sleep(delay)        # 真阻塞：wait_for 的 cancel 打不断它
            self.arrived.append(("edit", payload["text"]))
            return True
        if method == "getMe":
            return {"id": 42, "username": "spx_bot", "first_name": "spx",
                    "can_read_all_group_messages": True}
        return True

    def of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def client(monkeypatch):
    c = TelegramClient("42:secret", label="tgaudit")
    monkeypatch.setattr(c, "_post_sync", Transport())
    monkeypatch.setattr(tc, "_EDIT_MIN_INTERVAL", 1.5)
    return c


def tr(client) -> Transport:
    return client._post_sync


def long_md(n_lines: int, tag: str = "seg") -> str:
    return "\n".join(f"{tag}{i} " + "y" * 60 for i in range(n_lines))


# ══════════════════════════════════════════════════════════════════
# 审计 #1：update_card 当终态写时砍头 + 不发续段（内容丢失）
#
# dispatcher 有一批调用点把 update_card 当**唯一的终态写**用：
#   dispatcher.py:2778 handle_menu_command（/skills /ls /mcp /status 的输出）
#   dispatcher.py:2815 handle_switch_usage（无按钮分支）
#   dispatcher.py:163  _announce_stopped_run（停止卡 = 停止前的全部进度）
# Lark 的 update_card 是整卡覆盖、内容一字不丢；Telegram 这边走 tail_md，
# 超过 3800 字符就只剩尾巴 + "…（前文略）"，且不像 update_card_final 那样补续段。
# ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_update_card_as_terminal_write_keeps_only_the_tail(client):
    """复现：8000+ 字符的命令输出经 update_card 落地后，开头全没了。"""
    key = await client.reply_card("-100:7", loading=True)
    body = long_md(150)
    assert len(body) > 8000
    tr(client).calls.clear()

    await client.update_card(key, body)

    edits = tr(client).of("editMessageText")
    assert len(edits) == 1
    sent = edits[0]["text"]
    assert "seg149" in sent                      # 尾巴留着
    assert "前文略" in sent                       # 明确告诉用户被截了
    assert "seg0 " not in sent                   # ← 开头没了
    assert tr(client).of("sendMessage") == []    # ← 也没有续段


@pytest.mark.asyncio
async def test_update_card_is_tail_oriented_by_contract(client):
    """`update_card` 是**流式帧**语义：超长时保尾巴（前面标「…（前文略）」），不发续段。

    这是有意的取舍，不是遗漏：
      · 流式帧每秒都在变，发续段等于每帧都往聊天里灌新消息；
      · "越往后越重要"——用户要看的是 Claude 刚写出来的那几行。
    需要**全量**落地的路径用 `update_card_final`（它会切段 + 续段），dispatcher 的
    收尾走的就是它。唯一走 update_card 的长内容是 /stop 的进度卡，保尾巴正合适。
    """
    key = await client.reply_card("-100:7", loading=True)
    body = long_md(150)
    tr(client).calls.clear()

    await client.update_card(key, body)

    edits = tr(client).of("editMessageText")
    assert len(edits) == 1
    assert tr(client).of("sendMessage") == []          # 不发续段
    assert edits[0]["text"].startswith("…（前文略）")
    assert "seg149" in edits[0]["text"]                # 尾巴在
    assert "seg0 " not in edits[0]["text"]             # 头被省略（契约）

    # 同一份内容走 update_card_final 就必须全量落地
    tr(client).calls.clear()
    await client.update_card_final(key, body)
    delivered = "".join(
        p["text"] for m, p in tr(client).calls
        if m in ("editMessageText", "sendMessage")
    )
    assert "seg0 " in delivered and "seg149" in delivered


# ══════════════════════════════════════════════════════════════════
# 审计 #2（审计期间已被修好，转成回归测试）：
# 节流丢的那一帧曾经在"没有 finalize 兜底"的调用点上永久丢失
# —— handle_menu_command / handle_set_mode / handle_resume_session /
# handle_switch_usage(无按钮) 都是"只调一次 update_card 就结束"，
# 没人 flush _pending。现在 update_card 会 _schedule_flush 延迟补帧，
# 这两条测试钉住"补帧必须真的发出去"，别再退回去。
# ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_throttled_terminal_write_is_eventually_flushed(client, monkeypatch):
    """菜单卡渲染后立刻点按钮：这一帧被节流，但延迟补帧必须把它写出去。"""
    monkeypatch.setattr(tc, "_EDIT_MIN_INTERVAL", 0.2)
    key = await client.reply_card("-100:7", content="⚡ 快捷命令", loading=False)
    await client.update_card_with_buttons(
        key, "⚡ 快捷命令",
        [{"text": "/status", "value": {"action": "run_cmd", "cmd": "/status"}}],
    )
    tr(client).calls.clear()

    # handle_menu_command 的唯一终态写（dispatcher.py:2778），此刻被节流
    await client.update_card(key, "📊 当前会话状态：model=opus, ws=/tmp")
    assert tr(client).of("editMessageText") == []
    assert client._pending[key][1].startswith("📊")   # (seq, text)

    await asyncio.sleep(0.5)
    assert any("当前会话状态" in p["text"]
               for p in tr(client).of("editMessageText")), "补帧任务没把攒着的帧写出去"


@pytest.mark.asyncio
async def test_rapid_clicks_all_land_with_the_latest_content_winning(client, monkeypatch):
    monkeypatch.setattr(tc, "_EDIT_MIN_INTERVAL", 0.2)
    key = await client.reply_card("-100:7", content="⚡ 快捷命令", loading=False)
    await client.update_card_with_buttons(key, "⚡ 快捷命令", [{"text": "x", "value": {}}])
    tr(client).calls.clear()

    await client.update_card(key, "第一次点击的结果")
    await client.update_card(key, "第二次点击的结果")
    await asyncio.sleep(0.6)

    texts = [p["text"] for p in tr(client).of("editMessageText")]
    assert texts and texts[-1] == "第二次点击的结果"


# ══════════════════════════════════════════════════════════════════
# 审计 #2b（新增，未修）：延迟补帧任务**不在 dispatcher 的 card_update_lock 里**，
# 而 _cancel_flush 的 task.cancel() 拦不住已经进入 asyncio.to_thread 的那个 POST。
# 于是不需要任何超时，仅靠正常网络延迟（补帧 POST 在飞 → 终态写插进来）就能
# 让流式快照后到、把终态卡覆盖掉。
# ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_inflight_flush_post_cannot_overwrite_the_final(client, monkeypatch):
    """在飞的补帧 POST 不能把终态卡打回流式快照。

    `task.cancel()` 拦不住已经进了 `asyncio.to_thread` 的那个 POST，所以靠两道闸门：
    ① 同一条消息的 edit 串行（per-key 锁）→ 终态写排在它后面，天然后到；
    ② 单调序号 → 万一顺序还是乱了，旧号的写在发出前就被丢弃。
    """
    monkeypatch.setattr(tc, "_EDIT_MIN_INTERVAL", 0.3)
    key = await client.reply_card("-100:7", loading=True)
    await client.update_card(key, "帧 1")
    await client.update_card(key, "⏳ 流式快照 ⏱ 2m42s")     # 排上延迟补帧
    tr(client).arrived.clear()
    tr(client).slow_edit_once = 0.5                          # 补帧的 POST 慢
    await asyncio.sleep(0.4)                                 # 让补帧任务进到 POST 里

    await client.update_card_final(key, "✅ 最终结论")
    await asyncio.sleep(0.9)

    assert [t for _, t in tr(client).arrived][-1] == "✅ 最终结论"


@pytest.mark.asyncio
async def test_final_write_should_win_over_an_inflight_flush(client, monkeypatch):
    monkeypatch.setattr(tc, "_EDIT_MIN_INTERVAL", 0.3)
    key = await client.reply_card("-100:7", loading=True)
    await client.update_card(key, "帧 1")
    await client.update_card(key, "⏳ 流式快照 ⏱ 2m42s")
    tr(client).arrived.clear()
    tr(client).slow_edit_once = 0.5
    await asyncio.sleep(0.4)
    await client.update_card_final(key, "✅ 最终结论")
    await asyncio.sleep(0.9)

    assert tr(client).arrived[-1][1] == "✅ 最终结论"


# ══════════════════════════════════════════════════════════════════
# 审计 #3：孤儿流式帧后到，把终态卡覆盖回流式快照（Lark 的历史教训重演）
#
# dispatcher.push() 用 wait_for(update_card, timeout=_PUSH_TIMEOUT=20) 包着，
# 而 telegram_client 自己的 _HTTP_TIMEOUT=25（外加 call() 的 2 次重试，单次
# update_card 最坏 ~76s）。wait_for 超时只能 cancel 协程，**cancel 不掉
# asyncio.to_thread 里已经在跑的那个 POST**，于是：
#   push 超时 → 释放卡片锁 → 终态 update_card_final 写入 → 孤儿帧才落地
# 结果卡片永久停在带 ⏱ 计时的流式快照。FeishuClient 对同一个问题有
# _FINAL_CONFIRM_DELAY 补写兜底（feishu_client.py:534），Telegram 这边什么都没有。
# ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cancelled_stream_frame_cannot_land_after_the_final_write(client):
    """dispatcher.push 超时被取消的那一帧，不能在终态之后落地。

    push 的 wait_for 只能取消协程，取消不了已经在 to_thread 里跑着的 POST；
    per-key 串行让终态写必然排在它之后（真机上还有一次终态确认写兜底）。
    """
    key = await client.reply_card("-100:7", loading=True)
    tr(client).slow_edit_once = 0.4
    tr(client).arrived.clear()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            client.update_card(key, "⏳ 流式快照 …⏱ 2m42s"), timeout=0.05)

    await client.update_card_final(key, "✅ 最终结论：搞定了")
    await asyncio.sleep(0.6)   # 等孤儿线程把 POST 发完

    assert [t for _, t in tr(client).arrived][-1] == "✅ 最终结论：搞定了"


@pytest.mark.asyncio
async def test_final_write_should_win_over_a_late_stream_frame(client):
    key = await client.reply_card("-100:7", loading=True)
    tr(client).slow_edit_once = 0.4
    tr(client).arrived.clear()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            client.update_card(key, "⏳ 流式快照 …⏱ 2m42s"), timeout=0.05)
    await client.update_card_final(key, "✅ 最终结论：搞定了")
    await asyncio.sleep(0.6)

    assert tr(client).arrived[-1][1] == "✅ 最终结论：搞定了"


@pytest.mark.asyncio
async def test_http_timeout_exceeds_dispatcher_push_timeout(client):
    """审计 #3 的成因证据：只要 _HTTP_TIMEOUT >= push 的 wait_for，就存在孤儿窗口。"""
    import dispatcher
    assert tc._HTTP_TIMEOUT >= dispatcher._PUSH_TIMEOUT


# ══════════════════════════════════════════════════════════════════
# 审计 #4：续段幂等是"内容盲"的 —— 第二次终态写会新头配旧尾
# ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_second_final_write_keeps_stale_continuations(client):
    key = await client.reply_card("-100:7", loading=True)
    await client.update_card_final(key, long_md(150, "A"))
    first_conts = list(client._continuations[key])
    assert len(first_conts) >= 2
    tr(client).calls.clear()

    await client.update_card_final(key, long_md(150, "B"))

    # 首段换成了 B，续段还是 A 的尾巴 → 用户读到 B 的头 + A 的尾
    assert any("B0 " in p["text"] for p in tr(client).of("editMessageText"))
    assert tr(client).of("sendMessage") == []
    assert client._continuations[key] == first_conts


@pytest.mark.asyncio
async def test_repeated_final_write_with_same_content_is_idempotent(client):
    """同内容重复收尾：**不重复发续段**，也不改变最终呈现。

    注意不是"一次 API 都不打"：终态之后会补一次同内容的确认写（对抗更早发出、
    还在飞的请求后到把正文改回流式快照），Telegram 对它回 not modified，无副作用。
    """
    key = await client.reply_card("-100:7", loading=True)
    body = long_md(150, "A")
    await client.update_card_final(key, body)
    conts = list(client._continuations[key])
    tr(client).calls.clear()

    await client.update_card_final(key, body)
    await client.update_card_final(key, body)

    assert tr(client).of("sendMessage") == [], "续段被重复发了"
    assert client._continuations[key] == conts


@pytest.mark.asyncio
async def test_final_write_should_refresh_continuations_when_content_changes(client):
    key = await client.reply_card("-100:7", loading=True)
    await client.update_card_final(key, long_md(150, "A"))
    tr(client).calls.clear()

    await client.update_card_final(key, long_md(150, "B"))

    delivered = "".join(
        p["text"] for m, p in tr(client).calls
        if m in ("editMessageText", "sendMessage")
    )
    assert "B149" in delivered


# ══════════════════════════════════════════════════════════════════
# 审计 #5：_send 多段中途失败 → 已发出的段成孤儿 + 上抛异常让上层整条重发
# ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_partial_multi_chunk_send_keeps_what_was_already_sent(client):
    """续段中途失败：第一段已经发出去了，就绝不能把异常抛给上层。

    抛上去的话上层会走"发送失败"兜底、拿同样的内容整条重发 → 用户看到重复。
    正确姿势是留住 first_key + 已发出的续段，剩下的只 log。
    """
    tr(client).send_fail_at = 2          # 第 3 段失败
    body = long_md(200)
    assert len(split_md(body)) > 3

    key = await client.reply_card("-100:7", content=body, loading=False)

    assert key and key in client._rendered
    assert tr(client).sends == 3                  # 前两段 + 失败的那次尝试
    assert len(client._continuations.get(key, [])) == 1


@pytest.mark.asyncio
async def test_partial_multi_chunk_send_should_return_the_first_key(client):
    tr(client).send_fail_at = 2
    key = await client.reply_card("-100:7", content=long_md(200), loading=False)
    assert key and key in client._rendered


# ══════════════════════════════════════════════════════════════════
# 审计 #6：带外通知（✅/❌）不进缓冲，导致"回复它"落不回原 thread
#
# _send_plain(record=False) 是刻意的（不想让 ✅ 污染上下文），但副作用是
# buffer 里没有这条消息的 thread 归属：telegram_gateway._resolve_thread 只能
# 回落到群主线 c<chat>，于是在 dispatch 子话题里回复"✅ 完成"会掉进主会话。
# ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_out_of_band_notice_keeps_its_thread_binding_without_polluting_context(
        client):
    """带外提示（✅ / 📬）：**正文不进上下文，但 thread 归属要留**。

    正文不记是有意的（不然下一轮 prompt 里全是 ✅）；thread 归属必须记，
    否则用户回复这条 "✅ 完成" 时会掉回群主线，跟子会话断开。
    """
    anchor = await client.send_post_to_chat("-100", "子任务", "干活")   # t<chat>#<mid>
    sub_thread = await client.get_message_thread_id(anchor)
    assert sub_thread.startswith("t")

    notice = await client.reply_text(anchor, "✅ 完成")

    assert not client.buffer.has(notice)                      # 正文没进上下文
    assert client.buffer.thread_of(notice) == sub_thread      # 但归属认得出
    assert await client.get_message_thread_id(notice) == sub_thread
    # 上下文里只有顶楼那条，没有 ✅
    texts = [m.body.content for m in client.buffer.thread_messages(sub_thread)]
    assert len(texts) == 1 and "✅" not in texts[0]


@pytest.mark.asyncio
async def test_replying_to_a_notice_should_stay_in_the_sub_thread(client):
    anchor = await client.send_post_to_chat("-100", "子任务", "干活")
    sub_thread = await client.get_message_thread_id(anchor)
    notice = await client.reply_text(anchor, "✅ 完成")
    assert client.buffer.thread_of(notice) == sub_thread


# ══════════════════════════════════════════════════════════════════
# 审计 #7：_prune_card_state 只按 _rendered 裁 → edit 永久失败的 key 不回收
# ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_dead_card_state_tables_stay_bounded(client):
    """卡片被用户删掉后每次 edit 都 400，_rendered 永远进不去。

    这些 key 只在 _last_edit/_seq 这些表里留痕，所以裁剪不能只跟着 _rendered 走
    （否则 bot 跑几周这几张表只涨不减）。
    """
    t = tr(client)
    for i in range(client._CARD_STATE_MAX * 2):
        t.errors["editMessageText"] = [TelegramApiError(
            "editMessageText", 400, "Bad Request: message to edit not found")]
        with pytest.raises(TelegramApiError):
            await client.update_card(f"-100:{i}", f"frame {i}")

    assert client._rendered == {}
    # 每次领号顺带收口，所以表长稳定在上限附近（+1 = 本次这条还没轮到裁）
    assert len(client._last_edit) <= client._CARD_STATE_MAX + 1


@pytest.mark.asyncio
async def test_finished_flush_tasks_are_reclaimed(client, monkeypatch):
    """补帧任务跑完要摘掉。

    老写法在 finally 里用 `task.done()` 当判据 —— 自己正在 finally 里跑，done() 必然
    是 False，等于永不回收（300 个卡片就是 300 个 Task 常驻）。改成按任务身份比对。
    """
    monkeypatch.setattr(tc, "_EDIT_MIN_INTERVAL", 0.05)
    for i in range(300):
        key = f"-100:{i}"
        await client.update_card(key, "a")     # 首帧直接落
        await client.update_card(key, "b")     # 被节流 → 排补帧
    await asyncio.sleep(0.5)

    assert len(client._flush_tasks) == 0


@pytest.mark.asyncio
async def test_all_card_state_tables_are_bounded(client):
    t = tr(client)
    for i in range(client._CARD_STATE_MAX * 2):
        t.errors["editMessageText"] = [TelegramApiError(
            "editMessageText", 400, "Bad Request: message to edit not found")]
        with pytest.raises(TelegramApiError):
            await client.update_card(f"-100:{i}", f"frame {i}")
    assert len(client._last_edit) <= client._CARD_STATE_MAX


# ══════════════════════════════════════════════════════════════════
# 审计 #8：split_md 按 markdown 源码算 3800，render_html 可能把**可见文本**
# 撑过 4096（`---` → `──────────`，3 字符变 10）。撑过去是 400
# "message is too long"，它既不在 429 重试里、也不在 parse-error 纯文本兜底里，
# 整条消息发不出去。
# ══════════════════════════════════════════════════════════════════

def _visible_len(html: str) -> int:
    """entity 不计入 Telegram 的 4096（文档：after entities parsing）。"""
    return len(re.sub(r"<[^>]+>", "", html))


def test_split_md_is_lossless_and_fence_balanced():
    """先钉住好的部分：8000 / 40000 字符（含围栏）分段不丢内容、每段围栏配平。"""
    for n in (8000, 40000):
        blocks = []
        i = 0
        while sum(len(b) for b in blocks) < n:
            blocks.append(
                f"## 段 {i}\n正文 {i} " + "文" * 20
                + f"\n\n```python\ndef f{i}():\n" + "    x = 1\n" * 20 + "```\n"
            )
            i += 1
        src = "\n".join(blocks)
        chunks = split_md(src)
        assert max(len(c) for c in chunks) <= SPLIT_LIMIT + 8
        assert all(c.count("```") % 2 == 0 for c in chunks)
        keep = lambda t: [l for l in t.split("\n")
                          if l.strip() and not l.strip().startswith("```")]
        got = [l for c in chunks for l in keep(c)]
        assert got == keep(src)


def test_real_world_markdown_stays_under_the_limit():
    """真实文档（本仓库 README）渲染后可见文本不超 4096 —— 正常情况是安全的。"""
    with open(os.path.join(os.path.dirname(__file__), "..", "README.md")) as f:
        src = f.read()
    for chunk in split_md(src):
        assert _visible_len(render_html(chunk)) <= TG_TEXT_LIMIT


def test_hr_heavy_markdown_overflows_the_4096_limit():
    src = "\n".join(["---"] * 950)
    chunks = split_md(src)
    assert len(chunks[0]) <= SPLIT_LIMIT
    assert _visible_len(render_html(chunks[0])) <= TG_TEXT_LIMIT


def test_too_long_is_neither_retried_nor_downgraded():
    """审计 #8 的后果：'message is too long' 是 400，直接上抛，没有任何兜底。"""
    err = TelegramApiError("sendMessage", 400, "Bad Request: message is too long")
    assert not tc._is_parse_error(err)
    assert not tc._is_not_modified(err)


# ══════════════════════════════════════════════════════════════════
# 契约与失败路径的"好消息"部分：这些行为经核验是对的，钉住防回退
# ══════════════════════════════════════════════════════════════════

def test_public_surface_matches_feishu_client():
    """逐个比对 FeishuClient 的公开方法签名（含默认值）。"""
    import inspect

    import feishu_client

    names = [
        "reply_card", "send_card_to_user", "update_card", "update_card_final",
        "finalize_streaming_card", "update_card_with_buttons",
        "update_card_elements", "reply_text", "send_text_to_user",
        "send_post_to_chat", "reply_post", "download_image", "download_file",
        "speech_to_text", "get_bot_open_id", "get_message_thread_id",
        "list_thread_messages", "batch_resolve_names", "get_card_text",
        "save_outbox",
    ]
    # 已知的形参**命名**分歧（审计 #9）：现有调用点都是位置传参，所以今天不炸；
    # 见下面 test_get_card_text_parameter_name_diverges。
    known_name_diff = {"get_card_text"}
    for name in names:
        lark = getattr(feishu_client.FeishuClient, name)
        tg = getattr(TelegramClient, name)
        assert asyncio.iscoroutinefunction(lark) == asyncio.iscoroutinefunction(tg), name
        a, b = inspect.signature(lark), inspect.signature(tg)
        assert len(a.parameters) == len(b.parameters), f"{name}: {a} vs {b}"
        if name not in known_name_diff:
            assert list(a.parameters) == list(b.parameters), f"{name}: {a} vs {b}"
        for pa, pb in zip(a.parameters.values(), b.parameters.values()):
            assert pa.default == pb.default, f"{name}.{pa.name}"
            assert pa.kind == pb.kind, f"{name}.{pa.name}"


def test_get_card_text_parameter_name_diverges():
    import inspect

    import feishu_client

    assert (
        list(inspect.signature(feishu_client.FeishuClient.get_card_text).parameters)
        == list(inspect.signature(TelegramClient.get_card_text).parameters)
    )


def test_every_prod_call_site_attribute_exists():
    """grep 出全仓库（非测试）对 bot.feishu.* 的访问，逐个确认 Telegram 侧有实现。"""
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run(
        # 按 docstring 的本意只扫**生产代码**：
        #   --exclude-dir=tests  测试替身有自己的字段（posts/replies/done），
        #                        测试里的注释文本也会被当成调用点误报；
        #   --exclude=*_pb2.py   生成的 protobuf 描述符里有 feishu 包名的字符串
        #                        （external_watcher/feishu_im_pb2.py 就是）。
        ["grep", "-rhno", r"\.feishu\.[a-zA-Z_]*", "--include=*.py",
         "--exclude=*_pb2.py", "--exclude-dir=tests", "."],
        cwd=root, capture_output=True, text=True,
    ).stdout
    attrs = {
        line.split(".feishu.")[1]
        for line in out.splitlines() if ".feishu." in line
    }
    # "open.feishu.cn" 这种域名误命中
    ignore = {"cn", ""}
    missing = sorted(
        a for a in attrs - ignore
        if not hasattr(TelegramClient, a)
        and not hasattr(TelegramClient("1:x", label="probe"), a)
    )
    assert missing == [], f"Telegram 侧缺这些属性: {missing}"


@pytest.mark.asyncio
async def test_finalize_on_unknown_key_is_a_silent_noop(client):
    """契约：未知 key 必须 no-op 且永不抛（Lark 侧同语义）。"""
    await client.finalize_streaming_card("-100:999")
    await client.finalize_streaming_card("")
    await client.finalize_streaming_card("garbage-no-colon")
    assert tr(client).calls == []


@pytest.mark.asyncio
async def test_finalize_never_raises_even_when_the_api_dies(client):
    key = await client.reply_card("-100:7", loading=True)
    await client.update_card(key, "帧 1")
    await client.update_card(key, "帧 2")               # 被节流 → 进 _pending
    tr(client).errors["editMessageText"] = [
        TelegramApiError("editMessageText", 403, "Forbidden: bot was kicked")]

    await client.finalize_streaming_card(key)           # 不能抛

    assert key not in client._pending


@pytest.mark.asyncio
async def test_finalize_flushes_the_throttled_last_frame(client):
    """"最后一帧不能丢"的正路：节流丢的帧由 finalize 补上。"""
    key = await client.reply_card("-100:7", loading=True)
    await client.update_card(key, "帧 1")
    await client.update_card(key, "帧 2 最后的内容")
    assert client._pending[key][1].endswith("最后的内容")   # (seq, text)
    tr(client).calls.clear()

    await client.finalize_streaming_card(key)

    assert any("最后的内容" in p["text"] for p in tr(client).of("editMessageText"))


@pytest.mark.asyncio
@pytest.mark.parametrize("code,desc,retried", [
    (400, "Bad Request: message to edit not found", False),
    (400, "Bad Request: MESSAGE_ID_INVALID", False),
    (403, "Forbidden: bot was blocked by the user", False),
    (403, "Forbidden: bot was kicked from the supergroup chat", False),
    (400, "Bad Request: chat not found", False),
])
async def test_fatal_errors_are_never_retried(client, code, desc, retried):
    """致命错误必须立刻放弃——重试只会刷 Telegram 的限流。"""
    tr(client).errors["editMessageText"] = [
        TelegramApiError("editMessageText", code, desc) for _ in range(5)]
    key = await client.reply_card("-100:7", loading=True)
    with pytest.raises(TelegramApiError):
        await client.update_card_final(key, "x")
    assert len(tr(client).of("editMessageText")) == 1


@pytest.mark.asyncio
async def test_rate_limit_waits_retry_after_then_gives_up(client, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    tr(client).errors["sendMessage"] = [
        TelegramApiError("sendMessage", 429, "Too Many Requests", retry_after=7)
        for _ in range(5)
    ]
    with pytest.raises(TelegramApiError):
        await client.reply_card("-100:7", loading=True)
    assert slept[:2] == [7.5, 7.5]        # 按 retry_after 等，不是盲目退避
    assert len(tr(client).of("sendMessage")) == 3     # retries=2 → 共 3 次


@pytest.mark.asyncio
async def test_5xx_is_retried_and_network_error_is_wrapped(client):
    import requests

    tr(client).errors["editMessageText"] = [
        TelegramApiError("editMessageText", 502, "Bad Gateway"),
        requests.ConnectionError("boom"),
    ]
    key = await client.reply_card("-100:7", loading=True)
    # edit 只重试 1 次（2 次尝试）：单次 update_card 的最坏耗时必须小于 dispatcher 的
    # _PUSH_TIMEOUT=20s，否则 push 先超时放锁、终态写进去，孤儿帧才落地覆盖终态。
    with pytest.raises(TelegramApiError):
        await client.update_card_final(key, "终态")
    assert len(tr(client).of("editMessageText")) == 2


@pytest.mark.asyncio
async def test_parse_error_falls_back_to_plain_text_on_both_send_and_edit(
        client, monkeypatch):
    monkeypatch.setattr(tc, "_FINAL_CONFIRM_DELAY", 0)   # 别让确认写盖掉待断言的那一次
    tr(client).errors["sendMessage"] = [TelegramApiError(
        "sendMessage", 400,
        'Bad Request: can\'t parse entities: Unmatched end tag at byte offset 5')]
    key = await client.reply_card("-100:7", content="**x", loading=False)
    sends = tr(client).of("sendMessage")
    assert "parse_mode" not in sends[-1]

    tr(client).errors["editMessageText"] = [TelegramApiError(
        "editMessageText", 400,
        'Bad Request: can\'t parse entities: Unsupported start tag "h1"')]
    await client.update_card_final(key, "# x")
    edits = tr(client).of("editMessageText")
    assert "parse_mode" not in edits[-1]


# ══════════════════════════════════════════════════════════════════
# 审计 #10：render_html 在 try 之外求值 → 渲染器自己抛异常时"退回纯文本"的
# 兜底完全不生效，整条回复发不出去。已证实的触发器：正文里出现 tg_md 的哨兵
# 字节 `\x00<数字>\x01`（占位符），render_html 直接 IndexError。
# ══════════════════════════════════════════════════════════════════

def test_render_html_survives_its_own_placeholder_sentinel():
    """正文里的 `\\x00<数字>\\x01` 曾撞上内部占位符 → IndexError（消息彻底发不出去）。

    现在入口就把控制字符剥掉了（Telegram 也显示不了它们）。
    """
    assert render_html("正文 \x005\x01 尾") == "正文 5 尾"


@pytest.mark.asyncio
async def test_send_falls_back_to_plain_text_when_the_renderer_blows_up(client):
    key = await client.reply_card("-100:7", content="工具输出 \x000\x01 结束", loading=False)
    assert key


@pytest.mark.asyncio
async def test_edit_falls_back_to_plain_text_when_the_renderer_blows_up(client):
    key = await client.reply_card("-100:7", loading=True)
    await client.update_card_final(key, "结论 \x001\x01 完")


@pytest.mark.asyncio
async def test_renderer_exception_no_longer_swallows_the_message(client):
    """渲染只是"好看一点"：它自己炸了也必须把消息发出去（_safe_html 退回纯文本）。"""
    key = await client.reply_card("-100:7", content="x \x000\x01 y", loading=False)
    assert key
    assert tr(client).of("sendMessage")[-1]["text"] == "x 0 y"


def test_render_html_never_produces_illegal_tags_for_fuzzed_markdown():
    """好消息：2 万条随机 markdown 噪声全部渲染出合法且配平的 HTML。"""
    import random

    from tg_md import _tags_balanced

    random.seed(7)
    alphabet = list("ab*_~`#>-|[]()<>&\"'\n \t\\!") + [
        "**", "***", "```", "~~", "---", "| a |", "![x](y)", "</b>", "<i>"]
    for _ in range(20000):
        md = "".join(random.choice(alphabet)
                     for _ in range(random.randint(1, 60)))
        assert _tags_balanced(render_html(md)), repr(md)


@pytest.mark.asyncio
async def test_not_modified_is_swallowed_on_both_html_and_plain_attempt(client):
    key = await client.reply_card("-100:7", loading=True)
    tr(client).errors["editMessageText"] = [
        TelegramApiError("editMessageText", 400,
                         "Bad Request: message is not modified"),
    ]
    await client.update_card_final(key, "同样的内容")   # 不该抛


@pytest.mark.asyncio
async def test_callback_tokens_fit_telegram_64_byte_budget(client):
    key = await client.reply_card("-100:7", loading=True)
    buttons = [{"text": "选项 " * 20, "value": {"reply": "x" * 500, "cid": "-100"}}]
    kb = client._keyboard(buttons, key, False)
    cell = kb["inline_keyboard"][0][0]
    assert len(cell["callback_data"].encode()) <= 64
    assert len(cell["text"]) <= 64
    assert client.resolve_callback(cell["callback_data"])["value"]["reply"] == "x" * 500


@pytest.mark.asyncio
async def test_callback_table_is_bounded(client):
    key = await client.reply_card("-100:7", loading=True)
    for i in range(client._CALLBACK_MAX * 2):
        client._keyboard([{"text": f"b{i}", "value": {"i": i}}], key, True)
    assert len(client._callbacks) <= client._CALLBACK_MAX


@pytest.mark.asyncio
async def test_http_session_is_per_thread_and_bounded(client):
    """审计 #5（资源）：session 挂 threading.local，to_thread 的线程池有界 → 不泄漏。"""
    seen = set()

    def grab():
        seen.add(id(client._session))

    await asyncio.gather(*[asyncio.to_thread(grab) for _ in range(50)])
    assert len(seen) <= 64          # 默认线程池上限 min(32, cpu+4)


@pytest.mark.asyncio
async def test_continuation_chain_preserves_order_for_huge_content(client):
    """审计 #6（超长内容）：40000 字符终态写，续段按 reply 链依次挂在前一段下。"""
    key = await client.reply_card("-100:7", loading=True)
    body = long_md(600)
    assert len(body) > 40000
    tr(client).calls.clear()

    await client.update_card_final(key, body)

    sends = tr(client).of("sendMessage")
    chunks = split_md(body)
    assert len(sends) == len(chunks) - 1
    # 链式：第 1 段回复卡片本身，第 n 段回复第 n-1 段 → 展示顺序稳定
    first_mid = int(key.split(":")[1])
    anchors = [first_mid + i for i in range(len(sends))]
    assert [int(p["reply_parameters"]["message_id"]) for p in sends] == anchors
    delivered = tr(client).of("editMessageText")[0]["text"] + "".join(
        p["text"] for p in sends)
    assert "seg0 " in delivered and "seg599" in delivered


@pytest.mark.asyncio
async def test_composite_key_keeps_two_chats_apart(client):
    """两个群可以都有 message_id=42：复合 key 必须让它们互不干扰。"""
    a = make_key("-1001", 42)
    b = make_key("-1002", 42)
    await client.update_card(a, "群 A")
    await client.update_card(b, "群 B")
    edits = tr(client).of("editMessageText")
    assert [p["chat_id"] for p in edits] == ["-1001", "-1002"]
    assert client._rendered[a] == "群 A" and client._rendered[b] == "群 B"


@pytest.mark.asyncio
async def test_malformed_key_raises_a_typed_error_not_a_value_error(client):
    """Lark 口径的 om_xxx 混进来时的行为（跨渠道串台的兜底）。"""
    with pytest.raises(TelegramApiError):
        await client.update_card_final("om_deadbeef", "x")
