# 分发与多 agent 接入方案（对话式安装 + KB 初始化 + MCP 注册）

> 本文记录分发方案及落地状态。**终极目标：用户在自己的 agent 对话里，粘一次"安装口令"，
> 就自动完成 cosmic_kb 安装 → KB 初始化 → MCP 注册，重启一次 agent 即可直接使用，且带苍穹语义纪律。**
>
> 决策沿革：
> - 2026-06-22 拍板：兼容市面常用 agent（不绑定单一宿主）。
> - 2026-06-22 二次修订：主路线从"自研 PowerShell 脚本"改为"搭现成 MCP 分发车（uvx/.mcpb/Smithery）"。
> - 2026-07-13 落地第一阶段：wheel 内置 `cosmic-kb-understand` / `cosmic-kb-setup`，新增
>   `cosmic_kb skill install/status/uninstall`，支持 CodeBuddy / Qoder / TRAE；当前不自动注册 MCP。
> - **2026-07-14 主路线修订（本次合并）**：把"对话式安装"提为主路线——**首版走自研 `cosmic_kb bootstrap`
>   编排器 + 版本固定的"安装口令"，以公开 PyPI + 用户级隔离运行时为地基，首版仅 Windows**。
>   uvx / `.mcpb` / Smithery 降级为**后续跨平台 / 跨 agent 扩展**，不再是首版必经路。语义下沉进 MCP
>   仍是所有路线的公共地基，不因换路线而丢。

---

## 1. 目标与达成口径（先把"对话式"说诚实）

目标 = **"粘一次口令 → 全自动装好建好注册好 → 重启一次 agent → 直接能用"**。

它能达成，但"完全无缝对话"有 **3 个协议 / 安全层面消不掉的裂缝**，对外口径必须诚实标注：

| 目标 | 能否对话式完成 | 裂缝 |
|------|--------------|------|
| 安装 cosmic_kb | ✅ 能（粘一次口令，agent 跑完） | 前提 **PyPI 已发布**；装 Python 时会弹一次审批（仍在对话内） |
| KB 初始化（dym/cr/zip） | ✅ 全程对话，无口令 | 无 |
| KB 初始化（只读数据库） | 🟡 除口令外全对话 | **口令必须终端 `getpass` 隐藏输入**，不进对话/配置/日志（红线 #1、setup skill 口令纪律） |
| MCP 注册 | ✅ 能写配置 + 能校验工具列表 | **注册完必须重启 / 重连宿主才生效**（MCP server 常驻，协议本身决定，任何方案都绕不开） |

> 结论：**dym/cr/zip 路径连口令都不用；数据库路径只在口令这一步出对话框；所有路径都要"重启一次 agent"。**
> 不要对外承诺"数据库路径也 100% 无终端"或"零重启"。

---

## 2. 依赖方向修正：首次入口不能是 Skill

现状 `cosmic-kb-setup/SKILL.md` 有一处**自举死循环**：它写"`cosmic_kb` 命令不存在时就安装"，
但 **Skill 要先装进宿主才会被触发**——没装 cosmic_kb 的机器根本触发不了这个 Skill。

修正后的依赖方向：

```
用户粘贴"安装口令"（纯文本，可复制进 CodeBuddy / Qoder / TRAE 对话框，不依赖任何预装 Skill）
        ↓
普通 Agent：检查 Windows/winget/Python → 装 Python(用户级) → 建隔离运行时 → 从 PyPI 装 cosmic-kb[complete]
        ↓
包内 Bootstrap 编排器：plan → apply（装 Skill、建 KB、注册 MCP、校验）
        ↓
重启 / 重连 Agent
        ↓
cosmic-kb-setup Skill 接管后续维护（重建 / 升级 / 诊断 / 修 MCP）
```

**普通 Agent 只负责到"pip install 成功"为止，之后一律交给包内 Bootstrap**，不再让模型自由推测安装步骤。

### 2.1 安装口令（版本固定，随每次发版生成）

每次发布生成一段**版本固定**的口令，放 PyPI 项目页和 README，样例：

```
请为当前项目安装并初始化 cosmic-kb==<版本>。
仅允许从 https://pypi.org/simple 安装，使用 %USERPROFILE%\.cosmic_kb\runtime 用户级隔离环境。
安装后运行该环境中的 cosmic_kb bootstrap plan --project "<当前项目>" --json，
根据返回的问题向我确认，再运行 bootstrap apply；数据库口令只能通过终端隐藏输入。
```

普通 Agent 据此执行：

1. 检查 Windows、winget、Python 3.10+。
2. 缺 Python：**先请求用户批准**，再用 winget 用户级装 Python 3.11；**无 winget 则停止**并给出官方安装入口，不强行绕。
3. 建 `%USERPROFILE%\.cosmic_kb\runtime` 隔离环境（不污染系统 Python / 项目 venv）。
4. 从官方 PyPI 装固定版本 `cosmic-kb[complete]`。

> ⚠️ 口令是"版本固定"的 → **必须在发版流程（`make_dist.ps1`）里自动生成并写回 README/PyPI**，
> 靠人肉维护必然与实际版本对不上。

---

## 3. 主路线：包内 Bootstrap 编排器（新代码，首版核心）

新增**不依赖任何 Skill**的公共 CLI 接口：

```
cosmic_kb bootstrap plan   --project "<项目根>" --agent auto --json
cosmic_kb bootstrap apply  --project "<项目根>" <初始化参数> --json
cosmic_kb bootstrap status --project "<项目根>" --json
```

### 3.1 `plan`（只读探测，产出待办与提问）

只读检查并返回：

- Python、托管运行时、包版本、命中的 Agent。
- Java 源码根候选。
- dym / cr / zip 候选、数据库配置候选。
- 已有 KB、Skill、MCP 配置、同名冲突。
- `questions`（须向用户确认的元数据来源 / 路径 / 覆盖行为）、`planned_actions`、`manual_actions`。

Agent 据 `questions` 在对话里向用户确认，再进 apply。

### 3.2 `apply`（按确定顺序、全程幂等）

顺序执行，每步幂等（中途失败后重跑 plan/apply 从缺失步继续，不重复破坏已完成结果）：

1. 写入**不含敏感信息**的安装清单。
2. 安装两份正式 Skills。
3. 用数据库或 dym/cr/zip 建 KB。
4. 运行 `doctor`，按需运行 `coverage`。
5. 注册项目级 MCP。
6. **独立启动 MCP，完成 `initialize` + `tools/list`，确认 `resolve_fields` / `trace` / `bill` /
   `callers` / `cosmic_semantics` 五个核心工具可用**——仅写入配置**不算**成功。
7. 输出宿主重连步骤。

### 3.3 安装清单 `%USERPROFILE%\.cosmic_kb\install.json`

记录 runtime Python 绝对路径、CLI 绝对路径、版本、来源。**绝不写数据库口令。**
Setup Skill 从这里拿 CLI 绝对路径，不假设 `cosmic_kb` 在 PATH。

### 3.4 依赖收敛：新增 `complete` extra

`complete` 聚合 `parse / encoding / fuzzy / mcp / postgres`，保证首装依赖集合固定、可复现。
（现状 pyproject 只有 parse/encoding/mcp/postgres，`fuzzy` 与 `complete` 均待补。）

### 3.5 数据库口令：单进程隐藏输入

```
cosmic_kb bootstrap apply ... --metadata database --prompt-db-password
```

Bootstrap 在**同一进程内**用 `getpass` 取口令、完成连通检查与建库；口令不落命令行 / 配置 / JSON /
日志 / 清单。若当前 Agent 无法提供安全交互终端，则**只要求用户在集成终端手动跑这一条**，跑完 Agent 读结果继续。

### 3.6 MCP 注册（沿用项目绑定 + 就近发现 KB）

- 固定 server 名 `cosmic_kb`；启动命令用托管运行时 Python 的**绝对路径**；显式传当前项目 KB 绝对路径。
- CodeBuddy / Qoder 自动写共享 `.mcp.json`；TRAE 生成配置并引导用户在设置页确认。
- **同名不同配置默认停止**，仅用户确认后**先备份再替换**（写配置走原子写，避免半写坏宿主）。
- 注册后必须实际 `initialize` + `tools/list` 校验五工具（见 3.2 第 6 步）。

---

## 4. 公共地基：语义层必须下沉进 MCP（不因换路线而丢）

这是老方案的核心结论，**对话式安装同样依赖它**，否则非 Claude agent 只会"裸调"工具、丢三态置信度纪律。

- **MCP 是跨 agent 唯一通用标准**，段二证据通道天然可移植。
- 把 `SKILL.md` 里那套苍穹语义（纪律 + 路由 + references）下沉进 MCP server 本身，三个手段（FastMCP 都支持）：
  1. **server `instructions`**：初始化时回给宿主，多数客户端并入系统提示。写最小路由 + 纪律
     （"先调 `trace/bill/resolve_fields` 取证；结论带类·方法·行号 + `confirmed/likely/unknown` 三态，
     缺保存链路判 unknown，不臆造字段/方法名；讲 SDK 语义/事件时机调 `cosmic_semantics`"）。
  2. **工具 `cosmic_semantics(topic)`**：按需回传包内 `semantics/references/`、`rules/` 里对应主题的
     markdown（插件类型、事件边界、DynamicObject 路径、入库判断、anti-patterns）；**只回一篇文档**，
     源码全文仍由大模型按需直接读本机文件。
  3. （可选）MCP `prompts`：把"字段追溯""单据钻取"做成可复用模板，支持度不如 tools，锦上添花。

> 落地后，非 Claude agent 仅靠"注册 MCP server"就拿到完整段二能力（证据通道 + 语义层）。

---

## 5. Skill 重新定位（去掉自举职责）

- `cosmic-kb-setup`：**删除"负责首次获得 cosmic_kb"的隐含职责**，改为**已安装环境**的项目初始化 /
  重建 / 升级 / 诊断 / 修 MCP。从 `install.json` 取 CLI 绝对路径，不假设在 PATH。
  **`install.json` 不存在时，明确返回"需要用安装口令启动 Bootstrap"，不再自举。**
- 首次 Bootstrap 即使已把 Skill 写入宿主，也**不依赖当前会话立即发现它**——当前会话继续靠 Bootstrap JSON 完成；
  重启 / 新开会话后 Skill 自动触发，后续新项目只需建库 + 注册项目 MCP，不再重装运行时。
- `cosmic-kb-understand`：保持不变，只有 KB 和 MCP 实际可用后才进入理解流程。

---

## 6. 后续扩展：搭现成车做跨平台 / 跨 agent（首版之后）

首版把体验在 Windows + 公开 PyPI + 用户级运行时上跑通后，再搭现成车覆盖更广生态：

| 机制 | 覆盖 | 局限 |
|------|------|------|
| `uvx` / `npx` 直跑 | 零预装，一行起服务 | 要求包已发布且自包含 |
| `.mcpb`(MCP Bundle) | Claude Desktop 真·一键双击 | 偏 Claude Desktop，不通吃 Codex/Cursor |
| Smithery CLI | 跨客户端 `npx @smithery/cli install --client xxx` | 要先发到 Smithery + npm/PyPI，每 client 装一次 |
| MCP Registry / 深链 | "Add to Cursor"、`vscode:mcp/install` | 各家深链格式不同 |

> **坑 A（客观事实）**："一个动作通吃全部 agent"目前不存在，各生态各上一次车。首版不追求它。

---

## 7. 兜底：离线内网 `install.ps1`

用户处于离线内网、装不了 PyPI/winget/Smithery 时，退回"整包 zip + 本地脚本"：

1. 建 venv + `pip install -e ".[complete]"`（或对应分组）。
2. `cosmic_kb doctor` 自检资产。
3. 探测本机 agent，按各自格式**幂等写入 / 合并** MCP 配置（venv python 绝对路径，不写死 `--db`；
   已有 `cosmic_kb` 条目则跳过，不覆盖用户其他 server）。
4. 探测到 Claude Code 则复制 skill → `~/.claude/skills/`（Windows 用复制非软链）。
5. 兜底打印各 agent 手动配置片段。
6. `make_dist.ps1` 把 `install.ps1` 打进 zip（git archive，脚本被跟踪即随包）。

> 兜底脚本同样依赖 §3.4（complete extra）+ §4（语义下沉）——它们是所有路线的公共地基。

---

## 8. 风险与待办（实现前逐项确认，按依赖排序）

1. **【第 1 号闸门】PyPI 必须先发布**：当前 `cosmic-kb` PyPI 上无可装版本 → 口令里的 `pip install`
   直接失败，整条主路线空转。可先 TestPyPI / git URL 让 `uvx` 跑通验证。
2. **【全新代码】Bootstrap 编排器 + `install.json` + `complete`/`fuzzy` extra**：仓库现状一行都没有，
   是新工作量（改代码），不是纯打包。
3. **【公共地基】语义下沉进 MCP**（§4）：不做则非 Claude agent 裸调、丢纪律，装得再顺也用不对。
4. **修 setup skill 自举死循环**（§5）：install.json 不存在时回"请用安装口令"。
5. **口令版本同步**：发版流程自动生成口令并写回 README/PyPI。
6. **runtime 自升级**：`cosmic-kb` 出新版时 `.cosmic_kb\runtime` 的升级路径要在 bootstrap 里明确
   （apply 幂等只覆盖重跑，不等于版本 bump）。
7. **winget 缺失面**：家庭版 / 企业策略下 winget 可能不可用，须 fallback 到官方 Python 安装页并停止，
   不强行绕。
8. **MCP 配置半写**：写配置走原子写 + 冲突先备份再替换，避免中途失败坏宿主。
9. **各 agent 配置路径 / 格式仍在演进**：实现时按各 agent **现行官方文档**核实，不照训练记忆写死
   （`.mcpb` 由 `.dxt` 改名即一例）。
10. **资产体积**：随包资产仅语义 `references/rules` 与继承根模板（均为小文本）。原 9MB `ok-cosmic-docs.db` 运行期从未消费，已整体移除，不再随包/评估拆分。
11. **跨平台**：首版仅 Windows；离线 `install.sh` 与 macOS/Linux 留后续。

---

## 9. 验收口径

- **单元测试**：安装口令版本固定、托管路径、install.json、plan/apply 幂等与断点续跑、路径转义、
  口令不落盘、同名冲突保护。
- **干净 Windows VM 两条路径**：
  1. 无 Python → Agent 请求批准 → 装 Python → PyPI 装包 → dym/cr/zip 建库。
  2. 已有 Python → 数据库隐藏输入 → 建库 → MCP 注册。
- **从 CodeBuddy / Qoder / TRAE 空白新会话开始**（机器上预先没有 cosmic_kb 和 Skills），证明：
  - 用户只粘一次安装口令；Agent 完成环境 / 包 / Skill / KB / doctor / MCP 注册；
  - TRAE 仅保留官方要求的界面确认；
  - **重启后实际调用 MCP 工具成功**；
  - 任一日志 / JSON / 配置中均**不存在数据库口令**。
- **硬验收**：真实苍穹项目目录，用至少两种 agent 各问"某插件的某事件方法做了什么"，确认都能自动调
  MCP 取证、带证据与苍穹语义作答。

---

## 10. 决策小结（一句话）

> **"对话式安装"能达成你的目标，门票是：先发 PyPI + 写全 `bootstrap` 编排器 + 语义下沉进 MCP + 修
> setup skill 自举。** 首版 = 自研 bootstrap + 版本固定安装口令（Windows / 公开 PyPI / 用户级隔离运行时），
> uvx/.mcpb/Smithery 作后续跨平台扩展，离线 `install.ps1` 作兜底。对外体验诚实口径 = **"粘一次口令 →
> 全自动装好建好注册好 → 重启一次 agent → 直接能用"**（dym/cr/zip 连口令都不用）。

---

## 附：市场调研来源（2026-06）

- Anthropic — Desktop Extensions: one-click MCP install: <https://www.anthropic.com/engineering/desktop-extensions>
- modelcontextprotocol/mcpb（Desktop Extensions / MCP Bundle）: <https://github.com/modelcontextprotocol/mcpb>
- MCP Desktop Extensions Guide 2026（DXT/MCPB/`.mcpb` 改名）: <https://agentskillshub.dev/guides/mcp-desktop-extensions/>
- Smithery CLI 文档（跨客户端安装）: <https://smithery.ai/docs/concepts/cli>
- Smithery CLI 用法与替代（2026）: <https://apigene.ai/blog/smithery-cli>
