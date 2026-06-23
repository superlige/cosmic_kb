"""方法出向调用导航 method_calls（该方法调了项目内哪些方法 → 目标类/源文件/行）。

定位重置（2026-06-23）：段二大模型直接读本机源码 + 挂苍穹 skill，源码复述/方法解释交给它；
确定性层只回大模型自己猜不准的「跳转到定义」——project 内调用解析到具体类与源文件。

覆盖：
  * 事件方法：解析出项目内可下钻调用（svc.touch → AmDeepService），带 target_relpath；
  * 噪声不收：平台落库 sink（SaveServiceHelper.save）、字段取值习语（bill.set）等一律不进清单；
  * 跨类下钻：对被调 service 方法再 method_calls 同样可用；
  * 源码根自动解析（建库时记入 source_args）与显式 --source-root 覆盖；
  * 未命中：类不存在 / 同末段类名歧义 / 方法不存在 → 候选反问，不臆造；
  * 三层同口径：report.method_calls ↔ resolver.method_calls+builder ↔ MCP tool_method_calls。

tree-sitter 未装则跳过（调用分析依赖 [parse] extra）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_java")

from pathlib import Path

from cosmic_kb.bridge import linker, namespace
from cosmic_kb.graph import store
from cosmic_kb.ingest import scanner
from cosmic_kb.metadata.model import (
    MetaEntity, MetaField, MetaModel, MetaOperation, MetaPlugin,
)
from cosmic_kb.report import method_calls, project_map

# ── 源码 fixtures ──────────────────────────────────────────────────────────

DEEP_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class AmDeepOp extends AbstractOperationServicePlugIn {
  private AmDeepService svc = new AmDeepService();
  public void beforeExecuteOperationTransaction(BeforeOperationArgs e) {
    DynamicObject bill = e.getDataEntities()[0];
    bill.set("cqkd_head", 1);
    svc.touch(bill);
    SaveServiceHelper.save(bill);
  }
}
"""

DEEP_SVC = """package cqspb.am;
public class AmDeepService {
  public void touch(DynamicObject bill) {
    bill.set("cqkd_status", "B");
  }
}
"""

# 同末段类名、不同包 → 只给简单名时应判歧义。
DUP_A = """package cqspb.am;
public class AmDup {
  public void foo(DynamicObject bill) { bill.set("cqkd_head", 1); }
}
"""
DUP_B = """package cqspb.other;
public class AmDup {
  public void foo(DynamicObject bill) { bill.set("cqkd_head", 2); }
}
"""


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _fields(*keys):
    return [MetaField("TextField", k, k, "f" + k, "id" + k, "1", "entity", "header",
                      "cqkd_bill") for k in keys]


def _build(tmp_path: Path):
    src = tmp_path / "src"
    _w(src / "AmDeepOp.java", DEEP_OP)
    _w(src / "AmDeepService.java", DEEP_SVC)
    _w(src / "AmDupA.java", DUP_A)
    _w(src / "other" / "AmDupB.java", DUP_B)
    scan = scanner.scan(src)

    ents = [MetaEntity("BillEntity", "cqkd_bill", "单据头", "1", "header", None, "t")]
    flds = _fields("cqkd_head", "cqkd_status")
    ops = [MetaOperation("submit", "提交", "submit", None, None, resolved_from="self")]
    plugins = [MetaPlugin("cqspb.am.AmDeepOp", "op", "project", operation_key="submit")]
    m1 = MetaModel(key="cqkd_bill", name="资产单", model_type="BillFormModel",
                   form_type="bill", isv="cqkd", app_key="cqkd_am",
                   entities=ents, fields=flds, plugins=plugins, operations=ops)
    models = [m1]
    index = namespace.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    mm = project_map.module_map(scan, models, bridge, index=index)
    db = tmp_path / "kb.db"
    # source_args 记入源码根，供 method_calls 自动定位源文件（不传 --source-root 时）。
    store.build_kb(scan, models, bridge, mm, db, index=index,
                   source_args={"source_root": str(src)})
    return db, str(src)


# ── 1) 事件方法：解析出项目内可下钻调用，噪声不收 ────────────────────────────────
def test_event_method_project_calls(tmp_path: Path):
    db, src = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rd = method_calls.method_calls(
            conn, "cqspb.am.AmDeepOp", "beforeExecuteOperationTransaction", source_root=src)
        assert rd["found"] is True
        assert len(rd["methods"]) == 1
        m = rd["methods"][0]

        # 行号区间在（让大模型知道直接读哪几行），但不回源码全文。
        assert m["start_line"] and m["end_line"]
        assert "source_code" not in m

        # 项目内可下钻：svc.touch → AmDeepService，带目标源文件相对路径。
        names = {c["name"]: c for c in m["calls"]}
        assert "touch" in names
        touch = names["touch"]
        assert touch["target_fqn"] == "cqspb.am.AmDeepService"
        assert touch["target_relpath"] and touch["target_relpath"].endswith(".java")

        # 噪声不收：平台落库 sink save、字段取值习语 set/getDataEntities 不进清单。
        call_names = {c["name"] for c in m["calls"]}
        assert "save" not in call_names
        assert "set" not in call_names
        assert "getDataEntities" not in call_names
        # 收录的每一条都必须解析到了项目内目标。
        assert all(c["target_fqn"] for c in m["calls"])
    finally:
        conn.close()


# ── 2) 跨类下钻：对被调 service 方法再导航（叶子方法、无项目内调用）─────────────────
def test_drilldown_into_service(tmp_path: Path):
    db, src = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rd = method_calls.method_calls(conn, "cqspb.am.AmDeepService", "touch", source_root=src)
        assert rd["found"] is True
        m = rd["methods"][0]
        # touch 只调 bill.set（字段习语，外部接收者）→ 无项目内调用。
        assert m["summary"]["project_calls"] == 0
    finally:
        conn.close()


# ── 3) 源码根自动解析（不传 source_root，靠 source_args）──────────────────────────
def test_source_root_auto_from_meta(tmp_path: Path):
    db, _src = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rd = method_calls.method_calls(
            conn, "cqspb.am.AmDeepOp", "beforeExecuteOperationTransaction")
        assert rd["found"] is True
        assert rd["source_available"] is True
        assert any(c["name"] == "touch" for c in rd["methods"][0]["calls"])
    finally:
        conn.close()


# ── 4) 未命中：类不存在 / 末段类名歧义 / 方法不存在 ───────────────────────────────
def test_class_not_found(tmp_path: Path):
    db, src = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rd = method_calls.method_calls(conn, "cqspb.am.NoSuch", "x", source_root=src)
        assert rd["found"] is False
        assert rd["reason"] == "class_not_found"
    finally:
        conn.close()


def test_class_ambiguous(tmp_path: Path):
    db, src = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rd = method_calls.method_calls(conn, "AmDup", "foo", source_root=src)
        assert rd["found"] is False
        assert rd["reason"] == "class_ambiguous"
        fqns = {c["fqn"] for c in rd["candidates"]}
        assert {"cqspb.am.AmDup", "cqspb.other.AmDup"} <= fqns
    finally:
        conn.close()


def test_method_not_found_lists_candidates(tmp_path: Path):
    db, src = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rd = method_calls.method_calls(conn, "cqspb.am.AmDeepOp", "noSuchMethod", source_root=src)
        assert rd["found"] is False
        assert rd["reason"] == "method_not_found"
        assert "beforeExecuteOperationTransaction" in rd["candidates"]
    finally:
        conn.close()


# ── 5) resolver + builder：自然语言 method_calls 同口径 ───────────────────────────
def test_resolver_routes_method_calls(tmp_path: Path):
    from cosmic_kb.semantic import resolver

    db, _src = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rq = resolver.resolve(
            conn, "AmDeepOp 的 beforeExecuteOperationTransaction 方法做了什么")
        assert rq.intent == "method_calls"
        assert rq.class_fqn == "cqspb.am.AmDeepOp"
        assert rq.method_name == "beforeExecuteOperationTransaction"
    finally:
        conn.close()


def test_builder_method_calls(tmp_path: Path):
    from cosmic_kb.semantic.resolver import ResolvedQuery
    from cosmic_kb.context import builder

    db, _src = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rq = ResolvedQuery(
            "method_calls", "x", confidence=0.9,
            class_fqn="cqspb.am.AmDeepOp", method_name="beforeExecuteOperationTransaction")
        ctx = builder.build_context(conn, rq)
        assert ctx["status"] == "ok"
        assert ctx["evidence"]["found"] is True
        # 渲染不崩、含方法名与下钻目标。
        txt = builder.render_context(ctx)
        assert "beforeExecuteOperationTransaction" in txt
        assert "cqspb.am.AmDeepService" in txt
    finally:
        conn.close()


# ── 6) MCP 工具：与 report 同口径 ────────────────────────────────────────────────
def test_mcp_tool_method_calls(tmp_path: Path, monkeypatch):
    from cosmic_kb.mcp import server as mcp_server

    db, _src = _build(tmp_path)
    monkeypatch.setenv("COSMIC_KB_DB", str(db))
    got = mcp_server.tool_method_calls("cqspb.am.AmDeepOp", "beforeExecuteOperationTransaction")

    conn = store.open_kb(db)
    try:
        expected = method_calls.method_calls(
            conn, "cqspb.am.AmDeepOp", "beforeExecuteOperationTransaction")
    finally:
        conn.close()
    assert got == expected
    assert "method_calls" in mcp_server.TOOLS
