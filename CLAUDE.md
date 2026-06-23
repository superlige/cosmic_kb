# CLAUDE.md — 开局须知（每个新会话先读这个）

> 本文件是与 Claude 协作的"开机引导"。新会话开始时先读本文件，再按需读
> `docs/开发计划.md`（蓝图）和 `docs/阶段验收.md`（进度台账），即可快速回到状态。
> **对话是易失的，文件才是持久记忆**——重要信息一律落盘到这三个文件和代码/测试里。

## 这是什么项目

**苍穹历史项目本地理解工具**：接手陌生的金蝶苍穹（Cosmic）老项目时，在本机上跑的
项目理解工具。两类输入 → 确定性扫描建知识库(KB) → AI 查 KB 理解项目。

- **不是**通用分析库，**是**"接手陌生苍穹老项目时本机跑的理解工具"。
- 两类输入：① 从 globalsdk 导出的元数据 dym/zip；② 公司项目 Java 源码（野生、多模块）。

## 六条硬约束（设计红线，任何改动都要守住）

1. **本地优先**：扫描建库取证全程本机离线完成；接入大模型时，**允许其直接读取本机源码全文以完整理解代码**（不再强制只传最小证据包）。唯一底线是不把 KB / 报告 / 源码发布到公网站点，Web 仅绑 `127.0.0.1`。
2. **代码是"野生的"**：可能不可编译、缺依赖、混合编码(GBK/GB2312/UTF-8±BOM)、多 ISV 前缀。
   解析器**绝不依赖编译或依赖解析**。
3. **规模大**：成百上千文件 → 要性能、进度、缓存、增量重扫。
4. **信任优先**：覆盖率/可信度报告是**一等功能**，不是事后补。
5. **接手者视角**：第一需求是"这项目长什么样"，要先能出项目地图/理解报告。
6. **两段式解耦**：「确定性扫描器(建 KB)」与「AI 理解层(查 KB)」解耦，**KB 是契约**。

派生哲学：**处处置信度 + 证据行号 + unknown**——老项目分析天生不完整，宁可标 unknown 也不臆造。

## 已拍板关键决策

- **ISV / 前缀（影响阶段 2/3/4）**：桥接源码**只认元数据 `<ClassName>` 全限定名**（它一定等于源码包路径），**不靠 ISV/前缀去猜**（ISV 与代码包前缀常对不上）。
  - 阶段 2 解析元数据必须**完整保留 `<ClassName>`**（含包路径），不可只截类名。
  - 区分**两套前缀**：元数据标识前缀（`cqkd_`，管字段/实体，防串实体）与代码包前缀（`cqspb`，管模块归属）——分别建、不混。
  - 模块识别按**代码包路径前缀**聚类，不用 ISV；前缀由工具自动发现，仅作报告产物。

## 架构（两段式）

```
段一 本地确定性扫描器(Python)：Ingestion 摄取 → Metadata 解析 → Java 静态分析 → 桥接
                              → Cosmic KB(SQLite 图谱 + FTS5 + JSON 快照) → 可信度/理解报告
段二 AI 理解层(CLI/MCP)：NL→意图 → 查 KB 取证 → Context Builder → 挂苍穹 Skill → 带证据的解释
```

包结构 `cosmic_kb/`：`ingest / metadata / java / bridge / graph / semantic / context / report / cli`。
每个子包的 `__init__.py` 写了职责与计划模块，先读它再动手。

## 当前进度

- ✅ 阶段 0（脚手架 + 资产复用）、阶段 1（源码摄取 + 解析可信度报告）已完成并人工验收。
- ✅ 阶段 2（元数据解析 + 整包处理）：三类 dym 统一解析为 `MetaModel`、hex oid 模板回填、
  整包双层 zip；`cosmic_kb meta <dym|zip>`。
- ✅ **阶段 3（元数据 `<ClassName>` ↔ 源码桥接）已完成并人工验收**：
  `bridge/namespace.py`（源码 FQN 索引 + 前缀发现）+ `bridge/linker.py`（五态分类、孤儿
  收录并标常量类）+ `report/bridge_report.py`；`cosmic_kb bridge <源码根> <dym|zip>`；
  真实整包命中率 91.1%、孤儿 1075（常量 207 + 真孤儿 868）；59 passed。
- ✅ **阶段 2/3 增补（转换规则 + 转换插件桥接 + 插件基类孤儿）**：`.cr` 转换规则解析、转换插件
  桥接、插件基类孤儿闭包识别；图谱增 `convert_rule`/`converts_to`/`plugin_base`（schema v5）。
- ✅ **阶段 4（KB 图谱存储 + Web）已完成**：`graph/schema.sql`+`store.py`（SQLite+FTS5
  幂等重建）、`report/project_map.py`（多信号模块识别，已降为次要）、`report/overview.py`、
  本地 Web（`web/`）。
- ✅ **阶段 5+6（类内+跨类）+7（字段级排障引擎·旗舰）**（产品方向重定向：从「项目普查」转向
  「排障导航」，用户 2026-06-17 拍板）：输入字段标识 → 列出所有读/写它的**插件 + 事件函数 +
  是否落库 + 行号 + 源码路径**，按**实体坐标 (单据·层级·分录)** 分组定位。Java 语义层 `java/`
  （ast_index/constants/plugin_classifier/event_extractor/field_access/call_graph/persistence/
  project_graph/analyze），跨类回溯 + 数据包来源识别（事件入参/ORM load/转换/for-each/lambda/
  stream/DynamicObject 入参传播）+ 落库三态判定；未绑定苍穹插件（Task/WebApi）也作入口。
  KB 增 `field_access.access_class`（schema v7）；报告 `field_trace`/`bill_view`；CLI `trace`/`bill`。
- ✅ **信任优先·可信度报告（手段一 + 手段二）**（红线 #4）：手段一 `coverage`（元数据业务字段
  为分母算覆盖率 + 四维质量分解）；手段二 `scan-compare`（高精度 `field_access` vs 粗精度正则
  字面量，分桶：两者都见 / 仅粗扫见=疑似盲点 / 仅高精度见=精度增量）。KB 增 `coarse_field_hit`
  表（schema v8）；Web「扫描可信度」页签。
- ⬜ **阶段 8（业务流分析）拍板搁置**（2026-06-22）：不单独做，折进阶段 9 按需——业务流上下文
  只用现成 BOTP 边（`convert_rule`/`converts_to`），引用/审核回写无数据则留 unknown。
- ✅ **阶段 9（语义解析 + Context Builder + Skill 集成）**：自然语言提问 → 工具**确定性**解析成
  KB 查询并组装带证据答案（`ask` 不调 LLM，推理交段二 Skill）。`semantic/dictionary`（中文名↔标识
  词典 + 模糊候选）+ `resolver`（意图分类 + 低置信反问，不替用户拍板）+ `context/builder`（按意图
  复用取证函数组装证据包）；CLI `ask`（消歧退出码 3）。
- ✅ **阶段 10（MCP 封装·段二大模型接入）**：按红线 #6 走 MCP Server——`mcp/server.py`（FastMCP +
  取证工具，返回值与 CLI `--json` 同口径、零重写、未装 `[mcp]` 也可 import）；CLI `cosmic_kb mcp`
  + `cosmic_kb-mcp` 入口 + `.mcp.json`。取证走最小证据包 JSON；大模型亦可直接读本机源码全文做完整解释（红线 #1 放松后）。
- ✅ **阶段 10 增补·方法出向调用导航 `method_calls`**（2026-06-23 定位重置，取代原"方法级深读
  `read_method`"）：段二形态拍板为**大模型直接读本机源码 + 挂苍穹 skill**——复述源码 / 列平台·
  `equals`·常量调用 / 做自然语言解释，大模型自己做得更好，静态层在这块零增量甚至是噪声，故砍掉。
  确定性层只保留大模型猜不准的那件事——**野生多 ISV 前缀不可编译码上的"跳转到定义"**：给定 类
  全限定名 + 方法名 → 只回该方法调用的**项目内**方法（调用名 + 目标类全限定名 `target_fqn` +
  目标源文件 `target_relpath` + 调用行号），供大模型顺调用链逐层读源码下钻。**不回源码全文**（大
  模型自己读）、**不列平台/外部/落库 sink 调用**（噪声）、**不做字段落库取证**（那是 `trace` 的
  本职）。接收者类型解不出 → 不收录（宁缺毋滥）。schema 不变 v9。四入口：CLI `calls` + MCP 工具
  `method_calls` + `ask` 意图 `method_calls` + `trace`/`bill` 跳转提示。**当前 180 passed。**
- 详细进度与每阶段"背景/目标/验收结论/命令"见 `docs/阶段验收.md`。

## 常用命令（Windows / PowerShell）

```powershell
pip install -e ".[parse,encoding,dev,fuzzy,mcp]"  # 解析+编码+测试+模糊匹配+MCP（fuzzy/mcp 可选）
pytest -q                                # 跑测试（当前 178 passed）
cosmic_kb --version                      # 版本
cosmic_kb doctor                         # 资产体检（需 skill_assets/ok-cosmic-docs.db）
cosmic_kb ingest "<项目源码根>"          # 阶段1：摄取 + 覆盖率/可信度报告（--json 可留档）
cosmic_kb meta "<dym|cr 或整包 zip>"     # 阶段2：解析元数据(含转换规则 .cr)，分类计数/JSON 快照
cosmic_kb bridge "<项目源码根>" "<dym|zip|目录>"  # 阶段3：ClassName↔源码桥接报告（--json）
cosmic_kb build "<项目源码根>" "<dym|zip|目录>"   # 阶段4+5：建 KB（含字段级分析）
cosmic_kb trace "单据.字段|单据.分录.字段|单据.分录.子分录.字段"  # 旗舰：按层级精确定位字段→谁改了它/事件函数/是否落库（裸字段=列全部坐标）
cosmic_kb bill "<单据标识>"              # 单据钻取：操作集/插件/字段触达/风险
cosmic_kb calls "<类全限定名>" "<方法名>"        # 方法出向调用导航：该方法调了项目内哪些方法→目标类/源文件/行（供大模型顺调用链读源码下钻）；源码全文与"方法在干嘛"交给大模型直接读+苍穹 skill
cosmic_kb ask "<自然语言问题>"           # 阶段9：NL→意图→查 KB 取证（字段谁改的/单据钻取/插件解释/方法做了什么；消歧退出码3，--json 喂 Skill）
cosmic_kb coverage                       # 信任优先·手段一：字段覆盖率（元数据为分母）+ 扫描质量分解
cosmic_kb scan-compare                   # 信任优先·手段二：粗精度(源码字面量) vs 高精度(field_access)对比→疑似盲点/精度增量
cosmic_kb web                            # 本地浏览器排障（输字段→表格→跳源码；含「扫描可信度」页签：手段一+手段二）
cosmic_kb mcp                            # 阶段10：起 MCP 服务器，把取证命令暴露成 MCP 工具供 LLM 宿主调用（项目根 .mcp.json 自动识别）
```
> 若 `cosmic_kb` 脚本入口不可用，等价用 `python -m cosmic_kb.cli.main ...`。

## 编码与协作约定

- **对用户用简体中文回答**（用户偏好）。
- 代码注释/文档字符串用中文，风格与现有模块一致（务实、可解释，讲清"为什么这么做"）。
- 可选依赖分组放 `pyproject.toml` 的 optional-dependencies，避免一上来装一堆。
- 每个新功能配 `tests/` 测试；改完跑 `pytest -q` 确认不回归。
- **工作纪律：一个阶段 ≈ 一个会话。** 做完 → 写测试 → 用户人工验收 → 把"实现了什么 +
  验收结果"更新进 `docs/阶段验收.md` → git 提交 → 开新会话做下一阶段，保持上下文干净。
- 重要决策/架构取舍写进 `docs/`，不要只留在对话里。
