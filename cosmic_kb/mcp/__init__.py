"""段二 AI 理解层 · MCP 接入（把段一确定性取证暴露成 MCP 工具）。

定位：本子包是「大模型接入」的薄适配层，不做任何分析逻辑——只把 `cosmic_kb` 已有的
取证命令（`ask/trace/bill/coverage/scan-compare`）包成 MCP（Model Context Protocol）工具，
让 LLM 宿主（Claude Code / Claude Desktop / 任意 MCP 客户端）挂上 `comic-understand-long`
Skill 后，自己调工具取证、自己做自然语言推理。

为什么这么设计（对齐 CLAUDE.md 红线 #6「两段式解耦，KB 是契约」）：
- 取证逻辑只有一份，落在段一（resolver + context.builder + report.*）；MCP 工具返回值与
  CLI `--json` **完全同口径**，不重写、不另算。
- 解耦不破：LLM 不直接读 KB / 源码，只拿到**每次提问的最小证据包 JSON**。整库源码不出本机，
  正好满足放松后的红线 #1（"接入大模型解决问题优先，不强制离线"）下仍保住的隐私底线。

模块：
- `server`：FastMCP 服务器 + 五个工具的纯逻辑函数（纯函数不依赖 mcp 包，便于单测）。
"""
