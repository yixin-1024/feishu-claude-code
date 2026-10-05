"""跨 agent 派发的家族别名 —— 单一真相源。

以前这张表只写在 http_server.py 里，而 cc_mcp_server.py 把「可选 agent」硬编码在
dispatch_task 的工具说明里。两边漂移的后果是真实的：后端早就支持 agy / maka，
但模型在工具说明里看不到，用户说「派给 agy」只能退回默认 agent。

任何一侧要改，改这里。
"""

# 别名 → runner 名
AGENT_RUNNER_ALIASES: dict[str, str] = {
    "gpt": "codex", "codex": "codex", "openai": "codex", "chatgpt": "codex", "o1": "codex",
    "claude": "claude", "anthropic": "claude",
    "gemini": "opencode", "opencode": "opencode",
    "mimo": "mimo",
    "grok": "grok", "xai": "grok",
    "maka": "maka", "apache-maka": "maka",
    "agy": "agy", "antigravity": "agy",
}

# runner → 给模型看的说明。顺序即工具说明里的呈现顺序。
RUNNER_HINTS: list[tuple[str, str, str]] = [
    ("codex",    '"gpt"/"codex"/"chatgpt"', "GPT（OpenAI Codex CLI）"),
    ("claude",   '"claude"',                "Claude"),
    ("agy",      '"agy"/"antigravity"',     "AGY（Google Antigravity CLI，跑 Gemini）"),
    ("opencode", '"gemini"/"opencode"',     "opencode"),
    ("grok",     '"grok"/"xai"',            "Grok"),
    ("mimo",     '"mimo"',                  "MiMo Code"),
    ("maka",     '"maka"',                  "Apache Maka"),
]


def alias_doc() -> str:
    """生成一行别名说明，嵌进工具 description。"""
    return "; ".join(f"{alias} = {desc}" for _runner, alias, desc in RUNNER_HINTS)
