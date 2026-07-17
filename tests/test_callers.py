"""阶段 12.3 · call_edge 持久化与 callers CLI/MCP 反查验收。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from cosmic_kb.cli.main import main
from cosmic_kb.graph import store
from cosmic_kb.java import ast_index as ax
from cosmic_kb.java import call_edges, project_graph as pgmod
from cosmic_kb.java.parser import is_available
from cosmic_kb.java.symbols import SymbolTable
from cosmic_kb.mcp import server as mcp_server
from cosmic_kb.report import callers as callers_report

from _synthkb import make_kb


def _conn(db: Path):
    return store.open_kb(db)


def test_schema_v20_has_call_edge_access_method_and_indexes(tmp_path: Path):
    db = make_kb(tmp_path)
    conn = _conn(db)
    try:
        assert store.KB_SCHEMA_VERSION == "20"
        facc_cols = {r[1] for r in conn.execute("PRAGMA table_info(field_access)")}
        assert "access_method" in facc_cols
        cols = {r[1] for r in conn.execute("PRAGMA table_info(call_edge)")}
        assert cols == {
            "caller_fqn", "caller_method", "target_fqn", "target_method",
            "target_signature", "kind", "line", "col", "source_relpath",
            "resolution", "target_kind", "confidence", "evidence",
        }
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(call_edge)")}
        assert {"idx_call_edge_target", "idx_call_edge_caller"} <= indexes
    finally:
        conn.close()


def test_callers_returns_invocation_and_method_reference_with_coverage(tmp_path: Path):
    conn = _conn(make_kb(tmp_path))
    try:
        result = callers_report.callers(conn, "CollateralService.update")
    finally:
        conn.close()
    assert result["target"]["target_fqn"] == "cqspb.assets.CollateralService"
    assert result["summary"] == {
        "call_sites": 2, "caller_methods": 2, "method_references": 1,
        "by_resolution": {"expr": 1, "scope": 1},
    }
    assert {r["kind"] for r in result["callers"]} == {"invocation", "method_reference"}
    assert result["resolution_coverage"]["strong_zero_evidence"] is True


def test_callers_simple_class_ambiguity_asks_for_fqn(tmp_path: Path):
    db = make_kb(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO source_class(fqn,simple,package,relpath) VALUES(?,?,?,?)",
        [
            ("p.one.Duplicate", "Duplicate", "p.one", "p/one/Duplicate.java"),
            ("p.two.Duplicate", "Duplicate", "p.two", "p/two/Duplicate.java"),
        ],
    )
    conn.commit()
    conn.close()

    conn = _conn(db)
    try:
        result = callers_report.callers(conn, "Duplicate.run")
    finally:
        conn.close()
    assert result["status"] == "need_clarification"
    assert [c["locator"] for c in result["candidates"]] == [
        "p.one.Duplicate.run", "p.two.Duplicate.run"]
    assert "resolution_coverage" in result


def test_zero_result_wording_depends_on_symbol_coverage(tmp_path: Path):
    db = make_kb(tmp_path)
    conn = _conn(db)
    try:
        strong = callers_report.callers(conn, "CollateralService.unused")
    finally:
        conn.close()
    assert strong["callers"] == []
    assert strong["resolution_coverage"]["strong_zero_evidence"] is True
    assert "强证据" in strong["note"]

    raw = sqlite3.connect(str(db))
    raw.execute(
        "UPDATE kb_meta SET value=? WHERE key='symbol_resolution'",
        (json.dumps({"status": "disabled", "coverage": 0.0,
                     "reason": "--no-symbols"}),),
    )
    raw.commit()
    raw.close()
    conn = _conn(db)
    try:
        weak = callers_report.callers(conn, "CollateralService.unused")
    finally:
        conn.close()
    assert weak["resolution_coverage"]["strong_zero_evidence"] is False
    assert "不足以断言死代码" in weak["note"]


def test_callers_cli_json_and_mcp_share_report_contract(
    tmp_path: Path, monkeypatch, capsys,
):
    db = make_kb(tmp_path)
    rc = main(["callers", "CollateralService.update", "--db", str(db), "--json"])
    assert rc == 0
    cli = json.loads(capsys.readouterr().out)

    monkeypatch.setenv("COSMIC_KB_DB", str(db))
    mcp = mcp_server.tool_callers("CollateralService.update")
    assert mcp["callers"] == cli["callers"]
    assert mcp["summary"] == cli["summary"]
    assert mcp["resolution_coverage"] == cli["resolution_coverage"]
    assert mcp["pagination"]["complete"] is True
    assert mcp["summary"]["method_references"] == 1


def test_mcp_callers_paginates_to_completion_without_losing_rows(tmp_path: Path, monkeypatch):
    db = make_kb(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO call_edge VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(f"p.Caller{i}", "run", "cqspb.assets.CollateralService", "update", None,
          "invocation", 100 + i, 9, f"p/Caller{i}.java", "heuristic", "project", 0.6,
          "fallback=tree-sitter-heuristic") for i in range(85)],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("COSMIC_KB_DB", str(db))

    cursor = None
    rows: list[dict] = []
    while True:
        page = mcp_server.tool_callers("CollateralService.update", cursor=cursor)
        rows.extend(page["callers"])
        if page["pagination"]["complete"]:
            break
        cursor = page["pagination"]["next_cursor"]
        assert cursor
    assert len(rows) == 87
    assert len({(r["source_relpath"], r["line"], r["col"]) for r in rows}) == 87


@pytest.mark.skipif(not is_available(), reason="需要 tree-sitter Java")
def test_collect_call_edges_keeps_all_resolution_kinds_and_failed_sites():
    source = """package p;
class Caller {
  void run() {
    own();
    Target.go();
    Mystery.go();
    stream.map(Target::go);
  }
  void own() {}
}
class Target { static void go() {} }
"""
    root = ax.parse_tree(source)
    assert root is not None
    invs = list(ax.iter_invocations(root, include_refs=True))
    target_call = next(i for i in invs if i.name == "go" and i.kind == "invocation"
                       and i.object_text == "Target")
    target_ref = next(i for i in invs if i.name == "go" and i.kind == "method_reference")
    mystery = next(i for i in invs if i.object_text == "Mystery")

    table = SymbolTable()
    table.add_file_event({
        "event": "file", "relpath": "P.java", "status": "ok", "sites": [
            {"line": target_call.line, "col": target_call.col, "name": "go",
             "kind": "invocation", "resolution": "expr", "declaring": "p.Target",
             "signature": "p.Target.go()", "target_kind": "project", "argc": 0},
            {"line": target_ref.line, "col": target_ref.col, "name": "go",
             "kind": "method_reference", "resolution": "scope", "declaring": "p.Target",
             "signature": "p.Target.go()", "target_kind": "project"},
            {"line": mystery.line, "col": mystery.col, "name": "go",
             "kind": "invocation", "resolution": "failed", "reason": "unsolved-symbol",
             "argc": 0},
        ],
    })
    scan = SimpleNamespace(ok_files=[SimpleNamespace(relpath="P.java", text=source)])
    pg = pgmod.build_project_graph(scan, None, symbols=table)
    rows = call_edges.collect_call_edges(pg)

    assert {r.resolution for r in rows} == {"expr", "scope", "heuristic", "failed"}
    assert any(r.target_fqn == "p.Caller" and r.target_method == "own"
               and r.resolution == "heuristic" for r in rows)
    assert any(r.kind == "method_reference" and r.target_fqn == "p.Target" for r in rows)
    failed = [r for r in rows if r.target_method in {"go", "map"} and r.resolution == "failed"]
    assert failed and all(r.target_fqn is None for r in failed)
