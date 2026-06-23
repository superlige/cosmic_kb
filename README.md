# cosmic_kb —— 苍穹老项目本地排障导航工具

> 接手一个陌生的金蝶云苍穹（Cosmic）老项目，本工具**指向它的源码和元数据，纯本地扫一遍**，
> 然后让你用「字段 / 单据 / 自然语言」直接查：**这个字段是谁改的、在哪个插件的哪个事件函数、
> 改完落不落库、源码第几行** —— 所有结论都带类 / 方法 / 事件 / 行号证据，判不准就标 `unknown`，**绝不臆造**。

纯本地运行、不外传源码；专治「野生」苍穹项目：多模块、可能不可编译、缺依赖、GBK/UTF-8 混合编码、多 ISV 前缀。

---

## 它替你消灭的那件苦差事

排查苍穹单据的字段 bug 时，老办法是：在元数据里找到这张单据绑定的**一堆插件全路径**，
再一个个复制到源码里翻、肉眼找哪个动了这个字段、还要判断它到底落没落库。几十个插件翻到崩溃。

本工具一条命令把这件事干完：

```powershell
cosmic_kb trace "cqkd_assetcard.cqkd_entry.cqkd_amount"
```

输出（示意）按 **单据 · 层级 · 分录** 坐标分组，每组列出谁读写、在哪、落不落库：

```
单据 cqkd_assetcard「资产卡片」 · 分录 cqkd_entry · 字段 cqkd_amount（金额）
  写  AssetCardSavePlugin.beforeSave()          ✅落库   src/.../AssetCardSavePlugin.java:128
  写  AmountCalcPlugin.propertyChanged()         —内存   src/.../AmountCalcPlugin.java:· 64
  读  AssetCardAuditPlugin.beforeAudit()                  src/.../AssetCardAuditPlugin.java:· 92
  可能命中（层级/分录存疑） …
  未定位单据（来源判不出、但确实读写该字段，供人工核对） …
```

`✅落库 / —内存 / ❓存疑`一目了然，路径行号可直接跳源码。**判不准的不丢弃**——归进「可能命中（存疑）」或
「未定位」桶，满足排障「宁可多列也不漏」。

---

## 安装（Windows / PowerShell，Python ≥ 3.10）

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1                     # 被拦就先： Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

pip install -e ".[parse,encoding,fuzzy,mcp]"   # 见下表按需取舍

cosmic_kb --version                            # 验证
cosmic_kb doctor                               # 资产体检：semantics/templates 应 OK（随包）；ok-cosmic-docs.db 为 OPTIONAL
```

| 可选组 | 作用 | 建议 |
|--------|------|------|
| `parse` | Java 静态分析（字段追踪的基础） | **必装** |
| `encoding` | GBK/混合编码自动探测（真实老项目几乎必需） | **必装** |
| `fuzzy` | `ask` 中文名↔标识模糊匹配 | 可选（不装会降级，仍可用） |
| `mcp` | 接 Claude / LLM 宿主的 MCP 服务器 | 仅对接大模型时装 |
| `dev` | pytest 自测 | 仅开发/验收 |

> 命令入口不可用时，等价用 `python -m cosmic_kb.cli.main ...`。

---

## 三步上手

### 1）建知识库（KB）—— 指向你的项目，扫一次

```powershell
cosmic_kb build "D:\项目源码根" "D:\元数据.zip"
```

- 第一个参数是 Java 源码根，第二个是元数据（`.dym` / 转换规则 `.cr` / 整包 `.zip` / 含 zip 的目录）。
- KB（`cosmic_kb.db`）**默认随源码根落盘**，一项目一库、互不覆盖。

### 2）`cd` 进该项目目录就近用 KB

读类命令（trace / bill / ask / coverage / web …）会从当前目录**向上自动发现** `cosmic_kb.db`，
免去每次手敲 `--db`。在别处也行，加 `--db <路径>` 即可。

### 3）开查

```powershell
# 旗舰：字段→谁改了它、事件函数、是否落库、行号（按层级显式录入更精确）
cosmic_kb trace "单据.字段"                    # 表头字段
cosmic_kb trace "单据.分录.字段"               # 分录字段
cosmic_kb trace "单据.分录.子分录.字段"        # 子分录字段
cosmic_kb trace "cqkd_amount"                  # 裸字段=列出全部定义坐标供选

# 单据钻取：这张单的操作集 / 插件 / 字段触达 / 风险
cosmic_kb bill "cqkd_assetcard"

# 自然语言提问（确定性取证，不调大模型；同名歧义会反问，不替你拍板）
cosmic_kb ask "cqkd_amount 这个金额是谁改的"

# 本地浏览器排障：输字段→定宽表格→点路径跳源码
cosmic_kb web
```

---

## 命令速查

| 命令 | 作用 |
|------|------|
| `cosmic_kb build <源码根> <元数据>` | 建/重建 KB（含 Java 字段级分析），随源码根落盘 |
| `cosmic_kb trace <单据.[分录.[子分录.]]字段>` | **旗舰**：字段→读写它的插件/事件/是否落库/行号 |
| `cosmic_kb bill <单据标识>` | 单据钻取：操作集 / 插件 / 字段触达 / 风险 |
| `cosmic_kb ask "<自然语言问题>"` | NL→意图→查 KB 取证（消歧退出码 3，`--json` 喂 Skill） |
| `cosmic_kb coverage` | **信任优先**：以元数据字段为分母的覆盖率 + 扫描质量分解 |
| `cosmic_kb scan-compare` | **信任优先**：粗扫(字面量) vs 高精度对比 → 疑似盲点 / 精度增量 |
| `cosmic_kb web` | 本地浏览器排障（含「扫描可信度」页签） |
| `cosmic_kb mcp` | 起 MCP 服务器，把取证命令暴露给 LLM 宿主 |
| `cosmic_kb meta <dym\|cr\|zip>` | 只解析元数据，看分类计数 / JSON 快照 |
| `cosmic_kb bridge <源码根> <元数据>` | 元数据 `<ClassName>` ↔ 源码桥接命中率报告 |
| `cosmic_kb ingest <源码根>` | 只做源码摄取 + 解析可信度报告 |
| `cosmic_kb doctor` | 资产体检 |

---

## 信任优先：先证明「扫得准」再信结论

老项目分析天生不完整，所以**覆盖率/可信度是一等功能，不是事后补**：

- `cosmic_kb coverage` —— 以**元数据业务字段为分母**算字段覆盖率，并按「标识解析 / 来源定位 /
  落库判定 / 命中元数据」四维分解质量。
- `cosmic_kb scan-compare` —— 把**高精度**结果（AST + 跨类 + 落库判定）和**粗扫**（纯正则把字段标识
  当源码字面量搜）对比：两者都见=互证；仅粗扫见=**疑似盲点**（带源码行号，候选非确诊）；仅高精度见=
  常量解析触达的**精度增量**。诚实给红绿灯。

`web` 的「扫描可信度」页签把这两样接上，可视化查看。

---

## 接入任意 AI agent（MCP）

工具本身**不调 LLM**——确定性建库、确定性取证。要让大模型帮你做语义解释/排查建议时，走 **MCP**——
一次注册，**任意支持 MCP 的 agent 通用**（Claude Code / Claude Desktop / Cursor / Cline / Windsurf / Codex …）。

它把 `ask / trace / bill / method_calls / coverage / scan_compare` 包成 MCP 工具（返回值与 CLI `--json`
**同口径**，每次只回最小证据包，源码全文由大模型直接读本机文件），并额外暴露 `cosmic_semantics(topic)`
回传苍穹领域知识（插件类型 / 事件时机 / SDK 用法 / 入库判断 / 反模式黑名单）。**苍穹纪律（三态置信度、
不臆造、入库判断）随 MCP `instructions` 注入宿主系统提示**，故非 Claude agent 也能"带语义"作答，无需单独装 Skill。

### 使用者：把 agent 接到本工具的 MCP

前提：已 `pip install` 本工具（拿到 `cosmic_kb-mcp` 命令），并已在你的苍穹项目目录 `cosmic_kb build` 出 KB。

**启动命令**：`cosmic_kb-mcp`（stdio）。KB 路径优先级：环境变量 `COSMIC_KB_DB` > 启动目录就近向上发现
`cosmic_kb.db` > 当前目录。多项目时**给每个项目一份配置、用 `COSMIC_KB_DB` 指到该项目的 KB** 最稳。

- **Claude Code**：项目根已带 [`.mcp.json`](.mcp.json)，在该项目里启动 `claude` 自动识别、批准即用；
  或 `claude mcp add cosmic_kb -- cosmic_kb-mcp`。
- **Claude Desktop / Cursor / Cline / Windsurf / Continue**（通用 `mcpServers` JSON，填进各自的 MCP 配置文件）：

  ```json
  {
    "mcpServers": {
      "cosmic_kb": {
        "command": "cosmic_kb-mcp",
        "env": { "COSMIC_KB_DB": "D:\\你的项目源码根\\cosmic_kb.db" }
      }
    }
  }
  ```
  > 各家配置文件位置：Claude Desktop=`claude_desktop_config.json`；Cursor=`.cursor/mcp.json`；
  > Cline/Windsurf/Continue 见各自「MCP Servers」设置。若 `cosmic_kb-mcp` 不在 PATH，把 `command` 换成
  > venv 里的绝对路径，或用 `"command": "python", "args": ["-m","cosmic_kb.cli.main","mcp","--db","<KB路径>"]`。

- **Codex**（`~/.codex/config.toml`）：

  ```toml
  [mcp_servers.cosmic_kb]
  command = "cosmic_kb-mcp"
  env = { COSMIC_KB_DB = "D:\\你的项目源码根\\cosmic_kb.db" }
  ```

接好后直接问 agent「cqkd_amount 这个金额是谁改的」「这张单有哪些插件」「这个方法调了什么」，
它会自动调 MCP 工具取证、带类·方法·行号·三态置信度作答。

---

## 分发给同事 / 用户（工具开发者视角）

本工具已重构为**自包含包**（语义文档、模板等随包，见 [`docs/分发与多agent接入方案.md`](docs/分发与多agent接入方案.md)），
可当普通 wheel 分发：

**① 出 wheel（推荐，最干净）**

```powershell
pip install build
python -m build --wheel                         # 产出 dist\cosmic_kb-<版本>-py3-none-any.whl（资产已随包）
```

把这个 `.whl` 发给对方，对方：

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install "cosmic_kb-<版本>-py3-none-any.whl[parse,encoding,fuzzy,mcp]"
cosmic_kb doctor                                # semantics/templates OK 即装好
```

**② 整包 zip 兜底（离线内网、对方要看/改源码时）**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\make_dist.ps1
```
产出 `dist\cosmic_kb_dist_v<版本>_<日期>.zip`（自带「安装说明.md」），解压后 `pip install -e .` 即可。

> 9MB 离线 SDK 文档库 `ok-cosmic-docs.db` **不进 wheel**（运行期暂未消费）；需要时用环境变量
> `COSMIC_KB_DOCS_DB` 指向。后续上 PyPI / `uvx` / `.mcpb` / Smithery 一键分发另见分发方案文档。

---

## 设计红线（贯穿全程）

1. **本地优先**：纯本地建库取证；接大模型走 MCP 传最小证据，并允许其直接读本机源码全文做完整理解。底线是 KB / 报告不上公网、Web 仅绑 `127.0.0.1`。
2. **代码是野生的**：解析器**绝不依赖编译或依赖解析**，硬扛混合编码与多前缀。
3. **规模大**：要性能、进度、缓存、增量。
4. **信任优先**：覆盖率/可信度是一等功能。
5. **接手者视角**：先能定位字段、钻取单据。
6. **两段式解耦**：确定性扫描器（建 KB）与 AI 理解层（查 KB）解耦，**KB 是契约**。

> 派生哲学：**处处置信度 + 证据行号 + `unknown`** —— 宁可标 unknown，也不臆造。

---

## 两段式架构

```
段一  本地确定性扫描器  cosmic_kb/（Python 包）
      摄取 → 元数据解析 → Java 静态分析 → 桥接 → SQLite KB（图谱+FTS5）+ 覆盖率/排障报告
                          ↓  KB 是两段之间的契约
段二  AI 理解层  任意 MCP agent（苍穹语义已下沉进包，经 MCP `instructions` + `cosmic_semantics` 注入）
      查 KB 取证 → 取苍穹语义(cosmic_kb/semantics) → 输出带证据的解释 / 排查建议
      （comic-understand-long/ 为 Claude Code 的 skill 增强入口，非必要）
```

---

## 目录结构

```
cqkd_ai/
├── pyproject.toml            # cosmic_kb 可安装包定义
├── cosmic_kb/                # 段一：确定性扫描器
│   ├── cli/                  #   命令行入口
│   ├── ingest/ metadata/     #   源码摄取 / 元数据解析
│   ├── java/                 #   Java 静态分析 / 字段追踪 / 落库判定
│   ├── bridge/ graph/        #   元数据↔代码桥接 / SQLite 图谱
│   ├── semantic/ context/    #   NL→意图 / Context Builder
│   ├── report/               #   字段排障 / 单据钻取 / 覆盖率 / 扫描对比
│   ├── web/ mcp/             #   本地 Web / MCP 服务器（含 cosmic_semantics + instructions）
│   ├── semantics/            #   随包语义文档（references / rules，cosmic_semantics 取数源）
│   ├── metadata/templates/   #   随包继承根模板（bos_billtpl / bos_basetpl，操作 oid 回填）
│   └── _assets.py            #   资产定位（importlib.resources，随 wheel 走）
├── comic-understand-long/    # Claude Code skill 增强入口（SKILL.md + scripts，语义已下沉进包）
├── skill_assets/             # 离线 SDK 文档库 ok-cosmic-docs.db（可选，不进 wheel）
├── scripts/                  # 整包兜底脚本（make_dist.ps1 + 安装说明.md）
├── tests/                    # pytest（187 passed）
├── docs/                     # 项目企划 / 开发计划 / 阶段验收
├── samples/                  # 示例元数据
└── vendor/                   # 上游 ok-cosmic 原件（参考）
```

---

## 更多文档

- [`CLAUDE.md`](CLAUDE.md) —— 协作开局须知、关键决策、当前进度
- [`docs/开发计划.md`](docs/开发计划.md) —— 分阶段交付蓝图
- [`docs/阶段验收.md`](docs/阶段验收.md) —— 各阶段「实现了什么 + 验收记录」
- [`comic-understand-long/SKILL.md`](comic-understand-long/SKILL.md) —— 段二 AI 理解层操作手册
