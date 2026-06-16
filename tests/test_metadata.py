"""阶段 2 验收测试 —— dym 三类解析、模板 oid 回填、整包 zip、meta 命令。

合成 XML 用例保证关键机制的确定性（ParentId 建树、嵌套 Id 不误取）；真实样例用例
（samples/ 下三类 dym + 整包 zip）对齐验收口径。样例缺失时 skip，不让测试硬失败。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from cosmic_kb import _assets
from cosmic_kb.cli.main import main as cli_main
from cosmic_kb.metadata import dym_io, dym_parser, package_loader
from cosmic_kb.metadata.template_loader import TemplateRegistry

SAMPLES = _assets.PROJECT_ROOT / "samples"
BILL = SAMPLES / "bill" / "cqkd_assetcard.dym"
BASEDATA = SAMPLES / "basedata" / "cqkd_bill_calculation.dym"
FORM = SAMPLES / "form" / "cqkd_adjusttolcontract.dym"
PACKAGE = SAMPLES / "appzip" / "cqkd_flasset-cqkd_assets-20260610221847.zip"
TEMPLATE_DIR = SAMPLES / "bos_temp"

needs_bill = pytest.mark.skipif(not BILL.exists(), reason="缺单据样例")
needs_basedata = pytest.mark.skipif(not BASEDATA.exists(), reason="缺基础资料样例")
needs_form = pytest.mark.skipif(not FORM.exists(), reason="缺动态表单样例")
needs_pkg = pytest.mark.skipif(not PACKAGE.exists(), reason="缺整包样例")


# ── 合成 XML：验证核心机制（不依赖样例文件）────────────────────────
_SYNTH = """<?xml version="1.0" encoding="UTF-8"?>
<DeployMetadata><DesignMetas>
  <DesignFormMeta>
    <DataXml><FormMetadata>
      <Key>t_form</Key><Name>测试单</Name><Isv>cqkd</Isv>
      <InheritPath>ROOT_TPL</InheritPath>
      <Items>
        <BillFormAp><Plugins>
          <Plugin><ClassName>cqkd.demo.FormPlugin</ClassName></Plugin>
        </Plugins></BillFormAp>
        <ListMeta><FormMetadata><Items><FormAp><Plugins>
          <Plugin><ClassName>cqkd.demo.ListPlugin</ClassName></Plugin>
        </Plugins></FormAp></Items></FormMetadata></ListMeta>
      </Items>
    </FormMetadata></DataXml>
    <ModelType>BillFormModel</ModelType>
  </DesignFormMeta>
  <DesignEntityMeta>
    <DataXml><EntityMetadata>
      <Key>t_form</Key><Name>测试单</Name><InheritPath>ROOT_TPL</InheritPath>
      <Items>
        <BillEntity oid="MAINOID"><Id>MAIN</Id><Key>t_form</Key><Name>表头</Name>
          <Operations>
            <Operation action="edit" oid="SAVE_OID">
              <Plugins><Plugin><ClassName>cqkd.demo.SaveOp</ClassName></Plugin></Plugins>
              <Parameter><SaveParameter><StatusFieldId>NESTED</StatusFieldId></SaveParameter></Parameter>
            </Operation>
            <Operation>
              <Plugins><Plugin><ClassName>cqkd.demo.MyOp</ClassName></Plugin></Plugins>
              <Name>我的操作</Name><OperationType>donothing</OperationType>
              <Id>OWN_OP_ID</Id><Key>myop</Key>
            </Operation>
          </Operations>
        </BillEntity>
        <TextField><Id>F_HEAD</Id><Key>t_head</Key><FieldName>fk_head</FieldName><Name>表头字段</Name></TextField>
        <EntryEntity><Id>ENTRY1</Id><Key>t_entry</Key><Name>分录</Name></EntryEntity>
        <ComboField><Id>F_ENTRY</Id><Key>t_combo</Key><FieldName>fk_combo</FieldName>
          <ParentId>ENTRY1</ParentId><Name>分录下拉</Name>
          <Items><ComboItem><Caption>是</Caption><Value>1</Value></ComboItem>
                 <ComboItem><Caption>否</Caption><Value>0</Value></ComboItem></Items>
        </ComboField>
        <SubEntryEntity><Id>SUB1</Id><Key>t_sub</Key><ParentId>ENTRY1</ParentId><Name>子分录</Name></SubEntryEntity>
        <TextField><Id>F_SUB</Id><Key>t_subfield</Key><FieldName>fk_sub</FieldName>
          <ParentId>SUB1</ParentId><Name>子分录字段</Name></TextField>
        <TextField><Id>F_INH</Id><Key>t_inh</Key><ParentId>ROOT_TPL</ParentId><Name>继承字段</Name></TextField>
      </Items>
    </EntityMetadata></DataXml>
    <ModelType>BillFormModel</ModelType>
  </DesignEntityMeta>
</DesignMetas></DeployMetadata>
"""

_SYNTH_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<EntityMetadata><Items><BillEntity><Operations>
  <Operation><Id>SAVE_OID</Id><Key>save</Key><Name>保存</Name><OperationType>save</OperationType>
    <Parameter><SaveParameter><Id>SHOULD_NOT_PICK</Id></SaveParameter></Parameter>
  </Operation>
</Operations></BillEntity></Items></EntityMetadata>
"""


def _synth_model(tmp_path):
    """用合成模板目录解析合成 dym。"""
    (tmp_path / "bos_billtpl.dym").write_bytes(_SYNTH_TEMPLATE.encode("utf-8"))
    reg = TemplateRegistry(tmp_path)
    root = dym_io.parse_bytes(_SYNTH.encode("utf-8"))
    return dym_parser.parse_element(root, template_registry=reg, source_file="synth")


def test_synth_hierarchy_by_parentid(tmp_path):
    """字段层级靠 ParentId 建树回填：表头/分录/子分录/继承各归各位。"""
    m = _synth_model(tmp_path)
    by_key = {f.key: f for f in m.fields}
    assert by_key["t_head"].level == "header"
    assert by_key["t_combo"].level == "entry" and by_key["t_combo"].entity_key == "t_entry"
    assert by_key["t_subfield"].level == "subentry" and by_key["t_subfield"].entity_key == "t_sub"
    # ParentId 指向 InheritPath 根 → 继承字段
    assert by_key["t_inh"].kind == "inherited" and by_key["t_inh"].level == "header"


def test_synth_combo_items(tmp_path):
    m = _synth_model(tmp_path)
    combo = next(f for f in m.fields if f.key == "t_combo")
    assert [(c.caption, c.value) for c in combo.combo_items] == [("是", "1"), ("否", "0")]


def test_combo_items_not_tied_to_combofield_tag():
    """下拉项抽取按"是否含 ComboItem"判断，不写死 ComboField —— 新类型（如
    MulComboField 多选下拉）照样能抽到选项，避免基于样例已知类型而漏。"""
    el = ET.fromstring(
        "<MulComboField><Key>k</Key><Items>"
        "<ComboItem><Caption>甲</Caption><Value>a</Value></ComboItem>"
        "<ComboItem><Caption>乙</Caption><Value>b</Value></ComboItem>"
        "</Items></MulComboField>"
    )
    items = dym_parser._parse_combo_items(el)
    assert [(c.caption, c.value) for c in items] == [("甲", "a"), ("乙", "b")]


def test_synth_operation_oid_uses_direct_child_id(tmp_path):
    """模板回填取 Operation 直接子 Id（SAVE_OID），不误取嵌套 SaveParameter/Id。"""
    m = _synth_model(tmp_path)
    save = next(o for o in m.operations if o.oid == "SAVE_OID")
    assert save.key == "save" and save.name == "保存" and save.resolved_from == "template"
    own = next(o for o in m.operations if o.key == "myop")
    assert own.resolved_from == "self" and own.operation_type == "donothing"


def test_synth_plugins_three_locations(tmp_path):
    """界面/列表/操作三处插件都收齐，且操作插件回填绑定操作。"""
    m = _synth_model(tmp_path)
    by_type = {p.plugin_type: [x.class_name for x in m.plugins if x.plugin_type == p.plugin_type]
               for p in m.plugins}
    assert "cqkd.demo.FormPlugin" in by_type["form"]
    assert "cqkd.demo.ListPlugin" in by_type["list"]
    save_op_plugin = next(p for p in m.plugins if p.class_name == "cqkd.demo.SaveOp")
    assert save_op_plugin.plugin_type == "op" and save_op_plugin.operation_name == "保存"


def test_synth_platform_vs_project_source(tmp_path):
    """kd.bos.* = platform；其它（含 kd.gzw.* / cqkd. / cqspb.）= project。"""
    from cosmic_kb.metadata.dym_parser import _plugin_source
    assert _plugin_source("kd.bos.form.plugin.TemplateBillEdit") == "platform"
    assert _plugin_source("kd.gzw.asset.X") == "project"
    assert _plugin_source("cqspb.sysint.X") == "project"
    assert _plugin_source(None) == "unknown"


# ── 真实样例：单据 ──────────────────────────────────────────────────
@needs_bill
def test_bill_sample():
    m = dym_parser.parse_file(BILL, template_registry=TemplateRegistry(TEMPLATE_DIR))
    assert m.form_type == "bill" and m.model_type == "BillFormModel"
    assert m.key == "cqkd_assetcard" and m.name == "资产卡片"

    # 实体层级：1 表头 + 9 分录 + 1 子分录
    levels = [e.level for e in m.entities]
    assert levels.count("header") == 1
    assert levels.count("entry") == 9
    assert levels.count("subentry") == 1

    # 字段无遗漏 + 三级层级都在
    flevels = [f.level for f in m.fields]
    assert flevels.count("header") > 0
    assert flevels.count("entry") > 0
    assert flevels.count("subentry") > 0
    assert "unknown" not in flevels  # 全部字段都能归位，无悬空

    # 字段类型解析：下拉项抽到了
    combos = [f for f in m.fields if f.field_type == "ComboField" and f.combo_items]
    assert combos, "应解析出带下拉项的 ComboField"

    # 插件三类齐全
    ptypes = {p.plugin_type for p in m.plugins}
    assert {"form", "list", "op"} <= ptypes

    # hex oid 回填：4 个继承操作经模板译出中文名（保存/提交/删除/反审核）
    resolved = {o.name for o in m.operations if o.resolved_from == "template"}
    assert {"保存", "提交", "删除", "反审核"} <= resolved

    # 操作插件都绑定到了具体操作（保存上挂着压缩图片 Op）
    save_ops = [p for p in m.plugins if p.plugin_type == "op" and p.operation_name == "保存"]
    assert any("CompressImageOp" in (p.class_name or "") for p in save_ops)


@needs_bill
def test_bill_classname_fully_qualified():
    """阶段 3 桥接唯一钥匙：ClassName 必须保留全限定名（含包路径），不截末段。"""
    m = dym_parser.parse_file(BILL, template_registry=TemplateRegistry(TEMPLATE_DIR))
    names = [p.class_name for p in m.plugins if p.source == "project"]
    assert any(n and n.startswith("cqspb.sysint.assets.") for n in names)
    assert all("." in n for n in names if n)  # 没有被截成裸类名


# ── 真实样例：基础资料 ──────────────────────────────────────────────
@needs_basedata
def test_basedata_sample():
    m = dym_parser.parse_file(BASEDATA, template_registry=TemplateRegistry(TEMPLATE_DIR))
    assert m.form_type == "basedata" and m.model_type == "BaseFormModel"
    # 基础资料是实体型：应有带 FieldName 的 entity 字段
    assert any(f.kind == "entity" and f.db_column for f in m.fields)
    # 两级继承链
    assert len(m.inherit_path) == 2


# ── 真实样例：动态表单（验收第 1 条关键分支）──────────────────────
@needs_form
def test_dynamic_form_sample():
    m = dym_parser.parse_file(FORM, template_registry=TemplateRegistry(TEMPLATE_DIR))
    assert m.form_type == "dynamic" and m.model_type == "DynamicFormModel"
    # 动态表单字段照样解析出来，但无 DB 列 → kind=dynamic
    assert m.fields, "动态表单也要把字段解析出来"
    assert all(f.db_column is None for f in m.fields)
    assert any(f.kind == "dynamic" for f in m.fields)
    # kd.gzw.* 属客户 ISV，按 project 处理（非 platform）
    assert any(p.source == "project" and (p.class_name or "").startswith("kd.gzw.")
               for p in m.plugins)


# ── 真实样例：整包 zip（验收第 4 条）────────────────────────────────
@needs_pkg
def test_package_lists_all_forms():
    result = package_loader.load_package(
        PACKAGE, template_registry=TemplateRegistry(TEMPLATE_DIR)
    )
    assert len(result.ok_entries) > 100, "整包应列出大量表单"
    assert not result.failed_entries, "整包不应有解析失败的 dym"
    # appKey 作模块线索可用
    assert all(e.app_key for e in result.ok_entries)
    # 三类主线 + 其它类型都被如实标注（无误吞）
    ftypes = {e.model.form_type for e in result.ok_entries}
    assert {"bill", "basedata", "dynamic"} <= ftypes


# ── 多包解析（生产项目：一个 zip ≈ 一个业务模块）─────────────────────
APPZIP_DIR = SAMPLES / "appzip"
needs_multipkg = pytest.mark.skipif(
    sum(1 for _ in APPZIP_DIR.glob("*.zip")) < 2 if APPZIP_DIR.is_dir() else True,
    reason="appzip 下不足 2 个 zip，无法验证多包",
)


@needs_multipkg
def test_discover_zips_dir():
    found = package_loader.discover_zips(APPZIP_DIR)
    assert len(found) >= 2
    assert all(z.suffix == ".zip" for z in found)
    # 按名排序，稳定
    assert found == sorted(found)


@needs_multipkg
def test_load_packages_aggregate():
    zips = package_loader.discover_zips(APPZIP_DIR)
    multi = package_loader.load_packages(
        zips, template_registry=TemplateRegistry(TEMPLATE_DIR)
    )
    assert len(multi.packages) == len(zips)
    # 跨包总数 = 各包之和，无丢包
    assert multi.total_forms == sum(len(p.entries) for p in multi.packages)
    assert multi.ok_count > 100
    assert multi.failed_count == 0
    # 一个包≈一个模块：不同 zip 的 appKey 不同（多模块）
    app_keys = {
        e.app_key for p in multi.packages for e in p.ok_entries if e.app_key
    }
    assert len(app_keys) >= 2, "多包应覆盖多个 appKey(业务模块)"


@needs_multipkg
def test_cli_meta_multipackage_dir(capsys):
    rc = cli_main(["meta", str(APPZIP_DIR), "--template-dir", str(TEMPLATE_DIR)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "多包汇总" in out
    assert "按模块(appKey)" in out
    assert "各包明细" in out


@needs_multipkg
def test_cli_meta_multipackage_json(capsys):
    rc = cli_main(
        ["meta", str(APPZIP_DIR), "--template-dir", str(TEMPLATE_DIR), "--json"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["package_count"] >= 2
    assert data["ok"] > 100
    assert len(data["by_app_key"]) >= 2
    assert len(data["packages"]) == data["package_count"]


# ── CLI ─────────────────────────────────────────────────────────────
@needs_bill
def test_cli_meta_text(capsys):
    rc = cli_main(["meta", str(BILL), "--template-dir", str(TEMPLATE_DIR)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "资产卡片" in out and "插件" in out


@needs_bill
def test_cli_meta_json(capsys):
    rc = cli_main(["meta", str(BILL), "--template-dir", str(TEMPLATE_DIR), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["key"] == "cqkd_assetcard"
    assert data["form_type"] == "bill"
    assert len(data["fields"]) > 100
    assert data["plugins"], "JSON 快照应含插件"


def test_cli_meta_missing_path(capsys):
    rc = cli_main(["meta", "no_such_file.dym"])
    assert rc == 2
