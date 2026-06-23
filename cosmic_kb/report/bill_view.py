"""阶段 4/5 · 单据钻取视图（排障第二入口）。

围绕一张单据，把排障要的信息一页聚齐：身份 + 操作集（哪些操作有自定义插件）+ 按操作/类型
分组的插件 + 每个字段被哪些插件事件触达（嵌字段访问摘要）+ 桥接风险（有 project 插件却
找不到源码）。从 KB 读，与 field_trace 同口径。

延续 report 包约定：dict 在前，`render_*` 在后。
"""

from __future__ import annotations

import json
from typing import Any


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

    # 每个字段被哪些插件事件写/读（来自字段级分析），按字段聚合 + 记层级坐标。
    field_touch: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT field_key,level,entry_key,access,persists,plugin_fqn,access_class,"
        "event_method,line,source_relpath FROM field_access "
        "WHERE form_key=? AND field_key IS NOT NULL", (key,),
    ).fetchall():
        d = dict(r)
        slot = field_touch.setdefault(d["field_key"], {
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

    return {
        "form": dict(form),
        "entities": entities,
        "fields": fields,
        "operations": operations,
        "plugins": plugins,
        "bindings": bindings,
        "field_touch": field_touch,
        "entity_touch": list(entity_touch.values()),
        "risk_bindings": risk_bindings,
        "stats": {
            "entity_count": len(entities), "field_count": len(fields),
            "operation_count": len(operations), "plugin_count": len(plugins),
            "touched_fields": len(field_touch),
        },
    }


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

    if bv["operations"]:
        lines.append("")
        lines.append("【操作集】（★ = 有自定义操作插件，排障优先看）")
        for o in bv["operations"]:
            star = "★" if o["has_plugin"] else " "
            lines.append(f"  {star} {o['key'] or '?':<18} {o['name'] or '':<10} [{o['operation_type'] or '?'}]")

    if bv["plugins"]:
        lines.append("")
        lines.append("【插件清单】（某事件方法调了项目内哪些方法：calls <类全限定名> <方法名>）")
        for p in bv["plugins"]:
            op = f" ←{p['operation_key']}" if p["operation_key"] else ""
            lines.append(f"  [{p['plugin_type']}] {p['class_name']} ({p['source']}){op}")

    if bv["field_touch"]:
        lines.append("")
        items = sorted(bv["field_touch"].items(), key=lambda kv: -kv[1]["writers"])
        lines.append(f"【字段触达】（被插件读写的字段，前 {min(max_list, len(items))}；详情见 trace <字段>）")
        for fk, info in items[:max_list]:
            lines.append(f"  {fk:<26} 写{info['writers']}(落库{info['persisting']}) 读{info['readers']}")

    if bv["risk_bindings"]:
        lines.append("")
        lines.append("【风险】project 插件未命中源码 / 歧义（排障会卡在这）")
        for b in bv["risk_bindings"]:
            lines.append(f"  [{b['status']}] {b['class_name']} [{b['plugin_type']}]  {b['note'] or ''}")
    return "\n".join(lines)
