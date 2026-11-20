// cc-lark 每轮挂给 pi 的扩展：把本轮的 MCP server（cc-lark 运行时工具）注册进会话。
//
// pi 没有 --mcp-config；全局 ~/.pi/agent/mcp.json 又是所有会话共用的，并发话题的
// CC_LARK_* 各不相同，写进去会串。所以 pi_runner 把本轮配置（Claude `--mcp-config`
// 同款 JSON：{"mcpServers": {...}}）放进环境变量 CC_LARK_PI_MCP_CONFIG，这里读出来
// 逐个 registerMcpServer。注册不落盘，只活在这一个 pi 进程里。
//
// exposure 用 direct：cc-lark 只有十几个工具，直接声明给模型最省事，不用先 tool_search。
export default function (pi: any) {
  const raw = process.env.CC_LARK_PI_MCP_CONFIG;
  if (!raw) return;
  let cfg: any;
  try {
    cfg = JSON.parse(raw);
  } catch (err) {
    console.error(`[cc-lark] CC_LARK_PI_MCP_CONFIG 不是合法 JSON: ${err}`);
    return;
  }
  const servers = (cfg && cfg.mcpServers) || {};
  for (const [name, server] of Object.entries<any>(servers)) {
    try {
      pi.registerMcpServer(name, { exposure: "direct", ...server });
    } catch (err) {
      console.error(`[cc-lark] 注册 MCP server ${name} 失败: ${err}`);
    }
  }
}
