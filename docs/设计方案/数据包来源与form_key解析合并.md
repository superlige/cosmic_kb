# 数据包来源（form_key）解析说明

> 这份文档**只讲一件事**：一条字段读写记录，能不能钉出它属于**哪张单据/哪个实体**（`form_key`）。
> 它是 form_key 识别率的交接台账——每次做 form_key 优化、测试通过后，回来刷新 §2 的统计。
>
> 合并来源：`docs/form_key解析待办.md` + `docs/数据包来源解析.md`。

---

## 0. 先把概念钉死（看完这节再往下，否则一定混）

### 0.1 两种"未解析"，是两层完全不同的问题

排障引擎对一条 `obj.set("cqkd_amount", v)` 要回答两个独立的问题：

| 问题 | 字段是 | 解不出时 | 归到哪 |
|------|--------|----------|--------|
| **改的是哪个字段？** | `field_key`（如 `cqkd_amount`） | `field_key=None` | **动态写入候选** `dynamic_writers` |
| **这条读写属于哪张单据/实体？** | `form_key`（如 `cqkd_ht` 合同单） | `form_key=None` | **未定位来源** `unlocated` |

- `field_key=None`：连"改的是哪个字段"都不知道（key 来自循环/拼接/外部常量）。**本文档不讲这个**，它属于"动态写入"专题。
- `form_key=None`：**字段已经钉出来了**，只是不知道这一条具体读写发生在哪张单据/分录上（承载字段的 `DynamicObject` 不知道从哪来）。**本文档只讲这个。**

> 一句话：本文档讲的是"**知道改的是哪个字段、但不知道改的是哪张单据**"这一层。

### 0.2 `trace` 查某字段，**不会**返回全项目的未解析字段

这是最容易误解的一点。`cosmic_kb trace cqkd_ht.cqkd_zdgl.cqkd_qs` 永远**只围绕你查的那一个字段**，所有返回桶都是这个字段的记录。但"凭什么算这个字段的记录"，五个桶分两套判定逻辑：

| trace 返回桶 | 内容 | 范围 | 判断属于本字段的逻辑（代码位置） |
|-------------|------|------|--------------------------------|
| `groups` | 精确命中（form_key 钉出来了，按单据·层级·分录分组） | **仅本字段** | `field_access WHERE field_key=<被查字段>` 取数后，再过 `_is_exact`：form_key/level/entry_key 全匹配过滤条件（`field_trace.py:234,281-291`） |
| `possible` | 同单据、但层级/分录存疑 | **仅本字段** | 同上同一批行里，`r.form_key==查询单据` 但 `_is_exact` 不成立的（层级/分录判不准，宁可降级也不丢）（`field_trace.py:293`） |
| `unlocated` | 读写本字段、但 `form_key=None` 的行 → 折叠成「反推来源方法」清单（cap 10） | **仅本字段** | 同一批行里 `r.form_key is None` 的（字段已钉出、来源单据没钉出）（`field_trace.py:295`） |
| `dynamic_writers` | `field_key=None` 的动态写（*可能*在写本字段，按本字段单据范围收窄） | **仅本字段范围** | 字段 key 钉不出，无法靠 key 等值 → 退到**单据范围**：`form_key ∈ scope_forms`，或 `form_key IS NULL 但插件注册在 scope_forms 上`；`scope_forms = 本字段元数据定义单据 ∪ 本字段已定位行的单据`（`field_trace.py:245-268,382-403`） |
| `summary.unlocated_by_reason` | 本字段 `form_key=None` 行的成因直方图 | **仅本字段** | 同 `unlocated`，按 `all_rows` 里 `form_key is None` 的全部行统计（`field_trace.py:345-346`） |

**两套归属判定，别混：**

1. **字段 key 等值归属**（`groups`/`possible`/`unlocated`/`unlocated_by_reason`）：四桶全部出自 `SELECT … FROM field_access WHERE field_key=<被查字段>` 这**同一批行**——字段 key 本身就是硬归属（物理上 `obj.set("cqkd_qs")` 不可能落到别的字段上）。四桶只是把这批行按 form_key 状态再分流：钉准坐标→`groups`，同单据存疑→`possible`，来源 None→`unlocated`。所以 `unlocated` 不是"所有未解析字段"，而是"**这个字段被读写、但没钉出来源单据**的那部分记录"，且已按 (入口类, 事件方法) 去重折叠，不会刷屏。

2. **单据范围归属**（`dynamic_writers`）：这些行 `field_key=None`，**连改的是不是本字段都不确定**，没法用 key 等值挂靠。只能反过来用"本字段所在的单据"圈一个怀疑范围：`scope_forms` = 本字段在元数据里的定义单据（`occurrences`）∪ 本字段已定位行的 `form_key`；落在这些单据上、或 `form_key=None` 但插件注册在这些单据上的动态写，才"可能在写本字段"。这是**怀疑范围而非证明**，故标 `dynamic_writers`（候选）、交段二大模型读源码定性，绝不自动认领。指定 `--form` 精确查询时，范围进一步收窄到该单据（`field_trace.py:385-390`）。

> 想看**全项目**维度的未解析审计，用别的工具：`coverage`（覆盖率）、`scan-compare`（盲点）、`dynwrites`（钉不出字段的写）。`trace` 是聚焦单字段的。

### 0.3 form_key 为什么重要

`form_key` 越多，`read_source` 给同名字段（同一个 `<isv>_xxx` key 在多张单据都用、中文名可能不同）做归属收敛时就越能明确"这行就是 XX 单据的"；钉不出（`form_key=None`）时只能平铺所有候选，诱导段二大模型按命名惯例脑补归属——这正是要压住的风险。

---

## 1. 统计口径

以下数量按真实项目 `D:\kingdee\asset_management_sys\cosmic_kb.db` 的 `field_access` 表复算。重建命令：

```powershell
python -m cosmic_kb.cli.main build "D:\kingdee\asset_management_sys" --db-config "D:\kingdee\asset_management_sys\cosmic_db.json" --isv cqkd --db "D:\kingdee\asset_management_sys\cosmic_kb.db"
```

> 每次做 form_key 优化并测试通过后，回来刷新 §2 的两张表。

---

## 2. 当前识别情况（2026-07-16 · schema v18 真实整库）

> 已用项目 `cosmic_db.json` 全量同步 `cqkd` ISV 元数据并覆盖重建真实 KB；
> built_at 2026-07-16 12:17:16，`PRAGMA integrity_check=ok`。SymbolTable 解析
> 83712/86189（97.13%），以下指标全部由磁盘上 schema v18 KB 重新 SQL 复算。

### 2.1 总体定位率

| 口径 | 总数 | 未定位（form_key=None） | 未定位占比 |
|------|------|------------------------|------------|
| 全部字段读写 | 21,763 | 6,854 | 31.49% |
| 写访问 write | 7,809 | 1,780 | 22.79% |
| 读访问 read | 13,954 | 5,074 | 36.36% |

已定位 14,909 条（68.51%）。

### 2.2 已定位的来源依据分布

`form_key_source` 列（schema v10）诚实区分"数据流证明"与"元数据反推"：

| 来源依据 | 数量 | 占全部 | 强度 | 说明 |
|----------|------|--------|------|------|
| `data_flow` | 9,882 | 45.41% | 数据流证明 | 事件入参 / ORM / 转换源目标 / 集合·行变量 / 跨方法传播 |
| `reverse_callgraph` | 204 | 0.94% | 数据流证明 | 孤立 helper 被项目内可解析调用，实参来源安全收敛后反向传播（见 §4.6） |
| `metadata_unique` | 2,262 | 10.39% | 元数据反推 | 数据流断链，但字段 key 在元数据里只归一张单据，反查唯一 |
| `metadata_binding` | 1,756 | 8.07% | 元数据反推 | 字段 key 多候选，但与插件绑定单据取交后唯一 |
| `metadata_cooccur` | 805 | 3.70% | 元数据反推 | 同一接收者变量连读写多字段，候选单据交集唯一 |

> `data_flow`/`reverse_callgraph` 是真实数据流级证据；`metadata_*` 是"按字段归属反推来源单据"——不是冒充数据流。`read_source` 对 `metadata_*` 来源会注明"依据是字段归属、非数据流行号"。

---

## 3. 未定位成因（`null_reason`，schema v11）

每条 `form_key=None` 的行，在**全部回填之后**由 `cosmic_kb/java/null_reason.py::classify` 打**一个**互斥成因码（被反向调用图/元数据回填**救活**的行成因清空，不污染）。成因暴露在 `trace`（`summary.unlocated_by_reason` + unlocated 工作单每条带 `null_reason`）、`coverage`、本地 web「扫描可信度」页签，告诉段二/人：**这行为何 None、该不该继续追。**

成因码就是唯一权威分桶口径（不再维护"人工抽样分桶"另一套）。下表数量按 2026-06-27 复核抽样映射（codex 整库重建后用
`SELECT null_reason, COUNT(*) FROM field_access WHERE form_key IS NULL GROUP BY null_reason` 重算精确值）：

| `null_reason` | ≈数量 | 该不该追 | 含义与典型写法 |
|---------------|-------|----------|----------------|
| `local-or-container-source` | ~1,711 | 可读源码反推 | 本地 `new DynamicObject(...)`、`Map`/容器取出、helper 返回值、非 ORM 工厂；字段钉出来了但承载对象不知来源 |
| `helper-caller-unknown` | ~1,558 | 部分可反推 | `void fill(DynamicObject o)` 形参，调用方传进来的 `o` 来源未安全收敛（反向调用图已吃掉可收敛部分，剩余多为任务/报表/反射/框架入口） |
| `field-key-undeterminable` | ~1,289 | **无意义** | 字段 key 本身钉不出（动态循环/拼接/外部常量/歧义）——来源讨论无意义。归并了旧「仅常量备注」「动态字段」「key 未消歧」三类 |
| `dynamic-entity` | ~711 | **正确 None** | ORM 实体名是运行时变量/拼接：`load(id, getEntityKey())`、`load(id, entityName)`。静态不可钉，留 None 是对的 |
| `unknown` | ~616 | 先补证据 | 老记录或某些 `model.*`/`do.*` 路径没留下足够来源说明 |
| `basedata-ref` | ~595 拆分（读侧） | **正确 None** | `bill.getDynamicObject("org").getString(...)` 读基础资料引用对象自身的字段，无业务单据表头/分录坐标 |
| `basedata-write-suspect` | ~595 拆分（写侧） | **继续追** | `xxx.getDynamicObject("org").set(...)` 写到基础资料引用对象——苍穹不会取基础资料再 save，出现即扫描误绑、真实来源单据未定位（精确拆分计数待整库重建复算） |
| `model-context` | ~423 | 补元数据 | `getModel().setValue(...)` / `IDataModel model` 形参，但插件未注册绑定单据（拿不到 default_entity） |

> **"正确 None"（`CORRECT_NONE_REASONS`）只含 `basedata-ref`（读基础资料自身字段）和 `dynamic-entity`**（运行时实体名）：本就无业务单据坐标 / 运行时才知道，**不该诱导段二去硬追**。
> ⚠️ **`basedata-write-suspect`（写到基础资料）不是正确 None**：苍穹不会"取基础资料对象再保存"，这类写入一律是扫描器把接收者误绑成了基础资料引用、真实来源单据没追到，应继续追/修扫描器（红线 #4：不可用"无需追"掩盖扫描误判）。其余成因才是"理论上可继续"的缺口。

### 3.1 本轮（2026-06-28）方向结论

对真实库实测后，**红线内已无安全的高收益 form_key 提升空间**：

- `model-context`（~423）：根因不是"缺 getModel()→绑定接线"，而是 **51/53 个类在元数据里 0 绑定**（按基类识别出的表单插件未注册），拿不到 default_entity。红线内安全可救仅约 11 行。
- 把字段 key→单据交集放粗到方法级/类级，冲突率 28–35%（方法天然跨多单据），不安全；`metadata_cooccur` 已是安全天花板。

所以**本轮转信任优先**：不追数字，把未定位成因做成可消费的诚实诊断（即 `null_reason`）。后续若仍要提识别率，参 §5，且**不得放宽收敛红线**。

---

## 4. 目前能解析的写法（form_key 怎么钉出来的）

### 4.1 模型 API 写法

识别 `getModel()` 或已传播进来的模型/视图形参上的：

| API | 类型 | 层级判断 |
|-----|------|----------|
| `setValue` / `setItemValueByID` / `setItemValueByNumber` | 写 | 2 参=表头，3 参=分录，4 参=子分录 |
| `getValue` / `getItemValueByID` / `getItemValueByNumber` | 读 | 1 参=表头，2 参=分录，3 参=子分录 |

来源实体取插件绑定单据、转换目标单，或跨类传进来的模型/视图绑定单据。已覆盖 `helper(IDataModel model)`、`helper(IFormView view)` 形参写法。

### 4.2 DynamicObject 数据包写法

| 来源写法 | 判定结果 |
|----------|----------|
| 事件入参：`e.getDataEntity()`、`e.getDataEntities()[i]`、`getBizDataEntity()`、`extendedDataEntity.getDataEntity()` | 绑定单据/转换目标单表头 |
| 事件数组：`e.getDataEntities()` 后 for-each/lambda/下标取行 | 绑定单据表头集合元素 |
| ORM 集合：`load/loadFromCache(...)`、`QueryServiceHelper.query(...)` | 实参实体的表头集合 |
| ORM 单包：`loadSingle/loadSingleFromCache/newDynamicObject/queryOne(...)` | 实参实体的表头包 |
| 转换源单：`getValue(CONVERT_SOURCE)` / `getValue(...SOURCE...)` | 转换源单表头集合 |
| 取分录集合：`x.getDynamicObjectCollection(k)`、`getModel().getEntryEntity(k)` | 继承 `x` 来源，定位 entry/subentry + entry_key |
| 取基础资料：`x.getDynamicObject(k)` | `level=basedata`，标基础资料引用包 |
| 集合取行：`coll.get(i)`、`coll.iterator().next()`、`coll.addNew()` | 继承集合元素坐标 |
| 新行类型来自集合：`new DynamicObject(coll.getDynamicObjectType())` | 继承集合元素坐标 |
| 内联新行：`x.getDynamicObjectCollection("e").addNew()` / `.iterator().next()` | 直接识别为该分录元素 |
| 增强 for：`for (DynamicObject row : coll)` | `row` 继承 `coll` 元素坐标 |
| lambda：`coll.forEach(row -> row.set(...))` | lambda 形参继承集合元素坐标 |
| 内联 lambda：`bill.getDynamicObjectCollection("e").forEach(row -> ...)` | 直接从内联集合表达式推分录坐标 |
| stream 派生：`coll.stream()...collect/toList/toSet`、`Arrays.stream(coll)` | 继承原集合元素坐标 |
| 泛型集合：`List/Set/Collection<DynamicObject>` 由 `.add(loadSingle(...,"entity"))` 聚合 | 元素来源按 add 进去的已知实体收敛 |
| 根包常见命名：`bill/dataEntity/billObj/info/obj/dynamicObject/data.set(...)` | 绑定插件内保守按表头，来源=绑定单据 |
| helper 返回值：`row = this.helper(...)` 后 `row.set(...)` | helper return 坐标唯一时，调用方局部变量继承返回坐标 |

### 4.3 跨方法 / 跨类传播

从事件入口向被调方法传播（只传确定信息；没来源的裸表头上下文不硬传，避免把 service 误归单据）：

| 形参类型 | 传播内容 |
|----------|----------|
| `DynamicObject` | `level + entry_key + form_key` |
| `DynamicObject[]` / `DynamicObjectCollection` | 集合元素坐标 |
| `List/Set/Collection<DynamicObject>` | 泛型集合元素来源 |
| `IDataModel` / `IBillModel` / `IFormView` 等 | 绑定单据 |
| `String` | 调用方传入的字段 key / 分录 key 字面值 |

### 4.4 字段 key 反查元数据回填（数据流断链后兜底）

数据流追不到 `form_key`、但字段 key 已解析时，用"被读写的字段 key"反推来源实体（物理硬约束：`DO` 不可能 `.set("cqkd_x")` 除非其实体声明了该 key）：

| 层级 | 条件 | `form_key_source` |
|------|------|-------------------|
| 字段 key 唯一反查 | 该 key 在元数据里只属一张单据 | `metadata_unique` |
| 绑定收敛 | key 多候选，但与 `access_class`/入口插件绑定单据交集唯一 | `metadata_binding` |
| 同对象共现交集 | 同一接收者变量连读写多字段，候选单据交集唯一 | `metadata_cooccur` |

多候选不能收敛时继续留 None（红线 #4）。

### 4.5 注解驱动映射写入（A 档，2026-06-28）

`@…Annotation(value="cqkd_xxx") + convertTo…DynamicObject` 反射 `bill.set(annotation.value(), …)` 这类手写 ORM，字段 key 是注解里的**静态字面量**、可用 KB 字段表反验证，已由 `java/annotation_map.py` 识别并合成 `field_access` 写入行（via=`annotation-map`）——**写入本身不再隐形**（此前一条 access 都不产生）。其 `form_key` 仍走 §4.4 反查：唯一→`metadata_unique`，歧义无绑定收敛→**None**（不臆造）。精确单据坐标（按调用点 `DynamicObjectType` 实参收敛）留 B 档。

### 4.6 孤立方法反向调用图回填（`reverse_callgraph`，2026-06-27）

正向分析只从「绑定插件事件 / 未绑定苍穹插件根方法」出发跨类 BFS 传源；**没被任何事件 BFS 覆盖**的孤立 helper（`void fill(DynamicObject o){ o.set(...) }`）DO 形参不知来源 → 整片 None。反向调用图补这条断链：

| 条件 | 处理 |
|------|------|
| helper 在项目内被可解析调用（`_resolve_call` 解得出受者类型，含 `new Helper().m()`） | 纳入反向调用边 |
| 唯一/链式调用方实参来源已知 | 固定点逐跳传播，带 `param_ctx` 重跑 `analyze_method`，回填原 None 行 |
| 多调用点全部推出同一非空来源 | 视为一致，允许回填 |
| 0 调用方 / 来源冲突 / 任一调用点来源未知 / 递归自调用 | 一律留 None（红线 #4） |

只动 `form_key=None` 行、绝不改写已定位；单跳唯一调用方 confidence ≤0.85，链式/多调用一致 ≤0.80。实现集中在 `analyze.py`（`_build_reverse_calls` + `_propagate_reverse_props` + `_backfill_reverse_calls`，排在元数据兜底之前先定）。固定点版真实库回填 218 条（读 148 / 写 70，覆盖 12 个方法）；相对单跳版 202 条仅增 16 条，说明剩余缺口不在可安全收敛的反向调用链。

---

## 5. 仍需诚实留空的边界（红线 #4）

以下情况**不应为提高数字硬填**：

- ORM 实体名来自变量、方法返回值或拼接（`dynamic-entity`，正确 None）。
- **读取**基础资料引用对象自身字段，无单据表头/分录坐标（`basedata-ref`，正确 None）。
  （注：**写到**基础资料对象不在此列——苍穹不会取基础资料再保存，那是扫描误绑，`basedata-write-suspect`，应继续追。）
- 字段 key / 分录 key 来自运行时循环、配置、外部常量，静态无法唯一解析（`field-key-undeterminable`）。
- 报表 `RowMeta`、配置 Map、系统参数决定的字段集合，除非能静态收敛为唯一常量集。
- 同一 helper 被多入口以不同来源调用，无法唯一确认当前调用来源。
- 任务入口、报表入口、WebAPI 工具入口等框架调度、源码内无可解析业务单据实参的场景。
- 多候选字段 key 既无绑定收敛、也无同对象共现交集。
- 反射、`Map` 批量赋值、`BeanUtils` 拷贝、自研 DAO 等非标准 API，除非补明确规则 + 正反例测试。

---

## 6. 下一步优先级

1. ✅ **孤立方法反向调用图回填（已完成，§4.6）**。本轮未达 500+ 目标，说明剩余缺口不能靠继续放宽反向调用图安全规则解决。
2. ❌ **model / IDataModel 专项——经 2026-06-28 实测否决**：根因是 51/53 类元数据 0 绑定，红线内安全可救仅约 11 行。改在 `null_reason='model-context'` 诚实标注"插件未注册绑定单据"，留待补元数据或人工。
3. **ORM 动态实体常量实参专项**（`dynamic-entity` 子集）：处理"调用方传实体常量 → 被调方法参数 `entityName` → `load(entityName, ...)`"；实体名来自变量/配置/返回且无法唯一收敛时继续 None。
4. **反射目标方法解析专项**：配合 #3 处理 `midClass.getDeclaredMethod(...).invoke(...)`，仅当 `Class<?>` 和方法名字符串都能静态唯一确定时传播；否则不能把所有反射调用混成一个入口。
5. **动态字段静态集合子集**：只救字段集合来自静态数组、常量 `List`、明确枚举的场景；`RowMeta.getFieldNames()`、配置 Map 决定的字段继续留 None。
6. **补 evidence 文案与分类**（`unknown` 桶）：先补清楚是 model / do / 常量 / 元数据 / 来源断链，避免把多个问题混成一个"未定位"。
7. **扩充 ORM/写入 API 名单**：只补项目里真实出现、语义确定的 DAO/缓存/批量写 API；每补一类加正反例测试。

> 源码复核显示反向调用图这条路线已接近安全上限。继续提识别率应优先转向 ORM 动态实体常量实参、反射唯一目标和动态字段静态集合子集，**不得放宽多调用冲突/未知来源的安全红线**。能确定就打 `form_key` 和依据；不能确定就留 `None`，让查询结果进未定位/存疑桶。
