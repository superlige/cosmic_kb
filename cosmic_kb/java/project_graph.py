"""阶段 6（跨类版）· 全项目类索引 + 跨类调用回溯。

阶段 6 的类内调用图只在一个类里走（`call_graph.py`）。但真实项目里，插件事件常把单据
数据包丢给 service/工具类去改字段（用户 2026-06-17 反馈的 CollateralService 即此类）——
字段写入物理上发生在另一个类里。只看类内会整片漏掉。

本模块解决两件事：
  1. **全项目类索引**：每个项目源码顶层类型解析一次（树缓存），记 FQN→类信息（类内调用图、
     成员字段类型、方法表），并顺便灌常量表（复用同一棵树、不重复解析）。
  2. **跨类调用回溯**：从一个事件方法出发，沿「类内调用 + 可解析的跨类调用」BFS，返回每个
     可达 (类, 方法) + 调用路径。阶段 12 优先消费编译期 SymbolTable；未提供、调用点解不出或
     对不齐时，才退回**本地变量声明类型 / 成员字段类型 / 静态调用类名**启发式。

只跟进**项目自有类**（有源码、在索引里）；调到 kd.bos.* 等平台/外部方法解析不了，记为
unresolved（落库判定据此保守标 unknown）。守红线：处处置信度、跨类靠传递路径、解不出标 unknown。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING

from . import ast_index as ax
from . import call_graph as cgmod
from . import constants as const_mod

if TYPE_CHECKING:
    from ..bridge.namespace import SourceIndex
    from ..ingest.scanner import ScanResult
    from ..progress import Progress
    from .ast_index import TypeDecl
    from .call_graph import CallGraph
    from .constants import ConstantTable
    from .symbols import SymbolSite, SymbolTable


@dataclass
class ClassNode:
    """一个项目源码顶层类型的分析单元。"""

    fqn: str
    simple: str
    relpath: str
    type_decl: "TypeDecl"
    cg: "CallGraph"                          # 类内调用图（复用 call_graph）
    member_types: dict[str, str] = field(default_factory=dict)  # 成员字段名 → 类型简单名


@dataclass
class CrossReach:
    """从某事件入口跨类可达的一个方法。"""

    fqn: str
    method: str
    path: list[str]                          # [事件名, …, 限定名]（跨类段用 Simple.method）


@dataclass(frozen=True)
class ResolvedCall:
    """一个已落到项目源码方法的调用边，并保留解析来源供精度分级。"""

    fqn: str
    method: str
    source: str                              # local | symbol | heuristic

    @property
    def key(self) -> tuple[str, str]:
        return self.fqn, self.method


class ProjectGraph:
    """全项目类索引 + 常量表。"""

    def __init__(self, symbols: "SymbolTable | None" = None) -> None:
        self.classes: dict[str, ClassNode] = {}      # FQN → 类信息
        self.by_simple: dict[str, list[str]] = {}    # 简单名 → [FQN]（类型解析）
        self.const: "ConstantTable" = const_mod.ConstantTable()
        self.symbols = symbols
        # 接收者类型解析缓存：(fqn, method) → {变量名: 类型简单名}
        self._local_cache: dict[tuple[str, str], dict[str, str]] = {}

    # ── 接收者类型解析 ────────────────────────────────────────────────
    def _local_types(self, node: ClassNode, method: str) -> dict[str, str]:
        key = (node.fqn, method)
        cached = self._local_cache.get(key)
        if cached is not None:
            return cached
        md = node.cg.methods.get(method)
        types: dict[str, str] = {}
        if md is not None:
            for name, t in ax.iter_param_vars(md.node):
                if t:
                    types[name] = t
            for name, t in ax.iter_local_var_types(md.body):
                if t:
                    types[name] = t
        self._local_cache[key] = types
        return types

    def _symbol_site(self, node: ClassNode, inv: ax.Invocation) -> "SymbolSite | None":
        if self.symbols is None:
            return None
        return self.symbols.lookup(node.relpath, inv.line, inv.name, inv.col)

    def _resolve_target(
        self, node: ClassNode, method: str, inv: ax.Invocation,
    ) -> ResolvedCall | None:
        """把跨类调用解析成项目内目标；符号精确层优先，失败才退名字启发式。

        符号已成功落到 jar/jdk（或落到当前图未收录的源码类型）时不再用同名启发式覆盖这个
        确定性结论；只有无 site / failed site 才回退。
        """
        site = self._symbol_site(node, inv)
        if site is not None and site.resolved:
            if (site.target_kind == "project" and site.declaring in self.classes
                    and inv.name in self.classes[site.declaring].cg.methods):
                return ResolvedCall(site.declaring, inv.name, "symbol")
            return None

        recv = inv.object_text.strip()
        if recv in ("", "this"):
            return None
        base = recv.split(".", 1)[0].split("(", 1)[0].strip()
        simple: str | None = None
        if recv.startswith("new "):
            m = re.match(r"new\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*(?:<[^>]*>)?\s*\(", recv)
            simple = ax.simple_type_name(m.group(1)) if m else ax.simple_type_name(recv[4:])
        else:
            local = self._local_types(node, method)
            if base in local:
                simple = local[base]
            elif base in node.member_types:
                simple = node.member_types[base]
            elif base and base[:1].isupper() and base in self.by_simple:
                simple = base                       # 静态调用 ClassName.method()
        if not simple:
            return None
        fqns = self.by_simple.get(simple, [])
        if len(fqns) != 1:                          # 0 个或同名歧义 → 不臆造
            return None
        target = fqns[0]
        if inv.name in self.classes[target].cg.methods:
            return ResolvedCall(target, inv.name, "heuristic")
        return None

    # ── 跨类可达 ──────────────────────────────────────────────────────
    def reachable(self, start_fqn: str, start_method: str, *, max_depth: int = 8) -> list[CrossReach]:
        """从 (类, 事件方法) 出发，沿类内 + 可解析跨类调用 BFS，返回每个可达 (类, 方法) + 路径。"""
        if start_fqn not in self.classes or start_method not in self.classes[start_fqn].cg.methods:
            return []
        start = (start_fqn, start_method)
        seen: dict[tuple[str, str], list[str]] = {start: [start_method]}
        queue: list[tuple[tuple[str, str], int]] = [(start, 0)]
        while queue:
            (fqn, method), depth = queue.pop(0)
            if depth >= max_depth:
                continue
            node = self.classes[fqn]
            path = seen[(fqn, method)]
            # 类内调用。
            for callee in sorted(node.cg.calls.get(method, ())):
                k = (fqn, callee)
                if k not in seen:
                    seen[k] = path + [callee]
                    queue.append((k, depth + 1))
            # 跨类调用。
            md = node.cg.methods.get(method)
            if md is not None:
                for inv in ax.iter_invocations(md.body, include_refs=True):
                    tgt = self._resolve_target(node, method, inv)
                    key = tgt.key if tgt is not None else None
                    if key is not None and key not in seen:
                        tsimple = self.classes[tgt.fqn].simple
                        seen[key] = path + [f"{tsimple}.{tgt.method}"]
                        queue.append((key, depth + 1))
        return [CrossReach(fqn=f, method=m, path=p) for (f, m), p in seen.items()]

    def has_unresolved_external(self, reach: list[CrossReach]) -> bool:
        """可达集里是否存在「出本类且解析不到项目方法」的调用（落库 sink 可能藏在平台/外部）。"""
        for r in reach:
            node = self.classes.get(r.fqn)
            if node is None:
                continue
            md = node.cg.methods.get(r.method)
            if md is None:
                continue
            for inv in ax.iter_invocations(md.body, include_refs=True):
                recv = inv.object_text.strip()
                if recv in ("", "this"):
                    continue
                site = self._symbol_site(node, inv)
                if site is not None and site.resolved:
                    # 已证实是平台/JDK 目标：若它是落库 sink，find_sinks 会按 FQN 白名单收；
                    # 若不是 sink，就不再把它当“未知外泄”把 persistence 压到 0.3 档。
                    if site.target_kind in ("jar", "jdk"):
                        continue
                    if site.target_kind == "project" and site.declaring in self.classes:
                        continue
                if "." not in recv:
                    continue                        # 简单接收者（bill.set 等）不视为外泄
                if self._resolve_target(node, r.method, inv) is None:
                    return True
        return False


def build_project_graph(scan_result: "ScanResult", index: "SourceIndex",
                        symbols: "SymbolTable | None" = None,
                        progress: "Progress | None" = None) -> ProjectGraph:
    """解析全部项目 Java（每文件一次），建类索引 + 常量表。

    progress：可选进度报告器——这是 Java 分析里最先跑、也最容易被误当"卡住"的一步
    （逐文件 tree-sitter 解析），按文件数打点。
    """
    pg = ProjectGraph(symbols)
    java_files = [sf for sf in scan_result.ok_files
                  if sf.relpath.lower().endswith(".java")]
    for done, sf in enumerate(java_files, 1):
        if progress is not None:
            progress.tick(done, len(java_files), "个文件", label="解析工程 Java")
        root = ax.parse_tree(sf.text)
        if root is None:
            continue
        const_mod.collect_into(root, pg.const, sf.relpath)  # 复用本棵树灌常量，避免重复解析
        package = ax.package_name(root)
        for td in ax.iter_type_declarations(root):
            fqn = f"{package}.{td.name}" if package else td.name
            if fqn in pg.classes:
                continue                             # 同名 FQN（脏数据）取首个
            node = ClassNode(
                fqn=fqn, simple=td.name, relpath=sf.relpath, type_decl=td,
                cg=cgmod.build_call_graph(td),
                member_types={n: t for n, t in ax.iter_member_field_types(td) if t},
            )
            pg.classes[fqn] = node
            pg.by_simple.setdefault(td.name, []).append(fqn)
    return pg
