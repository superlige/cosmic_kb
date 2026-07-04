"""方法出向调用导航（输入 类全限定名 + 方法名 → 该方法调用的**项目内**方法及其位置）。

定位重置（2026-06-23）：原 `read_method` 想替段二大模型「读源码 + 复述方法在干嘛」。但段二
形态已定为**大模型直接读本机源码 + 挂苍穹 skill**——复述源码、列平台/`equals`/常量调用、做
自然语言解释，大模型自己做得更好，静态层在这块零增量甚至是噪声。

确定性扫描层对一个「会读源码的大模型」唯一不可替代的，是**野生、多 ISV 前缀、不可编译码上的
"跳转到定义"**：大模型读到方法体里 `xxxService.doX()`，它真正缺的一句话是「`doX` 在项目里的
哪个文件」——盲 grep 多前缀很容易命中错类。本报告只回这一件事：

  * 该方法调用的**项目内**方法清单：调用名 + 解析出的目标类全限定名 + 目标源码相对路径 + 调用行号；
  * 不回源码全文（大模型自己读）、不列平台/外部/`equals`/常量调用（噪声）、不做字段落库取证
    （那是 `field_trace` 的本职）、不做自然语言解释（那是段二 skill 的活）。

守红线：接收者类型解不出 → 不臆造（直接不收录，宁缺毋滥）；同末段类名 / 重载方法列候选反问。
延续 report 包约定：dict 在前（供 --json/MCP），`render_*` 文本在后。

CLI/resolver/builder 统一走本文件的富 dict（`method_calls()`，同口径）；只有 MCP 出口
（`tool_method_calls` 直调 + `ask` 解析出 method_calls 意图）换成 `method_calls_compact()`——
方法体调用多或重载方法多时富 dict 会破 host 32KB 硬上限被截断，与 `field_trace.trace_compact`/
`bill_view.bill_compact` 同一套 cap + 字节 governor + 游标分页治法（2026-07-06）。
"""

from __future__ import annotations

import json
from typing import Any

from ..graph import store
from ..java import ast_index as ax
from ..semantic import hints
from . import source_read


# ── 入口 ──────────────────────────────────────────────────────────────────────
def method_calls(
    conn, class_fqn: str, method_name: str, *, source_root: str | None = None,
) -> dict[str, Any]:
    """给定 类全限定名 + 方法名，返回该方法调用的项目内方法及位置（供大模型继续读源码下钻）。

    每个方法还附 `fields`（该方法体读写的字段 key + 是否落库 + 语义路由，**不含中文名**——字段名
    请调 `resolve_fields` 核对，杜绝按命名惯例/拼音猜）；钉不出具体字段的动态写入只计数。
    找不到类 / 同末段类名歧义 / 找不到方法 → 返回 `found=False` + candidates，不臆造。
    需要 tree-sitter（`[parse]` extra）做调用分析；未装则 found=True 但给空清单 + 提示。
    """
    java = json.loads(store.get_meta(conn, "java_analysis") or "{}")
    cls, ambiguous = _locate_class(conn, class_fqn)
    if cls is None:
        return _class_problem(class_fqn, method_name, ambiguous, java)

    fqn, relpath = cls["fqn"], cls["relpath"]
    root = _resolve_source_root(conn, source_root)
    src_text = _read_source(root, relpath)

    root_node = ax.parse_tree(src_text) if src_text else None
    if root_node is None:
        # tree-sitter 未装 / 源码缺失 / 解析失败 → 无法做调用分析，给空清单 + 明确提示。
        return _no_analysis(conn, cls, root, src_text, method_name, java)

    type_decl = _find_type(root_node, fqn)
    if type_decl is None:
        return _no_analysis(conn, cls, root, src_text, method_name, java)

    all_methods = list(ax.iter_methods(type_decl))
    matched = [m for m in all_methods if m.name == method_name]
    if not matched:
        names = sorted({m.name for m in all_methods})
        return _method_not_found(fqn, relpath, method_name, names, java)

    by_simple = _by_simple(conn)
    relpath_by_fqn = _relpath_by_fqn(conn)
    self_methods = {m.name for m in all_methods}
    methods = []
    for md in matched:
        p = _payload(fqn, type_decl, md, by_simple, relpath_by_fqn, self_methods)
        p["fields"] = _fields_in_method(conn, relpath, md.start_line, md.end_line)
        methods.append(p)
    return _assemble(cls, root, src_text, method_name, methods, java)


def _fields_in_method(conn, relpath, start, end) -> dict[str, Any]:
    """该方法体（按行范围）读写的字段 key + 语义路由，钉不出具体字段的动态写入只计数。

    field_access 无"访问方法"列，但有 source_relpath（相对源码根）+ line；方法的字段 =
    本文件里 line 落在 [start, end] 的访问。不附中文名——需要就调 resolve_fields 核对。
    """
    empty = {"writes": [], "reads": [], "dynamic_writes": 0}
    if not (relpath and start and end):
        return empty
    rows = conn.execute(
        "SELECT field_key,form_key,level,entry_key,access,persists,event_method,plugin_type,line "
        "FROM field_access WHERE source_relpath=? AND line BETWEEN ? AND ?",
        (relpath, start, end)).fetchall()
    writes: dict[tuple, dict[str, Any]] = {}
    reads: dict[tuple, dict[str, Any]] = {}
    dynamic = 0
    for r in rows:
        if not r["field_key"]:
            if r["access"] == "write":
                dynamic += 1          # 钉不出具体字段的动态写入：只诚实计数，交 dynwrites/读源码定性
            continue
        bucket = writes if r["access"] == "write" else reads
        key = (r["field_key"], r["entry_key"])
        if key in bucket:
            continue
        bucket[key] = {
            "field_key": r["field_key"],
            "form_key": r["form_key"],
            "level": r["level"], "entry_key": r["entry_key"],
            "persists": r["persists"], "line": r["line"],
            "semantics_topic": hints.event_topic(r["event_method"], r["plugin_type"]),
        }
    by_line = lambda d: sorted(d.values(), key=lambda x: x["line"] or 0)
    return {"writes": by_line(writes), "reads": by_line(reads), "dynamic_writes": dynamic}


# ── 定位类 / 源码根 / 读源码 ────────────────────────────────────────────────────
def _locate_class(conn, class_fqn: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """定位类文件。返回 (类记录 | None, 歧义候选)。精确 fqn 优先；只给简单名按末段匹配。"""
    row = conn.execute(
        "SELECT fqn,relpath,module FROM source_class WHERE fqn=?", (class_fqn,)).fetchone()
    if row and row["relpath"]:
        return dict(row), []
    # 末段简单名匹配（用户常只给类名而非全限定名）。
    rows = {}
    for r in conn.execute(
        "SELECT fqn,relpath,module FROM source_class WHERE simple=? OR fqn LIKE ?",
        (class_fqn, f"%.{class_fqn}")).fetchall():
        if r["relpath"]:
            rows.setdefault(r["fqn"], dict(r))
    uniq = list(rows.values())
    if len(uniq) == 1:
        return uniq[0], []
    if len(uniq) > 1:
        return None, uniq
    # 回落 plugin_method（source_class 未收录但插件方法表记了源码路径的）。
    prow = conn.execute(
        "SELECT plugin_fqn,source_relpath FROM plugin_method WHERE plugin_fqn=? "
        "AND source_relpath IS NOT NULL LIMIT 1", (class_fqn,)).fetchone()
    if prow:
        return {"fqn": prow["plugin_fqn"], "relpath": prow["source_relpath"], "module": None}, []
    return None, []


def _resolve_source_root(conn, source_root: str | None) -> str | None:
    """源码根：入参优先，否则取 kb_meta 的 source_args.source_root（委托 source_read 公共件）。"""
    return source_read.resolve_source_root(conn, source_root)


def _read_source(root: str | None, relpath: str | None) -> str | None:
    """按建库时同款编码探测读源文件（保证行号与 KB 记录一致）。读不到返回 None。"""
    text, _enc = source_read.read_text(root, relpath)
    return text


def _find_type(root_node, fqn: str):
    """在文件顶层类型里按简单名匹配（文件名与类名不一致时也按类名命中）。"""
    simple = fqn.rsplit(".", 1)[-1]
    for td in ax.iter_type_declarations(root_node):
        if td.name == simple:
            return td
    return None


def _by_simple(conn) -> dict[str, list[str]]:
    """简单类名 → [FQN]（把跨类调用的接收者类型解析成项目内类，唯一命中才下钻）。"""
    m: dict[str, list[str]] = {}
    for r in conn.execute("SELECT simple,fqn FROM source_class WHERE simple IS NOT NULL"):
        bucket = m.setdefault(r["simple"], [])
        if r["fqn"] not in bucket:
            bucket.append(r["fqn"])
    return m


def _relpath_by_fqn(conn) -> dict[str, str]:
    """项目内类 FQN → 源码相对路径（让大模型知道目标方法去哪个文件接着读）。"""
    return {r["fqn"]: r["relpath"]
            for r in conn.execute("SELECT fqn,relpath FROM source_class WHERE relpath IS NOT NULL")}


# ── 组装单个方法的调用清单 ─────────────────────────────────────────────────────
def _payload(fqn, type_decl, md, by_simple, relpath_by_fqn, self_methods) -> dict[str, Any]:
    calls = _project_calls(fqn, type_decl, md, by_simple, relpath_by_fqn, self_methods)
    return {
        "method_name": md.name,
        "start_line": md.start_line,
        "end_line": md.end_line,
        "calls": calls,
        "summary": {"project_calls": len(calls)},
    }


def _project_calls(fqn, type_decl, md, by_simple, relpath_by_fqn, self_methods) -> list[dict[str, Any]]:
    """只收**能确定性解析到项目内类**的调用：本类自调用 / 接收者类型唯一命中项目内类。

    解不出接收者类型、平台调用（`kd.` / `*Helper`）、落库 sink、`equals`/常量取值等——一律不收录
    （大模型读源码自己看得到，列出来纯属噪声）。每条给目标类 FQN + 目标源码相对路径 + 调用行号。
    """
    types: dict[str, str] = {}
    for n, t in ax.iter_param_vars(md.node):
        if t:
            types[n] = t
    for n, t in ax.iter_local_var_types(md.body):
        if t:
            types[n] = t
    member = {n: t for n, t in ax.iter_member_field_types(type_decl) if t}

    seen: dict[tuple, dict[str, Any]] = {}
    for inv in ax.iter_invocations(md.body):
        recv = (inv.object_text or "").strip()
        name = inv.name
        target_fqn: str | None = None
        if recv in ("", "this"):
            # 本类自身的另一个方法（继承自平台基类的事件回调不算，name 不在本类声明里）。
            if name in self_methods and name != md.name:
                target_fqn = fqn
        else:
            simple = _resolve_simple(recv, types, member, by_simple)
            if simple and len(by_simple.get(simple, [])) == 1:
                target_fqn = by_simple[simple][0]
        if target_fqn is None:        # 解不出项目内目标 → 不收录（宁缺毋滥）
            continue
        key = (target_fqn, name)
        if key not in seen:
            seen[key] = {
                "name": name,
                "receiver": recv or "this",
                "target_fqn": target_fqn,
                "target_relpath": relpath_by_fqn.get(target_fqn),
                "line": inv.line,
            }
    return sorted(seen.values(), key=lambda c: c["line"])


def _resolve_simple(recv, types, member, by_simple) -> str | None:
    """把接收者表达式解析成项目内类的简单名（局部/形参/成员/静态/new）；解不出 None。"""
    if recv.startswith("new "):
        return ax.simple_type_name(recv[4:])
    base = recv.split(".", 1)[0].split("(", 1)[0].strip()
    if base in types:
        return types[base]
    if base in member:
        return member[base]
    if base[:1].isupper() and base in by_simple:   # 静态调用 ClassName.method()
        return base
    return None


# ── 降级 / 未找到 ──────────────────────────────────────────────────────────────
def _no_analysis(conn, cls, root, src_text, method_name, java) -> dict[str, Any]:
    """tree-sitter 不可用 / 源码读不到 / 解析失败：确认方法存在则给空清单 + 提示，否则未命中。"""
    fqn, relpath = cls["fqn"], cls["relpath"]
    exists = conn.execute(
        "SELECT 1 FROM plugin_method WHERE plugin_fqn=? AND method_name=? LIMIT 1",
        (fqn, method_name)).fetchone()
    if not exists:
        names = [r["method_name"] for r in conn.execute(
            "SELECT DISTINCT method_name FROM plugin_method WHERE plugin_fqn=? ORDER BY method_name",
            (fqn,)).fetchall()]
        if names:
            return _method_not_found(fqn, relpath, method_name, names, java)
    methods = [{"method_name": method_name, "start_line": None, "end_line": None,
                "calls": [], "summary": {"project_calls": 0},
                "fields": {"writes": [], "reads": [], "dynamic_writes": 0}}]
    return _assemble(cls, root, src_text, method_name, methods, java)


def _assemble(cls, root, src_text, method_name, methods, java) -> dict[str, Any]:
    fqn, relpath = cls["fqn"], cls["relpath"]
    if not java.get("available", True):
        note = ("⚠ tree-sitter 未启用（pip install -e .[parse]）：无法做调用分析，清单为空。"
                "源码请由大模型直接读 " + str(relpath) + "。")
    elif not src_text:
        note = ("源码根未配置或文件读取失败：无法做调用分析。可加 --source-root <源码根>，"
                "或确认 KB 的 source_args 指向有效源码。")
    elif not any(m["calls"] for m in methods):
        note = "该方法未解析出项目内调用（要么没有，要么接收者类型解不出，已按宁缺毋滥不臆造）。"
    else:
        note = "清单只列项目内可下钻调用；平台/外部调用与源码全文请由大模型直接读源文件。"
    return {
        "found": True,
        "class_fqn": fqn,
        "class_simple": fqn.rsplit(".", 1)[-1],
        "module": cls.get("module"),
        "relpath": relpath,
        "source_root": root,
        "source_available": bool(src_text),
        "method_name": method_name,
        # 模式 B：被导航方法若是苍穹事件回调，焊上语义文档主题（解释它"在干嘛"前先核对触发时机/入库）。
        "semantics_topic": hints.event_topic(method_name),
        "overloaded": len(methods) > 1,
        "methods": methods,
        "java_available": java.get("available", True),
        "note": note,
    }


def _method_not_found(fqn, relpath, method_name, names, java) -> dict[str, Any]:
    return {
        "found": False, "reason": "method_not_found",
        "class_fqn": fqn, "relpath": relpath, "method_name": method_name,
        "candidates": names,
        "java_available": java.get("available", True),
        "note": f"类 {fqn} 里没有方法 {method_name}。"
                f"该类已知方法：{'、'.join(names) if names else '（KB 无方法记录）'}。",
    }


def _class_problem(class_fqn, method_name, ambiguous, java) -> dict[str, Any]:
    if ambiguous:
        return {
            "found": False, "reason": "class_ambiguous",
            "class_fqn": class_fqn, "method_name": method_name,
            "candidates": [{"fqn": c["fqn"], "relpath": c["relpath"]} for c in ambiguous],
            "java_available": java.get("available", True),
            "note": f"末段类名 {class_fqn} 命中 {len(ambiguous)} 个不同包的类，请用全限定名再查。",
        }
    return {
        "found": False, "reason": "class_not_found",
        "class_fqn": class_fqn, "method_name": method_name,
        "candidates": [],
        "java_available": java.get("available", True),
        "note": f"KB 里没有类 {class_fqn}（类名/包不对，或源码未纳入）。",
    }


# ── 紧凑投影（MCP 防截断）：calls/字段读写按方法计 cap + 字节 governor + 游标分页 ──────────
# 真实翻车（2026-07-06，InvoiceWriteBackTask.execute 经 ask 调用）：本报告原先只有一份富 dict，
# 未像 field_trace/bill_view 那样切一层紧凑投影——方法体调用多、或重载方法多时序列化轻松破
# host 32KB 硬上限，被从中段截断。修复思路与 trace_compact 同款：cap + 字节 governor（按
# host `json.dumps(indent=2)` 口径量，见 `_wire_len`）+ 游标分页（红线 #4：被 cap 的条目仍可
# 逐页取回，不只报计数）。
_MC_CAP_METHODS = 20   # 重载方法条数上限（多重载罕见，纯兜底）
_MC_CAP_CALLS = 40     # 单方法 calls 上限
_MC_CAP_FIELDS = 25    # 单方法 fields.writes / fields.reads 各自上限
_MC_BIG_CAP = 10 ** 9  # 分页态"不裁剪"哨兵，折叠出完整清单供分页 slice
_MC_BUDGET = 31000     # 序列化预算，留出 host 32768 硬上限的裕量（同 field_trace._wire_len 口径）
# cap 阶梯：(方法数, 单方法 calls, 单方法 fields) 从宽到窄，命中预算即返，最后一档兜底。
_MC_LADDER = [
    (_MC_CAP_METHODS, _MC_CAP_CALLS, _MC_CAP_FIELDS),
    (12, 25, 15),
    (8, 15, 10),
    (5, 10, 6),
    (3, 6, 4),
    (2, 3, 2),
    (1, 2, 1),         # 硬底：单方法也能塌到最小
]


def _wire_len(obj: Any) -> int:
    """按 MCP host 真实序列化方式（`json.dumps(indent=2, ensure_ascii=True)`）度量字节数。

    与 `field_trace._wire_len` 同一口径——indent 缩进会让深层嵌套结构膨胀，只量无缩进会低估。
    """
    return len(json.dumps(obj, ensure_ascii=True, indent=2))


def _cap_method(m: dict[str, Any], idx: int, *, cap_calls: int, cap_fields: int) -> dict[str, Any]:
    """单方法节点裁剪：calls 与 fields.writes/reads 各自 cap，真实总数/截断量留 `*_total`/`*_capped`。

    `idx` 是该方法在（裁剪前）`methods` 列表里的位置，用作分页游标定位（`calls:<idx>@offset`）。
    """
    calls = m["calls"]
    fields = m.get("fields") or {"writes": [], "reads": [], "dynamic_writes": 0}
    writes, reads = fields.get("writes", []), fields.get("reads", [])
    calls_capped = max(0, len(calls) - cap_calls)
    out: dict[str, Any] = {
        **m, "calls": calls[:cap_calls],
        "calls_total": len(calls), "calls_capped": calls_capped,
    }
    if calls_capped:
        out["calls_next_cursor"] = f"calls:{idx}@{cap_calls}"
    writes_capped = max(0, len(writes) - cap_fields)
    reads_capped = max(0, len(reads) - cap_fields)
    out_fields: dict[str, Any] = {
        "writes": writes[:cap_fields], "writes_total": len(writes), "writes_capped": writes_capped,
        "reads": reads[:cap_fields], "reads_total": len(reads), "reads_capped": reads_capped,
        "dynamic_writes": fields.get("dynamic_writes", 0),
    }
    if writes_capped:
        out_fields["writes_next_cursor"] = f"writes:{idx}@{cap_fields}"
    if reads_capped:
        out_fields["reads_next_cursor"] = f"reads:{idx}@{cap_fields}"
    out["fields"] = out_fields
    return out


def _build_compact(rd: dict[str, Any], *, cap_methods: int, cap_calls: int, cap_fields: int
                   ) -> dict[str, Any]:
    """一档 cap 下的一次组装（governor 会按字节预算反复调用收紧）。"""
    methods = rd["methods"]
    shown = methods[:cap_methods]
    out_methods = [_cap_method(m, i, cap_calls=cap_calls, cap_fields=cap_fields)
                  for i, m in enumerate(shown)]
    methods_capped = max(0, len(methods) - cap_methods)
    res: dict[str, Any] = {**rd, "methods": out_methods, "methods_total": len(methods),
                           "methods_capped": methods_capped}
    if methods_capped:
        res["methods_next_cursor"] = f"methods@{cap_methods}"
    capped_hit = bool(methods_capped) or any(
        m["calls_capped"] or m["fields"]["writes_capped"] or m["fields"]["reads_capped"]
        for m in out_methods)
    notes = [rd["note"]] if rd.get("note") else []
    if capped_hit:
        notes.append("部分方法/调用/字段因数量过多被截断（真实总数见 methods_total 与各节点 "
                     "*_total/*_capped）；被截段带 next_cursor，用 cursor=该值再调一次可翻页取回"
                     "全部被截条目，不丢数。")
    res["note"] = " ".join(notes) if notes else None
    return res


def _mc_parse_cursor(cursor: str) -> tuple[str, int]:
    """解析 `"section@offset"` → (section, offset)；缺/非法 offset 归 0（同 field_trace 惯例）。"""
    section, _, off = cursor.strip().partition("@")
    try:
        offset = max(0, int(off)) if off else 0
    except ValueError:
        offset = 0
    return section.strip(), offset


def _mc_page(rd: dict[str, Any], cursor: str, budget: int) -> dict[str, Any]:
    """聚焦分页：`methods@N` 翻方法列表本身；`calls:<idx>@N`/`writes:<idx>@N`/`reads:<idx>@N`
    翻某个方法（按原始 `methods` 下标定位）的调用/字段明细。"""
    base = {k: v for k, v in rd.items() if k != "methods"}
    section, offset = _mc_parse_cursor(cursor)

    head: dict[str, Any] = {}
    if section == "methods":
        items: list[Any] = [_cap_method(m, i, cap_calls=_MC_BIG_CAP, cap_fields=_MC_BIG_CAP)
                            for i, m in enumerate(rd["methods"])]
    else:
        kind, _, idx_s = section.partition(":")
        idx = int(idx_s) if idx_s.isdigit() else -1
        if kind not in ("calls", "writes", "reads") or not (0 <= idx < len(rd["methods"])):
            return {**base, "page": {"section": section,
                    "error": f"未知或不可分页的 section: {section}"
                             "（可分页：methods / calls:<idx> / writes:<idx> / reads:<idx>）"}}
        m = rd["methods"][idx]
        head = {"method_name": m["method_name"], "method_idx": idx}
        items = m["calls"] if kind == "calls" else (m.get("fields") or {}).get(kind, [])

    total = len(items)
    offset = min(max(0, offset), total)

    def _wrap(page: list[Any], nxt: int) -> dict[str, Any]:
        return {**base, "page": {**head, "section": section, "offset": offset,
                                 "returned": len(page), "total": total, "items": page,
                                 "next_cursor": (f"{section}@{nxt}" if nxt < total else None)}}

    page: list[Any] = []
    for it in items[offset:]:
        trial = page + [it]
        if page and _wire_len(_wrap(trial, offset + len(trial))) > budget:
            break          # 至少装一条（单条即便超 budget 也给，仍远小于 32KB）
        page = trial
    return _wrap(page, offset + len(page))


def method_calls_compact(
    conn, class_fqn: str, method_name: str, *, source_root: str | None = None,
    cursor: str | None = None, budget: int = _MC_BUDGET,
) -> dict[str, Any]:
    """**紧凑投影**（MCP 入口，防 host 32KB 截断）：calls / fields.writes/reads 按方法计
    cap + 字节 governor + 游标分页；未命中/歧义（`found=False`）体积天然小，原样透传。

    - governor：构完测序列化字节，超 `budget` 就逐级收紧 cap 重建，直至 ≤ budget——保证永不被截断。
    - 真实总数恒在 `methods_total`/各方法 `calls_total`/`fields.writes_total`/`fields.reads_total`；
      被 cap 的数在对应 `*_capped`。
    - `cursor`（形如 `"calls:0@40"`）：被 cap 的段带 `next_cursor`，用 cursor=该值再调一次即翻到
      下一页，逐页可**取回全部被截条目**（红线 #4：不仅报计数，还可达）。
    """
    rd = method_calls(conn, class_fqn, method_name, source_root=source_root)
    if not rd.get("found"):
        return rd
    if cursor:
        return _mc_page(rd, cursor, budget)
    res: dict[str, Any] = rd
    for cap_methods, cap_calls, cap_fields in _MC_LADDER:
        res = _build_compact(rd, cap_methods=cap_methods, cap_calls=cap_calls, cap_fields=cap_fields)
        if _wire_len(res) <= budget:
            return res
    return res


# ── 渲染（终端文本）──────────────────────────────────────────────────────────────
def render_method_calls(rd: dict[str, Any], *, max_list: int = 50) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    if not rd.get("found"):
        lines.append(f"方法调用导航: {rd.get('class_fqn')}#{rd.get('method_name')}  —— 未命中")
        lines.append("=" * 72)
        lines.append(f"  {rd.get('note') or ''}")
        cands = rd.get("candidates") or []
        if cands:
            lines.append("  候选：")
            for c in cands[:max_list]:
                lines.append(f"    - {c['fqn'] if isinstance(c, dict) else c}")
        return "\n".join(lines)

    lines.append(f"方法调用导航: {rd['class_simple']}#{rd['method_name']}"
                 + ("（重载多个）" if rd["overloaded"] else ""))
    lines.append("=" * 72)
    lines.append(f"  类 {rd['class_fqn']}  模块 {rd.get('module') or '?'}")
    lines.append(f"  源码 {rd['relpath']}（请直接读此文件看方法全文）"
                 + ("" if rd["source_available"] else "  ⚠源码未读到"))
    if rd.get("semantics_topic"):
        lines.append(f"  ⚑ {rd['method_name']} 是苍穹事件回调（{rd['semantics_topic']}）："
                     f"解释它在干嘛/判触发时机/是否入库前先 cosmic_semantics('{rd['semantics_topic']}')")
    if rd.get("note"):
        lines.append(f"  {rd['note']}")

    for m in rd["methods"]:
        loc = (f"行 {m['start_line']}–{m['end_line']}"
               if m["start_line"] else "（行号未知）")
        lines.append("")
        lines.append("─" * 72)
        lines.append(f"▼ {m['method_name']}()  {loc}  项目内调用 {m['summary']['project_calls']} 处")
        fl = m.get("fields") or {}
        if fl.get("writes") or fl.get("reads"):
            lines.append("  【本方法读写字段（key，中文名请调 resolve_fields 核对）】")
            _pl = {"yes": "✅落库", "no": "—内存", "unknown": "❓存疑", "na": ""}
            for w in fl.get("writes", [])[:max_list]:
                tp = f"  ⚑{w['semantics_topic']}" if w.get("semantics_topic") else ""
                lines.append(f"    写 {w['field_key']} {_pl.get(w['persists'], '')}  :{w['line']}{tp}")
            for r in fl.get("reads", [])[:max_list]:
                lines.append(f"    读 {r['field_key']}  :{r['line']}")
        if fl.get("dynamic_writes"):
            lines.append(f"  ⚠ 另有 {fl['dynamic_writes']} 处动态写入钉不出具体字段（→ dynwrites / 读源码定性）")
        if m["calls"]:
            lines.append("  【项目内调用】（→ 去对应文件接着读 / 可再对其调用导航）")
            for c in m["calls"][:max_list]:
                rel = c.get("target_relpath") or "?"
                lines.append(
                    f"    → {c['receiver']}.{c['name']}()  :{c['line']}")
                lines.append(
                    f"        定义于 {c['target_fqn']}  {rel}"
                    f"   下钻: calls {c['target_fqn']} {c['name']}")
    return "\n".join(lines)
