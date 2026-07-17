"""callers 入口可达性/死代码判定升级验收（entry_analysis 字段）。

覆盖 `cosmic_kb/report/entry_chain.py::registration_status`（元数据/孤儿插件三态注册反查）
与 `cosmic_kb/report/callers.py::entry_analysis`（verdict 五值 + 降级铁律）。合成 KB 沿用
`_synthkb.make_kb` 打底（CollateralOp 绑定 cqkd_assetcard.audit 的 op 插件、
CollateralService 是其孤儿 helper），叠加各用例专属的 plugin/convert_rule/source_class/
plugin_method/call_edge 行，做法与 `test_op_trigger.py` 的 chain_kb 一致。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cosmic_kb.graph import store
from cosmic_kb.mcp import server as mcp_server
from cosmic_kb.report import callers as callers_report
from cosmic_kb.report import entry_chain

from _synthkb import make_kb


def _exec_rows(db: Path, sql: str, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(sql, rows)
        conn.commit()
    finally:
        conn.close()


def _conn(db: Path):
    return store.open_kb(db)


# ── 用例 1：self_entry + 启用 ──────────────────────────────────────────────

def test_self_entry_enabled_is_reachable_confirmed(tmp_path: Path):
    db = make_kb(tmp_path)
    conn = _conn(db)
    try:
        result = callers_report.callers(conn, "CollateralOp.beforeExecuteOperationTransaction")
        ea = result["entry_analysis"]
        assert ea["verdict"] == "entry_reachable"
        assert ea["confidence"] == "confirmed"
        assert ea["chain_status"] == "self_entry"
        assert len(ea["entries"]) == 1
        assert ea["entries"][0]["terminal"] == "self"
        assert ea["entries"][0]["registration"]["status"] == "registered_enabled"
        assert "入口可达性" in result["note"]
    finally:
        conn.close()


# ── 用例 2：reached + enabled（现成素材：CollateralService.update）─────────

def test_reached_enabled_dedupes_entries_by_class(tmp_path: Path):
    db = make_kb(tmp_path)
    conn = _conn(db)
    try:
        result = callers_report.callers(conn, "CollateralService.update")
        ea = result["entry_analysis"]
        assert ea["verdict"] == "entry_reachable"
        assert ea["chain_status"] == "reached"
        # CollateralOp 通过 entry 链与 plugin_boundary 链两条路径都到达，按类去重只算一次。
        assert [e["class"] for e in ea["entries"]] == ["cqspb.assets.CollateralOp"]
        assert ea["entries"][0]["registration"]["status"] == "registered_enabled"
        assert ea["confidence"] == "confirmed"          # expr 边的 entry 链
    finally:
        conn.close()


# ── 用例 3：全部禁用 → entries_inactive/likely ─────────────────────────────

def _disabled_kb(tmp_path: Path) -> Path:
    db = make_kb(tmp_path)
    _exec_rows(db,
        "INSERT INTO plugin(uid,form_key,class_name,plugin_type,source,operation_key,"
        "operation_name,enabled) VALUES(?,?,?,?,?,?,?,?)", [
            ("pd1", "cqkd_contract", "cqspb.assets.DisabledOp", "op", "project",
             "submit", "提交", 0),
        ])
    _exec_rows(db,
        "INSERT INTO plugin_method(plugin_fqn,method_name,event_kind,event_phase,"
        "start_line,end_line,source_relpath) VALUES(?,?,?,?,?,?,?)", [
            ("cqspb.assets.DisabledOp", "beforeExecuteOperationTransaction",
             "beforeExecuteOperationTransaction", "transaction", 10, 30,
             "cqspb/assets/DisabledOp.java"),
        ])
    _exec_rows(db,
        "INSERT INTO call_edge(caller_fqn,caller_method,target_fqn,target_method,"
        "target_signature,kind,line,col,source_relpath,resolution,target_kind,"
        "confidence,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("cqspb.assets.DisabledOp", "beforeExecuteOperationTransaction",
             "cqspb.assets.DisabledSvc", "run", None, "invocation", 20, 9,
             "cqspb/assets/DisabledOp.java", "expr", "project", 1.0, "symbol:expr"),
        ])
    return db


def test_all_disabled_bindings_yield_entries_inactive(tmp_path: Path):
    db = _disabled_kb(tmp_path)
    conn = _conn(db)
    try:
        result = callers_report.callers(conn, "DisabledSvc.run")
        ea = result["entry_analysis"]
        assert ea["verdict"] == "entries_inactive"
        assert ea["confidence"] == "likely"
        assert ea["entries"][0]["registration"]["status"] == "registered_disabled"
        assert "禁用" in ea["note"]
        assert "不排除反射" in ea["note"]
    finally:
        conn.close()


# ── 用例 4：enabled NULL → registered_enabled_unknown → entry_unverifiable ──

def test_enabled_null_is_registration_unknown(tmp_path: Path):
    db = make_kb(tmp_path)
    _exec_rows(db,
        "INSERT INTO plugin(uid,form_key,class_name,plugin_type,source,operation_key,"
        "operation_name,enabled) VALUES(?,?,?,?,?,?,?,?)", [
            ("pu1", "cqkd_contract", "cqspb.assets.UnknownOp", "op", "project",
             "submit", "提交", None),
        ])
    conn = _conn(db)
    try:
        reg = entry_chain.registration_status(conn, "cqspb.assets.UnknownOp")
        assert reg["status"] == "registered_enabled_unknown"
        assert reg["bindings"][0]["enabled"] is None
        assert reg["bindings"][0]["enabled_source"] == "plugin"
    finally:
        conn.close()


# ── 用例 5：convert 规则启停（enabled_source=convert_rule）──────────────────

def test_convert_plugin_follows_rule_level_enabled(tmp_path: Path):
    db = make_kb(tmp_path)
    _exec_rows(db,
        "INSERT INTO convert_rule(id,name,source_entity,target_entity,source_entry,"
        "target_entry,isv,app_key,module,field_map_count,plugin_count,enabled,"
        "source_file) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("rule_on", "启用规则", "cqkd_contract", "cqkd_assetcard", None, None,
             "cqkd", "cqkd_assets", "cqkd_assets", 3, 1, 1, "rule_on.cr"),
            ("rule_off", "停用规则", "cqkd_contract", "cqkd_assetcard", None, None,
             "cqkd", "cqkd_assets", "cqkd_assets", 3, 1, 0, "rule_off.cr"),
        ])
    _exec_rows(db,
        "INSERT INTO plugin(uid,form_key,class_name,plugin_type,source,operation_key,"
        "operation_name,enabled) VALUES(?,?,?,?,?,?,?,?)", [
            ("pc1", "rule_on", "cqspb.assets.ConvertPluginOn", "convert", "project",
             None, None, None),
            ("pc2", "rule_off", "cqspb.assets.ConvertPluginOff", "convert", "project",
             None, None, None),
        ])
    conn = _conn(db)
    try:
        on = entry_chain.registration_status(conn, "cqspb.assets.ConvertPluginOn")
        assert on["status"] == "registered_enabled"
        assert on["bindings"][0]["enabled_source"] == "convert_rule"
        assert on["bindings"][0]["rule_name"] == "启用规则"

        off = entry_chain.registration_status(conn, "cqspb.assets.ConvertPluginOff")
        assert off["status"] == "registered_disabled"
        assert off["bindings"][0]["enabled_source"] == "convert_rule"
    finally:
        conn.close()


def test_convert_plugin_dangling_rule_is_unknown(tmp_path: Path):
    """CRPlugin 的 form_key 在 convert_rule 表里找不到（悬空）→ 归 unknown 档并注明。"""
    db = make_kb(tmp_path)
    _exec_rows(db,
        "INSERT INTO plugin(uid,form_key,class_name,plugin_type,source,operation_key,"
        "operation_name,enabled) VALUES(?,?,?,?,?,?,?,?)", [
            ("pc3", "rule_missing", "cqspb.assets.ConvertPluginGhost", "convert",
             "project", None, None, None),
        ])
    conn = _conn(db)
    try:
        reg = entry_chain.registration_status(conn, "cqspb.assets.ConvertPluginGhost")
        assert reg["status"] == "registered_enabled_unknown"
        assert reg["bindings"][0]["enabled_source"] == "convert_rule"
        assert "悬空" in reg["bindings"][0]["note"]
    finally:
        conn.close()


# ── 用例 6：孤儿 task 类 → orphan_unverifiable ──────────────────────────────

def test_orphan_task_class_is_unverifiable(tmp_path: Path):
    db = make_kb(tmp_path)
    _exec_rows(db,
        "INSERT INTO source_class(fqn,simple,package,relpath,module,is_orphan,"
        "orphan_role,plugin_base) VALUES(?,?,?,?,?,?,?,?)", [
            ("cqspb.assets.NightTaskX", "NightTaskX", "cqspb.assets",
             "cqspb/assets/NightTaskX.java", "cqkd_assets", 1, "plugin", "AbstractTask"),
        ])
    _exec_rows(db,
        "INSERT INTO call_edge(caller_fqn,caller_method,target_fqn,target_method,"
        "target_signature,kind,line,col,source_relpath,resolution,target_kind,"
        "confidence,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("cqspb.assets.NightTaskX", "execute", "cqspb.assets.TaskWorker", "doWork",
             None, "invocation", 15, 9, "cqspb/assets/NightTaskX.java", "expr",
             "project", 1.0, "symbol:expr"),
        ])
    conn = _conn(db)
    try:
        reg = entry_chain.registration_status(conn, "cqspb.assets.NightTaskX")
        assert reg["status"] == "orphan_unverifiable"
        assert reg["kind"] == "task"
        assert "未接入" in reg["note"]

        result = callers_report.callers(conn, "TaskWorker.doWork")
        ea = result["entry_analysis"]
        assert ea["verdict"] == "entry_unverifiable"
        assert ea["confidence"] == "unknown"
        entry = next(e for e in ea["entries"] if e["class"] == "cqspb.assets.NightTaskX")
        assert entry["registration"]["status"] == "orphan_unverifiable"
    finally:
        conn.close()


# ── 用例 7：孤儿 form 类未注册（唯一入口）→ entries_inactive ─────────────────

def test_orphan_form_class_unregistered_is_sole_inactive_entry(tmp_path: Path):
    db = make_kb(tmp_path)
    _exec_rows(db,
        "INSERT INTO source_class(fqn,simple,package,relpath,module,is_orphan,"
        "orphan_role,plugin_base) VALUES(?,?,?,?,?,?,?,?)", [
            ("cqspb.assets.OrphanFormPlugin", "OrphanFormPlugin", "cqspb.assets",
             "cqspb/assets/OrphanFormPlugin.java", "cqkd_assets", 1, "plugin",
             "AbstractFormPlugin"),
        ])
    _exec_rows(db,
        "INSERT INTO call_edge(caller_fqn,caller_method,target_fqn,target_method,"
        "target_signature,kind,line,col,source_relpath,resolution,target_kind,"
        "confidence,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("cqspb.assets.OrphanFormPlugin", "propertyChanged", "cqspb.assets.FieldSvc",
             "doX", None, "invocation", 22, 9, "cqspb/assets/OrphanFormPlugin.java",
             "expr", "project", 1.0, "symbol:expr"),
        ])
    conn = _conn(db)
    try:
        reg = entry_chain.registration_status(conn, "cqspb.assets.OrphanFormPlugin")
        assert reg["status"] == "orphan_unregistered"
        assert reg["kind"] == "form"

        result = callers_report.callers(conn, "FieldSvc.doX")
        ea = result["entry_analysis"]
        assert ea["verdict"] == "entries_inactive"
        assert ea["confidence"] == "likely"
        assert len(ea["entries"]) == 1
    finally:
        conn.close()


# ── 用例 8：no_entry + 强0 / 弱化后 unknown ─────────────────────────────────

def test_no_entry_found_strong_then_weak_after_symbol_disabled(tmp_path: Path):
    db = make_kb(tmp_path)
    conn = _conn(db)
    try:
        result = callers_report.callers(conn, "CollateralService.unused")
        ea = result["entry_analysis"]
        assert ea["chain_status"] == "not_found"
        assert ea["verdict"] == "no_entry_found"
        assert ea["confidence"] == "likely"
    finally:
        conn.close()

    import json
    raw = sqlite3.connect(str(db))
    raw.execute(
        "UPDATE kb_meta SET value=? WHERE key='symbol_resolution'",
        (json.dumps({"status": "disabled", "coverage": 0.0, "reason": "--no-symbols"}),),
    )
    raw.commit()
    raw.close()

    conn = _conn(db)
    try:
        result = callers_report.callers(conn, "CollateralService.unused")
        ea = result["entry_analysis"]
        assert ea["verdict"] == "no_entry_found"
        assert ea["confidence"] == "unknown"
        assert "不足以断言" in ea["note"]
    finally:
        conn.close()


# ── 用例 9：截断降级 → entry_unverifiable ───────────────────────────────────

def test_truncated_chains_downgrade_would_be_inactive_to_unverifiable(tmp_path: Path):
    db = _disabled_kb(tmp_path)
    # 再加一条无上游的调用点，人为制造第二条链，逼 max_chains 截断掉其中一条。
    _exec_rows(db,
        "INSERT INTO call_edge(caller_fqn,caller_method,target_fqn,target_method,"
        "target_signature,kind,line,col,source_relpath,resolution,target_kind,"
        "confidence,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("cqspb.assets.OtherCaller", "foo", "cqspb.assets.DisabledSvc", "run",
             None, "invocation", 5, 9, "cqspb/assets/OtherCaller.java", "heuristic",
             "project", 0.6, "fallback=tree-sitter-local"),
        ])
    conn = _conn(db)
    try:
        coverage = callers_report.resolution_coverage(conn)
        full = callers_report.entry_analysis(
            conn, "cqspb.assets.DisabledSvc", "run", coverage)
        assert full["verdict"] == "entries_inactive"     # 未截断时的基准：如实判定不可达

        truncated = callers_report.entry_analysis(
            conn, "cqspb.assets.DisabledSvc", "run", coverage, max_chains=1)
        assert truncated["chains_truncated"] >= 1
        assert truncated["verdict"] == "entry_unverifiable"
        assert truncated["confidence"] == "unknown"
        assert "本应判定为" in truncated["note"] and "截断" in truncated["note"]
    finally:
        conn.close()


# ── 用例 10：jar 目标 → not_analyzed（不跑 BFS）────────────────────────────

def test_jar_target_is_not_analyzed(tmp_path: Path):
    db = make_kb(tmp_path)
    conn = _conn(db)
    try:
        result = callers_report.callers(conn, "SaveServiceHelper.save")
        assert result["target"]["target_kind"] == "jar"
        ea = result["entry_analysis"]
        assert ea == {
            "verdict": "not_analyzed", "confidence": None, "chain_status": None,
            "entries": [], "reason": "platform_target",
            "note": ea["note"],
        }
        assert "平台/JDK" in ea["note"]
    finally:
        conn.close()


# ── 用例 11：need_clarification 不带 entry_analysis 键 ──────────────────────

def test_need_clarification_has_no_entry_analysis_key(tmp_path: Path):
    db = make_kb(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO source_class(fqn,simple,package,relpath) VALUES(?,?,?,?)",
        [
            ("p.one.Dup", "Dup", "p.one", "p/one/Dup.java"),
            ("p.two.Dup", "Dup", "p.two", "p/two/Dup.java"),
        ],
    )
    conn.commit()
    conn.close()

    conn = _conn(db)
    try:
        result = callers_report.callers(conn, "Dup.run")
        assert result["status"] == "need_clarification"
        assert "entry_analysis" not in result

        bad = callers_report.callers(conn, "NoSuchClass.run")
        assert "error" in bad and "entry_analysis" not in bad
    finally:
        conn.close()


# ── 用例 12：MCP 首页完整分析 / 后续页只带 verdict / 翻页不丢数 ─────────────

def test_mcp_first_page_full_entry_analysis_later_pages_slim(tmp_path: Path, monkeypatch):
    db = make_kb(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO call_edge VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(f"p.Caller{i}", "run", "cqspb.assets.CollateralService", "update", None,
          "invocation", 100 + i, 9, f"p/Caller{i}.java", "heuristic", "project", 0.6,
          "fallback=tree-sitter-heuristic") for i in range(85)],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("COSMIC_KB_DB", str(db))

    cursor = None
    rows: list[dict] = []
    first_ea = None
    later_eas: list[dict] = []
    while True:
        page = mcp_server.tool_callers("CollateralService.update", cursor=cursor)
        rows.extend(page["callers"])
        if cursor is None:
            first_ea = page["entry_analysis"]
        else:
            later_eas.append(page["entry_analysis"])
        if page["pagination"]["complete"]:
            break
        cursor = page["pagination"]["next_cursor"]
        assert cursor
    assert len(rows) == 87

    assert first_ea["verdict"] == "entry_reachable"
    assert first_ea["entries"]
    assert "chains" in first_ea or "chains_omitted" in first_ea
    for ea in later_eas:
        assert set(ea) == {"verdict", "confidence", "note"}
        assert ea["note"] == "完整入口分析见第一页"


# ── 用例 13：CLI 人读渲染含入口可达性段 ─────────────────────────────────────

def test_render_callers_includes_entry_reachability_section(tmp_path: Path):
    db = make_kb(tmp_path)
    conn = _conn(db)
    try:
        result = callers_report.callers(conn, "CollateralService.update")
        text = callers_report.render_callers(result)
        assert "入口可达性" in text
        assert "可达" in text
        assert "入口链" in text
    finally:
        conn.close()
