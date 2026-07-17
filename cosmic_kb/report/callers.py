"""阶段 12.3 · Java 方法反向调用查询（``callers``）。

输入 ``Class.method`` 或 ``完整包名.Class.method``，返回所有静态调用点。简单类名跨包重名时
沿用 trace 的反问范式，不替调用方猜选。所有结果（含错误/消歧/零命中）都附
``resolution_coverage``，让“0 个调用方”能按符号层质量被正确解读。
"""

from __future__ import annotations

import json
from typing import Any

from ..graph import store

_STRONG_COVERAGE = 0.95
_MCP_PAGE_SIZE = 40
_MCP_BUDGET = 28 * 1024


def resolution_coverage(conn) -> dict[str, Any]:
    """调用边实际分桶 + 建库时符号层状态；两者一起决定零结果证据强度。"""
    by = {r["resolution"]: r["n"] for r in conn.execute(
        "SELECT resolution, COUNT(*) n FROM call_edge GROUP BY resolution")}
    persisted = sum(by.values())
    persisted_resolved = by.get("expr", 0) + by.get("scope", 0)
    try:
        symbol = json.loads(store.get_meta(conn, "symbol_resolution") or "{}")
    except (TypeError, ValueError):
        symbol = {}
    coverage = symbol.get("coverage")
    if not isinstance(coverage, (int, float)):
        coverage = round(persisted_resolved / persisted, 4) if persisted else 0.0
    status = symbol.get("status") or "unknown"
    files_failed = symbol.get("files_failed", 0)
    strong = bool(
        status == "ok" and coverage >= _STRONG_COVERAGE and not files_failed)
    return {
        "status": status,
        "coverage": coverage,
        "threshold_for_strong_zero": _STRONG_COVERAGE,
        "files": symbol.get("files", 0),
        "files_failed": files_failed,
        "sites": symbol.get("sites", persisted),
        "resolved": symbol.get("resolved", persisted_resolved),
        "persisted_edges": persisted,
        "by_resolution": by,
        "strong_zero_evidence": strong,
        "reason": symbol.get("reason"),
    }


def _simple_name(fqn: str) -> str:
    return fqn.rsplit(".", 1)[-1].rsplit("$", 1)[-1]


def _class_candidates(conn, class_name: str, method: str) -> list[dict[str, Any]]:
    """从项目类声明与已解析平台目标联合找类；项目死代码即使 0 入边也能被简单名定位。"""
    candidates: dict[str, dict[str, Any]] = {}
    if "." in class_name:
        source_rows = conn.execute(
            "SELECT fqn, relpath FROM source_class WHERE fqn=?", (class_name,)).fetchall()
    else:
        source_rows = conn.execute(
            "SELECT fqn, relpath FROM source_class WHERE simple=?", (class_name,)).fetchall()
    for row in source_rows:
        candidates[row["fqn"]] = {
            "target_fqn": row["fqn"], "method": method,
            "source_relpath": row["relpath"], "target_kind": "project",
        }

    # 平台/JDK 类不在 source_class；也纳入已出现过的 call_edge 目标。
    for row in conn.execute(
        "SELECT DISTINCT target_fqn, target_kind FROM call_edge WHERE target_fqn IS NOT NULL"):
        fqn = row["target_fqn"]
        if (fqn == class_name if "." in class_name else _simple_name(fqn) == class_name):
            candidates.setdefault(fqn, {
                "target_fqn": fqn, "method": method,
                "source_relpath": None, "target_kind": row["target_kind"],
            })

    for fqn, item in candidates.items():
        item["locator"] = f"{fqn}.{method}"
        item["call_site_count"] = conn.execute(
            "SELECT COUNT(*) FROM call_edge WHERE target_fqn=? AND target_method=?",
            (fqn, method),
        ).fetchone()[0]
    return sorted(candidates.values(), key=lambda x: x["target_fqn"])


def callers(conn, locator: str) -> dict[str, Any]:
    """反查某 Java 方法的全部静态调用点（富投影，CLI ``--json`` 与 MCP 共用）。"""
    coverage = resolution_coverage(conn)
    locator = locator.strip()
    if not locator or "." not in locator:
        return {
            "error": "方法坐标应写 Class.method 或 完整包名.Class.method。",
            "query": locator,
            "resolution_coverage": coverage,
        }
    class_name, method = locator.rsplit(".", 1)
    if not class_name or not method:
        return {
            "error": "方法坐标应写 Class.method 或 完整包名.Class.method。",
            "query": locator,
            "resolution_coverage": coverage,
        }

    candidates = _class_candidates(conn, class_name, method)
    if not candidates:
        return {
            "error": f"KB 中没有找到目标类「{class_name}」；请核对全限定名。",
            "query": locator,
            "resolution_coverage": coverage,
        }
    if len(candidates) > 1:
        return {
            "kind": "callers",
            "status": "need_clarification",
            "query": locator,
            "class_name": class_name,
            "method": method,
            "candidates": candidates,
            "note": "简单类名跨包重名，请从 candidates 选择完整 locator 后重查；不按调用数猜选。",
            "resolution_coverage": coverage,
        }

    target = candidates[0]
    fqn = target["target_fqn"]
    rows = [dict(r) for r in conn.execute(
        "SELECT caller_fqn,caller_method,target_signature,kind,line,col,source_relpath,"
        "resolution,target_kind,confidence,evidence "
        "FROM call_edge WHERE target_fqn=? AND target_method=? "
        "ORDER BY source_relpath,line,col,caller_fqn,caller_method",
        (fqn, method),
    )]
    by_resolution: dict[str, int] = {}
    for row in rows:
        res = row.get("resolution") or "failed"
        by_resolution[res] = by_resolution.get(res, 0) + 1

    if rows:
        note = (
            f"发现 {len(rows)} 个静态调用点；resolution 标明逐边是符号精确绑定还是名字兜底。"
        )
    elif coverage["strong_zero_evidence"]:
        note = (
            "未发现指向该方法坐标的静态调用边；符号层完整可用且覆盖率达到 95%，"
            "这是“查无调用方”的强证据。仍不覆盖反射、字符串配置或运行时动态分派。"
        )
    else:
        note = (
            "未发现指向该方法坐标的调用边，但符号层不可用或覆盖率不足；当前至多是名字匹配口径，"
            "不足以断言死代码。"
        )

    return {
        "kind": "callers",
        "query": locator,
        "target": target,
        "callers": rows,
        "summary": {
            "call_sites": len(rows),
            "caller_methods": len({(r["caller_fqn"], r["caller_method"]) for r in rows}),
            "method_references": sum(1 for r in rows if r["kind"] == "method_reference"),
            "by_resolution": by_resolution,
        },
        "resolution_coverage": coverage,
        "note": note,
    }


def callers_compact(conn, locator: str, *, cursor: str | None = None) -> dict[str, Any]:
    """MCP 有界投影：逐页返回 callers，避免平台热点方法把 host 响应从中段截断。

    cursor 形如 ``callers@40``。每页先按 40 条切，再受 28KB 字节预算约束；被截条目一定给
    ``pagination.next_cursor``，可重复调用直至 ``complete=true``，不以 cap 冒充完整结果。
    """
    result = callers(conn, locator)
    if result.get("error") or result.get("status") == "need_clarification":
        return result
    offset = 0
    if cursor:
        prefix, sep, raw = cursor.partition("@")
        try:
            offset = int(raw) if sep and prefix == "callers" else -1
        except ValueError:
            offset = -1
        if offset < 0:
            return {
                "error": "callers cursor 无效；应使用返回的 pagination.next_cursor（形如 callers@40）。",
                "query": locator,
                "resolution_coverage": result["resolution_coverage"],
            }

    all_rows = result["callers"]
    items = all_rows[offset:offset + _MCP_PAGE_SIZE]
    compact = dict(result)
    compact["callers"] = items
    while len(items) > 1:
        compact["pagination"] = {
            "total": len(all_rows), "offset": offset, "returned": len(items),
            "complete": offset + len(items) >= len(all_rows),
            "next_cursor": (None if offset + len(items) >= len(all_rows)
                            else f"callers@{offset + len(items)}"),
        }
        if len(json.dumps(compact, ensure_ascii=False).encode("utf-8")) <= _MCP_BUDGET:
            break
        items = items[:-1]
        compact["callers"] = items
    next_offset = offset + len(items)
    compact["pagination"] = {
        "total": len(all_rows), "offset": offset, "returned": len(items),
        "complete": next_offset >= len(all_rows),
        "next_cursor": None if next_offset >= len(all_rows) else f"callers@{next_offset}",
    }
    if not compact["pagination"]["complete"]:
        compact["note"] = result["note"] + (
            " 当前为有界页；按 pagination.next_cursor 继续调用 callers，直至 complete=true。")
    return compact


def render_callers(result: dict[str, Any], *, max_list: int = 50) -> str:
    """CLI 人读文本；全部明细仍可用 ``--json`` 取得。"""
    cov = result.get("resolution_coverage") or {}
    cov_line = (
        f"符号覆盖: status={cov.get('status', 'unknown')} · "
        f"coverage={cov.get('coverage', 0):.1%} · "
        f"call_edge={cov.get('persisted_edges', 0)}"
    )
    if result.get("error"):
        return f"错误: {result['error']}\n{cov_line}"
    if result.get("status") == "need_clarification":
        lines = ["需要消歧: " + result["note"], cov_line, "候选："]
        lines.extend(f"  - {c['locator']}  (调用点 {c['call_site_count']})"
                     for c in result["candidates"][:max_list])
        return "\n".join(lines)

    target = result["target"]
    lines = [
        "=" * 72,
        f"Java 方法调用方 · {target['target_fqn']}.{target['method']}",
        "=" * 72,
        cov_line,
        result["note"],
    ]
    rows = result["callers"]
    if rows:
        lines.append("")
        lines.append(f"调用点（{len(rows)}）：")
        for row in rows[:max_list]:
            ref = " · method_reference" if row["kind"] == "method_reference" else ""
            lines.append(
                f"  - {row['caller_fqn'] or '<unknown>'}#{row['caller_method']}  "
                f"{row['source_relpath']}:{row['line']}:{row['col']}  "
                f"[{row['resolution']} confidence={row['confidence']:.2f}]{ref}")
        if len(rows) > max_list:
            lines.append(f"  … 其余 {len(rows) - max_list} 条见 --json")
    return "\n".join(lines)


__all__ = ["callers", "callers_compact", "render_callers", "resolution_coverage"]
