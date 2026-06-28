# 数据包来源与 form_key 解析合并说明

> 合并来源：`docs/form_key解析待办.md` + `docs/数据包来源解析.md`。
> 目的：把"当前能解析哪些写法"、"还有多少未定位"、"为什么未定位"放在同一份交接文档里。
>
> 统计口径：以下数量按真实项目 `D:\kingdee\asset_management_sys\cosmic_kb.db` 的
> `field_access` 复算。重建命令：
> `cosmic_kb build "D:\kingdee\asset_management_sys" "D:\codex\cqkd_ai\samples\appzip" "D:\codex\cqkd_ai\samples\trans"`。
> 后续每次做 `form_key` 识别率优化并完成测试后，都要同步刷新本节"当前识别情况"。

---

## 1. 当前识别情况

`form_key` 表示一条字段读写的"数据包来源实体/单据"。它是 `read_source` 对同名字段做单据归属收敛的关键：
`form_key` 越多，越能明确"这个字段是在这张单据/这个分录被读写"；`form_key=None` 时只能进入未定位/存疑桶。

| 口径 | 总数 | 未定位数 | 未定位占比 |
|------|------|----------|------------|
| 全部字段读写 | 20,259 | 6,903 | 34.07% |
| 写访问 write | 7,336 | 1,930 | 26.31% |
| 读访问 read | 12,923 | 4,973 | 38.48% |

已定位 13,356 条，占 65.93%。其中来源依据分布如下：

| 来源依据 | 数量 | 占全部读写 | 说明 |
|----------|------|------------|------|
| `data_flow` | 8,895 | 43.91% | 由事件入参、ORM、转换源/目标、集合/行变量、跨方法传播等数据流解析得到 |
| `reverse_callgraph` | 218 | 1.08% | 孤立 helper 被项目内可解析调用、调用方实参来源可安全收敛 → 沿「实参↔形参」反向传播（真实数据流级，详见 §3.6） |
| `metadata_unique` | 2,317 | 11.44% | 数据流断链，但字段 key 在元数据里只归一张单据，反查唯一 |
| `metadata_binding` | 1,169 | 5.77% | 字段 key 多候选，但与插件绑定单据取交后唯一 |
| `metadata_cooccur` | 757 | 3.74% | 同一接收者变量连写/连读多个字段，候选单据交集唯一 |

> 注：`reverse_callgraph` 为本轮（2026-06-27）新增来源，排在元数据兜底之前先定（数据流级证据强于
> 字段归属反推）。固定点传播版真实项目重建后命中 218 条（读 148 / 写 70，覆盖 12 个方法）。相对本轮优化前
> `form_key NULL 34.4%`、写访问 NULL `26.7%` 的口径，整体识别率约提升 0.27 个百分点，
> 写访问识别率约提升 0.39 个百分点；相对单跳版 202 条仅新增 16 条，说明剩余缺口主要不在可安全收敛的
> 反向调用链，而在实例字段、非标准来源、动态实体/字段等仍需另开专项的场景。

---

## 2. 未定位原因分桶

当前 `form_key=None` 共 6,903 条。下表是 2026-06-27 固定点反向调用图优化后、结合真实源码抽样复核后的
**当前互斥分桶**。分桶按优先级归因，每条 NULL 只计入一个主因，用于解释"为什么这条路线只新增 218 条"：
剩余大头已经不是单纯的"helper 链没传到"，而是动态实体、动态字段、反射/任务入口、模型上下文和元数据不足。
下一轮识别率专项结束后，应随 §1 一并重算本分桶。

| # | 未定位原因 | 数量 | 占未定位 | 占全部 | write/read | 后续判断 |
|---|------------|------|----------|--------|------------|----------|
| 1 | 数据包来源未识别（其他） | 1,711 | 24.79% | 8.45% | 454/1,257 | 来源既非可解析入参，也非 ORM 字面量；需实例字段、返回值、容器或专项 API |
| 2 | 入参调用方未知 / 不可安全收敛 | 1,558 | 22.57% | 7.69% | 335/1,223 | 反向调用图已吃掉可收敛部分；剩余多为任务/报表/反射/框架入口或实参源未知 |
| 3 | 仅字段常量备注、无来源说明 | 742 | 10.75% | 3.66% | 90/652 | 字段 key 有线索，但对象来源断链；不能按常量名猜单据 |
| 4 | ORM 实体名是动态表达式 | 711 | 10.30% | 3.51% | 332/379 | 需要常量实参/成员变量专项；纯静态不可钉时继续 None |
| 5 | 无 evidence 备注 | 616 | 8.92% | 3.04% | 179/437 | 先补证据和来源分类，再判断是否有安全规则可救 |
| 6 | 基础资料引用包 | 595 | 8.62% | 2.94% | 17/578 | 多数不是失败：读写的是基础资料对象本身，无业务单据表头坐标 |
| 7 | model / IDataModel 上下文未定位 | 423 | 6.13% | 2.09% | 277/146 | 应走插件绑定 + `getModel()` / `ChangeData` / 模型实例字段专项 |
| 8 | 动态字段 / 运行时字段集 | 329 | 4.77% | 1.62% | 188/141 | 字段名来自循环、配置、rowMeta 或拼接；只能救静态常量集合子集 |
| 9 | 字段 key / 分录 key 未消歧 | 218 | 3.16% | 1.08% | 58/160 | 常量冲突、外部常量、分录 key 变量；需补常量扫描或继续 None |

### 2.1 各原因解释

**1. 数据包来源未识别（其他）**

典型写法是本地 `new DynamicObject(...)`、从 `Map`/容器取出、helper 返回值、非 ORM 的工厂方法，或者局部变量没有从事件入参/ORM 查询/已知集合继承来源。字段 key 可能已经解析出来，但承载字段的对象不知道是哪张单据。

源码抽样中还出现了参数化服务或配置查询对象，例如 `GzZipTaskService` 里先按 `entityName` 取配置，再在配置包上读写字段。这类对象不是业务单据入口自然传来的包，不能用反向调用图推断。已完成的字段 key 反查能吃掉一大批这类断链；剩余部分通常需要跨方法返回值数据流、实例字段数据流或更深容器追踪，复杂度和误报风险都高。

**2. 入参调用方未知 / 不可安全收敛**

形如 `void fill(DynamicObject obj)`、`void calc(DynamicObject[] rows)`，当前方法内部能看到字段读写，但不知道调用方传进来的 `obj/rows` 来自哪里。

✅ **已实现"反向调用图回填"（2026-06-27，见 §3.6）**：如果 helper 的项目内调用点能把实参来源安全收敛到同一来源，就把来源沿"实参 -> 形参"传播进来重跑解析、回填 `form_key`（`form_key_source=reverse_callgraph`）；递归、来源冲突、调用方实参源未知时仍留 None。它只吃掉这桶里"可解析调用链 + 来源一致"那部分，其余继续诚实留空。

本次源码复核确认，剩余大头不是传播深度不足，而是调用点本身不属于普通可解析调用链：

- 任务入口/报表入口由框架调度，源码里没有业务单据实参来源。
- 反射调用只传 `Class<?> midClass` 和方法名字符串，例如 `GzZipTaskService.uploadZip(...)` 调 `getDeclaredMethod(...).invoke(...)`。
- 多源报表 helper 同时处理合同、收款单、退款单、转款单等数组，不能把整个方法统一归到一个 form。
- 配置对象如 `GzUploadUtil.postRequest(DynamicObject conf, ...)` 来源是接口配置表，不是当前业务单据。

**3. 仅字段常量备注、无来源说明**

证据里只有 `BaseCon.ID`、`ContractCon.ENTRY_...` 这类字段/分录常量，缺少对象来源。它说明字段标识有线索，但数据包来源没跟上。部分可以被元数据唯一反查解决；无法收敛时不能按常量名猜单据。

**4. ORM 实体名是动态表达式**

例如 `BusinessDataServiceHelper.loadSingle(id, entityName)`、`load(id, getEntityKey())`、字符串拼接实体名。运行时才知道实体名，静态分析不能猜。这里保留 `form_key=None` 是正确行为。

源码抽样中的典型模式是 `GzZipTaskService.uploadZip(String entityName, ...)`：调用方传入实体常量，但服务方法内部用参数 `entityName` 做 `BusinessDataServiceHelper.load(entityName, ...)`，同时又通过反射调用 `midClass.getDeclaredMethod(HeadMethodName, ...)`。这不是继续加深反向调用图能自然解决的问题，需要独立支持"常量实参传播到 ORM entityName"和"反射目标方法解析"，并且要在冲突/动态表达式时继续留 None。

**5. 无 evidence 备注**

通常是老记录或某些 `model.*` / `do.*` 路径没有留下足够来源说明。短期可补的是证据文案和实例字段/模型字段识别；不能仅因为它在某个插件类里出现就默认归属该插件绑定单据，尤其是多绑定和未绑定 service。

**6. 基础资料引用包**

`bill.getDynamicObject("org").set(...)` 这类写的是基础资料对象本身，不是单据表头/分录字段。它可以标 `level=basedata`，但没有明确的业务单据 `form_key`，不应硬塞到当前单据。

**7. model / IDataModel 上下文未定位**

典型写法是 `getModel().setValue(...)`、`getModel().getDataEntity(true)`、`ChangeData.getDataEntity()` 或 `IDataModel model` 形参。它们的来源应来自插件绑定单据、事件上下文或模型实例字段，而不是 `DynamicObject` 实参传播。源码抽样中 `ContractLeaglFormPlugin.propertyChanged(...)` 就同时出现 `ChangeData.getDataEntity()` 和 `getModel().setValue(...)`；这类应作为下一轮高优先级专项。

**8. 动态字段 / 运行时字段集**

字段或实体来自运行时集合、配置、报表元数据或拼接。典型源码是 `ReportBackUpService`：

- `for (String filterField : this.fixedFilterFields) { filterBill.get(filterField) }`
- `for (String fieldName : query.getRowMeta().getFieldNames()) { bak.set(fieldName, row.get(fieldName)) }`
- `BusinessDataServiceHelper.newDynamicObject(this.filterEntity)`

这些字段/实体只有运行时才知道，不能按当前类或调用方强行归属。后续只可安全覆盖"字段集合来自静态数组/常量 List 且实体唯一"的子集。

**9. 字段 key / 分录 key 未消歧**

字段或分录 key 来自拼接、循环变量、外部跨模块常量、未命中常量表的变量等。引擎会记录"动态/外部"原因，但无法反查元数据坐标。

---

## 3. 目前能解析的写法

### 3.1 字段 key 能解析的写法

| 写法 | 结果 |
|------|------|
| 字符串字面量：`setValue("cqkd_amount", v)` / `obj.set("cqkd_amount", v)` | `key_resolution=literal` |
| 类常量：`Const.AMOUNT` | 精确命中常量表，`key_resolution=constant` |
| 裸常量名全工程唯一：`KEY_AMOUNT` | 唯一命中常量表，置信略低 |
| String 形参由调用方传入常量/字面量 | 跨方法传播后解析成真实 key |
| 多值同名常量 | `ambiguous`，记录证据但不替选 |
| 拼接/循环变量/外部常量 | `concat` / `dynamic-loop` / `external-const` / `unknown`，不臆造字段 |

### 3.2 模型 API 写法

能识别 `getModel()` 或已传播进来的模型/视图形参上的：

| API | 类型 | 层级判断 |
|-----|------|----------|
| `setValue` / `setItemValueByID` / `setItemValueByNumber` | 写 | 2 个参数=表头，3 个参数=分录，4 个参数=子分录 |
| `getValue` / `getItemValueByID` / `getItemValueByNumber` | 读 | 1 个参数=表头，2 个参数=分录，3 个参数=子分录 |

来源实体取插件绑定单据、转换目标单，或跨类传进来的模型/视图绑定单据。已覆盖 `helper(IDataModel model)`、`helper(IFormView view)` 这类模型形参写法。

### 3.3 DynamicObject 数据包写法

能识别以下 `obj.set(...)` / `obj.get(...)` / `obj.getString(...)` 等读写：

| 来源写法 | 判定结果 |
|----------|----------|
| 事件入参：`e.getDataEntity()`、`e.getDataEntities()[i]`、`getBizDataEntity()`、`extendedDataEntity.getDataEntity()` | 绑定单据/转换目标单表头 |
| 事件数组：`e.getDataEntities()` 后 for-each/lambda/下标取行 | 绑定单据表头集合元素 |
| ORM 集合：`BusinessDataServiceHelper.load/loadFromCache(...)`、`QueryServiceHelper.query(...)` | 实参实体的表头集合 |
| ORM 单包：`loadSingle/loadSingleFromCache/newDynamicObject/queryOne(...)` | 实参实体的表头包 |
| 转换源单：`getValue(CONVERT_SOURCE)` / `getValue(...SOURCE...)` | 转换源单表头集合 |
| 取分录集合：`x.getDynamicObjectCollection(k)`、`getModel().getEntryEntity(k)` | 继承 `x` 来源，定位 entry/subentry 和 entry_key |
| 取基础资料：`x.getDynamicObject(k)` | `level=basedata`，标基础资料引用包 |
| 集合取行：`coll.get(i)`、`coll.iterator().next()`、`coll.addNew()` | 继承集合元素坐标 |
| 新行类型来自集合：`new DynamicObject(coll.getDynamicObjectType())` | 继承集合元素坐标 |
| 内联新行：`x.getDynamicObjectCollection("e").addNew()` / `.iterator().next()` | 直接识别为该分录元素 |
| 增强 for：`for (DynamicObject row : coll)` | `row` 继承 `coll` 元素坐标 |
| lambda：`coll.forEach(row -> row.set(...))` | lambda 形参继承集合元素坐标 |
| 内联 lambda：`bill.getDynamicObjectCollection("e").forEach(row -> row.set(...))` | 直接从内联集合表达式推分录坐标 |
| stream 派生集合：`coll.stream()...collect/toList/toSet`、`Arrays.stream(coll)` | 继承原集合元素坐标 |
| 泛型集合：`List/Set/Collection<DynamicObject>` 由 `.add(loadSingle(...,"entity"))` 聚合 | 元素来源按 add 进去的已知实体收敛 |
| 根包常见命名：`bill/dataEntity/billObj/info/obj/dynamicObject/data.set(...)` | 在绑定插件内保守按表头，来源=绑定单据 |
| helper 返回值：`row = this.helper(...)` 后 `row.set(...)` | 若 helper return 坐标唯一，调用方局部变量继承返回坐标 |

### 3.4 跨方法 / 跨类传播

从事件入口向被调方法传播：

| 形参类型 | 传播内容 |
|----------|----------|
| `DynamicObject` | `level + entry_key + form_key` |
| `DynamicObject[]` / `DynamicObjectCollection` | 集合元素坐标 |
| `List/Set/Collection<DynamicObject>` | 泛型集合元素来源 |
| `IDataModel` / `IBillModel` / `IFormView` 等模型/视图 | 绑定单据 |
| `String` | 调用方传入的字段 key / 分录 key 字面值 |

传播只传确定信息；没有来源的裸表头上下文不会硬传，避免把 service 误归到某张单据。

### 3.5 数据流断链后的元数据回填

当数据流追不到 `form_key`，但字段 key 已经解析出来，会做三层回填：

| 层级 | 条件 | `form_key_source` |
|------|------|-------------------|
| 字段 key 唯一反查 | 该字段 key 在元数据里只属于一张单据 | `metadata_unique` |
| 绑定收敛 | 字段 key 多候选，但候选与 `access_class` / 入口插件绑定单据交集唯一 | `metadata_binding` |
| 同对象共现交集 | 同一接收者变量连读写多个字段，候选单据交集唯一 | `metadata_cooccur` |

这三类都明确标注为"字段归属元数据反推"，不是冒充数据流来源。多候选不能收敛时继续留 None。

### 3.6 孤立方法反向调用图回填（`reverse_callgraph`，2026-06-27）

正向分析只从「绑定插件事件 / 未绑定苍穹插件根方法」出发跨类 BFS，沿「实参↔形参」传播来源坐标。
**没被任何事件 BFS 覆盖**的孤立 helper（典型 `void fill(DynamicObject o){ o.set("cqkd_x", …) }`）只走
全量孤立补全，DO 形参不知来源 → 写入整片 `form_key=None`。本轮反过来用**反向调用图**补这条断链：

| 条件 | 处理 |
|------|------|
| helper 在项目内被可解析地调用（`_resolve_call` 解得出受者类型，含 `new Helper().m()`） | 纳入反向调用边 |
| 唯一调用方或链式调用方实参来源已知 | 固定点逐跳传播，带 `param_ctx` 重跑 `analyze_method`，把重解析出的来源回填到原 `form_key=None` 行 |
| 多调用点全部推出同一个非空来源 | 视为来源一致，允许回填 |
| 0 个调用方（真孤儿）/ 来源冲突 / 任一调用点来源未知 / 递归自调用 | 一律留 None（红线 #4，不臆造） |

回填只动 `form_key=None` 的行、绝不改写已定位行；`form_key_source=reverse_callgraph` 与数据流/元数据
来源并列（属真实数据流级证据，**排在元数据兜底之前**先定，故 metadata_* 只补它没救到的）；单跳唯一调用方
confidence 最高 `0.85`，链式或多调用方一致最高 `0.80`。实现集中在 `cosmic_kb/java/analyze.py`（`_build_reverse_calls` +
`_propagate_reverse_props` + `_backfill_reverse_calls`，复用 `_resolve_call`/`_callee_prop`/`_RetResolver`/`analyze_method`），
零 schema 改动（v10）。

---

## 4. 仍需诚实留空的边界

以下情况不应该为了提高数字而硬填：

- ORM 实体名来自变量、方法返回值或拼接表达式。
- 字段 key / 分录 key 来自运行时循环、配置、外部常量，静态无法唯一解析。
- 报表 `RowMeta`、配置 Map、数据库参数、系统参数决定的字段集合，除非字段集合能静态收敛为唯一常量集。
- 基础资料对象本身的读写，没有对应单据表头/分录坐标。
- 同一 helper 被多个入口以不同来源调用，且无法唯一确认当前调用来源。
- 任务入口、报表入口、WebAPI 工具入口等由框架调度但源码内没有可解析业务单据实参的场景。
- 多候选字段 key 既无绑定收敛，也无同对象共现交集。
- 反射、`Map` 批量赋值、`BeanUtils` 拷贝、自研 DAO 等非标准 API，除非补明确规则和正反例测试。

> **注解驱动 POJO↔DynamicObject 映射写入（2026-06-28，A 档已识别）**：
> `@…Annotation(value="cqkd_xxx") + convertTo…DynamicObject` 反射 `bill.set(annotation.value(), …)` 这类
> 手写 ORM，字段 key 是注解里的**静态字面量**、可用 KB 字段表反验证，已由 `java/annotation_map.py` 识别并合成
> `field_access` 写入行（via=`annotation-map`）——**写入本身不再隐形**（此前一条 access 都不产生）。其 `form_key`
> 仍走既有「字段 key 反查元数据」回填：唯一→`metadata_unique`、歧义无绑定收敛→**None**（与上列同口径，不臆造）；
> `type="entry"` 时只采用注解里显式的分录容器成员（如 `entryDoName`）作为 `entry_key`，没有则留 None 交元数据回填，
> 绝不把字段 key 当分录容器 key。
> 精确单据坐标（按调用点 `DynamicObjectType` 实参收敛）留 B 档。这与上面的「通用反射留 None」是两件事：前者是
> **写入可见性**（已补），后者是**form_key 来源不可解**（仍 None）。真实库整库重建后的新增行数/NULL 率变化由 Codex
> 复算后回填本节。

---

## 5. 下一步优先级

1. ✅ **孤立方法反向调用图回填（已完成，2026-06-27，见 §3.6）**：原未定位 1,679 条（占未定位 25.0%）里
   "调用链可解析 + 实参来源可安全收敛"那部分已可确定性回填（`reverse_callgraph`）；来源冲突/源未知留 None。
   固定点传播版真实项目重建后回填 218 条（读 148 / 写 70），来源定位 NULL 率 34.07%。本轮未达到 500+ 目标，
   说明剩余缺口不能靠继续放宽反向调用图安全规则解决。
2. **model / IDataModel 上下文专项**：当前互斥桶 423 条（写 277 / 读 146）。覆盖 `getModel().setValue/getValue`、
   `getModel().getDataEntity(true)`、`ChangeData.getDataEntity()`、`IDataModel` 形参和模型实例字段；来源必须来自插件绑定、
   转换目标或事件上下文，未绑定 service 继续 None。
3. **ORM 动态实体常量实参专项**：当前互斥桶 711 条（写 332 / 读 379）。优先处理
   `GzZipTaskService.uploadZip(AssetCardMidCon.ENTITY, ...)` 这类"调用方传实体常量 -> 被调方法参数 `entityName`
   -> `BusinessDataServiceHelper.load(entityName, ...)`"；实体名来自变量/配置/方法返回且无法唯一收敛时继续 None。
4. **反射目标方法解析专项**：配合 #3 处理 `midClass.getDeclaredMethod(HeadMethodName, ...).invoke(...)`，
   仅当 `Class<?>` 和方法名字符串都能在调用点静态唯一确定时传播来源；否则不能把所有反射调用混成一个入口。
5. **动态字段静态集合子集**：当前互斥桶 329 条（写 188 / 读 141）。只救字段集合来自静态数组、常量 `List`、明确枚举的场景；
   `RowMeta.getFieldNames()`、配置 Map、数据库参数、系统参数决定的字段继续留 None。
6. **补 evidence 文案与分桶**：当前互斥桶 616 条无 evidence，先补清楚是 model、do、常量、元数据还是来源断链，
   避免后续评估时再把多个问题混成一个"未定位"桶。
7. **扩充 ORM/写入 API 名单**：只补项目里真实出现、语义确定的 DAO/缓存/批量写 API；每补一类都加正例和负例测试。

> 反向调用图已升级为固定点传播；源码复核显示这条路线已接近安全上限。继续提升识别率应优先转向
> model/IDataModel、ORM 动态实体常量实参、反射唯一目标和动态字段静态集合子集，而不是放宽多调用冲突/未知来源的安全红线。

所有改进继续守红线：能确定就打 `form_key` 和依据；不能确定就保留 `None`，让查询结果进入未定位/存疑桶。
