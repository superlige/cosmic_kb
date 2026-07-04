"""阶段 5 · 全局常量值表（字段标识解析）。

真实苍穹项目里字段标识常**不是裸字符串**，而是常量引用：`setValue(BillConst.AMOUNT, …)`
或 `setValue(KEY_AMOUNT, …)`，且多开发各建各的常量类（用户 2026-06-17 反馈）。只认字面量
会漏一大片，故先扫全工程所有项目 Java 的 `static final String`（含接口常量），建
「常量名 / 类.常量名 → 字面值」表，供 `field_access` 把常量引用解析回字段 key。

处处置信度：字面量直解=1.0；`类.常量`精确命中=0.95；裸常量名唯一命中=0.85；同名常量
多个不同字面值=ambiguous(标 unknown 留证据)；查不到=unknown。

`records`（2026-07-03 补）：本表原只在建库这一次性进程内存活（供 field_access/粗扫复用），
查询期（`read_source` 读源码）拿不到——遇到 `TemporaryStopCon.ENTITY` 这类限定常量引用，
字面值 `cqkd_ltyz` 根本没出现在源码正文里，只能让大模型凭常量英文名去猜中文单据名（真实
翻车案例）。故把逐条定义（含源文件+行号）持久化进 KB 的 `java_constant` 表（见
`graph/schema.sql` / `graph/store.py:_populate_java_constants`），`read_source` 建库后
仍可查表把源码里的 `类.常量` 引用解析回字面值再标注中文名，见 `report/read_source.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import ast_index as ax

if TYPE_CHECKING:
    from tree_sitter import Node
    from ..ingest.scanner import ScanResult


@dataclass
class KeyResolution:
    """字段 key 实参的解析结果。"""

    value: str | None              # 解析出的字段 key 字面值；解不出为 None
    kind: str                      # literal | constant | ambiguous | unknown | dynamic
    confidence: float
    note: str | None = None


class ConstantTable:
    """全工程 `static final String` 常量值表。"""

    def __init__(self) -> None:
        # 类简单名 → {常量名: 字面值}（供 `类.常量` 精确解析）。
        self.by_class: dict[str, dict[str, str]] = {}
        # 常量名 → 出现过的字面值集合（供裸常量名/跨类解析与歧义判定）。
        self.by_field: dict[str, set[str]] = {}
        # 逐条定义记录（类, 常量名, 字面值, 源文件, 行号）：供持久化进 KB（java_constant 表），
        # 让查询期（read_source）也能把源码里的 `类.常量` 引用解析回字面值——不去重、
        # 同名类在不同文件重复定义时全都留证据，查询侧按此判「多处定义、字面值不同」的歧义。
        self.records: list[tuple[str | None, str, str, str | None, int | None]] = []

    def _add(
        self, cls: str | None, name: str, literal: str,
        relpath: str | None = None, line: int | None = None,
    ) -> None:
        if cls:
            self.by_class.setdefault(cls, {})[name] = literal
        self.by_field.setdefault(name, set()).add(literal)
        self.records.append((cls, name, literal, relpath, line))

    def resolve(self, expr: str) -> KeyResolution:
        """解析一个常量引用表达式（`F_AMT` / `Const.F_AMT` / `a.b.Const.F_AMT`）。"""
        parts = [p for p in expr.replace(" ", "").split(".") if p]
        if not parts:
            return KeyResolution(None, "unknown", 0.0)
        field = parts[-1]
        cls = parts[-2] if len(parts) >= 2 else None
        if cls and cls in self.by_class and field in self.by_class[cls]:
            return KeyResolution(self.by_class[cls][field], "constant", 0.95,
                                 note=f"{cls}.{field}")
        lits = self.by_field.get(field)
        if not lits:
            return KeyResolution(None, "unknown", 0.0,
                                 note=f"未知常量 {expr}（可能跨模块/外部常量）")
        if len(lits) == 1:
            return KeyResolution(next(iter(lits)), "constant", 0.85,
                                 note=f"按常量名唯一匹配 {field}")
        return KeyResolution(None, "ambiguous", 0.3,
                             note=f"常量名 {field} 有 {len(lits)} 个不同字面值，无法消歧")

    def resolve_arg(self, inv: ax.Invocation, idx: int) -> KeyResolution | None:
        """解析方法调用第 idx 个实参作为字段 key：字面量直解，否则查常量表。

        返回 None 表示该实参既非字符串也非标识符（如表达式/拼接）—— 调用方按需标 dynamic。
        """
        lit = ax.arg_string(inv, idx)
        if lit is not None:
            return KeyResolution(lit, "literal", 1.0)
        ident = ax.arg_identifier(inv, idx)
        if ident is not None:
            return self.resolve(ident)
        return None


def build_constant_table(scan_result: "ScanResult") -> ConstantTable:
    """扫全工程所有可读 Java，建常量值表（按类归属，便于 `类.常量` 精确解析）。"""
    table = ConstantTable()
    for sf in scan_result.ok_files:
        if not sf.relpath.lower().endswith(".java"):
            continue
        root = ax.parse_tree(sf.text)
        if root is None:
            continue
        _collect(root, table, sf.relpath)
    return table


def collect_into(root: "Node", table: ConstantTable, relpath: str | None = None) -> None:
    """把一棵已解析的语法树里的常量灌进现有表（供 project_graph 复用解析、避免重复解析）。"""
    _collect(root, table, relpath)


def _collect(root: "Node", table: ConstantTable, relpath: str | None = None) -> None:
    """DFS 跟踪最近的「enclosing 类型简单名」，把常量归属到它（支持嵌套常量类）。"""
    stack: list[tuple["Node", str | None]] = [(root, None)]
    while stack:
        node, enclosing = stack.pop()
        if node.type in ("constant_declaration", "field_declaration"):
            in_iface = node.type == "constant_declaration"
            for name, literal, line in ax._const_from_field(node, in_iface):
                table._add(enclosing, name, literal, relpath, line)
            continue
        nxt = enclosing
        if node.type in ax._TYPE_DECL:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                nxt = name_node.text.decode("utf-8", "replace")
        for c in node.children:
            stack.append((c, nxt))
