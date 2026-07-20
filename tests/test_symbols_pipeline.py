"""阶段 12.2 · SymbolTable 注入 java/ 管线、方法引用与精度分级。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("tree_sitter_java")

from cosmic_kb.java import analyze as an
from cosmic_kb.java import ast_index as ax
from cosmic_kb.java import op_trigger
from cosmic_kb.java import persistence
from cosmic_kb.java import project_graph as pgmod
from cosmic_kb.java.constants import ConstantTable
from cosmic_kb.java.symbols import SymbolTable
from cosmic_kb.cli.main import build_parser


def _invocations(src: str, *, refs: bool = True) -> list[ax.Invocation]:
    root = ax.parse_tree(src)
    assert root is not None
    return list(ax.iter_invocations(root, include_refs=refs))


def _symbols(relpath: str, rows: list[tuple[ax.Invocation, str, str]]) -> SymbolTable:
    """rows=(invocation, declaring_fqn, target_kind)。"""
    table = SymbolTable()
    table.add_file_event({
        "event": "file", "relpath": relpath, "status": "ok",
        "sites": [
            {
                "line": inv.line, "col": inv.col, "name": inv.name, "kind": inv.kind,
                "argc": inv.arg_count, "resolution": "expr", "declaring": declaring,
                "signature": f"{declaring}.{inv.name}()", "static": True,
                "target_kind": target_kind,
            }
            for inv, declaring, target_kind in rows
        ],
    })
    return table


def _project(src: str, *, relpath: str = "P.java", symbols: SymbolTable | None = None):
    scan = SimpleNamespace(ok_files=[SimpleNamespace(relpath=relpath, text=src)])
    return pgmod.build_project_graph(scan, None, symbols=symbols)


def test_invocation_character_column_and_method_reference_switch():
    src = """package p;
class A {
  void run() { /*中文*/ target(); A::target; }
  void target() {}
}
"""
    default = _invocations(src, refs=False)
    assert [i.kind for i in default] == ["invocation"]

    all_sites = _invocations(src)
    assert [(i.name, i.kind, i.arg_count) for i in all_sites] == [
        ("target", "method_reference", None),
        ("target", "invocation", 0),
    ]
    line = src.splitlines()[2]
    for inv in all_sites:
        expected = line.index(inv.name, line.index("/*中文*/")) + 1
        if inv.kind == "method_reference":
            expected = line.rindex("target") + 1
        assert inv.col == expected


def test_symbol_resolves_expression_receiver_that_heuristic_cannot():
    src = """package p;
class Caller { void run() { factory().target(); } Object factory(){ return null; } }
class Target { void target() {} }
"""
    inv = next(i for i in _invocations(src) if i.name == "target")
    plain = _project(src)
    assert plain._resolve_target(plain.classes["p.Caller"], "run", inv) is None

    table = _symbols("P.java", [(inv, "p.Target", "project")])
    pg = _project(src, symbols=table)
    hit = pg._resolve_target(pg.classes["p.Caller"], "run", inv)
    assert hit == pgmod.ResolvedCall("p.Target", "target", "symbol")


def test_method_reference_enters_cross_class_reachability():
    src = """package p;
class Caller { void run() { ContractService::updateRlateAssets; } }
class ContractService { static void updateRlateAssets() {} }
"""
    inv = next(i for i in _invocations(src) if i.kind == "method_reference")
    table = _symbols("P.java", [(inv, "p.ContractService", "project")])
    pg = _project(src, symbols=table)
    reached = {(r.fqn, r.method) for r in pg.reachable("p.Caller", "run")}
    assert ("p.ContractService", "updateRlateAssets") in reached


def test_symbol_sink_accepts_static_import_and_method_reference_but_denies_project_homonym():
    src = """package p;
class Caller { void run() { save(); SaveServiceHelper::save; } }
"""
    invs = [i for i in _invocations(src) if i.name == "save"]
    platform = "kd.bos.servicehelper.operation.SaveServiceHelper"
    table = _symbols("P.java", [(i, platform, "jar") for i in invs])
    root = ax.parse_tree(src)
    sinks = persistence.find_sinks(root, symbols=table, relpath="P.java")
    assert len(sinks) == 2
    assert {s.receiver_source for s in sinks} == {"symbol"}

    fake = _symbols("P.java", [(i, "p.SaveServiceHelper", "project") for i in invs])
    assert persistence.find_sinks(root, symbols=fake, relpath="P.java") == []


def test_persistence_ignores_nonexistent_exec_operate_alias():
    src = """package p;
class Caller { void run() {
  OperationServiceHelper.execOperate("audit", "cqkd_bill", null, null);
} }
"""
    root = ax.parse_tree(src)
    assert persistence.find_sinks(root, relpath="P.java") == []


def test_operation_trigger_receiver_symbol_confirms_or_rejects_homonym():
    src = """package p;
class Caller { void run() {
  OperationServiceHelper.executeOperate("audit", "cqkd_bill", null, null);
} }
"""
    inv = next(i for i in _invocations(src) if i.name == "executeOperate")
    root = ax.parse_tree(src)
    const = ConstantTable()
    kw = dict(caller_class="p.Caller", caller_method="run", source_relpath="P.java")

    fake = _symbols("P.java", [(inv, "p.OperationServiceHelper", "project")])
    assert op_trigger.find_operation_triggers(root, const, symbols=fake, **kw) == []

    real = _symbols("P.java", [(
        inv, "kd.bos.servicehelper.operation.OperationServiceHelper", "jar")])
    rows = op_trigger.find_operation_triggers(root, const, symbols=real, **kw)
    assert len(rows) == 1
    assert rows[0].receiver_source == "symbol"
    assert (rows[0].op_key, rows[0].target_form_key) == ("audit", "cqkd_bill")


def test_reverse_propagation_marks_symbol_edge_source():
    src = """package p;
class Caller {
  void run(Object id) {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, "cqkd_bill");
    factory().fill(bill);
  }
  Helper factory() { return null; }
}
class Helper { void fill(DynamicObject o) { o.set("cqkd_x", 1); } }
"""
    inv = next(i for i in _invocations(src) if i.name == "fill")
    table = _symbols("P.java", [(inv, "p.Helper", "project")])
    pg = _project(src, symbols=table)
    result = an.AnalysisResult()
    for node in pg.classes.values():
        an._analyze_standalone(pg, node, pg.const, {"cqkd_bill"}, {}, {}, set(), result)
    an._backfill_reverse_calls(result, pg, pg.const, {"cqkd_bill"}, {})
    row = next(r for r in result.field_accesses if r.field_key == "cqkd_x")
    assert row.form_key == "cqkd_bill"
    assert row.edge_source == "symbol"


def test_confirmed_platform_non_sink_no_longer_counts_as_unknown_external():
    src = """package p;
class Caller { void run() { Factory.get().harmless(); } }
"""
    inv = next(i for i in _invocations(src) if i.name == "harmless")
    pg_plain = _project(src)
    reach = [pgmod.CrossReach("p.Caller", "run", ["run"])]
    assert pg_plain.has_unresolved_external(reach) is True

    table = _symbols("P.java", [(inv, "kd.bos.foo.PlatformHelper", "jar")])
    pg = _project(src, symbols=table)
    assert pg.has_unresolved_external(reach) is False


def test_build_cli_exposes_repeatable_classpath_and_no_symbols():
    args = build_parser().parse_args([
        "build", "src", "meta.dym", "--classpath-dir", "jars-a",
        "--classpath-dir", "jars-b", "--no-symbols",
    ])
    assert args.classpath_dir == ["jars-a", "jars-b"]
    assert args.no_symbols is True
