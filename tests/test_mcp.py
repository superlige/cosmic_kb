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


def test_tool_trace_same_as_report(kb_env):
    """tool_trace 走紧凑投影 trace_compact（防 host 截断），与 report 同口径、零重写。"""
    from cosmic_kb.report import field_trace

    got = mcp_server.tool_trace("cqkd_assetcard.cqkd_collateralstatus")
    conn = store.open_kb(kb_env)
    try:
        fk, form_key, entry_key, lvl = field_trace.parse_locator(
            "cqkd_assetcard.cqkd_collateralstatus")
        want = field_trace.trace_compact(
            conn, fk, form_key=form_key, entry_key=entry_key, level=lvl)
    finally:
        conn.close()
    assert got == want


def test_tool_bill_handles_null_field_key(kb_env):
    """单据钻取（紧凑投影）：即使有 field_key=NULL 的未定位访问也不崩，且不混进字段触达清单。"""
    conn = sqlite3.connect(str(kb_env))
    try:
        conn.execute(
            "INSERT INTO field_access(form_key,field_key,level,access,persists,plugin_fqn,"
            "event_method,line) VALUES('cqkd_assetcard',NULL,'header','write','unknown',"
            "'cqkd.x.SomeOp','endOperationTransaction',1)")
        conn.commit()
    finally:
        conn.close()

    # 默认 profile="overview" 瘦身：entity_touch/fields 都不带出（有专职工具顶替）。
    bv_overview = mcp_server.tool_bill("cqkd_assetcard")
    assert "entity_touch" not in bv_overview and "fields" not in bv_overview

    bv = mcp_server.tool_bill("cqkd_assetcard", profile="full")
    # 紧凑投影：逐字段事件已折叠为计数，按实体分组的 entity_touch 取代扁平 field_touch。
    assert "entity_touch" in bv and "field_touch" not in bv
    touched = [f["field_key"] for et in bv["entity_touch"] for f in et["fields"]]
    assert None not in touched                          # NULL field_key 不该混进触达清单
    assert "cqkd_collateralstatus" in touched           # 已定位写入仍在


def test_tool_bill_compact_under_budget_and_pages(kb_env):
    """tool_bill 走紧凑投影 bill_compact（防 host 32KB 截断）；与 report 同口径、支持游标分页。"""
    from cosmic_kb.report import bill_view, field_trace

    got = mcp_server.tool_bill("cqkd_assetcard")
    conn = store.open_kb(kb_env)
    try:
        want = bill_view.bill_compact(conn, "cqkd_assetcard")
    finally:
        conn.close()
    assert got == want
    assert field_trace._wire_len(got) <= field_trace._COMPACT_BUDGET
    # 分页：把某段游标喂回 tool_bill 应返回聚焦页（page.items + next_cursor）。
    pg = mcp_server.tool_bill("cqkd_assetcard", cursor="fields@0")
    assert pg["page"]["section"] == "fields"
    assert "items" in pg["page"]


def test_tool_bill_missing_form(kb_env):
    assert "error" in mcp_server.tool_bill("cqkd_nope")


def test_open_raises_when_kb_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COSMIC_KB_DB", str(tmp_path / "nope.db"))
    with pytest.raises(RuntimeError):
        mcp_server.tool_trace("cqkd_collateralstatus")
    with pytest.raises(RuntimeError):
        mcp_server.tool_bill("cqkd_assetcard")


def test_tools_registry_matches():
    """TOOLS 注册表收敛到 4 个排障核心工具（read_source/method_calls/ask 均已整体退役，
    防漏注册）。"""
    assert set(mcp_server.TOOLS) == {
        "trace", "bill",
        "resolve_fields", "cosmic_semantics"}
    for fn in mcp_server.TOOLS.values():
        assert callable(fn)


def test_tool_resolve_fields_qualified_exact_match(kb_env):
    """`"form_key.field_key"` 复合 key：cqkd_amount 跨 cqkd_assetcard/cqkd_contract 两单同名，
    带上 cqkd_contract 限定符应精确收敛到那一条，不是不带限定符时的全部候选。"""
    got = mcp_server.tool_resolve_fields(["cqkd_contract.cqkd_amount", "cqkd_amount"])
    qualified = got["resolved"]["cqkd_contract.cqkd_amount"]
    assert qualified is not None
    assert {it["form_key"] for it in qualified} == {"cqkd_contract"}
    # 不带限定符仍是老行为：两张单据的候选都摆出，不因为出现过复合 key 调用而变化。
    assert {it["form_key"] for it in got["resolved"]["cqkd_amount"]} == {
        "cqkd_assetcard", "cqkd_contract"}
    assert "mismatched_form" not in got


def test_tool_resolve_fields_qualified_mismatch_is_honest(kb_env):
    """限定符是真实存在的单据，但该单据下没有这个字段：不悄悄回退成全部候选掩盖问题，
    而是诚实报告 mismatched_form，指出该字段实际所在的单据（红线 #4）。"""
    got = mcp_server.tool_resolve_fields(["cqkd_contract.cqkd_collateralstatus"])
    mm = got.get("mismatched_form", {}).get("cqkd_contract.cqkd_collateralstatus")
    assert mm is not None
    assert mm["given_form"] == "cqkd_contract"
    assert mm["available_forms"] == ["cqkd_assetcard"]
    # 即便限定符不对，仍给出全局候选（不是 null），方便模型看出自己的假设错在哪。
    resolved = got["resolved"]["cqkd_contract.cqkd_collateralstatus"]
    assert resolved and resolved[0]["form_key"] == "cqkd_assetcard"


def test_tool_resolve_fields_entry_and_three_part_qualifier(kb_env):
    """真实排障复盘：模型照搬 trace 的点号坐标写法传 `"分录.字段"`/`"单据.分录.字段"`，
    MCP 工具与 report 层同口径都应精确收敛，不再全部落空返回 null。"""
    got = mcp_server.tool_resolve_fields(
        ["cqkd_entry.cqkd_amount", "cqkd_assetcard.cqkd_entry.cqkd_amount"])
    entry_only = got["resolved"]["cqkd_entry.cqkd_amount"]
    three_part = got["resolved"]["cqkd_assetcard.cqkd_entry.cqkd_amount"]
    assert entry_only and len(entry_only) == 1
    assert entry_only[0]["form_key"] == "cqkd_assetcard" and entry_only[0]["level"] == "entry"
    assert three_part and len(three_part) == 1
    assert three_part[0]["form_key"] == "cqkd_assetcard" and three_part[0]["level"] == "entry"


def test_tool_resolve_fields_resolves_form_name(kb_env):
    """纯表单标识（无对应字段/实体记录，如真实排障中的 cqkd_invoic_apply）：kind=form，
    与 report 层同口径（2026-07-05 复盘：此前模型对这类标识无工具可查，只能凭字面翻译）。"""
    conn = sqlite3.connect(str(kb_env))
    conn.execute(
        "INSERT INTO form(key,name,form_type,model_type,isv,app_key,module,source_dym) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("cqkd_invoic_apply", "开票申请单", "bill", "BillFormModel", "cqkd", "cqkd_assets",
         "cqkd_assets", "i.dym"),
    )
    conn.commit()
    conn.close()

    got = mcp_server.tool_resolve_fields(["cqkd_invoic_apply"])
    items = got["resolved"]["cqkd_invoic_apply"]
    assert items and len(items) == 1
    assert items[0] == {
        "kind": "form", "name": "开票申请单",
        "form_key": "cqkd_invoic_apply", "form_type": "bill",
    }


def test_audit_tools_not_exposed_to_mcp():
    """三个审计工具（coverage/scan_compare/dynamic_writes）只留 CLI，不再注册到 MCP（防回归混入）。
    其纯逻辑由 report.* 直连 + test_coverage/test_scan_compare 专属套件覆盖，无测试缺口。"""
    for name in ("coverage", "scan_compare", "dynamic_writes"):
        assert name not in mcp_server.TOOLS
        assert not hasattr(mcp_server, f"tool_{name}")


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
    assert "content" in mcp_server.tool_cosmic_semantics("plugin-form")


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
