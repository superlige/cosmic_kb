"""DB 元数据合成 —— 把底层库两张表的 fdata XML 拼回 MetaModel。

背景（见 docs/扩展元数据识别方案.md）：苍穹底层库里，一个单据/基础资料的元数据
拆存在两张设计表：
    t_meta_formdesign.fdata   → 布局/UI + 插件绑定，根节点 <FormMetadata>
    t_meta_entitydesign.fdata → 数据模型（实体/字段/操作），根节点 <EntityMetadata>

而本工具原有的 dym 解析器 `metadata.dym_parser` 吃的是**合体**结构：
    DeployMetadata/DesignMetas
      ├─ DesignFormMeta   → ModelType + DataXml → FormMetadata
      └─ DesignEntityMeta → ModelType + DataXml → EntityMetadata

两者内层 XML（FormMetadata / EntityMetadata）**完全同构**，差别只在外层包裹。
所以这里不重写解析器：把两段 DB XML 套回一个 DeployMetadata 骨架、补上反推的
ModelType，再交给 `parse_element`——解析器零改动，DB 只是"另一种 dym 来源"。

两个必须自己补的信息（DB XML 里没有、dym 外层才有）：
    1. ModelType —— DB XML 无此标签，按 entity 主实体标签反推
       （BillEntity→bill / BaseEntity→basedata / MainEntity→dynamic）。
    2. 组合 —— DB 是两条记录，须成对取回再合成；缺一端也能降级解析（只 UI 或只数据）。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ..metadata import dym_io
from ..metadata.dym_parser import parse_element
from ..metadata.model import MetaModel
from ..metadata.template_loader import TemplateRegistry

# entity 主实体标签 → dym 的 ModelType（与 dym_parser._MAIN_ENTITY_TAGS 一一对应）。
# 反推口径：数据模型的主实体标签唯一决定表单类别，比任何命名惯例都可靠。
_MAIN_TAG_TO_MODEL = {
    "BillEntity": "BillFormModel",
    "BaseEntity": "BaseFormModel",
    "MainEntity": "DynamicFormModel",
}


def _to_root(raw: bytes | str | None) -> ET.Element | None:
    """把一段 fdata（FormMetadata / EntityMetadata 明文 XML）解析成根元素。

    复用 dym_io 的健壮解码（信任声明→退 gb18030），DB 里若混了非 UTF-8 也不崩。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not raw.strip():
        return None
    return dym_io.parse_bytes(raw)


def _infer_model_type(entity_root: ET.Element | None) -> str | None:
    """按 entity 的主实体标签反推 ModelType；拿不到返回 None（form_type 记 unknown）。"""
    if entity_root is None:
        return None
    items = entity_root.find("Items")
    if items is None:
        return None
    for child in items:
        model_type = _MAIN_TAG_TO_MODEL.get(child.tag)
        if model_type:
            return model_type
    return None


def build_deploy_root(
    form_root: ET.Element | None,
    entity_root: ET.Element | None,
    model_type: str | None,
) -> ET.Element:
    """把 FormMetadata / EntityMetadata 两根套回 DeployMetadata 骨架（parse_element 的输入形态）。"""
    root = ET.Element("DeployMetadata")
    metas = ET.SubElement(root, "DesignMetas")
    if form_root is not None:
        dfm = ET.SubElement(metas, "DesignFormMeta")
        if model_type:
            ET.SubElement(dfm, "ModelType").text = model_type
        ET.SubElement(dfm, "DataXml").append(form_root)
    if entity_root is not None:
        dem = ET.SubElement(metas, "DesignEntityMeta")
        if model_type:
            ET.SubElement(dem, "ModelType").text = model_type
        ET.SubElement(dem, "DataXml").append(entity_root)
    return root


def assemble_model(
    form_fdata: bytes | str | None,
    entity_fdata: bytes | str | None,
    *,
    fnumber: str | None = None,
    template_registry: TemplateRegistry | None = None,
) -> MetaModel:
    """把两张设计表的 fdata 合成一个 MetaModel。

    form_fdata / entity_fdata 任一可为 None（对应 DB 里缺一条记录）：
    只给 entity → 拿到数据模型（字段/操作），缺 UI 插件；只给 form → 反之。
    两者皆空则抛错（无可解析内容）。
    """
    form_root = _to_root(form_fdata)
    entity_root = _to_root(entity_fdata)
    if form_root is None and entity_root is None:
        raise ValueError(f"form 与 entity 的 fdata 均为空，无法解析（fnumber={fnumber!r}）")

    model_type = _infer_model_type(entity_root)
    deploy_root = build_deploy_root(form_root, entity_root, model_type)
    source = f"db://{fnumber}" if fnumber else "db://<unknown>"
    return parse_element(
        deploy_root,
        template_registry=template_registry,
        source_file=source,
    )
