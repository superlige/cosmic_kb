"""隐藏坑 #1 · 程序化操作触发链验收测试。

覆盖三层：
  * 提取层（java/op_trigger.py）：executeOperate/invokeOperation 两种调用的
    操作 key + 目标单据实参解析（字面量/常量/表达式→dynamic/绑定推断/unknown，绝不臆造）；
  * 管线层（analyze → store）：真实小项目 build_kb 后 operation_trigger 表落库，
    操作坐标追踪（trace --kind operation）正查 + bill 计数/外发反查；
  * 报告层（合成 KB）：op_trace 三段呈现（triggered_by/unresolved_inbound/triggers_downstream）
    + 紧凑投影翻页取回（cap 可取回纪律）+ bill 最小信号 + 字段 trace 的 note 提示。

呈现取舍（2026-07-15 与用户拍板）：入站明细**不摊进 bill/字段 trace**，按需走操作坐标；
坐标判别纯显式（kind="operation"）；外发留在 bill 精简节（影响面视图）。
二次整合（同日）：unresolved_inbound 扩成**无法静态排除是本操作**的嫌疑全量——目标单据
解不出但操作 key 匹配（target_unresolved，表单插件外发挂不上操作坐标的形态）与双边解不出
（both_unresolved）也并入，对某操作的调用 trace 一次即完整，不再需要补查 bill.outbound_triggers。
提取/管线层依赖 tree-sitter（[parse] extra），未装则跳过；报告层用合成 KB 不依赖。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cosmic_kb.graph import store
from cosmic_kb.report import bill_view, field_trace, op_trace

from _synthkb import make_kb

# ── 提取层（unit：直接解析方法体） ────────────────────────────────────────────

TRIGGER_SRC = """package cqspb.am;
public class TriggerSvc {
  public void go(DynamicObject[] bills) {
    OperationServiceHelper.executeOperate("audit", "cqkd_b", bills, OperateOption.create());
    OperationServiceHelper.executeOperate(BillConst.OP_SUBMIT, BillConst.ENTITY_B, bills, null);
    OperationServiceHelper.execOperate("close", "cqkd_b", bills, null);
    String op = compute();
    OperationServiceHelper.executeOperate(op, "cqkd_b", bills, null);
  }
}
"""

CONST_SRC = """package cqspb.am;
public class BillConst {
  public static final String OP_SUBMIT = "submit";
  public static final String ENTITY_B = "cqkd_b";
}
"""

INVOKE_SRC = """package cqspb.am;
public class BtnPlugin {
  public void itemClick(Object e) {
    this.getView().invokeOperation("save");
  }
}
"""


def _method_body(src: str, method: str):
    from cosmic_kb.java import ast_index as ax

    root = ax.parse_tree(src)
    for td in ax.iter_type_declarations(root):
        for md in ax.iter_methods(td):
            if md.name == method:
                return md.body
    raise AssertionError(f"method {method} not found")


def _const_table(*srcs: str):
    from cosmic_kb.java import ast_index as ax
    from cosmic_kb.java import constants as cmod

    table = cmod.ConstantTable()
    for s in srcs:
        cmod.collect_into(ax.parse_tree(s), table)
    return table


def test_find_triggers_literal_constant_dynamic():
    """识别 executeOperate，忽略不存在的平台方法 execOperate，并保留解析置信度。"""
    pytest.importorskip("tree_sitter_java")
    from cosmic_kb.java import op_trigger as ot

    rows = ot.find_operation_triggers(
        _method_body(TRIGGER_SRC, "go"), _const_table(CONST_SRC),
        caller_class="cqspb.am.TriggerSvc", caller_method="go",
        source_relpath="cqspb/am/TriggerSvc.java")
    assert len(rows) == 3

    lit = rows[0]
    assert (lit.via, lit.op_key, lit.op_key_resolution, lit.op_key_confidence) == \
        ("executeOperate", "audit", "literal", 1.0)
    assert (lit.target_form_key, lit.target_resolution) == ("cqkd_b", "literal")

    const = rows[1]
    assert (const.via, const.op_key, const.op_key_resolution) == ("executeOperate", "submit", "constant")
    assert (const.target_form_key, const.target_confidence) == ("cqkd_b", 0.95)

    dyn = rows[2]
    # 局部变量实参是 identifier，走常量表查不到 → unknown（"dynamic" 留给表达式/拼接实参）。
    assert dyn.op_key is None and dyn.op_key_resolution == "unknown"
    assert dyn.target_form_key == "cqkd_b"       # 目标仍是字面量，照常解析


def test_find_triggers_invoke_operation_binding():
    """invokeOperation 目标不在实参里：唯一绑定→binding；多绑定→ambiguous；无绑定→unknown。"""
    pytest.importorskip("tree_sitter_java")
    from cosmic_kb.java import op_trigger as ot

    body = _method_body(INVOKE_SRC, "itemClick")
    const = _const_table()

    bound = ot.find_operation_triggers(
        body, const, caller_class="cqspb.am.BtnPlugin", caller_method="itemClick",
        source_relpath="x.java", bound_form="cqkd_a")
    assert len(bound) == 1
    t = bound[0]
    assert (t.via, t.op_key, t.op_key_resolution) == ("invokeOperation", "save", "literal")
    assert (t.target_form_key, t.target_resolution) == ("cqkd_a", "binding")

    ambiguous = ot.find_operation_triggers(
        body, const, caller_class="c", caller_method="m", source_relpath="x.java",
        bound_form=None, bound_ambiguous=True)
    assert ambiguous[0].target_form_key is None
    assert ambiguous[0].target_resolution == "ambiguous"

    unknown = ot.find_operation_triggers(
        body, const, caller_class="c", caller_method="m", source_relpath="x.java")
    assert unknown[0].target_form_key is None
    assert unknown[0].target_resolution == "unknown"


# ── 管线层（真实小项目 build_kb） ────────────────────────────────────────────

PUSH_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class AmPushOp extends AbstractOperationServicePlugIn {
  public void afterExecuteOperationTransaction(AfterOperationArgs e) {
    OperationServiceHelper.executeOperate("audit", "cqkd_bill2", e.getDataEntities(), null);
  }
}
"""

BTN_PLUGIN = """package cqspb.am;
import kd.bos.form.plugin.AbstractBillPlugIn;
public class AmBtnPlugin extends AbstractBillPlugIn {
  public void itemClick(Object e) {
    this.getView().invokeOperation("save");
  }
}
"""


def _build_pipeline(tmp_path: Path):
    from cosmic_kb.bridge import linker, namespace
    from cosmic_kb.ingest import scanner
    from cosmic_kb.metadata.model import (
        MetaEntity, MetaField, MetaModel, MetaOperation, MetaPlugin,
    )
    from cosmic_kb.report import project_map

    src = tmp_path / "src"
    for name, text in (("AmPushOp.java", PUSH_OP), ("AmBtnPlugin.java", BTN_PLUGIN)):
        p = src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    scan = scanner.scan(src)

    def _m(key, name, plugins, ops):
        return MetaModel(
            key=key, name=name, model_type="BillFormModel", form_type="bill",
            isv="cqkd", app_key="cqkd_am",
            entities=[MetaEntity("BillEntity", key, "头", "1", "header", None, "t_" + key)],
            fields=[MetaField("TextField", "cqkd_head", "表头字段", "fh", "idh", "1",
                              "entity", "header", key)],
            plugins=plugins, operations=ops)

    m1 = _m("cqkd_bill", "上游单", [
        MetaPlugin("cqspb.am.AmPushOp", "op", "project", operation_key="push"),
        MetaPlugin("cqspb.am.AmBtnPlugin", "form", "project"),
    ], [
        MetaOperation("push", "推送", "push", None, None, resolved_from="self"),
        MetaOperation("save", "保存", "save", None, None, resolved_from="self"),
    ])
    m2 = _m("cqkd_bill2", "下游单", [], [
        MetaOperation("audit", "审核", "audit", None, None, resolved_from="self"),
    ])
    models = [m1, m2]
    index = namespace.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    mm = project_map.module_map(scan, models, bridge, index=index)
    db = tmp_path / "kb.db"
    counts = store.build_kb(scan, models, bridge, mm, db, index=index)
    return db, counts


def test_pipeline_triggers_into_kb(tmp_path: Path):
    """真管线：executeOperate 跨单据触发 + invokeOperation 自触发入库，操作坐标/bill 双向可查。"""
    pytest.importorskip("tree_sitter_java")
    db, counts = _build_pipeline(tmp_path)
    assert counts["operation_trigger"] >= 2
    conn = store.open_kb(db)
    try:
        # 正查（操作坐标）：下游单 cqkd_bill2.audit 的触发链指回上游单。
        ot = op_trace.operation_trace(conn, "cqkd_bill2.audit")
        assert len(ot["triggered_by"]) == 1
        t = ot["triggered_by"][0]
        assert t["caller_class"] == "cqspb.am.AmPushOp"
        assert t["via"] == "executeOperate" and t["line"] > 0
        assert t["caller_forms"] == ["cqkd_bill"]     # 上游单据，供递归拼链

        # 正查：上游单自己的 save 被界面插件 invokeOperation 触发（目标=绑定单据推断）。
        ot_save = op_trace.operation_trace(conn, "cqkd_bill.save")
        assert any(t["via"] == "invokeOperation" and t["caller_class"] == "cqspb.am.AmBtnPlugin"
                   for t in ot_save["triggered_by"])

        # bill 最小信号：下游单 audit 挂计数（明细不再内联）。
        bv2 = bill_view.bill_view(conn, "cqkd_bill2")
        audit = next(o for o in bv2["operations"] if o["key"] == "audit")
        assert audit["programmatic_trigger_count"] == 1
        assert "programmatic_triggers" not in audit
        assert bv2["stats"]["programmatic_trigger_count"] == 1

        # 反查：上游单的外发触发列出 cqkd_bill2.audit（自触发 save 不重复出现在 outbound）。
        bv1 = bill_view.bill_view(conn, "cqkd_bill")
        out = bv1["outbound_triggers"]
        assert any(t["target_form_key"] == "cqkd_bill2" and t["op_key"] == "audit" for t in out)
        assert all(t["target_form_key"] != "cqkd_bill" for t in out)
    finally:
        conn.close()


# ── 报告层（合成 KB：op_trace 三段 + bill 最小信号 + trace note） ─────────────

_TRIG_COLS = ("caller_class", "caller_method", "line", "source_relpath", "via", "op_key",
              "op_key_resolution", "op_key_confidence", "target_form_key",
              "target_resolution", "target_confidence", "evidence", "receiver_source")


def _add_triggers(db: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(
            f"INSERT INTO operation_trigger VALUES({','.join('?' * len(_TRIG_COLS))})", rows)
        conn.commit()
    finally:
        conn.close()


def _trig(caller, method, line, *, op_key="audit", op_res="literal", op_conf=1.0,
          target="cqkd_assetcard", tgt_res="literal", tgt_conf=1.0, via="executeOperate"):
    return (caller, method, line, caller.replace(".", "/") + ".java", via,
            op_key, op_res, op_conf, target, tgt_res, tgt_conf, None, "text")


@pytest.fixture()
def trig_kb(tmp_path: Path) -> Path:
    """合成 KB + 触发点：ContractAuditOp（绑定 cqkd_contract.submit 的 op 插件）触发
    cqkd_assetcard.audit；NightTask 的操作 key 解不出（dynamic）。"""
    db = make_kb(tmp_path)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO plugin(uid,form_key,class_name,plugin_type,source,operation_key,"
            "operation_name,enabled) VALUES(?,?,?,?,?,?,?,?)",
            ("p2", "cqkd_contract", "cqspb.assets.ContractAuditOp", "op", "project",
             "submit", "提交", 1))
        conn.execute(
            "INSERT INTO operation(form_key,key,name,operation_type,resolved_from,"
            "has_operation_plugin) VALUES(?,?,?,?,?,?)",
            ("cqkd_contract", "submit", "提交", "submit", "self", 1))
        conn.commit()
    finally:
        conn.close()
    _add_triggers(db, [
        _trig("cqspb.assets.ContractAuditOp", "afterExecuteOperationTransaction", 88),
        # 操作 key 解不出（dynamic）→ unresolved_inbound 嫌疑。
        _trig("cqspb.assets.NightTask", "execute", 30, op_key=None, op_res="dynamic", op_conf=0.0),
    ])
    return db


def test_operation_trace_three_sections(trig_kb: Path):
    """操作坐标三段：triggered_by（含 caller_forms）/ unresolved_inbound / triggers_downstream。"""
    conn = store.open_kb(trig_kb)
    try:
        # 下游视角：cqkd_assetcard.audit 被谁触发。
        ot = op_trace.operation_trace(conn, "cqkd_assetcard.audit")
        assert ot["kind"] == "operation" and ot["form_name"] == "资产卡片"
        assert ot["operation"]["name"] == "审核"
        assert [t["caller_class"] for t in ot["triggered_by"]] == \
            ["cqspb.assets.ContractAuditOp"]
        assert ot["triggered_by"][0]["caller_forms"] == ["cqkd_contract"]
        assert [t["caller_class"] for t in ot["unresolved_inbound"]] == ["cqspb.assets.NightTask"]
        assert ot["summary"] == {"triggered_by": 1, "unresolved_inbound": 1,
                                 "triggers_downstream": 0, "plugins": 1}
        assert "上游单据" in ot["note"] and "unresolved_inbound" in ot["note"]

        # 上游视角：cqkd_contract.submit 的操作插件对外触发 cqkd_assetcard.audit（级联下行）。
        ot_up = op_trace.operation_trace(conn, "cqkd_contract.submit")
        down = ot_up["triggers_downstream"]
        assert len(down) == 1
        assert (down[0]["target_form_key"], down[0]["target_form_name"], down[0]["op_key"]) == \
            ("cqkd_assetcard", "资产卡片", "audit")
        assert down[0]["next_trace"] == "cqkd_assetcard.audit"   # 递归拼链的下一跳坐标
    finally:
        conn.close()


def test_operation_trace_unresolved_target_suspects(trig_kb: Path):
    """入站完整性（二次整合）：目标单据解不出的触发点并入 unresolved_inbound——
    op key 匹配→target_unresolved；双边解不出→both_unresolved（进所有操作坐标的嫌疑）；
    op key 解出且≠所查操作→静态可排除，不进嫌疑。嫌疑按强弱排序、带 suspect_reason。"""
    _add_triggers(trig_kb, [
        # 表单插件绑多张单，invokeOperation("audit") 目标 ambiguous → audit 坐标的强嫌疑。
        _trig("cqspb.assets.MultiFormBtn", "itemClick", 55, op_key="audit",
              target=None, tgt_res="ambiguous", tgt_conf=0.3, via="invokeOperation"),
        # 操作 key 与目标全解不出 → 任何操作坐标都排除不掉的弱嫌疑（排最后）。
        _trig("cqspb.assets.GenericSvc", "fire", 77, op_key=None, op_res="dynamic",
              op_conf=0.0, target=None, tgt_res="dynamic", tgt_conf=0.0),
        # 操作 key 解出且 ≠ audit：静态可排除，不得混进 audit 的嫌疑。
        _trig("cqspb.assets.OtherSvc", "run", 99, op_key="submit",
              target=None, tgt_res="dynamic", tgt_conf=0.0),
    ])
    conn = store.open_kb(trig_kb)
    try:
        ot = op_trace.operation_trace(conn, "cqkd_assetcard.audit")
        # 嫌疑段 = 既有 NightTask（目标钉本单据、op 解不出）+ 两条目标解不出形态，按强弱排序。
        assert [(t["caller_class"], t["suspect_reason"]) for t in ot["unresolved_inbound"]] == [
            ("cqspb.assets.NightTask", "op_unresolved"),
            ("cqspb.assets.MultiFormBtn", "target_unresolved"),
            ("cqspb.assets.GenericSvc", "both_unresolved"),
        ]
        assert ot["summary"]["unresolved_inbound"] == 3
        assert len(ot["triggered_by"]) == 1          # 确定入站不受嫌疑扩容影响
        assert "target_unresolved" in ot["note"] and "完整" in ot["note"]

        # both_unresolved 在别的操作坐标同样出现（排除不掉任何操作）；op key 不匹配的不串台；
        # OtherSvc（op=submit，目标解不出）恰是 submit 坐标的 target_unresolved。
        ot_sub = op_trace.operation_trace(conn, "cqkd_contract.submit")
        by_cls = {t["caller_class"]: t["suspect_reason"] for t in ot_sub["unresolved_inbound"]}
        assert by_cls.get("cqspb.assets.GenericSvc") == "both_unresolved"
        assert by_cls.get("cqspb.assets.OtherSvc") == "target_unresolved"
        assert "cqspb.assets.MultiFormBtn" not in by_cls   # op=audit ≠ submit，静态排除
        assert "cqspb.assets.NightTask" not in by_cls      # 目标已钉 cqkd_assetcard，静态排除

        # 紧凑投影嫌疑行带 suspect_reason + 双侧解析档位（读源码定性的最小上下文）。
        res = op_trace.operation_trace_compact(conn, "cqkd_assetcard.audit")
        row = next(t for t in res["unresolved_inbound"]
                   if t["caller_class"].endswith("MultiFormBtn"))
        assert row["suspect_reason"] == "target_unresolved"
        assert (row["target_form_key"], row["target_resolution"]) == (None, "ambiguous")
        # 人读渲染同口径展示成因。
        assert "target_unresolved" in op_trace.render_operation_trace(ot)
    finally:
        conn.close()


def test_operation_trace_ghost_op_rescued_by_matching_suspect(trig_kb: Path):
    """操作既不在元数据操作集、也无确定入站，但存在 op key 匹配的目标解不出嫌疑 →
    不报"查无此操作"，照给嫌疑证据 + in_metadata=False 诚实注明。"""
    _add_triggers(trig_kb, [
        _trig("cqspb.assets.GhostSvc", "call", 12, op_key="ghost",
              target=None, tgt_res="unknown", tgt_conf=0.0),
    ])
    conn = store.open_kb(trig_kb)
    try:
        ot = op_trace.operation_trace(conn, "cqkd_assetcard.ghost")
        assert "error" not in ot
        assert ot["operation"] == {"key": "ghost", "in_metadata": False}
        assert ot["triggered_by"] == []
        assert any(t["caller_class"] == "cqspb.assets.GhostSvc"
                   and t["suspect_reason"] == "target_unresolved"
                   for t in ot["unresolved_inbound"])
    finally:
        conn.close()


def test_operation_trace_locator_paths(trig_kb: Path):
    """坐标解析：裸操作 key 唯一命中自动定位；不存在的操作诚实报错并列操作集；段数超限报错。"""
    conn = store.open_kb(trig_kb)
    try:
        # 裸 "audit" 只有 cqkd_assetcard 定义 → 自动定位。
        ot = op_trace.operation_trace(conn, "audit")
        assert ot.get("form_key") == "cqkd_assetcard" and len(ot["triggered_by"]) == 1

        bad = op_trace.operation_trace(conn, "cqkd_assetcard.nonexist")
        assert "error" in bad and "audit" in bad["available_operations"]

        assert "error" in op_trace.operation_trace(conn, "a.b.c")
        assert "error" in op_trace.operation_trace(conn, "cqkd_nosuchform.audit")
    finally:
        conn.close()


def test_operation_trace_op_not_in_metadata(trig_kb: Path):
    """操作 key 有触发点但不在元数据操作集：直查照常给证据，但不串成其他操作的嫌疑。"""
    _add_triggers(trig_kb, [
        _trig("cqspb.assets.BatchSvc", "push", 10, op_key="push"),
    ])
    conn = store.open_kb(trig_kb)
    try:
        ot = op_trace.operation_trace(conn, "cqkd_assetcard.push")
        assert ot["operation"] == {"key": "push", "in_metadata": False}
        assert [t["caller_class"] for t in ot["triggered_by"]] == ["cqspb.assets.BatchSvc"]
        assert "不在" in ot["note"]
        # key 已明确解析为 push，是否存在于元数据操作集都不可能变成 audit。
        ot_audit = op_trace.operation_trace(conn, "cqkd_assetcard.audit")
        assert all(t["op_key"] != "push" for t in ot_audit["unresolved_inbound"])
    finally:
        conn.close()


def test_operation_trace_refresh_is_not_updatetax_suspect(trig_kb: Path):
    """真实回归：invokeOperation("refresh") 已解析成不同 key，不得成为 updatetax 嫌疑。"""
    conn = sqlite3.connect(str(trig_kb))
    try:
        conn.execute(
            "INSERT INTO operation(form_key,key,name,operation_type,resolved_from,"
            "has_operation_plugin) VALUES(?,?,?,?,?,?)",
            ("cqkd_assetcard", "updatetax", "更新税额", "donothing", "self", 1),
        )
        conn.commit()
    finally:
        conn.close()
    _add_triggers(trig_kb, [
        _trig("cqspb.assets.ContractFormPlugin", "afterDoOperation", 339,
              op_key="refresh", target="cqkd_assetcard", tgt_res="binding",
              tgt_conf=0.85, via="invokeOperation"),
    ])

    conn = store.open_kb(trig_kb)
    try:
        ot = op_trace.operation_trace(conn, "cqkd_assetcard.updatetax")
        assert all(t["op_key"] != "refresh" for t in ot["unresolved_inbound"])
        assert not any(t["caller_class"].endswith("ContractFormPlugin")
                       for t in ot["unresolved_inbound"])
    finally:
        conn.close()


def test_operation_trace_compact_and_pagination(trig_kb: Path):
    """紧凑投影：预算内 + 翻页门；超 cap 的 triggered_by 经 cursor 逐页取回全部（不丢数）。"""
    _add_triggers(trig_kb, [
        _trig("cqspb.assets.BatchSvc", f"m{i}", 100 + i) for i in range(25)
    ])
    conn = store.open_kb(trig_kb)
    try:
        res = op_trace.operation_trace_compact(conn, "cqkd_assetcard.audit")
        assert field_trace._wire_len(res) <= field_trace._COMPACT_BUDGET
        assert len(res["triggered_by"]) == 20            # 首档 cap
        assert res["triggered_by_capped"] == 6
        assert res["pagination"]["complete"] is False
        assert any(p["section"] == "triggered_by" for p in res["pagination"]["pending"])
        assert res["unresolved_inbound"]                 # 非空段照常带出

        # 游标翻页取回全部 26 条（1 ContractAuditOp + 25 BatchSvc）。
        got: list[dict] = []
        cursor = "triggered_by@0"
        while cursor:
            page = op_trace.operation_trace_compact(
                conn, "cqkd_assetcard.audit", cursor=cursor)["page"]
            got.extend(page["items"])
            cursor = page["next_cursor"]
        assert len(got) == 26
        assert {g["caller_class"] for g in got} == \
            {"cqspb.assets.ContractAuditOp", "cqspb.assets.BatchSvc"}
    finally:
        conn.close()


def test_bill_minimal_signal(trig_kb: Path):
    """bill 只留最小信号：每操作计数 + stats（含 unresolved），入站明细不再内联；外发节保留。"""
    conn = store.open_kb(trig_kb)
    try:
        bv = bill_view.bill_view(conn, "cqkd_assetcard")
        audit = next(o for o in bv["operations"] if o["key"] == "audit")
        assert audit["programmatic_trigger_count"] == 1
        assert "programmatic_triggers" not in audit
        assert "unresolved_triggers" not in bv
        assert bv["stats"]["programmatic_trigger_count"] == 2   # 含解不出的那条，计数诚实
        assert bv["stats"]["unresolved_trigger_count"] == 1

        # 紧凑投影：计数带出 + note 指路操作坐标；不出现明细数组。
        res = bill_view.bill_compact(conn, "cqkd_assetcard")
        assert res["pagination"]["complete"] is True
        c_audit = next(o for o in res["operations"] if o["key"] == "audit")
        assert c_audit["programmatic_trigger_count"] == 1
        assert 'kind="operation"' in res["note"]
        assert field_trace._wire_len(res) <= field_trace._COMPACT_BUDGET

        # 上游单 cqkd_contract：外发节保留（表单插件外发挂不上操作坐标，bill 是唯一出口）。
        bvc = bill_view.bill_view(conn, "cqkd_contract")
        out = bvc["outbound_triggers"]
        assert len(out) == 1
        assert (out[0]["target_form_key"], out[0]["target_form_name"], out[0]["op_key"]) == \
            ("cqkd_assetcard", "资产卡片", "audit")
        resc = bill_view.bill_compact(conn, "cqkd_contract")
        assert resc["outbound_triggers"][0]["target_form_key"] == "cqkd_assetcard"
    finally:
        conn.close()


def test_trace_field_note_hint(trig_kb: Path):
    """字段 trace：写入点来自操作插件且操作有触发点 → note 点名操作坐标；行/类不再挂明细。"""
    conn = store.open_kb(trig_kb)
    try:
        # CollateralOp（绑 cqkd_assetcard.audit）写 cqkd_collateralstatus → note 提示。
        rich = field_trace.field_trace(conn, "cqkd_collateralstatus", form_key="cqkd_assetcard")
        assert "cqkd_assetcard.audit" in rich["note"] and 'kind="operation"' in rich["note"]
        assert "programmatic_triggers" not in rich["groups"][0]["writers"][0]

        compact = field_trace.trace_compact(conn, "cqkd_collateralstatus",
                                            form_key="cqkd_assetcard")
        assert "cqkd_assetcard.audit" in compact["note"]
        assert "programmatic_triggers" not in compact["groups"][0]["writers"]["classes"][0]
    finally:
        conn.close()


def test_no_triggers_no_noise(tmp_path: Path):
    """无触发点的库：bill/trace 不出现触发点字段与提示；操作坐标追踪诚实返回空三段。"""
    db = make_kb(tmp_path)
    conn = store.open_kb(db)
    try:
        bv = bill_view.bill_view(conn, "cqkd_assetcard")
        assert all(o["programmatic_trigger_count"] == 0 for o in bv["operations"])
        assert bv["outbound_triggers"] == []
        assert bv["stats"]["unresolved_trigger_count"] == 0
        res = bill_view.bill_compact(conn, "cqkd_assetcard")
        audit = next(o for o in res["operations"] if o["key"] == "audit")
        assert "programmatic_trigger_count" not in audit
        assert "outbound_triggers" not in res
        assert "程序化操作触发点" not in res["note"]

        rich = field_trace.field_trace(conn, "cqkd_collateralstatus", form_key="cqkd_assetcard")
        assert "程序化触发点" not in (rich["note"] or "")

        ot = op_trace.operation_trace(conn, "cqkd_assetcard.audit")
        assert ot["triggered_by"] == [] and ot["unresolved_inbound"] == []
        assert "未发现" in ot["note"]
        cot = op_trace.operation_trace_compact(conn, "cqkd_assetcard.audit")
        assert cot["pagination"]["complete"] is True
        assert "unresolved_inbound" not in cot and "triggers_downstream" not in cot
    finally:
        conn.close()


# ── 入口回溯（entry_chains：触发点 → 插件事件入口） ──────────────────────────


def _exec_rows(db: Path, sql: str, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.executemany(sql, rows)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def chain_kb(trig_kb: Path) -> Path:
    """在 trig_kb 上叠加入口回溯素材：
      * ContractAuditOp.afterExecuteOperationTransaction 补事件行 → 既有触发点即 self_entry；
      * DeepSvc.fireAudit（新触发点，埋在 service 里）← ContractBillPlugin.itemClick（事件入口，
        expr 边）与 LegacyHelper.run（heuristic 边、无上游、非插件类 → no_static_caller）；
      * NightTask（孤儿任务插件类，orphan_role='plugin'）无上游 → plugin_boundary。"""
    _exec_rows(trig_kb,
               "INSERT INTO plugin_method(plugin_fqn,method_name,event_kind,event_phase,"
               "start_line,end_line,source_relpath) VALUES(?,?,?,?,?,?,?)", [
                   ("cqspb.assets.ContractAuditOp", "afterExecuteOperationTransaction",
                    "afterExecuteOperationTransaction", "transaction", 80, 95,
                    "cqspb/assets/ContractAuditOp.java"),
                   ("cqspb.assets.ContractBillPlugin", "itemClick", "itemClick", "memory",
                    40, 60, "cqspb/assets/ContractBillPlugin.java"),
                   ("cqspb.assets.NightTask", "execute", "helper", "helper", 10, 40,
                    "cqspb/assets/NightTask.java"),
               ])
    _exec_rows(trig_kb,
               "INSERT INTO plugin(uid,form_key,class_name,plugin_type,source,operation_key,"
               "operation_name,enabled) VALUES(?,?,?,?,?,?,?,?)", [
                   ("p3", "cqkd_contract", "cqspb.assets.ContractBillPlugin", "form",
                    "project", None, None, 1),
               ])
    _exec_rows(trig_kb,
               "INSERT INTO source_class(fqn,simple,package,relpath,module,is_orphan,"
               "orphan_role,plugin_base) VALUES(?,?,?,?,?,?,?,?)", [
                   ("cqspb.assets.NightTask", "NightTask", "cqspb.assets",
                    "cqspb/assets/NightTask.java", "cqkd_assets", 1, "plugin", "AbstractTask"),
                   ("cqspb.assets.DeepSvc", "DeepSvc", "cqspb.assets",
                    "cqspb/assets/DeepSvc.java", "cqkd_assets", 1, None, None),
               ])
    _exec_rows(trig_kb,
               "INSERT INTO call_edge(caller_fqn,caller_method,target_fqn,target_method,"
               "target_signature,kind,line,col,source_relpath,resolution,target_kind,"
               "confidence,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                   ("cqspb.assets.ContractBillPlugin", "itemClick",
                    "cqspb.assets.DeepSvc", "fireAudit", None, "invocation", 44, 9,
                    "cqspb/assets/ContractBillPlugin.java", "expr", "project", 1.0,
                    "symbol:expr"),
                   ("cqspb.assets.LegacyHelper", "run",
                    "cqspb.assets.DeepSvc", "fireAudit", None, "invocation", 12, 5,
                    "cqspb/assets/LegacyHelper.java", "heuristic", "project", 0.6,
                    "fallback=tree-sitter-local"),
               ])
    _add_triggers(trig_kb, [
        _trig("cqspb.assets.DeepSvc", "fireAudit", 90),
    ])
    return trig_kb


def test_entry_chains_reach_plugin_entry(chain_kb: Path):
    """触发点埋在 service 深处：沿 call_edge 向上回溯到插件事件入口（最短链 + 逐跳调用边证据）；
    heuristic 边的链降级 likely；追不到上游且非插件类的链 terminal=no_static_caller。"""
    conn = store.open_kb(chain_kb)
    try:
        ot = op_trace.operation_trace(conn, "cqkd_assetcard.audit")
        deep = next(t for t in ot["triggered_by"]
                    if t["caller_class"] == "cqspb.assets.DeepSvc")
        ec = deep["entry_chains"]
        assert ec["status"] == "reached"
        entry_chain = ec["chains"][0]                      # entry 链排最前
        assert entry_chain["terminal"] == "entry"
        assert entry_chain["confidence"] == "confirmed"     # expr 边，全链强证据
        assert entry_chain["entry"]["event"] == "itemClick"
        assert entry_chain["entry"]["phase"] == "memory"
        assert [b["form_key"] for b in entry_chain["entry"]["bindings"]] == ["cqkd_contract"]
        hops = entry_chain["hops"]
        assert [(h["class"], h["method"]) for h in hops] == [
            ("cqspb.assets.ContractBillPlugin", "itemClick"),
            ("cqspb.assets.DeepSvc", "fireAudit"),
        ]
        assert (hops[0]["call_line"], hops[0]["call_resolution"]) == (44, "expr")

        # heuristic 边的另一条链：LegacyHelper 无上游且非插件类 → no_static_caller/unknown。
        legacy = next(c for c in ec["chains"]
                      if c["hops"][0]["class"] == "cqspb.assets.LegacyHelper")
        assert legacy["terminal"] == "no_static_caller"
        assert legacy["confidence"] == "unknown"
        assert "entry_chains=" in ot["note"] and "no_static_caller" in ot["note"]
    finally:
        conn.close()


def test_entry_chains_self_entry_and_boundary(chain_kb: Path):
    """触发点本身是事件方法 → self_entry；孤儿任务插件类追不到上游 → plugin_boundary(likely)。"""
    conn = store.open_kb(chain_kb)
    try:
        ot = op_trace.operation_trace(conn, "cqkd_assetcard.audit")
        cao = next(t for t in ot["triggered_by"]
                   if t["caller_class"] == "cqspb.assets.ContractAuditOp")
        assert cao["entry_chains"]["status"] == "self_entry"
        assert cao["entry_chains"]["entry"]["event"] == "afterExecuteOperationTransaction"

        night = next(t for t in ot["unresolved_inbound"]
                     if t["caller_class"] == "cqspb.assets.NightTask")
        nec = night["entry_chains"]
        assert nec["status"] == "boundary_only"
        assert nec["chains"][0]["terminal"] == "plugin_boundary"
        assert nec["chains"][0]["confidence"] == "likely"
        assert nec["chains"][0]["entry"]["plugin_base"] == "AbstractTask"
    finally:
        conn.close()


def test_entry_chains_compact_and_render(chain_kb: Path):
    """紧凑投影：入站行带压缩后的 entry_chains（路径字符串 + 入口摘要）；人读渲染画出入口链。"""
    conn = store.open_kb(chain_kb)
    try:
        res = op_trace.operation_trace_compact(conn, "cqkd_assetcard.audit")
        assert field_trace._wire_len(res) <= field_trace._COMPACT_BUDGET
        deep = next(t for t in res["triggered_by"]
                    if t["caller_class"] == "cqspb.assets.DeepSvc")
        sec = deep["entry_chains"]
        assert sec["status"] == "reached"
        top = sec["chains"][0]
        assert top["entry"] == {"event": "itemClick", "phase": "memory",
                                "forms": ["cqkd_contract"]}
        assert top["path"] == [
            "cqspb.assets.ContractBillPlugin#itemClick@cqspb/assets/ContractBillPlugin.java:44",
            "cqspb.assets.DeepSvc#fireAudit",
        ]
        cao = next(t for t in res["triggered_by"]
                   if t["caller_class"] == "cqspb.assets.ContractAuditOp")
        assert cao["entry_chains"]["status"] == "self_entry"

        text = op_trace.render_operation_trace(op_trace.operation_trace(
            conn, "cqkd_assetcard.audit"))
        assert "入口链" in text and "事件=itemClick/memory" in text
        assert "触发点本身即插件事件入口" in text
    finally:
        conn.close()
