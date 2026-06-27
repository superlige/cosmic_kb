"""阶段 4 · Cosmic KB 灌库与重建（SQLite + FTS5）。

把阶段 1-3 的三类内存产物（ScanResult / MetaModel / BridgeResult）+ 阶段 4 的模块识别
结果，一次性沉淀进 SQLite 图谱。KB 是段一与段二之间的契约（见 CLAUDE.md）。

设计要点（守红线）：
  * **幂等重建**：`build_kb` 在单事务里先 DROP 全部对象、再按 `schema.sql` 重建、再灌库——
    KB 任意时刻可从零重建（硬约束 #10「增量与可重建」）。
  * **只用标准库 sqlite3**：不引 ORM、不加运行期硬依赖（沿用 pyproject「零硬依赖」基调）。
  * **节点 + 通用 edge 表**：阶段 8 业务流边复用同一张 edge 表。
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from ..bridge import namespace

if TYPE_CHECKING:
    from ..bridge.linker import BridgeResult
    from ..bridge.namespace import SourceIndex
    from ..ingest.scanner import ScanResult
    from ..metadata.model import MetaModel

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
KB_SCHEMA_VERSION = "10"

# DROP 顺序（FTS 虚拟表与各表；DROP TABLE 对 search 同样有效，会连带清掉 FTS 影子表）。
_OBJECTS = [
    "search", "edge", "coarse_field_hit", "field_access", "plugin_method", "operation",
    "binding", "source_class", "convert_rule",
    "plugin", "field", "entity", "form", "module", "kb_meta",
]


def _field_uid(form_key: str | None, f: Any) -> str:
    """字段稳定唯一键：form + 实体 + (key 或 id)。单用 key 不唯一（见 model.py）。"""
    return f"{form_key}::{f.entity_key}::{f.key or f.id}"


def _plugin_uid(form_key: str | None, p: Any) -> str:
    return f"{form_key}::{p.class_name}::{p.plugin_type}"


def _connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def build_kb(
    scan_result: "ScanResult",
    models: Iterable["MetaModel"],
    bridge_result: "BridgeResult",
    module_map: dict[str, Any],
    db_path: str | Path,
    *,
    index: "SourceIndex | None" = None,
    source_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """幂等重建 KB 并灌库。返回各表计数摘要。

    index：可复用 build 流程已建好的源码索引（None 则自建），避免重复解析。
    source_args：记入 kb_meta 的来源信息（源码根 / 元数据输入 / 时间戳），供 report 判过期。
    """
    if index is None:
        index = namespace.build_index(scan_result)
    models = list(models)
    form_module: dict[str, str] = module_map["form_module"]
    class_module: dict[str, str] = module_map["class_module"]
    orphan_role = {o.fqn: o.role for o in bridge_result.orphans}
    orphan_base = {o.fqn: o.plugin_base for o in bridge_result.orphans}
    orphan_set = set(orphan_role)

    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn = _connect(db_path)
    try:
        with conn:  # 单事务：要么整体重建成功，要么回滚（幂等、不留半成品）
            for obj in _OBJECTS:
                conn.execute(f"DROP TABLE IF EXISTS {obj}")
            conn.executescript(schema_sql)
            counts = _populate(
                conn, scan_result, models, bridge_result, module_map,
                index, form_module, class_module, orphan_role, orphan_base, orphan_set,
            )
            # Java 字段级分析只跑一次：结果（含常量表）同时喂给高精度落库与粗扫侧。
            from ..java import analyze as java_analyze
            res = java_analyze.analyze(scan_result, models, bridge_result, index)
            counts.update(_populate_java(conn, res))
            counts.update(_populate_coarse(conn, scan_result, models, res))
            _write_meta(conn, counts, bridge_result, module_map, source_args)
        return counts
    finally:
        conn.close()


def _populate(
    conn, scan_result, models, bridge_result, module_map,
    index, form_module, class_module, orphan_role, orphan_base, orphan_set,
) -> dict[str, Any]:
    # ── module ────────────────────────────────────────────────────────────
    conn.executemany(
        "INSERT INTO module(name,app_key,dominant_package,pkg_consistency,form_count,"
        "entity_count,plugin_count,class_count,orphan_real_count,confidence,evidence) "
        "VALUES(:name,:app_key,:dominant_package,:pkg_consistency,:form_count,"
        ":entity_count,:plugin_count,:class_count,:orphan_real_count,:confidence,:evidence)",
        [{k: m.get(k) for k in (
            "name", "app_key", "dominant_package", "pkg_consistency", "form_count",
            "entity_count", "plugin_count", "class_count", "orphan_real_count",
            "confidence", "evidence")} for m in module_map["modules"]],
    )

    # ── form / entity / field / plugin / convert_rule + edges + FTS ────────
    forms, entities, fields, plugins, edges, search = [], [], [], [], [], []
    convert_rules: list = []
    for m in models:
        mod = form_module.get(m.key) if m.key else None

        # 转换规则：不是表单（无实体/字段），单独入 convert_rule 表 + converts_to 边；
        # 其转换插件仍照常入 plugin 表/bound_to 边（与界面/操作插件同等桥接）。
        if m.form_type == "convert":
            c = m.convert
            convert_rules.append((
                m.key, m.name,
                c.source_entity if c else None, c.target_entity if c else None,
                c.source_entry if c else None, c.target_entry if c else None,
                m.isv, m.app_key, mod,
                c.field_map_count if c else 0, len(m.plugins),
                (1 if c.enabled else 0) if c and c.enabled is not None else None,
                m.source_file,
            ))
            if c and c.source_entity and c.target_entity:
                edges.append((
                    "form", c.source_entity, "form", c.target_entity,
                    "converts_to", 1.0, m.name or m.key,
                ))
            for p in m.plugins:
                uid = _plugin_uid(m.key, p)
                plugins.append((uid, m.key, p.class_name, p.plugin_type, p.source,
                                p.operation_key, p.operation_name))
                if p.class_name:
                    edges.append(("plugin", uid, "class", p.class_name, "bound_to", 1.0, None))
                    search.append(("plugin", p.class_name, p.operation_name or "", m.key or ""))
            continue

        forms.append((m.key, m.name, m.form_type, m.model_type, m.isv,
                      m.app_key, mod, m.source_file))
        if m.key:
            search.append(("form", m.key, m.name or "", mod or ""))
            if mod:
                edges.append(("module", mod, "form", m.key, "module_contains", 1.0, None))

        # parent_id 是父实体的 **oid**（如 1B+5Q7IXAJGI），但 entity.parent_key 列语义是
        # 「父实体 key」。用本单据自己的实体 oid→key 映射把 oid 翻成 key（解不出留 None，
        # 不向消费者泄漏无意义的 oid）；表头 parent_id 为 None，照常留 None。
        id_to_key = {e.id: e.key for e in m.entities if e.id}
        for e in m.entities:
            parent_key = id_to_key.get(e.parent_id) if e.parent_id else None
            entities.append((m.key, e.key, e.name, e.level, parent_key, e.table_name))
            eid = f"{m.key}::{e.key}"
            edges.append(("form", m.key, "entity", eid, "has_entity", 1.0, None))
            search.append(("entity", e.key or "", e.name or "", m.key or ""))

        for f in m.fields:
            uid = _field_uid(m.key, f)
            fields.append((uid, m.key, f.entity_key, f.key, f.name,
                           f.db_column, f.field_type, f.kind, f.level))
            edges.append(("form", m.key, "field", uid, "has_field", 1.0, None))
            search.append(("field", f.key or "", f.name or "", m.key or ""))

        for p in m.plugins:
            uid = _plugin_uid(m.key, p)
            plugins.append((uid, m.key, p.class_name, p.plugin_type, p.source,
                            p.operation_key, p.operation_name))
            edges.append(("form", m.key, "plugin", uid, "has_plugin", 1.0, None))
            if p.class_name:
                edges.append(("plugin", uid, "class", p.class_name, "bound_to", 1.0, None))
                search.append(("plugin", p.class_name, p.operation_name or "", m.key or ""))

    conn.executemany("INSERT INTO form VALUES(?,?,?,?,?,?,?,?)", forms)
    conn.executemany("INSERT INTO entity VALUES(?,?,?,?,?,?)", entities)
    conn.executemany("INSERT INTO field VALUES(?,?,?,?,?,?,?,?,?)", fields)
    conn.executemany("INSERT INTO plugin VALUES(?,?,?,?,?,?,?)", plugins)
    conn.executemany("INSERT INTO convert_rule VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", convert_rules)

    # ── operation（操作集；has_plugin 标是否有自定义操作插件，落库判定/排查入口用）──
    operations = []
    for m in models:
        op_keys_with_plugin = {
            p.operation_key for p in m.plugins
            if p.plugin_type == "op" and p.operation_key
        }
        for op in m.operations:
            operations.append((
                m.key, op.key, op.name, op.operation_type, op.resolved_from,
                1 if op.key in op_keys_with_plugin else 0,
            ))
    conn.executemany("INSERT INTO operation VALUES(?,?,?,?,?,?)", operations)

    # ── source_class（全部源码顶层类型；标孤儿与归属模块）─────────────────
    classes = []
    for u in index.units:
        for fqn, simple in zip(u.all_fqns, u.all_types):
            is_orphan = 1 if fqn in orphan_set else 0
            role = orphan_role.get(fqn)
            base = orphan_base.get(fqn)
            mod = class_module.get(fqn)
            classes.append((fqn, simple, u.package, u.relpath, mod, is_orphan, role, base))
            search.append(("class", fqn, simple, mod or ""))
            if mod:
                edges.append(("module", mod, "class", fqn, "module_contains", 1.0, None))
    conn.executemany("INSERT INTO source_class VALUES(?,?,?,?,?,?,?,?)", classes)

    # ── binding（桥接三态）─────────────────────────────────────────────────
    bindings = [
        (b.class_name, b.form_key, b.plugin_type, b.status,
         b.source_relpath, b.confidence, b.note)
        for b in bridge_result.bindings
    ]
    conn.executemany("INSERT INTO binding VALUES(?,?,?,?,?,?,?)", bindings)

    # ── edge / search 批量写 ───────────────────────────────────────────────
    conn.executemany("INSERT INTO edge VALUES(?,?,?,?,?,?,?)", edges)
    conn.executemany("INSERT INTO search VALUES(?,?,?,?)", search)

    return {
        "module": len(module_map["modules"]),
        "form": len(forms),
        "entity": len(entities),
        "field": len(fields),
        "plugin": len(plugins),
        "convert_rule": len(convert_rules),
        "operation": len(operations),
        "source_class": len(classes),
        "binding": len(bindings),
        "edge": len(edges),
        "search": len(search),
    }


def _populate_java(conn, res) -> dict[str, Any]:
    """阶段5+6+7：把 Java 字段级分析结果 → plugin_method / field_access 表。

    res 由 rebuild 统一跑一次（与粗扫侧共享常量表，避免重复解析全工程 Java）。
    tree-sitter 未装时 res.available=False，两表为空（计数 0）；由报告层据 kb_meta 的
    java_available 如实提示用户装 [parse] extra，不臆造。
    """
    import json as _json

    conn.executemany(
        "INSERT INTO plugin_method VALUES(?,?,?,?,?,?,?)",
        [(pm.plugin_fqn, pm.method_name, pm.event_kind, pm.event_phase,
          pm.start_line, pm.end_line, pm.source_relpath) for pm in res.plugin_methods],
    )
    conn.executemany(
        "INSERT INTO field_access VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r.form_key, r.field_key, r.level, r.entry_key, r.plugin_fqn, r.plugin_type,
          r.access_class, r.event_method, r.event_phase, r.access, r.persists,
          r.persist_reason, r.via, r.line, _json.dumps(r.path, ensure_ascii=False),
          r.key_resolution, r.confidence, r.source_relpath, r.evidence, r.form_key_source)
         for r in res.field_accesses],
    )
    # field_access 也进 FTS（按字段 key / 类名搜得到）。
    conn.executemany(
        "INSERT INTO search VALUES(?,?,?,?)",
        [("field_access", r.field_key or "", r.plugin_fqn, r.form_key or "")
         for r in res.field_accesses if r.field_key],
    )
    conn.execute(
        "INSERT INTO kb_meta(key,value) VALUES('java_analysis',?)",
        (_json.dumps({
            "available": res.available,
            "analyzed_plugins": res.analyzed_plugin_count,
            "standalone_classes": res.standalone_class_count,
            "field_access": len(res.field_accesses),
            "plugin_method": len(res.plugin_methods),
        }, ensure_ascii=False),),
    )
    return {"plugin_method": len(res.plugin_methods), "field_access": len(res.field_accesses)}


# 业务字段标识（字面量或常量名）作 get/set/getValue/setValue 首参 → 更强的读写信号。
# 允许首参与调用之间夹常量类限定（`getValue(BillConst.AMOUNT)`）：可选的 `限定.` 链。
_RW_IDIOM_RE = re.compile(
    r'(?:getValue|setValue|\.get|\.set)\s*\(\s*(?:[A-Za-z_$][\w$]*\s*\.\s*)*$')
# Java 标识符字符集（含 $，苍穹生成类常见）。
_IDENT_START = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$")
_IDENT_PART = _IDENT_START | frozenset("0123456789")


def _scan_java_tokens(text: str):
    """单遍扫 Java 源码，**跳过注释与字符串/字符内部**，产出代码区的 token。

    yield (kind, value, start)：
      * ('str',   字符串字面量内容, 开引号偏移)
      * ('ident', 标识符,           起始偏移)
    纯 Python 状态机、不依赖 tree-sitter —— 保证 [parse] 未装时粗扫仍可用；注释里的
    字符串/标识符一律不产出，根治「注释被当成代码命中」（信任手段二验收硬伤之一）。
    """
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":          # 行注释
            i += 2
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":        # 块注释
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        elif ch == '"':                                              # 字符串字面量
            start = i
            i += 1
            buf: list[str] = []
            while i < n and text[i] not in ('"', "\n"):
                if text[i] == "\\" and i + 1 < n:                    # 转义对，整体跳过
                    buf.append(text[i + 1])
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            if i < n and text[i] == '"':
                i += 1
            yield ("str", "".join(buf), start)
        elif ch == "'":                                             # 字符字面量（仅跳过）
            i += 1
            while i < n and text[i] not in ("'", "\n"):
                i += 2 if text[i] == "\\" else 1
            if i < n and text[i] == "'":
                i += 1
        elif ch in _IDENT_START:                                    # 标识符
            start = i
            i += 1
            while i < n and text[i] in _IDENT_PART:
                i += 1
            yield ("ident", text[start:i], start)
        else:
            i += 1


def _populate_coarse(conn, scan_result, models, res) -> dict[str, Any]:
    """信任「手段二」粗扫侧：用一个**独立、笨、难出错**的基线扫，交叉验证高精度会不会漏字段。

    扫两类引用（都不解析坐标/落库/调用链，保持与高精度 field_access 的独立性）：
      * 业务字段标识的**字符串字面量**（`getValue("cqkd_x")`、常量类里的 `"cqkd_x"`）；
      * 解析回某业务字段的**常量名引用**（`getValue(BillConst.AMOUNT)`）—— 真实苍穹项目字段
        标识多为常量引用，只扫字面量会漏掉最该核对的强信号盲点，故复用 `res.const_table`
        （高精度已建好的全工程常量值表）把**唯一映射**的常量名也算召回。
    用单遍 Java 词法扫描跳过注释，根治「注释被当成代码命中」。只收业务字段
    （entity/dynamic/basedata_prop），与覆盖率分母口径一致。

    via 四态：`rw-idiom`/`literal`（字面量作不作 get/set 首参）、`const-rw-idiom`/`const-ref`
    （常量名作不作 get/set 首参）；强弱信号由 report/scan_compare.py 据此分桶。
    """
    import bisect

    from ..report.scan_compare import BUSINESS_KINDS  # 延迟导入避免 report→store 环

    biz_keys = {
        f.key
        for m in models if m.form_type != "convert"
        for f in m.fields
        if f.key and f.kind in BUSINESS_KINDS
    }
    if not biz_keys:
        return {"coarse_field_hit": 0}

    # 常量名 → 业务 key：仅取唯一映射（同名常量有多个不同字面值=歧义，不归，避免误报）。
    const_to_key: dict[str, str] = {}
    const_table = getattr(res, "const_table", None)
    if const_table is not None:
        for name, lits in const_table.by_field.items():
            if len(lits) == 1:
                lit = next(iter(lits))
                if lit in biz_keys:
                    const_to_key[name] = lit

    seen: set[tuple[str, str, int]] = set()   # (field_key, relpath, line) 去重
    rows: list[tuple[str, str, int, str]] = []
    for sf in scan_result.ok_files:
        text = sf.text
        if not text or not sf.relpath.lower().endswith(".java"):
            continue
        # 行起始偏移表，供 O(log n) 定位行号（避免每个命中都 count 一遍）。
        line_starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                line_starts.append(i + 1)
        for kind, value, start in _scan_java_tokens(text):
            if kind == "str":
                key = value if value in biz_keys else None
                strong, weak = "rw-idiom", "literal"
            else:  # ident → 常量名反查
                key = const_to_key.get(value)
                strong, weak = "const-rw-idiom", "const-ref"
            if key is None:
                continue
            line = bisect.bisect_right(line_starts, start)
            dedup = (key, sf.relpath, line)
            if dedup in seen:
                continue
            seen.add(dedup)
            pre = text[max(0, start - 24):start]
            via = strong if _RW_IDIOM_RE.search(pre) else weak
            rows.append((key, sf.relpath, line, via))

    conn.executemany("INSERT INTO coarse_field_hit VALUES(?,?,?,?)", rows)
    return {"coarse_field_hit": len(rows)}


def _write_meta(conn, counts, bridge_result, module_map, source_args) -> None:
    meta = {
        "schema_version": KB_SCHEMA_VERSION,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": _json(counts),
        "code_prefixes": _json(bridge_result.code_prefixes),
        "meta_prefixes": _json(bridge_result.meta_prefixes),
        "health": _json(module_map["health"]),
    }
    if source_args:
        meta["source_args"] = _json(source_args)
    conn.executemany(
        "INSERT INTO kb_meta(key,value) VALUES(?,?)", list(meta.items())
    )


def _json(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


# ── 读取侧（供报告层与阶段 9/10 复用）──────────────────────────────────────

def open_kb(db_path: str | Path) -> sqlite3.Connection:
    """以只读友好的方式打开 KB；行可按列名访问。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kb_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def kb_exists(db_path: str | Path) -> bool:
    """db 文件存在且 schema 版本匹配（不匹配视为需重建）。"""
    p = Path(db_path)
    if not p.is_file():
        return False
    try:
        conn = open_kb(p)
        try:
            return get_meta(conn, "schema_version") == KB_SCHEMA_VERSION
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def search(conn: sqlite3.Connection, query: str, *, limit: int = 50) -> list[sqlite3.Row]:
    """FTS5 全文检索（中文名↔标识 / 字段 / 类名）。为阶段 9 NL 查询预留。"""
    return conn.execute(
        "SELECT kind,key,name,extra FROM search WHERE search MATCH ? LIMIT ?",
        (query, limit),
    ).fetchall()
