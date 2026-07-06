# trace 返回详解（"到底是完整还是截断"一次讲清）

> 起因：段二大模型查 `cqkd_ht.cqkd_zdgl.cqkd_qs`，一边说"核心组写/读完整
> （`groups_capped=0`、`capped=0`）"，一边又报"显示层截断 716B / 6493B、unlocated
> 21 条被截、coarse 5 条被截"。看起来自相矛盾，其实是**三种完全不同的"截断"被混成
> 了一个词**。本文把它们拆开，给现状（实测）+ 解决方案。

---

## 一、先分清三种"截断"——它们性质完全不同

| # | 名称 | 谁干的 | 会丢数据吗 | 如何识别 |
|---|------|--------|-----------|---------|
| ① | **summary 真实总数** | 我们（恒保留） | **绝不**（红线 #4） | `summary` 里的计数永远是全量真实值 |
| ② | **设计内 cap（节点裁剪）** | 我们（有意为之） | **不丢、且可逐页取回**：总数在 `*_total`，被截段带 `next_cursor` | `groups_capped` / `capped` / `sites_capped` / `methods_capped` / `locations_capped` 等 > 0；旁边有 `next_cursor` |
| ③ | **host 显示层硬切** | MCP 宿主（被动） | **会丢**：从 JSON 中段一刀切断、结构都破坏 | 返回的 JSON 不合法/中途断在某个类名里（如 `MatchBillUnAuditOp` 中段） |

**关键认知**：
- ② 不是 bug，是**防 ③ 的手段**。它把"无界的明细"主动收敛成有界节点。
- **②"被截"不再只是一个计数——被截段带 `next_cursor`（如 `unlocated@4`），用 `cursor=` 该值再调
  一次 `trace` 就翻到该段下一页，循环到 `next_cursor=null` 即把被截条目一条不漏取回。** 这是
  2026-06-28 的升级：用户指出"只通知有截断、内容拿不到 = 仍丢信息"，确实如此——红线 #4 从"总数不丢"
  升级为"被截条目对消费方**可达**"。（详见第六节。）
- ③ 才是真正危险的——它在传输/显示层把超长字符串从中间一刀剁了，JSON 直接坏掉、信息无声丢失。
  我们做整个 compact governor，目标就是**让返回永远 ≤ host 上限，从根上消灭 ③**。

> 用户那次看到的"显示截断 716B / 6493B"是 **③**；"unlocated 21 条被截 / coarse 5 条被截"
> 是 **②**。模型把这俩并排列在一张"截断检查"表里，才显得"既完整又截断"。**③ 已由 governor 对齐
> host indent 口径消灭（第二节）；② 的被截条目已由游标分页变为可逐页取回（第六节）。**

---

## 二、真正的根因：governor 的字节度量没对齐 host 的序列化方式

> 这是本轮（2026-06-28）才挖到的真根因，**纠正**上一版文档"截断 = 陈旧 server"的判断。
> 用户重连 MCP（新 compact 代码确已生效，返回里有 `groups_total`/`occurrences_total`）后，
> `trace cqkd_ht.cqkd_zdgl.cqkd_qs` **仍被 host 截断 716 字节**（`716 of 33484 bytes elided`）。

逐层拆解为什么"governor 说没超、host 却切了"：

1. **MCP 底层固定用 `indent=2` 序列化。** `mcp/server/lowlevel/server.py`：
   ```python
   unstructured_content = [types.TextContent(type="text", text=json.dumps(results, indent=2))]
   ```
   即工具返回值发到 host 的文本，是 **`json.dumps(result, indent=2)`**（`ensure_ascii` 默认 True）。
2. **indent 缩进让深层嵌套结构膨胀约 35%。** 我们的 trace 返回是「组 → 类 → sites/methods」多层嵌套，
   每层每行都加缩进空白与换行。实测同一个 dict：

   | 度量方式 | 字节 |
   |---------|-----|
   | 无缩进 `json.dumps(ensure_ascii=True)` | **25413**（governor 旧口径） |
   | 无缩进 utf-8 | 23318 |
   | **`indent=2, ensure_ascii=True`（host 真实口径）** | **34347** |

3. **governor 量错了对象。** 旧 governor 用**无缩进** 25413 比预算 30000 → 判"没超"→ 不裁剪
   （所以 `groups_capped=0`、`note=null`，看着"完整"）；但 host 实际发出的是 **34347 字节的缩进版**，
   超 32768 → 从中段硬切 716 字节。**"完整"是 governor 的错觉，③ 是真的，且来自当前代码、与陈旧无关。**

### 修复

- 新增 `field_trace._wire_len(obj)` = `len(json.dumps(obj, ensure_ascii=True, indent=2))`，**与 host
  完全同口径**；governor 改用它度量。预算 `_COMPACT_BUDGET` 调到 **31000**（距 32768 硬上限留 ~1768 裕量）。
- ladder 补一档中间梯级（indent 让步长变粗，多一档把预算用满、少裁次要工作单）。

### 修复后实测（真实库 `cosmic_kb.db`，按 host indent 口径）

| 查法 | access | wire(indent=2) 字节 | < 32768 |
|------|--------|:---:|:---:|
| 坐标查 `cqkd_ht.cqkd_zdgl.cqkd_qs` | 默认 | 29626 | ✅ |
| 坐标查 | write | 30467 | ✅ |
| 坐标查 | read | 27189 | ✅ |
| 裸字段 `cqkd_qs`（全量调） | 默认 | 23201 | ✅ |
| 裸字段 | write | 20951 | ✅ |
| 裸字段 | read | 30610 | ✅ |

- **结论：所有查法均 < 32768 → ③（host 硬切）不会再发生。** 写入插件清单（13 个）始终完整保留
  （`writers.capped=0`）；被裁的只是次要工作单（`unlocated`/`dynamic_writers`/`occurrences`/概览），
  真实总数都在各自 `*_total`/`summary`，要全量走 `access='write'` 或 CLI。

---

## 三、逐字段读懂一次 trace 返回（默认 access）

```
field_key / field_name      被查字段标识 + 已核对中文名（照抄，勿按拼音猜）
filter / precise            本次查询的坐标过滤、是否精确到单坐标
summary                     ★真实总数（coords/writers/readers… 永远全量，红线 #4）
occurrences[] / occurrences_total   元数据定义坐标（消歧菜单）；occ 被裁时 total 给全量
groups[] / groups_total / groups_capped
                            按坐标(单据·层级·分录)分组；单一定义但被跨单据插件读写时
                            groups 仍可能被裁，真实组数 = groups_total，被截 = groups_capped
                            （若字段本身跨单据**定义**且未指定 form_key，trace 直接反问，
                            返回体只有 field_key/status/filter/occurrences/note，不会走到这里）
  └ writers{classes[],capped,…}      该坐标的写入：按类合并，sites=写入点(行号/落库)
  └ readers_overview{…,capped}       默认只回读取的"按类计数概览"
possible{}                  层级/分录存疑的命中（按类合并）
unlocated{methods[],total,capped}    ★来源单据没钉出(form_key=None)的"反推工作单"，
                            按方法去重；total=全量，capped>0 表示清单没列全
dynamic_writers{methods[],total_methods,methods_capped}
                            ★字段 key 钉不出的动态写"该读方法"清单，同样 total 给全量
coarse{locations[],coarse_only,locations_capped}   （access=read 才有）粗扫疑似盲点
note                        被 cap 时在此提示"用 form/entry/level 收窄或单看一侧"
```

**判断"完整 vs 截断"的唯一正确姿势**：
1. 看 `summary` —— 这是真相，全量总数永远在这。
2. 看各节点 `*_total` / `*_capped` —— 若 `capped==0` 且 `len(节点)==total`，该节点**展示也全**；
   若 `capped>0`，是 **②**（设计裁剪，不丢数），按 `note` 收窄重查即可拿全明细。
3. JSON 能完整解析、没断在半个类名里 —— 没有 **③**。

---

## 四、解决方案 / 操作清单

1. **（已修）governor 按 host 真实口径（indent=2）度量。** 见第二节——这才是这次截断的真根因。
   改完务必**重连 / 重启 MCP server**（常驻进程，启动时才载入新码），否则继续跑旧度量、继续被切。
2. **读返回别把 ② 当丢数，被截条目用 `next_cursor` 翻页取回（见第六节）。** `*_capped>0` 旁带
   `next_cursor`（如 `unlocated@4`）：把它原样作 `cursor=` 再调一次 `trace` 即取该段下一页，循环到
   `next_cursor=null` 拿全。也可① 带坐标收窄（`trace('单据.字段')` 或 `form=/entry=/level=`）→ 单坐标
   groups_capped 归零；② `access='read'`/`'write'` 单看一侧，省一半字节。
3. **要彻底全量、一条不裁** → 走 **CLI**：`cosmic_kb trace "单据.分录.字段"`（本地 stdout 无 32KB 限制，
   compact 只为 MCP 防截而生）；动态写入全量审计走 `cosmic_kb dynwrites`。
4. **dynamic_writers / unlocated 是工作单，不是结论。** 它们按方法去重列出"该读方法"，要把"谁改了 X /
   来自哪张单据"答全，对清单逐个读本机源码核实，别把 `plugin_form_label` 当确诊。

---

## 五、设计回顾（compact governor 为什么这样做）

- **写/读拆分**（`access` 参数）：默认回写入明细 + 读取概览；读明细单独 `access='read'`。一次只传一半。
- **按类合并**：散落行按 `access_class`/`plugin_fqn` 塌成有界类节点，每类只出现一次。
- **无界段也逐档收紧**：坐标组 `groups` / `occurrences` / `dynamic_writers` / `coarse` 本身无界
  （字段定义单一但被跨单据插件读写时，证据仍可命中十几张单据），是"全量调"撑爆的主因——governor
  的 ladder 对它们也逐级裁剪，否则 per-class cap 再小也压不下整组体积（这是旧版无法收敛、仍被 ③
  切的洞）。字段本身跨单据**定义**且未指定 `form_key` 的情况已在闸门里提前反问，不会进 ladder。
- **字节 governor（按 host 真实口径）**：构完用 `_wire_len`（= `json.dumps(ensure_ascii=True, indent=2)`，
  与 MCP 底层 `mcp/server/lowlevel/server.py` 同口径）量字节，超 `budget=31000` 就沿 ladder 收紧 cap
  重建，直至 ≤ budget。**必须含 indent**——缩进让深层嵌套膨胀 ~35%，只量无缩进会低估而被 host 切（本轮真根因）。
- **红线 #4**：无论怎么裁，真实总数恒在 `summary` 与各节点 `*_total`，截掉量在 `*_capped`，绝不丢数；
  **更进一步，被截条目都可经游标分页逐页取回（第六节），对消费方可达**。

---

## 六、游标分页：被 cap 的条目「逐页取回」（2026-06-28）

> 起因：用户指出"`*_capped>0` 时被截的那几条，大模型只看到计数、内容拿不到——这不还是丢信息？"
> 完全成立。32KB 硬上限下单次装不全是物理约束，唯一既不丢又**可达**的办法是**分页**。

**机制**：
1. **overview 标记**：某段被 cap 时，在它旁边写一个 `next_cursor`，形如 `"unlocated@4"`
   （段名 + 已展示条数=下一页起点）。可分页段：`writers` / `readers` / `unlocated` /
   `dynamic_writers` / `possible` / `coarse` / `occurrences`。
2. **翻页取下一页**：把该 `next_cursor` 原样作 `cursor=` 再调一次
   `trace(field, ..., cursor='unlocated@4')`。返回变成聚焦单段的一页：
   ```
   page: { section, offset, returned, total, items:[...该段从 offset 起、预算内能装下的下一页],
           next_cursor: "unlocated@9" | null }
   ```
3. **循环到底**：一直用上一页的 `page.next_cursor` 翻，直到它为 `null`，即把该段
   **全部条目一条不漏取回**（不是只有计数）。每页本身也过 `_wire_len` 预算，恒 < 32768。

**注意**：
- `writers`/`readers` 是嵌套在坐标组里的段，**仅单坐标（精确查、命中单个坐标组）可分页**；裸字段命中
  多单据时分页会回 `page.error` 提示先用 `form/entry/level` 收窄——因为跨坐标的 writers 没有统一序。
- `groups`（坐标组）本身**不分页**：它是"消歧菜单"，正确动作是带坐标收窄到你要的那张单据，而非翻十几个
  单据的整组明细。
- 分页只为 MCP 防截而生；CLI（stdout 无 32KB 限制）一次给全，无需翻页。

**实现**：`field_trace._page_section`（按 `_section_full` 取该段完整有序列表 → 从 offset 起逐条按
`_wire_len` 预算装入 → 算 next_cursor）；`_annotate_next_cursors` 给 overview 被截段补 `next_cursor`。
`dynamic_writers` 为此在 `_collect_materials` 里多存一份完整折叠清单 `dynamic_writers_full`（共享 dict
仍只展示前 10），否则翻不到第 11 条。MCP `tool_trace` 增 `cursor` 形参，`INSTRUCTIONS` 加翻页纪律。
