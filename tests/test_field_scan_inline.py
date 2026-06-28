"""提高字段扫描率（1+2+3）验收测试：
  C1 模型/视图类型形参识别为模型上下文（helper(IDataModel model){ model.setValue(...) } 不再整片漏）；
  C2 内联 `X.getDynamicObjectCollection("k").addNew()` 赋给 DynamicObject 局部 → 新行继承分录坐标；
  C3 内联 `X.getDynamicObjectCollection("k").forEach(o->o.set(..))` lambda 行变量绑定。

走轻量单元路径（`ast_index.parse_tree` → `analyze_method`），复用 analyze 的形参提取助手；含误报/回归护栏。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("tree_sitter_java")

from cosmic_kb.java import analyze as an
from cosmic_kb.java import ast_index as ax
from cosmic_kb.java import call_graph as cgmod
from cosmic_kb.java import field_access as fa
from cosmic_kb.java import project_graph as pgmod
from cosmic_kb.java.constants import ConstantTable


def _analyze(src: str, *, default_entity=None, model_entities=None,
             known=("cqkd_bill",)) -> dict:
    """解析单方法源码 → 跑 analyze_method，返回 {field_key: FieldAccess}。"""
    root = ax.parse_tree(src)
    td = list(ax.iter_type_declarations(root))[0]
    md = list(ax.iter_methods(td))[0]
    env = fa._Env(
        const=ConstantTable(), default_entity=default_entity,
        known_entities=frozenset(known),
        do_vars=ax.dynamicobject_vars(md.node),
        do_params=an._do_params(md.node),
        do_array_params=an._do_array_params(md.node),
        coll_params=an._coll_params(md.node),
        do_coll_vars=frozenset(ax.dynamicobject_collection_vars(md.node)),
        model_params=an._model_params(md.node),
        model_entities=dict(model_entities or {}),
    )
    accs, _ = fa.analyze_method(md.body, env)
    return {a.field_key: a for a in accs}


# ── C1：模型/视图类型形参识别 ──────────────────────────────────────────────

def test_model_params_extracted():
    """analyze._model_params 按类型白名单（_MODEL_TYPES）抽形参名，不靠变量名猜。"""
    src = ("package p; public class C {\n"
           "  public void m(IDataModel model, IFormView view, Object other, String s) {}\n}\n")
    md = list(ax.iter_methods(list(ax.iter_type_declarations(ax.parse_tree(src)))[0]))[0]
    assert an._model_params(md.node) == frozenset({"model", "view"})


def test_c1_model_param_write_scanned():
    """helper(IDataModel model) 里 model.setValue 被扫出；来源走 model_entities（跨类绑定单据）。"""
    src = ("package p; public class C {\n"
           "  public void calc(IDataModel model) {\n"
           "    model.setValue(\"cqkd_x\", 1);\n"
           "    Object v = model.getValue(\"cqkd_y\");\n"
           "  }\n}\n")
    by = _analyze(src, model_entities={"model": "cqkd_bill"})
    assert by["cqkd_x"].access == "write"
    assert by["cqkd_x"].level == "header" and by["cqkd_x"].entity == "cqkd_bill"
    assert by["cqkd_y"].access == "read" and by["cqkd_y"].entity == "cqkd_bill"


def test_c1_model_param_no_binding_falls_to_default():
    """无 model_entities（调用方未走到）时来源回落 default_entity；写入仍被扫出（form_key 再由反查兜底）。"""
    src = ("package p; public class C {\n"
           "  public void calc(IBillModel model) { model.setValue(\"cqkd_x\", 1); }\n}\n")
    by = _analyze(src, default_entity=None)
    assert "cqkd_x" in by and by["cqkd_x"].entity is None     # 来源未定位 → 留给 _backfill_form_key


def test_c1_view_getmodel_chain():
    """IFormView 形参的 view.getModel().setValue —— 来源尝试走 view 的绑定单据。"""
    src = ("package p; public class C {\n"
           "  public void calc(IFormView view) { view.getModel().setValue(\"cqkd_x\", 1); }\n}\n")
    by = _analyze(src, model_entities={"view": "cqkd_bill"})
    assert by["cqkd_x"].entity == "cqkd_bill"


def test_c1_non_model_param_not_scanned():
    """误报护栏：非 _MODEL_TYPES 形参的 .setValue 不入账（不靠变量名猜）。"""
    src = ("package p; public class C {\n"
           "  public void calc(Object model) { model.setValue(\"cqkd_x\", 1); }\n}\n")
    assert _analyze(src) == {}


# ── C2：内联 getDynamicObjectCollection(...).addNew() ──────────────────────

def test_c2_inline_addnew_chain():
    """DynamicObject row = bill.getDynamicObjectCollection("k").addNew(); row.set(...) → 分录新行坐标。"""
    src = ("package p; public class C {\n"
           "  public void m(BeforeOperationArgs e) {\n"
           "    DynamicObject bill = e.getDataEntities()[0];\n"
           "    DynamicObject row = bill.getDynamicObjectCollection(\"cqkd_entry\").addNew();\n"
           "    row.set(\"cqkd_entryf\", 1);\n"
           "  }\n}\n")
    by = _analyze(src, default_entity="cqkd_bill")
    a = by["cqkd_entryf"]
    assert (a.level, a.entry_key, a.entity) == ("entry", "cqkd_entry", "cqkd_bill")


def test_c2_inline_addnew_owner_unresolved_entity_none():
    """owner 来源解不出（new 出来的集合 owner）→ 新行 entity=None（红线#4，不臆造），但层级/分录仍可信。"""
    src = ("package p; public class C {\n"
           "  public void m() {\n"
           "    DynamicObject row = something.getDynamicObjectCollection(\"cqkd_entry\").addNew();\n"
           "    row.set(\"cqkd_entryf\", 1);\n"
           "  }\n}\n")
    by = _analyze(src, default_entity=None)
    a = by.get("cqkd_entryf")
    assert a is not None and a.level == "entry" and a.entry_key == "cqkd_entry" and a.entity is None


def test_c2_variable_addnew_still_works():
    """回归：变量形式 coll.addNew() 既有路径不退化。"""
    src = ("package p; public class C {\n"
           "  public void m(BeforeOperationArgs e) {\n"
           "    DynamicObject bill = e.getDataEntities()[0];\n"
           "    DynamicObjectCollection coll = bill.getDynamicObjectCollection(\"cqkd_entry\");\n"
           "    DynamicObject row = coll.addNew();\n"
           "    row.set(\"cqkd_entryf\", 1);\n"
           "  }\n}\n")
    by = _analyze(src, default_entity="cqkd_bill")
    a = by["cqkd_entryf"]
    assert (a.level, a.entry_key, a.entity) == ("entry", "cqkd_entry", "cqkd_bill")


# ── C3：内联 getDynamicObjectCollection(...).forEach(o -> o.set(..)) lambda ──

def test_c3_inline_foreach_lambda():
    """bill.getDynamicObjectCollection("k").forEach(r -> r.set(..)) → r 绑定到分录元素行坐标。"""
    src = ("package p; public class C {\n"
           "  public void m(BeforeOperationArgs e) {\n"
           "    DynamicObject bill = e.getDataEntities()[0];\n"
           "    bill.getDynamicObjectCollection(\"cqkd_entry\").forEach(r -> r.set(\"cqkd_entryf\", 2));\n"
           "  }\n}\n")
    by = _analyze(src, default_entity="cqkd_bill")
    a = by["cqkd_entryf"]
    assert (a.level, a.entry_key, a.entity) == ("entry", "cqkd_entry", "cqkd_bill")


def test_c3_inline_stream_foreach_lambda():
    """链上带 .stream() 仍能从内联 getDynamicObjectCollection 收敛元素来源。"""
    src = ("package p; public class C {\n"
           "  public void m(BeforeOperationArgs e) {\n"
           "    DynamicObject bill = e.getDataEntities()[0];\n"
           "    bill.getDynamicObjectCollection(\"cqkd_entry\").stream()"
           ".forEach(r -> r.set(\"cqkd_entryf\", 3));\n"
           "  }\n}\n")
    by = _analyze(src, default_entity="cqkd_bill")
    a = by["cqkd_entryf"]
    assert (a.level, a.entry_key, a.entity) == ("entry", "cqkd_entry", "cqkd_bill")


def test_c3_variable_foreach_still_works():
    """回归：变量形式 coll.forEach(o->..) 既有路径不退化。"""
    src = ("package p; public class C {\n"
           "  public void m(BeforeOperationArgs e) {\n"
           "    DynamicObject bill = e.getDataEntities()[0];\n"
           "    DynamicObjectCollection coll = bill.getDynamicObjectCollection(\"cqkd_entry\");\n"
           "    coll.forEach(o -> o.set(\"cqkd_entryf\", 4));\n"
           "  }\n}\n")
    by = _analyze(src, default_entity="cqkd_bill")
    a = by["cqkd_entryf"]
    assert (a.level, a.entry_key, a.entity) == ("entry", "cqkd_entry", "cqkd_bill")


# ── 重载方法不得被同名覆盖丢失（用户 2026-06-27：floorInit(IDataModel) 被 floorInit(DynamicObject) 顶掉）──

# 两个 floorInit 重载：第一个收 DynamicObject，第二个 @NotNull IDataModel model（模型形参，写法同真实样本）。
# 旧逻辑 CallGraph.methods 按名 setdefault → 第二个重载被丢，standalone 永远扫不到 model.getValue("cqkd_ssfq")。
_OVERLOAD_SRC = (
    "package cqkd.am.assets.service.building;\n"
    "public class BuildingService {\n"
    "  public static void floorInit(DynamicObject building) {\n"
    "    building.set(\"cqkd_other\", 1);\n"
    "  }\n"
    "  public static void floorInit(@NotNull IDataModel model, Integer i, Integer j, Boolean isAbove) {\n"
    "    DynamicObject fk_cqkd_ssfq = (DynamicObject) model.getValue(\"cqkd_ssfq\");\n"
    "    model.setValue(\"cqkd_lc\", 2);\n"
    "  }\n}\n"
)


def _project_graph(src: str, relpath: str = "BuildingService.java") -> "pgmod.ProjectGraph":
    """从内存源码建真实 ProjectGraph（build_project_graph 只读 ok_files 的 relpath/text，index 不用）。"""
    scan = SimpleNamespace(ok_files=[SimpleNamespace(relpath=relpath, text=src)])
    return pgmod.build_project_graph(scan, None)


def test_overload_methods_not_dropped_from_call_graph():
    """build_call_graph：同名重载全保留在 method_decls；methods 仍按名取首个（调用边匹配口径不变）。"""
    td = list(ax.iter_type_declarations(ax.parse_tree(_OVERLOAD_SRC)))[0]
    cg = cgmod.build_call_graph(td)
    floor_decls = [m for m in cg.method_decls if m.name == "floorInit"]
    assert len(floor_decls) == 2, "两个 floorInit 重载都必须保留在 method_decls"
    assert {m.param_count for m in floor_decls} == {1, 4}
    assert sum(1 for m in cg.method_decls if m.name == "floorInit") == 2
    assert cg.methods["floorInit"].param_count == 1   # methods 仍是首个重载（向后兼容）


def test_standalone_scans_all_overloads():
    """standalone 补扫覆盖全部重载：IDataModel 重载里的 model.getValue("cqkd_ssfq") 必须进入 field_access。"""
    pg = _project_graph(_OVERLOAD_SRC)
    node = pg.classes["cqkd.am.assets.service.building.BuildingService"]
    result = an.AnalysisResult()
    an._analyze_standalone(
        pg, node, pg.const, frozenset(),
        plugin_base={}, bound_entity={}, covered=set(), result=result,
    )
    keys = {r.field_key for r in result.field_accesses}
    assert "cqkd_ssfq" in keys, "IDataModel model 重载里的 model.getValue 写入不能被同名重载覆盖丢失"
    assert "cqkd_lc" in keys                          # 同一重载里的 model.setValue 也扫到
    assert "cqkd_other" in keys                        # 首个重载照常不丢


def test_standalone_skips_only_bfs_covered_overload():
    """covered 按名记录，只跳过事件 BFS 实际分析过的那个重载（=methods[name]），其余同名重载仍补扫。"""
    pg = _project_graph(_OVERLOAD_SRC)
    fqn = "cqkd.am.assets.service.building.BuildingService"
    node = pg.classes[fqn]
    result = an.AnalysisResult()
    # 模拟事件 BFS 已覆盖 floorInit（首个 DynamicObject 重载）——standalone 不得因此跳过 IDataModel 重载。
    an._analyze_standalone(
        pg, node, pg.const, frozenset(),
        plugin_base={}, bound_entity={}, covered={(fqn, "floorInit")}, result=result,
    )
    keys = {r.field_key for r in result.field_accesses}
    assert "cqkd_ssfq" in keys                         # IDataModel 重载仍补扫
    assert "cqkd_other" not in keys                    # 首个重载已 covered → 跳过（不重复归因）


# ── C4：变量名作用域冲突——for-each/lambda 循环变量覆盖同名 basedata 误绑（红线 #4）──────────
#
# doc_ctx 方法级扁平、无块级作用域：同名局部变量「先到先得」。真实项目 AssetCardCleanQuantityTask
# 里内层 `asset = card.getDynamicObject(基础资料字段)` 先把 asset 钉成 basedata，外层
# `for (DynamicObject asset : 已load的卡片集合)` 想重绑表头却被旧绑定挡住，于是该循环里对资产卡片
# 真实字段的 set(...) 被错盖成 level=basedata → null_reason=basedata-ref「正确None·无需追」而**静默漏报**。
# 修复：for-each/lambda 循环变量是新作用域的新绑定，来源集合有坐标(header/entry/subentry)时覆盖
# 同名 basedata 旧绑定。本用例护栏：写入必须归 header（不得是 basedata）。

def test_c4_foreach_var_overrides_shadowed_basedata_binding():
    """同名 asset：内层 getDynamicObject(basedata) 在前，外层 for-each(已load表头集合) 的写入必须归 header。"""
    src = ("package p;\n"
           "import kd.bos.servicehelper.BusinessDataServiceHelper;\n"
           "public class CleanTask {\n"
           "  public void execute(){\n"
           "    DynamicObject[] cards = BusinessDataServiceHelper.load(\"cqkd_bill\", sel, fil);\n"
           "    for (DynamicObject row : rows) {\n"
           "      DynamicObject asset = row.getDynamicObject(\"cqkd_assetref\");\n"  # 基础资料钻取
           "      long x = asset.getLong(\"cqkd_id\");\n"
           "    }\n"
           "    for (DynamicObject asset : cards) {\n"                               # 同名，真·表头循环
           "      asset.set(\"cqkd_status\", 1);\n"                                  # 真实业务写入
           "    }\n"
           "  }\n}\n")
    by = _analyze(src)
    w = by["cqkd_status"]
    assert w.access == "write"
    assert w.level == "header", "for-each 循环变量被同名 basedata 误绑覆盖：真实写入不得归 basedata"
    assert w.level != "basedata"
    assert w.entity == "cqkd_bill"                     # 来源=已 load 的卡片集合实体，form_key 能定位


def test_c4_lambda_param_overrides_shadowed_basedata_binding():
    """lambda 形参同理：同名 it 先被 getDynamicObject 钉 basedata，forEach 行变量写入仍须归 header。"""
    src = ("package p;\n"
           "import kd.bos.servicehelper.BusinessDataServiceHelper;\n"
           "public class CleanTask {\n"
           "  public void execute(){\n"
           "    DynamicObject[] cards = BusinessDataServiceHelper.load(\"cqkd_bill\", sel, fil);\n"
           "    DynamicObject it = src.getDynamicObject(\"cqkd_assetref\");\n"        # 先钉 basedata
           "    long x = it.getLong(\"cqkd_id\");\n"
           "    Arrays.stream(cards).forEach(it -> it.set(\"cqkd_status\", 1));\n"    # 同名 lambda 形参
           "  }\n}\n")
    by = _analyze(src)
    w = by["cqkd_status"]
    assert w.access == "write"
    assert w.level == "header" and w.level != "basedata"
    assert w.entity == "cqkd_bill"


def test_c4_genuine_basedata_write_still_basedata():
    """护栏（防过度修正）：没有同名集合循环覆盖时，真·基础资料对象内部写入仍判 basedata。"""
    src = ("package p;\n"
           "public class C {\n"
           "  public void m(DynamicObject bill){\n"
           "    DynamicObject org = bill.getDynamicObject(\"cqkd_org\");\n"
           "    org.set(\"cqkd_name\", \"x\");\n"                                     # 写基础资料对象内部字段
           "  }\n}\n")
    by = _analyze(src)
    assert by["cqkd_name"].level == "basedata"         # 未被任何同名循环覆盖 → 仍正确判 basedata


def test_c4_orm_reassignment_overrides_shadowed_basedata_binding():
    """同名 contract：先从基础资料字段取引用，随后 loadSingle 成明确单据对象，写入必须归 header。"""
    src = ("package p;\n"
           "import kd.bos.servicehelper.BusinessDataServiceHelper;\n"
           "public class C {\n"
           "  public void m(DynamicObject row){\n"
           "    DynamicObject contract = row.getDynamicObject(\"cqkd_contract\");\n"
           "    contract = BusinessDataServiceHelper.loadSingle(contract.getPkValue(), \"cqkd_ht\");\n"
           "    contract.set(\"cqkd_zdysje_all\", 1);\n"
           "  }\n}\n")
    by = _analyze(src, known=("cqkd_ht",))
    w = by["cqkd_zdysje_all"]
    assert w.access == "write"
    assert w.level == "header" and w.level != "basedata"
    assert w.entity == "cqkd_ht"


def test_c4_addall_arrays_aslist_preserves_loaded_array_entity():
    """List.addAll(Arrays.asList(load数组)) 后 for-each 行变量应继承数组实体来源。"""
    src = ("package p;\n"
           "import java.util.*;\n"
           "import kd.bos.servicehelper.BusinessDataServiceHelper;\n"
           "public class C {\n"
           "  public void m(){\n"
           "    List<DynamicObject> contractList = new ArrayList<>();\n"
           "    DynamicObject[] validContractArray = BusinessDataServiceHelper.load(\"cqkd_ht\", sel, fs);\n"
           "    contractList.addAll(Arrays.asList(validContractArray));\n"
           "    for (DynamicObject contract : contractList) {\n"
           "      contract.set(\"cqkd_total_number_leas\", 1);\n"
           "    }\n"
           "  }\n}\n")
    by = _analyze(src, known=("cqkd_ht",))
    w = by["cqkd_total_number_leas"]
    assert w.access == "write"
    assert w.level == "header" and w.entity == "cqkd_ht"


def test_c4_ambiguous_entity_constant_chooses_known_entity():
    """ORM 实体参数常量歧义时，若只有一个字面值是已知实体，可安全收敛到该实体。"""
    src = ("package p;\n"
           "import java.util.*;\n"
           "import kd.bos.servicehelper.BusinessDataServiceHelper;\n"
           "public class C {\n"
           "  public void m(){\n"
           "    List<DynamicObject> contractList = new ArrayList<>();\n"
           "    DynamicObject[] validContractArray = BusinessDataServiceHelper.load(ContractCon.ENTITY, sel, fs);\n"
           "    contractList.addAll(Arrays.asList(validContractArray));\n"
           "    for (DynamicObject contract : contractList) {\n"
           "      contract.set(\"cqkd_total_number_leas\", 1);\n"
           "    }\n"
           "  }\n}\n")
    const = ConstantTable()
    const._add("ContractCon", "ENTITY", "entity")
    const._add("ContractCon", "ENTITY", "cqkd_ht")
    root = ax.parse_tree(src)
    md = list(ax.iter_methods(list(ax.iter_type_declarations(root))[0]))[0]
    env = fa._Env(
        const=const, known_entities=frozenset({"cqkd_ht"}),
        do_vars=ax.dynamicobject_vars(md.node),
        do_params=an._do_params(md.node),
        do_array_params=an._do_array_params(md.node),
        coll_params=an._coll_params(md.node),
        do_coll_vars=frozenset(ax.dynamicobject_collection_vars(md.node)),
    )
    accs, _ = fa.analyze_method(md.body, env)
    by = {a.field_key: a for a in accs}
    assert by["cqkd_total_number_leas"].entity == "cqkd_ht"
