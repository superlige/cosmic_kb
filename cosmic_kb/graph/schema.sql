-- 阶段 4 · Cosmic KB 图谱 schema（SQLite + FTS5）
--
-- KB 是段一（扫描器）与段二（AI 理解层）之间的契约（见 CLAUDE.md 六条硬约束之
-- 「两段式解耦」）。本 schema 把阶段 1-3 的三类内存产物（ScanResult / MetaModel /
-- BridgeResult）沉淀成可持久、可重建、可被 AI 查询的图谱。
--
-- 设计要点：
--   * 节点表 + 一张通用 edge 表（阶段 8 业务流边复用同一张表，不另起炉灶）。
--   * 处处置信度 + 证据：凡解析/推断得来的关系都带 confidence(0~1) 与 evidence（来源/行号）。
--   * 幂等重建：store.build_kb 先 DROP 全部再按本文件重建（见 store.py），KB 任意时刻可从零重建。
--   * 模块识别按多信号：module 锚定 appKey、辅以代码包前缀（dominant_package）、
--     带 pkg_consistency 置信度（见 report/project_map.py）。绝不靠单一包路径层级硬切。

-- ── 模块（按 appKey 锚定的业务模块；含 'unknown' 与 '未归类' 两个特殊桶）──────────
CREATE TABLE module (
    name              TEXT PRIMARY KEY,   -- 模块名（取 appKey；特殊桶用 'unknown'/'未归类'）
    app_key           TEXT,               -- 锚定的 appKey（特殊桶为 NULL）
    dominant_package  TEXT,               -- 主导代码包签名（绑定类包前缀众数；用于孤儿归类与交叉校验）
    pkg_consistency   REAL,               -- 包结构一致度 0~1（绑定类包前缀与主导签名一致的占比）
    form_count        INTEGER DEFAULT 0,
    entity_count      INTEGER DEFAULT 0,
    plugin_count      INTEGER DEFAULT 0,
    class_count       INTEGER DEFAULT 0,  -- 归入本模块的源码类数（含绑定类 + 一致孤儿）
    orphan_real_count INTEGER DEFAULT 0,  -- 真孤儿数（role=unknown），风险信号
    confidence        REAL DEFAULT 1.0,   -- 模块切分置信度（随 pkg_consistency 降级）
    evidence          TEXT                -- 切分依据简述（如 'appKey=cqkd_assets, 主导包 cqspb.assets'）
);

-- ── 表单（dym → MetaModel）──────────────────────────────────────────────────
CREATE TABLE form (
    key           TEXT,                   -- 表单标识（cqkd_assetcard）；可能为 NULL
    name          TEXT,                   -- 中文名
    form_type     TEXT,                   -- 归一类型 bill/basedata/dynamic/...
    model_type    TEXT,                   -- 原始 ModelType
    isv           TEXT,                   -- 元数据 ISV（仅报告产物）
    app_key       TEXT,                   -- 所属应用（模块主锚）
    module        TEXT,                   -- 归属模块名（→ module.name）
    source_dym    TEXT,                   -- 来源 dym 路径/成员（原厂 DB 来源为 db://<fnumber>）
    is_extension  INTEGER DEFAULT 0,      -- 1=本行是扩展别名（内容已并入 extends 指向的原厂 key）
    extends       TEXT                    -- 非空=扩展别名指向的原厂 form_key（见 dbmeta/integrate.py）
);

-- ── 实体（表头 / 分录 / 子分录）──────────────────────────────────────────────
CREATE TABLE entity (
    form_key    TEXT,
    key         TEXT,
    name        TEXT,
    level       TEXT,                     -- header/entry/subentry
    parent_key  TEXT,                     -- 父实体 key（表头为 NULL）
    table_name  TEXT
);

-- ── 字段 ─────────────────────────────────────────────────────────────────────
CREATE TABLE field (
    uid         TEXT,                     -- 稳定唯一键（form_key + entity_key + key/id）
    form_key    TEXT,
    entity_key  TEXT,
    key         TEXT,
    name        TEXT,
    db_column   TEXT,
    field_type  TEXT,
    kind        TEXT,                     -- entity/dynamic/basedata_prop/platform/inherited
    level       TEXT
);

-- ── 插件（界面/列表/操作）──────────────────────────────────────────────────
CREATE TABLE plugin (
    uid             TEXT,                 -- form_key + class_name + plugin_type
    form_key        TEXT,
    class_name      TEXT,                 -- <ClassName> 全限定名（桥接唯一键）
    plugin_type     TEXT,                 -- form/list/op/writeback
    source          TEXT,                 -- project/platform/unknown
    operation_key   TEXT,
    operation_name  TEXT
);

-- ── 源码类（namespace 索引 + bridge 孤儿）──────────────────────────────────
CREATE TABLE source_class (
    fqn           TEXT,                   -- 全限定名
    simple        TEXT,                   -- 末段类名
    package       TEXT,
    relpath       TEXT,                   -- 相对源码根 POSIX 路径
    module        TEXT,                   -- 归属模块名（→ module.name；未归类为 '未归类'）
    is_orphan     INTEGER DEFAULT 0,      -- 1=孤儿（未被任何元数据插件绑定）
    orphan_role   TEXT,                   -- plugin/constant/unknown（仅孤儿有值）
    plugin_base   TEXT                    -- orphan_role='plugin' 时命中的苍穹插件基类
);

-- ── 转换规则（.cr / ConvertRuleModel）：单据上下游关系（BOTP）──────────────────
--   不是表单，单列一表；上下游关系另以 edge.kind='converts_to' 表达，便于路径追踪。
CREATE TABLE convert_rule (
    id              TEXT,                 -- 规则 Id（snowflake，稳定键）
    name            TEXT,                 -- 规则中文名
    source_entity   TEXT,                 -- 源单据(上游) 标识
    target_entity   TEXT,                 -- 目标单据(下游) 标识
    source_entry    TEXT,                 -- 源分录 key（表头级为 NULL）
    target_entry    TEXT,                 -- 目标分录 key
    isv             TEXT,
    app_key         TEXT,
    module          TEXT,                 -- 归属模块名（→ module.name）
    field_map_count INTEGER DEFAULT 0,    -- 字段映射条数（规模线索）
    plugin_count    INTEGER DEFAULT 0,    -- 绑定的转换插件数
    enabled         INTEGER,              -- 是否启用
    source_file     TEXT
);

-- ── 桥接绑定（bridge 三态：linked/linked_by_name/external/missing/ambiguous）──
CREATE TABLE binding (
    class_name      TEXT,                 -- 元数据 <ClassName>
    form_key        TEXT,
    plugin_type     TEXT,
    status          TEXT,
    source_relpath  TEXT,                 -- 命中的源码文件（命中才有）
    confidence      REAL,
    note            TEXT
);

-- ── 操作（按钮行为：save/submit/audit/donothing…）──────────────────────────
--   落库判定要操作类型：入库类操作的事务内事件改字段直接落库，donothing 需显式 save。
CREATE TABLE operation (
    form_key        TEXT,
    key             TEXT,                 -- 操作标识（save/submit/自定义）
    name            TEXT,
    operation_type  TEXT,                 -- OperationType（save/audit/donothing/…）
    resolved_from   TEXT,                 -- self/template/unknown（可信度来源）
    has_plugin      INTEGER DEFAULT 0     -- 是否有自定义操作插件绑定（排查入口）
);

-- ── 插件方法（阶段5：事件函数 / helper + 落库相位）──────────────────────────
CREATE TABLE plugin_method (
    plugin_fqn      TEXT,                 -- 插件类全限定名
    method_name     TEXT,
    event_kind      TEXT,                 -- 生命周期事件名 / 'helper'
    event_phase     TEXT,                 -- memory/transaction/build/validate/none/helper
    start_line      INTEGER,
    end_line        INTEGER,
    source_relpath  TEXT
);

-- ── 字段访问（旗舰数据：输入字段→哪些插件的哪个事件函数改了它、是否落库）────────
--   阶段5（事件/字段）+ 阶段6 类内（调用链路径）+ 阶段7（落库判定）的合体产物。
CREATE TABLE field_access (
    form_key        TEXT,                 -- 入口单据（插件绑定的单据；多单据复用时各成记录）
    field_key       TEXT,                 -- 字段标识（旗舰查询的锚；解不出为 NULL）
    level           TEXT,                 -- header/entry/subentry/basedata/unknown
    entry_key       TEXT,                 -- 所属分录/子分录 key（表头为 NULL）
    plugin_fqn      TEXT,                 -- 入口插件/类全限定名（跨类回溯时=触发该写入的插件）
    plugin_type     TEXT,                 -- form/list/op/writeback/convert/service（service=未注册的项目类）
    access_class    TEXT,                 -- 该读写**物理所在**的类全限定名（跨类时≠plugin_fqn）
    event_method    TEXT,                 -- 入口事件函数（propertyChanged/beforeExecute…）
    event_phase     TEXT,                 -- 该事件落库相位
    access          TEXT,                 -- read/write
    persists        TEXT,                 -- yes/no/unknown/na（read 为 na）
    persist_reason  TEXT,                 -- 落库结论理由
    via             TEXT,                 -- model.setValue / do.set / model.getValue / do.get
    line            INTEGER,              -- 访问所在行号（直达源码）
    path            TEXT,                 -- 事件→…→方法 调用路径（JSON 数组）
    key_resolution  TEXT,                 -- literal/constant/ambiguous/unknown/dynamic
    confidence      REAL,
    source_relpath  TEXT,
    evidence        TEXT,
    form_key_source TEXT,                  -- form_key 来源种类：data_flow（数据流解析）/
                                           -- metadata_unique·metadata_binding·metadata_cooccur
                                           -- （字段key反查元数据回填，依据是字段归属非数据流）/
                                           -- NULL=未定位。诚实区分元数据反推与数据流证明（红线#4）。
    null_reason     TEXT                   -- 未定位成因（form_key=NULL 时**为何** NULL，信任优先红线#4）：
                                           -- field-key-undeterminable / basedata-ref（读基础资料自身字段）/
                                           -- basedata-write-suspect（写到基础资料·疑似误绑·应继续追）/
                                           -- dynamic-entity / helper-caller-unknown / model-context /
                                           -- local-or-container-source / unknown；form_key 已定位则 NULL。
                                           -- 单一真源 java/null_reason.py，供 trace/coverage/web 导航。
);

-- ── Java 全局常量值表（类.常量 → 字面值；供 read_source 解析源码里的常量引用）───────
--   起因：`TemporaryStopCon.ENTITY` 这类限定常量引用，字面值（如 cqkd_ltyz）根本不出现
--   在源码正文里，read_source 按文本扫已知 key 的老办法扫不到，逼大模型凭常量英文名去猜
--   中文单据名（真实翻车案例）。本表持久化全工程 `static final String` 常量定义（含接口
--   常量），查询期把源码里的 `类.常量` 引用查表解析回字面值，再按常规字段/实体词典标注中文
--   名，杜绝瞎猜。同一 (class_name, const_name) 若在工程里被不同类重复定义出不同字面值，
--   查询侧按歧义处理、不擅自选一个（红线 #4）。
CREATE TABLE java_constant (
    class_name      TEXT,               -- 常量所属类/接口简单名（非全限定名，同名类需靠歧义兜底）
    const_name      TEXT,               -- 常量名（如 ENTITY / KEY_AMOUNT）
    literal         TEXT,               -- 常量字面值
    source_relpath  TEXT,               -- 定义所在源文件（相对源码根）
    line            INTEGER             -- 定义所在行号
);

-- ── 粗精度扫描命中（信任「手段二」：粗扫 vs 高精度对比的粗扫侧）──────────────
--   高精度侧 = field_access（AST + 跨类 + 数据流 + 落库）。粗扫侧 = 单遍 Java 词法扫描
--   （跳过注释/字符串内部），把业务字段标识当**字符串字面量**或**唯一映射的常量名引用**
--   搜出来（不解析实体坐标/落库/调用链），作为**召回底线**——粗扫见到、高精度却没记
--   field_access 的字段 = 疑似盲点（值得人工核对）。
--   via 仅作线索（强信号=作 get/set/getValue/setValue 首参）：rw-idiom/literal=字面量、
--   const-rw-idiom/const-ref=常量名引用。
CREATE TABLE coarse_field_hit (
    field_key   TEXT,                     -- 命中的业务字段标识
    relpath     TEXT,                     -- 出现该引用的源码文件（相对源码根）
    line        INTEGER,                  -- 行号（直达源码）
    via         TEXT                      -- rw-idiom/literal · const-rw-idiom/const-ref（粗判，仅线索）
);

-- ── 通用边表（阶段 4 关系 + 阶段 8 业务流边复用同一张表）──────────────────
--   kind: has_entity / has_field / has_plugin / bound_to / module_contains /
--         converts_to（单据上下游：源单据 → 目标单据，来自转换规则）/ ...
CREATE TABLE edge (
    src_type    TEXT,                     -- form/entity/plugin/module/class
    src_id      TEXT,
    dst_type    TEXT,
    dst_id      TEXT,
    kind        TEXT,
    confidence  REAL DEFAULT 1.0,
    evidence    TEXT
);

-- ── 构建元信息（时间戳 / 源路径 / 计数 / 双前缀 / 模块切分快照）──────────────
CREATE TABLE kb_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- ── 索引（查询热点：按模块、按表单、按类名、按边类型）──────────────────────
CREATE INDEX idx_form_module      ON form(module);
CREATE INDEX idx_form_key         ON form(key);
CREATE INDEX idx_entity_form      ON entity(form_key);
CREATE INDEX idx_field_form       ON field(form_key);
CREATE INDEX idx_plugin_form      ON plugin(form_key);
CREATE INDEX idx_plugin_class     ON plugin(class_name);
CREATE INDEX idx_class_module     ON source_class(module);
CREATE INDEX idx_class_fqn        ON source_class(fqn);
CREATE INDEX idx_binding_class    ON binding(class_name);
CREATE INDEX idx_convert_source   ON convert_rule(source_entity);
CREATE INDEX idx_convert_target   ON convert_rule(target_entity);
CREATE INDEX idx_edge_src         ON edge(src_type, src_id);
CREATE INDEX idx_edge_kind        ON edge(kind);
CREATE INDEX idx_operation_form   ON operation(form_key);
CREATE INDEX idx_pmethod_fqn      ON plugin_method(plugin_fqn);
CREATE INDEX idx_facc_field       ON field_access(field_key);
CREATE INDEX idx_facc_plugin      ON field_access(plugin_fqn);
CREATE INDEX idx_facc_form        ON field_access(form_key);
CREATE INDEX idx_facc_aclass      ON field_access(access_class);
CREATE INDEX idx_facc_coord       ON field_access(field_key, form_key, level, entry_key);
CREATE INDEX idx_facc_nullreason  ON field_access(null_reason);
CREATE INDEX idx_coarse_field     ON coarse_field_hit(field_key);
CREATE INDEX idx_javaconst_class  ON java_constant(class_name, const_name);
CREATE INDEX idx_javaconst_name   ON java_constant(const_name);

-- ── FTS5 全文检索（为阶段 9 NL 查询打底：中文名↔标识 / 字段 / 类名）──────────
--   kind: form/entity/field/plugin/class —— 统一检索面，extra 放归属上下文（模块/表单）。
CREATE VIRTUAL TABLE search USING fts5(
    kind        UNINDEXED,
    key,
    name,
    extra,
    tokenize = 'unicode61'
);
