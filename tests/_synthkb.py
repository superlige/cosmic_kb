"""阶段9 测试用合成 KB —— 直接按 schema.sql 建库 + 插入最小行，不跑重型 Java 管线。

覆盖语义/上下文层要用到的全部表与场景：
  * 两张单据：cqkd_assetcard「资产卡片」、cqkd_contract「合同信息」。
  * 同名字段跨单据：cqkd_amount「金额」在两张单各一份（消歧/反问场景）。
  * 旗舰链路：cqkd_collateralstatus「抵押状态」被 op 插件经 service 跨类写入并落库。
  * 插件解释：CollateralService（孤儿 service）+ CollateralOp（已绑定 op 插件）。
  * 操作解释：cqkd_assetcard 的 audit「审核」操作绑定 CollateralOp。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cosmic_kb.graph import store


def make_kb(tmp_path: Path) -> Path:
    db = tmp_path / "synth.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    conn.executemany(
        "INSERT INTO form(key,name,form_type,model_type,isv,app_key,module,source_dym) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [
            ("cqkd_assetcard", "资产卡片", "bill", "BillFormModel", "cqkd", "cqkd_assets",
             "cqkd_assets", "a.dym"),
            ("cqkd_contract", "合同信息", "bill", "BillFormModel", "cqkd", "cqkd_assets",
             "cqkd_assets", "c.dym"),
        ],
    )
    conn.executemany(
        "INSERT INTO entity(form_key,key,name,level,parent_key,table_name) VALUES(?,?,?,?,?,?)",
        [
            ("cqkd_assetcard", "cqkd_assetcard", "资产卡片主体", "header", None, "t_card"),
            ("cqkd_assetcard", "cqkd_entry", "资产明细", "entry", "cqkd_assetcard", "t_entry"),
            ("cqkd_contract", "cqkd_contract", "合同主体", "header", None, "t_contract"),
        ],
    )
    conn.executemany(
        "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        [
            ("u1", "cqkd_assetcard", "cqkd_assetcard", "cqkd_collateralstatus", "抵押状态",
             "fcollst", "ComboField", "entity", "header"),
            ("u2", "cqkd_assetcard", "cqkd_entry", "cqkd_amount", "金额",
             "famount", "AmountField", "entity", "entry"),
            ("u3", "cqkd_contract", "cqkd_contract", "cqkd_amount", "金额",
             "famount", "AmountField", "entity", "header"),
        ],
    )
    conn.executemany(
        "INSERT INTO plugin(uid,form_key,class_name,plugin_type,source,operation_key,operation_name,enabled) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [
            ("p1", "cqkd_assetcard", "cqspb.assets.CollateralOp", "op", "project",
             "audit", "审核", 1),
        ],
    )
    conn.executemany(
        "INSERT INTO source_class(fqn,simple,package,relpath,module,is_orphan,orphan_role,plugin_base) "
        "VALUES(?,?,?,?,?,?,?,?)",
        [
            ("cqspb.assets.CollateralOp", "CollateralOp", "cqspb.assets",
             "cqspb/assets/CollateralOp.java", "cqkd_assets", 0, None, None),
            ("cqspb.assets.CollateralService", "CollateralService", "cqspb.assets",
             "cqspb/assets/CollateralService.java", "cqkd_assets", 1, None, None),
        ],
    )
    conn.executemany(
        "INSERT INTO operation(form_key,key,name,operation_type,resolved_from,has_operation_plugin) "
        "VALUES(?,?,?,?,?,?)",
        [
            ("cqkd_assetcard", "audit", "审核", "audit", "self", 1),
            ("cqkd_assetcard", "save", "保存", "save", "template", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO plugin_method(plugin_fqn,method_name,event_kind,event_phase,start_line,"
        "end_line,source_relpath) VALUES(?,?,?,?,?,?,?)",
        [
            ("cqspb.assets.CollateralOp", "beforeExecuteOperationTransaction",
             "beforeExecuteOperationTransaction", "transaction", 20, 39,
             "cqspb/assets/CollateralOp.java"),
            ("cqspb.assets.CollateralService", "update", "helper", "helper", 40, 70,
             "cqspb/assets/CollateralService.java"),
        ],
    )
    conn.executemany(
        "INSERT INTO field_access(form_key,field_key,level,entry_key,plugin_fqn,plugin_type,"
        "access_class,access_method,event_method,event_phase,access,persists,persist_reason,via,line,path,"
        "key_resolution,confidence,source_relpath,evidence,edge_source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("cqkd_assetcard", "cqkd_collateralstatus", "header", None,
             "cqspb.assets.CollateralOp", "op", "cqspb.assets.CollateralService",
             "update", "beforeExecuteOperationTransaction", "transaction", "write", "yes",
             "入库类操作(audit)事务内 setValue", "do.set", 41,
             json.dumps(["beforeExecuteOperationTransaction", "update"]),
             "literal", 0.95, "cqspb/assets/CollateralService.java", "", "heuristic"),
            ("cqkd_assetcard", "cqkd_amount", "entry", "cqkd_entry",
             "cqspb.assets.CollateralOp", "op", "cqspb.assets.CollateralOp",
             "beforeExecuteOperationTransaction", "beforeExecuteOperationTransaction", "transaction", "read", "na",
             "", "model.getValue", 55, json.dumps(["beforeExecuteOperationTransaction"]),
             "literal", 0.9, "cqspb/assets/CollateralOp.java", "", "local"),
        ],
    )
    conn.executemany(
        "INSERT INTO binding(class_name,form_key,plugin_type,status,source_relpath,confidence,note) "
        "VALUES(?,?,?,?,?,?,?)",
        [
            ("cqspb.assets.CollateralOp", "cqkd_assetcard", "op", "linked",
             "cqspb/assets/CollateralOp.java", 1.0, ""),
        ],
    )
    conn.executemany(
        "INSERT INTO call_edge(caller_fqn,caller_method,target_fqn,target_method,target_signature,"
        "kind,line,col,source_relpath,resolution,target_kind,confidence,evidence) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("cqspb.assets.CollateralOp", "beforeExecuteOperationTransaction",
             "cqspb.assets.CollateralService", "update",
             "cqspb.assets.CollateralService.update(kd.bos.dataentity.entity.DynamicObject)",
             "invocation", 31, 9, "cqspb/assets/CollateralOp.java", "expr", "project", 1.0,
             "symbol:expr"),
            ("cqspb.assets.CollateralOp", "wireHandlers",
             "cqspb.assets.CollateralService", "update",
             "cqspb.assets.CollateralService.update(kd.bos.dataentity.entity.DynamicObject)",
             "method_reference", 35, 21, "cqspb/assets/CollateralOp.java", "scope", "project", 0.95,
             "symbol:scope"),
            ("cqspb.assets.CollateralService", "update", "kd.bos.servicehelper.SaveServiceHelper",
             "save", "kd.bos.servicehelper.SaveServiceHelper.save(kd.bos.orm.dataentity.DynamicObject)",
             "invocation", 60, 13, "cqspb/assets/CollateralService.java", "expr", "jar", 1.0,
             "symbol:expr"),
            ("cqspb.assets.CollateralService", "update", None, "dynamicInvoke", None,
             "invocation", 64, 13, "cqspb/assets/CollateralService.java", "failed", None, 0.0,
             "symbol:failed; reason=unsolved-symbol"),
        ],
    )
    # 字段级分析可用标记（field_trace 读 java_analysis）。
    conn.executemany(
        "INSERT INTO kb_meta(key,value) VALUES(?,?)",
        [
            ("schema_version", store.KB_SCHEMA_VERSION),
            ("java_analysis", json.dumps({"available": True})),
            ("symbol_resolution", json.dumps({
                "status": "ok", "coverage": 0.97, "files": 2, "files_failed": 0,
                "sites": 100, "resolved": 97,
            })),
            ("built_at", "test"),
        ],
    )
    conn.commit()
    conn.close()
    return db


_SCHEMA_PATH = Path(store.__file__).with_name("schema.sql")
