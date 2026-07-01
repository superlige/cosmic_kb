"""bill 轴 A · 场景/插件类型分流测试。

顾问真实排障是「动作优先」：把单据绑定插件先按场景（操作/界面/列表/反写/转换）切开。本测试
验证 `bill_view` 组装的 `plugin_lanes`（车道顺序 = 排障优先级、空车道不出现、词表外归 other 不吞、
语义文档路由正确），以及 `bill_compact` 的轻量车道索引不冲击 32KB 预算、平铺 plugins 段仍在。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cosmic_kb.graph import store
from cosmic_kb.report import bill_view as B
from cosmic_kb.report.field_trace import _wire_len, _COMPACT_BUDGET

from _synthkb import make_kb


def _add_plugins(db: Path) -> None:
    """给 cqkd_assetcard 补插 form/list/convert + 一个词表外类型（xyz），凑齐多车道。

    合成库现成只有一个 op 插件（CollateralOp）。这里补足以覆盖：主力车道 form、其他单据绑定车道
    list/convert、以及词表外 plugin_type（验证归 other 兜底不被吞）。writeback 故意不加——验证空车道不出现。
    """
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            "INSERT INTO plugin(uid,form_key,class_name,plugin_type,source,operation_key,operation_name) "
            "VALUES(?,?,?,?,?,?,?)",
            [
                ("p_form", "cqkd_assetcard", "cqspb.assets.CardFormPlugin", "form", "project", None, None),
                ("p_list", "cqkd_assetcard", "cqspb.assets.CardListPlugin", "list", "project", None, None),
                ("p_conv", "cqkd_assetcard", "cqspb.assets.CardConvertPlugin", "convert", "project", None, None),
                ("p_xyz", "cqkd_assetcard", "cqspb.assets.WeirdPlugin", "xyz", "project", None, None),
                # 平台预制 kd.bos.*（source=platform）：应被排除在车道外、只计数。
                ("p_plat", "cqkd_assetcard", "kd.bos.business.plugin.CodeRuleOp", "op", "platform",
                 "save", "保存"),
            ],
        )
        # 给 form 插件一条 missing binding，验证 binding_risk 挂上。
        conn.execute(
            "INSERT INTO binding(class_name,form_key,plugin_type,status,source_relpath,confidence,note) "
            "VALUES(?,?,?,?,?,?,?)",
            ("cqspb.assets.CardFormPlugin", "cqkd_assetcard", "form", "missing", None, 0.0, "找不到源码"),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def kb(tmp_path: Path) -> Path:
    db = make_kb(tmp_path)
    _add_plugins(db)
    return db


def _conn(db: Path):
    return store.open_kb(db)


def test_lane_order_and_empty_omitted(kb):
    """车道按排障优先级 op→form→list→convert→other；writeback 无插件不出现。"""
    conn = _conn(kb)
    try:
        bv = B.bill_view(conn, "cqkd_assetcard")
    finally:
        conn.close()
    lanes = bv["plugin_lanes"]
    ids = [ln["lane_id"] for ln in lanes]
    assert ids == ["operation", "form", "list", "convert", "other"]
    assert "writeback" not in ids            # 空车道不出现


def test_lane_semantics_and_membership(kb):
    """op/form 车道 label + 语义文档路由正确，且插件归位到对应车道。"""
    conn = _conn(kb)
    try:
        bv = B.bill_view(conn, "cqkd_assetcard")
    finally:
        conn.close()
    by_id = {ln["lane_id"]: ln for ln in bv["plugin_lanes"]}

    op = by_id["operation"]
    assert op["label"] == "操作插件"
    assert op["semantics_topic"] == "plugin-operation"
    assert op["count"] == 1
    assert any(p["class_name"] == "cqspb.assets.CollateralOp" for p in op["plugins"])
    # op 插件绑定的 operation_key 透传（供轴 B 用；轴 A 只呈现不分组）。
    assert op["plugins"][0]["operation_key"] == "audit"

    form = by_id["form"]
    assert form["label"] == "界面插件"
    assert form["semantics_topic"] == "plugin-form"
    # form 插件的 missing binding → binding_risk 标出。
    assert form["plugins"][0]["binding_risk"] == "missing"


def test_unknown_type_bucketed_to_other(kb):
    """词表外 plugin_type（xyz）归 other 车道兜底，不被吞。"""
    conn = _conn(kb)
    try:
        bv = B.bill_view(conn, "cqkd_assetcard")
    finally:
        conn.close()
    by_id = {ln["lane_id"]: ln for ln in bv["plugin_lanes"]}
    assert "other" in by_id
    other = by_id["other"]
    assert other["label"] == "其他插件"
    assert other["semantics_topic"] is None
    assert any(p["class_name"] == "cqspb.assets.WeirdPlugin" for p in other["plugins"])


def test_platform_plugins_excluded_from_lanes(kb):
    """平台预制 kd.bos.*（source=platform）不进车道，但计数诚实呈现（不静默丢）。"""
    conn = _conn(kb)
    try:
        bv = B.bill_view(conn, "cqkd_assetcard")
    finally:
        conn.close()
    # op 车道只含项目 CollateralOp，平台 CodeRuleOp 不在。
    op = next(ln for ln in bv["plugin_lanes"] if ln["lane_id"] == "operation")
    classes = {p["class_name"] for p in op["plugins"]}
    assert "cqspb.assets.CollateralOp" in classes
    assert "kd.bos.business.plugin.CodeRuleOp" not in classes
    # 任何车道都不含 kd.bos.* 前缀插件。
    for ln in bv["plugin_lanes"]:
        assert all(not p["class_name"].startswith("kd.bos.") for p in ln["plugins"])
    # 排除计数诚实呈现。
    assert bv["platform_plugins_excluded"] == 1
    # 平铺 plugins 仍保留平台插件（完整清单不删）。
    assert any(p["class_name"] == "kd.bos.business.plugin.CodeRuleOp" for p in bv["plugins"])


def test_compact_lane_index_is_light(kb):
    """bill_compact 带轻量车道索引（只 count/topic，无逐插件行），平铺 plugins 段仍在，字节不超预算。"""
    conn = _conn(kb)
    try:
        res = B.bill_compact(conn, "cqkd_assetcard")
    finally:
        conn.close()
    assert "plugin_lanes" in res
    for ln in res["plugin_lanes"]:
        assert set(ln) == {"lane_id", "label", "semantic", "semantics_topic", "count"}
        assert "plugins" not in ln           # 轻量：不复制逐插件行
    assert "plugins" in res and res["plugins"]   # 逐插件明细仍在平铺段
    assert _wire_len(res) <= _COMPACT_BUDGET


def test_render_bill_text_has_lanes(kb):
    """render_bill 文本按车道分组，含各车道 label + 语义文档路由。"""
    conn = _conn(kb)
    try:
        bv = B.bill_view(conn, "cqkd_assetcard")
    finally:
        conn.close()
    text = B.render_bill(bv)
    assert "按场景分流" in text
    assert "操作插件" in text and "界面插件" in text
    assert "cosmic_semantics('plugin-operation')" in text
    assert "⚠missing" in text               # form 插件 binding 风险标出
    assert "平台预制插件 kd.bos.*" in text   # 平台插件折叠计数诚实呈现
