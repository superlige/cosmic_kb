"""段二分发 · 对话式安装 Bootstrap 编排器（首版）。

依据 `docs/设计方案/分发与多agent接入方案.md` §3。分两块：

- :mod:`cosmic_kb.bootstrap.mcp_register`：MCP 自动注册（写共享 `.mcp.json` / TRAE 导入包）+
  `verify_mcp()` 子进程起服务器做 `initialize`+`tools/list` 四工具校验（§3.6 + §3.2 步骤 5–6）。
- :mod:`cosmic_kb.bootstrap.orchestrator`：`plan/apply/status` 三段编排 + `install.json` 清单
  （§3.1–§3.5）。每步幂等、可断点续跑；数据库口令单进程隐藏输入，绝不落盘。

设计红线：所有产物（清单 / 配置 / JSON / 日志）**绝不写数据库口令**（红线 #1）。
"""

from __future__ import annotations

from .mcp_register import (
    SERVER_NAME,
    REQUIRED_TOOLS,
    McpRegisterError,
    register,
    server_spec,
    verify_mcp,
)

__all__ = [
    "SERVER_NAME",
    "REQUIRED_TOOLS",
    "McpRegisterError",
    "register",
    "server_spec",
    "verify_mcp",
]
