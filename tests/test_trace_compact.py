"""信任优先 · trace 紧凑投影（写/读拆分 + 按类合并 + cap/字节 governor）验收测试。

起因：trace 经 MCP 返回被 host 在 32KB 处从中间截断——per-section 行级 cap 管不住（坐标组数 +
unlocated/possible 等独立数组无界）。修复：MCP 走 `trace_compact`——
  ① 按 access 写/读拆分，每次只返一侧；
  ② 把"散落的行/方法"按**类**塌缩成"有界的类数"；
  ③ cap 类节点 + 字节 governor 逐级收紧 cap，保证序列化 ≤ budget（永不被截断）；
  ④ 真实总数恒在 summary / 各节点 capped（红线 #4 不丢数）。

本测覆盖：access 过滤、按类合并去重、cap+真实总数、字节 governor 硬保证、占位坐标兼容。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cosmic_kb.graph import store
from cosmic_kb.report import field_trace

from _synthkb import make_kb


# field_access 列序（与 _synthkb 同款，少数列省略由默认填充）。
_FA_INSERT = (
    "INSERT INTO field_access(form_key,field_key,level,entry_key,plugin_fqn,plugin_type,"
    "access_class,event_method,event_phase,access,persists,persist_reason,via,line,path,"
    "key_resolution,confidence,source_relpath,evidence) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _row(field_key, *, plugin_fqn, access_class=None, access="read", method="propertyChanged",
         line=1, persists="na", form_key="cqkd_assetcard", level="header", entry_key=None,
         via="model.getValue", key_resolution="literal"):
    return (
        form_key, field_key, level, entry_key, plugin_fqn, "form",
        access_class or plugin_fqn, method, "load", access, persists, "", via, line,
        json.dumps([method]), key_resolution, 0.9, "x/Y.java", "",
    )


def _seed(db: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(_FA_INSERT, rows)
        conn.commit()
    finally:
        conn.close()


# ── 单元：按类合并 ────────────────────────────────────────────────────────────
def _wrow(plugin, cls, method, line, persists="yes"):
    return {
        "access_class": cls, "plugin_fqn": plugin, "plugin_type": "op",
        "plugin_form_label": "f「名」", "plugin_cross_form": False,
        "event_method": method, "line": line, "via": "do.set", "persists": persists,
        "persist_reason": "save", "key_resolution": "literal", "source_relpath": "a/B.java",
        "semantics_topic": "plugin-operation",
    }


def test_merge_writers_dedups_class_and_hoists_constants():
    """同类多次写入塌成一个类节点：类级常量只存一份，行级细节进 sites，真实总数在 total。"""
    rows = [_wrow("p.A", "p.A", "m1", 1), _wrow("p.A", "p.A", "m1", 2),
            _wrow("p.A", "p.A", "m2", 3), _wrow("p.B", "p.B", "m", 9)]
    out = field_trace._merge_writers_by_class(rows, cap_classes=60, cap_sites=12)
    assert out["total"] == 4 and out["capped"] == 0
    assert len(out["classes"]) == 2
    a = next(c for c in out["classes"] if c["class_fqn"] == "p.A")
    assert a["count"] == 3 and len(a["sites"]) == 3
    # 类级常量只在类节点出现一次，不在每个 site 重复。
    assert a["plugin_type"] == "op" and "plugin_type" not in a["sites"][0]
    # site 保留行级 line/persists + calls 导航。
    assert {s["line"] for s in a["sites"]} == {1, 2, 3}
    assert a["sites"][0]["calls"] == "calls p.A m1"


def test_merge_writers_caps_classes_and_sites():
    """类数超 cap_classes、单类 site 数超 cap_sites → 截断且把真实数留在 capped/sites_capped。"""
    rows = [_wrow(f"p.C{i}", f"p.C{i}", "m", 1) for i in range(20)]
    rows += [_wrow("p.HOT", "p.HOT", "m", j) for j in range(30)]  # 一个热类 30 个写入点
    out = field_trace._merge_writers_by_class(rows, cap_classes=10, cap_sites=5)
    assert out["total"] == 50
    assert len(out["classes"]) == 10 and out["capped"] == 11  # 21 类 → 留 10、截 11
    hot = out["classes"][0]  # 热类写入最多、排最前
    assert hot["class_fqn"] == "p.HOT" and hot["count"] == 30
    assert len(hot["sites"]) == 5 and hot["sites_capped"] == 25


def test_merge_readers_folds_methods_under_class():
    """读取按类合并，类内按方法去重计数；类级常量一份、方法保 count/calls。"""
    rows = [{"access_class": "p.R", "plugin_fqn": "p.R", "plugin_type": "form",
             "plugin_form_label": None, "event_method": "m1", "semantics_topic": "plugin-form"}
            for _ in range(4)]
    rows += [{"access_class": "p.R", "plugin_fqn": "p.R", "plugin_type": "form",
              "plugin_form_label": None, "event_method": "m2", "semantics_topic": "plugin-form"}]
    out = field_trace._merge_readers_by_class(rows, cap_classes=60, cap_methods=12)
    assert out["total"] == 5 and len(out["classes"]) == 1
    cls = out["classes"][0]
    assert cls["total"] == 5 and cls["class_fqn"] == "p.R"
    top = cls["methods"][0]
    assert top["method"] == "m1" and top["count"] == 4 and top["calls"] == "calls p.R m1"


# ── 集成：access 拆分 ─────────────────────────────────────────────────────────
def test_compact_default_has_writers_and_reader_overview(tmp_path: Path):
    """默认（access=None）：写入明细 + 读取仅按类计数概览，无读取方法明细。"""
    db = make_kb(tmp_path)
    conn = store.open_kb(db)
    try:
        ft = field_trace.trace_compact(conn, "cqkd_collateralstatus")
    finally:
        conn.close()
    assert ft["access"] == "all"
    g = ft["groups"][0]
    assert "writers" in g and "classes" in g["writers"]
    assert "readers_overview" in g and "readers" not in g
    # readers_overview 只有 class_fqn + total，没有方法明细。
    for c in g["readers_overview"]["classes"]:
        assert set(c) == {"class_fqn", "total"}
    assert "dynamic_writers" in ft and "coarse" not in ft


def test_compact_access_read_only_readers(tmp_path: Path):
    """access='read'：只回读取（类→方法）+ coarse，无 writers / dynamic_writers。"""
    db = make_kb(tmp_path)
    # 给 cqkd_amount(entry) 之外再加几条读取，确保有读取类。
    _seed(db, [_row("cqkd_collateralstatus", plugin_fqn="p.Reader1", method="r1"),
               _row("cqkd_collateralstatus", plugin_fqn="p.Reader1", method="r1", line=2),
               _row("cqkd_collateralstatus", plugin_fqn="p.Reader2", method="r2")])
    conn = store.open_kb(db)
    try:
        ft = field_trace.trace_compact(conn, "cqkd_collateralstatus", access="read")
    finally:
        conn.close()
    assert ft["access"] == "read"
    g = ft["groups"][0]
    assert "readers" in g and "writers" not in g and "readers_overview" not in g
    assert "dynamic_writers" not in ft and "coarse" in ft
    classes = {c["class_fqn"] for c in g["readers"]["classes"]}
    assert {"p.Reader1", "p.Reader2"} <= classes
    r1 = next(c for c in g["readers"]["classes"] if c["class_fqn"] == "p.Reader1")
    assert r1["total"] == 2  # 同类两处读取合并、count 累加


def test_compact_access_write_only_writers(tmp_path: Path):
    """access='write'：只回写入 + dynamic_writers，无读取任何形态。"""
    db = make_kb(tmp_path)
    conn = store.open_kb(db)
    try:
        ft = field_trace.trace_compact(conn, "cqkd_collateralstatus", access="write")
    finally:
        conn.close()
    assert ft["access"] == "write"
    g = ft["groups"][0]
    assert "writers" in g and "readers" not in g and "readers_overview" not in g
    assert "dynamic_writers" in ft and "coarse" not in ft


# ── 集成：cap 真实总数 + 字节 governor ────────────────────────────────────────
def test_compact_caps_keep_real_totals_in_summary(tmp_path: Path):
    """构造海量读取类 → 默认视图 readers_overview 按类计数，真实总数恒在 summary（不丢数）。"""
    db = make_kb(tmp_path)
    rows = [_row("cqkd_collateralstatus", plugin_fqn=f"p.R{i}", method=f"m{i}", line=i)
            for i in range(300)]
    _seed(db, rows)
    conn = store.open_kb(db)
    try:
        ft = field_trace.trace_compact(conn, "cqkd_collateralstatus")
    finally:
        conn.close()
    # summary.readers 是真实读取总数（300 + synth 自带），不被 cap 影响。
    assert ft["summary"]["readers"] >= 300


def test_compact_never_exceeds_budget(tmp_path: Path):
    """字节 governor 硬保证：哪怕海量写入/读取，序列化也 ≤ 30000 字节（< 32KB host 上限）。"""
    db = make_kb(tmp_path)
    rows = []
    for i in range(400):  # 400 个写入类，每类多个写入点
        for j in range(5):
            rows.append(_row(
                "cqkd_collateralstatus", plugin_fqn=f"cqspb.pkg.VeryLongPluginClassName{i}",
                access="write", method="beforeExecuteOperationTransaction", line=j,
                persists="yes", via="do.set"))
    _seed(db, rows)
    conn = store.open_kb(db)
    try:
        for access in (None, "write", "read"):
            ft = field_trace.trace_compact(conn, "cqkd_collateralstatus", access=access)
            size = len(json.dumps(ft, ensure_ascii=False))
            assert size <= 30000, f"access={access} 超预算: {size}"
            # 即便被 cap，真实总数仍在 summary（synth 自带 1 个写入 + 注入 2000）。
            assert ft["summary"]["writers"] == 2001
    finally:
        conn.close()
