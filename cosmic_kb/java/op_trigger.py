"""隐藏坑 #1 · 程序化操作触发点提取（executeOperate / invokeOperation）。

`OperationServiceHelper.executeOperate("audit", "cqkd_b", ...)` 这类**代码触发**的操作，
设计器完全不展示，单据标识还常是常量引用——接手老项目时"这单没人碰操作怎么执行了"
只能全局搜源码硬拼（真实最高频隐藏坑）。本模块把这类调用点提取成结构化事实：
谁（类·方法·行号）触发了哪个单据的哪个操作。

只存**单跳原子事实**，不预拼多跳链（A→B→C 级联由段二 agent 递归查 bill/trace 拼出）：
预拼会遇到 unknown 传播/环/组合爆炸，违背「确定性事实 + 宿主推理」的两段式哲学。

识别两种调用（persistence.py 的 sink 口径 + dbmeta/discover.py 的实参位先例）：
  * `OperationServiceHelper.executeOperate(opKey, entityNumber, ...)` —— arg0=操作 key，
    arg1=目标单据（discover.py `_resolve_op_call_entity` 已证明的参数位）。
    苍穹平台没有 `execOperate` 这个方法（曾误当作同签名别名识别，2026-07-20 经用户核实后
    去掉，避免把项目里自定义同名方法误判成平台触发）。
  * `view.invokeOperation(opKey, ...)` —— 操作 key 在 arg0，**目标单据不在实参里**
    （触发的是当前视图绑定单据自己的操作），由调用方传入 enclosing 类的唯一绑定单据；
    绑多张/未绑定则标 unknown，不臆造。

处处置信度：实参字面量=1.0；常量引用走 `ConstantTable.resolve`（0.95/0.85/ambiguous）；
表达式/拼接标 dynamic；解不出一律 unknown 留证据。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import ast_index as ax

if TYPE_CHECKING:
    from tree_sitter import Node

    from .constants import ConstantTable, KeyResolution
    from .symbols import SymbolTable


@dataclass
class OperationTriggerRow:
    """一条程序化操作触发点事实（单跳：调用坐标 → 目标单据.操作）。"""

    caller_class: str              # 调用点所在类 FQN
    caller_method: str             # 调用点所在方法名
    line: int
    source_relpath: str
    via: str                       # executeOperate | invokeOperation
    op_key: str | None             # 操作 key（audit/submit/...）；解不出为 None
    op_key_resolution: str         # literal | constant | ambiguous | dynamic | unknown
    op_key_confidence: float
    target_form_key: str | None    # 目标单据标识；解不出为 None
    target_resolution: str         # literal | constant | binding | ambiguous | dynamic | unknown
    target_confidence: float
    receiver_source: str = "text"  # symbol | text（接收者识别精度）
    evidence: str | None = None


def _resolution(kr: "KeyResolution | None") -> tuple[str | None, str, float, str | None]:
    """把 ConstantTable.resolve_arg 的结果归一成 (值, 解析方式, 置信度, 备注)。

    resolve_arg 返回 None（实参是表达式/拼接）→ dynamic；返回 unknown/ambiguous 原样保留。
    """
    if kr is None:
        return None, "dynamic", 0.0, "实参为表达式/拼接，静态解不出"
    return kr.value, kr.kind, kr.confidence, kr.note


def find_operation_triggers(
    method_body: "Node | None",
    const: "ConstantTable",
    *,
    caller_class: str,
    caller_method: str,
    source_relpath: str,
    bound_form: str | None = None,
    bound_ambiguous: bool = False,
    symbols: "SymbolTable | None" = None,
) -> list[OperationTriggerRow]:
    """在一个方法体内找程序化操作触发点。

    `bound_form`：enclosing 类的**唯一**绑定单据（供 invokeOperation 定目标）；
    `bound_ambiguous`：绑定了多张单据（有绑定但钉不出唯一）→ 目标标 ambiguous 而非 unknown。
    """
    if method_body is None:
        return []
    out: list[OperationTriggerRow] = []
    for inv in ax.iter_invocations(method_body):
        recv_tail = inv.object_text.strip().rsplit(".", 1)[-1].split("(", 1)[0]
        site = (symbols.lookup(source_relpath, inv.line, inv.name, inv.col)
                if symbols is not None else None)
        resolved_site = site is not None and site.resolved

        helper_match = recv_tail == "OperationServiceHelper"
        receiver_source = "text"
        if resolved_site:
            helper_match = site.declaring == "kd.bos.servicehelper.operation.OperationServiceHelper"
            receiver_source = "symbol"
        if helper_match and inv.name == "executeOperate":
            op_val, op_kind, op_conf, op_note = _resolution(const.resolve_arg(inv, 0))
            tgt_val, tgt_kind, tgt_conf, tgt_note = _resolution(const.resolve_arg(inv, 1))
            notes = [n for n in (op_note, tgt_note) if n]
            out.append(OperationTriggerRow(
                caller_class=caller_class, caller_method=caller_method,
                line=inv.line, source_relpath=source_relpath, via=inv.name,
                op_key=op_val, op_key_resolution=op_kind, op_key_confidence=op_conf,
                target_form_key=tgt_val, target_resolution=tgt_kind,
                target_confidence=tgt_conf,
                receiver_source=receiver_source,
                evidence="; ".join(notes) if notes else None,
            ))
            continue

        if inv.name.startswith("invokeOperation") and inv.arg_count >= 1:
            # 符号成功时，项目内同名 invokeOperation 明确否决；平台 kd.bos.* 目标才采信。
            if resolved_site and not (site.declaring or "").startswith("kd.bos."):
                continue
            op_val, op_kind, op_conf, op_note = _resolution(const.resolve_arg(inv, 0))
            if bound_form:
                tgt_val, tgt_kind, tgt_conf = bound_form, "binding", 0.85
                tgt_note = "目标=本类绑定单据（invokeOperation 作用于当前视图）"
            elif bound_ambiguous:
                tgt_val, tgt_kind, tgt_conf = None, "ambiguous", 0.3
                tgt_note = "本类绑定多张单据，invokeOperation 目标无法钉唯一"
            else:
                tgt_val, tgt_kind, tgt_conf = None, "unknown", 0.0
                tgt_note = "本类无元数据绑定，invokeOperation 目标单据未知"
            notes = [n for n in (op_note, tgt_note) if n]
            out.append(OperationTriggerRow(
                caller_class=caller_class, caller_method=caller_method,
                line=inv.line, source_relpath=source_relpath, via="invokeOperation",
                op_key=op_val, op_key_resolution=op_kind, op_key_confidence=op_conf,
                target_form_key=tgt_val, target_resolution=tgt_kind,
                target_confidence=tgt_conf,
                receiver_source="symbol" if resolved_site else "text",
                evidence="; ".join(notes) if notes else None,
            ))
    return sorted(out, key=lambda row: (row.line, row.via))


def collect_triggers(pg, bound_entity: dict[str, set[str]]) -> list[OperationTriggerRow]:
    """全项目扫描采集触发点（独立于插件事件 BFS：定时任务/service 类里的调用也要扫到）。

    逐类逐**重载**方法体扫（`cg.method_decls` 全量，不漏同名重载）；invokeOperation 的
    目标单据取本类唯一绑定单据（`bound_entity` 由 analyze 第①轮建好）。
    """
    out: list[OperationTriggerRow] = []
    for fqn, node in pg.classes.items():
        ents = bound_entity.get(fqn) or set()
        bound_form = next(iter(ents)) if len(ents) == 1 else None
        for md in node.cg.method_decls:
            out.extend(find_operation_triggers(
                md.body, pg.const,
                caller_class=fqn, caller_method=md.name,
                source_relpath=node.relpath,
                bound_form=bound_form, bound_ambiguous=len(ents) > 1,
                symbols=getattr(pg, "symbols", None),
            ))
    return _dedup(out)


def _dedup(rows: list[OperationTriggerRow]) -> list[OperationTriggerRow]:
    """同文件同名类重复解析等极端脏数据下按 (类, 方法, 行, via) 收敛成一条。"""
    seen: set[tuple] = set()
    out: list[OperationTriggerRow] = []
    for r in rows:
        k = (r.caller_class, r.caller_method, r.line, r.via)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out
