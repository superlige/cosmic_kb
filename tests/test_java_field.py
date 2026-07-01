"""阶段 5+6（类内）+7 验收测试 —— 字段级排障引擎。

覆盖用户拍板的关键场景：
  * setValue 2/3/4 参 → 表头/分录/子分录层级，界面相位不落库；
  * 操作插件事务内事件 + 入库类操作（submit）→ 落库；
  * donothing 操作需显式 save 才落库（有 sink=yes、无 sink=no）；
  * DynamicObject 树形赋值（分录/子分录/基础资料）按层级 + entry_key 归位；
  * 常量类引用解析回字段 key；
  * 事件→本类 helper→save 的类内调用链，落库带路径；
  * 同一插件类被两单据绑定 → 字段记录各自归单据（多单据消歧）。

tree-sitter 未装则跳过（字段级分析依赖 [parse] extra）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_java")

from pathlib import Path

from cosmic_kb.bridge import linker, namespace
from cosmic_kb.graph import store
from cosmic_kb.ingest import scanner
from cosmic_kb.metadata.model import (
    MetaEntity, MetaField, MetaModel, MetaOperation, MetaPlugin,
)
from cosmic_kb.report import bill_view, field_trace, project_map

# ── 源码 fixtures ──────────────────────────────────────────────────────────

FORM_PLUGIN = """package cqspb.am;
import kd.bos.form.plugin.AbstractBillPlugIn;
public class AmFormPlugin extends AbstractBillPlugIn {
  public void propertyChanged(PropertyChangedArgs e) {
    getModel().setValue("cqkd_head", 1);
    getModel().setValue("cqkd_entryf", 2, row);
    getModel().setValue("cqkd_subf", 3, row, sub);
  }
}
"""

SUBMIT_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class AmSubmitOp extends AbstractOperationServicePlugIn {
  public void beforeExecuteOperationTransaction(BeforeOperationArgs e) {
    DynamicObject bill = e.getDataEntities()[0];
    bill.set("cqkd_head", 9);
    fillEntries(bill);
  }
  private void fillEntries(DynamicObject bill) {
    DynamicObjectCollection entry = bill.getDynamicObjectCollection("cqkd_entry");
    for (DynamicObject r : entry) {
      r.set("cqkd_entryf", 5);
      DynamicObjectCollection sub = r.getDynamicObjectCollection("cqkd_sub");
      for (DynamicObject s : sub) { s.set("cqkd_subf", 7); }
    }
  }
}
"""

# donothing 操作：本类无 sink → 不落库。
CALC_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class AmCalcOp extends AbstractOperationServicePlugIn {
  public void beforeExecuteOperationTransaction(BeforeOperationArgs e) {
    DynamicObject bill = e.getDataEntities()[0];
    bill.set("cqkd_head", 1);
  }
}
"""

# donothing 操作：调用链里显式 save → 落库。
CALC_SAVE_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class AmCalcSaveOp extends AbstractOperationServicePlugIn {
  public void beforeExecuteOperationTransaction(BeforeOperationArgs e) {
    DynamicObject bill = e.getDataEntities()[0];
    bill.set("cqkd_head", 1);
    persist(bill);
  }
  private void persist(DynamicObject bill) {
    SaveServiceHelper.save(new DynamicObject[]{bill});
  }
}
"""

# 常量类 + 引用它的插件。
CONST_CLASS = """package cqspb.am;
public class AmConst {
  public static final String F_AMT = "cqkd_amount";
}
"""
CONST_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class AmConstOp extends AbstractOperationServicePlugIn {
  public void beforeExecuteOperationTransaction(BeforeOperationArgs e) {
    DynamicObject bill = e.getDataEntities()[0];
    bill.set(AmConst.F_AMT, 100);
  }
}
"""

# 跨类回溯：操作插件把单据数据包丢给 service 改字段（验收反馈的 CollateralService 场景）。
SVC_CLASS = """package cqspb.am;
public class AmStatusService {
  public void updateStatus(DynamicObject bill) {
    bill.set("cqkd_status", "B");
  }
}
"""
SVC_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class AmSvcOp extends AbstractOperationServicePlugIn {
  private AmStatusService svc = new AmStatusService();
  public void beforeExecuteOperationTransaction(BeforeOperationArgs e) {
    DynamicObject bill = e.getDataEntities()[0];
    svc.updateStatus(bill);
  }
}
"""

# 孤立 service：未被任何插件调用，但单独 set 字段（必须全量补全统计到）。
LONE_SVC = """package cqspb.am;
public class AmLoneService {
  public void touch(DynamicObject collateral) {
    collateral.set("cqkd_collateralstatus", 1);
  }
}
"""

# ORM 加载别的实体 + for 循环写字段：来源实体应取 load 的实参，不是插件绑定单据。
ORM_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class AmOrmOp extends AbstractOperationServicePlugIn {
  public void beforeExecuteOperationTransaction(BeforeOperationArgs e) {
    DynamicObject[] needStartBills = BusinessDataServiceHelper.load("cqkd_assetcollateral", "x", new QFilter[]{});
    for (DynamicObject other : needStartBills) {
      other.set("cqkd_othh", "B");
      DynamicObjectCollection sub = other.getDynamicObjectCollection("cqkd_colentry");
      for (DynamicObject row : sub) { row.set("cqkd_otrow", 1); }
    }
  }
}
"""

# Lambda over stream：load 别的实体后 Arrays.stream(...).forEach(o -> o.set(...))（真实漏检形态）。
LAMBDA_SVC = """package cqspb.am;
public class AmLambdaService {
  public static void run() {
    DynamicObject[] needBills = BusinessDataServiceHelper.load("cqkd_assetcollateral", "f", new QFilter[]{});
    Arrays.stream(needBills).forEach(o -> o.set("cqkd_lamf", "1"));
  }
}
"""

# 未绑定的调度计划插件（AbstractTask）静态调 service：service load 别的实体 + 入库。
TASK_PLUGIN = """package cqspb.am;
import kd.bos.schedule.executor.AbstractTask;
public class AmTask extends AbstractTask {
  public void execute(RequestContext c, java.util.Map m) {
    AmTaskService.run();
  }
}
"""
TASK_SVC = """package cqspb.am;
public class AmTaskService {
  public static void run() {
    DynamicObject[] bills = BusinessDataServiceHelper.load("cqkd_assetcollateral", "f", new QFilter[]{});
    Arrays.stream(bills).forEach(o -> o.set("cqkd_taskf", "1"));
    SaveServiceHelper.save(bills);
  }
}
"""

# 未绑定的 WebApi 插件：loadSingle(实体在第2参) + set + save。
WEBAPI_PLUGIN = """package cqspb.am;
import kd.bos.webapi.plugin.AbstractBillWebApiPlugin;
public class AmWebApi extends AbstractBillWebApiPlugin {
  public Object doCustomService(java.util.Map p) {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(123L, "cqkd_assetcollateral");
    bill.set("cqkd_apif", "x");
    SaveServiceHelper.save(bill);
    return null;
  }
}
"""

# 表单插件 closedCallBack 把 getModel()+常量分录 key 传给跨类 service，service 里
# `model.getDataEntity().getDynamicObjectCollection(keyParam)` 循环写分录字段
# —— 真实项目 AdjustHtContract→AdjustContractService.calTolAdjust 的形态（用户 2026-06-17 报障：
# 精确坐标查不到，写入被错判为 header/None/未定位）。
KEYPARAM_CONST = """package cqspb.am;
public class AmConsts {
  public static final String ENTRY_ZQ = "cqkd_entry";
  public static final String F_TZ = "cqkd_entryf";
}
"""
KEYPARAM_SVC = """package cqspb.am;
public class AmKeyParamService {
  public static void calc(IFormView view, IDataModel model, String periodBillKey) {
    DynamicObject dataEntity = model.getDataEntity(true);
    DynamicObjectCollection periodBills = dataEntity.getDynamicObjectCollection(periodBillKey)
        .stream().collect(Collectors.toCollection(DynamicObjectCollection::new));
    for (DynamicObject periodBill : periodBills) {
      periodBill.set(AmConsts.F_TZ, 1);
    }
  }
}
"""
KEYPARAM_PLUGIN = """package cqspb.am;
import kd.bos.form.plugin.AbstractBillPlugIn;
public class AmKeyParamPlugin extends AbstractBillPlugIn {
  public void closedCallBack(ClosedCallBackEvent e) {
    AmKeyParamService.calc(getView(), getModel(), AmConsts.ENTRY_ZQ);
  }
}
"""

# 已绑定表单插件，但某 helper 不被任何事件调用 → 落入第②轮全量补全。修复前 default_entity=None
# 致其 getModel() 写入来源判不出（form_key=None）；修复后应沿用本类**唯一绑定单据**作来源。
HELPER_PLUGIN = """package cqspb.am;
import kd.bos.form.plugin.AbstractBillPlugIn;
public class AmHelperPlugin extends AbstractBillPlugIn {
  public void propertyChanged(PropertyChangedArgs e) {
    getModel().setValue("cqkd_head", 1);
  }
  public void recalcUnreached() {
    getModel().setValue("cqkd_status", 2);
  }
}
"""

# 泛型集合 List<DynamicObject> 来源传播：事件里用 .add(loadSingle(...,"实体")) 累积一个 List，
# 传给 helper（List<DynamicObject> 形参），helper for-each 读字段——读的来源应是 add 进来的实体。
LISTPARAM_PLUGIN = """package cqspb.am;
import kd.bos.form.plugin.AbstractBillPlugIn;
public class AmListParamPlugin extends AbstractBillPlugIn {
  public void afterCreateNewData(EventObject e) {
    java.util.List<DynamicObject> list = new java.util.ArrayList<>();
    DynamicObject originbill = BusinessDataServiceHelper.loadSingle(123L, "cqkd_assetcollateral");
    list.add(originbill);
    this.fillFromList(list);
  }
  public void fillFromList(java.util.List<DynamicObject> list) {
    for (DynamicObject dynamicObject : list) {
      String v = dynamicObject.getString("cqkd_othh");
    }
  }
}
"""

# 转换插件 afterConvert：目标单数据包写字段，应归到目标单。
CONVERT_PLUGIN = """package cqspb.am;
import kd.bos.entity.plugin.AbstractConvertPlugIn;
public class AmConvertPlugin extends AbstractConvertPlugIn {
  public void afterConvert(AfterConvertEventArgs e) {
    ExtendedDataEntity[] arr = e.getTargetExtDataEntitySet().FindByEntityKey("cqkd_target");
    for (ExtendedDataEntity ede : arr) {
      DynamicObject contract = ede.getDataEntity();
      contract.set("cqkd_tfield", "x");
    }
  }
}
"""


# helper 在模型上造好分录行后 `return rows.get(r)`，调用方拿到该行再取子分录 `addNew()` 写字段。
# 复刻真实项目 GenBillByCycleBill：returnvalue 携带数据包坐标 + 集合 addNew 取新行（两处此前判不出
# 来源单据，整片落入「未定位单据」，查不到归属单据 cqkd_ht）。
GENROW_PLUGIN = """package cqspb.am;
import kd.bos.form.plugin.AbstractBillPlugIn;
public class AmGenRowPlugin extends AbstractBillPlugIn {
  public void afterDoOperation(Object e) {
    DynamicObject row = this.makeEntryRow();
    this.fillSub(row);
  }
  private DynamicObject makeEntryRow() {
    int r = this.getModel().createNewEntryRow("cqkd_entry");
    this.getModel().setValue("cqkd_entryf", 1, r);
    DynamicObjectCollection rows = this.getModel().getDataEntity(true).getDynamicObjectCollection("cqkd_entry");
    return rows.get(r);
  }
  private void fillSub(DynamicObject row) {
    DynamicObjectCollection subs = row.getDynamicObjectCollection("cqkd_sub");
    DynamicObject sub = subs.addNew();
    sub.set("cqkd_subf", "0");
  }
}
"""


# DynamicObject[] 数组入参 + 跨方法传播 + stream(lambda 内含 getDynamicObjectCollection) + addNew。
# 复刻 ContractUpdateAssetOp.genSubBill：数组入参的元素行 + 流式筛选后再取子分录新行写字段，
# 此前数组入参被当单个表头包、stream 又被 lambda 内的取分录正则误判，整片来源单据判不出。
ARRAY_PARAM_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class AmArrayOp extends AbstractOperationServicePlugIn {
  public void beginOperationTransaction(BeginOperationTransactionArgs e) {
    DynamicObject[] entities = e.getDataEntities();
    genSub(entities);
  }
  public static void genSub(DynamicObject[] entities) {
    for (DynamicObject entity : entities) {
      DynamicObjectCollection rows = entity.getDynamicObjectCollection("cqkd_entry");
      List<DynamicObject> picked = rows.stream()
          .filter(o -> o.getDynamicObjectCollection("cqkd_sub").size() == 0)
          .collect(Collectors.toList());
      for (DynamicObject r : picked) {
        DynamicObjectCollection subs = r.getDynamicObjectCollection("cqkd_sub");
        DynamicObject sub = subs.addNew();
        sub.set("cqkd_subf", "0");
      }
    }
  }
}
"""


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _fields(*keys):
    return [MetaField("TextField", k, k, "f" + k, "id" + k, "1", "entity", "header",
                      "cqkd_bill") for k in keys]


# 独立校验器（AbstractValidator）：op 插件 onAddValidators 里 new 出来挂载、不进元数据绑定。
# 此前一律落成 orphan_role='unknown'、plugin_type='service'，bill/plugin 看不到——
# 现应识别为 kind='validator'，validate() 作入口、读单据字段（提交/审核报错真凶）。
VALIDATOR_PLUGIN = """package cqspb.am;
import kd.bos.entity.validate.AbstractValidator;
import kd.bos.entity.ExtendedDataEntity;
public class AmValidator extends AbstractValidator {
  public void validate() {
    ExtendedDataEntity[] ds = getDataEntities();
    for (ExtendedDataEntity d : ds) {
      Object v = d.getDataEntity().get("cqkd_valf");
      if (v == null) { addErrorMessage(d, "请填写"); }
    }
  }
}
"""


def _build(tmp_path: Path):
    src = tmp_path / "src"
    _w(src / "AmFormPlugin.java", FORM_PLUGIN)
    _w(src / "AmSubmitOp.java", SUBMIT_OP)
    _w(src / "AmCalcOp.java", CALC_OP)
    _w(src / "AmCalcSaveOp.java", CALC_SAVE_OP)
    _w(src / "AmConst.java", CONST_CLASS)
    _w(src / "AmConstOp.java", CONST_OP)
    _w(src / "AmStatusService.java", SVC_CLASS)
    _w(src / "AmSvcOp.java", SVC_OP)
    _w(src / "AmLoneService.java", LONE_SVC)
    _w(src / "AmOrmOp.java", ORM_OP)
    _w(src / "AmConvertPlugin.java", CONVERT_PLUGIN)
    _w(src / "AmLambdaService.java", LAMBDA_SVC)
    _w(src / "AmTask.java", TASK_PLUGIN)
    _w(src / "AmTaskService.java", TASK_SVC)
    _w(src / "AmWebApi.java", WEBAPI_PLUGIN)
    _w(src / "AmConsts.java", KEYPARAM_CONST)
    _w(src / "AmKeyParamService.java", KEYPARAM_SVC)
    _w(src / "AmKeyParamPlugin.java", KEYPARAM_PLUGIN)
    _w(src / "AmGenRowPlugin.java", GENROW_PLUGIN)
    _w(src / "AmArrayOp.java", ARRAY_PARAM_OP)
    _w(src / "AmHelperPlugin.java", HELPER_PLUGIN)
    _w(src / "AmListParamPlugin.java", LISTPARAM_PLUGIN)
    _w(src / "AmValidator.java", VALIDATOR_PLUGIN)
    scan = scanner.scan(src)

    ents = [MetaEntity("BillEntity", "cqkd_bill", "单据头", "1", "header", None, "t"),
            MetaEntity("EntryEntity", "cqkd_entry", "分录", "2", "entry", "1", "te"),
            MetaEntity("SubEntryEntity", "cqkd_sub", "子分录", "3", "subentry", "2", "ts")]
    flds = _fields("cqkd_head", "cqkd_entryf", "cqkd_subf", "cqkd_amount", "cqkd_status",
                   "cqkd_valf")
    ops = [MetaOperation("submit", "提交", "submit", None, None, resolved_from="self"),
           MetaOperation("calc", "计算", "donothing", None, None, resolved_from="self"),
           MetaOperation("calc2", "计算2", "donothing", None, None, resolved_from="self"),
           MetaOperation("cst", "常量", "submit", None, None, resolved_from="self")]
    plugins = [
        MetaPlugin("cqspb.am.AmFormPlugin", "form", "project"),
        MetaPlugin("cqspb.am.AmSubmitOp", "op", "project", operation_key="submit"),
        MetaPlugin("cqspb.am.AmCalcOp", "op", "project", operation_key="calc"),
        MetaPlugin("cqspb.am.AmCalcSaveOp", "op", "project", operation_key="calc2"),
        MetaPlugin("cqspb.am.AmConstOp", "op", "project", operation_key="cst"),
        MetaPlugin("cqspb.am.AmSvcOp", "op", "project", operation_key="submit"),
        MetaPlugin("cqspb.am.AmOrmOp", "op", "project", operation_key="submit"),
        MetaPlugin("cqspb.am.AmKeyParamPlugin", "form", "project"),
        MetaPlugin("cqspb.am.AmGenRowPlugin", "form", "project"),
        MetaPlugin("cqspb.am.AmArrayOp", "op", "project", operation_key="submit"),
        MetaPlugin("cqspb.am.AmHelperPlugin", "form", "project"),
        MetaPlugin("cqspb.am.AmListParamPlugin", "form", "project"),
    ]
    m1 = MetaModel(key="cqkd_bill", name="资产单", model_type="BillFormModel",
                   form_type="bill", isv="cqkd", app_key="cqkd_am",
                   entities=ents, fields=flds, plugins=plugins, operations=ops)
    # 第二张单据复用同一个操作插件类（多单据消歧）。
    m2 = MetaModel(key="cqkd_bill2", name="资产单2", model_type="BillFormModel",
                   form_type="bill", isv="cqkd", app_key="cqkd_am",
                   entities=[MetaEntity("BillEntity", "cqkd_bill2", "头", "9", "header", None, "t2")],
                   fields=_fields("cqkd_head"),
                   plugins=[MetaPlugin("cqspb.am.AmSubmitOp", "op", "project", operation_key="submit")],
                   operations=[MetaOperation("submit", "提交", "submit", None, None, "self")])
    # 被 ORM load 的别的实体（含分录），只为提供已知实体 + 字段定义坐标。
    m3 = MetaModel(key="cqkd_assetcollateral", name="抵押单", model_type="BillFormModel",
                   form_type="bill", isv="cqkd", app_key="cqkd_am",
                   entities=[MetaEntity("BillEntity", "cqkd_assetcollateral", "头", "1", "header", None, "t3"),
                             MetaEntity("EntryEntity", "cqkd_colentry", "抵押分录", "2", "entry", "1", "te3")],
                   fields=_fields("cqkd_othh", "cqkd_otrow"))
    # 转换规则（目标单 cqkd_target ← 源单 cqkd_src）+ 转换插件。
    from cosmic_kb.metadata.model import ConvertInfo
    m4 = MetaModel(key="cr_1", name="下推规则", model_type="ConvertRuleModel",
                   form_type="convert", isv="cqkd", app_key="cqkd_am",
                   plugins=[MetaPlugin("cqspb.am.AmConvertPlugin", "convert", "project")],
                   convert=ConvertInfo(source_entity="cqkd_src", target_entity="cqkd_target"))
    models = [m1, m2, m3, m4]
    index = namespace.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    mm = project_map.module_map(scan, models, bridge, index=index)
    db = tmp_path / "kb.db"
    counts = store.build_kb(scan, models, bridge, mm, db, index=index)
    return db, counts


def _facc(conn, **where):
    cols = ("field_key", "level", "entry_key", "plugin_fqn", "plugin_type",
            "event_method", "event_phase", "access", "persists", "via", "form_key")
    sql = "SELECT " + ",".join(cols) + " FROM field_access"
    if where:
        sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
    return [dict(zip(cols, r)) for r in conn.execute(sql, tuple(where.values())).fetchall()]


def test_setvalue_levels(tmp_path: Path):
    """getModel().setValue 实参个数 → 表头/分录/子分录层级；界面相位不落库。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rows = {r["field_key"]: r for r in _facc(conn, plugin_fqn="cqspb.am.AmFormPlugin")}
        assert rows["cqkd_head"]["level"] == "header"
        assert rows["cqkd_entryf"]["level"] == "entry"
        assert rows["cqkd_subf"]["level"] == "subentry"
        # 界面 propertyChanged：内存相位，不落库。
        assert all(r["persists"] == "no" for r in rows.values())
        assert all(r["event_method"] == "propertyChanged" for r in rows.values())
    finally:
        conn.close()


def test_op_transaction_persists(tmp_path: Path):
    """操作插件事务内事件 + submit（入库类）→ 落库。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rows = _facc(conn, plugin_fqn="cqspb.am.AmSubmitOp", form_key="cqkd_bill")
        head = next(r for r in rows if r["field_key"] == "cqkd_head")
        assert head["persists"] == "yes"
        assert head["event_phase"] == "transaction"
        assert head["via"] == "do.set"
    finally:
        conn.close()


def test_dynamicobject_tree_levels(tmp_path: Path):
    """DynamicObject 树形赋值：分录/子分录按层级 + entry_key 归位（经类内调用链）。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rows = {r["field_key"]: r for r in _facc(conn, plugin_fqn="cqspb.am.AmSubmitOp",
                                                 form_key="cqkd_bill")}
        assert rows["cqkd_entryf"]["level"] == "entry"
        assert rows["cqkd_entryf"]["entry_key"] == "cqkd_entry"
        assert rows["cqkd_subf"]["level"] == "subentry"
        assert rows["cqkd_subf"]["entry_key"] == "cqkd_sub"
        # 子分录字段在 helper 里写的，仍判落库（submit 事务）。
        assert rows["cqkd_subf"]["persists"] == "yes"
    finally:
        conn.close()


def test_donothing_needs_explicit_save(tmp_path: Path):
    """donothing 操作：本类无 sink → 不落库；调用链里有 save → 落库。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        no_sink = _facc(conn, plugin_fqn="cqspb.am.AmCalcOp")
        assert no_sink and all(r["persists"] == "no" for r in no_sink)
        with_save = _facc(conn, plugin_fqn="cqspb.am.AmCalcSaveOp")
        assert with_save and all(r["persists"] == "yes" for r in with_save)
    finally:
        conn.close()


def test_constant_resolution(tmp_path: Path):
    """常量类引用 AmConst.F_AMT → 解析回字段 key cqkd_amount。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rows = _facc(conn, plugin_fqn="cqspb.am.AmConstOp")
        assert any(r["field_key"] == "cqkd_amount" for r in rows)
    finally:
        conn.close()


def test_call_chain_path(tmp_path: Path):
    """事件→本类 helper 的调用链路径记入 field_access.path。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        import json
        r = conn.execute(
            "SELECT path FROM field_access WHERE plugin_fqn='cqspb.am.AmSubmitOp' "
            "AND field_key='cqkd_entryf' AND form_key='cqkd_bill'").fetchone()
        path = json.loads(r[0])
        assert path[0] == "beforeExecuteOperationTransaction"
        assert "fillEntries" in path
    finally:
        conn.close()


def test_multi_bill_disambiguation(tmp_path: Path):
    """同一插件类被两单据绑定 → 字段写入各自归单据。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        forms = {r["form_key"] for r in _facc(conn, plugin_fqn="cqspb.am.AmSubmitOp",
                                              field_key="cqkd_head")}
        assert forms == {"cqkd_bill", "cqkd_bill2"}
    finally:
        conn.close()


def test_field_trace_report(tmp_path: Path):
    """旗舰 field_trace：写入排序（落库优先）+ 概况统计。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        ft = field_trace.field_trace(conn, "cqkd_head", form_key="cqkd_bill")
        # 写该字段：op(落库) + form(内存)。落库的排前面（顶层扁平 writers 已删，改从分组取）。
        grp = next(g for g in ft["groups"] if g["writers"])
        assert grp["writers"][0]["persists"] == "yes"
        assert ft["summary"]["persisting_writers"] >= 1
        text = field_trace.render_field_trace(ft)
        assert "cqkd_head" in text and "落库" in text
        # 粗精度块形状（只留「仅粗扫见」，去掉 total/both）。
        cs = ft["coarse"]
        assert {"coarse_only", "idiom", "const_excluded", "high_rows", "locations"} <= set(cs)
        assert isinstance(cs["locations"], list)
    finally:
        conn.close()


def test_field_trace_coarse_only_filtering(tmp_path: Path):
    """字段查询的粗扫块只留「仅粗扫见」：高精度也记被剔除、常量类命中被剔除。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        # 取一条该字段的高精度命中作为「高精度也记」锚点（应被剔除）。
        row = conn.execute(
            "SELECT source_relpath,line FROM field_access WHERE field_key='cqkd_head' "
            "AND source_relpath IS NOT NULL LIMIT 1").fetchone()
        assert row is not None
        conn.execute("DELETE FROM coarse_field_hit WHERE field_key='cqkd_head'")
        conn.execute("INSERT INTO coarse_field_hit VALUES(?,?,?,?)",
                     ("cqkd_head", row["source_relpath"], row["line"], "rw-idiom"))   # 高精度也记 → 剔除
        conn.execute("INSERT INTO coarse_field_hit VALUES(?,?,?,?)",
                     ("cqkd_head", "wild/Nowhere.java", 999, "literal"))              # 仅粗扫见 → 保留
        conn.commit()
        cs = field_trace.field_trace(conn, "cqkd_head")["coarse"]
        # 高精度也记被剔除，只剩 wild/Nowhere.java 这一条仅粗扫见（且非强信号、非常量类）。
        assert cs["coarse_only"] == 1 and cs["idiom"] == 0 and cs["const_excluded"] == 0
        locs = {(l["relpath"], l["line"]) for l in cs["locations"]}
        assert ("wild/Nowhere.java", 999) in locs
        assert (row["source_relpath"], row["line"]) not in locs

        # 把 wild/Nowhere.java 标成常量类 → 该命中应被剔除（coarse_only 归零）。
        conn.execute(
            "INSERT INTO source_class VALUES(?,?,?,?,?,?,?,?)",
            ("wild.Nowhere", "Nowhere", "wild", "wild/Nowhere.java", None, 1, "constant", None))
        conn.commit()
        cs2 = field_trace.field_trace(conn, "cqkd_head")["coarse"]
        assert cs2["coarse_only"] == 0 and cs2["const_excluded"] == 1
    finally:
        conn.close()


def test_bill_view_report(tmp_path: Path):
    """单据视图：操作集 has_plugin、字段触达 + 实体分组。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        bv = bill_view.bill_view(conn, "cqkd_bill")
        submit = next(o for o in bv["operations"] if o["key"] == "submit")
        assert submit["has_plugin"] == 1
        assert "cqkd_head" in bv["field_touch"]
        # 字段触达按实体分组：表头实体下含 cqkd_head。
        header = next(g for g in bv["entity_touch"] if g["entity_key"] is None)
        assert any(f["field_key"] == "cqkd_head" for f in header["fields"])
    finally:
        conn.close()


def test_cross_class_attribution(tmp_path: Path):
    """跨类回溯：插件把数据包丢给 service 改字段 → 归因到插件+事件，并随 submit 落库。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        r = conn.execute(
            "SELECT access_class,form_key,persists,event_method FROM field_access "
            "WHERE plugin_fqn='cqspb.am.AmSvcOp' AND field_key='cqkd_status'").fetchone()
        assert r is not None, "service 里的写入应归因到调用它的插件"
        assert r["access_class"] == "cqspb.am.AmStatusService"   # 物理所在类 ≠ 入口插件
        assert r["form_key"] == "cqkd_bill"
        assert r["persists"] == "yes"                            # submit 事务内 → 落库
        assert r["event_method"] == "beforeExecuteOperationTransaction"
    finally:
        conn.close()


def test_standalone_service_capture(tmp_path: Path):
    """全量补全：未被任何插件调用的 service 单独 set 字段也要统计到（落库存疑）。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        r = conn.execute(
            "SELECT plugin_fqn,plugin_type,form_key,persists,access_class FROM field_access "
            "WHERE field_key='cqkd_collateralstatus'").fetchone()
        assert r is not None, "孤立 service 的字段写入必须被全量补全统计"
        assert r["plugin_fqn"] == "cqspb.am.AmLoneService"
        assert r["plugin_type"] == "service"
        assert r["form_key"] is None                            # 未定位到具体单据
        assert r["persists"] == "unknown"                       # 落库取决于调用方
    finally:
        conn.close()


def test_orm_load_source_entity(tmp_path: Path):
    """ORM load 别的实体 + for 循环：写入归到 load 的实体（非插件绑定单据），层级正确。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        hh = conn.execute(
            "SELECT form_key,level FROM field_access WHERE plugin_fqn='cqspb.am.AmOrmOp' "
            "AND field_key='cqkd_othh'").fetchone()
        assert hh is not None and hh["form_key"] == "cqkd_assetcollateral"  # 来源=load 实参，非 cqkd_bill
        assert hh["level"] == "header"
        rw = conn.execute(
            "SELECT form_key,level,entry_key FROM field_access WHERE plugin_fqn='cqspb.am.AmOrmOp' "
            "AND field_key='cqkd_otrow'").fetchone()
        assert rw is not None and rw["form_key"] == "cqkd_assetcollateral"
        assert rw["level"] == "entry" and rw["entry_key"] == "cqkd_colentry"
    finally:
        conn.close()


def test_lambda_stream_capture(tmp_path: Path):
    """Arrays.stream(load结果).forEach(o -> o.set(...)) 的 lambda 行变量写入要抓到，
    且来源实体取 load 的实参（真实项目 CollateralService 漏检形态的回归）。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        r = conn.execute(
            "SELECT form_key,access_class FROM field_access WHERE field_key='cqkd_lamf'").fetchone()
        assert r is not None, "lambda 行变量 o.set(...) 的写入必须被抓到"
        assert r["form_key"] == "cqkd_assetcollateral"
        assert r["access_class"] == "cqspb.am.AmLambdaService"
    finally:
        conn.close()


def test_parse_locator():
    """层级显式点号查询：按段数判定坐标。"""
    p = field_trace.parse_locator
    assert p("cqkd_amount") == ("cqkd_amount", None, None, None)
    assert p("cqkd_bill.cqkd_head") == ("cqkd_head", "cqkd_bill", None, "header")
    assert p("cqkd_bill.cqkd_entry.cqkd_entryf") == ("cqkd_entryf", "cqkd_bill", "cqkd_entry", "entry")
    assert p("f.e.s.fld") == ("fld", "f", "s", "subentry")


def test_unbound_task_entry(tmp_path: Path):
    """未绑定的 AbstractTask 作跨类入口：execute→service.load(别的实体)→lambda 写+save 入库。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        r = conn.execute(
            "SELECT plugin_fqn,plugin_type,access_class,form_key,persists FROM field_access "
            "WHERE field_key='cqkd_taskf'").fetchone()
        assert r is not None, "调度计划插件链路里的字段写入必须被抓到"
        assert r["plugin_fqn"] == "cqspb.am.AmTask"           # 入口=任务
        assert r["plugin_type"] == "task"
        assert r["access_class"] == "cqspb.am.AmTaskService"   # 物理写在 service
        assert r["form_key"] == "cqkd_assetcollateral"        # 来源=load 实参
        assert r["persists"] == "yes"                         # 链路有 save sink
    finally:
        conn.close()


def test_unbound_webapi_entry(tmp_path: Path):
    """未绑定的 WebApi 插件作入口：loadSingle(实体在第2参)+set+save。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        r = conn.execute(
            "SELECT plugin_type,form_key,persists FROM field_access WHERE field_key='cqkd_apif'").fetchone()
        assert r is not None
        assert r["plugin_type"] == "webapi"
        assert r["form_key"] == "cqkd_assetcollateral"        # loadSingle 第 2 参实体
        assert r["persists"] == "yes"
    finally:
        conn.close()


def test_validator_entry(tmp_path: Path):
    """独立校验器（AbstractValidator）识别为 kind='validator'：validate() 作入口、读字段被抓到。

    回归 docs/动作车道词表.md 附录 B 的盲区：此前校验器落 orphan_role='unknown'、
    plugin_type='service'、plugin_method 全 helper、bill/plugin 看不见。修复后：
      * field_access.plugin_type='validator'、event_method/phase='validate'；
      * 读到的字段 key 经反查回填来源单据（校验器读单据字段、不写）；
      * source_class 里该类 orphan_role='plugin'、plugin_base='AbstractValidator'。
    """
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        r = conn.execute(
            "SELECT plugin_fqn,plugin_type,event_method,event_phase,access,form_key "
            "FROM field_access WHERE field_key='cqkd_valf'").fetchone()
        assert r is not None, "校验器 validate() 里读的字段必须被抓到"
        assert r["plugin_fqn"] == "cqspb.am.AmValidator"
        assert r["plugin_type"] == "validator"               # 不再是 service
        assert r["event_method"] == "validate"
        assert r["event_phase"] == "validate"
        assert r["access"] == "read"                         # 校验器读字段校验、不写
        assert r["form_key"] == "cqkd_bill"                  # 字段 key 唯一反查回填来源单据
        # 孤儿角色：识别为 plugin（命中苍穹基类），不再是 unknown。
        sc = conn.execute(
            "SELECT orphan_role,plugin_base FROM source_class "
            "WHERE fqn='cqspb.am.AmValidator'").fetchone()
        assert sc is not None
        assert sc[0] == "plugin"
        assert sc[1] == "AbstractValidator"
    finally:
        conn.close()


def test_precise_possible_bucket(tmp_path: Path):
    """精确层级查询：层级不匹配的写入进「可能命中」桶而非被丢（不遗漏）。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        # cqkd_head 实为表头字段；按分录精确查 → 精确桶空、可能命中桶含表头写入。
        ft = field_trace.field_trace(conn, "cqkd_head", form_key="cqkd_bill",
                                     entry_key="cqkd_entry", level="entry")
        assert not ft["groups"]                  # 无分录级精确命中
        assert ft["possible"]                    # 表头写入落入可能命中
        assert any(r["level"] == "header" for r in ft["possible"])
    finally:
        conn.close()


def test_orm_variants_source():
    """ORM loadSingleFromCache / queryOne 的来源实体识别（实体在不同实参位）。"""
    from cosmic_kb.java import ast_index as ax, field_access as fa
    from cosmic_kb.java.constants import ConstantTable
    src = (
        "package p; public class C {\n"
        "  public void m() {\n"
        "    DynamicObject a = BusinessDataServiceHelper.loadSingleFromCache(1L, \"cqkd_x\");\n"
        "    a.set(\"f1\", 1);\n"
        "    DynamicObject b = QueryServiceHelper.queryOne(\"cqkd_y\", \"f\", null);\n"
        "    b.set(\"f2\", 2);\n"
        "  }\n}\n"
    )
    root = ax.parse_tree(src)
    td = list(ax.iter_type_declarations(root))[0]
    md = list(ax.iter_methods(td))[0]
    env = fa._Env(const=ConstantTable(), known_entities=frozenset({"cqkd_x", "cqkd_y"}),
                  do_vars=ax.dynamicobject_vars(md.node), do_params=frozenset())
    accs, _ = fa.analyze_method(md.body, env)
    by = {a.field_key: a for a in accs}
    assert by["f1"].entity == "cqkd_x"
    assert by["f2"].entity == "cqkd_y"


def test_cross_class_full_coord_propagation(tmp_path: Path):
    """跨类全坐标传播：入口把分录行传进 service，service 内写入保留分录层级 + 来源。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        # AmSubmitOp.fillEntries 内对分录行 r.set("cqkd_entryf")（同类 helper，全坐标传播）。
        r = conn.execute(
            "SELECT level,entry_key,form_key FROM field_access WHERE plugin_fqn='cqspb.am.AmSubmitOp' "
            "AND field_key='cqkd_entryf' AND form_key='cqkd_bill'").fetchone()
        assert r is not None
        assert r["level"] == "entry" and r["entry_key"] == "cqkd_entry"
    finally:
        conn.close()


def test_convert_target_attribution(tmp_path: Path):
    """转换插件 afterConvert：目标单数据包写字段 → 归到目标单 cqkd_target。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        r = conn.execute(
            "SELECT form_key,access_class FROM field_access WHERE plugin_fqn='cqspb.am.AmConvertPlugin' "
            "AND field_key='cqkd_tfield'").fetchone()
        assert r is not None, "转换插件 afterConvert 的目标单写入应被抓到"
        assert r["form_key"] == "cqkd_target"
    finally:
        conn.close()


def test_keyparam_cross_class_entry_attribution(tmp_path: Path):
    """跨类传播 getModel()+常量分录 key：service 里 model.getDataEntity().getDynamicObjectCollection(形参)
    循环写分录字段 → 归到插件绑定单据的正确分录坐标（AdjustHtContract→AdjustContractService 报障回归）。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        r = conn.execute(
            "SELECT form_key,level,entry_key,access_class,access FROM field_access "
            "WHERE plugin_fqn='cqspb.am.AmKeyParamPlugin' AND field_key='cqkd_entryf'").fetchone()
        assert r is not None, "service 里经常量分录 key 的分录写入必须被抓到"
        assert r["form_key"] == "cqkd_bill"                      # 来源=插件绑定单据（model.getDataEntity）
        assert r["level"] == "entry"                             # getDynamicObjectCollection → 分录
        assert r["entry_key"] == "cqkd_entry"                    # 常量形参解析回分录 key
        assert r["access_class"] == "cqspb.am.AmKeyParamService"  # 物理写在 service
        assert r["access"] == "write"
        # 精确坐标查询应命中（而非落入「未定位单据」）。
        ft = field_trace.field_trace(conn, "cqkd_entryf", form_key="cqkd_bill",
                                     entry_key="cqkd_entry", level="entry")
        assert any(any(w["plugin_fqn"] == "cqspb.am.AmKeyParamPlugin" for w in g["writers"])
                   for g in ft["groups"]), "精确坐标 cqkd_bill.cqkd_entry.cqkd_entryf 应命中该写入"
    finally:
        conn.close()


def test_return_value_and_addnew_source(tmp_path: Path):
    """helper 返回模型分录行 + 集合 addNew 取新行 → 子分录写入归到插件绑定单据（而非未定位）。

    复刻 GenBillByCycleBill：`bill = this.makeEntryRow()`（返回 rows.get(r)）→ `subs.addNew()` →
    `sub.set("cqkd_subf",…)`。修复前 subBill 来源判不出 form_key=None（落「未定位单据」、按单据
    查不到）；修复后应归到 cqkd_bill / subentry / cqkd_sub，与元数据定义坐标一致。
    """
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rows = _facc(conn, plugin_fqn="cqspb.am.AmGenRowPlugin", field_key="cqkd_subf",
                     access="write")
        assert rows, "子分录写入应被归因到 AmGenRowPlugin"
        r = rows[0]
        assert r["form_key"] == "cqkd_bill", "返回值携带的来源单据应传播到 addNew 行（不再是 None）"
        assert r["level"] == "subentry"
        assert r["entry_key"] == "cqkd_sub"
        # 按单据钻取也能看到该写入（用户排障入口：从元数据单据找到改字段的插件）。
        ft = field_trace.field_trace(conn, "cqkd_subf", form_key="cqkd_bill")
        # 顶层扁平 writers 已删——从分组里找该插件写入。
        assert any(w["plugin_fqn"] == "cqspb.am.AmGenRowPlugin"
                   for g in ft["groups"] for w in g["writers"]), \
            "查 cqkd_bill 应能看到 AmGenRowPlugin 写入 cqkd_subf"
    finally:
        conn.close()


def test_array_param_source_propagation(tmp_path: Path):
    """DynamicObject[] 数组入参 + stream(lambda 内取分录) + addNew → 子分录写入归到绑定单据。

    复刻 ContractUpdateAssetOp.genSubBill：数组入参元素行经流式筛选后取子分录新行写字段。
    修复前数组入参被当单个表头包、stream 又被 lambda 内 getDynamicObjectCollection 误判 → 来源
    单据整片判不出（form_key=None）；修复后应归到 cqkd_bill / subentry / cqkd_sub。
    """
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rows = _facc(conn, plugin_fqn="cqspb.am.AmArrayOp", field_key="cqkd_subf",
                     access="write")
        assert rows, "数组入参链路的子分录写入应被归因到 AmArrayOp"
        r = rows[0]
        assert r["form_key"] == "cqkd_bill", "数组入参来源单据应沿 for-each/stream/addNew 传播"
        assert r["level"] == "subentry"
        assert r["entry_key"] == "cqkd_sub"
    finally:
        conn.close()


def test_bound_plugin_helper_uses_binding_entity(tmp_path: Path):
    """已绑定插件里未被事件覆盖的 helper（落第②轮补全）→ 沿用本类唯一绑定单据作来源（不再 None）。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        r = conn.execute(
            "SELECT form_key,plugin_type,event_method FROM field_access "
            "WHERE plugin_fqn='cqspb.am.AmHelperPlugin' AND field_key='cqkd_status'").fetchone()
        assert r is not None, "未被事件覆盖的 helper 写入仍应被全量补全抓到"
        assert r["form_key"] == "cqkd_bill", "应沿用该类唯一绑定单据作来源（修复前为 None）"
        assert r["event_method"] == "recalcUnreached"
    finally:
        conn.close()


def test_list_dynamicobject_param_source_propagation(tmp_path: Path):
    """泛型集合 List<DynamicObject>：.add(loadSingle(...,\"实体\")) 累积 → 经 List 形参传播给
    helper，helper for-each 读字段的来源应是 add 进来的实体（修复前整条链 form_key=None）。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        r = conn.execute(
            "SELECT form_key,via FROM field_access "
            "WHERE access_class='cqspb.am.AmListParamPlugin' "
            "AND field_key='cqkd_othh' AND via='do.getString'").fetchone()
        assert r is not None, "List<DynamicObject> 形参里的 for-each 读应被抓到"
        assert r["form_key"] == "cqkd_assetcollateral", \
            "来源应由 add(loadSingle(...,'cqkd_assetcollateral')) 经 List 形参传播得出（修复前为 None）"
    finally:
        conn.close()


def test_field_trace_coordinate_grouping(tmp_path: Path):
    """同字段跨单据 → 按实体坐标分组；form 过滤缩到单个坐标。"""
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        ft = field_trace.field_trace(conn, "cqkd_head")
        forms = {g["form_key"] for g in ft["groups"]}
        assert {"cqkd_bill", "cqkd_bill2"} <= forms              # 两单据各成一组
        assert ft["summary"]["coords"] >= 2
        narrowed = field_trace.field_trace(conn, "cqkd_head", form_key="cqkd_bill")
        assert {g["form_key"] for g in narrowed["groups"]} == {"cqkd_bill"}
    finally:
        conn.close()
