"""发现"代码库里实际引用到、但本地元数据没有"的候选原厂（vendor）实体 key。

苍穹原厂实体页面非常多，不可能全量摄取；本工具是二开故障排查用途，只应该拉取代码库
真正访问到的那部分。用户拍板（2026-07-03，替代 2026-07-02 的"只列候选不自动拉取"）：
放弃"字符串字面量形状过滤"的粗糙候选（噪声太大、命中即字段名/map key/操作编码常量），
改用**三类确定性信号**，命中即"必定摄取"：

    ① `from_extensions` —— 本地元数据存在 `<isv>_<fnumber>_ext` 扩展（`InheritPath` 非空 +
       命名匹配，见 `metadata/extension.py::detect_extension`）→ 母体 fnumber 必摄取。
    ② `from_orm_calls` —— 插件经 `BusinessDataServiceHelper` / `QueryServiceHelper` 查询
       非本地实体：按接收者+方法名重载签名，只取 entityName **参数位**的值（字面量直取，
       常量引用经 `ConstantTable` 解析），不做"扫全文取首个字面量"。
    ③ `from_operation_calls` —— 插件对非本地实体执行操作（`OperationServiceHelper
       .executeOperate` / `DeleteServiceHelper.delete` / `SaveServiceHelper.save` 系列）。
       `save(var)` 仅在**同一方法体内**回溯 `var` 的 ORM 初始化来源，解不出诚实跳过
       （不做跨方法/全程序数据流猜测）。

歧义/取不出参数位一律跳过，绝不回退到"扫全文取首个字面量"——宁可漏候选，不可混进
字段名/map key/操作编码/排序子句这类噪声（红线 #4：宁 unknown 不臆造）。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from tree_sitter import Node

    from ..ingest.scanner import ScanResult
    from ..java.constants import ConstantTable
    from ..java import ast_index as ax
    from ..metadata.model import MetaModel

_KEY_SHAPE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")

# 苍穹平台**物理库**命名规约：字段物理列名 `fk_<fieldkey>`（基础资料/引用型字段的外键列）、
# 表物理表名 `tk_<entitykey...>`，二者形状和"实体 fnumber"完全一样（都是小写下划线），
# 但语义是物理列/表名，不是实体标识——这是平台级保留前缀，不是项目自家 ISV 前缀，
# 与 isv_prefixes 分开处理。
_PHYSICAL_SCHEMA_PREFIX_RE = re.compile(r"^(?:fk|tk)_")

_EVIDENCE_CAP = 5  # 每个候选最多留几条证据行号（报告可读性，红线 #4 已足够摆证据）


@dataclass
class SignalHit:
    """一个候选 key 在某一信号下的命中：计数 + 证据行号（relpath:line，封顶 `_EVIDENCE_CAP`）。"""

    count: int = 0
    evidence: list[str] = field(default_factory=list)

    def add(self, relpath: str, line: int) -> None:
        self.count += 1
        if len(self.evidence) < _EVIDENCE_CAP:
            self.evidence.append(f"{relpath}:{line}")


@dataclass
class VendorCandidate:
    """一个必摄取候选 key，附三信号命中详情。"""

    key: str
    ext_source: str | None = None   # 命中信号①的本地扩展模型 key（如 cqkd_bd_customer_ext）
    orm_hits: int = 0
    op_hits: int = 0
    evidence: list[str] = field(default_factory=list)

    @property
    def sources(self) -> list[str]:
        out: list[str] = []
        if self.ext_source:
            out.append("ext")
        if self.orm_hits:
            out.append("orm")
        if self.op_hits:
            out.append("op")
        return out

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "ext_source": self.ext_source,
            "orm_hits": self.orm_hits,
            "op_hits": self.op_hits,
            "sources": self.sources,
            "evidence": self.evidence,
        }


def known_keys_from_models(models: Iterable["MetaModel"]) -> set[str]:
    """本地已知 key 集合（表单 key + 实体 key + **字段 key**），供三信号过滤链排除用。"""
    keys: set[str] = set()
    for m in models:
        if m.key:
            keys.add(m.key)
        for e in m.entities:
            if e.key:
                keys.add(e.key)
        for f in m.fields:
            if f.key:
                keys.add(f.key)
    return keys


def known_keys_from_db(db_path: str | Path) -> set[str]:
    """已建好 KB 的已知 key 全集：form + entity + field 三表并集。

    KB 一旦建过，就是本项目"已知所有 key"的权威来源，不必再靠 `--meta` 重新解析一遍元数据。
    """
    conn = sqlite3.connect(str(db_path))
    try:
        keys: set[str] = set()
        for table in ("form", "entity", "field"):
            try:
                rows = conn.execute(f"SELECT DISTINCT key FROM {table} WHERE key IS NOT NULL").fetchall()
            except sqlite3.OperationalError:
                continue  # 旧 schema / 精简测试库可能没建这张表，跳过而非报错
            keys.update(r[0] for r in rows if r[0])
        return keys
    finally:
        conn.close()


def isv_prefixes_from_db(db_path: str | Path) -> dict[str, int]:
    """已建好 KB 的 ISV 前缀分布（按 form.key 的 `xxx_` 前缀计数），`--meta` 未给时的兜底。

    逻辑同 `bridge.namespace.discover_meta_prefixes`，只是数据源换成 KB 而非现算的
    MetaModel 列表——两者殊途同归（KB 本就是那批模型 build 出来的）。
    """
    from collections import Counter

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT key FROM form WHERE key IS NOT NULL").fetchall()
    finally:
        conn.close()
    counter: Counter[str] = Counter()
    for (key,) in rows:
        if "_" in key:
            counter[key.split("_", 1)[0] + "_"] += 1
        else:
            counter["(none)"] += 1
    return dict(counter.most_common())


def _passes_filter(value: str | None, known_keys: set[str], isv_prefixes: tuple[str, ...]) -> bool:
    if not value or value in known_keys:
        return False
    if not _KEY_SHAPE_RE.match(value):
        return False
    if value.startswith(isv_prefixes):
        return False
    if _PHYSICAL_SCHEMA_PREFIX_RE.match(value):
        return False
    return True


# ── 信号① 扩展母体 ────────────────────────────────────────────────

def from_extensions(models: Iterable["MetaModel"], isv_prefixes: Iterable[str]) -> dict[str, str]:
    """本地扩展模型必定扩展了某原厂 fnumber → 该 fnumber 必摄取，来源记扩展模型自身 key。"""
    from ..metadata.extension import detect_extension

    out: dict[str, str] = {}
    for m in models:
        candidate = detect_extension(m, isv_prefixes)
        if candidate:
            out[candidate] = m.key or "?"
    return out


# ── 信号② ORM 查询 ────────────────────────────────────────────────

def _arg_value(inv: "ax.Invocation", idx: int, const_table: "ConstantTable") -> str | None:
    kr = const_table.resolve_arg(inv, idx)
    return kr.value if kr else None


def _resolve_orm_call_entity(inv: "ax.Invocation", const_table: "ConstantTable") -> str | None:
    """按 BusinessDataServiceHelper/QueryServiceHelper 重载签名定位 entityName 参数位。

    取不准的重载（如 load 的 pks+type 重载、algoKey 与 entity 二义未消歧）一律返回 None
    ——诚实跳过，不猜。
    """
    recv_tail = inv.object_text.strip().rsplit(".", 1)[-1].split("(", 1)[0]
    name = inv.name

    if recv_tail == "BusinessDataServiceHelper":
        if name in ("load", "loadFromCache"):
            if inv.arg_count >= 1 and inv.args[0].type == "string_literal":
                return _arg_value(inv, 0, const_table)
            return None  # arg0 非字符串：pks+type 重载，跳过
        if name in ("loadSingle", "loadSingleFromCache"):
            if inv.arg_count == 2:
                return _arg_value(inv, 1, const_table)
            if inv.arg_count == 3:
                if inv.args[2].type == "string_literal":
                    return _arg_value(inv, 1, const_table)   # (pk, entity, fields)
                return _arg_value(inv, 0, const_table)        # (entity, fields, QFilter[])
            return None
        if name == "newDynamicObject":
            return _arg_value(inv, 0, const_table)
        return None

    if recv_tail == "QueryServiceHelper" and name in ("query", "queryOne", "exists"):
        v0 = _arg_value(inv, 0, const_table)
        if v0 and "." in v0:
            return _arg_value(inv, 1, const_table)   # algoKey 重载（arg0 形如 kd.xxx）
        return v0

    return None


def from_orm_calls(
    scan_result: "ScanResult",
    known_keys: set[str],
    isv_prefixes: Iterable[str],
    const_table: "ConstantTable",
) -> dict[str, SignalHit]:
    """全项目扫 ORM 查询调用，按重载参数位规则表解析 entityName，过滤链把关。"""
    from ..java import ast_index as ax_mod

    prefixes = tuple(p for p in isv_prefixes if p and p != "(none)")
    hits: dict[str, SignalHit] = {}
    for sf in scan_result.ok_files:
        if not sf.text or not sf.relpath.lower().endswith(".java"):
            continue
        root = ax_mod.parse_tree(sf.text)
        if root is None:
            continue
        for inv in ax_mod.iter_invocations(root):
            entity = _resolve_orm_call_entity(inv, const_table)
            if _passes_filter(entity, known_keys, prefixes):
                hits.setdefault(entity, SignalHit()).add(sf.relpath, inv.line)
    return hits


# ── 信号③ 操作执行 ────────────────────────────────────────────────

def _as_invocation(node: "Node | None") -> "ax.Invocation | None":
    """把单个 method_invocation 节点包成 Invocation（复用 iter_invocations 的字段抽取逻辑）。"""
    from ..java import ast_index as ax_mod

    if node is None or node.type != "method_invocation":
        return None
    return next(ax_mod.iter_invocations(node), None)


def _resolve_op_call_entity(
    inv: "ax.Invocation", const_table: "ConstantTable",
) -> tuple[str | None, str | None]:
    """操作执行类调用的实体解析。返回 `(entity, var_name)`：

    `var_name` 非 None 表示这是 `SaveServiceHelper.save(var)` 这类——实体需调用方在
    同方法体内回溯 `var` 的 ORM 来源，本函数只负责取出变量名。
    """
    recv_tail = inv.object_text.strip().rsplit(".", 1)[-1].split("(", 1)[0]
    name = inv.name

    if recv_tail == "OperationServiceHelper" and name in ("executeOperate", "execOperate"):
        return _arg_value(inv, 1, const_table), None
    if recv_tail == "DeleteServiceHelper" and name == "delete":
        return _arg_value(inv, 0, const_table), None
    if recv_tail == "SaveServiceHelper" and name in ("save", "update", "saveOperate"):
        from ..java import ast_index as ax_mod

        return None, ax_mod.arg_identifier(inv, 0)
    return None, None


def _local_orm_entities(
    body: "Node | None", const_table: "ConstantTable",
    known_keys: set[str], isv_prefixes: tuple[str, ...],
) -> dict[str, str]:
    """方法体内局部变量 → 其 ORM 初始化/赋值解析出的实体（供 save(var) 同方法回溯）。"""
    from ..java import ast_index as ax_mod

    out: dict[str, str] = {}
    for lv in ax_mod.iter_local_vars(body):
        inv = _as_invocation(lv.init)
        if inv is None:
            continue
        entity = _resolve_orm_call_entity(inv, const_table)
        if _passes_filter(entity, known_keys, isv_prefixes):
            out[lv.name] = entity
    for asn in ax_mod.iter_assignments(body):
        inv = _as_invocation(asn.value)
        if inv is None:
            continue
        entity = _resolve_orm_call_entity(inv, const_table)
        if _passes_filter(entity, known_keys, isv_prefixes):
            out[asn.name] = entity
    return out


def _scan_method_for_op_calls(
    body: "Node | None", relpath: str, const_table: "ConstantTable",
    known_keys: set[str], isv_prefixes: tuple[str, ...], hits: dict[str, SignalHit],
) -> None:
    from ..java import ast_index as ax_mod

    if body is None:
        return
    local_entities = _local_orm_entities(body, const_table, known_keys, isv_prefixes)
    for inv in ax_mod.iter_invocations(body):
        entity, var = _resolve_op_call_entity(inv, const_table)
        if entity is None and var is not None:
            entity = local_entities.get(var)
        if _passes_filter(entity, known_keys, isv_prefixes):
            hits.setdefault(entity, SignalHit()).add(relpath, inv.line)


def from_operation_calls(
    scan_result: "ScanResult",
    known_keys: set[str],
    isv_prefixes: Iterable[str],
    const_table: "ConstantTable",
) -> dict[str, SignalHit]:
    """全项目扫操作执行调用（executeOperate/delete/save），`save(var)` 限同方法体回溯。"""
    from ..java import ast_index as ax_mod

    prefixes = tuple(p for p in isv_prefixes if p and p != "(none)")
    hits: dict[str, SignalHit] = {}
    for sf in scan_result.ok_files:
        if not sf.text or not sf.relpath.lower().endswith(".java"):
            continue
        root = ax_mod.parse_tree(sf.text)
        if root is None:
            continue
        for type_decl in ax_mod.iter_type_declarations(root):
            for method in ax_mod.iter_methods(type_decl):
                _scan_method_for_op_calls(
                    method.body, sf.relpath, const_table, known_keys, prefixes, hits,
                )
    return hits


# ── 三路合并 ──────────────────────────────────────────────────────

def discover_candidates(
    *,
    models: Iterable["MetaModel"] | None = None,
    scan_result: "ScanResult | None" = None,
    db_path: "str | Path | None" = None,
    known_keys: set[str] | None = None,
    isv_prefixes: Iterable[str] = (),
) -> list[VendorCandidate]:
    """三类确定性信号合并；ext 优先，其次按 orm+op 命中数降序。

    `known_keys` 若与 `db_path` 同给，会自动并入 KB 的已知 key 全集（见
    `known_keys_from_db`）——`--meta`/`models` 不再是过滤生效的必要条件。
    """
    merged_known = set(known_keys or set())
    if db_path:
        merged_known |= known_keys_from_db(db_path)
    prefixes = tuple(p for p in isv_prefixes if p and p != "(none)")

    ext_hits: dict[str, str] = from_extensions(models, prefixes) if models is not None else {}

    orm_hits: dict[str, SignalHit] = {}
    op_hits: dict[str, SignalHit] = {}
    if scan_result is not None:
        from ..java.constants import build_constant_table

        const_table = build_constant_table(scan_result)
        orm_hits = from_orm_calls(scan_result, merged_known, prefixes, const_table)
        op_hits = from_operation_calls(scan_result, merged_known, prefixes, const_table)

    keys = set(ext_hits) | set(orm_hits) | set(op_hits)
    out: list[VendorCandidate] = []
    for k in keys:
        orm = orm_hits.get(k)
        op = op_hits.get(k)
        evidence = [*(orm.evidence if orm else []), *(op.evidence if op else [])]
        out.append(VendorCandidate(
            key=k,
            ext_source=ext_hits.get(k),
            orm_hits=orm.count if orm else 0,
            op_hits=op.count if op else 0,
            evidence=evidence[:_EVIDENCE_CAP],
        ))
    out.sort(key=lambda c: (c.ext_source is None, -(c.orm_hits + c.op_hits), c.key))
    return out
