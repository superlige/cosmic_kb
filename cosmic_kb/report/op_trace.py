"""隐藏坑 #1 · 操作坐标追踪（`trace --kind operation`：输入 单据.操作 → 程序化触发链取证）。

设计（2026-07-15 与用户拍板）：程序化触发点的**入站明细**不摊进 bill / 字段 trace 的返回
（对不查触发链的调用是纯冗余），改为按需查询——操作坐标就是天然的查询粒度。返回三段：
  * `triggered_by`        谁的代码触发了本操作（executeOperate/invokeOperation 调用点，附
                          caller_forms=调用方插件绑定的上游单据，递归 trace 上游即拼跨单据链）；
  * `unresolved_inbound`  **无法静态排除是本操作**的嫌疑触发点（红线 #4 全量摆出，每条带
                          `suspect_reason` 成因）：目标已钉本单据但操作 key 解不出/不在操作集
                          （op_unresolved）、操作 key 正是本操作但目标单据解不出
                          （target_unresolved——表单插件绑多张单/目标动态拼接的外发即此形态，
                          2026-07-15 二次整合后并入操作坐标，查"谁调了本操作"一次即完整）、
                          两者都解不出（both_unresolved，可能是任何操作的调用，最弱嫌疑）；
  * `triggers_downstream` 本操作的插件代码又对外触发了谁——级联链（A→B→C）下行就是对每条的
                          next_trace 坐标反复 trace，一跳一查（KB 只存单跳事实，不预拼多跳链）。

坐标判别**纯显式**：调用方必须传 kind="operation" 才走本模块，不做字段/操作自动猜测
（同一 key 理论上可既是字段又是操作，猜错比多传一个参数贵得多）。
bill 侧只保留最小发现性信号（operations[].programmatic_trigger_count + stats 计数），
明细全在本模块；bill.outbound_triggers 保留的是单据级**影响面**视图（本单据插件打出去了哪些），
其中无法排除的切片（target=NULL）已并入本模块 unresolved_inbound，不再是查触发链的必查补充。

延续 report 包约定：dict 在前（`operation_trace` 供 CLI --json / render），紧凑投影
`operation_trace_compact` 供 MCP（cap + 字节 governor + 游标分页，翻页协议与 trace/bill 同套）。
"""

from __future__ import annotations

from typing import Any

# 复用 trace 的「host 口径字节度量 + 游标解析 + 预算 + 翻页门」单一事实源（红线 #6）。
from .field_trace import (_COMPACT_BUDGET, _parse_cursor, _pending_from_flat_cursors,
                          _wire_len, pagination_gate)

# operation_trigger 取数列（rich 全带；slim 投影只留渲染所需，confidence 由 resolution 档位自明）。
_TRIG_COLS = ("caller_class,caller_method,line,source_relpath,via,op_key,op_key_resolution,"
              "op_key_confidence,target_form_key,target_resolution,target_confidence")

# 入站嫌疑成因：排序强弱（op/target 单边解不出都是强嫌疑，双边解不出最弱）与人读标签。
_SUSPECT_RANK = {"op_unresolved": 0, "target_unresolved": 1, "both_unresolved": 2}
_SUSPECT_LABEL = {
    "op_unresolved": "目标=本单据、操作key解不出/不在操作集",
    "target_unresolved": "操作key匹配、目标单据解不出（表单插件外发常见形态）",
    "both_unresolved": "操作与目标都解不出",
}


def operation_trace(conn, locator: str, *, form_key: str | None = None) -> dict[str, Any]:
    """操作坐标追踪（**富投影**：CLI --json / render 用）。

    `locator` 写法：`"单据.操作key"`（推荐）或裸 `"操作key"`（唯一命中自动定位，跨单据反问）；
    显式 `form_key` 参数覆盖坐标首段。错误/消歧返回与 trace 字段坐标同口径：
    `{"error": ...}` / `{"status": "need_clarification", ...}`。
    """
    parts = [p for p in locator.strip().split(".") if p]
    if not parts:
        return {"error": "空的操作坐标。写法：\"单据.操作key\"（如 cqkd_ht.audit）。"}
    if len(parts) > 2:
        return {"error": f"操作坐标只有「单据.操作」两段（收到 {len(parts)} 段：{locator}）。"
                         "分录/字段层级写法属于字段坐标（kind=\"field\"）。"}
    if len(parts) == 2:
        form_key = form_key or parts[0]
        op_key = parts[1]
    else:
        op_key = parts[0]

    # 裸操作 key：按元数据操作集 ∪ 触发点目标联合消歧（触发点可能指向元数据未随包的操作）。
    if form_key is None:
        cand = {r[0] for r in conn.execute(
            "SELECT DISTINCT form_key FROM operation WHERE key=?", (op_key,))}
        cand |= {r[0] for r in conn.execute(
            "SELECT DISTINCT target_form_key FROM operation_trigger "
            "WHERE op_key=? AND target_form_key IS NOT NULL", (op_key,))}
        if not cand:
            return {"error": f"没有任何单据定义操作「{op_key}」，也没有指向它的程序化触发点。"}
        if len(cand) > 1:
            forms = sorted(cand)
            return {"status": "need_clarification", "op_key": op_key, "candidate_forms": forms,
                    "note": f"操作「{op_key}」在 {len(cand)} 个单据上都存在"
                            f"（{'、'.join(forms)}），请指定单据后再查，如 \"{forms[0]}.{op_key}\"。"}
        form_key = next(iter(cand))

    form = conn.execute(
        "SELECT key,name,is_extension,extends FROM form WHERE key=?", (form_key,)).fetchone()
    if form is None:
        return {"error": f"单据不存在: {form_key}"}

    notes: list[str] = []
    if form["is_extension"] and form["extends"]:
        notes.append(f"⚑ {form_key} 是扩展别名，内容已并入原厂单据 {form['extends']}，"
                     f"请改查 \"{form['extends']}.{op_key}\"")

    op = conn.execute(
        "SELECT key,name,operation_type,resolved_from,has_operation_plugin FROM operation "
        "WHERE form_key=? AND key=?", (form_key, op_key)).fetchone()

    # 入站：谁的代码触发了本 单据.操作（executeOperate arg0/arg1 或 invokeOperation+绑定推断）。
    inbound = [dict(r) for r in conn.execute(
        f"SELECT {_TRIG_COLS} FROM operation_trigger WHERE target_form_key=? AND op_key=? "
        f"ORDER BY caller_class,line", (form_key, op_key)).fetchall()]

    # 入站嫌疑：**无法静态排除是本操作**的触发点全量摆出（红线 #4），suspect_reason 注明成因——
    #   op_unresolved     目标已钉本单据、操作 key 解不出（dynamic/unknown）或不在元数据操作集；
    #   target_unresolved 操作 key 正是所查操作、目标单据解不出——表单插件绑多张单/目标动态拼接
    #                     的外发即此形态（bill.outbound_triggers 里 target=NULL 的切片），
    #                     2026-07-15 二次整合：并入操作坐标后查"谁调了本操作"不再需要补查 bill；
    #   both_unresolved   操作 key 与目标都解不出——可能是任何操作的调用，最弱嫌疑排最后。
    # 只有「操作 key 解出且≠所查操作」或「目标钉到别的单据」才允许静态排除，不进嫌疑。
    op_set = {r[0] for r in conn.execute(
        "SELECT key FROM operation WHERE form_key=?", (form_key,))}
    unresolved: list[dict[str, Any]] = []
    for r in conn.execute(
            f"SELECT {_TRIG_COLS} FROM operation_trigger WHERE target_form_key=? OR "
            f"(target_form_key IS NULL AND (op_key=? OR op_key IS NULL)) "
            f"ORDER BY caller_class,line", (form_key, op_key)).fetchall():
        t = dict(r)
        if t["target_form_key"] == form_key:
            # 已钉本单据：所查操作本身已完整列在 triggered_by；解出且在操作集的是本单据
            # **别的**操作的入站（查该操作坐标即可见）——两者都不算本操作的嫌疑。
            if t["op_key"] == op_key or (t["op_key"] is not None and t["op_key"] in op_set):
                continue
            t["suspect_reason"] = "op_unresolved"
        else:
            t["suspect_reason"] = ("target_unresolved" if t["op_key"] == op_key
                                   else "both_unresolved")
        unresolved.append(t)
    unresolved.sort(key=lambda t: (_SUSPECT_RANK[t["suspect_reason"]],
                                   t["caller_class"], t["line"]))

    if op is None and not inbound and not any(
            t["suspect_reason"] == "target_unresolved" for t in unresolved):
        ops = [r[0] for r in conn.execute(
            "SELECT key FROM operation WHERE form_key=? ORDER BY key", (form_key,))]
        return {"error": f"单据 {form_key} 的元数据操作集里没有操作「{op_key}」，"
                         "也没有指向它（或操作 key 与之匹配）的程序化触发点。",
                "available_operations": ops[:40]}
    if op is None:
        notes.append(f"⚠ 操作「{op_key}」不在 {form_key} 的元数据操作集（可能是平台默认操作或"
                     "元数据未随包），以下为指向它的程序化触发点证据，操作本体信息不可考。")

    _attach_caller_forms(conn, inbound + unresolved)

    # 本操作绑定的操作插件（触发链的"执行体"；下游外发从这些类的调用点查）。
    plugins = [dict(r) for r in conn.execute(
        "SELECT class_name,source,enabled FROM plugin WHERE form_key=? AND plugin_type='op' "
        "AND operation_key=? ORDER BY class_name", (form_key, op_key)).fetchall()]

    # 下行：本操作插件的代码又对外触发了谁（级联链就是对 next_trace 坐标继续 trace）。
    # 自单据目标排除——那是本单据别的操作的入站，查该操作坐标即可见，不重复。
    downstream: list[dict[str, Any]] = []
    own = [p["class_name"] for p in plugins]
    if own:
        ph = ",".join("?" * len(own))
        downstream = [dict(r) for r in conn.execute(
            f"SELECT {_TRIG_COLS} FROM operation_trigger WHERE caller_class IN ({ph}) "
            f"AND (target_form_key IS NULL OR target_form_key<>?) ORDER BY caller_class,line",
            (*own, form_key)).fetchall()]
        tgt_keys = sorted({t["target_form_key"] for t in downstream if t["target_form_key"]})
        tgt_names: dict[str, str] = {}
        if tgt_keys:
            tq = ",".join("?" * len(tgt_keys))
            tgt_names = {r[0]: r[1] for r in conn.execute(
                f"SELECT key,name FROM form WHERE key IN ({tq})", tgt_keys).fetchall()}
        for t in downstream:
            t["target_form_name"] = tgt_names.get(t["target_form_key"])
            # 递归导航坐标：目标单据与操作 key 都钉出时给下一跳 trace 坐标；钉不出留 None 不臆造。
            t["next_trace"] = (f"{t['target_form_key']}.{t['op_key']}"
                               if t["target_form_key"] and t["op_key"] else None)

    summary = {"triggered_by": len(inbound), "unresolved_inbound": len(unresolved),
               "triggers_downstream": len(downstream), "plugins": len(plugins)}

    if inbound:
        notes.append("triggered_by=谁的代码程序化触发了本操作（caller_forms=调用方插件绑定的"
                     "上游单据；对上游继续 trace(kind=\"operation\") 可递归拼出跨单据触发链）。")
    else:
        notes.append("未发现明确指向本操作的程序化触发点（不排除操作 key/目标单据动态拼接"
                     "解不出的情况，见 unresolved_inbound）。")
    if unresolved:
        cnt: dict[str, int] = {}
        for t in unresolved:
            cnt[t["suspect_reason"]] = cnt.get(t["suspect_reason"], 0) + 1
        detail = "；".join(f"{k}×{n}（{_SUSPECT_LABEL[k]}）" for k, n in
                          sorted(cnt.items(), key=lambda kv: _SUSPECT_RANK[kv[0]]))
        notes.append(f"unresolved_inbound：{len(unresolved)} 条**无法静态排除是本操作**的嫌疑"
                     f"触发点：{detail}——triggered_by+此段合起来才是对本操作调用的完整答案，"
                     "排\"没人手动执行操作怎么执行了\"时必查，读源码定性，勿忽略。")
    if downstream:
        notes.append("triggers_downstream=本操作插件又对外触发的操作（级联链下行）：对每条的 "
                     "next_trace 坐标继续 trace(kind=\"operation\") 即逐跳拼链。")

    return {
        "kind": "operation",
        "form_key": form_key, "form_name": form["name"],
        "operation": dict(op) if op else {"key": op_key, "in_metadata": False},
        "plugins": plugins,
        "triggered_by": inbound,
        "unresolved_inbound": unresolved,
        "triggers_downstream": downstream,
        "summary": summary,
        "note": " ".join(notes) or None,
    }


def _attach_caller_forms(conn, triggers: list[dict[str, Any]]) -> None:
    """给触发点补 caller_forms=调用方插件绑定的单据（上游是哪张单，省 agent 一次反查）。"""
    forms: dict[str, list[str]] = {}
    for cls in {t["caller_class"] for t in triggers}:
        forms[cls] = sorted({r[0] for r in conn.execute(
            "SELECT DISTINCT form_key FROM plugin WHERE class_name=? AND form_key IS NOT NULL",
            (cls,)).fetchall()})
    for t in triggers:
        t["caller_forms"] = forms.get(t["caller_class"], [])


# ── 紧凑投影（MCP 入口）：cap + 字节 governor + 游标分页 ──────────────────────────────
_OP_PAGE_SECTIONS = ("triggered_by", "unresolved_inbound", "triggers_downstream")
# cap 阶梯（触发点通常个位数，首档已宽裕；极端库逐档收紧，被截段带 *_next_cursor 可翻页取全）。
_OP_LADDER = [(20, 10, 10), (10, 6, 6), (5, 3, 3), (2, 1, 1)]


def _slim_in(t: dict[str, Any]) -> dict[str, Any]:
    """入站触发点紧凑形状：调用坐标 + 解析档位 + 上游单据（op_key=所查操作，自明不重复）。"""
    return {k: t.get(k) for k in
            ("caller_class", "caller_method", "line", "source_relpath", "via",
             "op_key_resolution", "caller_forms")}


def _slim_unres(t: dict[str, Any]) -> dict[str, Any]:
    """未归位触发点：suspect_reason 打头说明为何排除不掉，op/target 两侧解析档位都带
    （读源码定性所需最小上下文），诚实说明为何没挂上。"""
    return {k: t.get(k) for k in
            ("suspect_reason", "caller_class", "caller_method", "line", "source_relpath", "via",
             "op_key", "op_key_resolution", "target_form_key", "target_resolution",
             "caller_forms")}


def _slim_down(t: dict[str, Any]) -> dict[str, Any]:
    """下行触发点：目标坐标 +（已核对）目标中文名 + next_trace 递归导航；目标 NULL=解不出即风险。"""
    return {k: t.get(k) for k in
            ("caller_class", "caller_method", "line", "source_relpath", "via", "op_key",
             "target_form_key", "target_form_name", "target_resolution", "next_trace")}


_SLIM_BY_SECTION = {"triggered_by": _slim_in, "unresolved_inbound": _slim_unres,
                    "triggers_downstream": _slim_down}


def _cap_flag(res: dict[str, Any], sec: str, total: int, cap: int) -> bool:
    """列表段被 cap 时记 `<sec>_capped` + `<sec>_next_cursor`（与 bill 同一套扁平游标命名）。"""
    if total > cap:
        res[f"{sec}_capped"] = total - cap
        res[f"{sec}_next_cursor"] = f"{sec}@{len(res[sec])}"
        return True
    return False


def _build_op_compact(ot: dict[str, Any], cap_in: int, cap_unres: int, cap_down: int
                      ) -> dict[str, Any]:
    """一档 cap 下构建紧凑操作追踪 dict（governor 按字节预算逐档收紧调用）。"""
    res: dict[str, Any] = {
        "kind": "operation",
        "form_key": ot["form_key"], "form_name": ot["form_name"],
        "operation": ot["operation"],
        "plugins": ot["plugins"],
        "summary": ot["summary"],
        "triggered_by": [_slim_in(t) for t in ot["triggered_by"][:cap_in]],
    }
    capped = _cap_flag(res, "triggered_by", len(ot["triggered_by"]), cap_in)
    if ot["unresolved_inbound"]:   # 非空才带出，不给无嫌疑的查询垫空数组
        res["unresolved_inbound"] = [_slim_unres(t) for t in ot["unresolved_inbound"][:cap_unres]]
        capped |= _cap_flag(res, "unresolved_inbound", len(ot["unresolved_inbound"]), cap_unres)
    if ot["triggers_downstream"]:
        res["triggers_downstream"] = [_slim_down(t) for t in ot["triggers_downstream"][:cap_down]]
        capped |= _cap_flag(res, "triggers_downstream", len(ot["triggers_downstream"]), cap_down)
    note = ot["note"] or ""
    if capped:
        note += (" 被 cap 的段带 *_next_cursor，用 trace(同坐标, kind=\"operation\", "
                 "cursor=该值) 翻页可取回全部被截条目（不丢数）。")
    res["note"] = note or None
    pending = _pending_from_flat_cursors(res)
    reordered = {"pagination": pagination_gate(pending), **res}
    res.clear()
    res.update(reordered)
    return res


def _page_op_section(ot: dict[str, Any], section: str, offset: int, budget: int
                     ) -> dict[str, Any]:
    """聚焦分页：只回某段从 offset 起、预算内能装下的下一页 items + next_cursor。"""
    base = {"kind": "operation", "form_key": ot["form_key"],
            "operation_key": ot["operation"].get("key")}
    slim = _SLIM_BY_SECTION.get(section)
    if slim is None:
        return {**base, "page": {"section": section,
                "error": f"未知或不可分页的 section: {section}"
                         f"（可分页：{', '.join(_OP_PAGE_SECTIONS)}）"}}
    items = [slim(t) for t in ot[section]]
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


def operation_trace_compact(
    conn, locator: str, *, form_key: str | None = None,
    cursor: str | None = None, budget: int = _COMPACT_BUDGET,
) -> dict[str, Any]:
    """**紧凑投影**（MCP 入口，防 host 32KB 截断）：cap + 字节 governor + 游标分页。

    错误/消歧返回与富投影同口径直接透传；`cursor`（形如 `"triggered_by@20"`）翻页取回被截段。
    """
    ot = operation_trace(conn, locator, form_key=form_key)
    if "error" in ot or ot.get("status") == "need_clarification":
        return ot
    if cursor:
        section, offset = _parse_cursor(cursor)
        return _page_op_section(ot, section, offset, budget)
    res: dict[str, Any] = {}
    for caps in _OP_LADDER:
        res = _build_op_compact(ot, *caps)
        if _wire_len(res) <= budget:
            return res
    return res


def render_operation_trace(ot: dict[str, Any]) -> str:
    """CLI 人读文本（与 dict 同数据，延续 report 包 dict 在前 / render 在后约定）。"""
    if ot.get("error"):
        lines = [f"错误: {ot['error']}"]
        if ot.get("available_operations"):
            lines.append("该单据元数据操作集: " + "、".join(ot["available_operations"]))
        return "\n".join(lines)
    if ot.get("status") == "need_clarification":
        return f"⚠ {ot.get('note')}"

    op = ot["operation"]
    lines: list[str] = []
    lines.append("=" * 72)
    nm = f"「{op.get('name')}」" if op.get("name") else ""
    typ = f"  [{op.get('operation_type')}]" if op.get("operation_type") else ""
    lines.append(f"操作触发链追踪: {ot['form_key']}.{op.get('key')}{nm}{typ}"
                 f"  ({ot['form_name'] or '?'})")
    lines.append("=" * 72)
    s = ot["summary"]
    lines.append(f"  入站触发 {s['triggered_by']}  未归位嫌疑 {s['unresolved_inbound']}  "
                 f"下行外发 {s['triggers_downstream']}  操作插件 {s['plugins']}")
    if ot.get("note"):
        lines.append(f"  {ot['note']}")

    if ot["triggered_by"]:
        lines.append("")
        lines.append("【谁触发了本操作】（executeOperate/invokeOperation 调用点，设计器不展示）")
        for t in ot["triggered_by"]:
            src = f"（上游单据: {', '.join(t['caller_forms'])}）" if t.get("caller_forms") else ""
            res_flag = "" if t["op_key_resolution"] in ("literal", "constant") \
                else f" [{t['op_key_resolution']}]"
            lines.append(f"  ⚡ {t['caller_class']}.{t['caller_method']} [{t['via']}] "
                         f"{t['source_relpath']}:{t['line']}{res_flag}{src}")

    if ot["unresolved_inbound"]:
        lines.append("")
        lines.append("【未归位的入站嫌疑】（操作 key 或目标单据解不出、无法静态排除是本操作——"
                     "读源码定性，勿忽略）")
        for t in ot["unresolved_inbound"]:
            lines.append(f"  ⚡ [{t['suspect_reason']}] op_key={t['op_key'] or '?'} "
                         f"[{t['op_key_resolution']}] → {t['target_form_key'] or '?'} "
                         f"[{t['target_resolution']}]  "
                         f"{t['caller_class']}.{t['caller_method']} [{t['via']}] "
                         f"{t['source_relpath']}:{t['line']}")

    if ot["triggers_downstream"]:
        lines.append("")
        lines.append("【本操作插件对外触发】（级联链下行；对 next_trace 坐标继续 trace --kind operation）")
        for t in ot["triggers_downstream"]:
            tgt = t["target_form_key"] or "?（目标未解析）"
            nm2 = f"「{t['target_form_name']}」" if t.get("target_form_name") else ""
            nxt = f"  → 下一跳: trace \"{t['next_trace']}\" --kind operation" if t.get("next_trace") else ""
            lines.append(f"  ⚡ → {tgt}{nm2}.{t['op_key'] or '?'}  "
                         f"{t['caller_class']}.{t['caller_method']} [{t['via']}] "
                         f"{t['source_relpath']}:{t['line']}{nxt}")

    if ot["plugins"]:
        lines.append("")
        lines.append("【本操作绑定的操作插件】")
        for p in ot["plugins"]:
            off = "  [停用]" if p.get("enabled") == 0 else ""
            lines.append(f"  [op] {p['class_name']} ({p['source']}){off}")
    return "\n".join(lines)
