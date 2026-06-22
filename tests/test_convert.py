"""阶段 2/3 增补验收测试 —— 转换规则解析 + 转换插件桥接 + 插件基类孤儿识别。

覆盖：
  * `.cr` 转换规则解析（单据上下游、分录映射、字段映射计数、转换插件）；
  * 单层 zip（samples/trans）整包加载兜底；
  * 转换插件作为 plugin_type='convert' 走桥接命中；
  * 继承苍穹插件基类（含经过项目中间基类的传递闭包）的孤儿打 role='plugin'；
  * 图谱落库：convert_rule 表 / converts_to 边 / source_class.plugin_base。

样例缺失时 skip，不让测试硬失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cosmic_kb import _assets
from cosmic_kb.bridge import linker, namespace
from cosmic_kb.ingest import scanner
from cosmic_kb.metadata import dym_parser, package_loader
from cosmic_kb.metadata.model import ConvertInfo, MetaModel, MetaPlugin

SAMPLES = _assets.PROJECT_ROOT / "samples"
TRANS_DIR = SAMPLES / "trans"
TRANS_ZIP = TRANS_DIR / "1187235223415319552.zip"

needs_trans = pytest.mark.skipif(not TRANS_ZIP.exists(), reason="缺转换规则样例")


def _write(p: Path, text: str, encoding: str = "utf-8") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode(encoding))


def _convert_model(key, name, plugin_fqn, *, src="cqkd_a", tgt="cqkd_b"):
    plugins = [MetaPlugin(class_name=plugin_fqn, plugin_type="convert", source="project")] if plugin_fqn else []
    return MetaModel(
        key=key, name=name, model_type="ConvertRuleModel", form_type="convert",
        isv="cqkd", plugins=plugins,
        convert=ConvertInfo(source_entity=src, target_entity=tgt),
    )


# ── 阶段 2：转换规则解析 ──────────────────────────────────────────────
@needs_trans
def test_load_trans_package_single_layer():
    """单层 zip（metadata/*.cr 直接在外层）应被整包加载兜底解析。"""
    res = package_loader.load_package(TRANS_ZIP)
    assert res.ok_entries, "应解析出转换规则"
    converts = [e.model for e in res.ok_entries if e.model.form_type == "convert"]
    assert len(converts) == len(res.ok_entries)  # 该样例全是转换规则
    assert len(converts) >= 50


@needs_trans
def test_convert_rule_relation_and_plugin():
    """转换规则应带上下游单据、字段映射计数，部分绑定转换插件。"""
    res = package_loader.load_package(TRANS_ZIP)
    models = [e.model for e in res.ok_entries]

    for m in models:
        assert m.convert is not None
        assert m.convert.source_entity and m.convert.target_entity
        # 转换规则不是表单：无实体/字段。
        assert not m.entities and not m.fields

    with_plugin = [m for m in models if m.plugins]
    assert with_plugin, "应有转换规则绑定了转换插件"
    p = with_plugin[0].plugins[0]
    assert p.plugin_type == "convert"
    assert p.source == "project"
    assert p.class_name and "." in p.class_name  # 完整保留 ClassName 全限定名

    # 字段映射计数应为正（有映射的规则）。
    assert any(m.convert.field_map_count > 0 for m in models)


@needs_trans
def test_convert_to_dict_roundtrip():
    res = package_loader.load_package(TRANS_ZIP)
    d = res.ok_entries[0].model.to_dict()
    assert d["form_type"] == "convert"
    assert "convert" in d
    assert set(d["convert"]) >= {"source_entity", "target_entity", "field_map_count"}


# ── 阶段 3：转换插件桥接 ──────────────────────────────────────────────
def test_convert_plugin_bridged(tmp_path: Path):
    _write(
        tmp_path / "botp" / "Push.java",
        "package cqkd.am.botp;\npublic class Push extends AbstractConvertPlugIn {}\n",
    )
    scan = scanner.scan(tmp_path)
    m = _convert_model("r1", "下推", "cqkd.am.botp.Push")
    res = linker.link(scan, [m])
    linked = [b for b in res.bindings if b.status == "linked"]
    assert len(linked) == 1
    assert linked[0].plugin_type == "convert"
    assert linked[0].class_name == "cqkd.am.botp.Push"


# ── 阶段 3：插件基类孤儿（传递闭包）──────────────────────────────────
def test_plugin_orphan_transitive(tmp_path: Path):
    # 项目中间基类 + 经过它的插件 + 直接插件 + 普通类。
    _write(tmp_path / "a" / "XxxBasePlugin.java",
           "package a;\npublic abstract class XxxBasePlugin extends AbstractBillPlugIn {}\n")
    _write(tmp_path / "a" / "Foo.java",
           "package a;\npublic class Foo extends XxxBasePlugin {}\n")
    _write(tmp_path / "a" / "Direct.java",
           "package a;\npublic class Direct extends AbstractListPlugin implements java.io.Serializable {}\n")
    _write(tmp_path / "a" / "Bar.java", "package a;\npublic class Bar {}\n")

    scan = scanner.scan(tmp_path)
    res = linker.link(scan, [])  # 无元数据绑定 → 全是孤儿
    roles = {o.fqn: (o.role, o.plugin_base) for o in res.orphans}

    assert roles["a.Foo"] == ("plugin", "AbstractBillPlugIn")      # 传递闭包：经中间基类
    assert roles["a.XxxBasePlugin"] == ("plugin", "AbstractBillPlugIn")
    assert roles["a.Direct"] == ("plugin", "AbstractListPlugin")   # 直接继承
    assert roles["a.Bar"][0] == "unknown"                          # 普通类仍真孤儿


def test_resolve_plugin_classes_simple_name(tmp_path: Path):
    _write(tmp_path / "P.java",
           "package x;\npublic class P extends AbstractOperationServicePlugIn {}\n")
    idx = namespace.build_index(scanner.scan(tmp_path))
    assert namespace.resolve_plugin_classes(idx).get("P") == "AbstractOperationServicePlugIn"


def test_extends_implements_extracted(tmp_path: Path):
    _write(tmp_path / "G.java",
           "package x;\npublic class G<T> extends Base<T> implements I1, I2<String> {}\n")
    idx = namespace.build_index(scanner.scan(tmp_path))
    unit = idx.by_fqn["x.G"][0]
    assert set(unit.type_supers.get("G", [])) == {"Base", "I1", "I2"}


# ── 阶段 4：图谱落库 ──────────────────────────────────────────────────
def test_kb_convert_rule_and_edges(tmp_path: Path):
    from cosmic_kb.bridge import namespace as ns
    from cosmic_kb.graph import store
    from cosmic_kb.report import project_map

    src = tmp_path / "src"
    _write(src / "botp" / "Push.java",
           "package cqkd.am.botp;\npublic class Push extends AbstractConvertPlugIn {}\n")
    _write(src / "dead" / "Dead.java",
           "package cqkd.am.dead;\npublic class Dead extends AbstractBillPlugIn {}\n")

    scan = scanner.scan(src)
    models = [_convert_model("r1", "下推", "cqkd.am.botp.Push", src="cqkd_a", tgt="cqkd_b")]
    index = ns.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    mm = project_map.module_map(scan, models, bridge, index=index)

    db = tmp_path / "kb.db"
    counts = store.build_kb(scan, models, bridge, mm, db, index=index)
    assert counts["convert_rule"] == 1
    assert counts["form"] == 0  # 转换规则不进 form 表

    conn = store.open_kb(db)
    try:
        row = conn.execute("SELECT source_entity,target_entity,plugin_count FROM convert_rule").fetchone()
        assert row["source_entity"] == "cqkd_a" and row["target_entity"] == "cqkd_b"
        assert row["plugin_count"] == 1
        edge = conn.execute(
            "SELECT src_id,dst_id FROM edge WHERE kind='converts_to'").fetchone()
        assert edge["src_id"] == "cqkd_a" and edge["dst_id"] == "cqkd_b"
        base = conn.execute(
            "SELECT plugin_base FROM source_class WHERE simple='Dead'").fetchone()
        assert base["plugin_base"] == "AbstractBillPlugIn"
    finally:
        conn.close()
