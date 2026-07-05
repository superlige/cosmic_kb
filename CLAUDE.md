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

> 一行一里程碑。**每条的背景/目标/验收结论/实现细节都在 `docs/阶段验收.md` 台账**，本节只够回到状态。
> 当前 schema **v13**，**460 passed, 4 skipped**。产品方向 2026-06-17 重定向：从「项目普查」转向「字段级排障导航」。

- ✅ **阶段 0–2**：脚手架 + 资产复用；源码摄取 + 解析可信度报告；元数据解析（三类 dym→`MetaModel`、hex oid 回填、整包双层 zip）。
- ✅ **阶段 3（+2/3 增补）**：元数据 `<ClassName>` ↔ 源码桥接（FQN 索引 + 前缀发现 + 五态分类 + 孤儿/常量类）；`.cr` 转换规则、转换插件桥接、插件基类孤儿（schema v5）。
- ✅ **阶段 4**：KB 图谱存储（SQLite+FTS5 幂等重建）+ `overview`/`project_map` + 本地 Web。
- ✅ **阶段 5+6+7（旗舰·字段级排障引擎）**：输入字段 → 列出所有读/写它的插件 + 事件函数 + 是否落库 + 行号 + 源码路径，按实体坐标 (单据·层级·分录) 分组。Java 语义层 `java/`（跨类回溯 + 数据包来源识别 + 落库三态）；schema v7；CLI `trace`/`bill`。
- ✅ **信任优先·可信度报告**（红线 #4）：`coverage`（元数据为分母算覆盖率 + 质量分解）+ `scan-compare`（粗扫 vs 高精度→盲点/精度增量）；schema v8；Web「扫描可信度」页签。
- ⬜ **阶段 8（业务流）拍板搁置**（2026-06-22）：折进阶段 9 按需，只用现成 BOTP 边，无数据留 unknown。
- ✅ **阶段 9（语义解析 + ask）**：NL → 意图 → **确定性**查 KB 组装证据包（`ask` 不调 LLM，推理交段二 Skill）；消歧退出码 3。
- ✅ **阶段 10（MCP 封装·段二接入）**：MCP Server 把取证命令暴露成工具（返回值与 CLI `--json` 同口径）；段二大模型直接读本机源码全文 + 挂苍穹 Skill（红线 #1 放松后）。schema v9。

  段二接入打磨（一系列增补，主线=**堵"大模型按命名惯例猜字段名/臆断语义"**与**form_key 识别率**）：
  - `method_calls`（2026-06-23）：野生不可编译码上的「跳转到定义」——给类+方法 → 只回项目内被调方法（目标类/源文件/行）；源码全文与"在干嘛"交大模型读。
  - `dynwrites` + 动态写入分类（2026-06-24）：字段 key 钉不出的写入按 `key_resolution` 细分，折叠成「该读方法」清单交段二读源码定性。
  - `resolve_fields`（2026-06-25）：O(1) 字段名核对（标识→真实中文名+坐标，覆盖 field+entity 两表，钉不出回 null）。
  - 模式 B 语义增强（2026-06-25）：把已核对 `field_name` + 事件 `semantics_topic` **焊进导航工具返回值**（host 无钩子，唯一必读的硬信息是工具返回值）；`semantic/hints.py`。
  - `read_source`（模式 A，2026-06-25）+ 三档置信消歧（2026-06-26）：读源码走我们的工具（野生编码正确解码 + 自动标注字段名）；同名跨单据按本文件 `form_key` 收敛为 unique/resolved/⚠️ambiguous，绝不默认当前单据。
  - **MCP 工具面精简 10→7**（2026-06-28）：审计工具（coverage/scan-compare/dynwrites）下沉 CLI-only，段二只留 7 个排障主路径工具。

  form_key 识别率系列（信任优先，真实库 NULL **60.3%→34.07%**，0 改写、不臆造，详见 `docs/数据包来源与form_key解析合并.md`）：
  - 绑定回落 + 泛型集合建模（→56.1%）；字段 key 反查元数据三层回填 + addNew 习语（→34.6%，schema **v10** 加 `form_key_source` 诚实区分数据流证明/元数据反推）。
  - 提高字段扫描率（模型形参识别 + 内联集合链）：补回此前**完全扫不出**的写入 +705 行。
  - 孤立方法反向调用图回填（`reverse_callgraph`，固定点传播）：回填 218 条。**结论：红线内已无安全的高收益 form_key 提升空间。**
  - **trace 防 MCP 32KB 截断**（2026-06-27）：MCP 走紧凑投影 `trace_compact`（写/读拆分 + 按类合并 + 字节 governor + 游标分页），真实总数恒在 summary、被 cap 量可翻页取回（红线 #4）。
  - **null_reason 落库 + 暴露**（2026-06-28，schema **v11**，当前转向）：给每条 `form_key=None` 打互斥成因码（`java/null_reason.py`，8 码：2026-06-29 把基础资料「写」从 `basedata-ref` 拆出 `basedata-write-suspect`——苍穹不会取基础资料再 save，写到基础资料即扫描误绑、应继续追，不再标"正确 None"），告诉段二/人「这行为何 None、该不该追」；暴露在 trace/coverage/web/MCP。**未改任何 form_key 判定逻辑。**
  - **`read_source` 限定常量引用解析**（2026-07-03，schema **v13**）：真实翻车——`TemporaryStopCon.ENTITY` 字面值不出现在被分析文件正文里，`read_source` 扫不到，模型凭常量英文名猜成"临停单"（真实"临时收入"）。全工程常量定义（`java/constants.py:ConstantTable.records`，含源文件+行号）持久化进新表 `java_constant`；`read_source` 再扫一遍 `类.常量` 限定引用查表解回字面值、按常规三档标注（`field_names` 挂在该表达式本身，带 `resolved_constant`）；解不出静默跳过，同名类多处定义出不同字面值标 ambiguous 不擅自选一个。
  - **`read_source` 常量条目截断优先级修复**（2026-07-03 复盘二）：上条上线后真实大文件（394 行、命中 58 个 key）仍复现"常量解析失败"——根因不在解析本身，而在**紧凑投影的截断顺序**：`field_names` 字典先塞普通字面量 key（按字母序）、常量条目**追加在最后**，大文件里普通 key 一多，MCP 32KB 预算耗尽在常量条目之前，导致解出来的常量标注被静默截断在 `field_names_next_cursor` 翻页线之外，模型看不到只能又退回猜。修复：`report/read_source.py` 把常量条目挪到 `field_names` 字典**最前**（`{**const_field_names, **plain_field_names}`），只要预算 ladder 选中的 cap ≥ 常量条目数（几乎总成立，常量引用天然少）就不会被截断；`tests/test_read_source.py` 补两条测试锁住顺序 + 极紧预算下常量条目仍存活。**这类"数据算对了但被截断顺序坑没"的 bug，日后排查 read_source/trace 类"工具返回不完整"问题时先看截断/分页选取逻辑，而非只查解析算法本身。**
  - **`read_source` 表单标识兜底**（2026-07-04 复盘三）：同一份真实大文件复测又暴露另一处遗漏——`BusinessDataServiceHelper.load("cqkd_invoic_apply", ...)` 里的字面量是**表单标识**（`form.key`），当该单据表头实体 key 不等于表单 key（常态）时，`read_source` 的 `_known_keys` 只查 `field`/`entity` 两表，这个 token 连"候选"都进不去，模型只能凭标识片段谐音瞎猜（`cqkd_invoic_apply`→"开票申请"、`cqkd_contractbill`→"合同账单"，均属臆造）。修复：`_known_keys` 把 `form.key` 也纳入扫描候选；字段/容器分类钉不出时新增 `_classify_form_key` 兜底查 `form` 表，命中标 `coordinates[].kind="form"`（同 key 多义则 ambiguous、不擅自选一个）。`tests/test_read_source.py` 补三条测试（表单兜底命中/表单多义歧义/字段优先于表单不被覆盖）。**教训：本条与「复盘二」外观相同（用户看到的都是"字段名没标出来"）但根因不同——复盘二是候选进了字典却被截断顺序挤掉，本条是候选压根没进 `_known_keys` 的扫描范围；排查前先确认是"扫描阶段漏了这张表"还是"扫到了却在分类/截断阶段丢了"，别一上来就假设是同一类根因。**
  - **`read_source` 游标翻页强制提醒**（2026-07-05 复盘四）：同一份真实样本第四轮实测——这次数据本身是对的（`cqkd_zkd`「转款单」已在 `field_names` 里标好了），但模型只翻了 `content_next_cursor`、没翻 `field_names_next_cursor`，凭 `load("cqkd_zkd")` 的变量名 `srctransid` 谐音猜成"转账单"。根因不是代码/数据 bug，是**工具描述的信息架构分散了模型注意力**：`content` 游标的翻页步骤给了完整因果链（游标值→怎么传→返回什么），`field_names` 游标只有"同法翻页"四字、没有等价的因果链被激活；警戒措辞又是"非 null 时不能下覆盖性结论"这种否定式约束，容易被"少调用"倾向覆盖；表单标识也在 `field_names` 里这条信息又离游标说明很远。修复不是加更多文字，是把行动指令直接焊进**返回值本体**：`field_names_next_cursor`/`content_next_cursor` 非 null 时各自伴随一个 `*_next_cursor_action` 字段给出具体该做什么（含真实翻车案例复述）；翻页返回的 `page.reminder` 在还有下一页时同样携带提醒，翻到 `next_cursor=null` 才消失；`_RS_COMPACT_NOTE`/MCP `tool_read_source` 描述改写为「① content ② field_names ③ 都读全才能下结论」的编号行动清单，弱化长段落、强化肯定式指令。`tests/test_read_source.py` 补四条测试（field_names 游标带 action 提醒/field_names 翻页 page.reminder 到末页消失/content 游标带 action 提醒/content 翻页 page.reminder 到末页消失）。**教训：前三轮复盘都是"数据/候选池/截断顺序"这类工具内部逻辑 bug，本轮是工具内部逻辑全对、但返回值的"信息呈现方式"没能让模型在正确的时刻执行正确的动作——排查"模型仍在猜"类问题时，先确认数据是否已经在返回值里（本轮确认过，在），若数据已在但模型没用上，要从"模型会怎么读这份返回值"的角度改工具描述/返回结构，而不是继续找数据层的 bug。**
  - **MCP 描述精简**（2026-07-05 复盘四·续）：复盘四验证结果正确后，用户指出 `INSTRUCTIONS`/各工具
    docstring（MCP 协议里随工具清单常驻发给模型，不是按需才出现）本身已经堆得很长，尤其是每条纪律都
    配一段真实翻车案例复述，长期占上下文。**关键区分**：`INSTRUCTIONS`/docstring 是**静态**的，每次
    工具列表都会带上；复盘四加的 `*_next_cursor_action`/`page.reminder` 是**动态**的，只在游标非 null
    这个真正相关的时刻才出现在返回值里——前者该精简，后者该保留（只是同样删掉了里面的叙事性例子）。
    改法：`INSTRUCTIONS` 从六段大长文改写为 7 条一行纪律（先取证/路由/read_source/分页纪律/字段名纪律/
    语义强制查/细节见工具自身），删去所有举例（如"`zjjnqk` 是租金还是资金"）；`server.py` 七个
    `tool_*` docstring 逐个只留"是什么/何时用/禁止什么"，砍掉机制铺陈和场景叙事（`INSTRUCTIONS` 从
    ~1500 字压到 701 字）；`read_source.py` 的 `_RS_COMPACT_NOTE`/`*_next_cursor_action`/`page.reminder`
    同步删掉"cqkd_zkd 猜成转账单"这类叙事例子，只留祈使句指令。**未改任何取证逻辑**，纯文本精简，
    `tests/test_read_source.py` 里断言了固定关键词（"必须"/"禁止"）的用例逐字核对过仍成立，
    `pytest -q` 全量仍 **460 passed, 4 skipped**（无新增/删减测试）。
  - **`read_source` 两步取证协议**（2026-07-04）：用户实测反馈返回"太臃肿、token 消耗大"——根因是
    默认对正文做**全文盲扫**（token 与 KB 全量已知 key 取交集，常见字段 key 是 `remark`/`amount`
    这类通用英文词，撞上大量无关局部变量，噪音不可控，只有模型自己读得懂代码语境才能分辨"这是不
    是本文件真正引用的业务标识"）。改为三态 `keys` 参数：`None`（缺省，CLI/Web 富投影不变，仍老式
    全文盲扫）／`[]`（MCP 压缩投影默认，未传 keys 时落到此档，`field_names` 强制留空，只给源码正
    文）／显式列表（只核对传入的标识，含 `Xxx.CONST` 限定常量引用，零盲扫噪音）。两步流程：模型先
    `read_source(relpath)` 读正文，自报陌生标识后 `keys=[...]` 二次调用核对。`INSTRUCTIONS`/
    `tool_read_source`/`tool_resolve_fields` docstring/`_RS_COMPACT_NOTE` 同步改写。CLI 未改动
    （`keys=None` 默认档零回归）。`tests/test_read_source.py` 新增 6 条 + 改写 4 条既有用例（原先依
    赖默认全文盲扫产出数据的用例，改为显式传入等价 `keys`）。详见 `docs/阶段验收.md`。**验收待定：
    Claude 未自跑 `pytest -q`，交 codex 执行确认零回归。**
  - **退役 read_source（MCP）+ 字段名解析改走"模型自读源码 + resolve_fields 精确核对"**
    （2026-07-05，**MCP 工具面 7→6**）：上条两步协议量化后（`docs/read_source字段名解析逻辑.md`
    §5：真实库 ambiguous 占 30.3%，两个"结构性漏判"候选修复方向收益均 <1.1% 且有误判风险）用户
    判断"工具自动消歧"复杂度收益比不划算，且当前工作流下源码已是本地 UTF-8（`read_source` 当初为
    防"宿主原生 reader 读 GBK 乱码"而建的前提不再成立）——**整个 MCP `read_source` 工具退役**，
    改用宿主自带 reader 读源码；模型自己从源码字面量（如 `.load("cqkd_zkd", ...)`）识别实体 key
    后，把 `resolve_fields` 的 key 写成 `"实体key.字段key"` 做精确匹配（查到即唯一答案；给的实体
    key 不含该字段时，返回值新增 `mismatched_form` 诚实指出字段真实所在单据，不悄悄回退掩盖）。
    同一批把 `method_calls`（`fields.reads/writes`）、`ask`（`plugin_explain`/`operation_explain`）
    的字段自动中文名标注一并砍掉（同属"全局候选盲扫"，与 read_source 同标准）；`trace`/`bill` 的
    `field_name`（精确坐标查出，非盲扫）与全部 `semantics_topic`（事件→语义文档路由，确定性映射）
    不受影响。**CLI `cosmic_kb source`（人工终端排障）不受影响**，`report/read_source.py` 富模式
    函数原样保留，只删了服务 MCP 的紧凑投影/两步协议/游标分页那一层；`semantic/hints.py` 的
    `FieldNames`/`build_field_names`/`annotate_field` 因此成为死代码一并删除。详见
    `docs/阶段验收.md`"退役 read_source（MCP）"条目。**验收待定：Claude 未自跑 `pytest -q`，交
    codex 执行确认零回归。**
  - **`resolve_fields` 补齐单据(表单)中文名 + 文案硬化"必须核实"**（2026-07-05，真实排障复盘）：
    上条上线后真实翻车——模型在工具调用预算压力下把部分字段/表单标识判定为"次要"、跳过
    `resolve_fields` 核实直接凭字面翻译，且表单类标识（如 `cqkd_invoic_apply`）当时**无工具可
    查**（`resolve_fields` 只接了 `field`/`entity` 两表）。两处修复：① `resolve_fields`/`_items_for`
    新增查 `form` 表，命中追加 `kind="form"` 条目（与字段/容器命中并列，同 key 若既是表单 key
    又是表头实体 key 两条都摆出不互相覆盖）；② `INSTRUCTIONS`/`tool_resolve_fields`/
    `tool_method_calls` docstring 改成祈使句并**明确排除"调用预算紧张"这条具体合理化路径**——
    批量传参本身就是为一次调用覆盖多个标识设计的，不是"选重要的核实、次要的跳过"的理由。分录/
    子分录容器名称查询确认此前已支持（`entity` 表 `level` 列本就覆盖 header/entry/subentry），
    本次只是补测试覆盖 subentry 档。详见 `docs/阶段验收.md`"resolve_fields 补齐单据(表单)中文名"
    条目。**验收待定：Claude 未自跑 `pytest -q`，交 codex 执行确认零回归。**
  - **`resolve_fields` 补齐"分录.字段"/"单据.分录.字段"复合限定符**（2026-07-05，同一次真实
    排障复盘续）：上条上线后用户拿真实对话验证，发现模型照搬 `trace` 的点号坐标写法（`分录.字段`
    /`单据.分录.字段`）传给 `resolve_fields` 全部返回 `null`——根因是 `_split_qualified` 只认
    `单据.字段` 两段式且限定符须命中 `form` 表，模型传的分录 key（`entity` 表）两次调用都不满足
    这个前提，落到"裸 key 查、查无此字面 key"分支返回空，模型误判"字段未登记"。改写
    `_split_qualified` 按段数解析（与 `field_trace.parse_locator` 同一套坐标惯例）：两段式前段
    先试 `form` 再试 `entity`，三段及以上首段=单据+倒数第二段=分录/子分录；主循环按
    `(form_key, entry_key)` 双维度过滤，`mismatched_form` 视限定符类型给 `given_form`/
    `given_entry`（可同时出现），纯两段式既有行为/测试不变。详见 `docs/阶段验收.md`
    "resolve_fields 补齐'分录.字段'/'单据.分录.字段'复合限定符"条目。**验收待定：Claude 未自跑
    `pytest -q`，交 codex 执行确认零回归。**
  - **dbmeta · 增量二开元数据同步**（2026-07-05）：真正频繁变动的是项目自己的二开元数据（非
    原厂），实测确认 `t_meta_formdesign`/`t_meta_entitydesign` 有 `fisv`/`fmodifydate` 列、
    转换规则另在独立表 `t_botp_convertrule`、一个平台库唯一二开 ISV；`build --db-config` 自动
    按 ISV+`fmodifydate` 增量拉取自己的 form/entity/转换规则变更，同 key 整条替换（非 vendor
    合并语义），水位存 `kb_meta`；纯 DB 零 zip 首次建库亦可（`meta` 位置参数改 `nargs="*"`）；
    新增 `--isv`/`--full-refresh`；新增 `dbmeta/sync.py`；schema 未变（仍 v13）。详见
    `docs/阶段验收.md`"dbmeta · 增量二开元数据同步（build-only）"条目。**验收待定：Claude 未
    自跑 `pytest -q`，交 codex 执行确认零回归；另有 `server_now_iso()` 真库时区行为需用户手动
    验证一次。**

> ⚠️ MCP server 常驻，改 MCP/取证源码后需**重连/重启 MCP** 才生效；改 schema 后需 `cosmic_kb build` 重建 KB。

## 常用命令（Windows / PowerShell）

```powershell
pip install -e ".[parse,encoding,dev,fuzzy,mcp]"  # 解析+编码+测试+模糊匹配+MCP（fuzzy/mcp 可选）
pytest -q                                # 跑测试（当前 460 passed, 4 skipped）
cosmic_kb --version                      # 版本
cosmic_kb doctor                         # 资产体检（需 skill_assets/ok-cosmic-docs.db）
cosmic_kb ingest "<项目源码根>"          # 阶段1：摄取 + 覆盖率/可信度报告（--json 可留档）
cosmic_kb meta "<dym|cr 或整包 zip>"     # 阶段2：解析元数据(含转换规则 .cr)，分类计数/JSON 快照
cosmic_kb bridge "<项目源码根>" "<dym|zip|目录>"  # 阶段3：ClassName↔源码桥接报告（--json）
cosmic_kb build "<项目源码根>" ["<dym|zip|目录> ..."] [--db-config <配置> [--isv <ISV>] [--full-refresh]]  # 阶段4+5：建 KB（含字段级分析）；给了 --db-config 自动增量同步本项目二开 form/entity/转换规则变更（纯 DB 冷启动可省略 dym/zip 参数）
cosmic_kb trace "单据.字段|单据.分录.字段|单据.分录.子分录.字段"  # 旗舰：按层级精确定位字段→谁改了它/事件函数/是否落库（裸字段=列全部坐标）
cosmic_kb bill "<单据标识>"              # 单据钻取：操作集/插件/字段触达/风险
cosmic_kb calls "<类全限定名>" "<方法名>"        # 方法出向调用导航：该方法调了项目内哪些方法→目标类/源文件/行；并附该方法读写字段 key + 语义路由（中文名不再自动标注，需要调 resolve_fields 核对）；源码全文与"方法在干嘛"交给大模型直接读+苍穹 skill
cosmic_kb source "<相对源码根的源文件路径>"      # CLI 人工排障：读源码（野生编码正确解码）+ 自动标注其中字段 key 真实中文名（--lines A-B 读区间）；同名跨单据按本文件数据包来源三档消歧（unique/resolved/⚠️ambiguous 别默认当前单据）。段二（AI）已改走宿主自带 reader + resolve_fields，本命令只服务人工终端排障
cosmic_kb ask "<自然语言问题>"           # 阶段9：NL→意图→查 KB 取证（字段谁改的/单据钻取/插件解释/方法做了什么；消歧退出码3，--json 喂 Skill）
cosmic_kb coverage                       # 信任优先·手段一：字段覆盖率（元数据为分母）+ 扫描质量分解
cosmic_kb scan-compare                   # 信任优先·手段二：粗精度(源码字面量) vs 高精度(field_access)对比→疑似盲点/精度增量
cosmic_kb dynwrites [--form/--cause/--cls] # 信任优先：字段 key 钉不出的读写（动态循环/拼接/外部常量/歧义/未识别）按「该读方法」去重列出，交段二大模型读源码定性（防爆上下文）
cosmic_kb resolve "<字段/分录/单据标识> ..."  # 标识核对：字段/表头实体/分录/子分录/单据(表单)→真实元数据中文名+坐标（O(1) 打词典，比 trace 便宜；同 key 多坐标全摆出，钉不出回 null，防大模型按命名惯例臆断中文名）；支持复合限定符精确匹配，与 trace 同一套点号坐标写法："单据.字段"/"分录.字段"/"单据.分录.字段"，限定符不含该字段时返回 mismatched_form 诚实提示
cosmic_kb web                            # 本地浏览器排障（输字段→表格→跳源码；含「扫描可信度」页签：手段一+手段二）
cosmic_kb mcp                            # 阶段10：起 MCP 服务器，把取证命令暴露成 MCP 工具供 LLM 宿主调用（项目根 .mcp.json 自动识别）
```
> 若 `cosmic_kb` 脚本入口不可用，等价用 `python -m cosmic_kb.cli.main ...`。

## 编码与协作约定

- **对用户用简体中文回答**（用户偏好）。
- 代码注释/文档字符串用中文，风格与现有模块一致（务实、可解释，讲清"为什么这么做"）。
- 可选依赖分组放 `pyproject.toml` 的 optional-dependencies，避免一上来装一堆。
- **分工（2026-06-27 起）**：**Claude 负责开发 + 验收文档（`docs/阶段验收.md`）更新**；
  **codex 负责测试 + git 提交**。Claude 改完代码即写/改对应验收结论，不自己跑测试套件、
  不自己 `git commit`；交给 codex 跑 `pytest -q` 与提交。
- 每个新功能仍需配 `tests/` 测试用例（Claude 写用例，codex 负责执行验证不回归）。
- 后续凡是做 `form_key` 识别率优化，测试完成后必须同步刷新
  `docs/数据包来源与form_key解析合并.md` 的 **§2「当前识别情况」两张统计表**（总体定位率 + 来源依据分布）。
- **工作纪律：一个阶段 ≈ 一个会话。** Claude 开发 → 写测试用例 → 更新 `docs/阶段验收.md`
  → 用户人工验收 → codex 跑测试 + git 提交 → 开新会话做下一阶段，保持上下文干净。
- 重要决策/架构取舍写进 `docs/`，不要只留在对话里。
