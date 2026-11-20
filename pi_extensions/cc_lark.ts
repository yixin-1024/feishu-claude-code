// cc-lark 每轮挂给 pi 的扩展（pi_runner 用 -e 加载），只活在这一个 pi 进程里，不落盘。
//
// 1) 模型表：CC_LARK_PI_PROVIDER = {"provider": "google", "config": {...models}}。
//    registerProvider 带 models 会把 Google 的模型表整张换掉——pi 在 bot 里只接
//    pi_runner.PI_GEMINI_MODELS 那几款 Gemini（Gemini 4 全家 + 3.8 Flash）。pi 自带的表里
//    没有 Neon / Hydrogen / Carbon，不补的话它们会照抄 Argon 的价格和推理档位。
//    xhighModels（Neon / Hydrogen）：pi 的 Google 适配层只放行 minimal/low/medium/high，
//    模型表里 xhigh 映射成 high；会话档位是 xhigh 时，这里在请求发出前改写成 XHIGH。
//
// 2) cc-lark 运行时 MCP：pi 没有 --mcp-config，全局 ~/.pi/agent/mcp.json 又是所有会话共用的，
//    并发话题的 CC_LARK_* 各不相同，写进去会串。pi_runner 把本轮配置（Claude `--mcp-config`
//    同款 JSON：{"mcpServers": {...}}）放进 CC_LARK_PI_MCP_CONFIG，这里逐个 registerMcpServer。
//    exposure 用 direct：cc-lark 只有十几个工具，直接声明给模型，不用先 tool_search。
function readJson(name: string): any {
  const raw = process.env[name];
  if (!raw) return undefined;
  try {
    return JSON.parse(raw);
  } catch (err) {
    console.error(`[cc-lark] ${name} 不是合法 JSON: ${err}`);
    return undefined;
  }
}

export default function (pi: any) {
  const provider = readJson("CC_LARK_PI_PROVIDER");
  if (provider && provider.provider && provider.config) {
    try {
      pi.registerProvider(provider.provider, provider.config);
    } catch (err) {
      console.error(`[cc-lark] 注册模型表 ${provider.provider} 失败: ${err}`);
    }
    const xhighModels = new Set<string>(provider.xhighModels || []);
    if (xhighModels.size) {
      pi.on("before_provider_request", (event: any, ctx: any) => {
        const model = ctx && ctx.model;
        if (!model || model.provider !== provider.provider || !xhighModels.has(model.id)) return undefined;
        if (ctx.thinkingLevel !== "xhigh") return undefined;
        const payload = event && event.payload;
        const thinking = payload && payload.config && payload.config.thinkingConfig;
        if (!thinking || !thinking.thinkingLevel) return undefined;
        return {
          ...payload,
          config: { ...payload.config, thinkingConfig: { ...thinking, thinkingLevel: "XHIGH" } },
        };
      });
    }
  }

  const mcp = readJson("CC_LARK_PI_MCP_CONFIG");
  const servers = (mcp && mcp.mcpServers) || {};
  for (const [name, server] of Object.entries<any>(servers)) {
    try {
      pi.registerMcpServer(name, { exposure: "direct", ...server });
    } catch (err) {
      console.error(`[cc-lark] 注册 MCP server ${name} 失败: ${err}`);
    }
  }
}
