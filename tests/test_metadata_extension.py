"""原厂元数据并入 KB · 扩展识别 + 合并（metadata/extension.py + metadata/merge.py）验收测试。

用真实样例端到端验证：`samples/db_xml/bd_customer_{form,entity}.txt`（原厂 DB fdata）
+ `samples/bill/cqkd_bd_customer_ext.dym`（本地扩展）——两者标识刚好是
`bd_customer` ↔ `cqkd_bd_customer_ext`，是本功能天然的验收样本。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from cosmic_kb import _assets
from cosmic_kb.dbmeta.assemble import assemble_model
from cosmic_kb.metadata import dym_parser
from cosmic_kb.metadata.extension import detect_extension
from cosmic_kb.metadata.merge import (
    build_extension_alias, merge_vendor_extension, strip_vendor_plugins,
)
from cosmic_kb.metadata.model import MetaEntity, MetaField, MetaModel

DB_XML = _assets.PROJECT_ROOT / "samples" / "db_xml"
FORM_FDATA = DB_XML / "bd_customer_form.txt"
ENTITY_FDATA = DB_XML / "bd_customer_entity.txt"
EXT_DYM = _assets.PROJECT_ROOT / "samples" / "bill" / "cqkd_bd_customer_ext.dym"

needs_sample = pytest.mark.skipif(
    not (FORM_FDATA.exists() and ENTITY_FDATA.exists() and EXT_DYM.exists()),
    reason="缺 db_xml / cqkd_bd_customer_ext 样例",
)


@pytest.fixture()
def vendor_model() -> MetaModel:
    return assemble_model(FORM_FDATA.read_bytes(), ENTITY_FDATA.read_bytes(), fnumber="bd_customer")


@pytest.fixture()
def extension_model() -> MetaModel:
    return dym_parser.parse_file(str(EXT_DYM))


# ── detect_extension ────────────────────────────────────────────────────────

@needs_sample
def test_detect_extension_real_sample(extension_model):
    """结构信号(InheritPath 非空) + 命名信号(cqkd_..._ext) 都满足 → 推出候选 bd_customer。"""
    assert extension_model.inherit_path  # 结构信号
    assert detect_extension(extension_model, {"cqkd_": 1}) == "bd_customer"


def test_detect_extension_no_inherit_path_returns_none():
    m = MetaModel(key="cqkd_bd_customer_ext", name=None, model_type=None, form_type="basedata",
                  isv="cqkd", inherit_path=[])  # 结构信号缺失
    assert detect_extension(m, {"cqkd_": 1}) is None


def test_detect_extension_wrong_suffix_returns_none():
    m = MetaModel(key="cqkd_bd_customer", name=None, model_type=None, form_type="basedata",
                  isv="cqkd", inherit_path=["root1"])  # 没有 _ext 后缀
    assert detect_extension(m, {"cqkd_": 1}) is None


def test_detect_extension_prefix_not_recognized_returns_none():
    m = MetaModel(key="other_bd_customer_ext", name=None, model_type=None, form_type="basedata",
                  isv="other", inherit_path=["root1"])
    assert detect_extension(m, {"cqkd_": 1}) is None  # 前缀不在本项目已知集合里


def test_detect_extension_none_bucket_ignored():
    """isv_prefixes 里 '(none)' 桶（discover_meta_prefixes 对无下划线 key 的归类）不参与匹配。"""
    m = MetaModel(key="cqkd_bd_customer_ext", name=None, model_type=None, form_type="basedata",
                  isv="cqkd", inherit_path=["root1"])
    assert detect_extension(m, {"(none)": 1}) is None
    assert detect_extension(m, {"cqkd_": 1, "(none)": 3}) == "bd_customer"


# ── strip_vendor_plugins ─────────────────────────────────────────────────────

@needs_sample
def test_strip_vendor_plugins(vendor_model):
    assert vendor_model.plugins  # 样例本身带原厂项目插件
    stripped = strip_vendor_plugins(vendor_model)
    assert stripped.plugins == []
    assert vendor_model.plugins  # 原对象不被就地修改


def test_strip_vendor_plugins_noop_when_empty():
    m = MetaModel(key="x", name=None, model_type=None, form_type="basedata", isv=None)
    assert strip_vendor_plugins(m) is m  # 无插件时直接返回原对象，不多余拷贝


def test_strip_vendor_plugins_keeps_explicitly_disabled():
    """真实翻车：`kd.cf.lgc.ht.opplugin.AdjustAmountOpplugin` 在原厂层被显式禁用
    （`<Enabled>false</Enabled>`），旧逻辑无条件清空原厂插件会把这条确定性信号也丢掉，
    KB 里只剩别处(如继承拷贝、无 Enabled 标签)的同类名条目，误报成 enabled=null（未知）
    而非"已确认禁用"。`enabled is False` 的原厂插件必须保留；enabled=True/None（无源码、
    追不到执行体）继续按拍板丢弃。"""
    from cosmic_kb.metadata.model import MetaPlugin

    vendor = MetaModel(
        key="cqkd_billadjust", name="账单调整", model_type="BillFormModel", form_type="bill", isv=None,
        plugins=[
            MetaPlugin(class_name="kd.cf.lgc.ht.opplugin.AdjustAmountOpplugin",
                       plugin_type="op", source="project", enabled=False),
            MetaPlugin(class_name="kd.bos.form.plugin.Foo", plugin_type="form", source="platform", enabled=None),
            MetaPlugin(class_name="kd.bd.master.CustomerSavePlugin", plugin_type="op", source="project", enabled=True),
        ],
    )
    stripped = strip_vendor_plugins(vendor)
    assert [p.class_name for p in stripped.plugins] == ["kd.cf.lgc.ht.opplugin.AdjustAmountOpplugin"]
    assert stripped.plugins[0].enabled is False
    assert vendor.plugins[0].enabled is False  # 原对象不被就地修改


def test_merge_vendor_extension_keeps_vendor_disabled_plugin():
    """merge_vendor_extension 汇总 plugins 时须带上 vendor 侧（已被 strip 收窄到仅剩确认
    禁用条目）+ 扩展侧，而不是无条件清零 vendor 那一半。"""
    from cosmic_kb.metadata.model import MetaPlugin

    header = MetaEntity("BaseEntity", "bd_x", "X", "h1", "header", None, "t_x")
    vendor = strip_vendor_plugins(MetaModel(
        key="bd_x", name="X", model_type="BaseFormModel", form_type="basedata", isv=None,
        entities=[header],
        plugins=[MetaPlugin(class_name="kd.cf.lgc.ht.opplugin.AdjustAmountOpplugin",
                             plugin_type="op", source="project", enabled=False)],
    ))
    ext_header = MetaEntity("BaseEntity", "cqkd_x_ext", "X扩展", "eh1", "header", None, None)
    ext = MetaModel(
        key="cqkd_x_ext", name=None, model_type=None, form_type="basedata", isv="cqkd",
        inherit_path=["root"], entities=[ext_header],
        plugins=[MetaPlugin(class_name="cqspb.XExtPlugin", plugin_type="form", source="project")],
    )
    merged = merge_vendor_extension(vendor, [ext])
    names = {p.class_name for p in merged.plugins}
    assert names == {"kd.cf.lgc.ht.opplugin.AdjustAmountOpplugin", "cqspb.XExtPlugin"}
    disabled = next(p for p in merged.plugins if p.class_name == "kd.cf.lgc.ht.opplugin.AdjustAmountOpplugin")
    assert disabled.enabled is False


# ── merge_vendor_extension：端到端（真实样例）───────────────────────────────

@needs_sample
def test_merge_vendor_extension_real_sample(vendor_model, extension_model):
    vendor = strip_vendor_plugins(vendor_model)
    merged = merge_vendor_extension(vendor, [extension_model])

    # 主 key = 原厂 key，不是扩展别名 key。
    assert merged.key == "bd_customer"
    assert merged.is_extension is False
    assert merged.extends is None

    # 只有一个表头实体（扩展自己的表头没有重复落一条）。
    headers = [e for e in merged.entities if e.level == "header"]
    assert len(headers) == 1
    assert headers[0].key == "bd_customer"

    # 扩展的分录实体被正确重新挂到原厂表头下（parent_id 改写）。
    ext_entry = next(e for e in merged.entities if e.key == "cqkd_formerentry")
    assert ext_entry.parent_id == headers[0].id

    # 扩展表头字段的 entity_key 被重写指向原厂表头 key。
    khlx = [f for f in merged.fields if f.key == "cqkd_khlx"]
    assert khlx and khlx[0].entity_key == "bd_customer"
    # 扩展分录字段的 entity_key 维持自己的分录 key，不受影响。
    formername = [f for f in merged.fields if f.key == "cqkd_formername"]
    assert formername and formername[0].entity_key == "cqkd_formerentry"

    # 原厂标准字段仍在（合并没有丢东西）。
    assert any(f.key == "simplename" for f in merged.fields)

    # 插件只来自扩展（原厂已 strip），原厂标准操作 + 扩展自定义操作都并入。
    assert merged.plugins == extension_model.plugins
    assert len(merged.operations) == len(vendor.operations) + len(extension_model.operations)


def test_merge_vendor_extension_no_local_extension_passthrough():
    """extensions=[] 时相当于只把原厂模型原样返回（is_extension/extends 归零）。"""
    header = MetaEntity("BaseEntity", "bd_x", "X", "h1", "header", None, "t_x")
    vendor = MetaModel(key="bd_x", name="X", model_type="BaseFormModel", form_type="basedata",
                       isv=None, entities=[header],
                       fields=[MetaField("TextField", "name", "名称", "fname", "f1", "h1",
                                         "platform", "header", "bd_x")])
    merged = merge_vendor_extension(vendor, [])
    assert merged.key == "bd_x"
    assert merged.fields == vendor.fields
    assert merged.plugins == []


def test_merge_vendor_extension_field_key_conflict_keeps_extension_side():
    """同 entity_key+key 两边都定义（正常不该发生）：保留扩展侧，丢原厂侧，记 warning 不静默。"""
    header = MetaEntity("BaseEntity", "bd_x", "X", "h1", "header", None, "t_x")
    vendor = MetaModel(
        key="bd_x", name="X", model_type="BaseFormModel", form_type="basedata", isv=None,
        entities=[header],
        fields=[MetaField("TextField", "dup", "原厂版", "fdup", "f1", "h1",
                          "entity", "header", "bd_x")],
    )
    ext_header = MetaEntity("BaseEntity", "cqkd_x_ext", "X扩展", "eh1", "header", None, None)
    ext = MetaModel(
        key="cqkd_x_ext", name=None, model_type=None, form_type="basedata", isv="cqkd",
        inherit_path=["root"], entities=[ext_header],
        fields=[MetaField("TextField", "dup", "扩展版", "fdup2", "f2", "eh1",
                          "entity", "header", "cqkd_x_ext")],
    )
    merged = merge_vendor_extension(vendor, [ext])
    dup_fields = [f for f in merged.fields if f.key == "dup"]
    assert len(dup_fields) == 1
    assert dup_fields[0].name == "扩展版"          # 保留扩展侧
    assert any("字段 key 冲突" in w for w in merged.warnings)  # 不静默


# ── build_extension_alias ────────────────────────────────────────────────────

@needs_sample
def test_build_extension_alias_empties_content(extension_model):
    alias = build_extension_alias(extension_model, "bd_customer")
    assert alias.key == "cqkd_bd_customer_ext"    # 原 key 不变，仍可被按原 key 查到
    assert alias.is_extension is True
    assert alias.extends == "bd_customer"
    assert alias.entities == []
    assert alias.fields == []
    assert alias.plugins == []
    assert alias.operations == []
    # 原对象不受影响（replace 返回新对象）。
    assert extension_model.fields  # 原扩展模型的字段列表仍在
