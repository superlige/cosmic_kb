"""阶段 4/5 · 单据钻取视图（排障第二入口）。

围绕一张单据，把排障要的信息一页聚齐：身份 + 操作集（哪些操作有自定义插件）+ 按操作/类型
分组的插件 + 每个字段被哪些插件事件触达（嵌字段访问摘要）+ 桥接风险（有 project 插件却
找不到源码）。从 KB 读，与 field_trace 同口径。

延续 report 包约定：dict 在前，`render_*` 在后。
"""

from __future__ import annotations

import json
from typing import Any

from ..semantic import hints
# 复用 trace 的「host 口径字节度量 + 游标解析 + 预算/哨兵」单一事实源（红线 #6：度量逻辑只此一份）。
from .field_trace import (_wire_len, _parse_cursor, _COMPACT_BUDGET,
                          pagination_gate, _pending_from_flat_cursors)


def bill_view(conn, key: str) -> dict[str, Any] | None:
    """单据钻取详情；单据不存在返回 None。"""
    form = conn.execute("SELECT * FROM form WHERE key=?", (key,)).fetchone()
    if form is None:
        return None

    entities = [dict(r) for r in conn.execute(
        "SELECT key,name,level,parent_key,table_name FROM entity WHERE form_key=? ORDER BY level",
        (key,)).fetchall()]
    fields = [dict(r) for r in conn.execute(
        "SELECT entity_key,key,name,db_column,field_type,kind,level FROM field WHERE form_key=?",
        (key,)).fetchall()]
    operations = [dict(r) for r in conn.execute(
        "SELECT key,name,operation_type,resolved_from,has_plugin FROM operation "
        "WHERE form_key=? ORDER BY has_plugin DESC,key", (key,)).fetchall()]
    plugins = [dict(r) for r in conn.execute(
        "SELECT class_name,plugin_type,source,operation_key,operation_name FROM plugin "
        "WHERE form_key=?", (key,)).fetchall()]
    bindings = [dict(r) for r in conn.execute(
        "SELECT class_name,plugin_type,status,source_relpath,confidence,note FROM binding "
        "WHERE form_key=?", (key,)).fetchall()]

    # 字段/容器标识 → 本单据内真实中文名（模式 B：焊进 field_touch，杜绝段二按命名惯例臆断字段名）。
    name_by_key = {f["key"]: f["name"] for f in fields if f.get("name")}
    for e in entities:
        name_by_key.setdefault(e["key"], e["name"])

    # 每个字段被哪些插件事件写/读（来自字段级分析），按字段聚合 + 记层级坐标。
    field_touch: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT field_key,level,entry_key,access,persists,plugin_fqn,plugin_type,access_class,"
        "event_method,line,source_relpath FROM field_access "
        "WHERE form_key=? AND field_key IS NOT NULL", (key,),
    ).fetchall():
        d = dict(r)
        slot = field_touch.setdefault(d["field_key"], {
            "field_name": name_by_key.get(d["field_key"]),   # 已核对中文名（钉不出留 None）
            "writers": 0, "persisting": 0, "readers": 0,
            "level": d["level"], "entry_key": d["entry_key"], "events": []})
        if d["access"] == "write":
            slot["writers"] += 1
            if d["persists"] == "yes":
                slot["persisting"] += 1
        else:
            slot["readers"] += 1
        cross = d["access_class"] and d["access_class"] != d["plugin_fqn"]
        slot["events"].append({
            "plugin": (d["plugin_fqn"] or "").rsplit(".", 1)[-1],
            "access_class": (d["access_class"] or "").rsplit(".", 1)[-1] if cross else None,
            "event": d["event_method"], "access": d["access"], "persists": d["persists"],
            "line": d["line"], "source_relpath": d["source_relpath"],
            # 模式 B：事件 → 语义文档主题，提示「判触发时机/入库先查语义」。
            "semantics_topic": hints.event_topic(d["event_method"], d["plugin_type"]),
        })

    # 按实体分组的字段触达（前端以实体为单位展示）：实体 key → 该实体下被触达的字段清单。
    entity_name_by = {e["key"]: e["name"] for e in entities}
    entity_touch: dict[str, dict[str, Any]] = {}
    for fk, info in field_touch.items():
        ek = info["entry_key"] or "__header__"
        slot = entity_touch.setdefault(ek, {
            "entity_key": info["entry_key"], "level": info["level"],
            "entity_name": entity_name_by.get(info["entry_key"]), "fields": []})
        slot["fields"].append({"field_key": fk, **info})
    for slot in entity_touch.values():
        slot["fields"].sort(key=lambda f: -f["writers"])

    # 风险：有 project 插件却找不到源码 / 歧义。
    risk_bindings = [b for b in bindings if b["status"] in ("missing", "ambiguous")]

    # 轴 A · 场景/插件类型分流：把平铺插件清单按 plugin_type 切成带语义的车道（叠加视图，
    # 不替换 plugins 平铺）。binding 命中态挂到插件上，便于渲染层标「未命中源码」风险。
    # 平台预制插件（kd.bos.*）不进车道，但计数诚实呈现（红线 #4，不静默丢）。
    plugin_lanes = _build_plugin_lanes(plugins, bindings)
    platform_plugins_excluded = sum(1 for p in plugins if p.get("source") == "platform")

    # 扩展别名（form.is_extension=1）：内容已并入 extends 指向的原厂 form_key（见
    # cosmic_kb/metadata/merge.py::build_extension_alias）——本行 entities/fields/plugins
    # 皆空是设计如此，不是没扫到，加一句重定向提示。
    note = None
    if form["is_extension"] and form["extends"]:
        note = (f"⚑ {key} 是扩展别名，内容已并入原厂单据 {form['extends']}，"
                f"请改查 cosmic_kb bill {form['extends']}")

    return {
        "form": dict(form),
        "entities": entities,
        "fields": fields,
        "operations": operations,
        "plugins": plugins,
        "plugin_lanes": plugin_lanes,
        "platform_plugins_excluded": platform_plugins_excluded,
        "bindings": bindings,
        "field_touch": field_touch,
        "entity_touch": list(entity_touch.values()),
        "risk_bindings": risk_bindings,
        "note": note,
        "stats": {
            "entity_count": len(entities), "field_count": len(fields),
            "operation_count": len(operations), "plugin_count": len(plugins),
            "touched_fields": len(field_touch),
        },
    }


def _build_plugin_lanes(
    plugins: list[dict[str, Any]], bindings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """轴 A：按 plugin_type 把插件分成带语义的场景车道（op+form 主力在前，空车道不出现）。

    只覆盖单据绑定的 5 类 plugin_type；词表外的类型归 `other` 车道兜底（不吞）。每车道附一句
    触发场景语义 + 语义文档路由（判触发时机/是否入库前先 cosmic_semantics）。孤儿插件（validator/
    task/report 等无 form_key）本就不进 plugin 表，故不在此分流——归后续「孤儿类型目录旁路」。

    **平台预制插件（source=platform，即 kd.bos.* 前缀）排除在车道外**：它们由平台提供、无源码、
    不是二开排障目标，列进车道只会淹没真正要看的项目插件。排除数量由调用方另计并诚实呈现（不静默丢）。
    """
    # binding 命中态：class_name → status（missing/ambiguous 提示"未命中源码"）。
    status_by_cls = {b["class_name"]: b["status"] for b in bindings}
    # 车道桶：lane_id → 车道 dict。优先级序 = 词表 plugin_type 顺序映射成 lane_id（op→operation…），
    # 词表外类型（other）动态垫在其后。
    buckets: dict[str, dict[str, Any]] = {}
    lane_order = [hints.plugin_lane(pt)[0] for pt in hints.PLUGIN_LANE_ORDER]
    for p in plugins:
        if p.get("source") == "platform":   # 平台预制 kd.bos.*：不进车道
            continue
        lane_id, label, semantic = hints.plugin_lane(p["plugin_type"])
        slot = buckets.get(lane_id)
        if slot is None:
            slot = buckets[lane_id] = {
                "lane_id": lane_id, "label": label, "semantic": semantic,
                "semantics_topic": hints.event_topic(None, p["plugin_type"]),
                "plugins": [], "count": 0,
            }
            if lane_id not in lane_order:
                lane_order.append(lane_id)   # 未知类型（other）垫在词表车道之后
        st = status_by_cls.get(p["class_name"])
        slot["plugins"].append({
            "class_name": p["class_name"], "plugin_type": p["plugin_type"],
            "source": p["source"], "operation_key": p.get("operation_key"),
            "operation_name": p.get("operation_name"),
            "binding_risk": st if st in ("missing", "ambiguous") else None,
        })
        slot["count"] += 1
    return [buckets[lid] for lid in lane_order if lid in buckets]


# ── 紧凑投影（MCP 防截断）：折叠逐字段事件 + cap/字节 governor + 游标分页 ──────────────
# 富 bill_view 对大单据（实测 cqkd_ht 序列化 2.76MB，57/344 单据超 32KB）会被 MCP host 从中段
# **硬切**——比 trace 修复前还糟（连 summary 都未必活下来）。根因：field_touch/entity_touch 把每个
# 字段被触达的**逐条事件**（实测 2899 条）全展开。本投影与 trace_compact 同款治理：
#   ① 折叠——每字段的逐条事件塌成「写/落库/读」计数，要看「某字段谁改的」逐字段用 `trace 单据.字段`；
#   ② 删冗余——丢弃 field_touch（与 entity_touch 重复的扁平副本），只留按实体分组的 entity_touch；
#   ③ cap + 字节 governor——各列表 cap、按 host 口径 _wire_len 逐档收紧直至 ≤ 预算；
#   ④ 游标分页——被 cap 的段带 `*_next_cursor`，`bill(key, cursor=该值)` 翻页取回全部被截条目（红线 #4）。
# 富 bill_view 不动（CLI/Web 走 HTTP/终端无 32KB 限制，仍用富投影）。
_BILL_PAGE_SECTIONS = ("fields", "operations", "plugins", "bindings", "entities", "entity_touch")

# cap 阶梯（从宽到窄）：(字段元数据, 操作, 插件, 绑定, 实体, entity_touch 扁平字段行)。
_BILL_LADDER = [
    (60, 40, 40, 30, 40, 80),
    (40, 30, 30, 20, 30, 50),
    (25, 20, 20, 15, 20, 30),
    (15, 12, 12, 10, 15, 20),
    (8, 8, 8, 6, 8, 10),
    (4, 4, 4, 3, 4, 5),   # 硬底：极端单据也塌到最小
]


def _slim_form(f: dict[str, Any]) -> dict[str, Any]:
    return {k: f.get(k) for k in ("key", "name", "form_type", "module")}


def _slim_entity(e: dict[str, Any]) -> dict[str, Any]:
    return {k: e.get(k) for k in ("key", "name", "level", "parent_key", "table_name")}


def _slim_op(o: dict[str, Any]) -> dict[str, Any]:
    return {k: o.get(k) for k in ("key", "name", "operation_type", "has_plugin")}


def _slim_plugin(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p.get(k) for k in
            ("class_name", "plugin_type", "source", "operation_key", "operation_name")}


def _slim_binding(b: dict[str, Any]) -> dict[str, Any]:
    return {k: b.get(k) for k in
            ("class_name", "plugin_type", "status", "source_relpath", "note")}


def _slim_lanes(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """轴 A 车道的轻量索引（MCP 投影）：每车道只留 lane_id/label/语义/文档路由/计数，无逐插件行。"""
    return [{"lane_id": ln["lane_id"], "label": ln["label"], "semantic": ln["semantic"],
             "semantics_topic": ln["semantics_topic"], "count": ln["count"]} for ln in lanes]


def _slim_field_meta(f: dict[str, Any]) -> dict[str, Any]:
    return {k: f.get(k) for k in ("entity_key", "key", "name", "field_type", "kind", "level")}


def _touch_rows(bv: dict[str, Any]) -> list[dict[str, Any]]:
    """把 entity_touch 拍平成有序的「字段触达行」（每行带实体上下文 + trace 导航），分页据此线性切片。

    逐条事件已折叠为计数；要看某字段「谁改的/在哪个事件函数/是否落库」逐字段用 `trace`（每行已给锚点）。
    """
    form_key = bv["form"]["key"]
    rows: list[dict[str, Any]] = []
    for et in bv["entity_touch"]:
        for fld in et["fields"]:
            fk = fld["field_key"]
            rows.append({
                "entity_key": et["entity_key"], "entity_name": et["entity_name"],
                "level": et["level"], "field_key": fk,
                "field_name": fld.get("field_name"),   # 已核对中文名（钉不出留 None）
                "writers": fld["writers"], "persisting": fld["persisting"], "readers": fld["readers"],
                "entry_key": fld.get("entry_key"),
                "trace": f"trace {form_key}.{fk}",     # 逐字段下钻导航（谁改的/是否落库见 trace）
            })
    return rows


def _group_touch_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把扁平字段触达行按实体（保序）重新分组，供 overview 展示——与扁平 offset 一一对应（翻页无缝接续）。"""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for r in rows:
        if cur is None or cur["entity_key"] != r["entity_key"]:
            cur = {"entity_key": r["entity_key"], "entity_name": r["entity_name"],
                   "level": r["level"], "fields": []}
            out.append(cur)
        cur["fields"].append({k: r[k] for k in
                              ("field_key", "field_name", "writers", "persisting", "readers", "entry_key")})
    return out


def _cap_flag(res: dict[str, Any], sec: str, total: int, cap: int) -> bool:
    """列表段被 cap 时记 `<sec>_capped` + `<sec>_next_cursor`（游标翻页取回被截条目）。返回是否截断。"""
    if total > cap:
        res[f"{sec}_capped"] = total - cap
        res[f"{sec}_next_cursor"] = f"{sec}@{len(res[sec])}"
        return True
    return False


def _build_bill_compact(
    bv: dict[str, Any], cap_fields: int, cap_ops: int, cap_plugins: int,
    cap_bindings: int, cap_entities: int, cap_touch: int, profile: str,
) -> dict[str, Any]:
    """一档 cap 下构建紧凑 bill dict（governor 会按字节预算反复调用收紧）。

    `profile="overview"`（默认）只给单据概览 + 插件绑定，不含 `fields`/`entity_touch`——
    这两段本有专职工具顶替（字段名核对用 `resolve_fields`，谁改的/是否落库用 `trace`），
    默认带出来对"解读单据/方法"场景是纯冗余。`profile="full"` 补回这两段，与今天的
    紧凑投影形状一致。两档均可用 `cursor="fields@0"`/`"entity_touch@0"` 单独翻页取回，
    不受 profile 限制（红线 #4：不静默丢证据，只是不主动塞）。
    """
    ents, ops, plugins = bv["entities"], bv["operations"], bv["plugins"]
    binds, fields = bv["bindings"], bv["fields"]
    full = profile == "full"

    res: dict[str, Any] = {
        "form": _slim_form(bv["form"]),
        "stats": bv["stats"],
        "entities": [_slim_entity(e) for e in ents[:cap_entities]],
        "entities_total": len(ents),
        "operations": [_slim_op(o) for o in ops[:cap_ops]],
        "operations_total": len(ops),
        "plugins": [_slim_plugin(p) for p in plugins[:cap_plugins]],
        "plugins_total": len(plugins),
        # 轴 A 轻量车道索引：只给分流语义/优先级/文档路由 + 计数，不复制逐插件行（明细在
        # 平铺 plugins 段，各带 plugin_type，LLM 自行归位）。体积极小，不进 ladder cap。
        "plugin_lanes": _slim_lanes(bv["plugin_lanes"]),
        "platform_plugins_excluded": bv.get("platform_plugins_excluded", 0),
        "bindings": [_slim_binding(b) for b in binds[:cap_bindings]],
        "bindings_total": len(binds),
        "risk_bindings": [_slim_binding(b) for b in bv["risk_bindings"]],  # 通常很少，整列内联
    }
    capped = False
    capped |= _cap_flag(res, "entities", len(ents), cap_entities)
    capped |= _cap_flag(res, "operations", len(ops), cap_ops)
    capped |= _cap_flag(res, "plugins", len(plugins), cap_plugins)
    capped |= _cap_flag(res, "bindings", len(binds), cap_bindings)

    if full:
        touch = _touch_rows(bv)
        touch_shown = touch[:cap_touch]
        res["entity_touch"] = _group_touch_rows(touch_shown)
        res["touched_fields_total"] = len(touch)
        res["fields"] = [_slim_field_meta(x) for x in fields[:cap_fields]]
        res["fields_total"] = len(fields)
        capped |= _cap_flag(res, "fields", len(fields), cap_fields)
        if len(touch_shown) < len(touch):    # entity_touch 按扁平字段行计数翻页（offset=扁平行号）
            res["entity_touch_capped"] = len(touch) - len(touch_shown)
            res["entity_touch_next_cursor"] = f"entity_touch@{len(touch_shown)}"
            capped = True

    note = ""
    if bv.get("note"):   # 扩展别名重定向提示（见 bill_view），优先摆最前，别被防截断说明淹没
        note += bv["note"] + " "
    if full:
        note += ("紧凑投影（防 MCP 32KB 截断）：每字段的逐条事件已折叠为「写/落库/读」计数——要看『某字段"
                "谁改的/在哪个事件函数/是否落库』逐字段用 `trace 单据.字段`（entity_touch 每行已给 trace 锚点）。"
                "插件按场景车道分流见 `plugin_lanes`（操作/界面/列表/反写/转换，op+form 主力在前，带语义文档路由）；"
                "逐插件明细在平铺 `plugins`（各带 plugin_type，按此归位）——只含单据绑定插件，孤儿类不在此。"
                "平台预制插件 kd.bos.*（source=platform）不进车道（`platform_plugins_excluded` 计数），非二开排障目标。")
    else:
        note += ("单据概览（默认瘦身投影）：不含逐字段元数据 `fields` 与按实体分组的读写触达 `entity_touch`——"
                "字段名核对改用 `resolve_fields`（批量更省），某字段谁改的/是否落库改用 `trace 单据.字段`；"
                "确要看这两段可 `bill(key, profile=\"full\")` 换完整紧凑投影，或 `cursor=\"fields@0\"`/"
                "`\"entity_touch@0\"` 单独翻页取回（不问自答，取回全部证据两不误）。"
                "插件按场景车道分流见 `plugin_lanes`（操作/界面/列表/反写/转换，带语义文档路由）；"
                "逐插件明细在平铺 `plugins`——只含单据绑定插件，孤儿类不在此。"
                "平台预制插件 kd.bos.*（source=platform）不进车道（`platform_plugins_excluded` 计数）。")
    if capped:
        note += ("各列表真实总数在 `*_total`，被 cap 截掉的段带 `*_next_cursor`，"
                 "用 `bill(key, cursor=该值)` 再调可逐页**取回全部被截条目**（不丢数）；"
                 f"可分页：{', '.join(_BILL_PAGE_SECTIONS)}。")
    res["note"] = note
    pending = _pending_from_flat_cursors(res)
    reordered = {"pagination": pagination_gate(pending), **res}
    res.clear()
    res.update(reordered)
    return res


def _bill_section_full(bv: dict[str, Any], section: str) -> list[dict[str, Any]] | None:
    """某段的**完整（未 cap）有序列表**（与 overview 同序，保证 offset 一致）；未知段返回 None。"""
    if section == "fields":
        return [_slim_field_meta(f) for f in bv["fields"]]
    if section == "operations":
        return [_slim_op(o) for o in bv["operations"]]
    if section == "plugins":
        return [_slim_plugin(p) for p in bv["plugins"]]
    if section == "bindings":
        return [_slim_binding(b) for b in bv["bindings"]]
    if section == "entities":
        return [_slim_entity(e) for e in bv["entities"]]
    if section == "entity_touch":
        return _touch_rows(bv)            # 扁平字段触达行（每行带实体上下文 + trace 导航）
    return None


def _bill_page_section(bv: dict[str, Any], section: str, offset: int, budget: int) -> dict[str, Any]:
    """聚焦分页：只回某段从 offset 起、预算内能装下的下一页 items + next_cursor。"""
    base = {"form_key": bv["form"]["key"], "form_name": bv["form"].get("name")}
    items = _bill_section_full(bv, section)
    if items is None:
        return {**base, "page": {"section": section,
                "error": f"未知或不可分页的 section: {section}（可分页：{', '.join(_BILL_PAGE_SECTIONS)}）"}}
    total = len(items)
    offset = min(max(0, offset), total)

    def _wrap(page: list[dict[str, Any]], nxt: int) -> dict[str, Any]:
        next_cursor = f"{section}@{nxt}" if nxt < total else None
        pending = [{"section": section, "next_cursor": next_cursor}] if next_cursor else []
        return {"pagination": pagination_gate(pending), **base,
                "page": {"section": section, "offset": offset, "returned": len(page),
                        "total": total, "items": page, "next_cursor": next_cursor}}

    page: list[dict[str, Any]] = []
    for it in items[offset:]:
        trial = page + [it]
        if page and _wire_len(_wrap(trial, offset + len(trial))) > budget:
            break             # 至少装一条（单条即便超 budget 也给，仍远小于 32KB）
        page = trial
    return _wrap(page, offset + len(page))


_BILL_PROFILES = ("overview", "full")


def bill_compact(
    conn, key: str, *, cursor: str | None = None, budget: int = _COMPACT_BUDGET,
    profile: str = "overview",
) -> dict[str, Any]:
    """**紧凑投影**（MCP 入口，防 host 32KB 截断）：折叠逐字段事件 + cap/字节 governor + 游标分页。

    - `profile="overview"`（默认）：单据概览 + 插件绑定，不含 `fields`/`entity_touch`
      （字段名核对改用 `resolve_fields`，谁改的/是否落库改用 `trace`）。
      `profile="full"`：补回这两段，与旧版紧凑投影形状一致。
    - 每字段逐条事件折叠为计数；要看「某字段谁改的」逐字段用 `trace 单据.字段`。
    - 真实总数在 `*_total`；被 cap 截掉的段带 `*_next_cursor`，用 `bill(key, cursor=该值)` 翻页取全
      ——不论 profile 是哪档，`cursor="fields@0"`/`"entity_touch@0"` 都能单独把这两段翻出来。
    - governor：构完测序列化字节，超 `budget` 就逐档收紧 cap 重建，直至 ≤ budget——保证永不被 host 截断。
    单据不存在返回 `{"error": ...}`（与 tool_bill 同口径）；非法 profile 同样返回 `{"error": ...}`。
    """
    if profile not in _BILL_PROFILES:
        return {"error": f"未知 profile: {profile}（可选 {'/'.join(_BILL_PROFILES)}）"}
    bv = bill_view(conn, key)
    if bv is None:
        return {"error": f"单据不存在: {key}"}
    if cursor:
        section, offset = _parse_cursor(cursor)
        return _bill_page_section(bv, section, offset, budget)
    res: dict[str, Any] = {}
    for caps in _BILL_LADDER:
        res = _build_bill_compact(bv, *caps, profile)
        if _wire_len(res) <= budget:
            return res
    return res


def render_bill(bv: dict[str, Any], *, max_list: int = 30) -> str:
    f = bv["form"]
    st = bv["stats"]
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"单据视图: {f['key']}  {f['name'] or ''}  [{f['form_type']}]  «{f['module'] or '?'}»")
    lines.append("=" * 72)
    lines.append(
        f"  实体 {st['entity_count']}  字段 {st['field_count']}  操作 {st['operation_count']}  "
        f"插件 {st['plugin_count']}  有插件触达的字段 {st['touched_fields']}"
    )

    if bv.get("note"):
        lines.append("")
        lines.append(bv["note"])

    if bv["operations"]:
        lines.append("")
        lines.append("【操作集】（★ = 有自定义操作插件，排障优先看）")
        for o in bv["operations"]:
            star = "★" if o["has_plugin"] else " "
            lines.append(f"  {star} {o['key'] or '?':<18} {o['name'] or '':<10} [{o['operation_type'] or '?'}]")

    if bv.get("plugin_lanes"):
        lines.append("")
        lines.append("【插件清单·按场景分流】（单据绑定插件，不含孤儿类：调度/报表/校验器等，见 coverage 边界声明）")
        lines.append("  （某事件方法调了项目内哪些方法：calls <类全限定名> <方法名>）")
        for lane in bv["plugin_lanes"]:
            lines.append("")
            lines.append(f"  ▶ {lane['label']}（{lane['count']}）  {lane['semantic']}")
            if lane["semantics_topic"]:
                lines.append(f"    ↳ 判触发时机/是否入库前先 cosmic_semantics('{lane['semantics_topic']}')")
            for p in lane["plugins"]:
                op = f" ←{p['operation_key']}" if p["operation_key"] else ""
                warn = f"  ⚠{p['binding_risk']}" if p.get("binding_risk") else ""
                lines.append(f"      [{p['plugin_type']}] {p['class_name']} ({p['source']}){op}{warn}")
        if bv.get("platform_plugins_excluded"):
            lines.append("")
            lines.append(f"  （另有 {bv['platform_plugins_excluded']} 个平台预制插件 kd.bos.* 未列入车道"
                         "：平台提供、无源码、非二开排障目标）")

    if bv["field_touch"]:
        lines.append("")
        items = sorted(bv["field_touch"].items(), key=lambda kv: -kv[1]["writers"])
        lines.append(f"【字段触达】（被插件读写的字段，前 {min(max_list, len(items))}；详情见 trace <字段>）")
        for fk, info in items[:max_list]:
            nm = f"「{info['field_name']}」" if info.get("field_name") else ""
            lines.append(f"  {fk:<26}{nm} 写{info['writers']}(落库{info['persisting']}) 读{info['readers']}")

    if bv["risk_bindings"]:
        lines.append("")
        lines.append("【风险】project 插件未命中源码 / 歧义（排障会卡在这）")
        for b in bv["risk_bindings"]:
            lines.append(f"  [{b['status']}] {b['class_name']} [{b['plugin_type']}]  {b['note'] or ''}")
    return "\n".join(lines)
