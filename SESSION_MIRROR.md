# Claude Code → Lark 会话镜像（session mirror）

把本机 Claude Code 的**终端/IDE 会话**单向镜像到 Lark：你发的 prompt、Claude 的回复、
产出的文件，实时同步成卡片（⏳ 工作中 → ✅ 完成）。一个会话 = 一个 Lark 话题 thread。

## 架构

```
你在终端跑 claude
   └─(UserPromptSubmit hook)→ session_mirror_hook.py   # 门：按 cwd 路由 + 排除 bot，命中就落 marker
                                       │ 写 ~/.claude/session_mirror/active/<sid>.json
                                       ▼
   claude_session_mirror.py (launchd 常驻)              # 引擎：tail jsonl → patch Lark 卡片
                                       │ raw Lark HTTP（复用 .env 的 SPX app 凭证）
                                       ▼
                                 Lark 群/私聊（thread per session）
```

- **实时、不依赖 Stop hook**：一轮是否结束靠"文件静默 `idle_finalize_sec` 秒"判定。长 pause
  后又恢复输出会自动把卡片从 ✅ 翻回 ⏳ 再结算，不丢内容。
- **去重靠事件 uuid**：offset 不必精确，重叠重读不会重复推送。
- **纯 stdlib**，无第三方依赖。

## 文件清单

| 路径 | 作用 |
|---|---|
| `claude_session_mirror.py` (本仓库) | 守护进程（launchd 跑它） |
| `~/.claude/hooks/session_mirror_hook.py` | UserPromptSubmit 钩子（门） |
| `~/.claude/session_mirror.json` | 配置（路由 / 排除 / 凭证来源） |
| `~/.claude/settings.json` → `hooks.UserPromptSubmit` | 注册钩子 |
| `~/Library/LaunchAgents/com.cclark.session-mirror.plist` | launchd keepalive |
| `~/.claude/session_mirror/{active,state}/` | marker / 断点状态 |
| `~/.claude/session_mirror/daemon.log` | 守护进程日志 |

## 配置 `~/.claude/session_mirror.json`

```jsonc
{
  "enabled": true,
  "env_file": ".../feishu-claude-code/.env",   // 从这里读 SPX_APP_ID / SPX_APP_SECRET
  "app_id_env": "SPX_APP_ID",
  "app_secret_env": "SPX_APP_SECRET",

  "default_chat_id": "",                         // 没匹配到 route 时的兜底；空=不兜底（纯白名单）
  "routes": [
    { "prefix": "/abs/path/to/projA", "chat_id": "oc_xxx" },   // 最长前缀匹配
    { "prefix": "/abs/path/to/projB", "chat_id": "oc_yyy" }    // 不同路径 → 不同群
  ],
  "exclude_prefixes": [ ".../feishu-claude-code" ],  // 永远不镜像（即便落在某 route 下）

  "poll_interval_sec": 1.5,
  "idle_finalize_sec": 8,      // 静默多久判定一轮结束
  "max_reply_chars": 2500,     // 卡片里回复的截断长度
  "max_prompt_chars": 320
}
```

**只有 cwd 命中某条 route（或设了 `default_chat_id`）的会话才会被镜像** —— 这就是"哪些路径下
推、推到哪个群"的开关。改完配置守护进程**热生效**（下一拍轮询就读新配置），无需重启。

### 怎么拿一个群的 chat_id
把 bot 拉进群，然后：`lark-cli --profile spx im messages（list）` 或在群里发消息后
`lark-cli --profile spx im +chat-messages-list ...`，或直接用已知的 `oc_...`。

## bot 自我排除

cc-lark 自己 spawn 的 claude 会带 `CC_LARK_MIRROR_OFF=1`（已加在 `claude_runner.py` /
`claude_pty.py` 的 spawn env），钩子见到就跳过——所以 bot 自己的活动不会被镜像。
⚠️ **该改动需 cc-lark 重启后生效**。在你把 route 指到"bot 也会干活的目录"之前，
请先重启 cc-lark（发 `/restart` 或重启 `cc-lark-main` launchd），否则那段时间 bot 的会话
也会进镜像。当前默认 route 只有 demo 目录，bot 不在那干活，无此风险。

## 运维

```bash
# 看日志
tail -n 50 ~/.claude/session_mirror/daemon.log
# 重启守护进程（改了 .py 后）
launchctl kickstart -k gui/$(id -u)/com.cclark.session-mirror
# 停 / 起
launchctl bootout gui/$(id -u)/com.cclark.session-mirror
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cclark.session-mirror.plist
# 临时关镜像：把 config 里 "enabled" 设 false（热生效）
```

测试可直接跑：`python3 claude_session_mirror.py --once --dry-run`（卡片打到 stderr，不真发）。

## 已知边界 / TODO

- **回复截断**：超 `max_reply_chars` 的回复在卡片里截断（标注"完整见本机终端"）。
  TODO：超长转 Lark doc 给链接。
- **首次扫描**：新 session 第一拍从 jsonl 头读（靠 `min_ts` 过滤历史）；resume 一个超大
  jsonl 时这一拍会整文件读一次，一次性开销，之后增量。可优化成按时间戳二分。
## 无缝衔接（resume from Lark）✅ 已实现

在镜像出来的 Lark thread 里回复，cc-lark 会直接 `--resume` 那个本机终端 CLI session
（同 session_id、同 cwd）——终端会话和 Lark thread 变成同一段对话。

实现（最小改动、全 fail-safe）：
1. 守护进程建 thread 根卡片后，拉它的 `thread_id`，落 `~/.claude/session_mirror/threads/<thread_id>.json`
   = `{session_id, cwd, chat_id, preview}`。
2. `session_store.get_current`（每条消息取 Session 的唯一出口）：thread 还没绑 session 时，
   查上面的 link；命中就把本 thread 绑到 `session_id + cwd` 并落盘，之后正常续。任何异常回落原行为。
3. 交接后调 `_release_mirror_session`：删该 session 的 active marker + thread link，让守护进程
   停止跟踪——否则 bot resume 后自己回复，镜像会把同一 session 的新事件重复推一遍。

**前提 / 注意**：
- 改的是 `session_store.py`，**需 cc-lark 重启后生效**。
- 群里**首条**回复仍要 `@ bot`（cc-lark 对"新 thread 首条"要求 @）；绑定+建会话记录后续不用再 @。
- 假设该 profile 的 runner 是 `claude`（镜像只处理 claude 会话，spx 默认就是 claude）。
- resume 的是同一个 jsonl：别在终端那边同时还开着同一 session 跑，避免两个进程并发写。
