"""信任优先 · unknown 字段细分（动态循环/拼接/外部常量）+ 全局审计 + trace 折进验收。

覆盖本次拍板的口径（用户 2026-06-24）：字段 key 钉不出的写入**不臆造字段**，而是按成因诚实细分，
并在 trace 里按单据/数据包限定范围亮出「动态写入候选」、在全局 `dynwrites` 里汇总，交段二大模型读源码定性。
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
from cosmic_kb.report import dynamic_writes, field_trace, project_map

# 一个操作插件：表头字面量字段（可解析）+ 三类钉不出 key 的写入（动态循环/拼接/外部常量）。
DYN_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class AmDynOp extends AbstractOperationServicePlugIn {
  static final String PREFIX = "cqkd_amt";
  public void beforeExecuteOperationTransaction(BeforeOperationArgs e) {
    DynamicObject bill = e.getDataEntities()[0];
    bill.set("cqkd_head", 1);                      // literal → 解析到具体字段
    for (String f : pickFields()) {                // 动态循环：字段集运行时定
      bill.set(f, 2);
    }
    String setKey = PREFIX + "_" + suffix;          // 拼接键（局部变量持拼接结果）
    bill.set(setKey, 3);
    bill.set(EXTERNAL_FIELD, 4);                     // 外部/跨模块常量（未命中常量表）
  }
}
"""


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _build(tmp_path: Path):
    src = tmp_path / "src"
    _w(src / "AmDynOp.java", DYN_OP)
    scan = scanner.scan(src)
    ents = [MetaEntity("BillEntity", "cqkd_bill", "单据头", "1", "header", None, "t")]
    flds = [MetaField("TextField", "cqkd_head", "表头字段", "fhead", "idh", "1",
                      "entity", "header", "cqkd_bill")]
    ops = [MetaOperation("submit", "提交", "submit", None, None, resolved_from="self")]
    plugins = [MetaPlugin("cqspb.am.AmDynOp", "op", "project", operation_key="submit")]
    m1 = MetaModel(key="cqkd_bill", name="资产单", model_type="BillFormModel",
                   form_type="bill", isv="cqkd", app_key="cqkd_am",
                   entities=ents, fields=flds, plugins=plugins, operations=ops)
    index = namespace.build_index(scan)
    bridge = linker.link(scan, [m1], index=index)
    mm = project_map.module_map(scan, [m1], bridge, index=index)
    db = tmp_path / "kb.db"
    store.build_kb(scan, [m1], bridge, mm, db, index=index)
    return db


def _null_rows(conn):
    return [dict(zip(("key_resolution", "access", "form_key", "field_key"), r))
            for r in conn.execute(
                "SELECT key_resolution,access,form_key,field_key FROM field_access "
                "WHERE field_key IS NULL")]


def test_null_key_classified_by_cause(tmp_path: Path):
    """钉不出 key 的写入被细分为 dynamic-loop / concat / external-const，且 field_key 仍为空（不臆造）。"""
    db = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rows = _null_rows(conn)
        causes = {r["key_resolution"] for r in rows}
        assert {"dynamic-loop", "concat", "external-const"} <= causes
        # 一律 field_key 为空（诚实不臆造）+ 来源单据解析到 cqkd_bill。
        assert all(r["field_key"] is None for r in rows)
        assert all(r["form_key"] == "cqkd_bill" for r in rows
                   if r["key_resolution"] in ("dynamic-loop", "concat", "external-const"))
        # 字面量字段照常解析到具体 key（细分不影响正常解析）。
        head = conn.execute(
            "SELECT key_resolution FROM field_access WHERE field_key='cqkd_head'").fetchone()
        assert head["key_resolution"] in ("literal", "constant")
    finally:
        conn.close()


def test_global_dynwrites_method_worklist(tmp_path: Path):
    """全局审计按成因桶汇总 + 按方法去重的「该读方法」清单（不回逐行，防上下文爆炸）。"""
    db = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        d = dynamic_writes.summarize(conn)
        assert d["total"] >= 3 and d["writes"] >= 3
        by_cause = d["by_cause"]
        for cause in ("dynamic-loop", "concat", "external-const"):
            assert by_cause.get(cause, {}).get("writes", 0) >= 1
        # 三类的动态写都在同一个方法里 → 各成因桶去重后应是 1 个方法、带 calls 锚点。
        dl = by_cause["dynamic-loop"]
        assert dl["total_methods"] == 1 and len(dl["methods"]) == 1
        m = dl["methods"][0]
        assert m["class_fqn"] == "cqspb.am.AmDynOp" and m["calls"].startswith("calls ")
        # 渲染不崩、带成因标签 + 方法行。
        text = dynamic_writes.render_dynamic_writes(d)
        assert "动态循环" in text and "拼接键" in text and "AmDynOp" in text
    finally:
        conn.close()


def test_dynwrites_filter(tmp_path: Path):
    """过滤参数：按 cause 切片只回该成因的方法清单。"""
    db = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        d = dynamic_writes.summarize(conn, cause="concat")
        assert set(d["by_cause"]) == {"concat"}
        assert d["writes"] >= 1
    finally:
        conn.close()


def test_trace_surfaces_dynamic_writers_scoped(tmp_path: Path):
    """trace 某字段时，同单据内钉不出 key 的写入作为「动态写入候选」亮出（按 form_key 限定范围）。"""
    db = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        ft = field_trace.field_trace(conn, "cqkd_head", form_key="cqkd_bill", level="header")
        dyn = ft["dynamic_writers"]
        assert dyn["total"] >= 3
        assert all(by >= 1 for by in (
            dyn["by_cause"]["dynamic-loop"],
            dyn["by_cause"]["concat"],
            dyn["by_cause"]["external-const"]))
        # 折叠成方法清单：三类动态写都在 AmDynOp 同一事件方法里 → 去重后 1 个方法。
        assert dyn["total_methods"] == 1
        m = dyn["methods"][0]
        assert m["class_fqn"] == "cqspb.am.AmDynOp" and m["count"] >= 3
        assert m["calls"].startswith("calls ")
        # 渲染含动态写入候选段 + 方法行。
        text = field_trace.render_field_trace(ft)
        assert "动态写入候选" in text and "个方法" in text and "AmDynOp" in text
    finally:
        conn.close()
