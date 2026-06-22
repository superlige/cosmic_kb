"""阶段 7 · 落库判定（事件相位 × 操作类型 × 调用链到 sink）。

用户 2026-06-17 拍板的判定规则：
  * 入库类操作（save/submit/audit…）的事务内事件（beforeExecuteOperationTransaction/
    begin/endOperationTransaction）里 setValue → **直接落库**；
  * donothing 类操作 → 必须**显式调用** SaveServiceHelper.save 等 sink 才落库；
  * 界面插件 propertyChanged 等内存相位事件的 setValue → 仅改内存，除非下游有显式 sink。
显式 sink 常藏在不可预测层级的调用链里 → 由 call_graph 给出「事件可达路径是否触达 sink」。

处处置信度：判不准（操作类型未知、调用链出本类）一律给 unknown + 证据，绝不臆造。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import ast_index as ax

if TYPE_CHECKING:
    from tree_sitter import Node

# 落库 sink：显式持久化/事务调用。按「接收者类名 + 方法名」识别。
_SINK_RULES: list[tuple[str, set[str], str]] = [
    ("SaveServiceHelper", {"save", "update", "saveOperate"}, "SaveServiceHelper 保存"),
    ("OperationServiceHelper", {"executeOperate", "execOperate"}, "OperationServiceHelper 执行操作"),
    ("BusinessDataServiceHelper", {"save"}, "BusinessDataServiceHelper 保存"),
    ("DeleteServiceHelper", {"delete"}, "DeleteServiceHelper 删除"),
    ("DB", {"execute", "update", "executeBatch", "insert"}, "DB 直写"),
]
# getView().invokeOperation("save"/...) / invokeOperationWithSelect：触发平台入库操作。
_INVOKE_OP_RE = re.compile(r'invokeOperation\w*\(\s*"([^"]+)"')
_PERSIST_INVOKE_OPS = {"save", "submit", "audit", "unaudit", "unsubmit", "delete", "push"}

# 入库类操作类型（OperationType）：事务内事件改字段会随事务落库。
_PERSISTING_OP_TYPES = {
    "save", "submit", "unsubmit", "audit", "unaudit", "delete",
    "push", "disable", "enable", "drawbill",
}
# 明确不落库的操作类型。
_NONPERSIST_OP_TYPES = {"donothing"}


@dataclass
class Sink:
    kind: str
    line: int


def find_sinks(method_body: "Node | None") -> list[Sink]:
    """在一个方法体内找显式落库 sink。"""
    if method_body is None:
        return []
    out: list[Sink] = []
    for inv in ax.iter_invocations(method_body):
        recv = inv.object_text.strip()
        recv_tail = recv.rsplit(".", 1)[-1].split("(", 1)[0]
        for cls, names, desc in _SINK_RULES:
            if recv_tail == cls and inv.name in names:
                out.append(Sink(kind=desc, line=inv.line))
                break
        else:
            if inv.name.startswith("invokeOperation"):
                m = _INVOKE_OP_RE.search(ax._text(inv.node))
                if m and m.group(1) in _PERSIST_INVOKE_OPS:
                    out.append(Sink(kind=f"invokeOperation(\"{m.group(1)}\")", line=inv.line))
    return out


@dataclass
class Verdict:
    persists: str                  # yes | no | unknown
    reason: str
    confidence: float


def verdict(event_phase: str, op_type: str | None, sink_reachable: bool,
            *, has_external: bool = False) -> Verdict:
    """据事件相位 × 操作类型 × 是否可达 sink 给落库结论。"""
    # 1) 调用链可达显式 sink：落库（无论相位）。
    if sink_reachable:
        return Verdict("yes", "事件可达路径触达显式落库 sink（save/executeOperate/…）", 0.85)

    # 2) 事务相位（操作/反写插件）：取决于操作类型。
    if event_phase == "transaction":
        if op_type in _PERSISTING_OP_TYPES:
            return Verdict("yes", f"事务内事件 + 入库类操作（{op_type}），setValue 随事务落库", 0.9)
        if op_type in _NONPERSIST_OP_TYPES:
            v = Verdict("no", f"donothing 类操作（{op_type}），需显式 save 才落库；本类未见 sink", 0.7)
        elif op_type:
            v = Verdict("unknown", f"事务内事件，但操作类型 {op_type} 非标准入库类，落库存疑", 0.4)
        else:
            v = Verdict("unknown", "事务内事件，但未能确定所绑操作类型，落库存疑", 0.4)
        if has_external:
            return Verdict("unknown", v.reason + "；且调用链出本类（sink 可能在跨类工具函数）", 0.3)
        return v

    # 3) 构建/下推相位（转换插件 afterConvert）：写目标单据包，随目标保存落库（条件落库）。
    if event_phase == "build":
        return Verdict("yes", "下推/构建写目标单据数据包，随目标单据保存落库（条件落库）", 0.6)

    # 4) 内存/校验/无相位（界面插件等）：仅改内存，需保存操作触发才落库。
    base = {
        "memory": ("no", "界面内存相位事件，setValue 仅改内存模型，需保存操作触发才落库", 0.7),
        "validate": ("no", "校验相位，通常只读不写库", 0.6),
        "none": ("no", "无落库语义的事件", 0.6),
    }.get(event_phase, ("unknown", f"未知事件相位 {event_phase}，落库存疑", 0.3))
    if has_external:
        return Verdict("unknown", base[1] + "；但调用链出本类（落库可能在跨类工具函数）", 0.3)
    return Verdict(*base)
