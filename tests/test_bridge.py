"""阶段 3 验收测试 —— 元数据 ↔ 源码桥接。

覆盖：命名空间抽取（package/顶层类/内部类/注释噪声/tree-sitter 兜底场景）、
三态分类（精确命中 / 平台外部 / 孤儿 / 按名降级 / 歧义 / 未找到）、前缀自动发现、
桥接报告、bridge 命令端到端。
"""

from __future__ import annotations

from pathlib import Path

from cosmic_kb.bridge import linker, namespace
from cosmic_kb.ingest import scanner
from cosmic_kb.metadata.model import MetaModel, MetaPlugin
from cosmic_kb.report import bridge_report


def _write(p: Path, text: str, encoding: str = "utf-8") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode(encoding))


def _plugin(class_name, *, ptype="form", source="project"):
    return MetaPlugin(class_name=class_name, plugin_type=ptype, source=source)


def _model(key, name, plugins):
    return MetaModel(
        key=key, name=name, model_type="BillFormModel",
        form_type="bill", isv="cqkd", plugins=plugins,
    )


# ── 命名空间抽取 ────────────────────────────────────────────────
def test_extract_package_and_fqn(tmp_path: Path):
    _write(
        tmp_path / "Foo.java",
        "package cqspb.sysint.assets;\n\npublic class Foo {}\n",
    )
    idx = namespace.build_index(scanner.scan(tmp_path))
    assert "cqspb.sysint.assets.Foo" in idx.by_fqn
    unit = idx.by_fqn["cqspb.sysint.assets.Foo"][0]
    assert unit.package == "cqspb.sysint.assets"
    assert unit.primary_type == "Foo"


def test_nested_class_not_top_level(tmp_path: Path):
    _write(
        tmp_path / "Outer.java",
        "package a.b;\npublic class Outer {\n  static class Inner {}\n}\n",
    )
    idx = namespace.build_index(scanner.scan(tmp_path))
    assert "a.b.Outer" in idx.by_fqn
    # 内部类 Inner 不应被当作顶层类型。
    assert "a.b.Inner" not in idx.by_fqn
    assert "Inner" not in idx.by_simple


def test_comment_and_string_noise_ignored(tmp_path: Path):
    _write(
        tmp_path / "Real.java",
        'package a;\n// class Fake {}\n/* class AlsoFake {} */\n'
        'public class Real { String s = "class Nope {}"; }\n',
    )
    idx = namespace.build_index(scanner.scan(tmp_path))
    assert set(idx.by_simple) == {"Real"}


def test_no_package_default(tmp_path: Path):
    _write(tmp_path / "Bare.java", "public class Bare {}\n")
    idx = namespace.build_index(scanner.scan(tmp_path))
    assert "Bare" in idx.by_fqn
    assert idx.by_fqn["Bare"][0].package is None


def test_secondary_top_level_type(tmp_path: Path):
    _write(
        tmp_path / "Main.java",
        "package a;\npublic class Main {}\nclass Helper {}\n",
    )
    idx = namespace.build_index(scanner.scan(tmp_path))
    assert "a.Main" in idx.by_fqn
    assert "a.Helper" in idx.by_fqn  # 同文件次类也入索引


# ── 三态分类 ────────────────────────────────────────────────────
def test_exact_hit(tmp_path: Path):
    _write(
        tmp_path / "cqspb" / "AssetCardFormPlugin.java",
        "package cqspb.assets;\npublic class AssetCardFormPlugin {}\n",
    )
    scan = scanner.scan(tmp_path)
    models = [_model("cqkd_assetcard", "资产卡片",
                     [_plugin("cqspb.assets.AssetCardFormPlugin")])]
    result = linker.link(scan, models)
    assert len(result.linked) == 1
    b = result.linked[0]
    assert b.status == "linked"
    assert b.confidence == 1.0
    assert b.source_relpath.endswith("AssetCardFormPlugin.java")


def test_platform_external(tmp_path: Path):
    scan = scanner.scan(tmp_path)  # 空源码树
    models = [_model("cqkd_x", "X",
                     [_plugin("kd.bos.list.plugin.StandardListPlugin",
                              source="platform")])]
    result = linker.link(scan, models)
    assert len(result.external) == 1
    assert not result.missing  # 平台类不报缺失


def test_platform_detected_by_kd_prefix_even_if_mislabeled(tmp_path: Path):
    scan = scanner.scan(tmp_path)
    # 元数据来源误标 project，但类名 kd.* → 仍归外部。
    models = [_model("cqkd_x", "X",
                     [_plugin("kd.bos.form.FormPlugin", source="project")])]
    result = linker.link(scan, models)
    assert len(result.external) == 1


def test_external_lowercase_last_segment(tmp_path: Path):
    """末段全小写 → 非 Java 类引用，归外部不报缺失（2026-06-16 规则）。"""
    scan = scanner.scan(tmp_path)
    models = [_model("cqkd_x", "X",
                     [_plugin("dev.tpl.base.kd.bos.form.plugin.templatebaseedit")])]
    result = linker.link(scan, models)
    assert len(result.external) == 1
    assert not result.missing


def test_external_embedded_kd_bos(tmp_path: Path):
    """内嵌 .kd.bos. 但末段 PascalCase → 仍归外部。"""
    scan = scanner.scan(tmp_path)
    models = [_model("cqkd_x", "X",
                     [_plugin("dev.tpl.base.kd.bos.form.plugin.TemplateBaseEdit")])]
    result = linker.link(scan, models)
    assert len(result.external) == 1


def test_pascalcase_project_class_still_linked(tmp_path: Path):
    """正常 PascalCase 项目类不受新规则影响，照常精确命中。"""
    _write(tmp_path / "P.java", "package p;\npublic class AssetCardFormPlugin {}\n")
    scan = scanner.scan(tmp_path)
    models = [_model("cqkd_x", "X", [_plugin("p.AssetCardFormPlugin")])]
    result = linker.link(scan, models)
    assert len(result.linked) == 1


def test_missing_project_plugin(tmp_path: Path):
    scan = scanner.scan(tmp_path)  # 空源码树
    models = [_model("cqkd_x", "X", [_plugin("cqspb.assets.GhostPlugin")])]
    result = linker.link(scan, models)
    assert len(result.missing) == 1
    assert result.missing[0].confidence == 0.0


def test_linked_by_name_when_fqn_mismatch(tmp_path: Path):
    # 源码包路径与元数据 ClassName 不一致，但类名唯一 → 降级命中。
    _write(
        tmp_path / "WrongPkg.java",
        "package some.other.pkg;\npublic class WrongPkg {}\n",
    )
    scan = scanner.scan(tmp_path)
    models = [_model("cqkd_x", "X", [_plugin("cqspb.expected.WrongPkg")])]
    result = linker.link(scan, models)
    assert len(result.linked_by_name) == 1
    assert result.linked_by_name[0].confidence == 0.6


def test_ambiguous_same_simple_name(tmp_path: Path):
    _write(tmp_path / "a" / "Dup.java", "package pkg.a;\npublic class Dup {}\n")
    _write(tmp_path / "b" / "Dup.java", "package pkg.b;\npublic class Dup {}\n")
    scan = scanner.scan(tmp_path)
    # ClassName 的 FQN 两处都不命中，但末段 Dup 出现两次 → 歧义。
    models = [_model("cqkd_x", "X", [_plugin("pkg.c.Dup")])]
    result = linker.link(scan, models)
    assert len(result.ambiguous) == 1
    assert len(result.ambiguous[0].candidates) == 2


def test_orphan_collection(tmp_path: Path):
    _write(
        tmp_path / "Bound.java",
        "package p;\npublic class Bound {}\n",
    )
    _write(
        tmp_path / "OrphanSvc.java",
        "package p;\npublic class OrphanSvc {}\n",
    )
    scan = scanner.scan(tmp_path)
    models = [_model("cqkd_x", "X", [_plugin("p.Bound")])]
    result = linker.link(scan, models)
    orphan_fqns = {o.fqn for o in result.orphans}
    assert "p.OrphanSvc" in orphan_fqns
    assert "p.Bound" not in orphan_fqns  # 已绑定不算孤儿


def test_orphan_role_constant(tmp_path: Path):
    """常量类按包名/类名信号打 role=constant，与真孤儿区分。"""
    _write(
        tmp_path / "LeaseDetailCon.java",
        "package cqspb.bd.common.cons.report;\npublic class LeaseDetailCon {}\n",
    )
    _write(
        tmp_path / "AssetService.java",
        "package cqspb.svc;\npublic class AssetService {}\n",
    )
    scan = scanner.scan(tmp_path)
    result = linker.link(scan, [])  # 无元数据 → 全是孤儿
    roles = {o.fqn: o.role for o in result.orphans}
    assert roles["cqspb.bd.common.cons.report.LeaseDetailCon"] == "constant"
    assert roles["cqspb.svc.AssetService"] == "unknown"


def test_orphan_role_constant_by_name_only(tmp_path: Path):
    """包名无 cons 段时，只认强后缀 Const*；短后缀 Con/Cons 已收紧去掉（2026-06-16）。"""
    _write(tmp_path / "BizConstants.java", "package p;\npublic class BizConstants {}\n")
    _write(tmp_path / "ParkConstant.java", "package p;\npublic class ParkConstant {}\n")
    _write(tmp_path / "FieldCon.java", "package p;\npublic class FieldCon {}\n")
    _write(tmp_path / "Falcon.java", "package p;\npublic class Falcon {}\n")
    scan = scanner.scan(tmp_path)
    result = linker.link(scan, [])
    roles = {o.fqn: o.role for o in result.orphans}
    assert roles["p.BizConstants"] == "constant"   # 强后缀 Constants
    assert roles["p.ParkConstant"] == "constant"   # 强后缀 Constant
    assert roles["p.FieldCon"] == "unknown"        # 短后缀 Con 已不再判常量
    assert roles["p.Falcon"] == "unknown"          # 小写 con 不误判


def test_orphan_constant_con_suffix_still_caught_via_package(tmp_path: Path):
    """*Con 类若在 cons 包里，仍由包名信号命中（收紧短后缀不会漏掉它们）。"""
    _write(
        tmp_path / "LeaseDetailCon.java",
        "package cqspb.bd.common.cons.report;\npublic class LeaseDetailCon {}\n",
    )
    scan = scanner.scan(tmp_path)
    result = linker.link(scan, [])
    roles = {o.fqn: o.role for o in result.orphans}
    assert roles["cqspb.bd.common.cons.report.LeaseDetailCon"] == "constant"


def test_summary_orphan_by_role(tmp_path: Path):
    _write(tmp_path / "XCon.java", "package p.cons;\npublic class XCon {}\n")
    _write(tmp_path / "Y.java", "package p.svc;\npublic class Y {}\n")
    scan = scanner.scan(tmp_path)
    s = bridge_report.summary(linker.link(scan, []))
    assert s["orphan_by_role"].get("constant") == 1
    assert s["orphan_by_role"].get("unknown") == 1


def test_plugin_base_recorded_for_bound_class(tmp_path: Path):
    """issue 1：已绑定元数据的插件类也要有 plugin_base（此前只在孤儿循环里算，绑定类落 None）。"""
    _write(
        tmp_path / "AuditOp.java",
        "package p;\nimport kd.bos.entity.plugin.AbstractOperationServicePlugIn;\n"
        "public class AuditOp extends AbstractOperationServicePlugIn {}\n",
    )
    scan = scanner.scan(tmp_path)
    models = [_model("cqkd_x", "X", [_plugin("p.AuditOp", ptype="op")])]
    result = linker.link(scan, models)
    assert result.linked and result.linked[0].status == "linked"
    assert result.plugin_bases["p.AuditOp"] == "AbstractOperationServicePlugIn"


def test_plugin_base_orphan_still_recorded(tmp_path: Path):
    """plugin_bases 是超集：未绑定的插件实现类（孤儿）依旧命中，且与 OrphanClass.plugin_base 一致。"""
    _write(
        tmp_path / "OrphanValidator.java",
        "package p;\npublic class OrphanValidator extends AbstractValidator {}\n",
    )
    scan = scanner.scan(tmp_path)
    result = linker.link(scan, [])
    assert result.plugin_bases["p.OrphanValidator"] == "AbstractValidator"
    orphan = next(o for o in result.orphans if o.fqn == "p.OrphanValidator")
    assert orphan.plugin_base == "AbstractValidator"
    assert result.plugin_bases["p.OrphanValidator"] == orphan.plugin_base


def test_no_class_name_plugin(tmp_path: Path):
    scan = scanner.scan(tmp_path)
    models = [_model("cqkd_x", "X", [_plugin(None)])]
    result = linker.link(scan, models)
    assert len(result.external) == 1
    assert "无 ClassName" in result.external[0].class_name


# ── 前缀自动发现 ────────────────────────────────────────────────
def test_prefix_discovery(tmp_path: Path):
    _write(tmp_path / "A.java", "package cqspb.x;\npublic class A {}\n")
    _write(tmp_path / "B.java", "package cqspb.y;\npublic class B {}\n")
    _write(tmp_path / "C.java", "package other.z;\npublic class C {}\n")
    scan = scanner.scan(tmp_path)
    models = [
        _model("cqkd_assetcard", "资产卡片", []),
        _model("cqkd_qz", "抵押", []),
        _model("cqbd_org", "组织", []),
    ]
    result = linker.link(scan, models)
    assert result.code_prefixes.get("cqspb") == 2
    assert result.code_prefixes.get("other") == 1
    assert result.meta_prefixes.get("cqkd_") == 2
    assert result.meta_prefixes.get("cqbd_") == 1


# ── 报告 ────────────────────────────────────────────────────────
def test_summary_hit_rate(tmp_path: Path):
    _write(tmp_path / "P.java", "package p;\npublic class P {}\n")
    scan = scanner.scan(tmp_path)
    models = [_model("cqkd_x", "X", [
        _plugin("p.P"),                       # linked
        _plugin("p.Gone"),                    # missing
        _plugin("kd.bos.X", source="platform"),  # external（不计入分母）
    ])]
    result = linker.link(scan, models)
    s = bridge_report.summary(result)
    assert s["project_plugin_total"] == 2
    assert s["hit_count"] == 1
    assert s["hit_rate"] == 0.5
    text = bridge_report.render(result)
    assert "桥接报告" in text


# ── CLI 端到端 ──────────────────────────────────────────────────
def test_cli_bridge_with_dym(tmp_path: Path):
    """用真实样例 dym + 合成源码跑通 bridge 命令（不依赖整个项目源码）。"""
    import json
    from cosmic_kb.cli.main import main

    repo = Path(__file__).resolve().parents[1]
    dym = repo / "samples" / "bill" / "cqkd_assetcard.dym"
    if not dym.exists():
        import pytest
        pytest.skip("缺样例 dym")

    # 造一个源码树（内容无所谓，验证命令链路与报告产出）。
    _write(tmp_path / "Some.java", "package cqspb.demo;\npublic class Some {}\n")

    out = tmp_path / "out.json"
    import contextlib, io as _io
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["bridge", str(tmp_path), str(dym), "--json"])
    assert rc in (0, 1)
    data = json.loads(buf.getvalue())
    assert "hit_rate" in data
    assert data["source_file_count"] >= 1
