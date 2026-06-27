"""阶段 4 验收测试 —— 多信号模块识别 + 理解报告 + CLI 端到端。

覆盖：appKey 锚定模块、孤儿仅在包前缀一致时归入（否则未归类）、包结构一致度/健康度、
"乱包路径"降级标低可信、风险热点不含常量类、超大表单按字段数、build/report 命令链路。
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

from cosmic_kb.bridge import linker, namespace
from cosmic_kb.graph import store
from cosmic_kb.ingest import scanner
from cosmic_kb.metadata.model import MetaField, MetaModel, MetaPlugin
from cosmic_kb.report import overview as overview_report
from cosmic_kb.report import project_map


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _plugin(class_name, *, ptype="form", source="project"):
    return MetaPlugin(class_name=class_name, plugin_type=ptype, source=source)


def _model(key, name, app_key, plugins, *, fields=None):
    return MetaModel(
        key=key, name=name, model_type="BillFormModel", form_type="bill",
        isv="cqkd", app_key=app_key, plugins=plugins, fields=fields or [],
    )


def _mk_map(tmp_path: Path):
    """干净的两模块项目 + 一个散落孤儿 + 一个常量孤儿。"""
    _write(tmp_path / "AssetCardFormPlugin.java",
           "package cqspb.assets;\npublic class AssetCardFormPlugin {}\n")
    _write(tmp_path / "AssetCardService.java",
           "package cqspb.assets;\npublic class AssetCardService {}\n")
    _write(tmp_path / "BdFormPlugin.java",
           "package cqspb.bd;\npublic class BdFormPlugin {}\n")
    _write(tmp_path / "RandomUtil.java",
           "package cqspb.shared;\npublic class RandomUtil {}\n")
    _write(tmp_path / "BdConstants.java",
           "package cqspb.bd.cons;\npublic class BdConstants {}\n")
    scan = scanner.scan(tmp_path)
    models = [
        _model("cqkd_assetcard", "资产卡片", "cqkd_assets",
               [_plugin("cqspb.assets.AssetCardFormPlugin")]),
        _model("cqkd_bd", "基础资料", "cqkd_bd",
               [_plugin("cqspb.bd.BdFormPlugin")]),
    ]
    index = namespace.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    return project_map.module_map(scan, models, bridge, index=index)


def test_appkey_anchored_modules(tmp_path: Path):
    mm = _mk_map(tmp_path)
    by_name = {m["name"]: m for m in mm["modules"]}
    assert "cqkd_assets" in by_name
    assert "cqkd_bd" in by_name
    # 资产模块：1 表单、主导包 cqspb.assets、绑定类 + 一致孤儿。
    assets = by_name["cqkd_assets"]
    assert assets["app_key"] == "cqkd_assets"
    assert assets["dominant_package"] == "cqspb.assets"
    assert assets["form_count"] == 1


def test_orphan_assigned_only_when_package_matches(tmp_path: Path):
    mm = _mk_map(tmp_path)
    cm = mm["class_module"]
    # service 在 cqspb.assets（资产独占包）→ 归 cqkd_assets。
    assert cm["cqspb.assets.AssetCardService"] == "cqkd_assets"
    # RandomUtil 在 cqspb.shared（无人独占）→ 未归类。
    assert cm["cqspb.shared.RandomUtil"] == project_map.MOD_UNCLASSIFIED
    # 未归类清单含且仅含真孤儿 RandomUtil。
    uncls = {o["fqn"] for o in mm["unclassified"]}
    assert uncls == {"cqspb.shared.RandomUtil"}


def test_constant_orphan_not_in_risk(tmp_path: Path):
    """常量孤儿（cons 包）归入模块但不计真孤儿，且不进未归类。"""
    mm = _mk_map(tmp_path)
    cm = mm["class_module"]
    # BdConstants 走 cqspb.bd.cons → 回退到独占包 cqspb.bd → 归 cqkd_bd。
    assert cm["cqspb.bd.cons.BdConstants"] == "cqkd_bd"
    uncls = {o["fqn"] for o in mm["unclassified"]}
    assert "cqspb.bd.cons.BdConstants" not in uncls
    bd = next(m for m in mm["modules"] if m["name"] == "cqkd_bd")
    assert bd["orphan_real_count"] == 0  # 常量不算真孤儿


def test_clean_structure_high_confidence(tmp_path: Path):
    mm = _mk_map(tmp_path)
    h = mm["health"]
    assert h["overall_consistency"] == 1.0
    assert h["scattered_package_count"] == 0
    assert "高度可信" in h["verdict"]


def test_messy_packages_downgrade(tmp_path: Path):
    """多开发者把不同模块的类塞进同一共享包 → 包结构不一致、降级标低可信。"""
    _write(tmp_path / "AssetThing.java",
           "package cqspb.common;\npublic class AssetThing {}\n")
    _write(tmp_path / "BdThing.java",
           "package cqspb.common;\npublic class BdThing {}\n")
    scan = scanner.scan(tmp_path)
    models = [
        _model("cqkd_assetcard", "资产卡片", "cqkd_assets",
               [_plugin("cqspb.common.AssetThing")]),
        _model("cqkd_bd", "基础资料", "cqkd_bd",
               [_plugin("cqspb.common.BdThing")]),
    ]
    index = namespace.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    mm = project_map.module_map(scan, models, bridge, index=index)
    h = mm["health"]
    # cqspb.common 被两模块共用 → 散落包、零独占命中 → 一致度 0、🔴。
    assert "cqspb.common" in h["scattered_packages"]
    assert h["overall_consistency"] == 0.0
    assert "仅供参考" in h["verdict"]


def test_oversized_form_in_risk(tmp_path: Path):
    """超大表单（字段数 ≥ 阈值）进风险热点。"""
    _write(tmp_path / "P.java", "package cqspb.assets;\npublic class P {}\n")
    big_fields = [
        MetaField("TextField", f"cqkd_f{i}", f"字段{i}", f"c{i}", f"id{i}",
                  None, "entity", "header", "cqkd_big")
        for i in range(overview_report.OVERSIZED_FORM_FIELDS + 5)
    ]
    scan = scanner.scan(tmp_path)
    models = [_model("cqkd_big", "巨型单据", "cqkd_assets",
                     [_plugin("cqspb.assets.P")], fields=big_fields)]
    index = namespace.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    mm = project_map.module_map(scan, models, bridge, index=index)
    db = tmp_path / "kb.db"
    store.build_kb(scan, models, bridge, mm, db, index=index)
    conn = store.open_kb(db)
    try:
        ov = overview_report.overview(conn)
    finally:
        conn.close()
    keys = {f["key"] for f in ov["risk"]["oversized_forms"]}
    assert "cqkd_big" in keys
    # 字段级排障已落地（阶段5+6+7），overview 含 field_analysis 概况块。
    assert "field_analysis" in ov
    assert ov["field_analysis"].get("available") is not None


def test_render_map_text(tmp_path: Path):
    mm = _mk_map(tmp_path)
    text = project_map.render_map(mm)
    assert "项目地图" in text
    assert "包结构健康度诊断" in text
    assert "cqkd_assets" in text


def test_cli_build_and_report(tmp_path: Path):
    """build → report overview / map 端到端，读同一 KB。"""
    from cosmic_kb.cli.main import main

    _write(tmp_path / "AssetCardFormPlugin.java",
           "package cqspb.assets;\npublic class AssetCardFormPlugin {}\n")
    repo = Path(__file__).resolve().parents[1]
    dym = repo / "samples" / "bill" / "cqkd_assetcard.dym"
    if not dym.exists():
        import pytest
        pytest.skip("缺样例 dym")

    db = tmp_path / "kb.db"
    # build
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["build", str(tmp_path), str(dym), "--db", str(db)])
    assert rc == 0
    assert db.is_file()
    assert "KB 已建好" in buf.getvalue()

    # report overview --json（读 KB）
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["report", "overview", "--db", str(db), "--json"])
    assert rc == 0
    ov = json.loads(buf.getvalue())
    assert "overview" in ov and "risk" in ov and "module_map" in ov

    # report map 文本
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["report", "map", "--db", str(db)])
    assert rc == 0
    assert "项目地图" in buf.getvalue()


def _bare_kb(tmp_path: Path):
    """直接按 schema 建一个最小 KB（手灌行），用于验报告富化，不跑全链路。"""
    import sqlite3

    from cosmic_kb.report import field_trace

    schema = (Path(field_trace.__file__).resolve().parents[1] / "graph" / "schema.sql").read_text("utf-8")
    conn = sqlite3.connect(str(tmp_path / "bare.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    # 单据 A=字段来源单，B=插件注册单（跨单据修改）。
    conn.execute("INSERT INTO form(key,name,form_type) VALUES('cqkd_src','源单据','bill')")
    conn.execute("INSERT INTO form(key,name,form_type) VALUES('cqkd_plugbill','插件单','bill')")
    conn.execute("INSERT INTO field(uid,form_key,entity_key,key,name,kind,level) "
                 "VALUES('u1','cqkd_src','cqkd_src','cqkd_x','字段X','entity','header')")
    # 插件 P 注册在单据 B 上。
    conn.execute("INSERT INTO plugin(uid,form_key,class_name,plugin_type,source) "
                 "VALUES('p1','cqkd_plugbill','com.x.PluginP','op','project')")
    # 一条写记录：数据包来源单=A，但触发它的插件 P 属于单据 B。
    conn.execute(
        "INSERT INTO field_access(form_key,field_key,level,entry_key,plugin_fqn,plugin_type,"
        "access_class,event_method,event_phase,access,persists,persist_reason,via,line,path,"
        "key_resolution,confidence,source_relpath,evidence) VALUES("
        "'cqkd_src','cqkd_x','header',NULL,'com.x.PluginP','op','com.x.PluginP',"
        "'beforeExecute','transaction','write','yes','入库操作事务内','model.setValue',42,"
        "'[\"beforeExecute\"]','literal',0.9,'src/com/x/PluginP.java',NULL)")
    # 转换规则：A 作为源单，下游目标单 B。
    conn.execute("INSERT INTO convert_rule(id,name,source_entity,target_entity) "
                 "VALUES('r1','测试转换','cqkd_src','cqkd_plugbill')")
    conn.commit()
    return conn


def test_field_trace_plugin_home_and_convert(tmp_path: Path):
    """字段排障富化：标出插件所属单据（跨单据）+ 转换上下游。"""
    from cosmic_kb.report import field_trace

    conn = _bare_kb(tmp_path)
    try:
        ft = field_trace.field_trace(conn, "cqkd_x")
    finally:
        conn.close()

    assert ft["groups"], "应有按来源实体分组的记录"
    g = next(g for g in ft["groups"] if g["form_key"] == "cqkd_src")
    w = g["writers"][0]
    # 插件 P 注册在 B，字段来源是 A → 跨单据修改，标签含 B。
    assert w["plugin_cross_form"] is True
    # 精简行只留 plugin_form_label（plugin_forms 列表已剔除，label 已编码所属单据）。
    assert "cqkd_plugbill" in (w["plugin_form_label"] or "")
    # 转换上下游：A 的下游目标单含 B。
    cc = g["convert_context"]
    assert any(x["entity"] == "cqkd_plugbill" for x in cc["downstream"])

    # CLI 文本渲染应包含所属单据与转换上下游摘要。
    text = field_trace.render_field_trace(ft)
    assert "cqkd_plugbill" in text
    assert "转换上下游" in text


def test_cli_report_missing_kb_errors(tmp_path: Path):
    """KB 不存在且未给重建入参 → 报错提示先 build。"""
    from cosmic_kb.cli.main import main

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = main(["report", "overview", "--db", str(tmp_path / "nope.db")])
    assert rc == 2
    assert "KB 不存在" in buf.getvalue()
