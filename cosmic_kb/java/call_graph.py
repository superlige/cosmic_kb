"""阶段 6（类内版）· 类内调用图 + 事件入口路径。

落库判定要面对真实开发的现实（用户 2026-06-17）：事件函数里调本类函数、函数内部再落库，
**层级不可预测**。故从每个事件方法出发，沿**本类私有/自有方法调用链**遍历可达方法，
记录路径（事件→…→方法）——字段写入与落库 sink 就能归到正确的事件入口上下文。

本轮只做**类内**调用链（同类方法按名匹配，接收者为 this/空）。跨类工具函数标 unresolved
（留证据，下层再深入），不臆造。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import ast_index as ax

if TYPE_CHECKING:
    from .ast_index import MethodDecl, TypeDecl


@dataclass
class CallGraph:
    methods: dict[str, "MethodDecl"]              # 方法名 → 声明（同名重载取首个，类内足够）
    calls: dict[str, set[str]]                    # 方法名 → 它调用的本类方法名集合
    external_calls: dict[str, int]                # 方法名 → 出本类（unresolved）的调用计数


def build_call_graph(type_decl: "TypeDecl") -> CallGraph:
    """建一个类的类内调用图。"""
    methods: dict[str, "MethodDecl"] = {}
    for m in ax.iter_methods(type_decl):
        methods.setdefault(m.name, m)   # 重载同名：类内路径分析取首个即可

    calls: dict[str, set[str]] = {}
    external: dict[str, int] = {}
    for name, m in methods.items():
        own: set[str] = set()
        ext = 0
        for inv in ax.iter_invocations(m.body):
            recv = inv.object_text.strip()
            if recv in ("", "this") and inv.name in methods and inv.name != name:
                own.add(inv.name)            # 同类方法调用（this.x() / x()）
            elif recv in ("", "this") and inv.name in methods:
                pass                          # 自调用（递归）忽略，防环
            elif recv not in ("", "this") and "." in (inv.object_text or ""):
                ext += 1                      # 出本类调用：unresolved（跨类工具函数）
        calls[name] = own
        external[name] = ext
    return CallGraph(methods=methods, calls=calls, external_calls=external)


@dataclass
class Reach:
    """从某事件入口可达的一个方法及其路径。"""

    method: str
    path: list[str] = field(default_factory=list)  # [事件名, …, method]


def reachable_from(cg: CallGraph, event_method: str) -> list[Reach]:
    """从事件方法出发，DFS 类内调用链，返回每个可达方法（含事件本身）+ 一条路径。

    同一方法可被多条路径触达，这里取**首次发现的最短路径**（够用、稳定、防环）。
    """
    if event_method not in cg.methods:
        return []
    seen: dict[str, list[str]] = {event_method: [event_method]}
    queue: list[str] = [event_method]
    while queue:
        cur = queue.pop(0)
        for callee in sorted(cg.calls.get(cur, ())):
            if callee not in seen:
                seen[callee] = seen[cur] + [callee]
                queue.append(callee)
    return [Reach(method=m, path=p) for m, p in seen.items()]


def has_external_calls(cg: CallGraph, reach: list[Reach]) -> bool:
    """该事件的可达路径里是否存在出本类调用（→ 落库/字段写入可能藏在跨类工具函数里）。"""
    return any(cg.external_calls.get(r.method, 0) > 0 for r in reach)


def roots(cg: CallGraph) -> list[str]:
    """调用图根方法：不被同类其它方法调用的方法（=对外公共入口，如 task 的 execute）。

    供未绑定插件（AbstractTask/WebApi…）选跨类回溯入口：从根方法出发即可覆盖其全部逻辑，
    又不会把被内部调用的 helper 当成独立入口（避免同一处写入被重复归因）。
    """
    called: set[str] = set()
    for callees in cg.calls.values():
        called |= callees
    return [name for name in cg.methods if name not in called]
