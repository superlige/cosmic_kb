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
| `cosmic_kb calls <类全限定名> <方法名>` | 方法出向调用导航：调了项目内哪些方法 + 本方法读写字段 |
| `cosmic_kb source <相对源文件路径>` | 读源码（野生编码正确解码）+ 自动标注字段真名 |
| `cosmic_kb resolve <字段标识> …` | 字段名 O(1) 核对：标识→真实中文名 + 坐标，钉不出回 null |
| `cosmic_kb coverage` | **信任优先**：以元数据字段为分母的覆盖率 + 扫描质量分解 |
| `cosmic_kb scan-compare` | **信任优先**：粗扫(字面量) vs 高精度对比 → 疑似盲点 / 精度增量 |
| `cosmic_kb dynwrites` | **信任优先**：字段 key 钉不出的动态读写按「该读方法」去重列出 |
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

它把 7 个排障主路径命令包成 MCP 工具——`ask / trace / bill / method_calls / resolve_fields /
read_source / cosmic_semantics`（返回值与 CLI `--json` **同口径**，每次只回最小证据包，源码全文由
大模型经 `read_source` 正确解码读取），其中 `cosmic_semantics(topic)` 回传苍穹领域知识（插件类型 /
事件时机 / SDK 用法 / 入库判断 / 反模式黑名单）。审计类命令（`coverage / scan-compare / dynwrites`）
下沉为 CLI-only，段二只留这 7 个。各工具返回字段的含义见上节「返回值字段词典」。**苍穹纪律（三态置信度、
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

## 返回值字段词典（工具到底回了什么）

工具返回（CLI `--json` / MCP）刻意用了一批专有字段名（`unlocated` / `dynamic_writers` /
`possible` …）。它们都不是随手起的——每个对应「老项目分析天生不完整」里的一种**确定性程度**。
读懂它们，才能分清哪些是确诊、哪些是「值得人工/大模型去核的工作单」、哪些被截断需翻页。下面按
工具逐一解释。

### 贯穿所有工具的通用约定

| 字段 | 真实含义 |
|------|---------|
| `summary` | **真实总数（永远全量）**。无论返回怎么被裁剪，这里的计数都是库里的真值——判「完整 vs 截断」先看它。红线 #4。 |
| `*_total`（如 `groups_total` / `fields_total`） | 该列表的真实条数。返回里只展示了一部分时，全量数在这。 |
| `*_capped` / `capped` / `sites_capped` / `methods_capped` | **被设计内 cap 截掉的条数**（>0 表示这一段没列全）。不是丢数——总数在 `*_total`，被截条目可翻页取回。 |
| `next_cursor` / `*_next_cursor`（如 `"unlocated@4"`） | **翻页游标**：把这个值原样作 `cursor=` 再调一次同一工具，即取回该段下一页（`page.items` + 新 `next_cursor`），循环到 `null` 即把被截条目**一条不漏取全**。 |
| `note` | 人读提示：为何没命中、被截了该怎么收窄/翻页、tree-sitter 没装等。 |
| `confidence` / 三态 | `confirmed`（确诊）/ `likely`（很可能）/ `unknown`（判不准，标 unknown 不臆造）。 |
| `persists`（落库三态） | `yes`=落库（写进数据库，CLI 显示 ✅落库）；`no`=仅内存（—内存）；`unknown`=存疑（❓存疑，缺保存链路一律判这个，不臆造）；`na`=不适用。 |

### `trace` —— 字段排障旗舰（参数最多，重点看这个）

输入字段、输出「谁读写它/在哪个事件/是否落库」。返回里**三种"命中桶"按确定性递减**排列：

| 字段 | 真实含义 |
|------|---------|
| `field_key` / `field_name` | 被查字段标识 + **已核对的真实中文名**（来自元数据，引用时照抄，**别按拼音/命名惯例猜**）。同一 key 跨多坐标且名字不同时 `field_name` 为 `null`，消歧看 `occurrences`。 |
| `filter` / `precise` | 本次查询的坐标过滤（form/entry/level）、是否精确到单一坐标。 |
| `occurrences` / `occurrences_total` | 该字段在**元数据里的定义坐标**（单据·层级·分录）——即"这个字段名在哪些单据上有定义"的**消歧菜单**，不是读写记录。 |
| `groups` / `groups_total` / `groups_capped` | **确诊命中**：按坐标 (单据·层级·分录) 分组，每组列该坐标下读写它的类。裸字段会命中多张单据，故 `groups` 可能被裁——真实组数在 `groups_total`。 |
| `groups[].writers` | 该坐标的**写入**，按写入类合并：`classes[]` 每类一份，行号/落库/via 列在该类的 `sites`。`total`/`capped`/`sites_capped` 同通用约定。 |
| `groups[].readers` / `readers_overview` | 读取。默认只回**按类计数概览** `readers_overview`（最省字节）；要读取明细另调 `access='read'`，回 `readers`（类→方法，顺 `calls` 去读源码）。 |
| `possible` | **存疑命中**：同单据同字段、但**层级/分录对不精确**（碰哪级分录判不准）的读写。不丢弃、降一档放这（"宁可多列也不漏"）。 |
| **`unlocated`** | **「反推来源单据」工作单**：这些读写**确实碰了被查字段**，但来源 DynamicObject **属于哪张单据没钉出**（`form_key=None`）。按方法去重，每条带 `calls` 导航 + `plugin_form_label`（该插件注册的单据，**只是线索非确诊**，别当确定来源）+ `null_reason`（见下）。要确认它改的是哪张单据，顺 `calls`/读源码反推。 |
| **`dynamic_writers`** | **「字段钉不出」的动态写入候选**：代码对运行时/配置决定的字段集做泛化写入（循环遍历/字符串拼接 key/引用外部常量），静态钉不出**具体改了哪个字段**，但可能正改本字段。按"该读方法"去重列出（`methods[]` + `calls` + `by_cause`），交人/大模型读源码定性。⚠️ 与 `unlocated` 对称：`unlocated` 是钉不出"哪张单据"，`dynamic_writers` 是钉不出"哪个字段"——**两者都是工作单不是结论，别直接当成"谁改了它"的答案**。 |
| `coarse`（仅 `access='read'`） | **粗扫疑似盲点**：纯正则把字段标识当源码字面量搜到、但高精度分析没覆盖的位置（`coarse_only` + `locations` 带行号）。候选非确诊，供人工核对扫描盲区。 |
| `summary` | 真实总数集合：`writers`/`readers`/`persisting_writers`(落库写入数)/`uncertain_writers`(存疑写入数)/`plugins`/`forms`/`coords`(坐标组数)/`possible`/`unlocated`/`dynamic_writers`/`annotation_writers`(注解反射映射写入数) + `unlocated_by_reason`(未定位成因直方图)。 |

#### `null_reason` —— `unlocated` 每条/每段的成因码（该不该追）

`form_key=None` 的行为何 None，决定了"值不值得顺源码反推"。7 个**互斥**成因码：

| 成因码 | 含义 | 该追吗 |
|--------|------|-------|
| `basedata-ref` | 写的是基础资料对象本身，本就无业务单据坐标 | **正确 None，无需追** |
| `dynamic-entity` | ORM 实体名是运行时变量/拼接，静态不可钉 | **正确 None，无需追** |
| `helper-caller-unknown` | helper 的 DynamicObject 入参，调用方未安全收敛 | 值得顺 `calls` 读源码反推 |
| `local-or-container-source` | 本地 new/Map/返回值等容器来源未识别 | 值得读源码反推 |
| `model-context` | `getModel()`/模型形参写入，但插件未注册绑定单据 | 读源码/补元数据可定（多为未注册表单插件） |
| `field-key-undeterminable` | 字段 key 本身就钉不出（动态/拼接/外部常量/歧义） | 来源讨论无意义 |
| `unknown` | 暂无足够证据归因 | 先补证据再判断 |

全量成因分布在 `summary.unlocated_by_reason`（真实总数恒在此）。

### `bill` —— 单据钻取

| 字段 | 真实含义 |
|------|---------|
| `form` / `stats` | 单据元信息 + 计数（实体数/字段数/操作数/插件数/被触达字段数）。 |
| `entities` / `fields` / `operations` / `plugins` / `bindings` | 该单的实体、字段元、操作集、插件清单、动态绑定（各有 `*_total`，被截带 `*_next_cursor`）。 |
| `entity_touch` | 按实体分组的**字段触达**：每字段被触达的逐条事件已折叠为「写/落库/读」计数（`writers`/`persisting`/`readers`）。每行带 `trace` 锚点（`"trace 单据.字段"`）——要看"某字段谁改的/在哪个事件/是否落库"，照它对该字段 `trace`。 |
| `risk_bindings` | 桥接有风险的绑定（如插件类没在源码命中），通常很少、整列内联。 |

### `method_calls` —— 方法出向调用导航

野生多前缀码上的"跳转到定义"。**只导航不解释**（方法在干嘛请读源码自己判）。

| 字段 | 真实含义 |
|------|---------|
| `found` | 是否定位到类+方法。`False` 时带 `reason`（`class_not_found`/`class_ambiguous`/`method_not_found`）+ `candidates`，请挑全限定名/正确方法名再查。 |
| `methods[].calls` | 该方法调用的**项目内**方法清单，每条：`name`(调用名) + `target_fqn`(目标类全限定名，可再对它 `method_calls` 下钻) + `target_relpath`(目标源文件，去这里接着读) + `line`(调用行号)。**只列项目内可下钻调用**，平台/外部/`equals`/常量调用一律不回（噪声）。 |
| `methods[].fields` | 该方法体读写的字段：`writes`/`reads`（带**已核对中文名** `field_name` + `persists` + `semantics_topic`）+ `dynamic_writes`(钉不出具体字段的动态写入数，只计数)。导航到方法、还没读源码就拿到真名，杜绝猜字段名。 |
| `semantics_topic` | 若该方法是苍穹事件回调，指明该读哪篇语义文档（如 `plugin-form`/`plugin-operation`）——解释它"何时触发/是否入库"前先 `cosmic_semantics(该 topic)`。 |

### `resolve_fields` —— 字段名 O(1) 核对

手上有一串字段 key、想核对真名时用（已在读源码用 `read_source` 即可，它自带 `field_names`）。

返回 `{"resolved": {key: [item, ...] | null}}`，每个 item 带 `kind` 判别：

- `kind:"field"`：字段定义 —— `{name, form_key, entity_key, level, field_kind, field_type, access}`。
  `access` 是派生取值语义：**多选基础资料字段（MulBasedataField）也用 `getDynamicObjectCollection()` 取集合，不是分录行**——取分录还是基础资料取决于 key，别凭 API 名当分录。
- `kind:"entry"/"subentry"/"header"`：**分录容器** key（不是字段 key）—— `{name, form_key, level, parent_key, access}`。
- 同 key 跨多坐标 → 回 list 全摆出（**不替你选**，消歧靠你读代码时的实体上下文）；**钉不出回 `null`——标 unknown，绝不臆造**。

### `read_source` —— 读源码（野生编码正确解码 + 自动标注字段名）

| 字段 | 真实含义 |
|------|---------|
| `content` / `content_next_cursor` | 正确解码、行号与 KB 对齐的源码正文；未读全时带游标，`cursor=该值` 续读至文件末尾。 |
| `encoding` / `total_lines` / `lines` | 探测到的编码、文件总行数、本次返回的行区间。 |
| `field_names` | 本文件出现的字段标识 → 真名，已按本文件**数据包来源做归属消歧**，按 `tier` 分三档： |
| └ `tier:"unique"` / `"resolved"` | 唯一确定 / 已按上下文解析到具体实体——`names` 可**直接照抄**。 |
| └ `tier:"ambiguous"` | **多张单据有同名字段、本文件未解析到具体实体**——`names` 为空，**别默认当前单据**，按 `note` 顺调用链消歧。 |

### `cosmic_semantics` —— 苍穹领域语义文档

- 命中 → `{topic, content}`（单篇 markdown 全文）。
- 空参/未命中 → `{status:"need_topic", available_topics:[...], grouped:{...}}`，每条带「何时用」，挑一个主题名再取全文。

> 想看 `trace` 返回"到底完整还是截断"的完整推演（三种"截断"的区别、游标分页机制），见
> [`docs/trace返回详解.md`](docs/trace返回详解.md)。

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
├── tests/                    # pytest（330 passed）
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
