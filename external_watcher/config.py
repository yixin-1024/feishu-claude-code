from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml


@dataclass
class GroupConfig:
    chat_id: str
    name: str = ""
    runner: str = "agy"
    model: str = ""
    cwd: str = ""
    # 别人发的消息：必须显式 @ 机主才触发。关掉 = 群里任何人说话都触发（慎用）。
    require_mention: bool = True
    # 机主自己发的消息：只有**显式 @ 自己**才触发。
    # 为什么这里不跟 require_mention 共用一个开关：代发出去的回复也是机主账号发的，
    # 一旦允许"机主发言即触发"就会自己回自己形成死循环。@ 自己是个明确的人为动作，
    # 而 agent 的回复永远不会带结构化 mention，所以这条通道天然不闭环。
    allow_self_mention: bool = True


@dataclass
class ExternalWatcherConfig:
    enabled: bool = False
    lark_domain: str = "larksuite.com"
    user_profile: str = "spx"
    owner_name: str = "Lu Yixin"
    # 留空则启动时用 `lark-cli auth status` 自动解析（推荐）。
    # 按 open_id 判定 @ 比按名字稳：改名、重名、别人昵称里带 @名字 都不会误判。
    owner_open_id: str = ""
    session_dir: str = "~/.feishu-claude/web_session"
    # 图片 / 附件落盘目录。必须在 lark-cli 允许的根目录内（cwd、/tmp、~/files）。
    download_dir: str = "/tmp/cc-lark-ext"
    poll_interval_sec: float = 5.0
    # 每轮每个群拉多少条根消息。话题群里回复挂在根消息下，一起返回。
    page_size: int = 20
    # 同时最多跑几个外部群 agent 会话（每个话题内部再串行）。
    max_concurrent: int = 3
    # Playwright 常驻浏览器。目前只是把页面开着，WS 帧还没解码，对触发没有贡献，
    # 却要常驻一个 headless Chromium；默认关闭，需要继续做协议监听时再打开。
    browser_listener: bool = False
    # 自动回复出错时，用 bot 身份私聊机主通报（外部群里绝不发错误信息）。
    notify_owner_on_error: bool = True
    groups: list[GroupConfig] = field(default_factory=list)

    @property
    def state_path(self) -> str:
        return os.path.join(os.path.expanduser(self.session_dir), "watcher_state.json")

    @classmethod
    def load(cls, path: str) -> "ExternalWatcherConfig":
        p = os.path.expanduser(path)
        if not os.path.exists(p):
            return cls(enabled=False)

        with open(p, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        defaults = cls()
        groups = []
        for g in raw.get("groups") or []:
            g = g or {}
            groups.append(
                GroupConfig(
                    chat_id=str(g.get("chat_id", "")).strip(),
                    name=str(g.get("name", "")).strip(),
                    runner=str(g.get("runner", "agy")).strip(),
                    model=str(g.get("model", "")).strip(),
                    cwd=str(g.get("cwd", "")).strip(),
                    require_mention=bool(g.get("require_mention", True)),
                    allow_self_mention=bool(g.get("allow_self_mention", True)),
                )
            )

        return cls(
            enabled=bool(raw.get("enabled", False)),
            lark_domain=str(raw.get("lark_domain", defaults.lark_domain)).strip(),
            user_profile=str(raw.get("user_profile", defaults.user_profile)).strip(),
            owner_name=str(raw.get("owner_name", defaults.owner_name)).strip(),
            owner_open_id=str(raw.get("owner_open_id", "")).strip(),
            session_dir=str(raw.get("session_dir", defaults.session_dir)).strip(),
            download_dir=str(raw.get("download_dir", defaults.download_dir)).strip(),
            poll_interval_sec=float(raw.get("poll_interval_sec", defaults.poll_interval_sec)),
            page_size=int(raw.get("page_size", defaults.page_size)),
            max_concurrent=int(raw.get("max_concurrent", defaults.max_concurrent)),
            browser_listener=bool(raw.get("browser_listener", defaults.browser_listener)),
            notify_owner_on_error=bool(
                raw.get("notify_owner_on_error", defaults.notify_owner_on_error)
            ),
            groups=[g for g in groups if g.chat_id],
        )
