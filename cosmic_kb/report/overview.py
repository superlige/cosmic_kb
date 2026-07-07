"""阶段 4 · 接手者「项目理解报告」（一键概览）。

接手陌生苍穹项目第一天最想要的一张报告：这项目长什么样、有哪几个业务模块、实体/插件
清单、以及"先看哪里"的风险热点。**从 KB（SQLite）读**——KB 是契约（见 CLAUDE.md），
`cosmic_kb build` 落库后本报告与阶段 9/10 都查同一个 KB，口径一致、不重算。

风险热点只出 **KB 能算准的**（用户 2026-06-16 拍板）：真孤儿 / 未找到源码的 project 插件 /
歧义绑定 / 超大表单(按字段数) / 包结构不一致的模块。**超大插件(按行数)、可疑入库点**需 Java
行为分析，明确标注"留待阶段 5-7"，不臆造。

延续 report 包约定：dict 在前（供 --json），`render_*` 文本在后（给人看）。
"""

from __future__ import annotations

import json
from typing import Any

from . import project_map
from ..graph import store

# 超大表单阈值（字段数）：资产卡片 390 字段属典型"巨型单据"，150 起即值得接手者留意。
OVERSIZED_FORM_FIELDS = 150
# 包结构不一致模块阈值：一致度低于此且有源码命中的模块，模块划分需人工核对。
LOW_CONSISTENCY = 0.6


def overview(conn) -> dict[str, Any]:
    """从 KB 连接组装理解报告 dict。"""
    counts = json.loads(store.get_meta(conn, "counts") or "{}")
    health = json.loads(store.get_meta(conn, "health") or "{}")
    code_prefixes = json.loads(store.get_meta(conn, "code_prefixes") or "{}")
    meta_prefixes = json.loads(store.get_meta(conn, "meta_prefixes") or "{}")

    # ── 桥接命中（从 binding 表算，等价阶段 3 口径）──────────────────────
    status_counts = {
        r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM binding GROUP BY status")
    }
    linked = status_counts.get("linked", 0) + status_counts.get("linked_by_name", 0)
    project_total = linked + status_counts.get("missing", 0) + status_counts.get("ambiguous", 0)
    hit_rate = round(linked / project_total, 4) if project_total else None

    orphan_real = conn.execute(
        "SELECT COUNT(*) FROM source_class WHERE is_orphan=1 AND orphan_role='unknown'"
    ).fetchone()[0]
    orphan_const = conn.execute(
        "SELECT COUNT(*) FROM source_class WHERE is_orphan=1 AND orphan_role='constant'"
    ).fetchone()[0]
    orphan_plugin = conn.execute(
        "SELECT COUNT(*) FROM source_class WHERE is_orphan=1 AND orphan_role='plugin'"
    ).fetchone()[0]

    overview_block = {
        "built_at": store.get_meta(conn, "built_at"),
        "module_count": health.get("module_count"),
        "form_count": counts.get("form"),
        "entity_count": counts.get("entity"),
        "field_count": counts.get("field"),
        "plugin_count": counts.get("plugin"),
        "source_class_count": counts.get("source_class"),
        "bridge_hit_rate": hit_rate,
        "bridge_status": status_counts,
        "orphan_real": orphan_real,
        "orphan_const": orphan_const,
        "orphan_plugin": orphan_plugin,
        "convert_count": counts.get("convert_rule"),
        "code_prefixes": code_prefixes,
        "meta_prefixes": meta_prefixes,
        "overall_pkg_consistency": health.get("overall_consistency"),
    }

    # ── 模块清单（复用 project_map 的地图）────────────────────────────────
    mm = project_map.load_map(conn)

    # ── 实体清单：表单按字段规模降序（接手者先看大单据）──────────────────
    # 额外带每单据的插件信号（排障导航「重点单据」要的是"哪有自定义插件"，不是纯规模）。
    forms = [dict(r) for r in conn.execute(
        "SELECT f.key,f.name,f.form_type,f.module,"
        "       (SELECT COUNT(*) FROM entity e WHERE e.form_key=f.key) entity_count,"
        "       (SELECT COUNT(*) FROM field fl WHERE fl.form_key=f.key) field_count,"
        "       (SELECT COUNT(*) FROM plugin p WHERE p.form_key=f.key) plugin_count,"
        "       (SELECT COUNT(*) FROM operation o WHERE o.form_key=f.key AND o.has_operation_plugin=1)"
        "         op_with_plugin_count "
        "FROM form f ORDER BY field_count DESC"
    ).fetchall()]

    # ── 插件清单：按归属/来源计数 ─────────────────────────────────────────
    plugin_by_type = {r["plugin_type"]: r["n"] for r in conn.execute(
        "SELECT plugin_type, COUNT(*) n FROM plugin GROUP BY plugin_type")}
    plugin_by_source = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) n FROM plugin GROUP BY source")}

    # ── 单据流转（BOTP）：转换规则的源→目标，按字段映射规模降序 ──────────────
    converts = [dict(r) for r in conn.execute(
        "SELECT name, source_entity, target_entity, field_map_count, plugin_count, enabled "
        "FROM convert_rule ORDER BY field_map_count DESC")]

    # ── 风险热点（只出 KB 能算准的）────────────────────────────────────────
    missing = [dict(r) for r in conn.execute(
        "SELECT DISTINCT class_name, form_key, plugin_type FROM binding "
        "WHERE status='missing' ORDER BY class_name")]
    ambiguous = [dict(r) for r in conn.execute(
        "SELECT class_name, form_key FROM binding WHERE status='ambiguous' ORDER BY class_name")]
    oversized_forms = [f for f in forms if (f["field_count"] or 0) >= OVERSIZED_FORM_FIELDS]
    low_consistency_modules = [
        m for m in mm["modules"]
        if m["pkg_consistency"] is not None and m["pkg_consistency"] < LOW_CONSISTENCY
        and (m["class_count"] or 0) > 0
    ]
    orphan_by_module = [dict(r) for r in conn.execute(
        "SELECT module, COUNT(*) n FROM source_class "
        "WHERE is_orphan=1 AND orphan_role='unknown' GROUP BY module ORDER BY n DESC")]
    # 继承插件基类却未被任何元数据绑定的源码类（死代码/元数据未给全的重要信号）。
    plugin_orphans = [dict(r) for r in conn.execute(
        "SELECT fqn, plugin_base, module FROM source_class "
        "WHERE is_orphan=1 AND orphan_role='plugin' ORDER BY plugin_base, fqn")]

    risk = {
        "orphan_real_total": orphan_real,
        "orphan_real_by_module": orphan_by_module,
        "plugin_orphans": plugin_orphans,
        "missing_plugins": missing,
        "ambiguous_bindings": ambiguous,
        "oversized_forms": [
            {"key": f["key"], "name": f["name"], "field_count": f["field_count"]}
            for f in oversized_forms
        ],
        "low_consistency_modules": [
            {"name": m["name"], "pkg_consistency": m["pkg_consistency"]}
            for m in low_consistency_modules
        ],
    }

    # ── 字段级排障分析概况（阶段5+6+7 旗舰能力的入口指标）────────────────────
    java = json.loads(store.get_meta(conn, "java_analysis") or "{}")
    fa_writes = conn.execute(
        "SELECT COUNT(*) FROM field_access WHERE access='write'").fetchone()[0]
    fa_persist = conn.execute(
        "SELECT COUNT(*) FROM field_access WHERE access='write' AND persists='yes'").fetchone()[0]
    fa_uncertain = conn.execute(
        "SELECT COUNT(*) FROM field_access WHERE access='write' AND persists='unknown'").fetchone()[0]
    # 被最多插件写的字段（排障高频热点：多处写同一字段最易出问题）。
    hot_fields = [dict(r) for r in conn.execute(
        "SELECT field_key, COUNT(DISTINCT plugin_fqn) plugins, "
        "       SUM(CASE WHEN persists='yes' THEN 1 ELSE 0 END) persisting "
        "FROM field_access WHERE access='write' AND field_key IS NOT NULL "
        "GROUP BY field_key ORDER BY plugins DESC, field_key LIMIT 20")]
    ops_with_plugin = conn.execute(
        "SELECT COUNT(*) FROM operation WHERE has_operation_plugin=1").fetchone()[0]
    field_analysis = {
        "available": java.get("available", True),
        "analyzed_plugins": java.get("analyzed_plugins", 0),
        "field_access_total": java.get("field_access", 0),
        "write_total": fa_writes,
        "persisting_writes": fa_persist,
        "uncertain_writes": fa_uncertain,
        "ops_with_plugin": ops_with_plugin,
        "hot_fields": hot_fields,
    }

    return {
        "overview": overview_block,
        "field_analysis": field_analysis,
        "module_map": mm,
        "forms": forms,
        "converts": converts,
        "plugins": {"by_type": plugin_by_type, "by_source": plugin_by_source,
                    "total": counts.get("plugin")},
        "risk": risk,
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.0%}"
    return str(v)


def _fmt_counter(counts: dict, limit: int | None = None) -> str:
    if not counts:
        return "(无)"
    items = sorted(counts.items(), key=lambda kv: -kv[1])
    if limit:
        items = items[:limit]
    return ", ".join(f"{k}×{v}" for k, v in items)


def render_overview(ov: dict[str, Any], *, max_list: int = 20) -> str:
    """理解报告人读文本：概览 / 模块 / 实体 / 插件 / 风险热点。"""
    o = ov["overview"]
    r = ov["risk"]
    lines: list[str] = []

    lines.append("=" * 72)
    lines.append("项目排障概览（字段级定位入口）")
    lines.append(f"构建时间: {o['built_at']}")
    lines.append("=" * 72)

    # ── 排障入口（旗舰）：字段级分析概况 + 用法 ──────────────────────────────
    fa = ov.get("field_analysis") or {}
    lines.append("【排障入口】输入字段标识即可定位「谁改了它·哪个事件函数·是否落库」")
    if not fa.get("available", True):
        lines.append("  ⚠ tree-sitter 未启用，字段级分析为空：pip install -e .[parse] 后重建 KB。")
    else:
        lines.append(
            f"  已分析插件 {fa.get('analyzed_plugins', 0)}    字段写入点 {fa.get('write_total', 0)}"
            f"（落库 {fa.get('persisting_writes', 0)} / 存疑 {fa.get('uncertain_writes', 0)}）"
            f"    带自定义插件的操作 {fa.get('ops_with_plugin', 0)}"
        )
        lines.append("  用法:  cosmic_kb trace <字段标识>   或  cosmic_kb web  在浏览器输字段")
        if fa.get("hot_fields"):
            lines.append("  高频字段（被多个插件写，最易冲突；前 10）:")
            for h in fa["hot_fields"][:10]:
                lines.append(f"    - {h['field_key']:<26} {h['plugins']} 个插件写（落库 {h['persisting']}）")
    lines.append("")

    # 概览
    lines.append("【项目规模】")
    lines.append(
        f"  业务模块 {o['module_count']} 个    表单 {o['form_count']}    "
        f"实体 {o['entity_count']}    字段 {o['field_count']}    插件 {o['plugin_count']}    "
        f"转换规则 {o.get('convert_count') or 0}"
    )
    lines.append(
        f"  源码顶层类 {o['source_class_count']}    "
        f"桥接命中率 {_fmt(o['bridge_hit_rate'])}    "
        f"包结构一致度 {_fmt(o['overall_pkg_consistency'])}"
    )
    lines.append(
        f"  孤儿类: 插件类 {o.get('orphan_plugin') or 0}"
        f" / 真孤儿 {o['orphan_real']} / 常量类 {o['orphan_const']}"
    )
    lines.append("  代码包前缀: " + _fmt_counter(o["code_prefixes"], limit=8))
    lines.append("  元数据标识前缀: " + _fmt_counter(o["meta_prefixes"], limit=8))

    # 模块清单（复用 project_map 渲染）
    lines.append("")
    lines.append(project_map.render_map(ov["module_map"], max_list=max_list))

    # 实体清单（按字段规模前 N）
    lines.append("")
    lines.append(f"【实体/表单清单】共 {len(ov['forms'])}，按字段规模前 {min(max_list, len(ov['forms']))}:")
    for f in ov["forms"][:max_list]:
        lines.append(
            f"  {(f['key'] or '?'):<30} {(f['name'] or '?'):<18} "
            f"[{f['form_type']}] 实体{f['entity_count']} 字段{f['field_count']}  «{f['module'] or '?'}»"
        )

    # 单据流转（BOTP）：转换规则上下游
    converts = ov.get("converts") or []
    if converts:
        lines.append("")
        lines.append(f"【单据流转 BOTP】共 {len(converts)} 条转换规则，按字段映射规模前 {min(max_list, len(converts))}:")
        for c in converts[:max_list]:
            pn = f"  插件{c['plugin_count']}" if c["plugin_count"] else ""
            off = "" if c["enabled"] in (1, None) else "  [停用]"
            lines.append(
                f"  {(c['source_entity'] or '?')} → {(c['target_entity'] or '?')}  "
                f"«{c['name'] or '?'}»  映射{c['field_map_count']}{pn}{off}"
            )

    # 插件清单
    p = ov["plugins"]
    lines.append("")
    lines.append(f"【插件清单】共 {p['total']}")
    lines.append("  按归属: " + _fmt_counter(p["by_type"]))
    lines.append("  按来源: " + _fmt_counter(p["by_source"]))

    # 风险热点
    lines.append("")
    lines.append("【风险热点】（先看哪里）")
    lines.append(f"  真孤儿类（无元数据绑定，service/util…）: {r['orphan_real_total']}")
    if r["orphan_real_by_module"]:
        top = ", ".join(f"{x['module']}×{x['n']}" for x in r["orphan_real_by_module"][:8])
        lines.append(f"      按模块: {top}")
    # 继承插件基类却未被绑定的源码类（死代码/元数据未给全）
    po = r.get("plugin_orphans") or []
    lines.append(f"  继承插件基类却未被元数据绑定: {len(po)}（死代码 或 元数据未给全）")
    if po:
        from collections import Counter as _C
        by_base = _C(x["plugin_base"] or "?" for x in po)
        lines.append("      按基类: " + _fmt_counter(dict(by_base), limit=8))
        for x in po[:max_list]:
            lines.append(f"      - {x['fqn']}  ({x['plugin_base']})  «{x['module'] or '?'}»")
        if len(po) > max_list:
            lines.append(f"      …… 另有 {len(po) - max_list} 个（--json 看全部）")
    lines.append(f"  project 插件未找到源码(missing): {len(r['missing_plugins'])}（可能源码未给全）")
    for m in r["missing_plugins"][:max_list]:
        lines.append(f"      - {m['class_name']}  [{m['plugin_type']}] ← {m['form_key']}")
    if len(r["missing_plugins"]) > max_list:
        lines.append(f"      …… 另有 {len(r['missing_plugins']) - max_list} 条（--json 看全部）")
    lines.append(f"  歧义绑定(ambiguous): {len(r['ambiguous_bindings'])}")
    lines.append(f"  超大表单(≥{OVERSIZED_FORM_FIELDS} 字段): {len(r['oversized_forms'])}")
    for f in r["oversized_forms"][:max_list]:
        lines.append(f"      - {f['key']} ({f['name']})  {f['field_count']} 字段")
    if r["low_consistency_modules"]:
        lines.append(f"  包结构不一致模块（划分需核对）: {len(r['low_consistency_modules'])}")
        for m in r["low_consistency_modules"]:
            lines.append(f"      - {m['name']}  一致度 {_fmt(m['pkg_consistency'])}")
    fa = ov.get("field_analysis") or {}
    if fa.get("uncertain_writes"):
        lines.append(
            f"  落库存疑的字段写入: {fa['uncertain_writes']}（操作类型未知 / 调用链出本类，"
            f"需人工核对；用 trace <字段> 看明细）"
        )

    return "\n".join(lines)
