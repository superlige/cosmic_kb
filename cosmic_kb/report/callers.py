"""阶段 12.3+ · Java 方法反向调用查询 + 苍穹入口可达性/死代码判定（``callers``）。

输入 ``Class.method`` 或 ``完整包名.Class.method``，返回所有静态调用点。简单类名跨包重名时
沿用 trace 的反问范式，不替调用方猜选。所有结果（含错误/消歧/零命中）都附
``resolution_coverage``，让“0 个调用方”能按符号层质量被正确解读。

苍穹平台上任何方法的最终入口都是插件事件函数——死代码判定光看"0 个静态调用点"不够（IDEA
本就能查），必须沿调用链上溯到插件类后反查元数据注册状态：界面/列表/操作/转换规则插件 KB
有注册表可确定是否注册+启用；调度计划/开放平台/工作流等 KB 未接入配置表的插件种类，如实报
"无法确定是否平台注册"（红线 #4：三态诚实、不臆造）。``entry_analysis`` 字段复用
``entry_chain.entry_chains()`` 的反向 BFS + 新增 ``entry_chain.registration_status()``
做落地判定，verdict 五值见 ``entry_analysis()`` docstring。
"""

from __future__ import annotations

import json
from typing import Any

from ..graph import store
from . import entry_chain

_STRONG_COVERAGE = 0.95
_MCP_PAGE_SIZE = 40
_MCP_BUDGET = 28 * 1024

# 入口可达性分析口径：BFS 分析上限比展示口径宽（先把全貌算完整，展示时再裁）。
_EA_MAX_CHAINS = 12
_EA_SHOW_CHAINS = 3
_EA_MAX_BINDINGS = 8

_INACTIVE_STATUSES = frozenset({"registered_disabled", "orphan_unregistered"})

_NOTE = {
    "entry_reachable": "至少一个入口/边界插件类已确认注册且启用，静态可达。",
    "entries_inactive": "找到的入口/边界插件类均已禁用或未在 KB 注册表注册，疑似不可达"
                        "（不排除反射、动态注册等运行时入口，不得直接断言死代码）。",
    "entry_unverifiable": "入口的注册/启用状态无法确认（KB 未接入该类注册表、启用状态未知，"
                          "或负向结论因截断/符号层弱化被降级），不足以断言可达或不可达。",
    "no_entry_found": "静态调用链沿 call_edge 向上追不到任何插件事件入口。",
}

_VERDICT_ZH = {
    "entry_reachable": "可达（已找到已注册且启用的插件入口）",
    "entries_inactive": "疑似不可达（入口均已禁用/未注册，不排除反射/动态注册等运行时入口）",
    "entry_unverifiable": "可达性未知（注册信息不足，或截断/符号层弱化拉低了置信度）",
    "no_entry_found": "静态追不到任何插件事件入口",
    "not_analyzed": "平台/JDK 目标，未做入口可达性分析",
}


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


def entry_analysis(conn, fqn: str, method: str, coverage: dict[str, Any], *,
                   max_depth: int = 10, max_chains: int = _EA_MAX_CHAINS,
                   max_nodes: int = 400) -> dict[str, Any]:
    """反向 BFS 回溯插件事件入口 + 反查元数据注册/启用状态 → 苍穹入口可达性判定。

    verdict 五值（``not_analyzed`` 由调用方对平台/JDK 目标短路，本函数只产生其余四值）：
      * ``entry_reachable``    ≥1 个入口/边界插件类 ``registration.status==registered_enabled``；
        confidence 该链无 heuristic 边（或本身即 self_entry）→ confirmed，否则 likely。
      * ``entries_inactive``   找到入口/边界类，且全部 registered_disabled/orphan_unregistered，
        且 BFS 无截断、符号层可用（status=ok 且 files_failed=0）→ likely（永不 confirmed，
        反射/动态注册这类静态分析看不到的入口始终可能存在）。
      * ``entry_unverifiable`` 存在 registered_enabled_unknown/orphan_unverifiable/
        no_registration_evidence 状态的入口；或本应判 entries_inactive 但遇截断/符号层弱化，
        降级为 unknown（红线 #4：负向结论绝不带截断冒充完整）。
      * ``no_entry_found``     entry_chains status=not_found；``resolution_coverage`` 达到
        strong_zero_evidence 且无截断 → likely，否则 unknown（同样永不 confirmed）。
    """
    ec = entry_chain.entry_chains(conn, fqn, method, max_depth=max_depth,
                                  max_chains=max_chains, max_nodes=max_nodes)
    symbol_weak = not (coverage.get("status") == "ok" and not coverage.get("files_failed"))
    truncated = bool(ec.get("chains_truncated")) or bool(ec.get("search_truncated"))

    entries: list[dict[str, Any]] = []
    chain_confirmed: dict[str, bool] = {}

    if ec["status"] == "self_entry":
        e = ec["entry"]
        entries.append({
            "class": fqn, "terminal": "self",
            "events": [{"event": e["event"], "phase": e["phase"]}],
            "registration": entry_chain.registration_status(conn, fqn),
        })
        chain_confirmed[fqn] = True
        truncated = False
    elif ec["status"] == "not_found":
        strong = bool(coverage.get("strong_zero_evidence")) and not truncated
        verdict = "no_entry_found"
        confidence = "likely" if strong else "unknown"
        note = _NOTE[verdict]
        if not strong:
            note += ("符号层不可用/覆盖不足，或搜索被截断，当前至多是名字匹配口径，"
                    "不足以断言死代码或无入口。")
        return {"verdict": verdict, "confidence": confidence, "chain_status": ec["status"],
                "entries": [], "chains": ec["chains"],
                "chains_truncated": ec["chains_truncated"],
                "search_truncated": ec["search_truncated"], "note": note}
    else:
        by_class: dict[str, dict[str, Any]] = {}
        for c in ec["chains"]:
            if c["terminal"] not in ("entry", "plugin_boundary"):
                continue
            extra = c.get("entry") or {}
            cls = extra.get("class")
            if not cls:
                continue
            if c["confidence"] == "confirmed":
                chain_confirmed[cls] = True
            else:
                chain_confirmed.setdefault(cls, False)
            slot = by_class.setdefault(cls, {"terminal": c["terminal"], "events": []})
            if c["terminal"] == "entry":
                ev = {"event": extra.get("event"), "phase": extra.get("phase")}
                if ev not in slot["events"]:
                    slot["events"].append(ev)
        for cls in sorted(by_class):
            slot = by_class[cls]
            entry: dict[str, Any] = {
                "class": cls, "terminal": slot["terminal"],
                "registration": entry_chain.registration_status(conn, cls),
            }
            if slot["terminal"] == "entry":
                entry["events"] = slot["events"]
            entries.append(entry)

    reachable = [e for e in entries if e["registration"]["status"] == "registered_enabled"]
    would_be_inactive = bool(entries) and all(
        e["registration"]["status"] in _INACTIVE_STATUSES for e in entries)

    if reachable:
        verdict = "entry_reachable"
        confidence = ("confirmed" if any(chain_confirmed.get(e["class"]) for e in reachable)
                      else "likely")
        note = _NOTE[verdict]
    elif would_be_inactive and not truncated and not symbol_weak:
        verdict = "entries_inactive"
        confidence = "likely"
        note = _NOTE[verdict]
    elif entries:
        verdict = "entry_unverifiable"
        confidence = "unknown"
        note = _NOTE[verdict]
        if would_be_inactive:
            reasons = []
            if truncated:
                reasons.append("入口搜索被截断（chains_truncated/search_truncated）")
            if symbol_weak:
                reasons.append("符号解析层不可用或存在失败文件")
            note += ("本应判定为「疑似不可达」，但" + "、".join(reasons) +
                    "，降级为无法确认，不排除遗漏的其他入口。")
    else:
        # reached/boundary_only 状态理论上必有 entries；防御兜底仍如实给 unverifiable。
        verdict = "entry_unverifiable"
        confidence = "unknown"
        note = _NOTE[verdict]

    return {"verdict": verdict, "confidence": confidence, "chain_status": ec["status"],
            "entries": entries, "chains": ec.get("chains", []),
            "chains_truncated": ec.get("chains_truncated", 0),
            "search_truncated": ec.get("search_truncated", False), "note": note}


def _build_entry_analysis(conn, target: dict[str, Any], method: str,
                          coverage: dict[str, Any]) -> dict[str, Any]:
    if target["target_kind"] in ("jar", "jdk"):
        return {
            "verdict": "not_analyzed", "confidence": None, "chain_status": None,
            "entries": [], "reason": "platform_target",
            "note": "目标是平台/JDK 类，不跑入口可达性回溯（超出项目源码范围，无法判定注册归属）。",
        }
    return entry_analysis(conn, target["target_fqn"], method, coverage)


def _slim_registration(reg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"status": reg["status"], "kind": reg.get("kind")}
    if reg.get("plugin_base"):
        out["plugin_base"] = reg["plugin_base"]
    bindings = reg.get("bindings") or []
    out["bindings"] = bindings[:_EA_MAX_BINDINGS]
    if len(bindings) > _EA_MAX_BINDINGS:
        out["bindings_truncated"] = len(bindings) - _EA_MAX_BINDINGS
    if reg.get("note"):
        out["note"] = reg["note"]
    return out


def _slim_entry_analysis(ea: dict[str, Any] | None) -> dict[str, Any] | None:
    """MCP 紧凑投影：注册状态压缩 bindings 上限，chains 复用 entry_chain 的紧凑路径字符串。"""
    if ea is None:
        return None
    out: dict[str, Any] = {"verdict": ea["verdict"], "confidence": ea.get("confidence"),
                           "chain_status": ea.get("chain_status"), "note": ea.get("note")}
    if ea.get("reason"):
        out["reason"] = ea["reason"]
    out["entries"] = []
    for e in ea.get("entries", []):
        se: dict[str, Any] = {"class": e["class"], "terminal": e["terminal"],
                              "registration": _slim_registration(e["registration"])}
        if e.get("events"):
            se["events"] = e["events"]
        out["entries"].append(se)
    if ea.get("chains"):
        ec_like = {"status": ea.get("chain_status"), "chains": ea["chains"],
                  "chains_truncated": ea.get("chains_truncated", 0),
                  "search_truncated": ea.get("search_truncated", False)}
        sc = entry_chain.slim_chains(ec_like, max_chains=_EA_SHOW_CHAINS, max_hops=8)
        out["chains"] = sc["chains"]
        if sc.get("chains_truncated"):
            out["chains_truncated"] = sc["chains_truncated"]
        if sc.get("search_truncated"):
            out["search_truncated"] = True
    return out


def _verdict_note_suffix(ea: dict[str, Any]) -> str:
    zh = _VERDICT_ZH.get(ea["verdict"], ea["verdict"])
    conf = f"（{ea['confidence']}）" if ea.get("confidence") else ""
    return f" 入口可达性：{zh}{conf}。"


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

    ea = _build_entry_analysis(conn, target, method, coverage)
    note += _verdict_note_suffix(ea)

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
        "entry_analysis": ea,
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
    ea = result.get("entry_analysis")
    if ea is not None:
        compact["entry_analysis"] = (
            _slim_entry_analysis(ea) if offset == 0 else
            {"verdict": ea["verdict"], "confidence": ea.get("confidence"),
             "note": "完整入口分析见第一页"})
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
    if (offset == 0 and compact.get("entry_analysis", {}).get("chains") and
            len(json.dumps(compact, ensure_ascii=False).encode("utf-8")) > _MCP_BUDGET):
        del compact["entry_analysis"]["chains"]
        compact["entry_analysis"]["chains_omitted"] = True
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
    lines.extend(_render_entry_analysis(result.get("entry_analysis")))
    return "\n".join(lines)


def _render_registration_line(reg: dict[str, Any]) -> str:
    status = reg["status"]
    if status == "registered_enabled":
        bits = []
        for b in reg["bindings"]:
            if "rule_id" in b:
                flag = ({1: "", 0: "[禁用]"}).get(b["enabled"], "[未知]")
                bits.append(f"convert规则「{b.get('rule_name') or b['rule_id']}」{flag}")
            else:
                flag = ({1: "", 0: "[禁用]"}).get(b["enabled"], "[未知]")
                bits.append(f"{b['plugin_type']}:{b.get('form_name') or b.get('form_key')}{flag}")
        return "已注册且启用：" + "；".join(bits)
    if status == "registered_disabled":
        return "已注册但全部有效绑定已禁用"
    if status == "registered_enabled_unknown":
        return "已注册，启用状态未知（NULL）"
    if status == "orphan_unregistered":
        return f"孤儿插件类（{reg.get('kind')}），KB 该类注册表查无绑定（未注册，不排除动态注册）"
    return reg.get("note") or "无注册证据"


def _render_entry_analysis(ea: dict[str, Any] | None) -> list[str]:
    if not ea:
        return []
    conf = f"（{ea['confidence']}）" if ea.get("confidence") else ""
    lines = ["", "【入口可达性】" + _VERDICT_ZH.get(ea["verdict"], ea["verdict"]) + conf]
    for e in ea.get("entries", []):
        ev = ""
        if e.get("events"):
            ev = " 事件=" + "/".join(x["event"] for x in e["events"])
        lines.append(f"  · {e['class']}{ev}: {_render_registration_line(e['registration'])}")
    if ea.get("chains"):
        lines.extend(entry_chain.render_lines(
            {"status": ea.get("chain_status"), "chains": ea["chains"],
             "chains_truncated": ea.get("chains_truncated", 0),
             "search_truncated": ea.get("search_truncated", False)},
            max_chains=_EA_SHOW_CHAINS))
    if ea.get("note"):
        lines.append(f"  {ea['note']}")
    return lines


__all__ = ["callers", "callers_compact", "entry_analysis", "render_callers",
          "resolution_coverage"]
