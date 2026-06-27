"""form_key 解析率提升 · 字段key反查回填（待办一三层）+ 待办二习语 验收测试。

单元层：直测 `_backfill_form_key` 三层收敛（①唯一反查 ②绑定收敛 ③同对象共现交集）+ 留 None 的诚实
边界；`_field_form_index` 建索引。
端到端层（需 tree-sitter）：跑全管线确认 `new DynamicObject(coll.getDynamicObjectType())` 习语
（待办二）数据流解析、字段 key 反查回填经 store 落到 `field_access.form_key_source`。
"""

from __future__ import annotations

from cosmic_kb.java.analyze import (
    AnalysisResult, FieldAccessRow, _backfill_form_key, _field_form_index,
)
from cosmic_kb.metadata.model import MetaField, MetaModel


# ── 单元：_backfill_form_key 三层 ───────────────────────────────────────────

def _row(field_key, *, form_key=None, access_class="cqspb.Svc", plugin_fqn="cqspb.Svc",
         receiver_var=None, level="unknown", entry_key=None, source_relpath="S.java",
         event_method="m") -> FieldAccessRow:
    return FieldAccessRow(
        form_key=form_key, field_key=field_key, level=level, entry_key=entry_key,
        plugin_fqn=plugin_fqn, plugin_type="service", access_class=access_class,
        event_method=event_method, event_phase="unknown", access="write",
        persists="unknown", persist_reason=None, via="do.set", line=1, path=["m"],
        key_resolution="literal", confidence=0.95, source_relpath=source_relpath,
        form_key_source="data_flow" if form_key else None, receiver_var=receiver_var,
    )


def _run(rows, field_idx, bound_entity=None):
    res = AnalysisResult(field_accesses=list(rows))
    _backfill_form_key(res, field_idx, bound_entity or {})
    return res.field_accesses


def test_layer1_unique_reverse():
    """字段 key 在元数据只归一个单据 → 直接定 form_key + level/entry_key（物理硬约束）。"""
    idx = {"cqkd_only": [("cqkd_bill", "cqkd_entry", "entry")]}
    (r,) = _run([_row("cqkd_only")], idx)
    assert r.form_key == "cqkd_bill"
    assert r.form_key_source == "metadata_unique"
    assert r.level == "entry"
    assert r.entry_key == "cqkd_entry"
    assert r.confidence <= 0.9
    assert "字段归属唯一" in r.evidence


def test_layer1_header_entry_key_none():
    """表头字段反查：entry_key 归 None（不是元数据里的主实体 key）。"""
    idx = {"cqkd_h": [("cqkd_bill", "cqkd_bill", "header")]}
    (r,) = _run([_row("cqkd_h")], idx)
    assert r.form_key == "cqkd_bill"
    assert r.level == "header"
    assert r.entry_key is None


def test_layer2_binding_converge_by_access_class():
    """多候选 + 写它的 access_class 绑定其中一张单据 → 绑定收敛定它。"""
    idx = {"cqkd_m": [("cqkd_a", None, "header"), ("cqkd_b", None, "header")]}
    rows = _run([_row("cqkd_m", access_class="cqspb.P")], idx, {"cqspb.P": {"cqkd_b"}})
    assert rows[0].form_key == "cqkd_b"
    assert rows[0].form_key_source == "metadata_binding"


def test_layer2_binding_converge_by_plugin_fqn():
    """入口插件（plugin_fqn）的绑定单据也参与收敛（service 类被绑定插件调用的情形）。"""
    idx = {"cqkd_m": [("cqkd_a", None, "header"), ("cqkd_b", None, "header")]}
    rows = _run([_row("cqkd_m", access_class="cqspb.Svc", plugin_fqn="cqspb.Plugin")],
                idx, {"cqspb.Plugin": {"cqkd_a"}})
    assert rows[0].form_key == "cqkd_a"
    assert rows[0].form_key_source == "metadata_binding"


def test_layer3_cooccurrence_intersection():
    """同接收者变量连写多字段：唯一字段把变量钉到 cqkd_a，歧义字段经交集塌缩到 cqkd_a。"""
    idx = {
        "cqkd_ux": [("cqkd_a", None, "header")],                       # 唯一 → ①
        "cqkd_uy": [("cqkd_a", None, "header")],                       # 唯一 → ①
        "cqkd_amb": [("cqkd_a", None, "header"), ("cqkd_b", None, "header"),
                     ("cqkd_c", None, "header")],                      # 歧义 → ③
    }
    rows = _run([
        _row("cqkd_ux", receiver_var="subRow"),
        _row("cqkd_uy", receiver_var="subRow"),
        _row("cqkd_amb", receiver_var="subRow"),
    ], idx)
    by = {r.field_key: r for r in rows}
    assert by["cqkd_ux"].form_key == "cqkd_a" and by["cqkd_ux"].form_key_source == "metadata_unique"
    assert by["cqkd_amb"].form_key == "cqkd_a"
    assert by["cqkd_amb"].form_key_source == "metadata_cooccur"


def test_ambiguous_stays_none():
    """多候选、无绑定、无共现 → 诚实留 None（红线 #4）。"""
    idx = {"cqkd_m": [("cqkd_a", None, "header"), ("cqkd_b", None, "header")]}
    (r,) = _run([_row("cqkd_m")], idx)
    assert r.form_key is None
    assert r.form_key_source is None


def test_cooccurrence_mixed_no_intersection_stays_none():
    """同变量两字段候选无交集 → 不臆造，留 None。"""
    idx = {
        "cqkd_p": [("cqkd_a", None, "header"), ("cqkd_b", None, "header")],
        "cqkd_q": [("cqkd_c", None, "header"), ("cqkd_d", None, "header")],
    }
    rows = _run([_row("cqkd_p", receiver_var="o"), _row("cqkd_q", receiver_var="o")], idx)
    assert all(r.form_key is None for r in rows)


def test_field_key_none_stays_none():
    """字段 key 本身解不出（None）→ 无从反查，留 None。"""
    (r,) = _run([_row(None)], {"cqkd_x": [("cqkd_a", None, "header")]})
    assert r.form_key is None


def test_existing_form_key_not_overwritten():
    """已由数据流解析出 form_key 的行不被回填覆盖。"""
    idx = {"cqkd_only": [("cqkd_other", None, "header")]}
    (r,) = _run([_row("cqkd_only", form_key="cqkd_real")], idx)
    assert r.form_key == "cqkd_real"
    assert r.form_key_source == "data_flow"


def test_field_form_index():
    """_field_form_index：按字段 key 聚合 (form, entity_key, level)，跳过 key=None。"""
    m = MetaModel(key="cqkd_bill", name="单", model_type="BillFormModel", form_type="bill",
                  isv="cqkd", fields=[
                      MetaField("TextField", "cqkd_a", "甲", None, "i1", None, "entity", "header", "cqkd_bill"),
                      MetaField("TextField", "cqkd_b", "乙", None, "i2", "p", "entity", "entry", "cqkd_entry"),
                      MetaField("TextField", None, "覆盖", None, "i3", None, "entity", "header", "cqkd_bill"),
                  ])
    idx = _field_form_index([m])
    assert idx["cqkd_a"] == [("cqkd_bill", "cqkd_bill", "header")]
    assert idx["cqkd_b"] == [("cqkd_bill", "cqkd_entry", "entry")]
    assert None not in idx and len(idx) == 2


# ── 端到端（需 tree-sitter）：待办二习语 + 反查回填经 store 落库 ───────────────

def _e2e(tmp_path):
    import pytest
    pytest.importorskip("tree_sitter_java")
    from cosmic_kb.bridge import linker, namespace
    from cosmic_kb.graph import store
    from cosmic_kb.ingest import scanner
    from cosmic_kb.metadata.model import MetaEntity, MetaOperation, MetaPlugin
    from cosmic_kb.report import project_map

    # 待办二：绑定操作插件在事务内取分录集合 → new DynamicObject(coll.getDynamicObjectType())
    #         造新行并写字段，新行应继承分录集合坐标（form=cqkd_bill / entry / cqkd_entry）。
    NEWDO_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class AmNewDoOp extends AbstractOperationServicePlugIn {
  public void beforeExecuteOperationTransaction(BeforeOperationArgs e) {
    DynamicObject bill = e.getDataEntities()[0];
    DynamicObjectCollection coll = bill.getDynamicObjectCollection("cqkd_entry");
    DynamicObject row = new DynamicObject(coll.getDynamicObjectType());
    row.set("cqkd_entryf", 1);
  }
}
"""
    # 反查回填：孤立 service 拿 Map 掏出的 DynamicObject（来源数据流断链），写一个元数据里**唯一**
    #          归属 cqkd_bill 的字段 cqkd_uniqf → 应被字段 key 反查回填 form=cqkd_bill。
    LONE_SVC = """package cqspb.am;
public class AmReverseService {
  public void fill(java.util.Map<String,DynamicObject> m) {
    DynamicObject obj = m.get("k");
    obj.set("cqkd_uniqf", 9);
  }
}
"""
    src = tmp_path / "src"
    for name, txt in (("AmNewDoOp.java", NEWDO_OP), ("AmReverseService.java", LONE_SVC)):
        p = src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(txt.encode("utf-8"))
    scan = scanner.scan(src)

    ents = [MetaEntity("BillEntity", "cqkd_bill", "单据头", "1", "header", None, "t"),
            MetaEntity("EntryEntity", "cqkd_entry", "分录", "2", "entry", "1", "te")]
    flds = [MetaField("TextField", k, k, "f" + k, "id" + k, "1", "entity", lvl, ent)
            for k, lvl, ent in (("cqkd_entryf", "entry", "cqkd_entry"),
                                ("cqkd_uniqf", "header", "cqkd_bill"))]
    plugins = [MetaPlugin("cqspb.am.AmNewDoOp", "op", "project", operation_key="submit")]
    ops = [MetaOperation("submit", "提交", "submit", None, None, resolved_from="self")]
    m1 = MetaModel(key="cqkd_bill", name="资产单", model_type="BillFormModel", form_type="bill",
                   isv="cqkd", app_key="cqkd_am", entities=ents, fields=flds,
                   plugins=plugins, operations=ops)
    index = namespace.build_index(scan)
    bridge = linker.link(scan, [m1], index=index)
    mm = project_map.module_map(scan, [m1], bridge, index=index)
    db = tmp_path / "kb.db"
    store.build_kb(scan, [m1], bridge, mm, db, index=index)
    return store.open_kb(db)


def test_e2e_newdynamicobject_idiom(tmp_path):
    """待办二：new DynamicObject(coll.getDynamicObjectType()) 新行继承分录集合坐标（数据流解析）。"""
    conn = _e2e(tmp_path)
    try:
        r = conn.execute(
            "SELECT form_key, level, entry_key, form_key_source FROM field_access "
            "WHERE field_key='cqkd_entryf'").fetchone()
        assert r is not None
        assert (r["form_key"], r["level"], r["entry_key"]) == ("cqkd_bill", "entry", "cqkd_entry")
        assert r["form_key_source"] == "data_flow"
    finally:
        conn.close()


def test_e2e_metadata_reverse_backfill(tmp_path):
    """反查回填：来源断链但字段 key 元数据唯一 → form_key 回填 + source=metadata_unique。"""
    conn = _e2e(tmp_path)
    try:
        r = conn.execute(
            "SELECT form_key, form_key_source FROM field_access "
            "WHERE field_key='cqkd_uniqf'").fetchone()
        assert r is not None
        assert r["form_key"] == "cqkd_bill"
        assert r["form_key_source"] == "metadata_unique"
    finally:
        conn.close()
