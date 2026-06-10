"""判断一条消息是不是要透传给 claude 内置 slash 命令（如 /compact）。

抽成独立**零依赖**叶子模块的原因：claude_pty.py 依赖 termios/pty/fcntl（平台
相关，print-only 部署可能根本没装），dispatcher 不能直接 import 它。但 dispatcher
在话题群里会先把"话题上下文"拼到用户消息前面（见 dispatcher 普通消息分支），
一旦拼了前缀，消息就不再以 `/compact` 开头 → claude 把它当普通文本，内置命令
不触发。所以 dispatcher 也需要在注入前识别这类控制命令并跳过注入。两边共享
同一份白名单，避免各写一份导致漂移。
"""

# 白名单：真正想透传给 claude 内置 slash 的命令。其他 `/` 开头的会被
# claude_pty._escape_for_pty 前置一个空格降级成普通文本（用户的 "/pwd" 通常是
# 想让模型执行 pwd，不是触发 claude 内置）。
CLAUDE_BUILTIN_PASSTHROUGH = ("/compact",)


def is_builtin_passthrough(message: str) -> bool:
    """消息是不是命中 CLAUDE_BUILTIN_PASSTHROUGH 的精确命令（带参数也算）。

    必须是消息**开头**的命令才算——`/compact` 被埋在话题上下文中间时返回 False，
    正是这个语义让 dispatcher 能据此决定"开头是控制命令就别拼前缀"。

    `startswith` 直接对 tuple 会误中 `/compactfoo`，所以严格匹配 cmd 本身、或 cmd
    后面紧跟空白（空格 / 换行，即带参数形式）。
    """
    if not message:
        return False
    stripped = message.lstrip()
    for cmd in CLAUDE_BUILTIN_PASSTHROUGH:
        if stripped == cmd or stripped.startswith(cmd + " ") or stripped.startswith(cmd + "\n"):
            return True
    return False
