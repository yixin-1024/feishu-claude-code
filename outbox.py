"""
发送失败兜底 outbox。

Claude 把结果算出来了，但 Lark 发送彻底失败（额度耗尽 99991403 / 卡片渲染
230099 故障 / 网络抖动重试用尽）时，结果不能就这么丢——落到本地
`logs/outbox-<label>.md`，额度恢复或排障时还能捞回来。

设计原则：
- record() 永不抛异常（兜底逻辑本身不能再制造故障）。
- 一条结果一个 markdown 区块，带时间戳 / 类型 / 错误原因，方便人肉翻。
- 仅由调用方在"确认发不出去"时主动调，不参与流式中间态，避免刷屏。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Optional

# logs 目录相对本文件（仓库根），与 dispatcher / launchd 日志同处一地
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

_lock = threading.Lock()


def _safe_label(label: str) -> str:
    """label 用作文件名片段，清掉路径分隔符等不安全字符。"""
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in (label or "")).strip("_")
    return cleaned or "bot"


def outbox_path(label: str) -> str:
    """返回某个 bot 的 outbox 文件路径（不保证文件已存在）。"""
    return os.path.join(_LOG_DIR, f"outbox-{_safe_label(label)}.md")


def record(
    label: str,
    content: str,
    *,
    kind: str = "result",
    error: str = "",
    meta: Optional[dict] = None,
) -> Optional[str]:
    """
    把一条发不出去的内容追加到 outbox 文件。返回写入的文件路径；失败返回 None。

    Args:
        label: bot/profile 标识（决定文件名）
        content: 要保留的正文（通常是 Claude 最终回复）
        kind:    条目类型（result / error / placeholder ...）
        error:   触发落盘的发送错误描述
        meta:    附加键值（chat_id / user / message_id 等），逐行写进 header
    """
    if not content:
        return None
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        path = outbox_path(label)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = [f"## [{ts}] {kind}"]
        if error:
            header.append(f"- ⚠️ 发送失败: {error}")
        for k, v in (meta or {}).items():
            if v:
                header.append(f"- {k}: {v}")
        block = "\n".join(header) + "\n\n" + content.rstrip() + "\n\n---\n\n"
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(block)
        return path
    except Exception as e:  # 兜底逻辑绝不能再抛
        print(f"[outbox] 落盘失败 label={label}: {e}", flush=True)
        return None
