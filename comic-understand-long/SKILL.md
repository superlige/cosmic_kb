---
name: comic-understand-long
description: "金蝶云苍穹历史项目「理解」Skill（非代码生成）。用于接手陌生苍穹老项目时，在本地基于元数据与 Java 源码做证据化追溯：某业务字段被哪些插件/服务/任务修改、某插件事件在什么前后端时机触发、某字段属于主实体/分录/子分录/基础资料哪条数据路径、某操作按钮影响哪些实体字段、改动后是否真的入库、单据间引用/下推(BOTP)/审核回写等业务流，以及生成项目地图与接手者理解报告。所有结论必须带类/方法/事件/行号证据与 confirmed/likely/unknown 置信度，宁标 unknown 不臆造，公司代码只在本机处理、不外传。"
---

# 苍穹项目理解 (Cosmic Project Understanding)

**定位**：本 Skill 是「**理解既有苍穹项目**」的工具，不是代码生成器。
区别于会写插件代码的 ok-cosmic —— 这里只做**读、查、追溯、解释**，输出永远带证据。

使用场景：在自己电脑上接手一个已存在的苍穹老项目（多模块、成百上千文件、可能不可编译、
中文 GBK 编码、多 ISV 前缀），需要先搞清楚"这项目干嘛的、有哪些模块、某字段谁改的、
某 bug 先查哪、改一处影响哪"。

---

## 核心纪律（最高优先级，任何回答都要遵守）

1. **证据优先，禁止臆造**。每一条关于字段/方法/类/事件/入库的结论，都必须能落到
   「脚本查询结果」或「源码文件:行号」或「元数据」上。**没查到就说没查到，不要编**字段名、
   方法名、类名。苍穹幻觉名详见 [rules/anti-patterns.md](rules/anti-patterns.md)。
2. **三态置信度**：每个判断标 `confirmed` / `likely` / `unknown` 并给原因。老项目天生不完整，
   宁可标 `unknown` 也不硬猜。尤其是"是否入库"这类，缺保存链路就判 `unknown`。
3. **本地离线**：公司代码只在本机分析。不要把整库源码贴到外部，AI 只按问题取**最小证据集**。
4. **不生成业务代码**。用户要的是"理解"，不是"写插件"。除非明确要求，否则不产出 Java 实现。
5. **野生代码假设**：代码可能不可编译、缺依赖、混编码、多前缀。遇到不认识的符号
   （`SaveServiceHelper`、`kd.bos.*`）一律当**外部已知平台符号**，用 SDK 文档解释，**不当错误**。

---

## 两段式架构（KB 是契约）

```
段一：本地确定性扫描器  cosmic_kb（Python 包，在项目根）
      Ingestion 摄取 → Metadata 解析 → Java 静态分析 → 桥接 → SQLite KB + 覆盖率/理解报告
                          ↓  KB 是契约
段二：AI 理解层（本 Skill）
      查 KB 取证 → 挂本 Skill 苍穹语义 → 输出带证据的解释 / 排查建议
```

- **段一**由 `cosmic_kb` 包负责（详见项目根 `docs/开发计划.md` 各阶段）。**当前处于阶段 0/1**，
  KB 尚在搭建，不要假设 `cosmic_kb ingest/report/ask` 已可用——只调用「下方明确列出的现有命令」。
- **段二**是本 Skill：提供苍穹语义（插件类型、事件时机、SDK 含义、入库判断规则）和现成查询脚本。

---

## 当前可用工具（阶段 0，已迁移到本 Skill）

入口：`scripts/cqkd_cosmic_understand.py`，统一封装下列只读取证能力。

```powershell
# 自检：确认本 Skill 的脚本/references/配置是否就位
python scripts\cqkd_cosmic_understand.py doctor

# 列出 / 读取苍穹语义参考文档（按主题）
python scripts\cqkd_cosmic_understand.py refs list
python scripts\cqkd_cosmic_understand.py refs read dynamic-object

# 在项目源码里扫某个字段标识的直接命中（带 读/写/过滤 粗分类 + 行号）
python scripts\cqkd_cosmic_understand.py scan-field --field-key cqkd_mortgagestatus --project-root <项目根>

# 查苍穹 SDK 类/方法签名（透传到 cosmic-api-knowledge.py，需 ok-cosmic 文档库）
python scripts\cqkd_cosmic_understand.py api search BusinessDataServiceHelper

# 查单据元数据字段（透传到 cosmic-form-metadata.py，需配置元数据来源）
python scripts\cqkd_cosmic_understand.py meta get --form-id cqkd_assetcard --fuzzy mortgage
```

> `api` / `meta` 依赖 SDK 文档库与元数据来源配置（见 `README.md` 的"启用"一节）。
> 若 `doctor` 报缺，先按提示补 `.cosmic-understand/config.json` 与资产，再用这两条。

---

## 理解工作流（接手者视角）

按"先看全貌 → 再追单点 → 永远带证据"推进。

### A. 先建立项目全貌
1. `doctor` 确认工具与资产就位。
2.（KB 就绪后由段一产出）项目地图 / 理解报告：模块清单、实体清单、插件清单、风险热点。
   阶段 0/1 KB 未就绪时，用 `scan-field` + 源码阅读 + 元数据脚本手工拼全貌，并**显式说明这是手工初判、非 KB 结论**。

### B. 字段追溯："某字段是谁改的？"
1. **定位字段**：中文名 → 标识。用 `meta` 查元数据确认字段标识、所属实体、是主实体/分录/子分录/基础资料字段（**先分清数据路径，避免同名字段串实体**）。
2. **扫写入点**：`scan-field --field-key <标识>`，看 `possible-write`（`setValue` / `.set(`）命中。
3. **判事件与时机**：命中所在类是什么插件类型、在哪个事件方法里（`propertyChanged` / `beforeDoOperation` / `afterExecuteOperationTransaction` / `validate` …）。不确定事件语义就 `refs read` 对应文档。
4. **判是否入库**：见下方"入库判断"。
5. **给结论**：类 + 方法 + 事件 + 行号 + 入库置信度，逐条带证据。

### C. 插件解释："这个 propertyChanged 做了什么？"
- 先确认插件类型（决定能力边界）→ 读取/写入了哪些字段（带路径）→ 调了哪些服务 →
  是页面赋值还是可能入库 → 业务含义 → 风险点 → 证据行号。
- 插件类型与事件边界先查 references（下方路由表）。

### D. 操作影响："某操作会影响哪些字段？"
- 操作 operationKey → 绑定哪个操作插件 → 插件在事务事件里写了哪些实体字段 →
  是否创建/写回其它单据 → 影响置信度。

---

## 回答格式要求（结构化、可核对）

字段/插件/操作类问题，按结构化块输出，缺项标 `unknown`，禁止留空靠想象补：

```
实体：cqkd_assetcard（资产卡片）
字段：cqkd_mortgagestatus（抵押状态）｜路径：主实体字段
写入点：
  - <类>#<方法>  事件=<event>  行号=<file:line>  入库=<confirmed|likely|unknown>（原因）
读取点：...
插件类型：...
事件触发时机：...
是否可能入库：confirmed | likely | unknown（原因）
代码证据：<file:line> 逐条
风险/排查建议：...
置信度：confirmed | likely | unknown
```

---

## 苍穹语义路由（references，按需读 1–2 个，别全量加载）

插件类型 / 事件边界：
- 表单/字段联动 → [plugin-form.md](references/base/plugin/plugin-form.md)；单据 → [plugin-bill.md](references/base/plugin/plugin-bill.md)
- 列表/批量 → [plugin-list.md](references/base/plugin/plugin-list.md)；树列表 → [plugin-tree-list.md](references/base/plugin/plugin-tree-list.md)
- 操作/审核/保存/校验 → [plugin-operation.md](references/base/plugin/plugin-operation.md)
- 下推/选单/转换 → [plugin-botp.md](references/base/plugin/plugin-botp.md)；反写 → [plugin-writeback.md](references/base/plugin/plugin-writeback.md)
- 后台任务 → [plugin-task.md](references/base/plugin/plugin-task.md)；工作流 → [plugin-workflow.md](references/base/plugin/plugin-workflow.md)；导入 → [plugin-import.md](references/base/plugin/plugin-import.md)

能力语义（封装层，理解代码意图时参考）：
- 保存/提交/审核链路 → [operate-chain.md](references/adv/operate-chain.md)
- 下推/来源追踪 → [botp-convert.md](references/adv/botp-convert.md)
- 查询/聚合 → [query-dataset.md](references/adv/query-dataset.md)
- DynamicObject 取值/路径 → [dynamic-object.md](references/adv/dynamic-object.md)
- 实体元数据/字段路径/DBRoute → [entity-metadata.md](references/adv/entity-metadata.md)
- 表单控件/元数据读取 → [form-utils.md](references/adv/form-utils.md)；弹性域 → [flex-prop.md](references/adv/flex-prop.md)
- 附件 → [attachment-api.md](references/adv/attachment-api.md)；跨线程上下文 → [request-context.md](references/adv/request-context.md)

原生 SDK 兜底（看不懂某 `kd.bos.*` 符号时）：
- ORM/QFilter → [sdk-orm-access.md](references/base/sdk/sdk-orm-access.md)
- DynamicObject → [sdk-dynamic-object.md](references/base/sdk/sdk-dynamic-object.md)
- 实体模型 → [sdk-entity-model.md](references/base/sdk/sdk-entity-model.md)
- 事务 → [sdk-tx.md](references/base/sdk/sdk-tx.md)；其余见 `refs list`。

---

## DynamicObject 路径判定（避免同名字段串实体）

判一个字段标识属于哪条路径，是理解的核心难点：

```
主实体字段     bill.getString("cqkd_xxx")              → cqkd_assetcard.cqkd_xxx
分录字段       getDynamicObjectCollection("entry") 的 row.set("cqkd_qz", v)
              → cqkd_assetcard.cqkd_entryentity.cqkd_qz
子分录字段     entry → subentry 再取
基础资料字段   bill.getDynamicObject("cqkd_customer").getString("name")
              → cqkd_assetcard.cqkd_customer.name（基础资料内部属性，非本单字段）
```

同名字段在不同实体都出现时，**结合元数据（`meta`）消歧**；跨方法不可解的标 `unknown`。
细节读 [dynamic-object.md](references/adv/dynamic-object.md) 与 [entity-metadata.md](references/adv/entity-metadata.md)。

---

## 入库判断（输出三态，不是 true/false）

| 判定 | 依据 |
|------|------|
| `confirmed` | set 后同方法内 `SaveServiceHelper.save` / 明确显式保存 |
| `likely` | set 后调封装 save / `OperationServiceHelper.executeOperate`；或操作插件事务事件里改 `e.getDataEntities()`（事务自动保存） |
| `unknown` | 只 `setValue`/`DynamicObject.set` 没找到保存链路；只改方法参数；保存链路跨方法不可解 |

**关键纪律**：`getModel().setValue` 是页面赋值、`DynamicObject.set` 是内存改包，**都不等于入库**。
找不到保存就判 `unknown` 并说明"未在当前证据内发现保存链路"，不要乐观判 confirmed。

---

## 多 ISV / 前缀命名空间

项目常混用 `cqkd_` / `cqspb` / `kd_` 等前缀，且**类名前缀（如 cqspb）与字段包名前缀（如 cqkd_）可能不一致**。
归属判断要分别看包名前缀映射与字段标识前缀，**不要因为前缀不同就误判不属于本项目**，也不要把不同前缀的同名字段当成一个。

---

## 子文档

- 苍穹幻觉方法名/类名/场景错配黑名单 → [rules/anti-patterns.md](rules/anti-patterns.md)
- 本地参考资料主题路由 → [references/README.md](references/README.md)
- 段一扫描器与各阶段计划 → 项目根 `docs/开发计划.md`、`docs/项目企划.md`
