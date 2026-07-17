"""扫描可信度报告 · 手段一「字段覆盖率」（以元数据字段为分母）。

红线 #4「信任优先：覆盖率/可信度报告是一等功能」。本报告回答接手者第一个信任问题：
**「这套字段级扫描，到底覆盖了项目里多少字段？没覆盖的是真没碰、还是我们漏了？」**

核心口径——**用元数据当分母**：元数据 `field` 表是项目"应有字段"的全集（确定性、不臆造），
Java 静态分析 `field_access` 是我们"实际观测到被读/写"的字段。两者一比即得字段覆盖率。

> 重要诚实声明：**未覆盖 ≠ 缺陷**。大量字段是纯展示/纯存储，本就没有任何插件去读写它，
> 覆盖率低不代表扫描漏了。所以本报告同时给出四个**质量分解**（字段标识解析、来源定位、
> 落库判定、命中元数据），让"覆盖率"这个数字能被正确解读——这才是真正的可信度。

延续 report 包约定：dict 在前（供 --json / Web `/api/coverage`），`render_*` 文本在后。
"""

from __future__ import annotations

import json
from typing import Any

from ..graph import store
from ..java import null_reason as nr

# 业务字段（覆盖率分母）：实体字段 / 动态字段 / 基础资料属性 —— 项目真正自定义、可能被代码
# 读写的字段。平台字段(platform，如 id/billno)与继承字段(inherited)是框架给的，不纳入分母
# 以免稀释"我们对业务字段的覆盖"这个真正关心的量（其计数仍在 by_kind 里如实列出）。
BUSINESS_KINDS = ("entity", "dynamic", "basedata_prop")

# 低覆盖重点单据阈值：有自定义插件（说明确有代码行为）却覆盖率偏低 → 最该人工核对的地方。
LOW_FORM_COVERAGE = 0.5
# 低覆盖单据至少要有几个业务字段才纳入（避免几字段的小单据噪声）。
LOW_FORM_MIN_FIELDS = 5


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def coverage(conn) -> dict[str, Any]:
    """从 KB 连接组装「字段覆盖率 + 扫描质量」可信度报告 dict。"""
    java = json.loads(store.get_meta(conn, "java_analysis") or "{}")
    java_available = java.get("available", True)
    symbol = json.loads(store.get_meta(conn, "symbol_resolution") or "{}")

    placeholders = ",".join("?" * len(BUSINESS_KINDS))

    # ── 字段分类计数（透明列出，让用户知道分母怎么来的）──────────────────────
    by_kind = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) n FROM field WHERE key IS NOT NULL GROUP BY kind")}

    # ── 手段一：字段覆盖率（逐单据算，再聚合到模块/全项目，口径一致）──────────
    # 每单据：业务字段数(distinct key) + 被任意 field_access 命中的字段数。
    per_form_rows = conn.execute(
        f"SELECT f.form_key form_key, "
        f"       COUNT(DISTINCT f.key) business, "
        f"       COUNT(DISTINCT CASE WHEN fa.field_key IS NOT NULL THEN f.key END) touched "
        f"FROM field f "
        f"LEFT JOIN field_access fa ON fa.form_key=f.form_key AND fa.field_key=f.key "
        f"WHERE f.kind IN ({placeholders}) AND f.key IS NOT NULL "
        f"GROUP BY f.form_key",
        BUSINESS_KINDS,
    ).fetchall()

    # 表单元信息（名/模块/插件数）：用于模块聚合与低覆盖单据定位。
    form_meta = {r["key"]: dict(r) for r in conn.execute(
        "SELECT f.key, f.name, f.module, "
        "       (SELECT COUNT(*) FROM plugin p WHERE p.form_key=f.key) plugin_count "
        "FROM form f")}

    forms: list[dict[str, Any]] = []
    module_agg: dict[str, dict[str, int]] = {}
    business_total = touched_total = 0
    for r in per_form_rows:
        fk, biz, touched = r["form_key"], r["business"], r["touched"]
        business_total += biz
        touched_total += touched
        meta = form_meta.get(fk, {})
        module = meta.get("module") or "未归类"
        agg = module_agg.setdefault(module, {"business": 0, "touched": 0, "forms": 0})
        agg["business"] += biz
        agg["touched"] += touched
        agg["forms"] += 1
        forms.append({
            "key": fk, "name": meta.get("name"), "module": module,
            "plugin_count": meta.get("plugin_count", 0),
            "business": biz, "touched": touched, "rate": _rate(touched, biz),
        })

    by_module = sorted(
        ({"module": m, **v, "rate": _rate(v["touched"], v["business"])}
         for m, v in module_agg.items()),
        key=lambda x: (-(x["rate"] or 0), -x["business"]),
    )

    # 低覆盖重点单据：有插件 + 字段够多 + 覆盖率偏低 —— 排障人最该人工核对的盲区。
    low_coverage_forms = sorted(
        (f for f in forms
         if f["plugin_count"] > 0 and f["business"] >= LOW_FORM_MIN_FIELDS
         and (f["rate"] or 0) < LOW_FORM_COVERAGE),
        key=lambda f: (f["rate"] or 0, -f["business"]),
    )

    # 读/写各自覆盖了多少业务字段（写比读更关键：写才可能改坏数据）。
    write_touched = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT DISTINCT f.form_key, f.key FROM field f "
        f"JOIN field_access fa ON fa.form_key=f.form_key AND fa.field_key=f.key "
        f"WHERE f.kind IN ({placeholders}) AND f.key IS NOT NULL AND fa.access='write')",
        BUSINESS_KINDS,
    ).fetchone()[0]
    read_touched = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT DISTINCT f.form_key, f.key FROM field f "
        f"JOIN field_access fa ON fa.form_key=f.form_key AND fa.field_key=f.key "
        f"WHERE f.kind IN ({placeholders}) AND f.key IS NOT NULL AND fa.access='read')",
        BUSINESS_KINDS,
    ).fetchone()[0]

    field_coverage = {
        "business_total": business_total,
        "touched": touched_total,
        "untouched": business_total - touched_total,
        "rate": _rate(touched_total, business_total),
        "write_touched": write_touched,
        "read_touched": read_touched,
        "by_kind": by_kind,
        "business_kinds": list(BUSINESS_KINDS),
        "by_module": by_module,
        "low_coverage_forms": [
            {"key": f["key"], "name": f["name"], "module": f["module"],
             "plugin_count": f["plugin_count"], "business": f["business"],
             "touched": f["touched"], "rate": f["rate"]}
            for f in low_coverage_forms
        ],
    }

    # ── 质量分解①：字段标识解析（literal/constant 可信；ambiguous/unknown/dynamic 存疑）──
    res_counts = {r["key_resolution"]: r["n"] for r in conn.execute(
        "SELECT key_resolution, COUNT(*) n FROM field_access GROUP BY key_resolution")}
    res_total = sum(res_counts.values())
    res_reliable = res_counts.get("literal", 0) + res_counts.get("constant", 0)
    resolution_quality = {
        "total": res_total,
        "by_resolution": res_counts,
        "reliable": res_reliable,
        "uncertain": res_total - res_reliable,
        "reliable_rate": _rate(res_reliable, res_total),
    }

    # ── 质量分解②：来源定位（form_key 判得出且 level 不为 unknown 才算定位到实体坐标）──
    loc_total = res_total
    located = conn.execute(
        "SELECT COUNT(*) FROM field_access WHERE form_key IS NOT NULL AND level!='unknown'"
    ).fetchone()[0]
    unlocated_form = conn.execute(
        "SELECT COUNT(*) FROM field_access WHERE form_key IS NULL").fetchone()[0]
    unknown_level = conn.execute(
        "SELECT COUNT(*) FROM field_access WHERE form_key IS NOT NULL AND level='unknown'"
    ).fetchone()[0]
    # 未定位成因分布（信任优先）：把「未定位单据」从一个不透明数字拆成确定性成因——哪些是
    # 「正确 None」（基础资料/动态实体，无需追）、哪些值得段二顺源码反推（helper/容器/model）。
    unlocated_reasons = {r["null_reason"] or nr.UNKNOWN: r["n"] for r in conn.execute(
        "SELECT null_reason, COUNT(*) n FROM field_access WHERE form_key IS NULL "
        "GROUP BY null_reason ORDER BY n DESC")}
    correct_none = sum(v for k, v in unlocated_reasons.items() if k in nr.CORRECT_NONE_REASONS)
    location_quality = {
        "total": loc_total,
        "located": located,
        "unlocated_form": unlocated_form,
        "unknown_level": unknown_level,
        "located_rate": _rate(located, loc_total),
        "unlocated_reasons": unlocated_reasons,
        "unlocated_correct_none": correct_none,   # 这部分本就该 None，不计入「待救」
    }

    # ── 质量分解③：落库判定（仅写入；yes/no 为确定，unknown 为存疑）─────────────
    w_total = conn.execute(
        "SELECT COUNT(*) FROM field_access WHERE access='write'").fetchone()[0]
    w_yes = conn.execute(
        "SELECT COUNT(*) FROM field_access WHERE access='write' AND persists='yes'").fetchone()[0]
    w_no = conn.execute(
        "SELECT COUNT(*) FROM field_access WHERE access='write' AND persists='no'").fetchone()[0]
    w_unknown = conn.execute(
        "SELECT COUNT(*) FROM field_access WHERE access='write' AND persists='unknown'"
    ).fetchone()[0]
    persist_quality = {
        "write_total": w_total,
        "persisting": w_yes,
        "memory_only": w_no,
        "uncertain": w_unknown,
        "certain_rate": _rate(w_yes + w_no, w_total),
    }

    # ── 质量分解④：命中元数据（解析出的 field_key 是否对得上元数据字段表）──────────
    #   对不上 = 可能平台字段 / 常量解析偏差 / 源码用了元数据没有的 key —— 反向信任信号。
    meta_keys = {r[0] for r in conn.execute(
        "SELECT DISTINCT key FROM field WHERE key IS NOT NULL")}
    resolved = matched = 0
    for r in conn.execute(
        "SELECT field_key, COUNT(*) n FROM field_access WHERE field_key IS NOT NULL "
        "GROUP BY field_key"
    ).fetchall():
        resolved += r["n"]
        if r["field_key"] in meta_keys:
            matched += r["n"]
    meta_match = {
        "resolved": resolved,
        "matched": matched,
        "unmatched": resolved - matched,
        "match_rate": _rate(matched, resolved),
    }

    # ── 质量分解⑤：调用边精度（schema v18）──────────────────────────────
    edge_counts = {r["edge_source"] or "heuristic": r["n"] for r in conn.execute(
        "SELECT edge_source, COUNT(*) n FROM field_access GROUP BY edge_source")}
    edge_total = sum(edge_counts.values())
    edge_quality = {
        "total": edge_total,
        "by_source": edge_counts,
        "exact": edge_counts.get("local", 0) + edge_counts.get("symbol", 0),
        "fallback": edge_counts.get("heuristic", 0),
        "mixed": edge_counts.get("mixed", 0),
    }

    # ── 上游可信度（桥接命中率 / Java 是否启用）——覆盖率天花板由它们决定 ──────────
    status_counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM binding GROUP BY status")}
    linked = status_counts.get("linked", 0) + status_counts.get("linked_by_name", 0)
    project_total = linked + status_counts.get("missing", 0) + status_counts.get("ambiguous", 0)
    upstream = {
        "java_available": java_available,
        "analyzed_plugins": java.get("analyzed_plugins", 0),
        "field_access_total": java.get("field_access", 0),
        "bridge_hit_rate": _rate(linked, project_total),
        "bridge_missing": status_counts.get("missing", 0),
        "symbol_resolution": symbol,
    }

    result = {
        "field_coverage": field_coverage,
        "resolution_quality": resolution_quality,
        "location_quality": location_quality,
        "persist_quality": persist_quality,
        "meta_match": meta_match,
        "edge_quality": edge_quality,
        "upstream": upstream,
    }
    result["verdict"] = _verdict(result)
    return result


def _verdict(c: dict[str, Any]) -> dict[str, str]:
    """据覆盖率 + 质量给一句总信任结论（含 level 供前端上色）。"""
    if not c["upstream"]["java_available"]:
        return {"level": "blocked",
                "text": "⚠ tree-sitter 未启用，字段级扫描为空，可信度无从谈起。"
                        "请 pip install -e .[parse] 后重建 KB。"}
    fc = c["field_coverage"]
    rate = fc["rate"] or 0
    res_rate = c["resolution_quality"]["reliable_rate"] or 0
    loc_rate = c["location_quality"]["located_rate"] or 0
    if rate >= 0.6 and res_rate >= 0.9 and loc_rate >= 0.9:
        lvl, head = "good", "✅ 扫描可信度高"
    elif rate >= 0.3 and res_rate >= 0.75:
        lvl, head = "ok", "🟡 扫描可信度中等"
    else:
        lvl, head = "low", "🔴 扫描可信度偏低"
    return {"level": lvl, "text": (
        f"{head}：业务字段覆盖 {rate:.0%}（{fc['touched']}/{fc['business_total']}）、"
        f"字段标识解析可信 {res_rate:.0%}、来源定位 {loc_rate:.0%}。"
        f"未覆盖字段多为纯展示/纯存储，属正常；重点看下方「低覆盖重点单据」。")}


# ── 人读文本（CLI）──────────────────────────────────────────────────────────

def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.0%}"


def _bar(v: float | None, width: int = 24) -> str:
    if v is None:
        return "[" + "·" * width + "]"
    fill = round(v * width)
    return "[" + "█" * fill + "·" * (width - fill) + "]"


def render_coverage(c: dict[str, Any], *, max_list: int = 20) -> str:
    fc = c["field_coverage"]
    rq = c["resolution_quality"]
    lq = c["location_quality"]
    pq = c["persist_quality"]
    mm = c["meta_match"]
    eq = c.get("edge_quality", {})
    up = c["upstream"]
    lines: list[str] = []

    lines.append("=" * 72)
    lines.append("扫描可信度报告 · 手段一「字段覆盖率」（以元数据字段为分母）")
    lines.append("=" * 72)
    lines.append(c["verdict"]["text"])
    lines.append("")

    # 手段一：字段覆盖率
    lines.append("【字段覆盖率】元数据业务字段 = 分母；代码里观测到被读/写 = 分子")
    lines.append(
        f"  {_bar(fc['rate'])} {_pct(fc['rate'])}   "
        f"覆盖 {fc['touched']} / {fc['business_total']} 个业务字段"
        f"（未覆盖 {fc['untouched']}）"
    )
    lines.append(
        f"  其中：被写 {fc['write_touched']} · 被读 {fc['read_touched']}  "
        f"（业务字段类别 = {', '.join(fc['business_kinds'])}）"
    )
    bk = fc["by_kind"]
    lines.append("  字段分类计数: " + ", ".join(f"{k}×{v}" for k, v in sorted(
        bk.items(), key=lambda kv: -kv[1])))
    lines.append("  注：未覆盖 ≠ 漏扫——大量字段纯展示/纯存储，本就无插件读写。")

    # 按模块
    if fc["by_module"]:
        lines.append("")
        lines.append("【按模块覆盖率】")
        for m in fc["by_module"][:max_list]:
            lines.append(
                f"  {_bar(m['rate'], 16)} {_pct(m['rate']):>4}  "
                f"{(m['module'] or '?'):<20} {m['touched']}/{m['business']}"
            )

    # 低覆盖重点单据
    if fc["low_coverage_forms"]:
        lines.append("")
        lines.append(
            f"【低覆盖重点单据】有自定义插件却覆盖<{LOW_FORM_COVERAGE:.0%}（最该人工核对，"
            f"前 {min(max_list, len(fc['low_coverage_forms']))}）:")
        for f in fc["low_coverage_forms"][:max_list]:
            lines.append(
                f"  {_pct(f['rate']):>4}  {(f['key'] or '?'):<28}"
                f"{('「'+f['name']+'」') if f.get('name') else '':<14} "
                f"覆盖 {f['touched']}/{f['business']} · 插件 {f['plugin_count']} «{f['module']}»"
            )

    # 质量分解
    lines.append("")
    lines.append("【扫描质量分解】（让覆盖率这个数字能被正确解读）")
    lines.append(
        f"  ① 字段标识解析可信 {_bar(rq['reliable_rate'], 16)} {_pct(rq['reliable_rate'])}  "
        f"（{rq['reliable']}/{rq['total']} 为字面量/常量；存疑 {rq['uncertain']}）")
    lines.append(
        f"  ② 来源单据定位     {_bar(lq['located_rate'], 16)} {_pct(lq['located_rate'])}  "
        f"（未定位单据 {lq['unlocated_form']} · 层级未知 {lq['unknown_level']}）")
    if lq.get("unlocated_reasons"):
        cn = lq.get("unlocated_correct_none", 0)
        lines.append(
            f"     未定位成因（共 {lq['unlocated_form']}，其中本就该 None 约 {cn}，无需追）：")
        for k, v in lq["unlocated_reasons"].items():
            lines.append(f"       · {nr.REASON_LABEL.get(k, k)}：{v}")
    lines.append(
        f"  ③ 落库判定确定     {_bar(pq['certain_rate'], 16)} {_pct(pq['certain_rate'])}  "
        f"（落库 {pq['persisting']} / 内存 {pq['memory_only']} / 存疑 {pq['uncertain']}，共 {pq['write_total']} 写）")
    lines.append(
        f"  ④ 命中元数据字段   {_bar(mm['match_rate'], 16)} {_pct(mm['match_rate'])}  "
        f"（对不上 {mm['unmatched']}：多为平台字段/常量解析偏差，共解析 {mm['resolved']}）")
    if eq:
        by = eq.get("by_source", {})
        lines.append(
            "  ⑤ 调用边精度       "
            f"local {by.get('local', 0)} / symbol {by.get('symbol', 0)} / "
            f"mixed {by.get('mixed', 0)} / heuristic {by.get('heuristic', 0)}")

    # 上游
    lines.append("")
    lines.append("【上游可信度】（覆盖率的天花板）")
    lines.append(
        f"  Java 分析 {'✅启用' if up['java_available'] else '⚠未启用'} · "
        f"已分析插件 {up['analyzed_plugins']} · "
        f"桥接命中率 {_pct(up['bridge_hit_rate'])}（missing {up['bridge_missing']}）")
    sym = up.get("symbol_resolution") or {}
    sym_status = sym.get("status", "未记录")
    sym_cov = sym.get("coverage")
    lines.append(
        f"  编译期符号 {sym_status} · provider={sym.get('provider') or '—'} · "
        f"jar={sym.get('jar_count', 0)} · coverage={_pct(sym_cov)}")
    if sym.get("reason"):
        lines.append(f"  符号降级原因: {sym['reason']}")
    return "\n".join(lines)
