"""阶段 12.3 · 全量 Java 调用边抽取。

``ProjectGraph`` 负责分析期的“能不能继续走到项目源码方法”；本模块负责另一件更原子、
更完整的事：把每个调用点本身持久化为 ``call_edge``。因此平台调用、类内调用、方法引用和
符号解不出的站点都保留，不用“只存成功边”制造虚假的完整感。

resolution 四档：
  * ``expr`` / ``scope``：Symbol Solver 的确定性绑定；
  * ``heuristic``：符号层无结果后，由 tree-sitter 的类内/接收者名字规则唯一命中；
  * ``failed``：两层都无法确定目标，target_fqn 留空但调用坐标仍落库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import ast_index as ax

if TYPE_CHECKING:
    from .ast_index import Invocation
    from .project_graph import ClassNode, ProjectGraph
    from .symbols import SymbolSite


_TYPE_NODES = {
    "class_declaration", "interface_declaration", "enum_declaration",
    "record_declaration", "annotation_type_declaration",
}
_CONFIDENCE = {"expr": 1.0, "scope": 0.95, "heuristic": 0.6, "failed": 0.0}


@dataclass(frozen=True)
class CallEdgeRow:
    caller_fqn: str | None
    caller_method: str
    target_fqn: str | None
    target_method: str
    target_signature: str | None
    kind: str
    line: int
    col: int
    source_relpath: str
    resolution: str
    target_kind: str | None
    confidence: float
    evidence: str


def _text(node) -> str:
    if node is None:
        return ""
    raw = node.text
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def _type_name(node) -> str | None:
    name = node.child_by_field_name("name") if node is not None else None
    return _text(name) or None


def _caller_context(outer: "ClassNode", inv: "Invocation") -> tuple[str, str, object | None]:
    """返回调用点所在的类 FQN、方法名与最近类型节点。

    ProjectGraph 只索引顶层类型，但调用点可能在内部类、构造器或字段初始化器里。这里沿
    tree-sitter parent 链还原 ``Outer$Inner``，并给非普通方法稳定的伪方法名，保证这些站点
    也能被反查而不是静默丢失。
    """
    cur = inv.node
    method: str | None = None
    fallback: str | None = None
    type_names: list[str] = []
    nearest_type = None
    while cur is not None:
        if method is None and cur.type == "method_declaration":
            method = _text(cur.child_by_field_name("name")) or "<unknown>"
        elif method is None and cur.type == "constructor_declaration":
            method = "<init>"
        elif method is None and cur.type == "static_initializer":
            method = "<clinit>"
        elif method is None and cur.type == "field_declaration":
            fallback = fallback or "<field-init>"
        elif method is None and cur.type == "class_body":
            fallback = fallback or "<initializer>"
        if cur.type in _TYPE_NODES:
            if nearest_type is None:
                nearest_type = cur
            name = _type_name(cur)
            if name:
                type_names.append(name)
        cur = cur.parent

    names = list(reversed(type_names))
    if not names:
        caller_fqn = outer.fqn
    else:
        # 最外层名字应与 ProjectGraph 的顶层类一致；脏树不一致时仍以已索引 FQN 为锚。
        if names[0] == outer.simple:
            caller_fqn = outer.fqn + "".join(f"${name}" for name in names[1:])
        else:
            caller_fqn = outer.fqn
    return caller_fqn, method or fallback or "<unknown>", nearest_type


def _declared_methods(type_node) -> set[str]:
    body = type_node.child_by_field_name("body") if type_node is not None else None
    if body is None:
        return set()
    out: set[str] = set()
    for child in body.children:
        if child.type != "method_declaration":
            continue
        name = _text(child.child_by_field_name("name"))
        if name:
            out.add(name)
    return out


def _symbol_evidence(site: "SymbolSite") -> str:
    if site.resolved:
        sig = f"; signature={site.signature}" if site.signature else ""
        return f"symbol:{site.resolution}{sig}"
    return f"symbol:failed; reason={site.reason or 'unknown'}"


def _from_symbol(
    caller_fqn: str | None,
    caller_method: str,
    relpath: str,
    site: "SymbolSite",
    *,
    evidence_prefix: str = "",
) -> CallEdgeRow:
    evidence = _symbol_evidence(site)
    if evidence_prefix:
        evidence = f"{evidence_prefix}; {evidence}"
    return CallEdgeRow(
        caller_fqn=caller_fqn,
        caller_method=caller_method,
        target_fqn=site.declaring if site.resolved else None,
        target_method=site.name,
        target_signature=site.signature if site.resolved else None,
        kind=site.kind,
        line=site.line,
        col=site.col,
        source_relpath=relpath,
        resolution=site.resolution if site.resolved else "failed",
        target_kind=site.target_kind if site.resolved else None,
        confidence=_CONFIDENCE.get(site.resolution if site.resolved else "failed", 0.0),
        evidence=evidence,
    )


def _from_invocation(
    pg: "ProjectGraph",
    outer: "ClassNode",
    inv: "Invocation",
    caller_fqn: str,
    caller_method: str,
    nearest_type,
    site: "SymbolSite | None",
) -> CallEdgeRow:
    if site is not None and site.resolved:
        return _from_symbol(caller_fqn, caller_method, outer.relpath, site)

    target_fqn: str | None = None
    target_method = inv.name
    target_kind: str | None = None
    evidence_bits: list[str] = []
    if site is not None:
        evidence_bits.append(_symbol_evidence(site))
    else:
        evidence_bits.append("symbol:no-site")

    recv = inv.object_text.strip()
    # 类内裸调用/this 调用是名字层能确定的边；递归也要存，不能沿用分析 BFS 的防环过滤。
    if recv in ("", "this") and inv.name in _declared_methods(nearest_type):
        target_fqn = caller_fqn
        target_kind = "project"
        evidence_bits.append("fallback=tree-sitter-local")
    else:
        resolved = pg._resolve_target(outer, caller_method, inv)
        if resolved is not None:
            target_fqn = resolved.fqn
            target_method = resolved.method
            target_kind = "project"
            evidence_bits.append(f"fallback=tree-sitter-{resolved.source}")

    resolution = "heuristic" if target_fqn else "failed"
    if target_fqn is None:
        evidence_bits.append(f"receiver={recv or '<bare>'}; target unresolved")
    return CallEdgeRow(
        caller_fqn=caller_fqn,
        caller_method=caller_method,
        target_fqn=target_fqn,
        target_method=target_method,
        target_signature=None,
        kind=inv.kind,
        line=inv.line,
        col=inv.col,
        source_relpath=outer.relpath,
        resolution=resolution,
        target_kind=target_kind,
        confidence=_CONFIDENCE[resolution],
        evidence="; ".join(evidence_bits),
    )


def collect_call_edges(pg: "ProjectGraph") -> list[CallEdgeRow]:
    """抽取全项目调用点；符号站点与 tree-sitter 对不齐时仍以 ``<unknown>`` caller 落库。"""
    rows: list[CallEdgeRow] = []
    matched_sites: set[int] = set()
    classes_by_relpath: dict[str, list[ClassNode]] = {}
    for node in pg.classes.values():
        classes_by_relpath.setdefault(node.relpath, []).append(node)
        # 每个顶层类型是一棵互不重叠的子树；从类型根扫可覆盖普通方法、构造器、初始化器和内部类。
        for inv in ax.iter_invocations(node.type_decl.node, include_refs=True):
            caller_fqn, caller_method, nearest_type = _caller_context(node, inv)
            site = pg._symbol_site(node, inv)
            if site is not None:
                matched_sites.add(id(site))
            rows.append(_from_invocation(
                pg, node, inv, caller_fqn, caller_method, nearest_type, site))

    # JavaParser 与 tree-sitter 在病态源码上可能只成功一边。SymbolTable 已拿到的站点必须保留；
    # caller 无法安全归属时留空/unknown，不凭同文件多类顺序猜选。
    if pg.symbols is not None:
        for site in pg.symbols.iter_sites():
            if id(site) in matched_sites:
                continue
            candidates = classes_by_relpath.get(site.relpath, [])
            caller_fqn = candidates[0].fqn if len(candidates) == 1 else None
            rows.append(_from_symbol(
                caller_fqn, "<unknown>", site.relpath, site,
                evidence_prefix="tree-sitter caller alignment failed",
            ))

    rows.sort(key=lambda r: (
        r.source_relpath, r.line, r.col, r.caller_fqn or "", r.caller_method,
        r.target_fqn or "", r.target_method,
    ))
    return rows


__all__ = ["CallEdgeRow", "collect_call_edges"]
