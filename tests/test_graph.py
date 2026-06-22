"""阶段 4 验收测试 —— 知识图谱存储（graph/store.py + schema.sql）。

覆盖：幂等重建（建两次计数一致、不残留）、节点/边灌库计数、FTS5 全文检索命中、
kb_meta 元信息与版本、source_class 孤儿标注与模块归属。
"""

from __future__ import annotations

from pathlib import Path

from cosmic_kb.bridge import linker, namespace
from cosmic_kb.graph import store
from cosmic_kb.ingest import scanner
from cosmic_kb.metadata.model import (
    MetaEntity, MetaField, MetaModel, MetaPlugin,
)
from cosmic_kb.report import project_map


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _plugin(class_name, *, ptype="form", source="project"):
    return MetaPlugin(class_name=class_name, plugin_type=ptype, source=source)


def _model(key, name, app_key, plugins, *, entities=None, fields=None):
    return MetaModel(
        key=key, name=name, model_type="BillFormModel", form_type="bill",
        isv="cqkd", app_key=app_key, plugins=plugins,
        entities=entities or [], fields=fields or [],
    )


def _build(tmp_path: Path):
    """造一个含两模块 + 孤儿的合成项目，跑全链路并灌进 temp KB。返回 (db, counts)。"""
    _write(tmp_path / "AssetCardFormPlugin.java",
           "package cqspb.assets;\npublic class AssetCardFormPlugin {}\n")
    _write(tmp_path / "AssetCardService.java",
           "package cqspb.assets;\npublic class AssetCardService {}\n")
    _write(tmp_path / "BdFormPlugin.java",
           "package cqspb.bd;\npublic class BdFormPlugin {}\n")
    _write(tmp_path / "RandomUtil.java",
           "package cqspb.shared;\npublic class RandomUtil {}\n")
    scan = scanner.scan(tmp_path)
    models = [
        _model("cqkd_assetcard", "资产卡片", "cqkd_assets",
               [_plugin("cqspb.assets.AssetCardFormPlugin")],
               entities=[MetaEntity("BillEntity", "cqkd_assetcard", "资产卡片主体",
                                    "1", "header", None, "t_asset")],
               fields=[MetaField("TextField", "cqkd_name", "名称", "fname", "f1",
                                 None, "entity", "header", "cqkd_assetcard")]),
        _model("cqkd_bd", "基础资料", "cqkd_bd",
               [_plugin("cqspb.bd.BdFormPlugin")]),
    ]
    index = namespace.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    mm = project_map.module_map(scan, models, bridge, index=index)
    db = tmp_path / "kb.db"
    counts = store.build_kb(scan, models, bridge, mm, db, index=index)
    return db, counts


def test_build_counts(tmp_path: Path):
    db, counts = _build(tmp_path)
    assert counts["form"] == 2
    assert counts["plugin"] == 2
    assert counts["source_class"] == 4
    assert counts["entity"] == 1
    assert counts["field"] == 1
    assert db.is_file()


def test_idempotent_rebuild(tmp_path: Path):
    """重建两次：计数一致、无残留（DROP→重建是幂等的）。"""
    db, counts1 = _build(tmp_path)
    db2, counts2 = _build(tmp_path)  # 同一 tmp_path → 同一 db 路径
    assert counts1 == counts2
    conn = store.open_kb(db)
    try:
        # 表里不应有翻倍残留。
        assert conn.execute("SELECT COUNT(*) FROM form").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM source_class").fetchone()[0] == 4
    finally:
        conn.close()


def test_kb_meta_and_version(tmp_path: Path):
    db, _ = _build(tmp_path)
    assert store.kb_exists(db)
    conn = store.open_kb(db)
    try:
        assert store.get_meta(conn, "schema_version") == store.KB_SCHEMA_VERSION
        assert store.get_meta(conn, "built_at")
        assert store.get_meta(conn, "health")
    finally:
        conn.close()


def test_source_class_orphan_flag(tmp_path: Path):
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rows = {r["fqn"]: r for r in conn.execute(
            "SELECT fqn,is_orphan,orphan_role,module FROM source_class")}
        # 绑定类非孤儿。
        assert rows["cqspb.assets.AssetCardFormPlugin"]["is_orphan"] == 0
        # service 孤儿、包前缀命中 → 归 cqkd_assets。
        assert rows["cqspb.assets.AssetCardService"]["is_orphan"] == 1
        assert rows["cqspb.assets.AssetCardService"]["module"] == "cqkd_assets"
        # 散落孤儿 → 未归类。
        assert rows["cqspb.shared.RandomUtil"]["module"] == project_map.MOD_UNCLASSIFIED
    finally:
        conn.close()


def test_fts_search(tmp_path: Path):
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        # 按中文名检索表单。
        hits = store.search(conn, "资产卡片")
        kinds = {(h["kind"], h["key"]) for h in hits}
        assert ("form", "cqkd_assetcard") in kinds
        # 按类名检索源码类。
        hits2 = store.search(conn, "AssetCardService")
        assert any(h["kind"] == "class" for h in hits2)
    finally:
        conn.close()


def test_edges_present(tmp_path: Path):
    db, _ = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        kinds = {r["kind"] for r in conn.execute("SELECT DISTINCT kind FROM edge")}
        assert {"has_entity", "has_field", "has_plugin", "bound_to", "module_contains"} <= kinds
        # 模块→类 的归属边存在。
        n = conn.execute(
            "SELECT COUNT(*) FROM edge WHERE kind='module_contains' AND dst_type='class'"
        ).fetchone()[0]
        assert n >= 1
    finally:
        conn.close()
