"""未定位成因（null_reason）确定性落库 + 暴露 验收测试（信任优先，红线 #4）。

三层：
- 纯逻辑层：`null_reason.classify` 每个成因码各一例 + 优先级归因（不需 tree-sitter）。
- 管线层（需 tree-sitter）：内存源码跑 standalone + 回填 + 定稿，确认各成因落到 FieldAccessRow，
  且被反向调用图救活的行成因清空（form_key 定位则 null_reason=None）。
- 端到端层：跑全管线经 store 落库，确认 `field_access.null_reason` 列被填、已定位行为 NULL，
  且 `field_trace` 返回 `summary.unlocated_by_reason` 直方图 + unlocated 工作单带成因。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cosmic_kb.java import null_reason as nr


# ── 纯逻辑层：classify 每个成因码 + 优先级 ──────────────────────────────────────

def _row(**kw):
    """构造 classify 的输入（dict 口径，含 form_key=None 默认即「未定位」）。"""
    base = dict(form_key=None, field_key="cqkd_x", level="header", via="do.set",
                key_resolution="literal", evidence=None)
    base.update(kw)
    return base


def test_located_row_has_no_reason():
    assert nr.classify(_row(form_key="cqkd_bill")) is None


def test_field_key_undeterminable_by_none():
    assert nr.classify(_row(field_key=None)) == nr.FIELD_KEY_UNDETERMINABLE


@pytest.mark.parametrize("kres", ["dynamic", "dynamic-loop", "concat", "external-const",
                                  "unknown", "ambiguous"])
def test_field_key_undeterminable_by_key_resolution(kres):
    assert nr.classify(_row(key_resolution=kres)) == nr.FIELD_KEY_UNDETERMINABLE


def test_basedata_ref():
    assert nr.classify(_row(level="basedata")) == nr.BASEDATA_REF


def test_dynamic_entity_from_note_constant():
    ev = "BaseCon.ID；" + nr.NOTE_DYNAMIC_ENTITY
    assert nr.classify(_row(evidence=ev)) == nr.DYNAMIC_ENTITY


def test_helper_caller_unknown():
    assert nr.classify(_row(evidence=nr.NOTE_HELPER_CALLER_UNKNOWN)) == nr.HELPER_CALLER_UNKNOWN
    assert nr.classify(_row(evidence=nr.NOTE_HELPER_CALLER_UNKNOWN_ARR)) == nr.HELPER_CALLER_UNKNOWN


def test_local_or_container_source():
    assert nr.classify(_row(evidence=nr.NOTE_SOURCE_UNIDENTIFIED)) == nr.LOCAL_OR_CONTAINER_SOURCE


def test_model_context():
    assert nr.classify(_row(via="model.setValue", evidence=None)) == nr.MODEL_CONTEXT


def test_unknown_fallback():
    assert nr.classify(_row(evidence=None, via="do.set")) == nr.UNKNOWN


# 优先级：字段 key 钉不出 凌驾 来源/基础资料/model；基础资料 凌驾 model。
def test_priority_field_key_beats_source():
    assert nr.classify(_row(key_resolution="concat",
                            evidence=nr.NOTE_DYNAMIC_ENTITY)) == nr.FIELD_KEY_UNDETERMINABLE


def test_priority_basedata_beats_model():
    assert nr.classify(_row(level="basedata", via="model.setValue")) == nr.BASEDATA_REF


def test_reason_label_covers_all_codes():
    for code in nr.ALL_REASONS:
        assert code in nr.REASON_LABEL


def test_classify_accepts_object_rows():
    """非 dict（dataclass/带属性对象）也能取值。"""
    obj = SimpleNamespace(form_key=None, field_key=None, level="header", via="do.set",
                          key_resolution="literal", evidence=None)
    assert nr.classify(obj) == nr.FIELD_KEY_UNDETERMINABLE


# ── 管线层（需 tree-sitter）──────────────────────────────────────────────────

pytest.importorskip("tree_sitter_java")

from cosmic_kb.java import analyze as an          # noqa: E402
from cosmic_kb.java import project_graph as pgmod  # noqa: E402


def _pipeline(src: str, known=("cqkd_bill",)) -> list:
    pg = pgmod.build_project_graph(
        SimpleNamespace(ok_files=[SimpleNamespace(relpath="N.java", text=src)]), None)
    known_fs = frozenset(known)
    result = an.AnalysisResult()
    for _fqn, node in pg.classes.items():
        an._analyze_standalone(pg, node, pg.const, known_fs,
                               plugin_base={}, bound_entity={}, covered=set(), result=result)
    an._backfill_reverse_calls(result, pg, pg.const, known_fs, {})
    result.field_accesses = an._dedup(result.field_accesses)
    an._finalize_null_reason(result)
    return result.field_accesses


_MIX = """package cqspb.am;
import kd.bos.servicehelper.BusinessDataServiceHelper;
public class Mix {
  public void fill(DynamicObject obj){ obj.set("cqkd_amt", 1); }       // helper-caller-unknown
  public void dyn(String ent){
    DynamicObject o = BusinessDataServiceHelper.loadSingle(id, ent);   // 动态实体
    o.set("cqkd_zr", 2);                                               // dynamic-entity
  }
  public void local(){
    DynamicObject n = new DynamicObject();
    n.set("cqkd_xx", 3);                                               // local-or-container-source
  }
}
"""


def test_pipeline_reasons_land_on_rows():
    rows = {r.field_key: r for r in _pipeline(_MIX)}
    assert rows["cqkd_amt"].null_reason == nr.HELPER_CALLER_UNKNOWN
    assert rows["cqkd_zr"].null_reason == nr.DYNAMIC_ENTITY
    assert rows["cqkd_xx"].null_reason == nr.LOCAL_OR_CONTAINER_SOURCE
    # 全部 form_key=None（这些确实未定位）。
    assert all(r.form_key is None for r in rows.values())


_RESCUED = """package cqspb.am;
import kd.bos.servicehelper.BusinessDataServiceHelper;
public class Caller {
  private Helper helper = new Helper();
  public void run(Object id){
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, "cqkd_bill");
    helper.fill(bill);
  }
}
class Helper {
  public void fill(DynamicObject o){ o.set("cqkd_amt", 1); }
}
"""


def test_rescued_row_has_no_reason():
    """被反向调用图回填救活（form_key 定位）→ 成因清空（红线 #4：救活就不该再标未定位成因）。"""
    rows = {r.field_key: r for r in _pipeline(_RESCUED)}
    r = rows["cqkd_amt"]
    assert r.form_key == "cqkd_bill"
    assert r.form_key_source == "reverse_callgraph"
    assert r.null_reason is None


# ── 端到端层：经 store 落库 + field_trace 暴露 ─────────────────────────────────

def test_e2e_null_reason_persisted_and_exposed(tmp_path):
    from cosmic_kb.bridge import linker, namespace
    from cosmic_kb.graph import store
    from cosmic_kb.ingest import scanner
    from cosmic_kb.metadata.model import MetaEntity, MetaField, MetaModel
    from cosmic_kb.report import field_trace, project_map

    # 未绑定 service helper：DO 入参调用方未知 → form_key=None / null_reason=helper-caller-unknown。
    HELPER = """package cqspb.am;
public class FillHelper {
  public void fill(DynamicObject o){ o.set("cqkd_amt", 1); }
}
"""
    src = tmp_path / "src"
    p = src / "FillHelper.java"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(HELPER.encode("utf-8"))
    scan = scanner.scan(src)

    # cqkd_amt 同名出现在两张**未绑定** service 涉及不到的单据 → 元数据反查歧义、metadata_* 都救不了；
    # 又无可解析调用方 → 反向调用图救不了。故 form_key 留 None，成因=helper-caller-unknown（DO 入参未知）。
    def _model(form):
        ent = [MetaEntity("BillEntity", form, "单据头", "1", "header", None, "t")]
        fld = [MetaField("TextField", "cqkd_amt", "金额", "fcqkd_amt", "idcqkd_amt",
                         "1", "entity", "header", form)]
        return MetaModel(key=form, name=form, model_type="BillFormModel", form_type="bill",
                         isv="cqkd", app_key="cqkd_am", entities=ent, fields=fld)

    models = [_model("cqkd_bill"), _model("cqkd_other")]
    index = namespace.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    mm = project_map.module_map(scan, models, bridge, index=index)
    db = tmp_path / "kb.db"
    store.build_kb(scan, models, bridge, mm, db, index=index)

    conn = store.open_kb(db)
    try:
        # 列被填：未定位行有成因，已定位行为 NULL（不污染）。
        r = conn.execute(
            "SELECT form_key, null_reason FROM field_access WHERE field_key='cqkd_amt'").fetchone()
        assert r is not None
        assert r["form_key"] is None
        assert r["null_reason"] == nr.HELPER_CALLER_UNKNOWN
        assert conn.execute(
            "SELECT COUNT(*) FROM field_access WHERE form_key IS NOT NULL AND null_reason IS NOT NULL"
        ).fetchone()[0] == 0

        # 裸字段查询：summary 直方图统计本字段全部 form_key=None 行的成因。
        ft = field_trace.field_trace(conn, "cqkd_amt")
        assert ft["summary"]["unlocated_by_reason"].get(nr.HELPER_CALLER_UNKNOWN, 0) >= 1

        # 精确查询（给 form_key）：form_key=None 行落入「反推来源单据」工作单，带成因。
        ftp = field_trace.field_trace(conn, "cqkd_amt", form_key="cqkd_bill")
        methods = ftp["unlocated"]["methods"]
        assert methods and methods[0]["null_reason"] == nr.HELPER_CALLER_UNKNOWN
        assert ftp["unlocated"]["by_reason"].get(nr.HELPER_CALLER_UNKNOWN, 0) >= 1
    finally:
        conn.close()
