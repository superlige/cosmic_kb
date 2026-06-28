"""bill 紧凑投影（防 MCP 32KB 截断）测试：折叠逐字段事件 + cap/字节 governor + 游标分页。

与 test_trace_compact 同思路：真实库大单据序列化达 2.76MB 会被 host 从中段硬切；bill_compact
把逐条事件折叠为计数、各列表 cap + 字节 governor，并给被 cap 段 next_cursor 让消费方逐页取回
（红线 #4：不仅报计数，还可达）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cosmic_kb.graph import store
from cosmic_kb.report import bill_view as B
from cosmic_kb.report.field_trace import _wire_len, _COMPACT_BUDGET

from _synthkb import make_kb


def _inflate(db: Path, n_fields: int = 120, events_per_field: int = 6) -> None:
    """往 cqkd_assetcard 灌入大量字段 + 每字段多条事件，逼出 cap/分页（模拟真实大单据）。"""
    conn = sqlite3.connect(str(db))
    try:
        for i in range(n_fields):
            fk = f"cqkd_f{i:03d}"
            conn.execute(
                "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (f"uf{i}", "cqkd_assetcard", "cqkd_assetcard", fk, f"字段{i}",
                 f"fc{i}", "TextField", "entity", "header"))
            for j in range(events_per_field):
                conn.execute(
                    "INSERT INTO field_access(form_key,field_key,level,entry_key,plugin_fqn,"
                    "plugin_type,access_class,event_method,event_phase,access,persists,"
                    "persist_reason,via,line,path,key_resolution,confidence,source_relpath,evidence) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("cqkd_assetcard", fk, "header", None, "cqspb.assets.CollateralOp", "op",
                     "cqspb.assets.CollateralService", f"evt{j}", "transaction", "write", "yes",
                     "x", "do.set", 100 + j, "[]", "literal", 0.9,
                     "cqspb/assets/CollateralService.java", ""))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def big_kb(tmp_path: Path) -> Path:
    db = make_kb(tmp_path)
    _inflate(db)
    return db


def _conn(db: Path):
    return store.open_kb(db)


def test_overview_never_exceeds_budget(big_kb):
    """灌爆后 overview 仍 ≤ 预算（governor 逐档收紧 cap），且真实总数在 *_total 不丢。"""
    conn = _conn(big_kb)
    try:
        ov = B.bill_compact(conn, "cqkd_assetcard")
    finally:
        conn.close()
    assert _wire_len(ov) <= _COMPACT_BUDGET
    assert "field_touch" not in ov                 # 扁平副本已删
    assert ov["fields_total"] >= 120               # 真实总数保留
    assert ov["touched_fields_total"] >= 120
    # 灌了 120 字段、cap 远小于此 → 必然截断并给游标。
    assert ov.get("fields_next_cursor", "").startswith("fields@")


def test_entity_touch_pagination_retrieves_all(big_kb):
    """entity_touch 被 cap 的字段行可逐页取回全部（一条不丢）。"""
    conn = _conn(big_kb)
    try:
        full = B._bill_section_full(B.bill_view(conn, "cqkd_assetcard"), "entity_touch")
        got, cur, pages = [], "entity_touch@0", 0
        while cur:
            r = B.bill_compact(conn, "cqkd_assetcard", cursor=cur)
            pg = r["page"]
            got += pg["items"]
            cur = pg["next_cursor"]
            pages += 1
            assert _wire_len(r) < 32768
            assert pages < 1000, "翻页未收敛"
    finally:
        conn.close()
    assert len(got) == len(full) >= 120
    # 每行带实体上下文 + trace 导航锚点（逐字段下钻）。
    assert all("trace" in it and it["trace"].startswith("trace cqkd_assetcard.") for it in got)


def test_fields_pagination_multipage_tiny_budget(big_kb):
    """极小 budget 下 fields 分多页、逐页 ≤ budget、合起来不丢条目。"""
    conn = _conn(big_kb)
    try:
        bv = B.bill_view(conn, "cqkd_assetcard")
        full = B._bill_section_full(bv, "fields")
        got, off, pages = [], 0, 0
        while True:
            r = B._bill_page_section(bv, "fields", off, budget=2000)
            pg = r["page"]
            got += pg["items"]
            pages += 1
            assert _wire_len(r) < 32768
            if not pg["next_cursor"]:
                break
            off = B._parse_cursor(pg["next_cursor"])[1]
            assert pages < 1000
    finally:
        conn.close()
    assert len(got) == len(full)
    assert pages > 1, "极小预算应分多页"


def test_unknown_section_errors(big_kb):
    """未知/不可分页段 → page.error 引导（不静默返回空）。"""
    conn = _conn(big_kb)
    try:
        r = B.bill_compact(conn, "cqkd_assetcard", cursor="nope@0")
    finally:
        conn.close()
    assert "error" in r["page"]


def test_missing_form_returns_error(big_kb):
    conn = _conn(big_kb)
    try:
        assert "error" in B.bill_compact(conn, "cqkd_nope")
    finally:
        conn.close()
