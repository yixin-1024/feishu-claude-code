#!/usr/bin/env python3
"""外部群监听器的**真机**联调（不参与 pytest 收集）。

跑真 lark-cli / 真外部群 / 真话题，只把两头换成替身：
  · run_agent → 固定回声（不烧模型、断言确定）
  · reply_markdown → 只记录（Lark 禁止应用以 user 身份往外部群写，见下）

因为往外部群发消息会被 Lark 拦（230027），这里不自己造触发消息，
而是拿话题里**已经存在的真实 @** 当输入：把它们从 seen 里摘掉再轮询一次，
等价于"这条消息刚刚到达"。

用法：
    .venv/bin/python3 tests/live_external_watcher_check.py <触发消息id> [更多id...]
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from external_watcher import dispatcher_bridge as bridge
from external_watcher.watcher import ExternalWebWatcher

CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "external_groups.yaml")

runs: list[dict] = []
posted: list[tuple[str, str, bool]] = []


async def fake_run_agent(**kw):
    runs.append(kw)
    return "[联调回声] 收到，我看一下。", f"sess-{len(runs)}", False


async def main(trigger_ids: list[str]) -> int:
    bridge.run_agent = fake_run_agent

    w = ExternalWebWatcher(CONFIG)
    w.state.path = f"/tmp/ext-live-state-{int(time.time())}.json"
    w.config.download_dir = "/tmp/cc-lark-ext-live"
    w.running = True
    w._sem = asyncio.Semaphore(3)

    async def fake_reply(message_id, text, in_thread=True):
        posted.append((message_id, text, in_thread))
        return [f"om_stub_{len(posted)}"]

    w.api.reply_markdown = fake_reply

    ok = True
    if not await w._resolve_owner():
        print("❌ user 身份不可用")
        return 1

    group = w.config.groups[0]
    print(f"\n=== 群: {group.name} ({group.chat_id}) ===")

    # 1) 真实拉群 + 拍平
    roots = await w.api.list_chat_messages(group.chat_id, page_size=w.config.page_size)
    from external_watcher.watcher import flatten_thread_messages
    flat = flatten_thread_messages(roots)
    print(f"[1] 顶层根消息 {len(roots)} 条 → 拍平后 {len(flat)} 条"
          f"（差值 {len(flat) - len(roots)} 条就是旧实现永远扫不到的话题内回复）")
    if len(flat) <= len(roots):
        print("⚠️ 拍平没多出消息，这个话题里可能确实没有回复")

    # 2) 基线：全部标已读，再把待测的触发消息摘出来，等价于"它们刚到"
    await w._check_group(group)
    for tid in trigger_ids:
        w.state.seen[group.chat_id] = [m for m in w.state.seen[group.chat_id] if m != tid]
        w.state._seen_index[group.chat_id].discard(tid)
    print(f"[2] 基线已建（{len(w.state.seen[group.chat_id])} 条），"
          f"摘出 {len(trigger_ids)} 条待触发")

    # 3) 再轮询一次
    await w._check_group(group)
    print(f"[3] 轮询完成，起了 {len(w._inflight)} 个任务")
    if w._inflight:
        await asyncio.gather(*list(w._inflight), return_exceptions=True)

    # 4) 断言
    print("\n=== 结果 ===")
    if len(runs) != len(trigger_ids):
        print(f"❌ 期望触发 {len(trigger_ids)} 次，实际 {len(runs)} 次")
        ok = False
    else:
        print(f"✅ 触发 {len(runs)} 次，与预期一致")

    for i, kw in enumerate(runs, 1):
        p = kw["message"]
        print(f"\n--- 第 {i} 轮 prompt（{len(p)} 字符, runner={kw['runner']}, "
              f"session={kw['session_id']}）---")
        print(p[:1200])
        if not p.startswith("【本轮 · 消息 id: "):
            print("❌ 缺【本轮】头"); ok = False
        if "【话题" not in p:
            print("⚠️ 本轮没注入话题上下文")

    print(f"\n--- 会话落盘 ---")
    for k, v in w.state.threads.items():
        print(f"  {k} → session={v.session_id} last_seen={v.last_seen[:20]}…")

    print(f"\n--- 本应发到群里的回复（真机被 Lark 拦，这里只记录）---")
    for anchor, text, in_thread in posted:
        print(f"  → reply_in_thread={in_thread} anchor={anchor} : {text[:60]}")
    if len(posted) != len(runs):
        print(f"❌ 回复条数 {len(posted)} != 触发条数 {len(runs)}"); ok = False

    print("\n结论:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1:])))
