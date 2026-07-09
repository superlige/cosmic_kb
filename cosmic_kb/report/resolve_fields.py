"""字段名核对 · 标识 → 真实元数据中文名+坐标（防命名惯例臆断）。

起因：段二大模型读 Java 源码时靠**命名惯例猜字段中文名**翻车（`cqkd_zjjnqk` 被猜成
"资金缴纳情况"，真实是"租金缴纳情况"）。现有能查字段名的工具全是"重"的（`trace`/`bill`/`ask`，
payload 大、语义是"谁改了它"而非"它叫什么"），模型不会为确认一个中文名去调，于是走阻力最小的
路：猜。本模块补一个 O(1)、专做"标识 → 真实中文名"的轻量取证：批量传 key，直接打词典层，
回最小包，**钉不出回 `None`（诚实留白，不臆造）**。

返回形状：`{"resolved": {key: [item, ...] | None, ...}, "mismatched_form": {key: {...}, ...}}`。
同一 key 可能同时命中 `field` 表（字段定义）、`entity` 表（分录容器）、`form` 表（单据本身），
故每个 key 回扁平 list，每个 item 自带 `kind` 判别：
  · 字段命中  —— `{kind:"field", name, form_key, entity_key, level, field_kind, field_kind_label}`
                （`field_kind` = field 表的 kind 列：entity/dynamic/basedata_prop/...；
                `field_kind_label` 是随附的中文标签，不用去翻 metadata/model.py 猜码值含义）
                下拉/枚举字段额外带 `combo_items:[{value,caption},...]`（存储值→中文含义）；
                基础资料/组织等引用字段命中目标单据时额外带 `ref_entity:{form_key,name}`，
                查不到目标单据（不在本次建库范围内）时退化为 `ref_entity_id`（原始 oid，
                诚实留痕不猜，2026-07 增强）。
  · 容器命中  —— `{kind:"entry"|"subentry"|"header", name, form_key, level, parent_key}`
                （`kind` 取 entity 表的 level，让模型识别"这是分录容器 key 不是字段 key"；
                覆盖表头/分录/子分录三档，与字段侧对称）
  · 单据命中  —— `{kind:"form", name, form_key, form_type}`
                （`form` 表本身；模型读到 `.load("cqkd_invoic_apply", ...)` 这类单据标识
                时同样要核实，不能因为它不是"字段"就绕过——2026-07-05 复盘：真实排障中模型
                对这类标识无工具可查，只能凭字面翻译，此处补上）

**实体限定精确匹配**（2026-07-05）：起因是 `read_source` 的"工具自动消歧"复杂度收益比不划算——
真实库量化（`docs/参考手册/read_source字段名解析逻辑.md` §5）显示结构性漏判修复收益 <1.1% 且有误判风险。
改为反过来：模型自己读源码能看到 `.load("cqkd_zkd", ...)` 这类实体字面量，直接把 key 写成复合
限定符传入，工具过滤候选做精确匹配，不必再靠文件级数据流去猜。

**复合限定符语法**（2026-07-05 复盘扩展）：与 `field_trace.parse_locator` 同一套点号坐标惯例
（`单据.字段`/`分录.字段`/`单据.分录.字段`），不再只认 `单据.字段` 两段式——真实排障发现模型
习惯照搬 `trace` 的三段式写法（如 `"cqkd_invoic_apply.cqkd_invoiceentry.cqkd_invoiceid"`）或
直接传分录限定（如 `"cqkd_invoiceentry.cqkd_invoiceid"`），老逻辑只认两段式且限定符必须命中
`form` 表，两种写法全部落空返回 `null`，模型误以为字段确实没登记，其实是工具语法太窄。判定逻辑
见 `_split_qualified`；过滤后有候选就返回过滤结果；过滤后为空但全局候选非空，说明模型给的
单据/分录假设是错的，不能悄悄回退掩盖这个信号——`resolved[key]` 仍给全局候选，同时在
`mismatched_form[key]` 里诚实提示真实归属（`given_form`/`available_forms`、
`given_entry`/`available_entities`，视限定符类型出现）。

**`kind="entity"` 两段式分录限定符 fail-closed**（2026-07-07）：起因是一次真实翻车——模型传
`{"keys":["cqkd_zdgl.cqkd_zdzfltk"],"kind":"entity"}`（两段式「分录.子分录」，不带单据前缀）
拿到看似钉准的单条候选，但 `_matches` 在 `form_key is None` 时完全不按单据过滤，如果这个
`(parent_key, key)` 组合恰好只在**别的**单据下存在，模型无从判断这条坐标是否真的属于当前排查
的单据，把未经单据校验的结果当成了 confirmed。`kind="entity"` 且限定符解析为两段式（无单据
前缀，即 `form_key is None and entry_key is not None`）时，工具直接拒绝，`resolved[key]` 给
`None`，改在 `invalid_request[key]` 里给出 `reason="missing_form_key"` + 补全提示，不再返回
候选列表让模型自己判断——这是"同 key 跨坐标全摆出、不替选"纪律在这一具体分支上的例外：形状
不对就拒，不判断是否真的存在歧义。三段式（必带单据前缀）、字段侧两段式、裸 key、`kind=None`
均不受影响。

设计纪律（对齐红线）：
- **复用词典层**：打现成 `Lexicon`（field + entity 同口径），不新造解析。
- **同 key 跨多坐标全摆出、不替选**：分录字段常一个 key 在多分录各有定义、名字还可能不同，
  工具诚实返回 list，消歧靠模型读代码时的实体上下文（红线·处处 unknown）——`kind="entity"`
  两段式无单据前缀是这条纪律的唯一例外（见上）。
- **纯读 `field`/`entity`/`form` 表**，零 schema 改动，不碰代码访问侧 `field_access`（那是 trace 本职）。

延续 report 包约定：dict 在前（供 --json / MCP），`render_*` 文本在后。
"""

from __future__ import annotations

from typing import Any

from ..semantic.dictionary import build_lexicon

# 层级 → 中文（与 semantic/dictionary.py:Candidate.label 同一套映射，保持文案一致）。
_LEVEL_CN = {"header": "表头", "entry": "分录", "subentry": "子分录", "basedata": "基础资料"}

# 分录容器取值语义（与字段侧 _access_hint 形成对照，强化"容器 vs 多选基础资料"二选一判别）。
_ENTRY_ACCESS = "分录容器——getDynamicObjectCollection() 取的是分录行集合（逐行 get(i)）"

# field_kind（field 表 kind 列）中文标签——与 metadata/model.py:FieldKind 注释同一套口径，
# 焊进返回值本体，模型不用去翻元数据模块源码就知道这个分类码是什么意思。
_FIELD_KIND_LABEL = {
    "entity": "落库实体字段（有 DB 列）",
    "dynamic": "动态表单字段（本就不落库，非缺失）",
    "basedata_prop": "基础资料带出的引用字段（同 key 可能跨实体不唯一）",
    "platform": "平台标准字段（随模板继承而来，如单据编号/状态）",
    "inherited": "继承父模板的字段",
    "unknown": "字段分类判不出",
}


def _access_hint(field_type: str | None) -> str | None:
    """字段 XML 标签名 → getDynamicObject(Collection) 的取值语义（中文）。判不出回 None（不臆造）。

    起因：模型见 `getDynamicObjectCollection(key)` 默认当"分录"，但多选基础资料字段
    （MulBasedataField）也用它取选中的基础资料集合——取分录还是基础资料，取决于 key 是什么。
    `field_type` 是精确信号：含 Basedata 即基础资料类，Mul 前缀即多选（取集合）。标量字段
    （Text/Amount/Combo…）本就不走 getDynamicObject*，不强加语义。
    """
    ft = field_type or ""
    if "Basedata" not in ft and "BaseData" not in ft:
        return None
    if ft.startswith("Mul"):
        return "多选基础资料字段——getDynamicObjectCollection() 取的是选中的基础资料对象集合，不是分录行"
    return "基础资料字段——getDynamicObject() 取关联的基础资料对象，不是分录"


def _split_qualified(key: str, lex) -> tuple[str | None, str | None, str] | None:
    """复合 key → `(form_key, entry_key, field_key)`；与 `field_trace.parse_locator` 同一套
    点号坐标惯例（`单据.字段`/`分录.字段`/`单据.分录.字段`/`单据.分录.子分录.字段`），模型已经
    在用 `trace` 的这套写法，`resolve_fields` 不该另立一套只认 `单据.字段` 两段式——2026-07-05
    真实排障：模型按三段式传 `"单据.分录.字段"`/两段式传 `"分录.字段"`，因为老逻辑只认两段式且
    限定符必须命中 `form` 表，两种写法全部落空返回 null。

    两段：前段命中 `form` 表按单据限定；否则命中 `entity` 表按分录/子分录限定；两处都不命中就
    不当限定符（防误切普通含点标识），返回 None 走裸 key 查询。
    三段及以上：首段=单据，倒数第二段=分录/子分录（多段时中间段仅供阅读，不参与过滤，与
    `parse_locator` 的"中段=父分录"同一取舍）；首段须命中 `form` 表才当限定符处理，否则整串
    按裸 key 查（不臆断哪段是单据）。
    """
    parts = [p for p in key.split(".") if p]
    if len(parts) < 2:
        return None
    field_key = parts[-1]
    if not field_key:
        return None
    if len(parts) == 2:
        qualifier = parts[0]
        if lex.form_by_key(qualifier) is not None:
            return qualifier, None, field_key
        if lex.entities_by_key(qualifier):
            return None, qualifier, field_key
        return None
    form_key, entry_key = parts[0], parts[-2]
    if lex.form_by_key(form_key) is None:
        return None
    return form_key, entry_key, field_key


def _items_for(key: str, lex, *, kind: str | None = None) -> list[dict[str, Any]]:
    """kind 给定时只跑对应那一路查询（field/entity/form），从根上不产出另外两路的噪声。"""
    items: list[dict[str, Any]] = []
    if kind is not None and kind not in ("field", "entity", "form"):
        kind = None
    for f in ([] if kind not in (None, "field") else lex.fields_by_key(key)):
        item: dict[str, Any] = {
            "kind": "field",
            "name": f.name,
            "form_key": f.form_key,
            "entity_key": f.entity_key,
            "level": f.level,
            "field_kind": f.kind,
            "field_kind_label": _FIELD_KIND_LABEL.get(f.kind, f.kind),
            "field_type": f.field_type,     # XML 标签名：判 getDynamicObjectCollection 取值语义的精确信号
            "access": _access_hint(f.field_type),  # 派生取值语义（基础资料 vs None），堵"凭 API 名当分录"
        }
        if f.combo_items:
            # 下拉/枚举取值真实含义（value→中文），堵"凭存储值猜枚举含义"。
            item["combo_items"] = [
                {"value": v, "caption": c} for v, c in f.combo_items
            ]
        if f.ref_form_key:
            # 基础资料/组织等引用字段命中目标单据（本次建库范围内可反查），
            # 告诉模型这是外键指向哪张实体，不是数据本身。
            item["ref_entity"] = {"form_key": f.ref_form_key, "name": f.ref_form_name}
        elif f.ref_entity_id and _access_hint(f.field_type):
            # 是引用字段但反查不到目标单据（诚实留痕，不静默丢弃这个信号，红线#4）。
            item["ref_entity_id"] = f.ref_entity_id
        items.append(item)
    for e in ([] if kind not in (None, "entity") else lex.entities_by_key(key)):
        items.append({
            "kind": e.level or "entry",  # entry/subentry/header：让模型识别这是容器不是字段
            "name": e.name,
            "form_key": e.form_key,
            "level": e.level,
            "parent_key": e.parent_key,
            "access": _ENTRY_ACCESS,
        })
    form = lex.form_by_key(key) if kind in (None, "form") else None
    if form is not None:
        items.append({
            "kind": "form",
            "name": form.name,
            "form_key": form.key,
            "form_type": form.form_type,
        })
    return items


def _plugin_items_for(key: str, conn, lex) -> list[dict[str, Any]]:
    """插件类名（简单名或全限定名）→ 绑定的单据/操作/启用态。

    起因：`bill`/`trace` 都要求先有 `form_key`，但段二模型手头常常只有一个插件类名
    （如 `AdjustAmountOpplugin`），工具箱里没有任何一环能把"类名"变成"form_key"——逼得
    模型走 `loadSingle` 字面量/命名惯例猜测这条被 server instructions 明令禁止的路。

    `plugin` 表（元数据插件注册，含运行时信息）为主，精确 `class_name` 命中优先；命中为空
    且 `key` 不含包名（裸简单类名）时，退化为全表扫描按 Python 侧
    `class_name.rsplit(".", 1)[-1] == key` 过滤（工程规模有限，全表扫描可接受，避免 SQL
    `LIKE` 通配符转义 `_`/`%` 的复杂度和误判风险）。`plugin` 表零命中时同样两级回落查
    `binding` 表（桥接匹配结果）——只标注 `binding_status`/`confidence`/`source_relpath`，
    `plugin_type`/`operation_key`/`operation_name`/`enabled` 留空，因为 `binding` 表的
    `plugin_type` 是桥接阶段按类名后缀猜的，不是元数据登记的权威信息，混进去会误导模型
    以为这是登记过的插件运行信息。
    """
    def _form_name(form_key: str | None) -> str | None:
        if not form_key:
            return None
        f = lex.form_by_key(form_key)
        return f.name if f is not None else None

    rows = conn.execute(
        "SELECT class_name,form_key,plugin_type,source,operation_key,operation_name,enabled "
        "FROM plugin WHERE class_name=?", (key,),
    ).fetchall()
    if not rows and "." not in key:
        rows = [
            r for r in conn.execute(
                "SELECT class_name,form_key,plugin_type,source,operation_key,operation_name,"
                "enabled FROM plugin",
            ).fetchall()
            if r["class_name"] and r["class_name"].rsplit(".", 1)[-1] == key
        ]
    items: list[dict[str, Any]] = [
        {
            "kind": "plugin",
            "class_name": r["class_name"],
            "form_key": r["form_key"],
            "form_name": _form_name(r["form_key"]),
            "plugin_type": r["plugin_type"],
            "operation_key": r["operation_key"],
            "operation_name": r["operation_name"],
            "enabled": None if r["enabled"] is None else bool(r["enabled"]),
            "source": r["source"],
        }
        for r in rows
    ]
    if items:
        return items

    brows = conn.execute(
        "SELECT class_name,form_key,status,confidence,source_relpath "
        "FROM binding WHERE class_name=?", (key,),
    ).fetchall()
    if not brows and "." not in key:
        brows = [
            r for r in conn.execute(
                "SELECT class_name,form_key,status,confidence,source_relpath FROM binding",
            ).fetchall()
            if r["class_name"] and r["class_name"].rsplit(".", 1)[-1] == key
        ]
    return [
        {
            "kind": "plugin",
            "class_name": r["class_name"],
            "form_key": r["form_key"],
            "form_name": _form_name(r["form_key"]),
            "plugin_type": None,
            "operation_key": None,
            "operation_name": None,
            "enabled": None,
            "binding_status": r["status"],
            "confidence": r["confidence"],
            "source_relpath": r["source_relpath"],
        }
        for r in brows
    ]


def _plugin_orphan_lookup(key: str, conn) -> dict[str, Any] | None:
    """`plugin`/`binding` 两表都查不到绑定时，查 `source_class` 确认"类存在、是插件子类、
    只是没有绑定关系"——与"类名连 source_class 都没有"（可能模型记错类名）区分，用不同信道
    诚实标注（红线·处处 unknown 但带证据）。"""
    row = conn.execute(
        "SELECT fqn,relpath,plugin_base FROM source_class "
        "WHERE (fqn=? OR simple=?) AND orphan_role='plugin' LIMIT 1",
        (key, key),
    ).fetchone()
    if row is None:
        return None
    return {
        "class_name": row["fqn"],
        "relpath": row["relpath"],
        "plugin_base": row["plugin_base"],
        "hint": (
            "该类存在且被识别为苍穹插件基类的子类，但未匹配到任何单据绑定关系，可能未启用/"
            "动态注册/桥接遗漏；若源码里有 loadSingle 等字面量可作为最后手段人工核实。"
        ),
    }


def _matches(it: dict[str, Any], form_key: str | None, entry_key: str | None) -> bool:
    """按 item 种类分支判限定符是否匹配（issue 5）：字段命中带 entity_key，容器命中
    （entry/subentry/header）只带 parent_key、没有 entity_key 这个键——统一用
    `entity_key` 比较会让容器命中永远 `.get()` 到 None、被误判不匹配（明明命中却进
    mismatched_form 的真实成因）。容器场景下 entry_key 语义即"父分录 key"，与
    parent_key 对齐。
    """
    if form_key is not None and it.get("form_key") != form_key:
        return False
    if entry_key is not None:
        if it.get("kind") == "field":
            if it.get("entity_key") != entry_key:
                return False
        elif it.get("kind") in ("entry", "subentry", "header"):
            if it.get("parent_key") != entry_key:
                return False
        elif it.get("entity_key") != entry_key:
            return False
    return True


def _kind_mismatch(
    field_key: str, lex, kind: str | None, form_key: str | None, entry_key: str | None,
) -> dict[str, Any] | None:
    """`kind` 指定了但按此 kind 查不到时，用 `kind=None` 反查其它词典，诊断"种类给错了"。

    命中其它 kind 的精确候选才返回诊断 dict（`requested_kind`/`actual_kinds`/`candidates`，
    带限定符时还给 `qualifier_matches`）；反查也没有全局候选（真的钉不出）返回 None，调用方
    据此回落到"钉不出"分支。`actual_kinds` 是 item 自身的 kind 取值（field/header/entry/
    subentry/form），跟 `resolve_fields(kind=...)` 参数域（field/entity/form）不是同一套词表，
    不能直接回填给 `kind=` 参数用——只是诚实告诉调用方"这个 key 实际是什么"。
    """
    if kind is None:
        return None
    all_items = _items_for(field_key, lex, kind=None)
    if not all_items:
        return None
    mm: dict[str, Any] = {
        "requested_kind": kind,
        "actual_kinds": sorted({it["kind"] for it in all_items}),
        "candidates": all_items,
    }
    if form_key is not None or entry_key is not None:
        mm["qualifier_matches"] = [it for it in all_items if _matches(it, form_key, entry_key)]
    return mm


def resolve_fields(
    conn, keys: list[str], *, kind: str | list[str | None] | None = None,
) -> dict[str, Any]:
    """字段/分录容器/单据标识 → 真实元数据中文名+实体坐标。钉不出回 None（不臆造）。

    `key` 支持复合限定符——`"单据.字段"`/`"分录.字段"`/`"单据.分录.字段"`（与 `trace` 的点号
    坐标同一套惯例，模型自己从源码字面量读出单据/分录 key 时用）；限定符不匹配任何全局候选时，
    `mismatched_form[key]` 会诚实提示真实归属，不悄悄回退。

    `kind`：`field`/`entity`/`form`/`plugin` 之一，给定时只返回对应种类的候选（issue 4）——模型
    自己读到 `.loadSingle(id, key)` 这类单据字面量时传 `kind="form"`，从根上避免混入同 key 的
    字段候选噪声。指定的 kind 查不到时，纵向两级诊断：先查种类对不对（`mismatched_kind`，如
    `cqkd_ht` 实为 form key 却传了 `kind="field"`）；种类对了才查单据/分录限定符对不对
    （`mismatched_form`，两者互斥触发，不处理二者共存——种类都不对时先纠种类，用户/模型改对
    种类后再跑一次自然会走到 `mismatched_form` 那一级）。

    **`kind` 也接受与 `keys` 等长的列表**（2026-07-08，真实翻车复盘）：批量传入的 key 常常分属
    不同层级——比如同时传单据号字面量、分录容器 key、字段 key 三个不同的标识——此时传单个
    `kind` 字符串会被广播到全部 key，必然对不上其中至少一个（模型曾把 `["cqkd_ht","cqkd_zdgl",
    "cqkd_qs"]` 全标 `kind="field"`，实际分别是 form/entity/field 三个不同种类，导致前两个全部
    落入 `mismatched_kind`）。分属不同层级时改传等长列表逐位对应，如
    `kind=["form","entity","field"]`；某位不确定就填 `None`（该 key 三路全查，不限定）。列表长度
    与 `keys` 不一致会直接 `ValueError`（形状错误，拒绝静默截断/循环补齐）。

    `kind="entity"` 时两段式「分录.字段」限定符不允许省略单据前缀：命中 `invalid_request[key]`
    （`reason="missing_form_key"`），`resolved[key]` 给 `None`，不给候选——需改传三段式
    「单据.分录.字段」（见模块 docstring）。

    `kind="plugin"`：`key` 整串按插件类名处理（简单名或全限定名均可），**不走**点号坐标限定符
    协议（类名本身带包名的点号，与「单据.字段」限定符语法冲突）。返回其绑定的单据/操作/启用态
    （`plugin` 表为主，`binding` 表回落）；两表都查不到但 `source_class` 里能确认"类存在、是
    插件子类"时，`resolved[key]` 仍给 `None`，但 `unbound_in_source[key]` 会诚实标注该类的
    源文件位置与插件基类（"钉不出绑定"与"钉不出类本身"是两种不同的 unknown，不能混为一谈）。
    """
    if isinstance(kind, list):
        if len(kind) != len(keys):
            raise ValueError(
                f"kind 列表长度({len(kind)})必须与 keys 长度({len(keys)})一致，"
                "逐位对应同位置 key 的种类；不确定就填 None，不要用单个 kind 广播给不同层级的 key。"
            )
        kinds = kind
    else:
        kinds = [kind] * len(keys)

    lex = build_lexicon(conn)
    resolved: dict[str, list[dict[str, Any]] | None] = {}
    mismatched: dict[str, dict[str, Any]] = {}
    mismatched_kind: dict[str, dict[str, Any]] = {}
    invalid_request: dict[str, dict[str, Any]] = {}
    unbound_in_source: dict[str, dict[str, Any]] = {}
    for key, kind in zip(keys, kinds):
        if kind == "plugin":
            # 类名本身带包名点号，不能套用「单据.字段」限定符协议，整串按裸类名处理。
            items = _plugin_items_for(key, conn, lex)
            if items:
                resolved[key] = items
            else:
                resolved[key] = None
                orphan = _plugin_orphan_lookup(key, conn)
                if orphan is not None:
                    unbound_in_source[key] = orphan
            continue

        qualified = _split_qualified(key, lex)
        form_key, entry_key, field_key = (None, None, key) if qualified is None else qualified

        if kind == "entity" and qualified is not None and form_key is None and entry_key is not None:
            # 两段式「分录.字段」+ kind=entity，无单据前缀：硬拒绝，见模块 docstring
            # 「kind="entity" 两段式分录限定符 fail-closed」。
            invalid_request[key] = {
                "reason": "missing_form_key",
                "entry_key": entry_key,
                "field_key": field_key,
                "hint": (
                    'kind="entity" 且限定符是分录/子分录（两段式）时不允许省略单据前缀——同一分录/'
                    "子分录 key 可能跨多张单据存在，两段式无法确认候选属于当前排查的单据，返回全局"
                    "候选反而可能被当成本单据证据。请改用三段式「单据.分录.字段」，单据 key 从源码"
                    '上下文读取（如 .loadSingle(id, "单据key")/BusinessDataServiceHelper.load 的'
                    f'第二个参数）：例如 "<单据key>.{entry_key}.{field_key}"。'
                ),
            }
            resolved[key] = None
            continue

        items = _items_for(field_key, lex, kind=kind)

        if not items:
            mm_kind = _kind_mismatch(field_key, lex, kind, form_key, entry_key)
            if mm_kind is not None:
                mismatched_kind[key] = mm_kind
                resolved[key] = mm_kind["candidates"]
            else:
                resolved[key] = None
            continue

        if qualified is None:
            resolved[key] = items
            continue

        filtered = [it for it in items if _matches(it, form_key, entry_key)]
        if filtered:
            resolved[key] = filtered
        else:
            resolved[key] = items
            mm: dict[str, Any] = {"field_key": field_key}
            if form_key is not None:
                mm["given_form"] = form_key
                mm["available_forms"] = sorted(
                    {it["form_key"] for it in items if it.get("form_key")})
            if entry_key is not None:
                mm["given_entry"] = entry_key
                mm["available_entities"] = sorted(
                    {it.get("entity_key") or it.get("parent_key")
                     for it in items if it.get("entity_key") or it.get("parent_key")})
            # issue 6：平台/继承字段在特定单据下没有 field 行很正常（随模板继承，未逐单据
            # 登记），不是"限定符写错单据/分录"——弱化措辞，不当硬警告。
            platform_hit = next(
                (it for it in items
                 if it.get("kind") == "field" and it.get("field_kind") in ("platform", "inherited")),
                None,
            )
            if platform_hit is not None:
                mm["note"] = (
                    f"该字段在其它单据的元数据中标记为『{_FIELD_KIND_LABEL[platform_hit['field_kind']]}』，"
                    "本单据未见显式定义不代表读取有误，可能是随模板继承获得，未逐单据登记。"
                )
            mismatched[key] = mm
    out: dict[str, Any] = {"resolved": resolved}
    if mismatched:
        out["mismatched_form"] = mismatched
    if mismatched_kind:
        out["mismatched_kind"] = mismatched_kind
    if invalid_request:
        out["invalid_request"] = invalid_request
    if unbound_in_source:
        out["unbound_in_source"] = unbound_in_source
    return out


def render_resolve_fields(data: dict[str, Any], *, max_list: int = 20) -> str:
    """文本视图：逐 key 一段；命中列坐标，钉不出明确打印 null（标 unknown，勿猜）。"""
    resolved = data.get("resolved", {})
    mismatched = data.get("mismatched_form", {})
    mismatched_kind = data.get("mismatched_kind", {})
    invalid_request = data.get("invalid_request", {})
    unbound_in_source = data.get("unbound_in_source", {})
    if not resolved:
        return "（未传入任何字段标识）"
    lines: list[str] = []
    for key, items in resolved.items():
        inv = invalid_request.get(key)
        if inv:
            lines.append(f"{key}: ⛔ {inv['hint']}")
            continue
        if not items:
            unb = unbound_in_source.get(key)
            if unb:
                lines.append(
                    f"{key}: ⚠ 类「{unb['class_name']}」（{unb['relpath']}）存在，是"
                    f"「{unb['plugin_base']}」的子类，但未匹配到任何单据绑定 — {unb['hint']}")
            else:
                lines.append(f"{key}: null（钉不出，标 unknown，勿猜）")
            continue
        mmk = mismatched_kind.get(key)
        if mmk:
            lines.append(
                f"{key}: ⚠ 指定种类「{mmk['requested_kind']}」查不到，"
                f"它实际是「{'、'.join(mmk['actual_kinds'])}」（以下列全部候选）")
        mm = mismatched.get(key)
        if mm:
            given = []
            avail = []
            if "given_form" in mm:
                given.append(f"单据「{mm['given_form']}」")
                avail.append(f"单据: {', '.join(mm['available_forms'])}")
            if "given_entry" in mm:
                given.append(f"分录「{mm['given_entry']}」")
                avail.append(f"分录: {', '.join(mm['available_entities'])}")
            note = mm.get("note")
            mark = "ℹ" if note else "⚠"
            lines.append(f"{key}: {mark} 限定的{'+'.join(given)}下未找到该字段，"
                         f"它实际出现在 {'；'.join(avail)}（以下列全部候选）")
            if note:
                lines.append(f"  · 提示: {note}")
        lines.append(f"{key}:")
        for it in items[:max_list]:
            name = it.get("name") or ""
            form = it.get("form_key") or "?"
            lvl_cn = _LEVEL_CN.get(it.get("level") or "", it.get("level") or "?")
            access = f"  〔{it['access']}〕" if it.get("access") else ""
            if it.get("kind") == "field":
                ft = f" · {it['field_type']}" if it.get("field_type") else (
                    f" · {it['field_kind']}" if it.get("field_kind") else "")
                lines.append(f"  · 字段 {key}「{name}」 — {form} · {lvl_cn}{ft}{access}")
                if it.get("combo_items"):
                    opts = " / ".join(
                        f"{c['value']}={c['caption']}" for c in it["combo_items"])
                    lines.append(f"    取值: {opts}")
                ref = it.get("ref_entity")
                if ref:
                    lines.append(f"    → 引用 {ref['form_key']}「{ref.get('name') or ''}」")
                elif it.get("ref_entity_id"):
                    lines.append(f"    → 引用未知实体(oid={it['ref_entity_id']})")
            elif it.get("kind") == "form":
                ftype = f" [{it['form_type']}]" if it.get("form_type") else ""
                lines.append(f"  · 单据 {key}「{name}」{ftype}")
            elif it.get("kind") == "plugin":
                fname = f"「{it['form_name']}」" if it.get("form_name") else ""
                op = (f" · 操作 {it['operation_key']}「{it.get('operation_name') or ''}」"
                      if it.get("operation_key") else "")
                enabled = it.get("enabled")
                en_txt = "" if enabled is None else (" · 启用" if enabled else " · 禁用")
                status = it.get("binding_status")
                st_txt = f" · 桥接:{status}(置信{it.get('confidence')})" if status else ""
                lines.append(f"  · 插件 {it['class_name']} → {form}{fname}{op}{en_txt}{st_txt}")
            else:
                parent = it.get("parent_key")
                phint = f" ← {parent}" if parent else ""
                lines.append(f"  · 容器 {key}「{name}」 — {form} · {lvl_cn}{phint}{access}")
        if len(items) > max_list:
            lines.append(f"  …（共 {len(items)} 条坐标，全部见 --json）")
    return "\n".join(lines)
