"""段二 MCP 接入测试：工具返回值与 CLI/取证函数同口径，且不依赖 mcp 包即可验证纯逻辑。

重点：
- `tool_*` 纯逻辑函数能直接调（不装 [mcp] 也能跑），返回与段一取证函数完全一致的证据包。
- KB 缺失时清晰报错（不返回空结果骗 LLM）。
- `tool_bill` 不被 NULL field_key 噎住（对齐 bill_view 的 field_key IS NOT NULL 过滤）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cosmic_kb.graph import store
from cosmic_kb.mcp import server as mcp_server

from _synthkb import make_kb


@pytest.fixture()
def kb_env(tmp_path: Path, monkeypatch):
    """建合成 KB 并把 COSMIC_KB_DB 指过去（MCP 工具按此环境变量开库）。"""
    db = make_kb(tmp_path)
    monkeypatch.setenv("COSMIC_KB_DB", str(db))
    return db


def test_tool_ask_same_as_builder(kb_env):
    """tool_ask == resolver.resolve + builder.build_context（不重写取证逻辑）。"""
    from cosmic_kb.semantic import resolver
    from cosmic_kb.context import builder

    got = mcp_server.tool_ask("cqkd_collateralstatus")
    conn = store.open_kb(kb_env)
    try:
        want = builder.build_context(conn, resolver.resolve(conn, "cqkd_collateralstatus"))
    finally:
        conn.close()
    assert got == want
    assert got["intent"] == "field_who_changed"


def test_tool_ask_clarification(kb_env):
    """同名字段跨单据 → 证据包退化为消歧候选，绝不替用户拍板。"""
    got = mcp_server.tool_ask("金额是谁改的？")
    assert got.get("status") == "need_clarification"
    assert got.get("candidates")


def test_tool_trace_same_as_report(kb_env):
    from cosmic_kb.report import field_trace

    got = mcp_server.tool_trace("cqkd_assetcard.cqkd_collateralstatus")
    conn = store.open_kb(kb_env)
    try:
        fk, form_key, entry_key, lvl = field_trace.parse_locator(
            "cqkd_assetcard.cqkd_collateralstatus")
        want = field_trace.field_trace(
            conn, fk, form_key=form_key, entry_key=entry_key, level=lvl)
    finally:
        conn.close()
    assert got == want


def test_tool_bill_handles_null_field_key(kb_env):
    """单据钻取：即使有 field_key=NULL 的未定位访问也不崩（回归保护）。"""
    conn = sqlite3.connect(str(kb_env))
    try:
        conn.execute(
            "INSERT INTO field_access(form_key,field_key,level,access,persists,plugin_fqn,"
            "event_method,line) VALUES('cqkd_assetcard',NULL,'header','write','unknown',"
            "'cqkd.x.SomeOp','endOperationTransaction',1)")
        conn.commit()
    finally:
        conn.close()

    bv = mcp_server.tool_bill("cqkd_assetcard")
    assert "field_touch" in bv
    # NULL field_key 不该混进按字段聚合的触达清单。
    assert None not in bv["field_touch"]


def test_tool_bill_missing_form(kb_env):
    assert "error" in mcp_server.tool_bill("cqkd_nope")


def test_tool_coverage_and_scan_compare(kb_env):
    assert isinstance(mcp_server.tool_coverage(), dict)
    assert isinstance(mcp_server.tool_scan_compare(), dict)


def test_open_raises_when_kb_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COSMIC_KB_DB", str(tmp_path / "nope.db"))
    with pytest.raises(RuntimeError):
        mcp_server.tool_ask("cqkd_collateralstatus")


def test_tools_registry_matches():
    """TOOLS 注册表与公开 tool_* 函数一一对应（防漏注册）。"""
    assert set(mcp_server.TOOLS) == {
        "ask", "trace", "bill", "method_calls", "coverage", "scan_compare",
        "dynamic_writes", "cosmic_semantics"}
    for fn in mcp_server.TOOLS.values():
        assert callable(fn)


def test_tool_cosmic_semantics_lists_topics():
    """空 topic → 返回按组分桶的可选主题清单（不依赖 KB / mcp 包）。"""
    got = mcp_server.tool_cosmic_semantics("")
    assert got.get("status") == "need_topic"
    assert got["available_topics"], "应能枚举随包语义主题"
    assert isinstance(got["grouped"], dict)


def test_tool_cosmic_semantics_reads_topic():
    """命中 topic（文件名 stem）→ 返回单篇 markdown 全文。"""
    got = mcp_server.tool_cosmic_semantics("anti-patterns")
    assert "content" in got and got["content"].strip()
    # 模糊到插件文档也应命中
    assert "content" in mcp_server.tool_cosmic_semantics("plugin-base")


def test_build_server_carries_instructions():
    """语义下沉：装了 [mcp] 时 server 带非空 instructions（跨 agent 注入苍穹纪律）。"""
    pytest.importorskip("mcp")
    srv = mcp_server.build_server()
    assert srv.instructions and "苍穹" in srv.instructions


def test_cli_mcp_kb_missing(tmp_path: Path):
    """cosmic_kb mcp：KB 缺失时退出码 2，不空转。"""
    from cosmic_kb.cli.main import main

    rc = main(["mcp", "--db", str(tmp_path / "nope.db")])
    assert rc == 2


def test_build_server_when_mcp_installed():
    """装了 [mcp] 才构造服务器；没装则跳过（纯逻辑已被前面用例覆盖）。"""
    pytest.importorskip("mcp")
    srv = mcp_server.build_server()
    assert srv is not None
