"""method_calls 紧凑投影（防 MCP 32KB 截断）测试：calls/字段读写按方法计 cap + 字节 governor + 游标分页。

起因（2026-07-06 真实翻车）：`ask` 问 `InvoiceWriteBackTask.execute 是做什么的` 经 MCP 返回被
host 从中段截断（62415 字节实测）——`report/method_calls.py` 当年只顾着给 trace/bill 补紧凑投影
（`efca38e`/`3f6d35a`），自己没补,富 dict 无界（方法体调用多 / 重载方法多时膨胀）。修复同款套路：
`method_calls_compact()` = cap + 字节 governor（按 host `json.dumps(indent=2)` 口径量）+ 游标
分页（红线 #4：被 cap 的条目仍可逐页取回，不只报计数）。

与 test_trace_compact/test_bill_compact 同思路，但不走 `_synthkb` 灌库（method_calls 需要真实
可解析的 Java 源码才能产出 calls/fields），改用 tree-sitter 真跑一遍小型多调用/多字段 fixture，
再用**小 budget**逼出 governor 收紧与分页（不需要真造出上千行源码）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_java")

from pathlib import Path

from cosmic_kb.bridge import linker, namespace
from cosmic_kb.graph import store
from cosmic_kb.ingest import scanner
from cosmic_kb.metadata.model import MetaEntity, MetaField, MetaModel, MetaOperation, MetaPlugin
from cosmic_kb.report import method_calls as mc


# 8 个自调用 + 4 处字段写入 + 2 处字段读取——足够在小 budget 下逼出 calls/fields 双重裁剪。
BIG_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class AmBigOp extends AbstractOperationServicePlugIn {
  public void execute(BeforeOperationArgs e) {
    DynamicObject bill = e.getDataEntities()[0];
    bill.set("cqkd_f0", 0);
    bill.set("cqkd_f1", 1);
    bill.set("cqkd_f2", 2);
    bill.set("cqkd_f3", 3);
    Object x0 = bill.get("cqkd_f0");
    Object x1 = bill.get("cqkd_f1");
    m0(); m1(); m2(); m3(); m4(); m5(); m6(); m7();
    SaveServiceHelper.save(bill);
  }
  public void m0(){} public void m1(){} public void m2(){} public void m3(){}
  public void m4(){} public void m5(){} public void m6(){} public void m7(){}
}
"""


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _fields(*keys):
    return [MetaField("TextField", k, k, "f" + k, "id" + k, "1", "entity", "header",
                      "cqkd_bill") for k in keys]


@pytest.fixture()
def big_kb(tmp_path: Path):
    src = tmp_path / "src"
    _w(src / "AmBigOp.java", BIG_OP)
    scan = scanner.scan(src)

    ents = [MetaEntity("BillEntity", "cqkd_bill", "单据头", "1", "header", None, "t")]
    flds = _fields("cqkd_f0", "cqkd_f1", "cqkd_f2", "cqkd_f3")
    ops = [MetaOperation("submit", "提交", "submit", None, None, resolved_from="self")]
    plugins = [MetaPlugin("cqspb.am.AmBigOp", "op", "project", operation_key="submit")]
    m1 = MetaModel(key="cqkd_bill", name="资产单", model_type="BillFormModel",
                   form_type="bill", isv="cqkd", app_key="cqkd_am",
                   entities=ents, fields=flds, plugins=plugins, operations=ops)
    index = namespace.build_index(scan)
    bridge = linker.link(scan, [m1], index=index)
    from cosmic_kb.report import project_map
    mm = project_map.module_map(scan, [m1], bridge, index=index)
    db = tmp_path / "kb.db"
    store.build_kb(scan, [m1], bridge, mm, db, index=index, source_args={"source_root": str(src)})
    conn = store.open_kb(db)
    try:
        yield conn
    finally:
        conn.close()


def _raw(conn):
    return mc.method_calls(conn, "cqspb.am.AmBigOp", "execute")


# ── 1) 小数据 + 默认 budget：不该被裁 ────────────────────────────────────────────
def test_small_result_not_capped(big_kb):
    rd = _raw(big_kb)
    got = mc.method_calls_compact(big_kb, "cqspb.am.AmBigOp", "execute")
    assert got["found"] is True
    assert got["methods_capped"] == 0
    m = got["methods"][0]
    assert m["calls_capped"] == 0 and m["fields"]["writes_capped"] == 0 \
        and m["fields"]["reads_capped"] == 0
    assert {c["name"] for c in m["calls"]} == {c["name"] for c in rd["methods"][0]["calls"]}


# ── 2) 未命中/歧义：原样透传，不套壳 ──────────────────────────────────────────────
def test_not_found_passthrough(big_kb):
    got = mc.method_calls_compact(big_kb, "cqspb.am.AmBigOp", "noSuchMethod")
    assert got["found"] is False
    assert got["reason"] == "method_not_found"
    assert "methods_total" not in got


# ── 3) 字节 governor：极小 budget 逼出 calls/fields 双重裁剪，且序列化必须 ≤ budget 或落到硬底档 ──
def test_tiny_budget_forces_capping(big_kb):
    got = mc.method_calls_compact(big_kb, "cqspb.am.AmBigOp", "execute", budget=350)
    m = got["methods"][0]
    assert m["calls_capped"] > 0
    assert m["calls_next_cursor"] and m["calls_next_cursor"].startswith("calls:0@")
    assert (m["fields"]["writes_capped"] > 0) or (m["fields"]["reads_capped"] > 0)
    assert got["note"] and "next_cursor" in got["note"]


# ── 4) 游标分页：逐页取回全部被截 calls，一条不丢（红线 #4） ─────────────────────────
def test_calls_pagination_retrieves_all(big_kb):
    rd = _raw(big_kb)
    full_names = {c["name"] for c in rd["methods"][0]["calls"]}

    budget = 350
    first = mc.method_calls_compact(big_kb, "cqspb.am.AmBigOp", "execute", budget=budget)
    m = first["methods"][0]
    got_names = {c["name"] for c in m["calls"]}
    cursor = m.get("calls_next_cursor")
    pages = 0
    while cursor:
        page = mc.method_calls_compact(
            big_kb, "cqspb.am.AmBigOp", "execute", cursor=cursor, budget=budget)
        pg = page["page"]
        assert pg["method_name"] == "execute"
        got_names |= {c["name"] for c in pg["items"]}
        cursor = pg["next_cursor"]
        pages += 1
        assert pages < 1000, "翻页未收敛"
    assert got_names == full_names


# ── 5) 游标分页：字段 writes 同样可逐页取回全部 ──────────────────────────────────────
def test_writes_pagination_retrieves_all(big_kb):
    rd = _raw(big_kb)
    full_keys = {w["field_key"] for w in rd["methods"][0]["fields"]["writes"]}
    assert len(full_keys) == 4          # fixture 埋了 4 处写入，确认 fixture 本身有效

    budget = 350
    first = mc.method_calls_compact(big_kb, "cqspb.am.AmBigOp", "execute", budget=budget)
    m = first["methods"][0]
    got_keys = {w["field_key"] for w in m["fields"]["writes"]}
    cursor = m["fields"].get("writes_next_cursor")
    pages = 0
    while cursor:
        page = mc.method_calls_compact(
            big_kb, "cqspb.am.AmBigOp", "execute", cursor=cursor, budget=budget)
        pg = page["page"]
        got_keys |= {w["field_key"] for w in pg["items"]}
        cursor = pg["next_cursor"]
        pages += 1
        assert pages < 1000, "翻页未收敛"
    assert got_keys == full_keys


# ── 6) 未知 section 报错而非静默返回空 ─────────────────────────────────────────────
def test_unknown_section_reports_error(big_kb):
    got = mc.method_calls_compact(big_kb, "cqspb.am.AmBigOp", "execute", cursor="bogus@0")
    assert "error" in got["page"]
