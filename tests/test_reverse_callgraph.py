"""孤立方法反向调用图回填（doc §5 #1）验收测试。

针对「孤立方法 DynamicObject 入参」桶（form_key 未定位最大杠杆）：未被任何事件 BFS 覆盖、
DO 形参不知来源的 helper，若在项目内**唯一被可解析地调用**、且调用方实参来源已知，就沿
「实参↔形参」把来源传播进来重跑、回填 form_key（source=reverse_callgraph）。

单元层（需 tree-sitter）：内存源码建真实 ProjectGraph → 跑 `_analyze_standalone` 造 None 行 →
`_backfill_reverse_calls`，覆盖唯一调用方成功 / 链式传播 / 多调用点一致成功 / 冲突或未知留 None / 零调用方 / 递归留 None。
端到端层：跑全管线确认回填经 store 落到 `field_access.form_key_source`，且排在元数据兜底之前
（字段 key 元数据**歧义**、metadata 救不了，唯有反向调用图能定）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("tree_sitter_java")

from cosmic_kb.java import analyze as an
from cosmic_kb.java import project_graph as pgmod


def _project_graph(src: str, relpath: str = "Rev.java") -> "pgmod.ProjectGraph":
    """从内存源码建真实 ProjectGraph（build_project_graph 只读 ok_files 的 relpath/text）。"""
    scan = SimpleNamespace(ok_files=[SimpleNamespace(relpath=relpath, text=src)])
    return pgmod.build_project_graph(scan, None)


def _pipeline(src: str, known=("cqkd_bill",)) -> dict:
    """标准化：建 pg → 对每个类跑 standalone 补扫造行 → 反向调用图回填 → 返回 {field_key: row}。"""
    pg = _project_graph(src)
    known_fs = frozenset(known)
    result = an.AnalysisResult()
    for _fqn, node in pg.classes.items():
        an._analyze_standalone(pg, node, pg.const, known_fs,
                               plugin_base={}, bound_entity={}, covered=set(), result=result)
    an._backfill_reverse_calls(result, pg, pg.const, known_fs, {})
    return {r.field_key: r for r in result.field_accesses}


# ── 唯一调用方 + 实参来源已知 → 回填 ───────────────────────────────────────────

_SUCCESS = """package cqspb.am;
public class RevCaller {
  private RevHelper helper = new RevHelper();
  public void run(Object id) {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, "cqkd_bill");
    helper.fill(bill);
  }
}
class RevHelper {
  public void fill(DynamicObject o) {
    o.set("cqkd_x", 1);
    Object v = o.get("cqkd_y");
  }
}
"""


def test_unique_caller_orm_source_backfilled():
    """唯一调用方实参来自 ORM loadSingle("cqkd_bill") → helper 的写/读都回填 form=cqkd_bill。"""
    by = _pipeline(_SUCCESS)
    w = by["cqkd_x"]
    assert w.form_key == "cqkd_bill"
    assert w.form_key_source == "reverse_callgraph"
    assert w.level == "header" and w.entry_key is None
    assert w.access == "write" and w.confidence <= 0.85
    assert "反向调用图" in w.evidence and "RevCaller.run" in w.evidence
    # 读访问同样回填（located 不区分读写，按 line/field_key/access 命中）。
    assert by["cqkd_y"].form_key == "cqkd_bill"
    assert by["cqkd_y"].form_key_source == "reverse_callgraph"


# ── 多调用点来源一致 → 回填；冲突/未知 → 留 None（红线 #4）────────────────────

_MULTI = """package cqspb.am;
public class RevCallerA {
  private RevHelperM helper = new RevHelperM();
  public void run(Object id) {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, "cqkd_bill");
    helper.fill(bill);
  }
}
class RevCallerB {
  private RevHelperM helper = new RevHelperM();
  public void go(Object id) {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, "cqkd_bill");
    helper.fill(bill);
  }
}
class RevHelperM {
  public void fill(DynamicObject o) { o.set("cqkd_x", 1); }
}
"""


def test_multiple_callers_same_source_backfilled():
    """helper 被两处调用，但实参来源完全一致 → 可安全回填。"""
    by = _pipeline(_MULTI)
    r = by["cqkd_x"]
    assert r.form_key == "cqkd_bill"
    assert r.form_key_source == "reverse_callgraph"
    assert r.confidence <= 0.80
    assert "多调用方来源一致传播" in r.evidence


_MULTI_CONFLICT = """package cqspb.am;
public class RevCallerA {
  private RevHelperM helper = new RevHelperM();
  public void run(Object id) {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, "cqkd_bill");
    helper.fill(bill);
  }
}
class RevCallerB {
  private RevHelperM helper = new RevHelperM();
  public void go(Object id) {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, "cqkd_other");
    helper.fill(bill);
  }
}
class RevHelperM {
  public void fill(DynamicObject o) { o.set("cqkd_x", 1); }
}
"""


def test_multiple_callers_conflicting_sources_stays_none():
    """多调用方实参来源冲突 → 不回填。"""
    by = _pipeline(_MULTI_CONFLICT, known=("cqkd_bill", "cqkd_other"))
    assert by["cqkd_x"].form_key is None
    assert by["cqkd_x"].form_key_source is None


_MULTI_UNKNOWN = """package cqspb.am;
public class RevCallerA {
  private RevHelperM helper = new RevHelperM();
  public void run(Object id) {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, "cqkd_bill");
    helper.fill(bill);
  }
}
class RevCallerB {
  private RevHelperM helper = new RevHelperM();
  public void go(DynamicObject x) { helper.fill(x); }
}
class RevHelperM {
  public void fill(DynamicObject o) { o.set("cqkd_x", 1); }
}
"""


def test_multiple_callers_with_unknown_source_stays_none():
    """多调用方中有实参来源未知 → 不能用另一处已知来源替它做整体归属。"""
    by = _pipeline(_MULTI_UNKNOWN)
    assert by["cqkd_x"].form_key is None
    assert by["cqkd_x"].form_key_source is None


# ── 链式 helper → 固定点逐跳传播 ─────────────────────────────────────────────

_CHAIN = """package cqspb.am;
public class RevChainCaller {
  private RevChainService service = new RevChainService();
  public void run(Object id) {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, "cqkd_bill");
    service.step1(bill);
  }
}
class RevChainService {
  private RevChainHelper helper = new RevChainHelper();
  public void step1(DynamicObject a) { helper.step2(a); }
}
class RevChainHelper {
  private RevChainUtil util = new RevChainUtil();
  public void step2(DynamicObject b) { util.step3(b); }
}
class RevChainUtil {
  public void step3(DynamicObject c) { c.set("cqkd_x", 1); }
}
"""


def test_three_hop_chain_backfilled():
    """ORM 来源经 service/helper/util 三跳传播到末端孤立方法。"""
    by = _pipeline(_CHAIN)
    r = by["cqkd_x"]
    assert r.form_key == "cqkd_bill"
    assert r.form_key_source == "reverse_callgraph"
    assert r.confidence <= 0.80
    assert "链式传播" in r.evidence


_INLINE_NEW = """package cqspb.am;
public class RevInlineNewCaller {
  public void run(Object id) {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, "cqkd_bill");
    new RevInlineNewHelper().fill(bill);
  }
}
class RevInlineNewHelper {
  public void fill(DynamicObject o) { o.set("cqkd_x", 1); }
}
"""


def test_inline_new_receiver_backfilled():
    """`new Helper().fill(bill)` 的显式构造接收者也能解析为项目内调用边。"""
    by = _pipeline(_INLINE_NEW)
    assert by["cqkd_x"].form_key == "cqkd_bill"
    assert by["cqkd_x"].form_key_source == "reverse_callgraph"


# ── 零调用方（真孤儿）→ 留 None ───────────────────────────────────────────────

_ORPHAN = """package cqspb.am;
class RevOrphan {
  public void fill(DynamicObject o) { o.set("cqkd_x", 1); }
}
"""


def test_zero_callers_stays_none():
    """helper 没有任何项目内调用方 → 无从回溯来源，留 None。"""
    by = _pipeline(_ORPHAN)
    assert by["cqkd_x"].form_key is None
    assert by["cqkd_x"].form_key_source is None


# ── 唯一调用方但实参来源未知 → 留 None ────────────────────────────────────────

_UNKNOWN = """package cqspb.am;
public class RevCallerU {
  private RevHelperU helper = new RevHelperU();
  public void run(DynamicObject x) { helper.fill(x); }
}
class RevHelperU {
  public void fill(DynamicObject o) { o.set("cqkd_x", 1); }
}
"""


def test_caller_arg_source_unknown_stays_none():
    """唯一调用方的实参本身（裸 DO 形参）也无来源 → prop 空，不臆造，留 None。"""
    by = _pipeline(_UNKNOWN)
    assert by["cqkd_x"].form_key is None
    assert by["cqkd_x"].form_key_source is None


# ── 递归自调用不触发回填 ──────────────────────────────────────────────────────

_RECUR = """package cqspb.am;
class RevRecur {
  public void fill(DynamicObject o) { o.set("cqkd_x", 1); fill(o); }
}
"""


def test_recursion_not_treated_as_caller():
    """自调用（递归）不进反向调用索引 → 等同零调用方，留 None。"""
    by = _pipeline(_RECUR)
    assert by["cqkd_x"].form_key is None


# ── 已定位行不被反向回填覆盖 ────────────────────────────────────────────────

def test_existing_located_row_not_overwritten():
    """反向回填只动 form_key=None 行，已定位的 data_flow 行保持原来源。"""
    row = an.FieldAccessRow(
        form_key="cqkd_other", field_key="cqkd_x", level="header", entry_key=None,
        plugin_fqn="cqspb.am.RevHelper", plugin_type="service",
        access_class="cqspb.am.RevHelper", event_method="fill", event_phase="unknown",
        access="write", persists="unknown", persist_reason=None, via="do.set",
        line=10, path=["fill"], key_resolution="literal", confidence=0.95,
        source_relpath="Rev.java", evidence="prelocated", form_key_source="data_flow",
    )
    pg = _project_graph(_SUCCESS)
    res = an.AnalysisResult(field_accesses=[row])
    an._backfill_reverse_calls(res, pg, pg.const, frozenset({"cqkd_bill", "cqkd_other"}), {})
    assert row.form_key == "cqkd_other"
    assert row.form_key_source == "data_flow"
    assert row.confidence == 0.95


# ── 端到端：回填经 store 落库，且排在元数据兜底之前（元数据歧义救不了）──────────

def test_e2e_reverse_callgraph_beats_metadata(tmp_path):
    """字段 key 元数据**多单据歧义**（metadata_unique/binding/cooccur 都救不了），唯一调用方实参
    来源（ORM）能把它定到 cqkd_bill → 经 store 落库 form_key_source=reverse_callgraph。

    两个普通 service（非苍穹插件、未绑定）→ 不走事件 BFS → 走 standalone（helper 写入 form_key=None）
    → 反向调用图回填先于元数据兜底定下来源。
    """
    from cosmic_kb.bridge import linker, namespace
    from cosmic_kb.graph import store
    from cosmic_kb.ingest import scanner
    from cosmic_kb.metadata.model import MetaEntity, MetaField, MetaModel
    from cosmic_kb.report import project_map

    CALLER = """package cqspb.am;
import kd.bos.servicehelper.BusinessDataServiceHelper;
public class RevSvcCaller {
  private RevSvcHelper helper = new RevSvcHelper();
  public void run(Object id) {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, "cqkd_bill");
    helper.fill(bill);
  }
}
"""
    HELPER = """package cqspb.am;
public class RevSvcHelper {
  public void fill(DynamicObject o) {
    o.set("cqkd_amb", 1);
  }
}
"""
    src = tmp_path / "src"
    for name, txt in (("RevSvcCaller.java", CALLER), ("RevSvcHelper.java", HELPER)):
        p = src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(txt.encode("utf-8"))
    scan = scanner.scan(src)

    # cqkd_amb 在两张单据都有同名字段 → 元数据反查歧义、无法收敛。
    def _model(form):
        ent = [MetaEntity("BillEntity", form, "单据头", "1", "header", None, "t")]
        fld = [MetaField("TextField", "cqkd_amb", "金额", "fcqkd_amb", "idcqkd_amb",
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
        r = conn.execute(
            "SELECT form_key, form_key_source, level FROM field_access "
            "WHERE field_key='cqkd_amb' AND access_class='cqspb.am.RevSvcHelper'").fetchone()
        assert r is not None
        assert r["form_key"] == "cqkd_bill"
        assert r["form_key_source"] == "reverse_callgraph"
        assert r["level"] == "header"
    finally:
        conn.close()
