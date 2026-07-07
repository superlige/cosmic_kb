"""事件→语义文档路由验收测试 —— 把 semantics_topic 焊进取证工具返回值。

起因：段二大模型读源码凭训练知识臆断事件触发时机/入库，软约束压不住。导航工具
（trace/bill）返回里内联 semantics_topic，让模型必定读到、想猜都没机会。

字段中文名自动标注（原 hints.FieldNames/build_field_names，曾一并焊进这些工具返回值）已于
2026-07-05 随 MCP `read_source` 工具退役一起砍掉——那是全局候选盲扫，非精确定位，与本文件覆盖
的 semantics_topic（确定性映射，无歧义风险）是两个不同问题。`trace`/`bill` 的 `field_name`
是按精确坐标查出的，风险类别不同，予以保留，仍在本测覆盖范围。字段名核对统一改走
`resolve_fields`（`test_resolve_fields.py` 覆盖）。

`ask` 命令 + 其依附的 `semantic.resolver`/`context.builder` 已于 2026-07 整体退役
（`semantics_topic` 现在只覆盖 trace/bill，不再有 ask 的 plugin/operation 证据路径）。
"""

from __future__ import annotations

from pathlib import Path

from cosmic_kb.graph import store
from cosmic_kb.report import bill_view, field_trace
from cosmic_kb.semantic import hints

from _synthkb import make_kb


def _conn(tmp_path: Path):
    return store.open_kb(make_kb(tmp_path))


# ── ① hints 纯逻辑 ────────────────────────────────────────────────────────────
def test_event_topic_by_method_name():
    """事件方法名是强信号：propertyChanged→表单。beforeDoOperation 不再硬编码默认值——
    它是表单插件界面侧回调，不是操作插件事务事件，不传 plugin_type 时无法确定种类，
    诚实返回 None（不臆造）。"""
    assert hints.event_topic("propertyChanged") == "plugin-form"
    assert hints.event_topic("beforeDoOperation") is None
    assert hints.event_topic("beforeExecuteOperationTransaction", "op") == "plugin-operation"


def test_event_topic_tier0_authoritative_by_plugin_type():
    """Tier 0：plugin_type 落在 event_extractor.EVENT_TABLE 时用 classify_method 权威校验，
    解决 beforeDoOperation/afterDoOperation（表单插件回调，从未在操作插件事务事件表里）、
    itemClick（表单工具栏 vs 列表工具栏语义不同）这两类方法名相同但种类不同的误标。"""
    assert hints.event_topic("afterDoOperation", "form") == "plugin-form"
    assert hints.event_topic("itemClick", "form") == "plugin-form"
    assert hints.event_topic("itemClick", "list") == "plugin-list"
    # op 种类没有 afterDoOperation 这个生命周期事件（不在 _OP 表），Tier 0 不命中，
    # 回落到方法名表（已删）、再回落到 plugin_type 表——诚实但也说明这个组合不该真实出现。
    assert hints.event_topic("validate", "validator") == "plugin-operation"


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


# ── ② 工具返回值带 semantics_topic + field_name ──────────────────────────────
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
