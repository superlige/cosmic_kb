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

1. **本地离线**：解决问题优先，如果接入大模型能解决问题，不强制要求离线代码不上传
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
- 🔧 **阶段 2/3 增补（转换规则 + 转换插件桥接 + 插件基类孤儿）已实现，待人工验收**：
  新元数据「转换规则」`.cr`（`ConvertRuleModel`，复用 `MetaModel`+`form_type='convert'`+
  `ConvertInfo` 承载单据上下游）；转换插件 `plugin_type='convert'` 走桥接；继承苍穹插件基类
  （传递闭包，含项目中间基类）的孤儿标 `role='plugin'`+`plugin_base`；图谱增 `convert_rule`
  表 / `converts_to` 边 / `source_class.plugin_base`（KB schema v5）；理解报告增「单据流转
  BOTP」与插件孤儿风险。样例 `samples/trans`（56 条）；`tests/test_convert.py`；90 passed。
- ✅ **阶段 4（KB 图谱存储 + Web）已完成**：`graph/schema.sql`+`store.py`（SQLite+FTS5
  幂等重建）、`report/project_map.py`（多信号模块识别，已降为次要）、`report/overview.py`、
  本地 Web（`web/`）。
- 🔧 **阶段 5+6（类内+跨类）+7（字段级排障引擎）首轮验收反馈已返工，待复验**（产品方向重定向：
  从「项目普查」转向「排障导航」，用户 2026-06-17 拍板）：
  - **旗舰能力**：输入字段标识 → 列出所有读/写该字段的**插件 + 事件函数 + 是否落库 + 行号 +
    源码路径**，消灭「把元数据里一堆插件全路径逐个复制到代码里翻」。
  - Java 语义层 `java/`：`ast_index`（AST 遍历）、`constants`（全局常量值表，解析常量引用的
    字段 key）、`plugin_classifier`+`event_extractor`（插件种类 + 事件方法领域表 + 落库相位）、
    `field_access`（两习语 + **DynamicObject 入参/局部识别**：`getModel().setValue` 按实参个数判
    表头/分录/子分录；DynamicObject 树形赋值靠方法内轻量数据流定层级+entry_key）、`call_graph`
    （类内调用链）、`project_graph`（**全项目类索引 + 跨类调用回溯**）、`persistence`（落库判定
    = 事件相位 × 操作类型 × 调用链到 sink）、`analyze`（两轮编排：插件跨类归因 + 全量孤立补全）。
  - **落库规则**：入库类操作（save/submit/audit）的事务内事件 setValue → 落库；donothing 需
    显式 save sink；界面 propertyChanged 仅内存。跨类解析不到的外部调用标 unresolved→unknown。
  - **两轮验收硬伤已修**：① 字段定位改为按**实体坐标 (单据·层级·分录)** 分组 + 定义坐标消歧
    + form/entry/level 过滤（不再按裸标识列所有插件）；② Web 重做（定宽表格防挤压、以实体为
    分组块）；③ service/工具类漏统计修复（跨类回溯归因 + 全量孤立补全 + DynamicObject 入参识别）；
    ④ **数据包来源实体识别**——`field_access` 追每个数据包来源：事件入参=绑定单据/转换目标源单、
    ORM `BusinessDataServiceHelper.load("实体",…)`=实参实体、for-each 循环变量继承来源；字段读写
    `form_key=数据包来源实体`（判不出=未定位，不臆造），并按「实参↔形参」跨方法/跨类传播来源实体
    （修复 load 别的实体 + for 循环写字段被漏检/归错单据，如 `cqkd_collateralstatus`）。识别覆盖
    事件入参(单包/数组)、ORM load/query、转换、getEntryEntity、for-each、**lambda `forEach(o->…)`**、
    stream 派生集合；同插件多操作绑定的重复归因按完整坐标去重。真实项目 `asset_management_sys`
    复验 `cqkd_collateralstatus`：service 的 lambda 写入、op 的数组循环写入均正确归到来源实体。
  - KB 增 `operation`/`plugin_method`/`field_access`(含 `access_class`) 表（**schema v7**）；报告
    `report/field_trace.py`（旗舰，坐标分组）+`bill_view.py`（字段触达按实体）；Web 重做单页。
  - **查询入口按层级显式录入**（点号段数定层级）：`单据.字段`(表头)/`单据.分录.字段`/
    `单据.分录.子分录.字段`；精确命中该坐标，层级/分录判不准的写入进「可能命中(存疑)」桶、
    来源判不出的进「未定位」——绝不遗漏。裸字段=列全部定义坐标(发现态)。
  - **未绑定元数据的苍穹插件也作跨类入口**：`AbstractTask`(调度)、WebApi、工作流等从其
    入口/根方法跨类回溯，数据包来源全靠 ORM + 全坐标传播（实参↔形参传 层级/分录/来源）；
    凡 `BusinessDataServiceHelper.load/loadSingle/loadFromCache/query/queryOne` 取的包都识别来源。
  - CLI：`cosmic_kb trace 单据.[分录.[子分录.]]字段`（仍可 `--form/--entry/--level` 覆盖）、
    `cosmic_kb bill <单据>`；`build` 已接入 Java 分析。
  - `tests/test_java_field.py` + 更新 graph/web/report 测试；**113 passed**。
- 🔧 **信任优先·可信度报告（手段一 + 手段二）待人工验收**（红线 #4「覆盖率/可信度是一等功能」）：
  - **手段一** `report/coverage.py`：以**元数据业务字段为分母**算字段覆盖率（被读/写=分子）+
    四维质量分解（标识解析/来源定位/落库判定/命中元数据）；`cosmic_kb coverage`。
  - **手段二** `report/scan_compare.py`：**粗精度 vs 高精度对比**——高精度=`field_access`
    （AST+跨类+落库），粗精度=纯正则把字段标识当**源码字面量**搜出来（`coarse_field_hit` 表，
    建库期算）。一比得：**两者都见**（互证）/**仅粗扫见=疑似盲点**（强信号=字面量作 get/set
    首参的 rw-idiom，弱信号=多为常量定义/注释）/**仅高精度见=精度增量**（常量解析触达，纯
    grep 抓不到）。verdict 以强信号盲点定红绿灯，诚实声明「盲点是候选非确诊」。`cosmic_kb
    scan-compare`。KB 增 `coarse_field_hit` 表（**schema v8**）；Web「扫描可信度」页签接续手段二。
    真实库复验：粗扫命中 2376 / 高精度 1547，强信号盲点 11（带 rw-idiom 源码行号）、弱信号 818、
    高精度独有 0（常量定义里也含字面量）。`tests/test_scan_compare.py`(6)+web 端点；**130 passed**。
- ⬜ **阶段 8（业务流分析）拍板搁置**（2026-06-22）：不单独做，折进阶段 9 按需——业务流上下文
  只用现成 BOTP 边（`convert_rule`/`converts_to`），引用/审核回写无数据则留 unknown。
- 🔧 **阶段 9（语义解析 + Context Builder + Skill 集成）待人工验收**：让接手者用自然语言提问，
  工具**确定性**地解析成 KB 查询并组装带证据的答案（`ask` 不调 LLM，LLM 推理交段二 Skill）。
  - `semantic/dictionary.py`（KB 中文名↔标识双向词典 + RapidFuzz 模糊候选，未装降级 difflib 子串）
    + `semantic/resolver.py`（关键词意图分类：旗舰 `field_who_changed`/`bill_drilldown`/
    `plugin_explain`/`operation_explain` + 复用 `parse_locator` 点号坐标 + **低置信反问候选菜单**，
    同名歧义绝不替用户拍板）+ `context/builder.py`（按意图**复用** `field_trace`/`bill_view` + 插件/
    操作薄查询组装证据包，dict+文本双输出，判不出标 unknown）。
  - CLI `cosmic_kb ask "<问题>"`（消歧退出码 3）；`fuzzy` 可选依赖（rapidfuzz）；`SKILL.md` 更新为
    首选 `cosmic_kb` 命令取证。真实库复验：`ask` 旗舰与 `trace` 同口径，候选按具体度排序。
    `tests/test_semantic.py`(14)+`tests/test_context.py`(6)+合成 KB 夹具；**153 passed**。
- 🔧 **阶段 10（MCP 封装·段二大模型接入）待人工验收**：按红线 #6 走 **MCP Server** 路径——不在
  `cosmic_kb` 内调 LLM，只把取证命令包成 MCP 工具交 LLM 宿主（Claude Code/Desktop）调用，整库源码
  不出本机、每次只传最小证据包。
  - `cosmic_kb/mcp/server.py`：FastMCP + 五工具 `ask`/`trace`/`bill`/`coverage`/`scan_compare`，
    **返回值与 CLI `--json` 完全同口径**（复用 resolver+builder+report.*，零重写）；纯逻辑与 mcp
    包装分离，未装 `[mcp]` 也可 import。CLI `cosmic_kb mcp`（stdio）+ `cosmic_kb-mcp` 入口 +
    `[mcp]` 可选依赖 + 项目根 `.mcp.json`；`SKILL.md` 补「MCP 工具」节。
  - 顺带修复 `report/bill_view.py` 单据钻取漏 `field_key IS NOT NULL` 过滤的崩溃（NULL key 未定位
    访问塌进 None 键 → 格式化报错）。`tests/test_mcp.py`(10)；**163 passed**。
- 详细进度与每阶段"实现了什么 + 验收记录"见 `docs/阶段验收.md`。

## 常用命令（Windows / PowerShell）

```powershell
pip install -e ".[parse,encoding,dev,fuzzy,mcp]"  # 解析+编码+测试+模糊匹配+MCP（fuzzy/mcp 可选）
pytest -q                                # 跑测试（当前 163 passed）
cosmic_kb --version                      # 版本
cosmic_kb doctor                         # 资产体检（需 skill_assets/ok-cosmic-docs.db）
cosmic_kb ingest "<项目源码根>"          # 阶段1：摄取 + 覆盖率/可信度报告（--json 可留档）
cosmic_kb meta "<dym|cr 或整包 zip>"     # 阶段2：解析元数据(含转换规则 .cr)，分类计数/JSON 快照
cosmic_kb bridge "<项目源码根>" "<dym|zip|目录>"  # 阶段3：ClassName↔源码桥接报告（--json）
cosmic_kb build "<项目源码根>" "<dym|zip|目录>"   # 阶段4+5：建 KB（含字段级分析）
cosmic_kb trace "单据.字段|单据.分录.字段|单据.分录.子分录.字段"  # 旗舰：按层级精确定位字段→谁改了它/事件函数/是否落库（裸字段=列全部坐标）
cosmic_kb bill "<单据标识>"              # 单据钻取：操作集/插件/字段触达/风险
cosmic_kb ask "<自然语言问题>"           # 阶段9：NL→意图→查 KB 取证（字段谁改的/单据钻取/插件解释；消歧退出码3，--json 喂 Skill）
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

## 分工（计划里的约定）

- **Claude**：摄取/桥接/图谱/路径追踪/业务流/Context —— 跨模块、需架构判断的硬骨头。
- **Codex**：编码探测器、事件识别规则、正则兜底、单测样例、references 整理 —— 边界清晰可独立验证的任务。
