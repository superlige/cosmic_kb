"""信任优先 · 全局「动态/未定位写入」审计清单（红线 #4 完整性安全网）。

`trace` 按**解析出的字段 key 字面值**查 `field_access`，故 `field_key=None` 的访问（字段 key
钉不出）对按字段查询**完全隐形**。真实苍穹老项目里这类访问不少，且绝大多数**不是工具漏跟了常量**，
而是代码本身对「运行时/配置/元数据决定的动态字段集」做泛化读写——静态在原理上钉不出唯一字段。

本报告把这些 null-key 访问**按成因桶**全量列出（每条带源码锚点 + 所属方法），作为：
  1. 接手者的**完整性体检**：trace 查全到底漏在哪、漏多少、什么形态；
  2. 段二大模型的**工作单**：顺锚点（`calls`/直接读源文件）逐处读源码，定性"这个泛化写入实际碰哪些
     字段、是否含我关心的那个"——确定性层不解释、不展开、不臆造（红线 #6 两段式分工）。

成因六态（与 `java/field_access.py` 的 key_resolution 取值一致）：
  · dynamic-loop  —— key 是迭代变量，循环写一个运行时/配置决定的字段集（`for(f:coll) set(f,..)`）。
  · concat        —— key 由字符串拼接而成（`CON.X + "_" + v`）。
  · external-const—— key 是未命中常量表的 `UPPER_CONST`/`类.常量`（跨模块/外部常量）。
  · unknown       —— 其余（多为小写局部变量持 key），未强分。
  · ambiguous     —— 跨类同名常量不同值，钉不出是哪个字段（重复常量类症状）。
  · dynamic       —— key 是表达式/方法返回值，无标识符可查。

延续 report 包约定：dict 在前（供 --json / Web / MCP），`render_*` 文本在后。
"""

from __future__ import annotations

from typing import Any

# 成因桶顺序与人读标签（dynamic-loop/concat/external-const 是"动态写入候选"主力，trace 也用这三类）。
_CAUSES = ["dynamic-loop", "concat", "external-const", "unknown", "ambiguous", "dynamic"]
_LABEL = {
    "dynamic-loop": "动态循环（遍历运行时/配置字段集）",
    "concat": "拼接键（运行时拼接字段标识）",
    "external-const": "外部/跨模块常量（不在扫描范围）",
    "unknown": "未识别（多为局部变量持 key）",
    "ambiguous": "歧义常量（跨类同名不同值，钉不出是哪个字段）",
    "dynamic": "动态表达式（key 为表达式/方法返回）",
}
# 成因短标签（worklist 里多成因并列时用）。
_CAUSE_SHORT = {
    "dynamic-loop": "动态循环", "concat": "拼接键", "external-const": "外部常量",
    "unknown": "未识别", "ambiguous": "歧义常量", "dynamic": "动态表达式",
}


def build_method_worklist(rows: list[dict[str, Any]], *, cap: int = 10) -> dict[str, Any]:
    """把 null-key 访问行按 (入口类, 事件方法) **去重成「该读方法」清单**，是控制大模型上下文的关键：

    同一方法写 N 个钉不出 key 的字段，大模型只需读这方法一次。故不回逐行，回去重后的方法清单——
    每条给：入口类全限定名 + 事件方法 + 多少处动态访问 + 成因 + 写入物理位置（`writes_in`，跨类
    时写在哪个 helper 的哪一行，供大模型直奔读源码）。按动态访问数降序，超 cap 截断并报剩余数。
    """
    groups: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        key = (r.get("plugin_fqn"), r.get("event_method"))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "class_fqn": r.get("plugin_fqn"), "method": r.get("event_method"),
                "count": 0, "writes": 0, "causes": set(), "writes_in": {},
            }
        g["count"] += 1
        if r.get("access") == "write":
            g["writes"] += 1
        g["causes"].add(r.get("key_resolution"))
        ac = r.get("access_class") or r.get("plugin_fqn")
        g["writes_in"].setdefault(ac, f"{r.get('source_relpath')}:{r.get('line')}")
    out: list[dict[str, Any]] = []
    for g in groups.values():
        causes = sorted(g["causes"], key=lambda c: _CAUSES.index(c) if c in _CAUSES else 99)
        out.append({
            "class_fqn": g["class_fqn"], "method": g["method"],
            "count": g["count"], "writes": g["writes"],
            "causes": causes,
            "cause_label": "/".join(_CAUSE_SHORT.get(c, c) for c in causes),
            "writes_in": [{"class": (ac or "").rsplit(".", 1)[-1], "anchor": an}
                          for ac, an in list(g["writes_in"].items())[:3]],
        })
    out.sort(key=lambda d: (-d["count"], d["class_fqn"] or ""))
    return {"total_methods": len(out), "methods": out[:cap], "capped": max(0, len(out) - cap)}


def summarize(conn, *, form_key: str | None = None, cause: str | None = None,
              class_fqn: str | None = None, max_methods: int = 10) -> dict[str, Any]:
    """组装全局 null-key 访问审计 dict（摘要 + **按方法去重的清单**，可选过滤）。

    为防上下文爆炸，**默认不回逐行**——按成因桶给计数，每桶给去重后的「该读方法」清单（cap
    `max_methods` 条）。`form_key`/`cause`/`class_fqn` 过滤让大模型有线索时只拉一个切片。
    """
    import json

    java = json.loads(_meta(conn, "java_analysis") or "{}")

    where = ["field_key IS NULL"]
    params: list[str] = []
    if form_key:
        where.append("form_key=?"); params.append(form_key)
    if cause:
        where.append("key_resolution=?"); params.append(cause)
    if class_fqn:        # 入口插件类 或 实际所在类 命中皆可
        where.append("(plugin_fqn=? OR access_class=?)"); params.extend([class_fqn, class_fqn])
    rows = [dict(r) for r in conn.execute(
        "SELECT form_key,level,entry_key,plugin_fqn,access_class,event_method,access,via,line,"
        "key_resolution,source_relpath,evidence FROM field_access WHERE " + " AND ".join(where) +
        " ORDER BY key_resolution, form_key, plugin_fqn, source_relpath, line", params
    ).fetchall()]

    by_cause: dict[str, dict[str, Any]] = {}
    for c in _CAUSES:
        crows = [r for r in rows if r["key_resolution"] == c]
        if not crows:
            continue
        wl = build_method_worklist(crows, cap=max_methods)
        by_cause[c] = {
            "label": _LABEL.get(c, c),
            "total": len(crows),
            "writes": sum(1 for r in crows if r["access"] == "write"),
            "reads": sum(1 for r in crows if r["access"] == "read"),
            "plugins": len({r["plugin_fqn"] for r in crows}),
            "forms": len({r["form_key"] for r in crows if r["form_key"]}),
            "unlocated": sum(1 for r in crows if not r["form_key"]),
            "total_methods": wl["total_methods"],
            "methods": wl["methods"],
            "capped": wl["capped"],
        }

    writes = sum(1 for r in rows if r["access"] == "write")
    return {
        "java_available": java.get("available", True),
        "filter": {"form_key": form_key, "cause": cause, "class_fqn": class_fqn},
        "total": len(rows),
        "writes": writes,
        "reads": len(rows) - writes,
        "forms_located": len({r["form_key"] for r in rows if r["form_key"]}),
        "unlocated": sum(1 for r in rows if not r["form_key"]),
        "by_cause": by_cause,
    }


def _meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kb_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


# ── 人读文本（CLI）──────────────────────────────────────────────────────────

def render_dynamic_writes(d: dict[str, Any], *, max_list: int = 20) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("信任优先 · 全局「动态/未定位写入」审计（字段 key 钉不出 → trace 按字段查不到）")
    lines.append("=" * 72)
    if not d["java_available"]:
        lines.append("⚠ tree-sitter 未启用（pip install -e .[parse]），字段级分析为空。")
        return "\n".join(lines)
    if not d["total"]:
        lines.append("✅ 未发现 field_key 钉不出的读写——所有字段访问都解析到了具体字段标识。")
        return "\n".join(lines)

    flt = d.get("filter") or {}
    active = "、".join(f"{k}={v}" for k, v in flt.items() if v)
    lines.append(
        f"共 {d['total']} 处（写 {d['writes']} / 读 {d['reads']}）字段 key 钉不出；"
        f"其中来源单据已定位 {d['forms_located']} 类、未定位 {d['unlocated']} 处。"
        + (f"  «过滤: {active}»" if active else "")
    )
    lines.append("注：已按「入口类·方法」去重成『该读方法』清单——大模型按方法读源码一次即可，")
    lines.append("    判定该方法实际碰哪些字段、是否含你关心的那个（不臆造）。需逐行/切片用 --json 或 --form/--cause/--class 过滤。")

    for c, b in d["by_cause"].items():
        lines.append("")
        lines.append("─" * 72)
        lines.append(
            f"▼ {b['label']}（{c}）：{b['total']} 处（写 {b['writes']} / 读 {b['reads']}）"
            f" → {b['total_methods']} 个方法 · 插件 {b['plugins']} · 单据 {b['forms']}"
            + (f" · 未定位单据 {b['unlocated']}" if b["unlocated"] else "")
        )
        for m in b["methods"][:max_list]:
            cls = (m["class_fqn"] or "?").rsplit(".", 1)[-1]
            lines.append(f"  · {cls}.{m['method']}  ({m['count']} 处/{m['cause_label']})")
            for w in m["writes_in"]:
                lines.append(f"        写入位于 {w['class']}  {w['anchor']}")
        if b["capped"]:
            lines.append(f"    …另有 {b['capped']} 个方法未列出（用 --cause {c} 或 --class 过滤、或 --json）")
    return "\n".join(lines)
