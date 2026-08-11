import asyncio
from types import SimpleNamespace

import codex_quota_watcher
import runtime


def test_codex_quota_notification_uses_markdown_card(monkeypatch):
    calls = []

    class Feishu:
        async def send_card_to_user(self, open_id, content="", loading=True):
            calls.append((open_id, content, loading))
            return "om_card"

    bot = SimpleNamespace(feishu=Feishu())
    monkeypatch.setattr(runtime, "_bot_loop", object())
    monkeypatch.setattr(runtime, "_bots", {"regtank": bot})

    captured = {}
    monkeypatch.setattr(
        codex_quota_watcher,
        "start_watcher_thread",
        lambda send_fn, interval: captured.update(send_fn=send_fn, interval=interval),
    )

    class Done:
        def result(self, timeout=None):
            return None

    def run_now(coro, loop):
        asyncio.run(coro)
        return Done()

    monkeypatch.setattr(runtime.asyncio, "run_coroutine_threadsafe", run_now)

    runtime.start_codex_quota_watcher("regtank", "ou_owner", interval_sec=123)
    captured["send_fn"]("♻️ **Codex 额度已重置**")

    assert captured["interval"] == 123
    assert calls == [("ou_owner", "♻️ **Codex 额度已重置**", False)]
