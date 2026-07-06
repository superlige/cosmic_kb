# 分发与多 agent 接入方案（用户侧"整套工具"安装）

> 本文是**方案设计**，不含实现。终极目标：用户**一键安装 → 自己的 agent 直接开始使用本工具**，
> 且**不绑定某一个 agent**（Claude Code / Codex / Cursor / Cline / Windsurf 等任意支持 MCP 的宿主）。
> 决策拍板：2026-06-22（用户要求兼容市面常用 agent）。
> 2026-06-22 二次修订：补市场调研——主路线从"自研 PowerShell 安装脚本"改为**搭现成的 MCP
> 分发车（自包含包 + .mcpb + Smithery + uvx）**，自研脚本降级为兜底。

---

## 1. 问题：用户装完为什么"调不起来"

现有分发（`scripts/make_dist.ps1` → zip + `scripts/安装说明.md`）**只覆盖了段一**：
venv + `pip install -e`，装好 `cosmic_kb` 命令行。但段二（让 agent 自动取证 + 用苍穹语义解释）
有两条接线，**既没自动化、也没写进说明书**，于是用户装完发现 agent 根本不会调：

| # | 组件 | 用户应得到 | 现状 |
|---|------|-----------|------|
| 1 | 段一 `cosmic_kb`（Python 包） | `cosmic_kb build/trace/ask` 命令 | ✅ 已覆盖 |
| 2 | **MCP 证据通道** | 任意 agent 能调 `ask/trace/bill/coverage/scan_compare` | ❌ 包里有 `.mcp.json`，但没注册进用户的 agent |
| 3 | **段二语义层** | agent 用苍穹语义（插件类型/事件时机/入库规则/三态置信度纪律）解释证据 | ❌ 只存在于 Claude 私有的 `comic-understand-long/SKILL.md`，非 Claude agent 拿不到 |

---

## 2. 架构判断：语义层必须从"Claude Skill"下沉到"MCP 服务器自带"

这是本方案的核心决定，影响后续所有实现。

- **MCP 是跨 agent 的唯一通用标准**。Claude Code、Codex、Cursor、Cline、Windsurf、Continue、
  VS Code（Copilot agent 模式）等都支持"注册一个 MCP server，调它暴露的 tools"。所以**段二的
  证据通道（组件 2）天然可移植**——一次实现，到处可用。
- **`Skill` 是 Claude Code 私有格式**（`.claude/skills/<名>/SKILL.md` + frontmatter 自动加载）。
  Codex 读 `AGENTS.md`、Cursor 读 `.cursor/rules`、各家"指令/规则"机制互不相同。**把段二语义层
  只绑在 SKILL 上 = 只服务 Claude Code，违背"兼容任意 agent"的目标。**

### 结论：语义层走 MCP，Skill 仅作 Claude Code 的增强

把 `SKILL.md` 里那套苍穹语义（纪律 + 路由 + references）**下沉进 MCP server 本身**，让任何 agent
都能通过 MCP 拿到，而不是依赖某家私有的技能机制。三个落地手段（MCP 协议原生支持，FastMCP 都能做）：

1. **server `instructions`**：`FastMCP("cosmic_kb", instructions="…")`。MCP 初始化时返回给宿主，
   多数客户端会并入系统提示。写**最小路由 + 纪律**：
   > "这是苍穹历史项目理解工具。问'某字段谁改的/某插件干嘛的/某单据有哪些操作'，先调
   > `ask/trace/bill` 取证；结论必须带类·方法·行号·`confirmed/likely/unknown` 三态，缺保存链路
   > 判 unknown，不臆造字段/方法名。解释苍穹 SDK 语义或插件事件时机时调 `cosmic_semantics`。"
2. **新增工具 `cosmic_semantics(topic)`**：返回 `comic-understand-long/references/` 与 `rules/`
   里对应主题的 markdown（插件类型、事件边界、DynamicObject 路径、入库判断、anti-patterns 黑名单）。
   任何 agent 需要苍穹领域知识时按需调，**该工具本身只回传一篇语义文档**（源码全文仍由大模型按需直接读本机文件）。
3.（可选）**MCP `prompts`**：把"字段追溯""单据钻取"做成可复用 prompt 模板，支持 prompts 的客户端
   能一键唤起。支持度不如 tools 普及，作锦上添花。

> 落地后，**非 Claude agent 仅靠"注册 MCP server"这一步就拿到完整段二能力**（证据通道 + 语义层）。
> Claude Code 额外把 `comic-understand-long/` 装进 `.claude/skills/` 作增强，但**不再是段二能用的必要条件**。

---

## 3. 市场调研：「一键装 → agent 直接用」是半解决问题，应搭现成车

2026-06 调研结论：**这个目标市面已有成熟机制，且 Context7 这类工具就是活样板**（装一次 MCP
server，之后 agent 自动调用）。我们**不该自研安装器，而该把自己改造成能搭这些车的标准件**。

### 3.1 现成的分发/一键机制

| 机制 | 谁做的 | 能到什么程度 | 局限 |
|------|--------|------------|------|
| **DXT / `.mcpb`(MCP Bundle)** | Anthropic | 真·一键：双击 `.mcpb`，依赖全打进包，像 Chrome 扩展 | 以 **Claude Desktop** 为主，不通吃 Codex/Cursor |
| **Smithery CLI** | 第三方 registry | **跨客户端**：`npx @smithery/cli install <server> --client cursor\|claude\|windsurf` 自动写对应配置 | 要先发布到 Smithery + npm/PyPI；每个 client 装一次 |
| **`uvx` / `npx` 直跑** | uv / npm | 零预装：`uvx cosmic-kb-mcp` 一行起服务，配置填这条命令即可 | 要求包**已发布且自包含** |
| **MCP Registry + 深链** | 官方 / Cursor / VS Code | "Add to Cursor"按钮、`vscode:mcp/install` 深链，点一下进配置 | 各家深链格式不同 |

### 3.2 三个坑（其中坑 B 正卡死我们现状）

- **坑 A — "一键"分生态，无单一产物通吃所有 agent。** `.mcpb` 偏 Claude Desktop，Smithery 跨
  客户端但每个 client 装一次。现实选择：针对用户实际在用的 2–3 个 agent 各给原生安装，或挂
  Smithery + 留一段兜底配置片段。**"一个动作通吃全部 agent"目前不存在。**
- **坑 B（最致命）— 现成一键车都假设"标准自包含包"，而我们现在不是。** DXT 把依赖打成自包含
  server、Smithery/uvx 从 PyPI/npm 拉包。但本工具现在只能 `pip install -e .` 装**整个仓库**，因为
  `skill_assets/`、`comic-understand-long/` 必须是 `cosmic_kb/` 的同级目录（`_assets.py` 靠
  `parents[1]` 定位）。**这条目录布局依赖直接挡死所有现成一键车。**
- **坑 C — "工具出现" ≠ "agent 正确地用 + 带苍穹语义"。** 一键只保证工具注册上；纪律（三态
  置信度、不臆造、入库判断）只有通过 §2 的 MCP `instructions`/`cosmic_semantics` 注入才跨 agent
  生效。少了它，装得再顺，非 Claude agent 也只会"裸调"。Context7 的"自动正确使用"正是靠这层。

---

## 4. 修正后的主路线：把自己变成标准 MCP 件，再搭车分发

按依赖顺序，前两步是**前置改造**（不做则上不了任何一键车），后三步是**分发上车**：

1. **【前置·解坑 B】重构成自包含包**：`skill_assets/`、`comic-understand-long/`（references/rules）
   作为 `package-data` 打进 `cosmic_kb`（`pyproject.toml` 已为 `graph/*.sql`、`web/static` 做过，
   照此扩展）；`_assets.py` 改为 `importlib.resources` 定位，**去掉 `parents[1]` 同级目录依赖与
   `-e` 可编辑安装要求**。改完即可被 `pip install cosmic-kb` / `uvx` 正常消费。
2. **【前置·解坑 C】语义下沉进 MCP**（§2）：`server.py` 加 `instructions` + `cosmic_semantics`
   工具，让任意 agent 自动正确使用、带苍穹纪律。
3. **发布到 PyPI**（或先给 git URL 让 `uvx` 跑）：得到 `uvx cosmic-kb-mcp` 一行启动能力。
4. **出 `.mcpb` 包**：Claude Desktop 用户双击一键装（依赖随包，免 Python 环境折腾）。
5. **挂 Smithery**：Cursor / Windsurf / Cline / Codex 等用 `npx @smithery/cli install --client xxx`
   跨客户端装；并在 README 给一段标准 `mcpServers` JSON / Codex `config.toml` 片段作兜底。

> 用户侧最终体验：Claude Desktop 双击 `.mcpb`；其他 agent 一行 `npx … install --client xxx` 或填
> `uvx` 启动命令——**装完 agent 直接开始用，且带苍穹语义**。
> KB 仍由用户在自己苍穹项目目录 `cosmic_kb build` 生成；MCP server 走"就近发现 KB"（commit
> ecd1167 多项目方案 A），在哪个项目开 agent 就读哪个项目的 KB。

---

## 5. 兜底：自研 `install.ps1`（仅离线/内网/无法访问 registry 时用）

若用户处于**离线内网、装不了 PyPI/npm/Smithery**，退化回"整包 zip + 本地安装脚本"路线：

1. 建 venv + `pip install -e ".[parse,encoding,fuzzy,mcp]"`
2. `cosmic_kb doctor` 自检资产
3. 探测本机 agent（`claude`/`codex` 在 PATH？`~/.cursor/`、`~/.codex/` 存在？），按各自格式**幂等
   写入/合并** MCP 配置（用 venv python 绝对路径作启动命令，不写死 `--db`），已存在 `cosmic_kb`
   条目则跳过、不覆盖用户其他 server
4. 探测到 Claude Code 则复制 skill → `~/.claude/skills/`（Windows 用复制非软链）
5. 兜底打印各 agent 的手动配置片段
6. `make_dist.ps1` 顺手把 `install.ps1` 打进 zip（已走 git archive，脚本被 git 跟踪即随包）

> 注意：自研脚本同样**依赖 §4 第 1、2 步**——目录布局重构与语义下沉是所有路线的公共地基，
> 不是只为某条路线做的。

---

## 6. 风险与待办（实现前逐项确认）

1. **§4 第 1、2 步是一切的前置**：自包含包重构 + MCP 语义下沉。这是**新工作量（改代码）**，
   不是纯打包；不做则现成一键车与自研脚本都白搭。
2. **各 agent 的 MCP 配置路径/格式仍在演进**：§3.1 与 §5 的 client 适配必须在实现时按各 agent
   **现行官方文档**核实（`.mcpb` 已由 `.dxt` 改名即一例），照训练记忆写死会过时。
3. **`.mcpb` / Smithery 的安全审核与发布流程**：公开 registry 会审包；公司代码不外传由架构保证
   （工具只在本机分析，registry 上只有工具本身不含用户项目），但仍需确认发布合规。
4. **资产体积**：`ok-cosmic-docs.db` 打进 wheel/`.mcpb` 会让包变大，评估是否拆为可选下载。
5. **跨平台**：PyPI/`uvx`/`.mcpb`/Smithery 天然跨平台；仅 §5 兜底脚本需 `install.sh` 对等版。
6. **验收口径**：装完后硬验收 = 在真实苍穹项目目录，用至少两种 agent（如 Claude Code + Codex）
   各问"某插件的某事件方法做了什么"，确认都能自动调 MCP 取证、带证据与苍穹语义作答。

---

## 7. 决策小结（一句话）

> **"一键 + 任意 agent 直接可用"市面已能做（Context7 是活样板），但门票是把自己变成"标准自包含
> MCP 包 + 语义塞进 MCP"。** 故主路线 = 先重构自包含包 + MCP 语义下沉（公共地基），再搭现成车
> （PyPI/`uvx` + `.mcpb` + Smithery）分发；自研 `install.ps1` 仅作离线内网兜底。

---

## 附：市场调研来源（2026-06）

- Anthropic — Desktop Extensions: one-click MCP install: <https://www.anthropic.com/engineering/desktop-extensions>
- modelcontextprotocol/mcpb（Desktop Extensions / MCP Bundle）: <https://github.com/modelcontextprotocol/mcpb>
- MCP Desktop Extensions Guide 2026（DXT/MCPB/`.mcpb` 改名）: <https://agentskillshub.dev/guides/mcp-desktop-extensions/>
- Smithery CLI 文档（跨客户端安装）: <https://smithery.ai/docs/concepts/cli>
- Smithery CLI 用法与替代（2026）: <https://apigene.ai/blog/smithery-cli>
