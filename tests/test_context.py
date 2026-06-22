"""阶段9 验收测试 —— Context Builder（context/builder.py）。

覆盖：三类意图证据包组装（字段谁改的 / 单据钻取 / 插件解释 / 操作解释）、反问透传、
unknown 不臆造（无命中字段时如实说没找到、不编字段名）。
"""

from __future__ import annotations

from pathlib import Path

from cosmic_kb.context import builder
from cosmic_kb.graph import store
from cosmic_kb.semantic import resolver

from _synthkb import make_kb


def _conn(tmp_path: Path):
    return store.open_kb(make_kb(tmp_path))


def test_ctx_field_who_changed(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "cqkd_collateralstatus")
        ctx = builder.build_context(conn, rq)
        assert ctx["status"] == "ok"
        assert ctx["intent"] == "field_who_changed"
        ev = ctx["evidence"]
        # 旗舰证据：CollateralService 跨类写入、落库 yes、带行号。
        assert ev["summary"]["writers"] >= 1
        assert ev["summary"]["persisting_writers"] >= 1
        w = ev["writers"][0]
        assert w["access_class"].endswith("CollateralService")
        assert w["line"] == 41
        # 渲染不报错且含字段标识。
        text = builder.render_context(ctx)
        assert "cqkd_collateralstatus" in text
    finally:
        conn.close()


def test_ctx_field_not_found_no_fabrication(tmp_path: Path):
    """字段在元数据里但无任何插件读写 → 如实说没找到，不编造写入点。"""
    conn = _conn(tmp_path)
    try:
        # cqkd_amount 在 contract 上无 write 记录（只有 assetcard.entry 的 read）。
        from cosmic_kb.semantic.resolver import ResolvedQuery
        rq = ResolvedQuery("field_who_changed", "x", confidence=1.0,
                           field_key="cqkd_amount", form_key="cqkd_contract", level="header")
        ctx = builder.build_context(conn, rq)
        assert ctx["status"] == "ok"
        # 该精确坐标无确定写入；advice 不得凭空给出写入点。
        assert ctx["evidence"]["summary"]["writers"] == 0
        assert any("没有" in a or "未找到" in a or "无确定" in a or "可能命中" in a or "未定位" in a
                   for a in ctx["advice"])
    finally:
        conn.close()


def test_ctx_bill_drilldown(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "cqkd_assetcard 这张单有哪些操作和插件？")
        ctx = builder.build_context(conn, rq)
        assert ctx["status"] == "ok"
        assert ctx["intent"] == "bill_drilldown"
        assert ctx["evidence"]["form"]["key"] == "cqkd_assetcard"
        # 有自定义插件的操作 audit 应被点名。
        assert any("audit" in a for a in ctx["advice"])
    finally:
        conn.close()


def test_ctx_plugin_explain(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "CollateralService 这个类干嘛的？")
        ctx = builder.build_context(conn, rq)
        assert ctx["status"] == "ok"
        ev = ctx["evidence"]
        # 孤儿 service：无注册、但跨类写了 collateralstatus 并落库。
        assert ev["summary"]["registrations"] == 0
        wkeys = {w["field_key"] for w in ev["writes"]}
        assert "cqkd_collateralstatus" in wkeys
        text = builder.render_context(ctx)
        assert "CollateralService" in text
    finally:
        conn.close()


def test_ctx_operation_explain(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "cqkd_assetcard 这个 audit 操作按钮影响哪些字段？")
        ctx = builder.build_context(conn, rq)
        assert ctx["status"] == "ok"
        ev = ctx["evidence"]
        assert ev["operation"]["key"] == "audit"
        assert any(p["class_name"].endswith("CollateralOp") for p in ev["plugins"])
    finally:
        conn.close()


def test_ctx_need_clarification(tmp_path: Path):
    """同名歧义 → 证据包退化为消歧菜单，不强答。"""
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "金额是谁改的？")
        ctx = builder.build_context(conn, rq)
        assert ctx["status"] == "need_clarification"
        assert len(ctx["candidates"]) >= 2
        text = builder.render_context(ctx)
        assert "消歧" in text or "候选" in text
    finally:
        conn.close()
