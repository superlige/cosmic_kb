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
    """默认 profile="overview"：不含 fields/entity_touch（这两段有专职工具顶替），仍 ≤ 预算。"""
    conn = _conn(big_kb)
    try:
        ov = B.bill_compact(conn, "cqkd_assetcard")
    finally:
        conn.close()
    assert _wire_len(ov) <= _COMPACT_BUDGET
    assert "field_touch" not in ov                 # 扁平副本已删
    assert "fields" not in ov and "fields_total" not in ov         # 默认瘦身：不带出
    assert "entity_touch" not in ov and "touched_fields_total" not in ov
    # stats 里仍有真实规模（不静默丢——只是不主动展开明细）。
    assert ov["stats"]["field_count"] >= 120


def test_profile_full_never_exceeds_budget(big_kb):
    """profile="full"：灌爆后仍 ≤ 预算（governor 逐档收紧 cap），且真实总数在 *_total 不丢。"""
    conn = _conn(big_kb)
    try:
        ov = B.bill_compact(conn, "cqkd_assetcard", profile="full")
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


# ── 顶层翻页门 `pagination`（散落 next_cursor 靠"自觉逐段检查"不可靠，
#    改成返回体第一个 key、complete/pending 一眼可判）──────────────────────
def test_pagination_gate_is_first_key_and_flags_incomplete(big_kb):
    """灌爆后 profile="full" 必然有段被截 → `pagination` 是第一个 key，且 complete=False、
    pending 里能找到与 `fields_next_cursor` 一致的游标（两套信号不打架）。
    （_inflate 只灌 fields/field_access，默认 profile="overview" 不含这两段、不会被截，
    故此用例显式要 profile="full" 才能复现截断场景。）"""
    conn = _conn(big_kb)
    try:
        ov = B.bill_compact(conn, "cqkd_assetcard", profile="full")
    finally:
        conn.close()
    assert next(iter(ov)) == "pagination"
    gate = ov["pagination"]
    assert gate["complete"] is False
    assert gate["instruction"]
    cursors = {p["next_cursor"] for p in gate["pending"]}
    assert ov["fields_next_cursor"] in cursors


def test_pagination_gate_complete_when_nothing_capped(tmp_path: Path):
    """未灌爆的小 KB：没有段被截 → complete=True、pending 为空。"""
    db = make_kb(tmp_path)
    conn = _conn(db)
    try:
        ov = B.bill_compact(conn, "cqkd_assetcard")
    finally:
        conn.close()
    assert next(iter(ov)) == "pagination"
    assert ov["pagination"] == {"complete": True, "pending": []}


# ── profile 两档 key 集合锁死（防止以后悄悄漂移）────────────────────────────────
_OVERVIEW_KEYS = {
    "pagination", "form", "stats", "entities", "entities_total", "operations",
    "operations_total", "plugins", "plugins_total", "plugin_lanes",
    "platform_plugins_excluded", "bindings", "bindings_total", "risk_bindings", "note",
}
_FULL_ONLY_KEYS = {"entity_touch", "touched_fields_total", "fields", "fields_total"}


def test_profile_overview_excludes_fields_and_entity_touch_by_default(tmp_path: Path):
    """默认 profile="overview"：key 集合恰好是概览+插件绑定，不含 fields/entity_touch。"""
    db = make_kb(tmp_path)
    conn = _conn(db)
    try:
        ov = B.bill_compact(conn, "cqkd_assetcard")
    finally:
        conn.close()
    assert _FULL_ONLY_KEYS.isdisjoint(ov)
    # 不断言恰好相等——被 cap 的段会带 `*_capped`/`*_next_cursor` 伴生 key，这里只锁"不该出现的没出现"。
    assert _OVERVIEW_KEYS.issubset(ov)


def test_profile_full_matches_legacy_shape(tmp_path: Path):
    """profile="full"：在 overview 基础上补回 fields/entity_touch 及其 *_total，形状与旧版一致。"""
    db = make_kb(tmp_path)
    conn = _conn(db)
    try:
        full = B.bill_compact(conn, "cqkd_assetcard", profile="full")
    finally:
        conn.close()
    assert _OVERVIEW_KEYS.issubset(full)
    assert _FULL_ONLY_KEYS.issubset(full)


def test_profile_invalid_returns_error(tmp_path: Path):
    db = make_kb(tmp_path)
    conn = _conn(db)
    try:
        r = B.bill_compact(conn, "cqkd_assetcard", profile="nope")
    finally:
        conn.close()
    assert "error" in r


def test_page_response_carries_pagination_gate(big_kb):
    """`cursor=` 翻页响应同样带 `pagination`：末页 complete=True，非末页 complete=False 且
    pending 里的 next_cursor 与 page.next_cursor 一致。"""
    conn = _conn(big_kb)
    try:
        r = B.bill_compact(conn, "cqkd_assetcard", cursor="fields@0")
        assert r["pagination"]["complete"] is (r["page"]["next_cursor"] is None)
        if r["page"]["next_cursor"]:
            assert r["pagination"]["pending"][0]["next_cursor"] == r["page"]["next_cursor"]
    finally:
        conn.close()
