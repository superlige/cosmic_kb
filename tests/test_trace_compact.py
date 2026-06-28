"""信任优先 · trace 紧凑投影（写/读拆分 + 按类合并 + cap/字节 governor）验收测试。

起因：trace 经 MCP 返回被 host 在 32KB 处从中间截断——per-section 行级 cap 管不住（坐标组数 +
unlocated/possible 等独立数组无界）。修复：MCP 走 `trace_compact`——
  ① 按 access 写/读拆分，每次只返一侧；
  ② 把"散落的行/方法"按**类**塌缩成"有界的类数"；
  ③ cap 类节点 + 字节 governor 逐级收紧 cap，保证序列化 ≤ budget（永不被截断）；
  ④ 真实总数恒在 summary / 各节点 capped（红线 #4 不丢数）。

**字节度量必须对齐 host 真实序列化**：MCP 底层 `mcp/server/lowlevel/server.py` 用
`json.dumps(result, indent=2)`（ensure_ascii 默认 True）发文本——indent 缩进让深层嵌套结构膨胀
~35%。governor 只量无缩进会严重低估、误判「没超」而被 host 从中段硬切。故一律用
`field_trace._wire_len()`（= indent=2, ensure_ascii=True）度量。

本测覆盖：access 过滤、按类合并去重、cap+真实总数、字节 governor 硬保证（按 host indent 口径）、占位坐标兼容。
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


def test_compact_unlocated_is_worklist(tmp_path: Path):
    """未定位单据（form_key=None 但确实读写本字段）在 compact 下折叠成「反推来源单据」工作单：
    按 (类,方法) 去重、写读分计、带 calls + plugin_form_label 来源线索（非自动回填）。"""
    db = make_kb(tmp_path)
    # 同字段、来源单据判不出（form_key=None），但插件注册在 cqkd_assetcard 上（→ plugin_form_label 线索）。
    _seed(db, [
        _row("cqkd_collateralstatus", plugin_fqn="cqspb.assets.CollateralOp",
             access_class="cqspb.assets.Helper", access="write", method="fill", line=10,
             form_key=None, via="do.set"),
        _row("cqkd_collateralstatus", plugin_fqn="cqspb.assets.CollateralOp",
             access_class="cqspb.assets.Helper", access="write", method="fill", line=11,
             form_key=None, via="do.set"),
    ])
    conn = store.open_kb(db)
    try:
        # 精确单据模式才会分出 unlocated 桶（裸字段查询全归坐标组）。
        ft = field_trace.trace_compact(conn, "cqkd_collateralstatus", form_key="cqkd_assetcard")
    finally:
        conn.close()
    ul = ft["unlocated"]
    assert set(ul) == {"total", "writes", "reads", "methods", "capped"}
    assert ul["total"] == 2 and ul["writes"] == 2          # 两处写入去重前真实数
    m = ul["methods"][0]
    assert m["method"] == "fill" and m["writes"] == 2 and m["calls"] == "calls cqspb.assets.CollateralOp fill"
    # 来源线索 = 插件注册单据（只读提示，不写进 form_key）。
    assert "cqkd_assetcard" in (m["plugin_form_label"] or "")


def test_compact_unlocated_never_exceeds_budget(tmp_path: Path):
    """防膨胀硬保证：海量 form_key=None 读写时，compact 折叠后仍 ≤ budget（折叠 site→count 必然更省）。"""
    db = make_kb(tmp_path)
    rows = []
    for i in range(400):
        rows.append(_row(
            "cqkd_collateralstatus", plugin_fqn=f"cqspb.pkg.VeryLongUnlocatedPlugin{i}",
            access="write", method="beforeExecuteOperationTransaction", line=i,
            form_key=None, via="do.set"))
    _seed(db, rows)
    conn = store.open_kb(db)
    try:
        for access in (None, "write", "read"):
            ft = field_trace.trace_compact(conn, "cqkd_collateralstatus",
                                           form_key="cqkd_assetcard", access=access)
            size = field_trace._wire_len(ft)  # host 真实序列化口径（indent=2）
            assert size <= field_trace._COMPACT_BUDGET, f"access={access} 超预算: {size}"
            # 被 cap 时多带 next_cursor（翻页取回被截条目），故用子集断言。
            assert {"total", "writes", "reads", "methods", "capped"} <= set(ft["unlocated"])
        # 真实总数不丢（summary.unlocated 记全部）。
        ft = field_trace.trace_compact(conn, "cqkd_collateralstatus", form_key="cqkd_assetcard")
        assert ft["summary"]["unlocated"] == 400
    finally:
        conn.close()


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
    """字节 governor 硬保证：哪怕海量写入/读取，host 真实序列化（indent=2）也 ≤ budget（< 32768）。"""
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
            size = field_trace._wire_len(ft)  # host 真实序列化口径（indent=2, ensure_ascii=True）
            assert size <= field_trace._COMPACT_BUDGET, f"access={access} 超预算: {size}"
            assert size < 32768, f"access={access} 越 host 硬上限: {size}"
            # 即便被 cap，真实总数仍在 summary（synth 自带 1 个写入 + 注入 2000）。
            assert ft["summary"]["writers"] == 2001
    finally:
        conn.close()


def test_compact_governor_matches_host_indent_serialization(tmp_path: Path):
    """governor 必须按 host 真实序列化口径（json.dumps indent=2, ensure_ascii=True）度量——这是
    真实截断的根因：MCP 底层用 indent=2 发文本，缩进让深层嵌套膨胀 ~35%；只量无缩进会低估而被截。
    海量中文 + 深层嵌套下，_wire_len（含 indent）也必须 ≤ budget 且 < 32768。"""
    db = make_kb(tmp_path)
    rows = [_row("cqkd_collateralstatus", plugin_fqn=f"p.R{i}", method=f"读取方法{i}", line=i)
            for i in range(400)]
    _seed(db, rows)
    conn = store.open_kb(db)
    try:
        for access in (None, "write", "read"):
            ft = field_trace.trace_compact(conn, "cqkd_collateralstatus", access=access)
            wire = field_trace._wire_len(ft)  # = len(json.dumps(ft, ensure_ascii=True, indent=2))
            assert wire == len(json.dumps(ft, ensure_ascii=True, indent=2))  # 钉死 host 口径
            assert wire <= field_trace._COMPACT_BUDGET, f"access={access} wire 超预算: {wire}"
            assert wire < 32768  # 永远在 host 硬上限内
            # 缩进确实更占字节：indent 口径必 ≥ 无缩进口径（防有人把度量改回无缩进）。
            assert wire >= len(json.dumps(ft, ensure_ascii=True))
    finally:
        conn.close()


def test_compact_caps_groups_for_bare_field(tmp_path: Path):
    """裸字段（全量调，无 form/entry/level）命中大量坐标组时，**坐标组本身**被 cap——这是旧版
    governor 收不住的洞（per-class cap 再小也压不下整组体积）。真实组数留 groups_total/summary.coords。"""
    db = make_kb(tmp_path)
    rows = []
    for i in range(120):  # 120 张不同单据 → 120 个坐标组
        for j in range(3):
            rows.append(_row(
                "cqkd_collateralstatus", plugin_fqn=f"cqspb.pkg.VeryLongPlugin{i}",
                access="write", method="beforeExecuteOperationTransaction", line=j,
                persists="yes", form_key=f"cqkd_someform{i}", via="do.set"))
    _seed(db, rows)
    conn = store.open_kb(db)
    try:
        ft = field_trace.trace_compact(conn, "cqkd_collateralstatus")  # 裸字段，全量调
        wire = field_trace._wire_len(ft)  # host 真实序列化口径（indent=2）
        assert wire <= field_trace._COMPACT_BUDGET, f"超预算: {wire}"
        # 坐标组被裁剪，但真实组数不丢（红线 #4）。
        assert ft["groups_total"] >= 120
        assert len(ft["groups"]) < ft["groups_total"]
        assert ft["groups_capped"] == ft["groups_total"] - len(ft["groups"])
        assert ft["summary"]["coords"] >= 120
        assert "截断" in (ft["note"] or "")
    finally:
        conn.close()


def test_cap_dynamic_writers_and_coarse_helpers():
    """纯逻辑：dynamic_writers / coarse 裁剪保真实总数，未超 cap 时原样返回。"""
    dw = {"total": 30, "total_methods": 25, "methods": list(range(25)), "capped": 0}
    out = field_trace._cap_dynamic_writers(dw, 10)
    assert len(out["methods"]) == 10 and out["methods_capped"] == 15
    assert out["total_methods"] == 25  # 真实总数不动
    assert field_trace._cap_dynamic_writers({"methods": [1, 2]}, 10)["methods"] == [1, 2]  # 未超不动

    coarse = {"coarse_only": 50, "locations": list(range(50))}
    out = field_trace._cap_coarse(coarse, 20)
    assert len(out["locations"]) == 20 and out["locations_capped"] == 30
    assert out["coarse_only"] == 50
    assert field_trace._cap_coarse({"locations": [1]}, 20)["locations"] == [1]


# ── 游标分页（被 cap 的条目可逐页取回，红线 #4 从"报计数"升级为"可达"）──────────────
def test_parse_cursor():
    """游标解析：section@offset，缺/非法 offset 归 0。"""
    assert field_trace._parse_cursor("unlocated@5") == ("unlocated", 5)
    assert field_trace._parse_cursor("readers") == ("readers", 0)
    assert field_trace._parse_cursor("unlocated@x") == ("unlocated", 0)
    assert field_trace._parse_cursor("unlocated@-3") == ("unlocated", 0)


def _walk(conn, field, access, section, *, form_key="cqkd_assetcard", budget=None):
    """顺 next_cursor 翻到底，收集全部条目；断言每页 host 序列化 < 32768。"""
    cur = f"{section}@0"
    items: list = []
    pages = 0
    total = None
    while cur:
        kw = {} if budget is None else {"budget": budget}
        ft = field_trace.trace_compact(conn, field, form_key=form_key,
                                       access=access, cursor=cur, **kw)
        pg = ft["page"]
        assert "error" not in pg, pg
        total = pg["total"]
        items += pg["items"]
        pages += 1
        assert field_trace._wire_len(ft) < 32768
        cur = pg["next_cursor"]
        assert pages < 1000, "翻页未收敛"
    return items, total, pages


def test_pagination_overview_sets_next_cursor(tmp_path: Path):
    """被 cap 的段在 overview 里带 next_cursor，指明翻页游标。"""
    db = make_kb(tmp_path)
    rows = [_row("cqkd_collateralstatus", plugin_fqn=f"cqspb.pkg.UnlocatedPlugin{i}",
                 access="write", method=f"m{i}", line=i, form_key=None, via="do.set")
            for i in range(60)]
    _seed(db, rows)
    conn = store.open_kb(db)
    try:
        ft = field_trace.trace_compact(conn, "cqkd_collateralstatus",
                                       form_key="cqkd_assetcard", access="write")
        u = ft["unlocated"]
        if u["capped"]:  # 60 方法折叠后通常超预算被截
            assert u["next_cursor"] == f"unlocated@{len(u['methods'])}"
            assert "next_cursor" in (ft["note"] or "")
    finally:
        conn.close()


def test_pagination_retrieves_all_capped_unlocated(tmp_path: Path):
    """翻页能把被 cap 的 unlocated 条目**一条不漏**取全（消费方可达，非仅计数）。"""
    db = make_kb(tmp_path)
    rows = [_row("cqkd_collateralstatus", plugin_fqn=f"cqspb.pkg.UnlocatedPlugin{i}",
                 access="write", method=f"m{i}", line=i, form_key=None, via="do.set")
            for i in range(60)]
    _seed(db, rows)
    conn = store.open_kb(db)
    try:
        items, total, _ = _walk(conn, "cqkd_collateralstatus", "write", "unlocated")
        assert len(items) == total == 60        # 60 个不同 (类,方法) 全部取回
        fqns = {it["class_fqn"] for it in items}
        assert len(fqns) == 60
    finally:
        conn.close()


def test_pagination_multipage_chains_under_tiny_budget(tmp_path: Path):
    """极小 budget 强制多页：next_cursor 持续推进、收敛、零丢失。"""
    db = make_kb(tmp_path)
    rows = [_row("cqkd_collateralstatus", plugin_fqn=f"p.R{i}", method=f"读取方法{i}", line=i)
            for i in range(40)]
    _seed(db, rows)
    conn = store.open_kb(db)
    try:
        items, total, pages = _walk(conn, "cqkd_collateralstatus", "read", "readers",
                                    budget=2500)
        assert len(items) == total          # 全部读取类取回
        assert pages > 1                    # 确实分了多页
    finally:
        conn.close()


def test_pagination_writers_readers_needs_single_coordinate(tmp_path: Path):
    """writers/readers 是嵌套段：多坐标（裸字段命中多单据）时分页应报错引导先收窄。"""
    db = make_kb(tmp_path)
    rows = [_row("cqkd_collateralstatus", plugin_fqn=f"p.W{i}", access="write",
                 method="m", line=i, form_key=f"cqkd_form{i}")
            for i in range(5)]
    _seed(db, rows)
    conn = store.open_kb(db)
    try:
        ft = field_trace.trace_compact(conn, "cqkd_collateralstatus", cursor="writers@0")
        assert "error" in ft["page"] and "收窄" in ft["page"]["error"]
    finally:
        conn.close()


def test_pagination_unknown_section_errors():
    """未知 section → 报错列出可分页段名。"""
    m = {"field_key": "k", "field_name": None, "filter": {}, "precise": True,
         "unlocated": [], "possible": [], "occurrences": [], "group_list": [],
         "coarse": {}, "dynamic_writers": {}, "dynamic_writers_full": []}
    out = field_trace._page_section(m, None, "nope", 0, 1000)
    assert "error" in out["page"]
