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
- ✅ **阶段 10 增补·unknown 字段诚实分类 + 动态写入交段二读源码**（2026-06-24 拍板）：字段 key 钉不出的
  写入（`field_access.field_key=None`，对 `trace` 隐形）按成因细分 `key_resolution`（`dynamic-loop`/`concat`
  /`external-const`/`unknown`）——绝大多数是代码对运行时/配置/元数据决定的**动态字段集泛化写入**，静态钉
  不出唯一字段，故确定性层**不解释/不展开/不调大模型**，只分类+收集导航证据，"碰哪些字段、是否含 X"交
  段二大模型直接读源码（红线 #1/#4/#6）。`trace X` 折进「动态写入候选」段（按同单据限定范围）、全局
  `dynwrites`（CLI + MCP `dynamic_writes`，form/cause/class 过滤）；二者一律**按 (入口类,事件方法) 去重成
  「该读方法」清单**（cap 10 + calls 锚点）防上下文爆炸。schema 不变 v9。**当前 191 passed。**
- ✅ **阶段 10 增补·字段名核对 `resolve_fields`**（2026-06-25 拍板）：段二大模型读源码靠**命名惯例猜
  字段中文名**翻车（`cqkd_zjjnqk` 猜成"资金"真实是"租金"），现有 `trace`/`bill`/`ask` 太"重"模型不愿
  为核一个名去调。新增 O(1) 轻量取证：标识 → 真实元数据中文名+实体坐标，**同时覆盖 `field`+`entity`
  两表**（分录容器 key 也能解析），同 key 多坐标全摆出不替选，**钉不出回 `null` 不臆造**。复用词典层
  `Lexicon`（补 `entities_by_key` 索引），纯逻辑 `report/resolve_fields.py`，零 schema 改动。配套 MCP
  `INSTRUCTIONS` 加「字段名纪律」。四入口：CLI `resolve` + MCP `resolve_fields`（+ 已有 trace/bill/ask
  仍可查名）。schema 不变 v9。**当前 201 passed。**
  - 配套修复：`entity.parent_key` 曾误存父实体 **oid**（`1B+5Q7IXAJGI`），现 `store.py` 用本单据 oid→key
    映射翻成父实体 **key**（`resolve_fields`/`bill` 不再泄漏 oid）；需 `cosmic_kb build` 重建 KB 生效。
- ✅ **阶段 10 增补·模式 B 语义增强（把核对结果焊进工具返回值）**（2026-06-25 拍板）：真实样本显示段二
  大模型**绕过** `resolve_fields`/`cosmic_semantics`，凭命名惯例猜字段名、凭训练知识臆断事件触发时机/
  入库——`INSTRUCTIONS` 软约束（规则在场、模型知道）压不住自信先验。诊断：模型"读源码→直接答"的默认
  回路一个取证工具都不碰，约束施加的位置不对。通用 host 无钩子，**唯一所有 MCP host 都必读的硬信息是
  我们自己工具的返回值**，故不"强制调用"（对自主 agent 不可达），改为在它本来就要走的导航工具返回里
  **内联**：① 字段旁 `field_name` 已核对中文名（trace 顶层 + bill `field_touch` + ask 插件/操作证据，
  覆盖 field+entity 两表，钉不出留 None）；② 事件方法旁 `semantics_topic`（`propertyChanged`→plugin-form、
  `beforeDoOperation`→plugin-operation…），提示"判触发时机/入库前先 cosmic_semantics(topic)"。新增中立
  复用模块 `semantic/hints.py`（事件→主题映射 + 字段名索引），report/context/method_calls 复用、不反向依赖
  mcp；`INSTRUCTIONS` 加「返回值已带证据，直接采用」。host-agnostic，零 schema 改动（v9）。
- ✅ **阶段 10 增补·堵两条"读源码猜字段名"路径：method_calls 带字段 + read_source（模式 A）**（2026-06-25）：
  第二次真实样本暴露模式 B 的覆盖天花板——模型走「`method_calls`（不带字段名）→ 宿主原生 reader 直接读
  源码 → 字段 key 只在源码正文里 → 猜」，全程没流经带名的工具。两手补齐：① **method_calls 延伸模式 B**——
  按方法 `start_line..end_line` 行范围圈 `field_access`，返回该方法读写字段 + 已核对名 + 是否落库 + 语义
  路由（导航到方法就拿真名）；钉不出的动态写只计数。② **新增 `read_source`（模式 A）**——让模型读源码走
  我们的工具：野生编码（GBK/GB2312/UTF-8±BOM）正确解码（原生 reader 易乱码=拉它过来的硬理由）+ 扫文本里
  已知字段 key 自动标注真名（常量 `KEY_X="cqkd_x"` 的字面值就在源码，无需常量表，直接复用 `resolve_fields`）；
  路径防越界。新增 `report/source_read.py`（源码读取公共件，method_calls 委托复用）+ `report/read_source.py`；
  四入口 CLI `source` + MCP `read_source` + `INSTRUCTIONS`「读源码优先用 read_source」+ method_calls 字段块。
  零 schema 改动（v9）。**天花板仍在（诚实）：read_source 是"引导非强制"——模型若坚持用原生 reader、连
  read_source 都不调，host-agnostic 拦不住；能做的是把"正确解码+自带真名+行号对齐"做成它用我们 reader 的
  硬理由。当前 220 passed。**
- ✅ **阶段 10 增补·read_source 字段名三档置信消歧**（2026-06-26 拍板）：真实样本暴露 read_source 把同一
  `<isv>_xxx` key 在**多张单据**的同名候选**全平铺**返回（名字可能不同），诱导段二模型脑补归属（严重误导）。
  诊断：read_source 明明知道"在读哪个文件"，却没用本文件的解析上下文收敛，直接倒 `resolve_fields` 全候选。
  修复（仅改 read_source，其余工具早已用 `FieldNames.get(key, form_key)` 按数据包来源取名、本无此病）：用本
  文件 `field_access.form_key`（=数据包**实际来源实体**，经 ORM load/事件入参/跨实体传播解析，`analyze.py` 里
  `form_key=acc.entity`）收敛同名候选，按**三档置信**标注——✅`unique`（元数据唯一）/ ✅`resolved`（本文件已
  解析到具体单据，含 `loadSingle` 跨实体的情形，附依据行号 + 标明其余单据）/ ⚠️`ambiguous`（多单据同名又没
  解析到实体 → 显式标歧义、列候选、指出消歧方向「看接收变量来源 dataEntity/loadSingle/getAllSonList」，绝不
  替选、不默认当前单据）。`field_names` 形状改为 `{key:{tier,names,coordinates,note}|None}`；MCP `INSTRUCTIONS`
  /工具说明加「ambiguous 别默认当前单据」。零 schema 改动（v9）。**当前 222 passed。**
- ✅ **信任优先·form_key 解析率提升（绑定回落 + 泛型集合建模）**（2026-06-27）：`read_source` 同名跨单据消歧靠
  `field_access.form_key` 收敛，None 时只能平铺候选诱导脑补。真实库 60.3% 写入 form_key=None，按 evidence 分桶挖到
  两个**可救**根因并补断链（红线 #4 不臆造）：① **绑定回落**——已绑定插件里未被事件 BFS 覆盖的 helper（第②轮孤立
  补全）用本类**唯一绑定单据**作来源（绑多张留 None）；② **泛型集合建模**——`List/Set/Collection<DynamicObject>`
  （项目里传查询结果最常用、原本三种 DO 形参都不认、整链来源 None）建模成集合：形参走「实参↔形参」坐标传播、
  局部走「空集合 + `.add(已知实体包)` 累积推断元素来源」轻量数据流。`ast_index` 加 `dynamicobject_collection_*`、
  `field_access._Env` 加 `do_coll_vars` + `.add` 累积、`analyze._coll_params` 并入 List<DO> 形参。零 schema 改动（v9）。
  真实库 form_key NULL **60.3%→56.1%**（救回 764 行，0 改写、99%+ 落确认实体）。**当前 227 passed。**
- ✅ **信任优先·form_key 解析率再提升（字段key反查回填三层 + addNew 习语，schema v10）**（2026-06-27）：数据流追不到
  DO 来源时，反过来用**被读写的字段 key** 问元数据反推来源实体——物理硬约束「DO 不可能 `.set("cqkd_xxx")` 除非其实体
  声明了该 key」，对返回值/Map/helper/new/stream 等容器断链**免疫**。`analyze.py` `_field_form_index`+`_backfill_form_key`
  在 `_dedup` 前对 form_key=None 行三层逐级塌缩：①字段key唯一反查（直接定 form_key+level+entry_key）②绑定收敛（与
  access_class/入口插件绑定单据取交）③同对象共现交集（同接收者变量连写多字段，候选 form 取交集；为此 `field_access`
  do.* 路径记 `receiver_var`）；仍解不出留 None（红线 #4）。待办二：`field_access` 加 `new DynamicObject(coll.getDynamicObjectType())`
  习语（新行继承集合分录坐标）。新增 schema **v10** 列 `form_key_source`（data_flow/metadata_unique·binding·cooccur/NULL）
  诚实区分元数据反推与数据流证明；`read_source` 对 metadata_* 来源注明「依据是字段归属、非数据流行号」。真实库 form_key
  NULL **56.1%→34.6%**（write 52.1%→27.4%；回填 4153 行=唯一2167+绑定1234+共现752，0 改写）。**当前 240 passed。**
  详见 `docs/form_key解析待办.md`（②反向调用图、③④诚实未知留作后续）。
- ✅ **信任优先·trace 防 MCP 截断（写/读拆分 + 按类合并 + 字节 governor）**（2026-06-27）：段二经 MCP 调 `trace`
  返回被宿主在 **32KB 硬上限**处从中间截断（真实样本 67879→32768）。先一轮"删 evidence 死重列 + 白名单投影 + readers
  折叠成「该读方法」+ 各数组 cap"仍 67KB——根因是**数组条数本身无界**（坐标组 + unlocated/possible/coarse/dynamic 都
  无界），行级 cap 管不住。MCP 改走紧凑投影 `field_trace.trace_compact`：① **写/读拆分**（`access` 参数 write/read/默认，
  每次只返一半）；② **按类合并**（散落行/方法按 `access_class or plugin_fqn` 塌成有界类节点，`_merge_writers_by_class`
  类→sites、`_merge_readers_by_class` 类→方法、`_readers_overview` 仅类+计数；每行重复的插件常量提到类节点只存一份）；
  ③ **cap + 字节 governor**（cap 类节点 + 按 `json.dumps` 字节预算 28000 逐级收紧重建直至 ≤ budget，真实总数恒在
  `summary`、截掉量在 `capped`/`sites_capped`/`methods_capped`，红线 #4 不丢数）。复用红线 #6 抽 `_collect_materials`
  共享取数，富 `field_trace()` 输出**逐字节不变**（现有测试不改即过）；`tool_ask` 字段意图 evidence 同样换 compact
  （堵 ask 截断，复用 rq 不动 builder/CLI）；CLI 文本/本地 Web（HTTP 无限制）不动。真实库 `cqkd_ht.cqkd_zdgl.cqkd_qs`
  富 dict 51456B→compact 默认 22761/write 20217/read 23182B（全 < 32KB）。零 schema 改动（v10）。注：MCP server 常驻，
  改源码需**重连/重启 MCP** 才生效。**当前 253 passed。**
- ✅ **信任优先·提高字段扫描率（模型形参识别 + 内联集合链，1+2+3）**（2026-06-27）：比 form_key=None 更糟的一类是
  **写入根本没被扫出**（access 记录都不产生、查询时彻底隐形）。三条边界清晰、高收益、低误报的确定性缺口，全落在
  `field_access.py` 轻量数据流：① **IDataModel/IBillModel/IFormView 形参识别为模型上下文**——helper
  `void calc(IDataModel model){ model.setValue(...) }` 的写入原被整条丢弃（`_is_model_receiver` 只认 getModel() 结尾/已知
  model_vars，形参永不入集）。`analyze._model_params` 按 `_MODEL_TYPES` 抽形参名注入 `_Env.model_params`，`_build_contexts`
  播种 `model_vars`；模型 API 来源改走 `_model_entity`（跨类 service 收 getModel()/getView() 定到绑定单据，插件自身回落
  default_entity 不变；仅类型白名单入集、不靠变量名猜=低误报）。② **内联 `X.getDynamicObjectCollection("k").addNew()`
  赋给 DynamicObject 局部**——原 `_GET_COLL_ARG_RE` 先命中把新行误当集合 → 后续 `row.set` 整片判不出；新增内联守卫
  （仿 stream 前置）+ 共享闭包 `_inline_coll_elem` 解析元素行坐标。③ **内联 `…getDynamicObjectCollection("k").forEach(o->o.set(..))`
  / `.stream()` lambda**——`_lambda_recv` 回传接收者原文，binding 处复用 `_inline_coll_elem` 兜底（owner 解不出则 entity=None,
  红线 #4）。零 schema 改动（v10）。真实库 field_access **19399→20104（+705：写 +402/读 +303，此前完全不可见）**，其中 via
  model.* +429（C1）、内联 do.* +276（C2/C3）；form_key NULL **率不升反降 34.6%→34.4%、写 27.4%→26.7%**（新行多数当场定到来源,
  不进未知堆）。`addNew`/`new DynamicObject` 的**变量形式**早由上一轮覆盖，本轮专补**内联链**。**当前 264 passed。**
- 详细进度与每阶段"背景/目标/验收结论/命令"见 `docs/阶段验收.md`。

## 常用命令（Windows / PowerShell）

```powershell
pip install -e ".[parse,encoding,dev,fuzzy,mcp]"  # 解析+编码+测试+模糊匹配+MCP（fuzzy/mcp 可选）
pytest -q                                # 跑测试（当前 264 passed）
cosmic_kb --version                      # 版本
cosmic_kb doctor                         # 资产体检（需 skill_assets/ok-cosmic-docs.db）
cosmic_kb ingest "<项目源码根>"          # 阶段1：摄取 + 覆盖率/可信度报告（--json 可留档）
cosmic_kb meta "<dym|cr 或整包 zip>"     # 阶段2：解析元数据(含转换规则 .cr)，分类计数/JSON 快照
cosmic_kb bridge "<项目源码根>" "<dym|zip|目录>"  # 阶段3：ClassName↔源码桥接报告（--json）
cosmic_kb build "<项目源码根>" "<dym|zip|目录>"   # 阶段4+5：建 KB（含字段级分析）
cosmic_kb trace "单据.字段|单据.分录.字段|单据.分录.子分录.字段"  # 旗舰：按层级精确定位字段→谁改了它/事件函数/是否落库（裸字段=列全部坐标）
cosmic_kb bill "<单据标识>"              # 单据钻取：操作集/插件/字段触达/风险
cosmic_kb calls "<类全限定名>" "<方法名>"        # 方法出向调用导航：该方法调了项目内哪些方法→目标类/源文件/行；并附该方法读写字段+已核对中文名（模式B，引用照抄勿猜）；源码全文与"方法在干嘛"交给大模型直接读+苍穹 skill
cosmic_kb source "<相对源码根的源文件路径>"      # 模式A：读源码（野生编码正确解码）+ 自动标注其中字段 key 真实中文名（--lines A-B 读区间）；同名跨单据按本文件数据包来源三档消歧（unique/resolved/⚠️ambiguous 别默认当前单据）；让大模型读源码走本工具而非宿主原生 reader（防乱码+防按拼音猜字段名）
cosmic_kb ask "<自然语言问题>"           # 阶段9：NL→意图→查 KB 取证（字段谁改的/单据钻取/插件解释/方法做了什么；消歧退出码3，--json 喂 Skill）
cosmic_kb coverage                       # 信任优先·手段一：字段覆盖率（元数据为分母）+ 扫描质量分解
cosmic_kb scan-compare                   # 信任优先·手段二：粗精度(源码字面量) vs 高精度(field_access)对比→疑似盲点/精度增量
cosmic_kb dynwrites [--form/--cause/--cls] # 信任优先：字段 key 钉不出的读写（动态循环/拼接/外部常量/歧义/未识别）按「该读方法」去重列出，交段二大模型读源码定性（防爆上下文）
cosmic_kb resolve "<字段/分录标识> ..."   # 字段名核对：标识→真实元数据中文名+坐标（O(1) 打词典，比 trace 便宜；分录容器/同 key 多坐标全摆出，钉不出回 null，防大模型按命名惯例臆断字段名）
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
