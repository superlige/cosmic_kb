"""扩展别名重定向提示（report/field_trace.py + report/bill_view.py）验收测试。

`metadata/merge.py::build_extension_alias` 产出的"空壳"表单行（`is_extension=1`,
`extends=<原厂 form_key>`）查询时内容必然为空——本测试确认 trace/bill 会给出重定向提示，
而不是让人误以为"这单据没被扫到"。用 `_synthkb` 合成 KB 之上叠一条别名行，不跑重型管线。
"""

from __future__ import annotations

from cosmic_kb.graph import store
from cosmic_kb.report import bill_view, field_trace

from tests._synthkb import make_kb


def _add_extension_alias(db_path):
    conn = store.open_kb(db_path)
    conn.execute(
        "INSERT INTO form(key,name,form_type,model_type,isv,app_key,module,source_dym,"
        "is_extension,extends) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("cqkd_bd_customer_ext", None, "basedata", "BaseFormModel", "cqkd", None, None,
         "a.dym", 1, "bd_customer"),
    )
    conn.commit()
    conn.close()


def test_bill_view_notes_extension_alias_redirect(tmp_path):
    db = make_kb(tmp_path)
    _add_extension_alias(db)
    conn = store.open_kb(db)
    bv = bill_view.bill_view(conn, "cqkd_bd_customer_ext")
    assert bv is not None
    assert bv["note"] is not None
    assert "bd_customer" in bv["note"]
    assert "cqkd_bd_customer_ext" in bv["note"]
    # 非别名单据不受影响，note 仍是 None。
    bv2 = bill_view.bill_view(conn, "cqkd_assetcard")
    assert bv2["note"] is None


def test_bill_compact_carries_redirect_note_first(tmp_path):
    db = make_kb(tmp_path)
    _add_extension_alias(db)
    conn = store.open_kb(db)
    compact = bill_view.bill_compact(conn, "cqkd_bd_customer_ext")
    assert compact["note"].startswith("⚑")
    assert "bd_customer" in compact["note"]


def test_render_bill_text_includes_redirect(tmp_path):
    db = make_kb(tmp_path)
    _add_extension_alias(db)
    conn = store.open_kb(db)
    bv = bill_view.bill_view(conn, "cqkd_bd_customer_ext")
    text = bill_view.render_bill(bv)
    assert "bd_customer" in text


def test_field_trace_notes_extension_alias_redirect(tmp_path):
    db = make_kb(tmp_path)
    _add_extension_alias(db)
    conn = store.open_kb(db)
    ft = field_trace.field_trace(conn, "cqkd_x", form_key="cqkd_bd_customer_ext")
    assert ft["note"] is not None
    assert "bd_customer" in ft["note"]
    assert "cqkd_bd_customer_ext" in ft["note"]


def test_field_trace_compact_notes_extension_alias_redirect(tmp_path):
    db = make_kb(tmp_path)
    _add_extension_alias(db)
    conn = store.open_kb(db)
    compact = field_trace.trace_compact(conn, "cqkd_x", form_key="cqkd_bd_customer_ext")
    assert compact["note"] is not None
    assert "bd_customer" in compact["note"]


def test_field_trace_bare_query_ambiguous_and_not_polluted_by_alias(tmp_path):
    """cqkd_amount 跨单据定义，裸查询触发消歧（不会跑到扩展别名判断那段代码）；
    单坐标精确查询的 note 也不会被别名机制污染。"""
    db = make_kb(tmp_path)
    _add_extension_alias(db)
    conn = store.open_kb(db)
    ft = field_trace.field_trace(conn, "cqkd_amount")
    assert ft["status"] == "need_clarification"
    assert "扩展别名" not in (ft.get("note") or "")

    ftp = field_trace.field_trace(conn, "cqkd_amount", form_key="cqkd_assetcard")
    assert ftp["note"] is None or "扩展别名" not in ftp["note"]
