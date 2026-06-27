# form_key 解析率 · 成因分析与待办（下次接手先读这个）

> **进度（2026-06-27 更新）**：✅ **待办一（字段key反查回填三层）+ 待办二（addNew/new DynamicObject 习语）已完成并验收**
> （schema v10 加 `form_key_source` 列，`analyze._backfill_form_key`，240 passed）。真实库 form_key NULL
> **56.1%→34.6%**（write 52.1%→27.4%），回填 4153 行（唯一2167+绑定1234+共现752）、0 改写既有 data_flow。
> 详见 `docs/阶段验收.md`「form_key 解析率提升（字段key反查回填三层 + addNew 习语）」。
> **下次接手做这里仍 ⬜ 的**：② 孤立方法反向调用图回填（最大杠杆，跨方法需防环/防爆）；③④ 是诚实未知，不做。
> 以下成因分析按当时（NULL 56.1%）的库写就，留作背景与剩余项依据。


> 背景：`field_access.form_key`（数据包来源实体）是 `read_source` 同名跨单据三档消歧的收敛依据，
> None 越多 → 越容易平铺候选诱导段二模型脑补归属。2026-06-27 完成「绑定回落 + 泛型集合建模」
> 后（NULL 60.3%→56.1%），对**剩余仍为 None** 的行做了一次数据驱动成因分析，结论记此，下次再处理。
>
> 分析基于真实库 `D:\kingdee\asset_management_sys\cosmic_kb_new2.db`（新代码重建）。

## 现状：form_key 仍为 None 共 10880 行（write 3589 + read 7291）

按"来源单据未定位"的成因归一分桶（取 `field_access.evidence` 中来源相关短语）：

| # | 成因 | 行数 | 占比 | write/read | 可救否 |
|---|------|------|------|-----------|--------|
| ① | **数据包来源未识别**：DynamicObject 由本地 `new`/`getModel`/字面构造而来，既非事件入参也非 ORM 查询结果 | 2965 | 27.3% | 1015/1950 | 部分（需深挖返回值数据流，收益递减） |
| ② | **孤立方法的 DynamicObject 入参**：helper 方法形参收 DO，但调用方未知 | 2545 | 23.4% | 601/1944 | **是（下一个最大杠杆）** |
| ⑥ | **仅字段常量备注、无来源说明**：多为未绑定的 service/task 类 `execute` 里本地构造的 DO | 1759 | 16.2% | 589/1170 | 少量（同①类） |
| ⑤ | **无 evidence 备注**：form 插件 `model.setValue`/`propertyChanged` + `do.set/get`，来源没解出 | 1451 | 13.3% | 707/744 | 部分（多绑单据/未绑定时无法定唯一） |
| ③ | **ORM 实体名是动态表达式**：`loadSingle(id, 变量/拼接)`，实体名不是字面量 | 1211 | 11.1% | 657/554 | 否（静态钉不出，红线#4） |
| ④ | **基础资料引用包**：写入归基础资料实体本身，无单据表头 | 949 | 8.7% | 20/929 | 否（本就正确，非失败） |

## 三类定性（哪些该留 None，哪些值得做）

**A. 真·静态钉不出 = 诚实未知（红线#4，应留 None，不是 bug）—— ③+④ ≈ 2160 行（20%）**
- ③ `loadSingle(id, entityName)` 里 `entityName` 是变量/字符串拼接，运行时才定，静态展开 = 臆造。
- ④ 基础资料引用包 929/949 是读，写入归基础资料实体本身、本就无单据表头坐标，标 None 是正确行为。

**B. 下一个真正值得做的杠杆 —— ② 孤立方法 DO 入参（2545 行，23%）**
- 这些是 helper `void fill(DynamicObject obj)`。本次"绑定回落"只覆盖了**已绑定插件**的第②轮孤立 helper；
  这 2545 行是**调用方未知的纯孤立方法**。
- 可救路径：**反向调用图**——若该 helper 在项目内**唯一被某处调用**且调用点实参来源已知，
  就能把来源单据沿"实参→形参"反向传播进来。确定性可做、不臆造（多调用点/来源未知则仍留 None）。
- 提示：项目里已有 `call_graph` / `method_calls`（出向）。本待办是它的**反向**用途——按 target
  方法聚合调用点，唯一调用点时回填 ctx。需防环、防上下文爆炸。

**C. 收益递减、不建议硬挖 —— ①⑤⑥（合计 6175 行，57%）**
- 本质都是"DynamicObject 在本地 `new`/`getModel`/helper 返回值造出来，来源链断在我们不追的返回值上"。
- 继续挖要做**跨方法返回值数据流**，复杂度高、误报风险大，与红线#4 抵触（易猜错单据）。
- ⑤ 里 form 插件的 `model.*` 只有插件**唯一绑定**时才能定来源，多绑/未绑就该是 None。

## ★ 待办一（最高优先级）✅ 已完成：字段 key 反查元数据 + 绑定/共现收敛回填 form_key

> 2026-06-27 二次分析新增。**专治 ①⑤⑥ 这一整类"返回值/容器断链"，且对数据流断链免疫**——
> 不管 DO 是 Map 掏的、helper 返回的、`new` 的、stream 出来的，只要最终 `.set(已知字段key)` 就能反推。

**原理（硬约束，非臆造）**：一个 DynamicObject 物理上不可能 `.set("cqkd_xxx", ...)`，除非它的实体
类型声明了 `cqkd_xxx`。所以当数据流追不到来源时，反过来用**被写的字段 key**问元数据 `field` 表
（`key → form_key/entity_key/level`），即可反推数据包来源实体 + 分录坐标。

**三层收敛（逐层把歧义塌缩，仍解不出就留 None）**：
1. **字段 key 唯一反查**（主力）：该 key 在元数据里只归一个单据 → 直接定 form_key + level + entry_key。
2. **绑定收敛**：key 归多单据（歧义），但写它的 `access_class` 是某单据的 `linked` 绑定插件、且该单据
   在候选集中 → 定它。
3. **同对象共现交集**：同一个 DO 变量在同方法里连写多个字段 → 候选 form 集合**取交集**。
   （例：`ContractAdjustFormPlugin.adjustMonthBill` 里 `subRow` 连写三字段，line 142/143 各自唯一归
   `cqkd_tzjezd`，line 141 单看 3 候选歧义，交集一取也塌缩成 `cqkd_tzjezd.subentry.cqkd_sonbill`。）
   注：需在 `field_access` 记录**接收者变量名**或在分析期就地按变量分组求交（当前未存接收者变量）。

**真实库量化（form_key=None 的 3589 行写访问）**：

| 手段 | 可救回 | 占比 |
|------|--------|------|
| ① 字段 key 唯一反查 | 787 | 21.9% |
| ② + 绑定收敛 | +263 | +7.3% |
| **①②合计（已验证）** | **1050** | **29.3%** |
| ③ 共现交集（再吃掉部分歧义的 1439，未单独量化） | 更多 | — |

留 None 的（正确）：歧义且无绑定无共现（~40%）、字段 key 本身 None/外部常量（~30%）。

**诚实性处理（红线#4，务必做）**：这是**元数据反推**不是数据流证明，要和现有 form_key 区分：
- 打**独立 evidence 备注**（如「来源由字段key反查元数据推得，数据流未追到」）+ **标置信档**：
  唯一反查=高（物理硬约束）、绑定/共现收敛=中。
- `read_source` 三档消歧里这种来源算 `resolved`，但注明依据是「字段归属」而非「loadSingle 行号」，不冒充数据流。

## 待办二 ✅ 已完成：`addNew()` / `new DynamicObject(coll.getType())` 习语回填（局部、低风险）

> `addNew()` 本已覆盖；本轮补 `new DynamicObject(coll.getDynamicObjectType())`（`field_access._NEW_DO_OF_COLL_RE`）。


> 从 ① 类样本里挖出的另一条边界清晰的确定性习语，比待办一更局部（单方法内即可解）。

**两个习语，元素归属"该集合所代表的分录实体"**：
- `DynamicObject row = someColl.addNew();` —— `row` 是 `someColl` 这个分录集合的新行。
- `DynamicObject row = new DynamicObject(someColl.getDynamicObjectType());` —— 同理，type 来自集合。

只要 `someColl` 来自 `X.getDynamicObjectCollection("cqkd_entryX")`，新行的 form_key = `X` 的来源单据、
entry_key = `cqkd_entryX`、level = 分录。难点：集合 owner `X`（`contract`/`cqkd_skdb` 等）本身的来源
有时仍需另解（与待办一/②叠加时由它们补）。

**真实样本**：
- `AmountSummaryUtils.java:235-241` —— `totmoneycolletion.addNew()` 后连写 `cqkd_typeodcost` 等。
- `PushSkFormPlugin.java:86-90` —— `new DynamicObject(cqkd_skd_ht.getDynamicObjectType())` 后写 `pkid` 等。

实现提示：在 `field_access._build_contexts` 里给 `coll.addNew()` / `new DynamicObject(coll.getType())`
的接收变量建 `_Ctx`，entity 取该集合（`getDynamicObjectCollection("...")`）已解析的来源/分录 key；
集合 owner 未解析则元素也留 None。注意与现有 `.add()` 累积逻辑共存、不冲突。

## 下次行动建议（按性价比排序）

1. **★ 待办一：字段 key 反查 + 绑定/共现收敛**——量最大（≥29% None 写）、对返回值/容器断链免疫、零臆造。**首选。**
2. **待办二：`addNew`/`new DynamicObject(coll.getType())` 习语**——局部、低风险，可与待办一叠加再救一批。
3. **② 孤立方法反向调用图回填**——确定性可做（~2545 行），但跨方法、需防环/防爆，复杂度高于上两条。
4. ③④ 不动（诚实未知，标 None 正确）。
5. 纯返回值跨方法数据流暂缓（收益已被待办一大量覆盖，硬挖剩余收益低、臆造风险高）。

## 复现命令

```powershell
# 重建带新代码的库（动你项目目录，按需）
cosmic_kb build "D:\kingdee\asset_management_sys" "<dym|zip|目录>"
# 成因分桶脚本见本次会话；核心是按 field_access.evidence 中来源短语归一分桶 form_key IS NULL 的行
```
