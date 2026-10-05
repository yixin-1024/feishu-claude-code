"""Lossless terminal-card pagination for long agent transcripts."""

import json


def split_display_pages(text: str, budget: int = 12000) -> list[str]:
    """Bound escaped UTF-8 size, leaving room for card JSON and buttons.

    Prefer line boundaries; never strip whitespace or discard a prefix. The
    escaped budget also covers quotes/backslashes when card JSON is nested in
    the message request's content string.
    """
    pages = []
    while text:
        lo, hi = 1, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            size = len(json.dumps(text[:mid], ensure_ascii=False).encode("utf-8"))
            if size <= budget:
                lo = mid
            else:
                hi = mid - 1
        end = lo
        if end < len(text):
            newline = text.rfind("\n", end // 2, end)
            if newline >= 0:
                end = newline + 1
        pages.append(text[:end])
        text = text[end:]
    return pages or [""]


async def publish_full_display(client, content, *, card_id, reply_to, user_id,
                               stopped, buttons=None):
    """Update the original card then append numbered cards in the same thread.

    Caller holds the run's card lock and has stopped its heartbeat. A failed
    continuation is preserved in outbox; no completion acknowledgement follows.
    """
    pages = split_display_pages(content)
    for index, page in enumerate(pages):
        if stopped():
            return False
        label = f"**（完整记录 {index + 1}/{len(pages)}）**\n\n" if len(pages) > 1 else ""
        body = label + page
        try:
            if index == 0:
                try:
                    await client.update_card_final(card_id, body)
                finally:
                    await client.finalize_streaming_card(card_id)
                target = card_id
            elif reply_to:
                target = await client.reply_card(reply_to, content=body, loading=False)
            else:
                target = await client.send_card_to_user(user_id, content=body, loading=False)
            if stopped():
                return False
            if index == len(pages) - 1 and buttons:
                await client.update_card_with_buttons(
                    target, body, buttons, flow=all(len(b["text"]) <= 10 for b in buttons))
        except Exception as exc:
            if stopped():
                return False
            saved = client.save_outbox(
                "".join(pages[index:]), kind="result", error=str(exc),
                meta={"card_msg_id": card_id, "reply_to": reply_to, "user": user_id})
            notice = "⚠️ 完整记录发送未完成。" + (
                "未送达内容已保存在本机待补发。" if saved else "记录保存也失败，请重试。")
            try:
                if reply_to:
                    await client.reply_text(reply_to, notice)
                else:
                    await client.send_text_to_user(user_id, notice)
            except Exception:
                pass
            return False
    return True
