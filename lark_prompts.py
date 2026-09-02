"""Lark 系统提示词模板加载与渲染。

为什么不直接用 string.Template：模板里大量 bash 变量（$ANCHOR / $RESP / $1 等），
string.Template 会把 `$ANCHOR` 也识别成 identifier。
本模块只识别 `${name}` 形式（双大括号），bash 的孤立 `$var` 原样保留。

模板放在 prompts/ 目录：
    prompts/default.md         — 兜底模板（兼容当前单一 profile 行为）
    prompts/_dispatch.md       — 派单段落（条件性注入到 default 的 ${dispatch_section}）
    prompts/{role}.md          — Trinity 角色模板（yushitai / zhongshu / ...）

调用入口：render_lark_prompt(profile, request_ctx) → str
"""

from __future__ import annotations

import os
import re
from typing import Optional

from bot_config import Profile

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

# 只识别 ${var} 形式，bash 的 $var / $(cmd) 不动
_VAR_RE = re.compile(r"\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# 简单内存缓存，避免每条消息都读盘（模板不会频繁改）
_template_cache: dict[str, str] = {}


def _load_template(name: str) -> str:
    """读取 prompts/{name}.md。命中缓存就直接返回。"""
    if name in _template_cache:
        return _template_cache[name]
    path = os.path.join(PROMPTS_DIR, f"{name}.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    _template_cache[name] = text
    return text


def clear_cache() -> None:
    """测试或热重载用。生产基本不调。"""
    _template_cache.clear()


def render(template_name: str, ctx: dict) -> str:
    """读取模板，把 ${var} 替换为 ctx[var]。缺变量直接抛 KeyError，不静默。"""
    template = _load_template(template_name)

    def _sub(m: re.Match) -> str:
        var = m.group(1)
        if var not in ctx:
            raise KeyError(
                f"prompt template '{template_name}' 引用了未提供的变量 ${{{var}}}"
            )
        return str(ctx[var])

    return _VAR_RE.sub(_sub, template)


def _build_lark_commands(cli_profile: str, thread_id: str) -> dict:
    """生成 lark-cli 命令片段（reply_cmd_text / image / file，create_doc）。

    抽出来是为了：① 减少模板里硬编码 ② 角色模板需要时可直接复用。

    ⚠️ 回复锚点用环境变量 `$CC_LARK_MESSAGE_ID`，**不要**把本轮消息 id 写死进来。
    这段文本最终进 `--append-system-prompt`，而 Claude API 的 prompt cache 是前缀
    匹配：system 段哪怕只变一个消息 id，续轮首调就得把整段历史（实测 300k~750k
    tokens）重新写一遍缓存。改成 env 后同一话题的 system prompt 逐轮字节一致，
    历史直接命中 cache_read（对照实验：只改一个 id 就从全命中退回全重写）。
    bot 在 spawn 时把 CC_LARK_MESSAGE_ID / CC_LARK_USER_ID 注入子进程 env，Bash
    工具里可直接展开；本轮的具体 id 另外写在用户消息开头的【本轮】行里。
    """
    reply_flag = "--reply-in-thread " if thread_id else ""
    profile_flag = f"--profile {cli_profile} "
    anchor = '"$CC_LARK_MESSAGE_ID"'
    return {
        "reply_cmd_text": (
            f"lark-cli {profile_flag}im +messages-reply --as bot --message-id {anchor} "
            f'{reply_flag}--text "<文本>"'
        ),
        "reply_cmd_image": (
            f"cd <文件所在目录> && lark-cli {profile_flag}im +messages-reply --as bot "
            f"--message-id {anchor} {reply_flag}--image <相对路径>"
        ),
        "reply_cmd_file": (
            f"cd <文件所在目录> && lark-cli {profile_flag}im +messages-reply --as bot "
            f"--message-id {anchor} {reply_flag}--file <相对路径>"
        ),
        # 新版 lark-cli 已下线 v1 的 `--markdown`；正文先落本地 .md 文件再用 @路径 传入，
        # 避免多行内容在 shell 里被转义弄坏（绝对路径 / 相对路径都认）。
        "create_doc": (
            f"lark-cli {profile_flag}docs +create --as user "
            f'--title "<简短标题>" --doc-format markdown --content @<本地 .md 文件路径>'
        ),
    }


def _build_location_block(
    raw_chat_id: str,
    thread_id: str,
    is_group: bool,
    brand: str,
    domain: str,
    cli_profile: str,
) -> str:
    """构造"当前会话信息"那一块条件性文本。

    只放**同一话题内逐轮不变**的信息。本轮消息 id / 提问者这类每轮都变的字段
    不进这里（进了就会让 system prompt 每轮不同、prompt cache 全失效，见
    _build_lark_commands 的说明），改为写在用户消息开头的【本轮】行 + 环境变量。
    """
    lines = [f"- chat_id: {raw_chat_id}"]
    if thread_id:
        lines.append(f"- thread_id: {thread_id}（话题群 / topic thread）")
    lines.append(
        "- 本轮用户消息 id 与提问者 open_id：见用户消息开头的【本轮 · …】行；"
        "在 Bash 里也可直接用环境变量 `$CC_LARK_MESSAGE_ID`（回复锚点）/ "
        "`$CC_LARK_USER_ID`（提问者）"
    )
    lines.append(f"- 场景: {'群聊' if is_group else '私聊'}")
    lines.append(f"- 平台: {brand}（domain: {domain}）")
    lines.append(f"- 对应 lark-cli profile: **{cli_profile}**")
    return "\n".join(lines)


def _fmt_minutes(seconds: float) -> str:
    """秒 → 「N 分钟」/「N 小时」的短文本。"""
    seconds = float(seconds)
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{int(seconds // 3600)} 小时"
    return f"{int(round(seconds / 60))} 分钟"


def _build_timeout_ctx(profile: Profile, runner: str) -> dict:
    """按实际配置渲染超时阈值文案，别在模板里写死数字。

    历史上模板写死「15 分钟无输出 / 60 分钟 wall-clock 强杀」，但本机早已
    CLAUDE_WALL_CLOCK_LIMIT_SEC=0（不限），agent 被一个不存在的上限吓着，会把本可
    一口气做完的活切碎或多排 wake。这里把 claude_pty 的 IDLE/STUCK 常量与
    resolve_claude_wall_clock_limit 的结果渲染进去，模板只引用变量。
    """
    backend = (runner or "claude").strip().lower()
    try:
        from claude_pty import IDLE_TIMEOUT as _idle, STUCK_CHILD_TIMEOUT as _stuck
    except Exception:  # noqa: BLE001 — 常量拿不到就退回历史默认
        _idle, _stuck = 300, 900

    if backend in {"claude", "codex"} or backend not in {"opencode", "mimo", "grok", "maka"}:
        if backend == "codex":
            idle = int(getattr(profile, "codex_idle_timeout_sec", 3600) or 3600)
            rules = [f"连续 {_fmt_minutes(idle)}没有任何新输出 → 强杀"]
            stuck_minutes = int(round(idle / 60))
            wall_rule = "单轮总时长不设上限（只要持续有输出就不会因总时长被杀）"
        else:
            from bot_config import resolve_claude_wall_clock_limit
            wall = resolve_claude_wall_clock_limit(profile_name=profile.name)
            stuck_minutes = int(round(_stuck / 60))
            rules = [
                f"{_fmt_minutes(_idle)}内完全无输出且无子进程 → 强杀",
                f"有子进程但你 {_fmt_minutes(_stuck)}没新输出 → 强杀",
            ]
            if wall > 0:
                wall_rule = f"任何情况下单轮 {_fmt_minutes(wall)} wall-clock → 强杀"
            else:
                wall_rule = "本机未设单轮 wall-clock 上限（只要持续有输出就不会因总时长被杀）"
        rules.append(wall_rule)
    else:
        idle_attr = {
            "opencode": "opencode_idle_timeout_sec",
            "mimo": "mimo_idle_timeout_sec",
            "grok": "grok_idle_timeout_sec",
            "maka": "maka_idle_timeout_sec",
        }[backend]
        idle = int(getattr(profile, idle_attr, 300) or 300)
        stuck_minutes = int(round(idle / 60))
        rules = [f"连续 {_fmt_minutes(idle)}没有任何新输出 → 强杀"]
        wall_rule = "单轮总时长不设上限（只要持续有输出就不会因总时长被杀）"
        rules.append(wall_rule)

    return {
        "timeout_rules": "；".join(rules),
        "stuck_minutes": str(stuck_minutes),
        "wall_clock_rule": wall_rule,
    }


def _build_dispatch_section(
    profile: Profile,
    raw_chat_id: str,
    asker_open_id: str,
    cli_profile: str,
    thread_id: str,
    is_group: bool,
) -> str:
    """派单段落，按条件注入。和原 _build_lark_system_prompt 的判定一致。"""
    if not (
        is_group
        and not thread_id
        and profile.dispatch_chat_id
        and raw_chat_id != profile.dispatch_chat_id
    ):
        return ""

    asker_at_tag = (
        f'{{"tag":"at","user_id":"{asker_open_id}"}}'
        if asker_open_id
        else '{"tag":"text","text":""}'
    )
    return render(
        "_dispatch",
        {
            "raw_chat_id": raw_chat_id,
            "dispatch_chat_id": profile.dispatch_chat_id,
            "asker_open_id_display": asker_open_id or "<未知>",
            "cli_profile": cli_profile,
            "asker_at_tag": asker_at_tag,
        },
    )


def render_lark_prompt(
    profile: Profile,
    raw_chat_id: str,
    thread_id: str,
    user_message_id: str,
    is_group: bool,
    asker_open_id: str = "",
    ticket_state: Optional[str] = None,
    ticket_id: str = "",
    ticket_history: str = "",
    runner: str = "",
) -> str:
    """构造注入到 Claude 的 Lark 系统提示。

    选择模板：
        profile.role 存在  → prompts/{role}.md（trinity 角色模板）
        profile.role 不存在 → prompts/default.md（兼容当前行为）

    runner：本轮会话后端。运行时 MCP 工具（wake_me_in / dispatch_task / ...）只注入
    claude/codex 后端（agent_runner 只给这两个后端透传 wake_context），所以 prompt 里的
    工具介绍段必须跟着分支——否则 opencode/mimo 的 agent 会被教唆去调不存在的工具、
    或开"过会儿回来"的空头支票。
    """
    cli_profile = profile.lark_cli_profile or profile.name
    brand = profile.brand_label

    backend = (runner or "claude").strip().lower()
    timeout_ctx = _build_timeout_ctx(profile, backend)
    runtime_mcp_section = render(
        "_runtime_mcp_claude" if backend in {"claude", "codex"} else "_runtime_mcp_other",
        timeout_ctx,
    )

    # ⚠️ user_message_id 故意不进 system prompt（只在 Trinity 角色模板里保留
    # ${message_id}）：它每轮都变，会把 Claude API 的 prompt cache 前缀打断。
    # 默认路径下本轮 id 走用户消息开头的【本轮】行 + CC_LARK_MESSAGE_ID env。
    base_ctx = {
        "runtime_mcp_section": runtime_mcp_section,
        "brand": brand,
        "cli_profile": cli_profile,
        "location_block": _build_location_block(
            raw_chat_id, thread_id, is_group, brand, profile.domain, cli_profile,
        ),
        "dispatch_section": _build_dispatch_section(
            profile, raw_chat_id, asker_open_id, cli_profile, thread_id, is_group,
        ),
        **_build_lark_commands(cli_profile, thread_id),
        **timeout_ctx,
    }

    role = getattr(profile, "role", None)
    if not role:
        return render("default", base_ctx)

    # Trinity 角色模板：注入角色-特定上下文
    role_ctx = {
        **base_ctx,
        "role": role,
        "ticket_id": ticket_id or "<新 ticket>",
        "state": ticket_state or "<待创建>",
        "ticket_history": ticket_history or "<无>",
        "court_chat_id": profile.dispatch_chat_id or raw_chat_id,
        "boss_chat_id": raw_chat_id,
        "boss_open_id": asker_open_id or "<未知>",
        "yushitai_open_id": getattr(profile, "yushitai_open_id", "") or "<未配置>",
        "zhongshu_open_id": getattr(profile, "zhongshu_open_id", "") or "<未配置>",
        "menxia_open_id": getattr(profile, "menxia_open_id", "") or "<未配置>",
        "shangshu_open_id": getattr(profile, "shangshu_open_id", "") or "<未配置>",
        "ganhuode_open_id": getattr(profile, "ganhuode_open_id", "") or "<未配置>",
        "message_id": user_message_id,
        "redraft_count": "0",  # TODO: 从 ticket history 算
        "rejection_count": "0",  # TODO: 从 ticket history 算
    }
    return render(role, role_ctx)


# ── 自检 ───────────────────────────────────────────────────────────

def _self_test():
    """跑一遍 default 模板渲染，确认没遗漏变量。"""
    from bot_config import Profile

    fake = Profile(
        name="test",
        app_id="cli_test",
        app_secret="secret",
        platform="lark",
        domain="open.larksuite.com",
        default_cwd="/tmp",
    )
    out = render_lark_prompt(
        fake,
        raw_chat_id="oc_xxx",
        thread_id="",
        user_message_id="om_yyy",
        is_group=False,
        asker_open_id="ou_zzz",
    )
    assert "你正在通过 Lark" in out or "你正在通过Lark" in out
    assert "oc_xxx" in out
    assert "om_yyy" not in out, "本轮消息 id 不该进 system prompt（会打断 prompt cache）"
    assert "$CC_LARK_MESSAGE_ID" in out
    assert "lark-cli --profile test" in out
    print(f"default template render OK, {len(out)} chars")

    # 带 dispatch 的渲染
    fake_with_dispatch = Profile(
        name="test",
        app_id="cli_test",
        app_secret="secret",
        platform="lark",
        domain="open.larksuite.com",
        default_cwd="/tmp",
        dispatch_chat_id="oc_dispatch",
    )
    out2 = render_lark_prompt(
        fake_with_dispatch,
        raw_chat_id="oc_other",
        thread_id="",
        user_message_id="om_yyy",
        is_group=True,
        asker_open_id="ou_zzz",
    )
    assert "调度 / 派单" in out2
    assert "oc_dispatch" in out2
    print(f"default+dispatch template render OK, {len(out2)} chars")


if __name__ == "__main__":
    _self_test()
