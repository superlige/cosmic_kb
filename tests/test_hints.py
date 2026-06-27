"""模式 B 验收测试 —— 把"已核对字段名"与"事件→语义文档路由"焊进取证工具返回值。

起因：段二大模型读源码按命名惯例猜字段名、凭训练知识臆断事件触发时机/入库，软约束压不住。
模式 B 在导航工具（trace/bill/ask/method_calls）的返回里内联核对名 + semantics_topic，
让模型必定读到、想猜都没机会。本测覆盖：
  ① hints 纯映射（事件→主题、字段名索引、歧义留 None 不替选）；
  ② 四个工具返回值确实带上了 field_name / semantics_topic。
"""

from __future__ import annotations

from pathlib import Path

from cosmic_kb.context import builder
from cosmic_kb.graph import store
from cosmic_kb.report import bill_view, field_trace, method_calls
from cosmic_kb.semantic import hints, resolver

from _synthkb import make_kb


def _conn(tmp_path: Path):
    return store.open_kb(make_kb(tmp_path))


# ── ① hints 纯逻辑 ────────────────────────────────────────────────────────────
def test_event_topic_by_method_name():
    """事件方法名是强信号：propertyChanged→表单、beforeDoOperation→操作。"""
    assert hints.event_topic("propertyChanged") == "plugin-form"
    assert hints.event_topic("beforeDoOperation") == "plugin-operation"
    assert hints.event_topic("beforeExecuteOperationTransaction", "op") == "plugin-operation"


def test_event_topic_falls_back_to_plugin_type():
    """方法名兜不住时回落 plugin_type；都兜不住返回 None（不臆造路由）。"""
    assert hints.event_topic(None, "form") == "plugin-form"
    assert hints.event_topic("someRandomHelper", "list") == "plugin-list"
    assert hints.event_topic("someRandomHelper") is None
    assert hints.event_topic(None, None) is None


def test_semantics_pointer_text():
    p = hints.semantics_pointer("propertyChanged")
    assert p and "plugin-form" in p and "cosmic_semantics" in p
    assert hints.semantics_pointer("noSuchEvent") is None


def test_build_field_names_covers_field_and_entity(tmp_path: Path):
    """字段名索引覆盖 field 表 + entity 表（分录容器 key 也能解析）。"""
    conn = _conn(tmp_path)
    try:
        names = hints.build_field_names(conn)
        assert names.get("cqkd_collateralstatus") == "抵押状态"   # field 表
        assert names.get("cqkd_entry") == "资产明细"               # entity 表分录容器
        assert names.get("cqkd_nope") is None                      # 钉不出留 None
    finally:
        conn.close()


def test_field_names_ambiguous_returns_none():
    """同 key 跨单据多个不同名 → 无单据上下文时回 None（诚实留白，不替选）；给 form_key 则精确。"""
    fn = hints.FieldNames(by_form={("f1", "k"): "甲名", ("f2", "k"): "乙名"}, by_key={})
    assert fn.get("k") is None              # 全局歧义不替选
    assert fn.get("k", "f1") == "甲名"      # 给单据上下文则精确
    assert fn.get("k", "f9") is None        # 未知单据回落全局（无）→ None


# ── ② 四个工具返回值确实带 field_name / semantics_topic ─────────────────────────
def test_field_trace_carries_name_and_topic(tmp_path: Path):
    """trace：顶层 field_name 已核对名；写入行带事件语义路由。"""
    conn = _conn(tmp_path)
    try:
        ft = field_trace.field_trace(conn, "cqkd_collateralstatus")
        assert ft["field_name"] == "抵押状态"
        # 顶层扁平 writers 已删（与 groups 重复）；改从分组里取首个写入行核对语义路由。
        w = next(w for g in ft["groups"] for w in g["writers"])
        assert w["semantics_topic"] == "plugin-operation"
        text = field_trace.render_field_trace(ft)
        assert "cosmic_semantics('plugin-operation')" in text
    finally:
        conn.close()


def test_bill_view_field_touch_carries_name_and_topic(tmp_path: Path):
    """bill：field_touch 每个字段带真实中文名（曾只印裸 key 害模型猜）+ 事件语义路由。"""
    conn = _conn(tmp_path)
    try:
        bv = bill_view.bill_view(conn, "cqkd_assetcard")
        slot = bv["field_touch"]["cqkd_collateralstatus"]
        assert slot["field_name"] == "抵押状态"
        assert slot["events"][0]["semantics_topic"] == "plugin-operation"
        assert bv["field_touch"]["cqkd_amount"]["field_name"] == "金额"
        text = bill_view.render_bill(bv)
        assert "「抵押状态」" in text
    finally:
        conn.close()


def test_ask_plugin_explain_carries_name_and_topic(tmp_path: Path):
    """ask 插件解释：跨类写入行带字段名 + 事件语义路由。"""
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "CollateralService 这个类干嘛的？")
        ctx = builder.build_context(conn, rq)
        w = next(w for w in ctx["evidence"]["writes"] if w["field_key"] == "cqkd_collateralstatus")
        assert w["field_name"] == "抵押状态"
        assert w["semantics_topic"] == "plugin-operation"
    finally:
        conn.close()


def test_ask_operation_explain_carries_name(tmp_path: Path):
    """ask 操作解释：字段触达行带真实中文名。"""
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "cqkd_assetcard 这个 audit 操作按钮影响哪些字段？")
        ctx = builder.build_context(conn, rq)
        touched = ctx["evidence"]["field_access"]
        cs = next(t for t in touched if t["field_key"] == "cqkd_collateralstatus")
        assert cs["field_name"] == "抵押状态"
        assert cs["semantics_topic"] == "plugin-operation"
    finally:
        conn.close()


def test_method_calls_event_method_carries_topic(tmp_path: Path):
    """method_calls：被导航的方法若是事件回调，返回带 semantics_topic（解释它在干嘛前先核语义）。"""
    conn = _conn(tmp_path)
    try:
        rd = method_calls.method_calls(
            conn, "cqspb.assets.CollateralOp", "beforeExecuteOperationTransaction")
        # 该方法 KB 无源码（synth 不带真实 java），但 found=True 且语义路由仍应焊上。
        assert rd.get("semantics_topic") == "plugin-operation"
    finally:
        conn.close()
