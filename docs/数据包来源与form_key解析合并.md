# 数据包来源与 form_key 解析合并说明

> 合并来源：`docs/form_key解析待办.md` + `docs/数据包来源解析.md`。
> 目的：把"当前能解析哪些写法"、"还有多少未定位"、"为什么未定位"放在同一份交接文档里。
>
> 统计口径：以下数量按仓库当前随附 `D:\codex\cqkd_ai\cosmic_kb.db` 的 `field_access`
> 复算。注意：`docs/阶段验收.md` 记录过后续"提高字段扫描率 1+2+3"重建后的口径
> `form_key NULL 34.6% -> 34.4%`、写访问 `27.4% -> 26.7%`；当前随附库仍是
> schema v10 字段 key 反查回填后的 34.6% 口径。

---

## 1. 当前识别情况

`form_key` 表示一条字段读写的"数据包来源实体/单据"。它是 `read_source` 对同名字段做单据归属收敛的关键：
`form_key` 越多，越能明确"这个字段是在这张单据/这个分录被读写"；`form_key=None` 时只能进入未定位/存疑桶。

| 口径 | 总数 | 未定位数 | 未定位占比 |
|------|------|----------|------------|
| 全部字段读写 | 19,399 | 6,713 | 34.6% |
| 写访问 write | 6,895 | 1,889 | 27.4% |
| 读访问 read | 12,504 | 4,824 | 38.6% |

已定位 12,686 条，占 65.4%。其中来源依据分布如下：

| 来源依据 | 数量 | 占全部读写 | 说明 |
|----------|------|------------|------|
| `data_flow` | 8,533 | 44.0% | 由事件入参、ORM、转换源/目标、集合/行变量、跨方法传播等数据流解析得到 |
| `metadata_unique` | 2,167 | 11.2% | 数据流断链，但字段 key 在元数据里只归一张单据，反查唯一 |
| `metadata_binding` | 1,234 | 6.4% | 字段 key 多候选，但与插件绑定单据取交后唯一 |
| `metadata_cooccur` | 752 | 3.9% | 同一接收者变量连写/连读多个字段，候选单据交集唯一 |

---

## 2. 未定位原因分桶

当前 `form_key=None` 共 6,713 条。分桶按 `field_access.evidence` 的来源说明归一，优先区分"能继续做"和"应该诚实留空"。

| # | 未定位原因 | 数量 | 占未定位 | 占全部 | write/read | 后续判断 |
|---|------------|------|----------|--------|------------|----------|
| 1 | 数据包来源未识别 | 1,953 | 29.1% | 10.1% | 571/1,382 | 可少量继续救，但收益递减 |
| 2 | 孤立方法 DynamicObject 入参 | 1,679 | 25.0% | 8.7% | 373/1,306 | 下一个最大杠杆 |
| 3 | 仅字段常量备注、无来源说明 | 801 | 11.9% | 4.1% | 178/623 | 多数只能留存疑 |
| 4 | ORM 实体名是动态表达式 | 780 | 11.6% | 4.0% | 397/383 | 静态不可钉，正确留 None |
| 5 | 无 evidence 备注 | 742 | 11.1% | 3.8% | 285/457 | 可补证据与局部规则 |
| 6 | 基础资料引用包 | 599 | 8.9% | 3.1% | 15/584 | 不是失败，本就无单据表头坐标 |
| 7 | 字段 key / 分录 key 动态或外部 | 159 | 2.4% | 0.8% | 70/89 | 字段或分录标识本身静态不可解 |

### 2.1 各原因解释

**1. 数据包来源未识别**

典型写法是本地 `new DynamicObject(...)`、从 `Map`/容器取出、helper 返回值、非 ORM 的工厂方法，或者局部变量没有从事件入参/ORM 查询/已知集合继承来源。字段 key 可能已经解析出来，但承载字段的对象不知道是哪张单据。

已完成的字段 key 反查能吃掉一大批这类断链；剩余部分通常需要跨方法返回值数据流、实例字段数据流或更深容器追踪，复杂度和误报风险都高。

**2. 孤立方法 DynamicObject 入参**

形如 `void fill(DynamicObject obj)`、`void calc(DynamicObject[] rows)`，当前方法内部能看到字段读写，但不知道调用方传进来的 `obj/rows` 来自哪里。

最值得继续做的是"反向调用图回填"：如果 helper 在项目内唯一被调用，并且调用点实参来源已知，就把来源沿"实参 -> 形参"传播进来；多调用点、递归、来源不一致时仍留 None。

**3. 仅字段常量备注、无来源说明**

证据里只有 `BaseCon.ID`、`ContractCon.ENTRY_...` 这类字段/分录常量，缺少对象来源。它说明字段标识有线索，但数据包来源没跟上。部分可以被元数据唯一反查解决；无法收敛时不能按常量名猜单据。

**4. ORM 实体名是动态表达式**

例如 `BusinessDataServiceHelper.loadSingle(id, entityName)`、`load(id, getEntityKey())`、字符串拼接实体名。运行时才知道实体名，静态分析不能猜。这里保留 `form_key=None` 是正确行为。

**5. 无 evidence 备注**

通常是老记录或某些 `model.*` / `do.*` 路径没有留下足够来源说明。短期可补的是证据文案和实例字段/模型字段识别；不能仅因为它在某个插件类里出现就默认归属该插件绑定单据，尤其是多绑定和未绑定 service。

**6. 基础资料引用包**

`bill.getDynamicObject("org").set(...)` 这类写的是基础资料对象本身，不是单据表头/分录字段。它可以标 `level=basedata`，但没有明确的业务单据 `form_key`，不应硬塞到当前单据。

**7. 字段 key / 分录 key 动态或外部**

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

---

## 4. 仍需诚实留空的边界

以下情况不应该为了提高数字而硬填：

- ORM 实体名来自变量、方法返回值或拼接表达式。
- 字段 key / 分录 key 来自运行时循环、配置、外部常量，静态无法唯一解析。
- 基础资料对象本身的读写，没有对应单据表头/分录坐标。
- 同一 helper 被多个入口以不同来源调用，且无法唯一确认当前调用来源。
- 多候选字段 key 既无绑定收敛，也无同对象共现交集。
- 反射、`Map` 批量赋值、`BeanUtils` 拷贝、自研 DAO 等非标准 API，除非补明确规则。

---

## 5. 下一步优先级

1. **孤立方法反向调用图回填**：当前未定位 1,679 条，占未定位 25.0%。唯一调用点 + 实参来源已知时可确定性回填；多调用点/来源冲突留 None。
2. **实例字段级数据流**：覆盖 `this.model = getModel()`、`this.bill = ...` 后跨方法使用的场景，重点补模型实例字段和 DynamicObject 实例字段。
3. **补 evidence 文案与分桶**：当前还有 742 条无 evidence，先让未定位原因更可解释，再决定是否值得救。
4. **扩充 ORM/写入 API 名单**：只补项目里真实出现、语义确定的 DAO/缓存/批量写 API；每补一类都加正例和负例测试。

所有改进继续守红线：能确定就打 `form_key` 和依据；不能确定就保留 `None`，让查询结果进入未定位/存疑桶。
