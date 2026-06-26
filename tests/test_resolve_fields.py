"""字段名核对工具测试：标识 → 真实元数据中文名+坐标，钉不出回 None（不臆造）。

合成 KB（`_synthkb.make_kb`）已含所需样本：
- `cqkd_collateralstatus`「抵押状态」表头字段（单坐标）。
- `cqkd_amount`「金额」跨两单据（assetcard·entry + contract·header）→ 同 key 多坐标。
- `cqkd_entry`「资产明细」分录容器（在 entity 表，不在 field 表）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cosmic_kb.graph import store
from cosmic_kb.report import resolve_fields

from _synthkb import make_kb


@pytest.fixture()
def conn(tmp_path: Path):
    db = make_kb(tmp_path)
    c = store.open_kb(db)
    yield c
    c.close()


def test_resolve_single_field_name(conn):
    """① 字段名解析：抵押状态原样返回，不靠命名惯例。"""
    r = resolve_fields.resolve_fields(conn, ["cqkd_collateralstatus"])["resolved"]
    items = r["cqkd_collateralstatus"]
    assert items and len(items) == 1
    it = items[0]
    assert it["kind"] == "field" and it["name"] == "抵押状态"
    assert it["form_key"] == "cqkd_assetcard" and it["level"] == "header"


def test_resolve_same_key_multi_coords(conn):
    """② 同 key 跨多坐标：金额在两张单各一份，全摆出不替选。"""
    items = resolve_fields.resolve_fields(conn, ["cqkd_amount"])["resolved"]["cqkd_amount"]
    coords = {(it["form_key"], it["level"]) for it in items}
    assert coords == {("cqkd_assetcard", "entry"), ("cqkd_contract", "header")}
    assert all(it["name"] == "金额" for it in items)


def test_resolve_entry_container_key(conn):
    """③ 分录容器 key（entity 表）：cqkd_entry 能解析，kind=entry + parent_key。"""
    items = resolve_fields.resolve_fields(conn, ["cqkd_entry"])["resolved"]["cqkd_entry"]
    assert items and len(items) == 1
    it = items[0]
    assert it["kind"] == "entry" and it["name"] == "资产明细"
    assert it["form_key"] == "cqkd_assetcard" and it["parent_key"] == "cqkd_assetcard"


def test_resolve_unknown_returns_none(conn):
    """④ 钉不出回 None（诚实留白，不臆造）。"""
    r = resolve_fields.resolve_fields(conn, ["cqkd_nope"])["resolved"]
    assert r["cqkd_nope"] is None


def test_resolve_batch_mixed(conn):
    """批量混合：一次传字段+容器+未知，各自正确归位。"""
    r = resolve_fields.resolve_fields(
        conn, ["cqkd_collateralstatus", "cqkd_entry", "cqkd_nope"])["resolved"]
    assert r["cqkd_collateralstatus"][0]["kind"] == "field"
    assert r["cqkd_entry"][0]["kind"] == "entry"
    assert r["cqkd_nope"] is None


def test_render_marks_null(conn):
    """文本视图明确标 null（提示标 unknown 勿猜），命中列坐标。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_collateralstatus", "cqkd_nope"])
    text = resolve_fields.render_resolve_fields(d)
    assert "抵押状态" in text
    assert "null" in text and "cqkd_nope" in text


def test_mcp_tool_same_as_report(tmp_path: Path, monkeypatch):
    """⑤ MCP 工具与 report 函数同口径（不重写取证逻辑）。"""
    from cosmic_kb.mcp import server as mcp_server

    db = make_kb(tmp_path)
    monkeypatch.setenv("COSMIC_KB_DB", str(db))
    got = mcp_server.tool_resolve_fields(["cqkd_amount", "cqkd_entry"])
    c = store.open_kb(db)
    try:
        want = resolve_fields.resolve_fields(c, ["cqkd_amount", "cqkd_entry"])
    finally:
        c.close()
    assert got == want


def test_cli_resolve_json(tmp_path: Path, capsys):
    """CLI resolve --json 跑通，输出含解析结果。"""
    from cosmic_kb.cli.main import main

    db = make_kb(tmp_path)
    rc = main(["resolve", "cqkd_collateralstatus", "cqkd_nope", "--db", str(db), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "抵押状态" in out and "resolved" in out
