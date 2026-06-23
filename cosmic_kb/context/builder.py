"""阶段9 · Context Builder（按意图查 KB 取最小证据集，组装成 AI 可答的结构化上下文）。

resolver 把一句话解析成 ResolvedQuery（意图 + 主体）后，本层按意图**复用已有取证函数**
把最小证据集拼成一个"证据包"：实体坐标 / 写入点（类·方法·事件·行号·路径）/ 是否落库 /
置信度 / 排查建议。**不重写取证逻辑**——字段走 `report.field_trace`，单据走
`report.bill_view`，插件/操作走本模块的薄查询（只读 KB 表）。

输出双形态（延续 report 包约定）：`to_dict()` 喂段二 Skill / 后续 MCP；`render_*()` 给人
直接在终端读。每条结论都落到 KB 查询结果或 `源码:行号`，判不出标 unknown，不臆造
（对齐 CLAUDE.md 红线 #1 与 SKILL.md 核心纪律）。

阶段 8 已搁置：业务流上下文只用现成 BOTP 边（`field_trace` 内已挂 convert 上下游），
引用/审核回写无数据则留 unknown，不假设有完整业务流图。
"""

from __future__ import annotations

import json
from typing import Any

from ..report import field_trace as ft_mod
from ..report import bill_view as bv_mod
from ..semantic.resolver import ResolvedQuery

_DISCLAIMER = ("结论均来自 KB 静态扫描（元数据 + Java 静态分析），带 confirmed/likely/unknown 置信度；"
               "解析不到的（平台 kd.bos.* / 外部调用 / 源码未给全）一律标 unknown，未臆造。")


def build_context(conn, rq: ResolvedQuery) -> dict[str, Any]:
    """ResolvedQuery → 证据包 dict。"""
    base: dict[str, Any] = {
        "question": rq.raw,
        "intent": rq.intent,
        "resolved": rq.to_dict(),
        "disclaimer": _DISCLAIMER,
    }

    # 需反问 / 没听懂 → 证据包退化为消歧菜单，不强答。
    if rq.need_clarification or rq.intent in ("ambiguous", "unknown"):
        base["status"] = "need_clarification"
        base["candidates"] = [
            {"kind": c.kind, "score": round(c.score, 1), "label": c.label()}
            for c in rq.candidates
        ]
        base["advice"] = [rq.note or "请补充更精确的字段标识/单据/类名。"]
        return base

    if rq.intent == "field_who_changed":
        return _ctx_field(conn, rq, base)
    if rq.intent == "bill_drilldown":
        return _ctx_bill(conn, rq, base)
    if rq.intent == "plugin_explain":
        return _ctx_plugin(conn, rq, base)
    if rq.intent == "method_calls":
        return _ctx_method(conn, rq, base)
    if rq.intent == "operation_explain":
        return _ctx_operation(conn, rq, base)

    base["status"] = "not_found"
    base["advice"] = ["未识别的意图。"]
    return base


# ── 字段：谁改了它（旗舰）────────────────────────────────────────────────────
def _ctx_field(conn, rq: ResolvedQuery, base: dict[str, Any]) -> dict[str, Any]:
    ft = ft_mod.field_trace(
        conn, rq.field_key, form_key=rq.form_key, entry_key=rq.entry_key, level=rq.level)
    base["status"] = "ok"
    base["evidence"] = ft
    s = ft["summary"]
    advice: list[str] = []
    if not ft["writers"] and not ft["readers"]:
        advice.append("没有任何插件读写该字段——可能字段名有误、只被平台处理、或源码未给全。")
    else:
        advice.append(
            f"共 {s['writers']} 处写入（落库 {s['persisting_writers']}、存疑 "
            f"{s['uncertain_writers']}）、{s['readers']} 处读取，分布在 {s['plugins']} 个插件/类。")
        if s["uncertain_writers"]:
            advice.append(f"有 {s['uncertain_writers']} 处写入落库判定为 unknown（缺保存链路/外部调用），"
                          "排障时优先核对其保存路径。")
        if s.get("possible"):
            advice.append(f"另有 {s['possible']} 处「可能命中（层级/分录存疑）」，见证据包 possible 桶。")
        if s.get("unlocated"):
            advice.append(f"另有 {s['unlocated']} 处来源单据未定位，见 unlocated 桶，供人工核对。")
    if ft.get("note"):
        advice.append(ft["note"])
    base["advice"] = advice
    return base


# ── 单据：钻取 ────────────────────────────────────────────────────────────────
def _ctx_bill(conn, rq: ResolvedQuery, base: dict[str, Any]) -> dict[str, Any]:
    bv = bv_mod.bill_view(conn, rq.form_key)
    if bv is None:
        base["status"] = "not_found"
        base["advice"] = [f"单据不存在: {rq.form_key}"]
        return base
    base["status"] = "ok"
    base["evidence"] = bv
    st = bv["stats"]
    advice = [
        f"单据 {bv['form']['key']}「{bv['form'].get('name') or ''}」：实体 {st['entity_count']}、"
        f"字段 {st['field_count']}、操作 {st['operation_count']}、插件 {st['plugin_count']}、"
        f"有插件触达的字段 {st['touched_fields']}。"]
    star_ops = [o for o in bv["operations"] if o["has_plugin"]]
    if star_ops:
        advice.append("有自定义操作插件的操作（排障优先看）：" +
                      "、".join(f"{o['key']}「{o['name'] or ''}」" for o in star_ops[:8]))
    if bv["risk_bindings"]:
        advice.append(f"⚠ {len(bv['risk_bindings'])} 个 project 插件未命中源码/歧义，排障会卡在这里。")
    base["advice"] = advice
    return base


# ── 插件/类：解释（薄查询，只读 KB 表）────────────────────────────────────────
def _ctx_plugin(conn, rq: ResolvedQuery, base: dict[str, Any]) -> dict[str, Any]:
    fqn = rq.class_fqn
    cls = conn.execute(
        "SELECT fqn,simple,package,relpath,module,is_orphan,orphan_role,plugin_base "
        "FROM source_class WHERE fqn=?", (fqn,)).fetchone()
    # 注册：该类绑定到哪些单据/操作（plugin 表）+ 桥接状态（binding 表）。
    registrations = [dict(r) for r in conn.execute(
        "SELECT form_key,plugin_type,operation_key,operation_name FROM plugin WHERE class_name=?",
        (fqn,)).fetchall()]
    bindings = [dict(r) for r in conn.execute(
        "SELECT form_key,plugin_type,status,source_relpath,confidence,note FROM binding "
        "WHERE class_name=?", (fqn,)).fetchall()]
    # 事件方法（plugin_method 按 plugin_fqn）。
    # DISTINCT：同一类注册到多个操作时 plugin_method 会重复记录同一事件方法，去重免刷屏。
    events = [dict(r) for r in conn.execute(
        "SELECT DISTINCT method_name,event_kind,event_phase,start_line,end_line,source_relpath "
        "FROM plugin_method WHERE plugin_fqn=? ORDER BY start_line", (fqn,)).fetchall()]
    # 字段读写：作为入口插件(plugin_fqn) 或 物理所在类(access_class) 触达的字段。
    accesses = [dict(r) for r in conn.execute(
        "SELECT DISTINCT field_key,form_key,level,entry_key,access,persists,event_method,via,line,"
        "source_relpath,plugin_fqn,access_class FROM field_access "
        "WHERE plugin_fqn=? OR access_class=? ORDER BY access,field_key",
        (fqn, fqn)).fetchall()]
    writers = [a for a in accesses if a["access"] == "write"]
    readers = [a for a in accesses if a["access"] == "read"]

    base["status"] = "ok" if cls or registrations or accesses else "not_found"
    base["evidence"] = {
        "class": dict(cls) if cls else {"fqn": fqn},
        "registrations": registrations,
        "bindings": bindings,
        "events": events,
        "writes": writers,
        "reads": readers,
        "summary": {
            "registrations": len(registrations), "events": len(events),
            "writes": len(writers), "reads": len(readers),
            "persisting": sum(1 for a in writers if a["persists"] == "yes"),
            "fields": len({a["field_key"] for a in accesses if a["field_key"]}),
        },
    }
    advice: list[str] = []
    if cls is None and not registrations:
        advice.append(f"KB 里没有这个类的记录：{fqn}。可能类名/包不对，或源码未纳入。")
    else:
        role = (cls["orphan_role"] if cls else None)
        if registrations:
            advice.append("注册归属：" + "、".join(
                f"{r['form_key']}[{r['plugin_type']}]" +
                (f"←{r['operation_key']}" if r["operation_key"] else "")
                for r in registrations[:8]))
        elif role == "plugin":
            advice.append(f"⚠ 继承苍穹插件基类 {cls['plugin_base']} 却未被任何元数据绑定"
                          "（死代码 / 元数据未给全的信号）。")
        elif role == "constant":
            advice.append("这是常量/标识定义类（不含业务读写逻辑）。")
        else:
            advice.append("未被任何元数据绑定的项目类（service/工具类），由其它插件跨类调用。")
        sm = base["evidence"]["summary"]
        advice.append(f"读写 {sm['fields']} 个字段（写 {sm['writes']}、落库 {sm['persisting']}、"
                      f"读 {sm['reads']}）；事件方法 {sm['events']} 个。")
        if any(b["status"] in ("missing", "ambiguous") for b in bindings):
            advice.append("⚠ 桥接有 missing/ambiguous，源码定位可能不全。")
    base["advice"] = advice
    return base


# ── 方法：出向调用导航（该方法调了项目内哪些方法、各在哪个文件；复用 report.method_calls）──
#   方法在干嘛由段二大模型直接读源码解释，工具只给它读不到/猜不准的下钻坐标。
def _ctx_method(conn, rq: ResolvedQuery, base: dict[str, Any]) -> dict[str, Any]:
    from ..report import method_calls as mc_mod

    rd = mc_mod.method_calls(conn, rq.class_fqn, rq.method_name)
    base["evidence"] = rd
    if not rd.get("found"):
        cands = rd.get("candidates") or []
        if cands:                                  # 类/方法歧义 → 候选菜单反问
            base["status"] = "need_clarification"
            base["candidates"] = [
                {"kind": "method", "score": 100.0,
                 "label": (c["fqn"] if isinstance(c, dict) else f"{rd.get('class_fqn')}#{c}")}
                for c in cands
            ]
        else:
            base["status"] = "not_found"
        base["advice"] = [rd.get("note") or "未找到该方法。"]
        return base

    base["status"] = "ok"
    advice: list[str] = [
        f"方法源码请直接读 {rd['relpath']}（行号见下），本工具只给确定性的项目内调用坐标。"]
    for m in rd["methods"]:
        n = m["summary"]["project_calls"]
        loc = f"行 {m['start_line']}–{m['end_line']}" if m["start_line"] else "（行号未知）"
        advice.append(f"方法 {rd['class_simple']}.{m['method_name']} {loc}：解析出 {n} 处项目内调用"
                      + ("（可逐层下钻）。" if n else "（无或接收者类型解不出，未臆造）。"))
    if rd.get("note"):
        advice.append(rd["note"])
    if any(m["calls"] for m in rd["methods"]):
        advice.append("逐层下钻：对每条调用的 target_fqn 再 method_calls，可顺着调用链往下读源码。")
    base["advice"] = advice
    return base


# ── 操作：解释（该单据下某操作绑定的插件 + 字段触达）──────────────────────────
def _ctx_operation(conn, rq: ResolvedQuery, base: dict[str, Any]) -> dict[str, Any]:
    form_key, op_key = rq.form_key, rq.operation_key
    op = conn.execute(
        "SELECT form_key,key,name,operation_type,resolved_from,has_plugin FROM operation "
        "WHERE form_key=? AND key=?", (form_key, op_key)).fetchone()
    plugins = [dict(r) for r in conn.execute(
        "SELECT class_name,plugin_type,operation_name FROM plugin "
        "WHERE form_key=? AND operation_key=?", (form_key, op_key)).fetchall()]
    fqns = {p["class_name"] for p in plugins}
    touched: list[dict[str, Any]] = []
    if fqns:
        ph = ",".join("?" * len(fqns))
        touched = [dict(r) for r in conn.execute(
            f"SELECT field_key,level,entry_key,access,persists,plugin_fqn,access_class,"
            f"event_method,line,source_relpath FROM field_access "
            f"WHERE form_key=? AND plugin_fqn IN ({ph}) ORDER BY access,field_key",
            (form_key, *sorted(fqns))).fetchall()]
    writers = [t for t in touched if t["access"] == "write"]
    base["status"] = "ok" if op or plugins else "not_found"
    base["evidence"] = {
        "operation": dict(op) if op else {"form_key": form_key, "key": op_key},
        "plugins": plugins,
        "field_access": touched,
        "summary": {
            "plugins": len(plugins), "writes": len(writers),
            "persisting": sum(1 for t in writers if t["persists"] == "yes"),
            "fields": len({t["field_key"] for t in touched if t["field_key"]}),
        },
    }
    advice: list[str] = []
    if op is None and not plugins:
        advice.append(f"单据 {form_key} 下未找到操作 {op_key}（或它没有自定义插件）。")
    else:
        otype = op["operation_type"] if op else "?"
        advice.append(f"操作 {op_key}「{(op['name'] if op else '') or ''}」类型 [{otype}]，"
                      f"绑定 {len(plugins)} 个插件。")
        sm = base["evidence"]["summary"]
        advice.append(f"这些插件在本操作上写 {sm['writes']} 处字段（落库 {sm['persisting']}）、"
                      f"触达 {sm['fields']} 个字段。")
    base["advice"] = advice
    return base


# ── 渲染（终端文本）──────────────────────────────────────────────────────────
def render_context(ctx: dict[str, Any], *, max_list: int = 50) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"问: {ctx['question']}")
    r = ctx.get("resolved", {})
    lines.append(f"意图: {ctx['intent']}  置信度: {r.get('confidence')}"
                 + (f"  «{r.get('note')}»" if r.get("note") else ""))
    lines.append("=" * 72)

    if ctx.get("status") == "need_clarification":
        lines.append("⚠ 需要消歧——下面是相近候选，请挑一个精确标识再问：")
        for i, c in enumerate(ctx.get("candidates", [])[:max_list], 1):
            lines.append(f"  {i:>2}. [{c['kind']}] {c['label']}  ({c['score']})")
        for a in ctx.get("advice", []):
            lines.append(f"  · {a}")
        return "\n".join(lines)

    # 旗舰/单据复用现成 render，避免重复造轮子。
    ev = ctx.get("evidence")
    if ctx["intent"] == "field_who_changed" and ev is not None:
        lines.append(ft_mod.render_field_trace(ev, max_list=max_list))
    elif ctx["intent"] == "bill_drilldown" and ev is not None:
        lines.append(bv_mod.render_bill(ev, max_list=max_list))
    elif ctx["intent"] == "plugin_explain" and ev is not None:
        lines.extend(_render_plugin(ev, max_list))
    elif ctx["intent"] == "method_calls" and ev is not None:
        from ..report import method_calls as mc_mod
        lines.append(mc_mod.render_method_calls(ev, max_list=max_list))
    elif ctx["intent"] == "operation_explain" and ev is not None:
        lines.extend(_render_operation(ev, max_list))

    if ctx.get("advice"):
        lines.append("")
        lines.append("【排查建议】")
        for a in ctx["advice"]:
            lines.append(f"  · {a}")
    lines.append("")
    lines.append(f"（{ctx['disclaimer']}）")
    return "\n".join(lines)


def _render_plugin(ev: dict[str, Any], max_list: int) -> list[str]:
    c = ev["class"]
    out = [f"类: {c.get('fqn')}"]
    if c.get("relpath"):
        out.append(f"  源码: {c['relpath']}  模块: {c.get('module') or '?'}  角色: {c.get('orphan_role') or '已绑定'}")
    if ev["registrations"]:
        out.append("【注册归属】")
        for rg in ev["registrations"][:max_list]:
            op = f" ←{rg['operation_key']}" if rg.get("operation_key") else ""
            out.append(f"  {rg['form_key']} [{rg['plugin_type']}]{op}")
    if ev["events"]:
        out.append("【事件方法】")
        for e in ev["events"][:max_list]:
            out.append(f"  {e['method_name']} [{e['event_kind']}/{e['event_phase']}] "
                       f"{e['source_relpath']}:{e['start_line']}")
    if ev["writes"]:
        out.append(f"【写入字段】（前 {min(max_list, len(ev['writes']))}）")
        for w in ev["writes"][:max_list]:
            pf = {"yes": "✅落库", "no": "—内存", "unknown": "❓存疑"}.get(w["persists"], "")
            out.append(f"  {w['field_key']:<26} {pf}  事件 {w['event_method']}  "
                       f"{w['source_relpath']}:{w['line']}")
    return out


def _render_operation(ev: dict[str, Any], max_list: int) -> list[str]:
    op = ev["operation"]
    out = [f"操作: {op.get('key')}「{op.get('name') or ''}」 [{op.get('operation_type') or '?'}] "
           f"单据 {op.get('form_key')}"]
    if ev["plugins"]:
        out.append("【绑定插件】")
        for p in ev["plugins"][:max_list]:
            out.append(f"  [{p['plugin_type']}] {p['class_name']}")
    if ev["field_access"]:
        out.append(f"【字段触达】（前 {min(max_list, len(ev['field_access']))}）")
        for a in ev["field_access"][:max_list]:
            pf = {"yes": "✅落库", "no": "—内存", "unknown": "❓存疑", "na": ""}.get(a["persists"], "")
            out.append(f"  {a['access']:<5} {a['field_key']:<26} {pf}  "
                       f"{a['source_relpath']}:{a['line']}")
    return out


def to_json(ctx: dict[str, Any]) -> str:
    return json.dumps(ctx, ensure_ascii=False, indent=2)
