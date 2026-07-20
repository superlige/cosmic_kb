"""候选原厂 key 发现（dbmeta/discover.py）验收测试 —— 三类确定性信号版。

用户拍板（2026-07-03）放弃"字符串字面量形状过滤"的粗糙候选，改用三类确定性信号，
命中即"必定摄取"：① 本地扩展母体（`from_extensions`）② ORM 查询（`from_orm_calls`，
按重载参数位规则表）③ 操作执行（`from_operation_calls`，含 `save(var)` 同方法回溯）。
验收核心是**反例**：map/json 构造 key、字段名、操作编码常量、排序子句字符串等
此前会被"扫全文取首个字面量"误伤的噪声，现在全部不应命中。
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from cosmic_kb import _assets
from cosmic_kb.dbmeta.discover import (
    SignalHit, VendorCandidate, discover_candidates, from_extensions,
    from_operation_calls, from_orm_calls, isv_prefixes_from_db, known_keys_from_db,
    known_keys_from_models,
)
from cosmic_kb.java.constants import build_constant_table
from cosmic_kb.metadata import dym_parser
from cosmic_kb.metadata.model import MetaEntity, MetaField, MetaModel


def _scan_result(files: dict[str, str]):
    return SimpleNamespace(
        ok_files=[SimpleNamespace(relpath=path, text=text) for path, text in files.items()]
    )


def _const(files: dict[str, str]):
    return build_constant_table(_scan_result(files))


# ── SignalHit ────────────────────────────────────────────────────────────────

def test_signal_hit_caps_evidence_at_five():
    hit = SignalHit()
    for i in range(8):
        hit.add("A.java", i + 1)
    assert hit.count == 8
    assert len(hit.evidence) == 5


# ── known_keys_from_models：form + entity + field 三表 ───────────────────────

def test_known_keys_from_models_covers_form_entity_and_field_keys():
    m = MetaModel(
        key="cqkd_bill", name=None, model_type=None, form_type="bill", isv="cqkd",
        entities=[MetaEntity("BillEntity", "cqkd_bill", None, "h1", "header", None, None),
                  MetaEntity("EntryEntity", "cqkd_entry", None, "e1", "entry", "h1", None)],
        fields=[MetaField("TextField", "cqkd_amt", "金额", "famt", "f1", "h1",
                          "entity", "header", "cqkd_bill")],
    )
    assert known_keys_from_models([m]) == {"cqkd_bill", "cqkd_entry", "cqkd_amt"}


# ── 信号① from_extensions ────────────────────────────────────────────────────

def test_from_extensions_hits_via_detect_extension():
    m = MetaModel(key="cqkd_bd_customer_ext", name=None, model_type=None, form_type="basedata",
                  isv="cqkd", inherit_path=["root1"])
    assert from_extensions([m], {"cqkd_": 1}) == {"bd_customer": "cqkd_bd_customer_ext"}


def test_from_extensions_no_inherit_path_not_hit():
    m = MetaModel(key="cqkd_bd_customer_ext", name=None, model_type=None, form_type="basedata",
                  isv="cqkd", inherit_path=[])
    assert from_extensions([m], {"cqkd_": 1}) == {}


EXT_DYM = _assets.PROJECT_ROOT / "samples" / "bill" / "cqkd_bd_customer_ext.dym"
needs_sample = pytest.mark.skipif(not EXT_DYM.exists(), reason="缺 cqkd_bd_customer_ext 样例")


@needs_sample
def test_from_extensions_real_sample():
    model = dym_parser.parse_file(str(EXT_DYM))
    assert from_extensions([model], {"cqkd_": 1}) == {"bd_customer": "cqkd_bd_customer_ext"}


# ── 信号② from_orm_calls：重载消歧逐条 ───────────────────────────────────────

def test_from_orm_calls_load_literal_arg0_hits():
    hits = from_orm_calls(
        _scan_result({"A.java": 'BusinessDataServiceHelper.load("bd_customer", type);\n'}),
        known_keys=set(), isv_prefixes=set(),
        const_table=_const({"A.java": ""}),
    )
    assert set(hits) == {"bd_customer"}
    assert hits["bd_customer"].count == 1


def test_from_orm_calls_load_non_string_arg0_skipped_pks_overload():
    java = 'BusinessDataServiceHelper.load(pks, EntityMetadataCache.getDataEntityType("bd_customer"));\n'
    hits = from_orm_calls(_scan_result({"A.java": java}), set(), set(), _const({"A.java": ""}))
    assert hits == {}   # arg0 非字符串（pks+type 重载），诚实跳过——不回退去猜别的实参


def test_from_orm_calls_load_single_two_arg_takes_arg1():
    java = 'BusinessDataServiceHelper.loadSingle(id, "bd_customer");\n'
    hits = from_orm_calls(_scan_result({"A.java": java}), set(), set(), _const({"A.java": ""}))
    assert set(hits) == {"bd_customer"}


def test_from_orm_calls_load_single_three_arg_string_fields_takes_arg1():
    # (pk, entity, fields)：arg2 是字符串字面量（字段列表）→ 取 arg1。
    java = 'BusinessDataServiceHelper.loadSingle(pkId, "bd_customer", "id,name");\n'
    hits = from_orm_calls(_scan_result({"A.java": java}), set(), set(), _const({"A.java": ""}))
    assert set(hits) == {"bd_customer"}


def test_from_orm_calls_load_single_three_arg_qfilter_takes_arg0():
    # (entity, fields, QFilter[])：arg2 非字符串（变量）→ 取 arg0。
    java = 'BusinessDataServiceHelper.loadSingle("bd_customer", "id,name,amount", filters);\n'
    hits = from_orm_calls(_scan_result({"A.java": java}), set(), set(), _const({"A.java": ""}))
    assert set(hits) == {"bd_customer"}


def test_from_orm_calls_new_dynamic_object_takes_arg0():
    java = 'BusinessDataServiceHelper.newDynamicObject("bd_customer");\n'
    hits = from_orm_calls(_scan_result({"A.java": java}), set(), set(), _const({"A.java": ""}))
    assert set(hits) == {"bd_customer"}


def test_from_orm_calls_query_normal_takes_arg0():
    java = 'QueryServiceHelper.query("bd_customer", "fname", filters);\n'
    hits = from_orm_calls(_scan_result({"A.java": java}), set(), set(), _const({"A.java": ""}))
    assert set(hits) == {"bd_customer"}


def test_from_orm_calls_query_algo_key_overload_takes_arg1():
    # arg0 形如 "kd.xxx"（含点号）是 algoKey，不是实体标识 —— 真实实体在 arg1。
    java = 'QueryServiceHelper.query("kd.bos.abc", "bd_customer", "fname");\n'
    hits = from_orm_calls(_scan_result({"A.java": java}), set(), set(), _const({"A.java": ""}))
    assert set(hits) == {"bd_customer"}
    assert "kd.bos.abc" not in hits


def test_from_orm_calls_resolves_constant_reference():
    java = (
        'public class Foo {\n'
        '    static final String ENTITY = "bd_customer";\n'
        '    public void run() {\n'
        '        BusinessDataServiceHelper.loadSingle(id, ENTITY);\n'
        '    }\n'
        '}\n'
    )
    hits = from_orm_calls(_scan_result({"Foo.java": java}), set(), set(), _const({"Foo.java": java}))
    assert set(hits) == {"bd_customer"}


# ── 反例：此前"扫全文取首个字面量"会误伤，现应全部不命中 ─────────────────────

def test_from_orm_calls_ignores_map_json_construct_key():
    java = (
        'map.put("bd_customer", value);\n'
        'j.put("bd_supplier", 1);\n'
    )
    hits = from_orm_calls(_scan_result({"A.java": java}), set(), set(), _const({"A.java": ""}))
    assert hits == {}


def test_from_orm_calls_ignores_getstring_field_name():
    java = 'String v = model.getString("bd_someattr");\n'
    hits = from_orm_calls(_scan_result({"A.java": java}), set(), set(), _const({"A.java": ""}))
    assert hits == {}


def test_from_orm_calls_ignores_sort_clause_extra_arg():
    java = 'QueryServiceHelper.query("bd_customer", "fname", filters, "createtime desc");\n'
    hits = from_orm_calls(_scan_result({"A.java": java}), set(), set(), _const({"A.java": ""}))
    assert set(hits) == {"bd_customer"}   # 排序子句在未检查的参数位，根本不会被取到


def test_from_orm_calls_skips_non_java_files():
    sr = _scan_result({"A.txt": 'BusinessDataServiceHelper.newDynamicObject("bd_customer");\n'})
    assert from_orm_calls(sr, set(), set(), build_constant_table(sr)) == {}


# ── 过滤链：ISV 前缀 / fk_·tk_ 物理前缀 / 已知 key（含字段）───────────────────

def test_from_orm_calls_excludes_isv_prefix():
    java = 'BusinessDataServiceHelper.loadSingle(id, "cqkd_bill");\n'
    hits = from_orm_calls(_scan_result({"A.java": java}), set(), {"cqkd_"}, _const({"A.java": ""}))
    assert hits == {}


def test_from_orm_calls_excludes_physical_schema_prefix():
    java = 'BusinessDataServiceHelper.newDynamicObject("tk_cqkd_assetcard_soe");\n'
    hits = from_orm_calls(_scan_result({"A.java": java}), set(), set(), _const({"A.java": ""}))
    assert hits == {}


def test_from_orm_calls_excludes_known_field_key():
    java = 'BusinessDataServiceHelper.newDynamicObject("cqkd_htid");\n'
    hits = from_orm_calls(_scan_result({"A.java": java}), {"cqkd_htid"}, set(), _const({"A.java": ""}))
    assert hits == {}


# ── 信号③ from_operation_calls ──────────────────────────────────────────────

def test_from_operation_calls_execute_operate_takes_arg1_not_op_code():
    java = (
        'public class Foo {\n'
        '    public void run() {\n'
        '        OperationServiceHelper.executeOperate("save", "bd_customer", pks, option);\n'
        '    }\n'
        '}\n'
    )
    hits = from_operation_calls(_scan_result({"Foo.java": java}), set(), set(), _const({"Foo.java": java}))
    assert set(hits) == {"bd_customer"}
    assert "save" not in hits   # 操作编码（arg0）不当实体


def test_from_operation_calls_ignores_nonexistent_exec_operate_alias():
    java = (
        'public class Foo {\n'
        '    public void run() {\n'
        '        OperationServiceHelper.execOperate("save", "bd_customer", pks, option);\n'
        '    }\n'
        '}\n'
    )
    hits = from_operation_calls(_scan_result({"Foo.java": java}), set(), set(), _const({"Foo.java": java}))
    assert hits == {}


def test_from_operation_calls_delete_service_helper_takes_arg0():
    java = (
        'public class Foo {\n'
        '    public void run() {\n'
        '        DeleteServiceHelper.delete("bd_customer", filters);\n'
        '    }\n'
        '}\n'
    )
    hits = from_operation_calls(_scan_result({"Foo.java": java}), set(), set(), _const({"Foo.java": java}))
    assert set(hits) == {"bd_customer"}


def test_from_operation_calls_save_resolves_via_same_method_orm_init():
    java = (
        'public class Foo {\n'
        '    public void doSave() {\n'
        '        DynamicObject obj = BusinessDataServiceHelper.loadSingle(id, "bd_customer");\n'
        '        SaveServiceHelper.save(obj);\n'
        '    }\n'
        '}\n'
    )
    hits = from_operation_calls(_scan_result({"Foo.java": java}), set(), set(), _const({"Foo.java": java}))
    assert set(hits) == {"bd_customer"}
    assert hits["bd_customer"].count == 1


def test_from_operation_calls_save_var_reassignment_also_resolves():
    java = (
        'public class Foo {\n'
        '    public void doSave() {\n'
        '        DynamicObject obj;\n'
        '        obj = BusinessDataServiceHelper.loadSingle(id, "bd_customer");\n'
        '        SaveServiceHelper.save(obj);\n'
        '    }\n'
        '}\n'
    )
    hits = from_operation_calls(_scan_result({"Foo.java": java}), set(), set(), _const({"Foo.java": java}))
    assert set(hits) == {"bd_customer"}


def test_from_operation_calls_save_cross_method_var_honestly_skipped():
    java = (
        'public class Foo {\n'
        '    private DynamicObject obj;\n'
        '    public void init() {\n'
        '        obj = BusinessDataServiceHelper.loadSingle(id, "bd_customer");\n'
        '    }\n'
        '    public void doSave() {\n'
        '        SaveServiceHelper.save(obj);\n'
        '    }\n'
        '}\n'
    )
    hits = from_operation_calls(_scan_result({"Foo.java": java}), set(), set(), _const({"Foo.java": java}))
    assert hits == {}   # 变量来源在另一方法，本次改造的诚实边界：不做全程序数据流追踪


# ── 三路合并 discover_candidates ─────────────────────────────────────────────

def test_discover_candidates_merges_three_signals_ext_priority():
    ext_model = MetaModel(key="cqkd_bd_customer_ext", name=None, model_type=None,
                          form_type="basedata", isv="cqkd", inherit_path=["root1"])
    java = (
        'public class Foo {\n'
        '    public void run() {\n'
        '        BusinessDataServiceHelper.loadSingle(id, "bd_customer");\n'
        '        OperationServiceHelper.executeOperate("save", "bd_supplier", pks, option);\n'
        '    }\n'
        '}\n'
    )
    sr = _scan_result({"Foo.java": java})
    out = discover_candidates(models=[ext_model], scan_result=sr, known_keys=set(), isv_prefixes={"cqkd_"})
    keys = [c.key for c in out]
    assert set(keys) == {"bd_customer", "bd_supplier"}
    assert keys[0] == "bd_customer"   # ext 信号优先排序
    top = out[0]
    assert top.ext_source == "cqkd_bd_customer_ext"
    assert top.orm_hits == 1
    assert top.op_hits == 0
    assert sorted(top.sources) == ["ext", "orm"]
    other = out[1]
    assert other.key == "bd_supplier" and other.op_hits == 1
    assert other.evidence and other.evidence[0].startswith("Foo.java:")


def test_discover_candidates_no_inputs_returns_empty():
    assert discover_candidates() == []


# ── known_keys_from_db / isv_prefixes_from_db（KB 侧已知 key / ISV 前缀）────

def _make_full_db(tmp_path, *, forms, entities=(), fields=()):
    db = tmp_path / "full.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE form(key TEXT)")
    conn.execute("CREATE TABLE entity(key TEXT)")
    conn.execute("CREATE TABLE field(key TEXT)")
    conn.executemany("INSERT INTO form VALUES(?)", [(k,) for k in forms])
    conn.executemany("INSERT INTO entity VALUES(?)", [(k,) for k in entities])
    conn.executemany("INSERT INTO field VALUES(?)", [(k,) for k in fields])
    conn.commit()
    conn.close()
    return db


def test_known_keys_from_db_unions_form_entity_field(tmp_path):
    db = _make_full_db(tmp_path, forms=["cqkd_bill"], entities=["cqkd_entry"], fields=["cqkd_htid"])
    assert known_keys_from_db(db) == {"cqkd_bill", "cqkd_entry", "cqkd_htid"}


def test_known_keys_from_db_tolerates_missing_tables(tmp_path):
    db = tmp_path / "min.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE form(key TEXT)")
    conn.execute("INSERT INTO form VALUES('cqkd_bill')")
    conn.commit()
    conn.close()
    assert known_keys_from_db(db) == {"cqkd_bill"}


def test_isv_prefixes_from_db_counts_first_segment(tmp_path):
    db = _make_full_db(tmp_path, forms=["cqkd_bill", "cqkd_entry", "bd_customer", "noprefix"])
    prefixes = isv_prefixes_from_db(db)
    assert prefixes["cqkd_"] == 2
    assert prefixes["bd_"] == 1
    assert prefixes["(none)"] == 1


def test_discover_candidates_db_path_auto_merges_known_keys(tmp_path):
    # 不传 known_keys（模拟 --meta 省略），db_path 给了就该自动并入，字段 key 不再误判候选。
    db = _make_full_db(tmp_path, forms=["cqkd_bill"], fields=["cqkd_htid"])
    java = (
        'public class Foo {\n'
        '    public void run() {\n'
        '        BusinessDataServiceHelper.newDynamicObject("cqkd_htid");\n'
        '        BusinessDataServiceHelper.newDynamicObject("bd_customer");\n'
        '    }\n'
        '}\n'
    )
    sr = _scan_result({"Foo.java": java})
    out = discover_candidates(scan_result=sr, db_path=db)
    assert [c.key for c in out] == ["bd_customer"]
