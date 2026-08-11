# feishu-claude-code

在飞书/Lark 里直接和你本机的 Claude Code 对话。

WebSocket 长连接，流式卡片输出，支持话题群上下文、运行心跳、自主发文件/截图。手机上随时 code review、debug、问问题。

> 复用 Claude Max/Pro 订阅，不需要 API Key，不需要公网 IP。
> 同时支持 **飞书** (`open.feishu.cn`) 和 **Lark 国际版** (`open.larksuite.com`)。

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-blue" alt="Python" />
  <img src="https://img.shields.io/badge/Claude_Code-CLI-blueviolet" alt="Claude Code" />
  <img src="https://img.shields.io/badge/License-MIT-green" alt="MIT" />
</p>

## 特性

**流式输出，实时可见**

- Claude 边想边输出，不是等半天发一坨
- 工具调用进度实时显示 (Bash、Read、Edit、Grep 等)
- **运行心跳**：卡片底部实时滚动 `⏱ 总时长 · 🔧 当前工具耗时 · ⚠️ 无输出 N 秒`，长任务不再让人怀疑进程死没死
- 长回复自动分段，不丢内容

**Bot 主动发文件/截图/文档**

- Claude 知道自己在 Lark 里，用户说"截图发群里"就会自己调 `lark-cli` 直接发到评论区
- 输出过长时自动建 Lark 文档，只把 doc URL 回给聊天，不刷屏卡片
- 动态系统提示注入当前 `chat_id` / `thread_id` / `message_id`，三种场景（私聊/普通群/话题群）无缝切换

**跨设备 Session 管理**

- 手机上开始的对话，回到电脑前接着聊
- CLI 终端里的会话也能在飞书恢复 (`/resume`)
- 后台自动生成会话摘要，方便找回历史对话
- CLI Handover: 终端会话一键移交到飞书继续

**交互式按钮**

- Claude 给出选项时，自动渲染成可点击按钮
- Y/N 确认、编号选项、Plan 模式审批，一键响应
- 输入 `/` 显示命令菜单，按钮分组一目了然

**群聊和话题群支持**

- **精确 @ 识别**：首次启动拉取 bot 自己的 `open_id` 缓存，只有真正 @ 到 bot 才响应
- **话题群上下文**：在话题评论里 @bot，自动拉取 `last_seen` 之后的话题历史作为前缀，不丢上下文
- **忘记 @ 补 @**：第一条忘记 @bot 没关系，下一条补一个 `@bot` 就能把前面那条捡回来处理（话题群生效）
- 每个群/话题独立 session、模型、推理强度、工作目录（同用户跨 chat 互不阻塞）
- `/ws` 为不同群绑定不同项目
- **访问控制**：`ALLOWED_OPEN_IDS` 用户白名单、`ALLOWED_GROUP_CHAT_IDS` 群聊白名单，未授权者静默忽略

**消息队列**

- 新消息不再打断当前任务，而是排队等待（同 chat 串行，跨 chat 并发）
- 当前任务繁忙时新消息收到 `📬 排队中` 回执
- 想真正打断时用 `/stop` 显式终止

**图片 / 文件 / 富文本**

- 直接发截图，Claude 自动下载并分析
- 支持文件 (`file`)、post 富文本（图文混排）、语音、视频下载
- Lark 附件会被解析后转成本地路径喂给 Claude

**派单 / 会话群分流**（可选）

- 把"调度群"和"会话群"分开：在大群被 @ → bot 自动在指定话题群创建新话题、把任务派给独立 session 跑，原群只回一两句"已派单去 XX"
- 适合人多噪声大的项目群：复杂任务（写代码 / 跑 SQL / 多步分析）在干净的话题群里推进，避免刷屏
- 派单前自动 list 群里最近 20 条消息当上下文，承接 session 不丢链路
- 配 `<PROFILE>_DISPATCH_CHAT_ID` 启用，未配则关闭

**定时任务**

- YAML 配 cron → 到点自动在话题群发顶楼消息 + 派单到独立 session（每日报、周报、巡检都能写）
- prompt 支持 `${VAR}` 引用 .env 变量，避免把 chat_id 落到代码库
- 本地 HTTP 端点：`/trigger` 手动触发、`/reload` 热加载 yaml，无需重启 bot

**健壮运行**

- 三段式超时：5 分钟无输出且无子进程 → 杀；15 分钟有子进程但无输出 → 杀；任意情况 60 分钟 wall-clock → 杀。编译/下载不会被误杀，runaway loop 也兜得住。最后一段可调：`CLAUDE_WALL_CLOCK_LIMIT_SEC=7200`（2 小时）/ `=0`（永不因 wall-clock 强杀，长任务用），支持 `<PROFILE>_` 前缀单独配
- 看门狗 6 小时自动重启，防止 WebSocket 假死
- API 调用自动重试 (指数退避)
- `cc-lark` 脚本封装 launchd + ngrok，一键 install/start/stop/restart/status/logs

## 快速开始

### 前置条件

| 依赖 | 最低版本 | 验证命令 |
|------|---------|---------|
| Python | 3.11+ | `python3 --version` |
| Claude Code CLI | 最新 | `claude --version` |
| Claude Max/Pro 订阅 | - | `claude "hi"` 能正常回复 |

### 安装

```bash
git clone https://github.com/joewongjc/feishu-claude-code.git
cd feishu-claude-code

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入飞书应用凭证（见下方「飞书应用配置」）

python3 main.py
```

预期输出：

```
🚀 飞书 Claude Bot 启动中...
   App ID      : cli_xxx...
✅ 连接飞书 WebSocket 长连接（自动重连）...
```

> 从旧版升级的用户可运行 `python3 migrate_sessions.py` 迁移 session 数据（会自动备份）。

## 命令速查

输入 `/` 可弹出按钮菜单，也可以直接输入命令。

### 会话管理

| 命令 | 说明 |
|------|------|
| `/new` | 开始新 session |
| `/new plan` | 新 session 并进入 Plan 模式 |
| `/resume` | 列出历史 session（按钮选择） |
| `/resume 3` | 恢复第 3 个 session |
| `/stop` | 停止当前运行中的任务；请单独发送，同条消息后的文字不会执行 |
| `/status` | 查看当前 session 信息 |

### 模型与模式

| 命令 | 说明 |
|------|------|
| `/model opus` | 切换到 Opus |
| `/model sonnet` | 切换到 Sonnet |
| `/model haiku` | 切换到 Haiku |
| `/effort` | 查看当前推理强度并用按钮选择档位 |
| `/effort high` | 当前对话改用 high（不重开 session） |
| `/effort default` | 清除当前对话覆盖，跟随 profile/CLI 默认 |
| `/mode bypass` | 跳过所有确认（默认） |
| `/mode plan` | 只规划不执行 |
| `/mode default` | 每次工具调用需确认 |
| `/mode accept` | 自动接受文件编辑 |

### 工作目录

| 命令 | 说明 |
|------|------|
| `/cd ~/project` | 切换工作目录 |
| `/ls` | 查看目录内容 |
| `/exec <cmd>` | 在当前 cwd 执行 shell 命令 (30s 超时) |
| `/ws save api ~/projects/api` | 保存命名工作空间 |
| `/ws use api` | 绑定当前会话到工作空间 |
| `/ws list` | 列出所有工作空间 |
| `/ws remove api` | 删除工作空间 |

### 信息查询

| 命令 | 说明 |
|------|------|
| `/usage` | 查看 Claude Max 用量和重置时间 (macOS) |
| `/accounts` | 查看已保存的 Claude Max 账户及自动切换状态 |
| `/switch <账户>` | 在 Claude runner 下切换本机全局 Claude Code 账户 |
| `/skills` | 列出已安装的 Claude Skills |
| `/mcp` | 列出 MCP Servers |
| `/help` | 帮助 |

### Skills 透传

`/commit`、`/review` 等未注册的斜杠命令直接转发给 Claude CLI 执行。你在 Claude Code 里能用的 Skill，飞书里也能用。

## 架构

```
┌──────────┐  WebSocket  ┌────────────────┐  subprocess  ┌────────────┐
│  飞书 App │◄───────────►│ feishu-claude  │─────────────►│ claude CLI │
│  (用户)   │  长连接      │  (main.py)     │ stream-json  │  (本机)     │
└──────────┘             └────────────────┘              └────────────┘
                                 │
                    ┌────────────┼────────────┐
                    │            │            │
              ┌─────▼──┐  ┌────▼─────┐  ┌──▼───────┐
              │commands│  │ session  │  │ feishu   │
              │        │  │ store    │  │ client   │
              └────────┘  └──────────┘  └──────────┘
```

**工作原理:**

1. 飞书通过 WebSocket 推送消息到本机
2. 调用 `claude` CLI 的 `--print --output-format stream-json` 模式
3. 解析 stream-json 事件流，提取文本增量和工具调用
4. 通过飞书卡片 PATCH API 实时更新消息内容
5. 每个聊天（私聊/群聊）维护独立的消息队列锁，保证并发安全

## 飞书应用配置

### 1. 创建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app)，点击「创建企业自建应用」
2. 填写应用名称（如 `Claude Code`），选择图标，点击创建

### 2. 添加机器人能力

1. 进入应用详情，左侧菜单选择「添加应用能力」
2. 添加「机器人」能力

### 3. 开启权限

进入「权限管理」页面，搜索并开启以下权限：

| 权限 scope | 说明 |
|-----------|------|
| `im:message` | 获取与发送单聊、群组消息 |
| `im:message:send_as_bot` | 以应用的身份发送消息 |
| `im:resource` | 获取消息中的资源文件（图片等） |

### 4. 启用长连接模式

1. 左侧菜单「事件与回调」→「事件配置」
2. 订阅方式选择「使用长连接接收事件」（不是 Webhook）
3. 添加事件：`im.message.receive_v1`（接收消息）

### 5. 开启卡片回调 (可选)

按钮交互（选项点击、命令菜单）需要订阅 `card.action.trigger`。推荐在「事件与回调」
的回调配置里选择「使用长连接接收回调」；SDK 会完成来源鉴权，且不需要公网入口。

如必须使用 Webhook：

1. 使用 ngrok 暴露本机 `CALLBACK_PORT`（默认 9981）
2. 回调地址填 `ngrok URL + /callback`
3. 把开放平台「加密策略」里的 Verification Token 写入
   `<PROFILE>_VERIFICATION_TOKEN`

> ngrok 只能指向 `CALLBACK_PORT`。不要暴露 `CONTROL_PORT`；后者仅供本机 MCP/运维调用。
> 所有按钮还会用对应 profile 的 App Secret 做 HMAC，绑定点击人、原消息和 30 天有效期；
> 服务升级后旧的未签名按钮会提示过期，重新打开菜单即可。

> 不配置卡片回调时，所有功能仍可用，只是按钮点击不生效，需要手动输入命令。

### 6. 获取凭证

1. 进入「凭证与基础信息」页面
2. 复制 App ID 和 App Secret，填入 `.env` 文件

### 7. 发布应用

1. 点击「版本管理与发布」→「创建版本」
2. 填写版本号和更新说明，提交审核
3. 管理员在飞书管理后台审核通过后即可使用

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|:---:|-------|------|
| `FEISHU_APP_ID` | 是 | - | 飞书/Lark 应用 App ID |
| `FEISHU_APP_SECRET` | 是 | - | 飞书/Lark 应用 App Secret |
| `LARK_DOMAIN` | 否 | `https://open.feishu.cn` | Lark 国际版填 `https://open.larksuite.com` |
| `DEFAULT_MODEL` | 否 | `opus[1m]` | 默认 Claude 模型；可用 CLI 别名 `opus[1m]`/`sonnet[1m]`/`fable[1m]`/`haiku`（=系列最新）或钉死具体版本 |
| `DEFAULT_CWD` | 否 | `~` | Claude CLI 默认工作目录 |
| `PERMISSION_MODE` | 否 | `bypassPermissions` | 工具权限模式 |
| `ALLOWED_OPEN_IDS` | 推荐 | 空=允许所有 | 用户 open_id 白名单，逗号分隔 |
| `ALLOWED_GROUP_CHAT_IDS` | 推荐 | 空=禁用所有群 | 群聊 chat_id 白名单 (oc_*)，逗号分隔 |
| `DISPATCH_CHAT_ID` | 否 | 空=禁用派单 | 会话群 chat_id；bot 在其它群被 @ 时把任务派到这里的新话题 |
| `CALLBACK_PORT` | 否 | `9981` | 卡片按钮回调 HTTP 端口 |
| `CONTROL_PORT` | 否 | `CALLBACK_PORT + 1` | 本机控制面端口，仅绑定 `127.0.0.1`，禁止转发到公网 |
| `CC_LARK_CONTROL_TOKEN` | 否 | 自动生成 | 控制面 Bearer token；默认保存在 `~/.feishu-claude/control-token`（0600） |
| `<PROFILE>_VERIFICATION_TOKEN` | 推荐 | 空 | Webhook 卡片回调的官方 Verification Token；长连接模式无需配置 |
| `NGROK_DOMAIN` | 否 | 随机 | ngrok 固定域名 (避免每次重启换 URL) |
| `STREAM_CHUNK_SIZE` | 否 | `20` | 流式推送的字符积累阈值 |
| `CLAUDE_CLI_PATH` | 否 | 自动查找 | Claude CLI 可执行文件路径 |

> 查自己的 open_id / chat_id：bot 启动后发条消息，终端日志里会打印 `user=ou_...` / `chat=oc_...`。
>
> 多 profile 模式下，所有变量都加 profile 前缀，例如 `WORK_DISPATCH_CHAT_ID`、`PERSONAL_ALLOWED_OPEN_IDS`。

## Trinity 三省体系（实验性 · 5 角色协作）

把"Boss 一句话 → bot 直接执行"重构成 5 个独立 bot 协作，每个 bot 是独立的 Lark app + 独立人格 + 独立 Claude session（**不共享上下文**），通过状态机驱动逐层调度。

**链路：**

```
下行（5 跳）：Boss → 御史台 → 中书 → 门下 → 尚书 → 干活的
上行（4 跳）：干活的 → 尚书 → 门下 → 御史台 → Boss
驳回：       门下 → 中书 重拟
升级：       御史台 → Boss 补信息
```

**5 个角色职责：**

| 角色 | 职能 | 上游 | 下游 |
|-|-|-|-|
| 御史台 | 入口分诊 + 终审 + 升级 | Boss | 中书（或直接答简单任务） |
| 中书 | 把口语化指令拟成结构化 ticket | 御史台 | 门下 |
| 门下 | 事前审议（封驳）+ 事后影子复审 | 中书 / 尚书 | 尚书 / 御史台 / 中书（驳回） |
| 尚书 | 拆并行子任务 / 派工 / 聚合 | 门下 / 干活的（回奏） | 干活的 / 门下（影子复审） |
| 干活的 | 真正执行（KYC / 开户 / 代码 / ...）。内嵌六部 6 人格切换 | 尚书 | 尚书（回奏） |

**配置：** 见 `.env.example` 的 "Trinity 三省体系" 段落。

**特性：**
- 每个角色独立 Claude session，**ticket 上下文不共享**——Boss 和御史台聊的不会泄露给下层
- bot 间通过话题群 @ 调度，所有 ticket 流程在群消息里可见可追溯
- 状态机 + valid_transitions 白名单，**非法转移直接拒绝、不进 Claude session**（省 token）
- ticket 决策日志持久化到 `~/.feishu-claude/tickets.json`

**何时启用：** 设 `ENABLE_TRINITY=true` 并给 profile 配 `<NAME>_ROLE=yushitai/zhongshu/menxia/shangshu/ganhuode`。**两个条件都满足才生效**——这样允许 .env 里同时保留 trinity 配置和遗留 profile，开关切换。

**默认 OFF**：不设 `ENABLE_TRINITY` 或设为 `false` 时，即使 profile 配了 `ROLE` 也走遗留行为，100% 向后兼容。

## 派单 / 会话群分流（遗留单 bot 模式）

适合人多噪声大的项目群：复杂任务在干净的话题群里跑，原群只留派单回执。

**启用：** 在 `.env` 里给某个 profile 加 `<NAME>_DISPATCH_CHAT_ID=oc_xxx`（指向一个话题群）。

**行为：** 之后凡是在**别的群**（非会话群本身）@ bot，bot 不会直接动手，而是：

1. 读最近 20 条群消息当上下文
2. 在会话群发一条 post 顶楼（@ 提问者拉订阅），起新话题
3. 把任务派给 `/spawn` 起独立 session 处理
4. 在原群只回一两句"已派单去 XX，请去会话群看进度"

**简单问候不走派单**（"你在干嘛"、"链接是啥"），系统提示有判断规则。

**手动派单：** 使用下方“本地控制端点”的 Bearer token 方式调用 `POST /spawn`。

## 内置 cc-lark 运行时 MCP

cc-lark 会在 Claude 与 Codex 会话启动时注入 `cc_mcp_server.py`。它提供
`wake_me_in`、`dispatch_task`、`read_thread`、`schedule_cron`、`list_crons`；stdio
前端只把鉴权请求发到本机 control listener，真正的派工与调度由常驻 bot 兑现。

`dispatch_task` 派出的子会话是**全新话题 + 全新 session**，不继承派发方 thread 的
`/model` `/effort`，默认跑目标 bot 的 profile 默认模型。要按活儿分配算力就显式传
`model` / `effort`（别名同 `/model`：`fable` / `opus` / `sonnet` / `haiku` …），
配合 `agent` 还能混编：一路 Opus 实现、一路 Fable 复核、一路 `agent="gpt"` 交叉验证。

这条路径只在话题群上下文可用；runner 会把当前 `profile / chat_id / thread_id /
anchor_message_id / user_id / control port / token` 通过 `CC_LARK_*` 环境变量注入给
MCP server。设置 `CC_LARK_WAKE_MCP=0` 可关闭自动注入。

## 定时任务

YAML 定义 cron 任务，到点自动在话题群发顶楼并派给 `/spawn`。

```bash
cp scheduled_tasks.yaml.example scheduled_tasks.yaml
# 按需改：cron / chat_id / user_id / topic / prompt 或 prompt_file
mkdir prompts && vim prompts/work_daily_briefing.md
```

`scheduled_tasks.yaml` 字段说明见模板内注释；支持 `${VAR}` 引用 `.env` 变量。

**本地控制端点**（仅 `127.0.0.1:CONTROL_PORT`，全部要求 Bearer token）：

| 端点 | 用途 |
|------|------|
| `GET /trigger` | 列出已注册任务 |
| `GET /trigger?name=xxx` 或 `POST /trigger` | 手动触发任务（绕过 cron） |
| `GET/POST /reload` | 热加载 yaml + prompt 文件，不打断进行中的任务 |
| `POST /spawn` | 派单进独立 session |
| `GET /handover` | CLI session 接管 |

```bash
set -a; source .env; set +a
CALLBACK_PORT=${CALLBACK_PORT:-9981}
CONTROL_PORT=${CONTROL_PORT:-$((CALLBACK_PORT + 1))}
TOKEN_FILE=${CC_LARK_CONTROL_TOKEN_FILE:-$HOME/.feishu-claude/control-token}
CONTROL_TOKEN=${CC_LARK_CONTROL_TOKEN:-$(cat "$TOKEN_FILE")}
AUTH=(-H "Authorization: Bearer $CONTROL_TOKEN")

curl "${AUTH[@]}" "http://127.0.0.1:$CONTROL_PORT/trigger"
curl "${AUTH[@]}" "http://127.0.0.1:$CONTROL_PORT/trigger?name=work_daily_briefing"
curl -X POST "${AUTH[@]}" "http://127.0.0.1:$CONTROL_PORT/reload"
```

公网 callback listener 对 `/spawn`、`/trigger`、`/wake`、`/dispatch`、cron、handover
等路径统一返回 404。即使经过 ngrok 后 socket peer 显示为 localhost，也无法进入控制面。

## 部署

### macOS：推荐用 `cc-lark` 包装脚本

一条命令装好 launchd + ngrok + 开机自启 + 符号链接：

```bash
./deploy/cc-lark install          # 装 bot + ngrok，开机自启
cc-lark status                    # 查状态 + 看最近 10 行日志
cc-lark restart                   # 重启（ngrok 优先，再重启 bot）
cc-lark logs -f                   # 跟踪 bot 日志
cc-lark logs ngrok -f             # 跟踪 ngrok 日志
cc-lark uninstall                 # 卸载
```

脚本只把 `.env` 的 `CALLBACK_PORT` 暴露给 ngrok；Python 进程另行监听本机
`CONTROL_PORT`。`ALLOWED_GROUP_CHAT_IDS=oc_xxx,oc_yyy` 支持多群白名单。

### macOS：手动 launchctl（不走包装）

```bash
cp deploy/feishu-claude.plist ~/Library/LaunchAgents/com.feishu-claude.bot.plist
# 修改 plist 中的路径为实际路径
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.feishu-claude.bot.plist
```

### Linux (systemd)

```bash
sudo cp deploy/feishu-claude.service /etc/systemd/system/
# 修改 service 中的路径和 User

sudo systemctl daemon-reload
sudo systemctl enable --now feishu-claude
journalctl -u feishu-claude -f
```

群里的 `/restart` 会从 `/proc/self/cgroup` 自动识别当前 `.service`。为防止误停服，
只有当 systemd 确认该 unit 处于 `active`、bot 是 `MainPID`、且配置了
`Restart=always` 时才会执行；上面的示例 service 已满足这些条件。

服务会自动重启。看门狗每 6 小时主动重启一次进程，刷新 WebSocket 连接。

## CLI Handover

从终端把当前 Claude Code 会话移交到飞书继续：

```bash
python3 handover.py "对话中的一段独特文本"
```

脚本会在 `~/.claude/projects/` 中搜索匹配的 session，然后通知飞书 Bot 切换过去。适合电脑前调试完，出门用手机继续跟进的场景。

---

## English

**feishu-claude-code** bridges your local Claude Code CLI with Feishu/Lark messenger via WebSocket. Supports both Feishu (`open.feishu.cn`) and Lark international (`open.larksuite.com`).

- **No public IP needed** — WebSocket long connection, runs on your local machine
- **Streaming card output** — Real-time typing, tool-call progress, and a live footer showing elapsed / current-tool / idle time so long-running scripts never look frozen
- **Autonomous file/screenshot/doc sending** — Claude knows it's in Lark and can call `lark-cli` to push images, files, or auto-generate a Lark doc and reply with the URL when output is long
- **Reuses Claude Max/Pro subscription** — No API key required
- **Cross-device sessions** — Continue between phone, desktop, and terminal (`/resume` + CLI handover)
- **Topic/thread groups** — Auto-fetch thread history as context; forgot to `@bot`? Just add one in the next message and it catches up
- **Precise @ detection** — Caches the bot's own open_id; only replies when actually mentioned
- **Access control** — Per-user and per-group allowlists
- **Queued messages** — New messages queue instead of interrupting; explicit `/stop` cancels
- **Interactive buttons** — Options and confirmations rendered as clickable buttons
- **Image / file / post support** — Screenshots, attachments, rich-text posts all downloaded and piped to Claude
- **Skills passthrough** — `/commit`, `/review`, etc. work directly
- **Smart idle timeout** — Detects active child processes, won't kill long compilations
- **`cc-lark` launchctl wrapper** — One-command install/start/stop/restart/status/logs for macOS
- **Dispatch / session group** — Mention bot in any group, it auto-creates a thread in a designated "session group" and runs the task in an isolated session there
- **Cron tasks** — YAML-defined cron jobs that fire posts into a thread group and spawn isolated sessions; hot-reload via `/reload` endpoint
- **Split control plane** — Public ngrok listener serves card callbacks only; privileged local APIs use a loopback-only, Bearer-authenticated port

Quick start: clone, `pip install -r requirements.txt`, configure `.env` with Feishu/Lark app credentials, run `python3 main.py` (or `./deploy/cc-lark install` on macOS).

See Chinese sections above for detailed setup instructions.

## License

[MIT](LICENSE)
