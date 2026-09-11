"""外部群监听器测试用的假 lark-cli 客户端。

只实现 LarkUserApi 里 watcher / bridge 真正会调的那几个方法，并把调用记下来，
让测试能断言"发了什么、发到哪"，而不是只断言不抛异常。
"""

from __future__ import annotations

from typing import Optional

from external_watcher.lark_user_api import LarkCliError


class FakeLarkUserApi:
    def __init__(
        self,
        chat_messages: Optional[list] = None,
        thread_messages: Optional[list] = None,
        owner: tuple[str, str] = ("ou_owner", "Lu Yixin"),
    ):
        self.chat_messages = chat_messages or []
        self.thread_messages = thread_messages or []
        self.owner = owner
        # 调用记录
        self.replies: list[tuple[str, str, bool]] = []   # (anchor, text, in_thread)
        self.downloads: list[tuple[str, str, str]] = []  # (message_id, key, kind)
        self.raw_calls: list[list[str]] = []
        # 故障注入
        self.fail_thread_list: Optional[Exception] = None
        self.fail_download: Optional[Exception] = None
        self.fail_reply: Optional[Exception] = None
        self.reply_counter = 0

    async def whoami(self):
        return self.owner

    async def list_chat_messages(self, chat_id, page_size=20, order="desc"):
        return list(self.chat_messages)

    async def list_thread_messages(self, thread_id, page_size=50, order="asc"):
        if self.fail_thread_list:
            raise self.fail_thread_list
        return list(self.thread_messages)

    async def download_resource(self, message_id, file_key, kind, out_path):
        self.downloads.append((message_id, file_key, kind))
        if self.fail_download:
            raise self.fail_download
        return f"{out_path}.jpg" if kind == "image" else f"{out_path}.bin"

    async def reply_markdown(self, message_id, text, in_thread=True):
        if self.fail_reply:
            raise self.fail_reply
        self.replies.append((message_id, text, in_thread))
        self.reply_counter += 1
        return [f"om_sent_{self.reply_counter}"]

    async def _run(self, args, timeout=None):
        self.raw_calls.append(list(args))
        return {}
