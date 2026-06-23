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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..graph import store
from ..java import ast_index as ax


# ── 入口 ──────────────────────────────────────────────────────────────────────
def method_calls(
    conn, class_fqn: str, method_name: str, *, source_root: str | None = None,
) -> dict[str, Any]:
    """给定 类全限定名 + 方法名，返回该方法调用的项目内方法及位置（供大模型继续读源码下钻）。

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
    methods = [
        _payload(fqn, type_decl, md, by_simple, relpath_by_fqn, self_methods)
        for md in matched
    ]
    return _assemble(cls, root, src_text, method_name, methods, java)


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
    """源码根：入参优先，否则取建库时记入 kb_meta 的 source_args.source_root。"""
    if source_root:
        return source_root
    raw = store.get_meta(conn, "source_args")
    if not raw:
        return None
    try:
        return (json.loads(raw) or {}).get("source_root")
    except (json.JSONDecodeError, TypeError):
        return None


def _read_source(root: str | None, relpath: str | None) -> str | None:
    """按建库时同款编码探测读源文件（保证行号与 KB 记录一致）。读不到返回 None。"""
    if not root or not relpath:
        return None
    from ..ingest import scanner
    p = Path(root) / relpath
    if not p.is_file():
        return None
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    enc, _conf = scanner.detect_encoding(raw)
    try:
        return raw.decode(enc, errors="strict")
    except (UnicodeDecodeError, LookupError):
        return raw.decode("gb18030", errors="replace")


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
                "calls": [], "summary": {"project_calls": 0}}]
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
    if rd.get("note"):
        lines.append(f"  {rd['note']}")

    for m in rd["methods"]:
        loc = (f"行 {m['start_line']}–{m['end_line']}"
               if m["start_line"] else "（行号未知）")
        lines.append("")
        lines.append("─" * 72)
        lines.append(f"▼ {m['method_name']}()  {loc}  项目内调用 {m['summary']['project_calls']} 处")
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
