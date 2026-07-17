"""阶段 2 · 单 dym 三类解析器（统一入口）。

把单据 / 基础资料 / 动态表单三类 dym 统一解析成 `MetaModel`（见 model.py）。
解析机制依据 docs/核心/阶段验收.md「样例结构勘探结论」逐条对齐：

    DeployMetadata → DesignMetas
      ├─ DesignFormMeta   → DataXml → FormMetadata   → Items  布局/UI + 插件
      └─ DesignEntityMeta → DataXml → EntityMetadata → Items  数据模型（实体+字段+操作）

核心原则（坑都在勘探结论里）：
    1. 字段类型 = XML 标签名；下拉项在 ComboField/Items/ComboItem。
    2. 层级靠 <ParentId> 而非 XML 嵌套：无 ParentId = 表头；ParentId 指向分录/子分录 Id。
    3. 实体区是扁平兄弟列表 → **建索引后按 ParentId 回填**，禁用就近/首个匹配。
    4. 取操作 oid / 字段 Id 一律取**直接子 <Id>**（ET 的 find/findtext 默认只看直接子），
       避免误取块内嵌套元素（StatusField/Parameter）的 Id。
    5. 插件三处不遗漏：界面（FormAp/Plugins）、列表（ListMeta/.../Plugins）、
       操作（Operation/Plugins，含 validator）；完整保留 <ClassName> 全限定名。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from . import dym_io
from .model import (
    ComboItem,
    ConvertInfo,
    MetaEntity,
    MetaField,
    MetaModel,
    MetaOperation,
    MetaPlugin,
)
from .template_loader import OperationDef, TemplateRegistry

# 主实体标签（按集合识别，不写死单一标签）——三类 ModelType 各一种。
_MAIN_ENTITY_TAGS = frozenset({"BillEntity", "BaseEntity", "MainEntity"})
_ENTRY_TAGS = frozenset({"EntryEntity"})
_SUBENTRY_TAGS = frozenset({"SubEntryEntity"})
_ALL_ENTITY_TAGS = _MAIN_ENTITY_TAGS | _ENTRY_TAGS | _SUBENTRY_TAGS

# 规则表达式里的"伪字段"标签：虽以 Field 结尾，但不是数据字段，必须排除。
# （正常情况下它们嵌在 Rules/BizRule 内、不是 Items 直接子节点，这里再兜一层。）
_NON_FIELD_TAGS = frozenset({
    "TargetField", "SrcField", "DataDimensionField", "DataAssistDimensionField",
    "DataDimensionFieldId", "FieldId",
})

# ModelType → 归一 form_type。前三类是阶段 2 主线；后几类整包里会遇到，如实标注。
_FORM_TYPE_BY_MODEL = {
    "BillFormModel": "bill",
    "BaseFormModel": "basedata",
    "DynamicFormModel": "dynamic",
    "ReportFormModel": "report",
    "WidgetFormModel": "widget",
    "ParameterFormModel_bill": "param",
    "ParameterFormModel_application": "param",
    "ConvertRuleModel": "convert",
}

# 平台插件命名空间（无源码、由 SDK 文档解释）。设计决策：平台 = kd.bos.*；
# 其它 kd.* （如 kd.gzw.* 客户 ISV）按项目源码处理（见 docs 阶段 2 拍板口径）。
_PLATFORM_PREFIXES = ("kd.bos.",)


def _is_field_tag(tag: str) -> bool:
    return tag.endswith("Field") and tag not in _NON_FIELD_TAGS


def _plugin_source(class_name: str | None) -> str:
    if not class_name:
        return "unknown"
    if any(class_name.startswith(p) for p in _PLATFORM_PREFIXES):
        return "platform"
    return "project"


def _parse_bool(text: str | None) -> bool | None:
    if text is None:
        return None
    return text.strip().lower() == "true"


# ── 布局插件（界面 / 列表）──────────────────────────────────────────
def _parse_plugin_element(el: ET.Element, plugin_type: str) -> MetaPlugin:
    """解析单个 <Plugin>。无 ClassName 的平台插件用 oid 属性当类名（勘探结论第 5 条）。"""
    class_name = el.findtext("ClassName")
    if not class_name:
        # 平台插件常无 ClassName，oid 属性本身即全限定类名（如 kd.bos.form.plugin.*）。
        class_name = el.get("oid")
    return MetaPlugin(
        class_name=class_name,
        plugin_type=plugin_type,  # type: ignore[arg-type]
        source=_plugin_source(class_name),  # type: ignore[arg-type]
        description=el.findtext("Description"),
        enabled=_parse_bool(el.findtext("Enabled")),
    )


def _collect_layout_plugins(elem: ET.Element, context: str, out: list[MetaPlugin]) -> None:
    """递归遍历布局树，按最近的 ListMeta 祖先区分界面/列表插件。

    根 FormAp/BillFormAp 下的 Plugins → form；ListMeta/MobListMeta 下的 → list。
    （Plugin 内部不再含 Plugins，命中后无需继续下钻。）
    """
    for child in elem:
        tag = child.tag
        if tag in ("ListMeta", "MobListMeta"):
            _collect_layout_plugins(child, "list", out)
        elif tag == "Plugins":
            for p in child.findall("Plugin"):
                out.append(_parse_plugin_element(p, context))
        else:
            _collect_layout_plugins(child, context, out)


# ── 操作（含 hex oid 模板回填）──────────────────────────────────────
def _resolve_operation(
    op: ET.Element, template_map: dict[str, OperationDef]
) -> tuple[MetaOperation, str | None]:
    """解析单个 <Operation>，必要时按 oid 走模板回填。返回 (操作, 警告或 None)。"""
    oid_attr = op.get("oid")
    self_key = op.findtext("Key")
    self_id = op.findtext("Id")  # 直接子 Id = 操作自身主键（自定义操作）
    warning: str | None = None

    if self_key:  # 自带 Key/Name 的新定义操作 → 直接取自身
        return (
            MetaOperation(
                key=self_key,
                name=op.findtext("Name"),
                operation_type=op.findtext("OperationType"),
                id=self_id,
                oid=oid_attr,
                resolved_from="self",
            ),
            None,
        )

    if oid_attr:  # 继承覆盖型（只有 oid）→ 走模板回填
        defn = template_map.get(oid_attr)
        if defn:
            return (
                MetaOperation(
                    key=defn.key,
                    name=defn.name,
                    operation_type=defn.operation_type,
                    id=self_id,
                    oid=oid_attr,
                    resolved_from="template",
                ),
                None,
            )
        warning = f"操作 oid={oid_attr} 在模板中未命中，操作语义记 unknown"
        return (
            MetaOperation(
                key=None, name=None, operation_type=None,
                id=self_id, oid=oid_attr, resolved_from="unknown",
            ),
            warning,
        )

    # 既无 Key 又无 oid —— 罕见，标 unknown 不臆造。
    return (
        MetaOperation(
            key=None, name=None, operation_type=None,
            id=self_id, oid=None, resolved_from="unknown",
        ),
        "发现无 Key 且无 oid 的操作，记 unknown",
    )


def _parse_operations(
    entity_meta: ET.Element, template_map: dict[str, OperationDef]
) -> tuple[list[MetaOperation], list[MetaPlugin], list[str]]:
    """解析业务操作并补齐继承根模板的预制操作。

    业务 dym / DB fdata 只会写出自定义操作和被当前单据覆盖过的继承操作；完全沿用模板
    默认值的 ``refresh`` 等预制操作不会再次出现在业务 XML 中。若这里只解析显式节点，
    最终 KB 的操作集就会把这些真实可调用的预制操作漏掉。

    显式业务节点优先：同 key 的自定义/覆盖操作保留其自身语义和插件，模板只补缺失项。
    """
    operations: list[MetaOperation] = []
    op_plugins: list[MetaPlugin] = []
    warnings: list[str] = []
    for ops in entity_meta.iter("Operations"):
        for op in ops.findall("Operation"):
            meta_op, warn = _resolve_operation(op, template_map)
            operations.append(meta_op)
            if warn:
                warnings.append(warn)
            plugins_el = op.find("Plugins")
            if plugins_el is not None:
                for p in plugins_el.findall("Plugin"):
                    plugin = _parse_plugin_element(p, "op")
                    plugin.operation_key = meta_op.key
                    plugin.operation_name = meta_op.name
                    plugin.operation_oid = meta_op.oid
                    op_plugins.append(plugin)

    # 继承根模板定义的是该 ModelType 的完整预制操作集。业务 XML 中未出现的条目不是
    # “当前单据没有”，而是“完全沿用模板、因此没有生成 action=edit 节点”。按 key 补齐，
    # 已显式出现的操作（含自定义同名覆盖）拥有更高优先级，不重复追加。
    explicit_keys = {op.key for op in operations if op.key}
    for defn in template_map.values():
        if not defn.key or defn.key in explicit_keys:
            continue
        operations.append(MetaOperation(
            key=defn.key,
            name=defn.name,
            operation_type=defn.operation_type,
            id=None,
            oid=defn.oid,
            resolved_from="template",
        ))
        explicit_keys.add(defn.key)
    return operations, op_plugins, warnings


# ── 实体与字段 ──────────────────────────────────────────────────────
def _parse_entities(items: ET.Element) -> list[MetaEntity]:
    """从 Items 抽取所有实体（主实体/分录/子分录）。"""
    entities: list[MetaEntity] = []
    main_id: str | None = None
    for child in items:
        tag = child.tag
        if tag not in _ALL_ENTITY_TAGS:
            continue
        ent_id = child.findtext("Id") or child.get("oid")
        if tag in _MAIN_ENTITY_TAGS:
            level, parent = "header", None
            main_id = ent_id
        elif tag in _ENTRY_TAGS:
            level = "entry"
            # 分录的父是表头；少数会显式带 ParentId。
            parent = child.findtext("ParentId") or main_id
        else:  # 子分录
            level = "subentry"
            parent = child.findtext("ParentEntryId") or child.findtext("ParentId")
        entities.append(
            MetaEntity(
                entity_tag=tag,
                key=child.findtext("Key"),
                name=child.findtext("Name"),
                id=ent_id,
                level=level,  # type: ignore[arg-type]
                parent_id=parent,
                table_name=child.findtext("TableName"),
            )
        )
    return entities


def _parse_combo_items(el: ET.Element) -> list[ComboItem]:
    """下拉项：ComboField/Items/ComboItem（Caption=中文、Value=值）。"""
    items_el = el.find("Items")
    if items_el is None:
        return []
    out: list[ComboItem] = []
    for ci in items_el.findall("ComboItem"):
        out.append(ComboItem(caption=ci.findtext("Caption"), value=ci.findtext("Value")))
    return out


def _classify_field(
    *,
    tag: str,
    key: str | None,
    db_column: str | None,
    parent_id: str | None,
    is_override: bool,
    inherit_roots: set[str],
    form_type: str,
) -> str:
    """字段分类口径（用户 2026-06-14 拍板）。优先级见下。"""
    if tag == "BasedataPropField":
        return "basedata_prop"
    if parent_id and parent_id in inherit_roots:
        return "inherited"          # ParentId 指向 InheritPath 根 = 继承标准字段
    if is_override and key is None:
        return "inherited"          # action=edit 覆盖型，覆盖父模板已定义字段
    if db_column:
        return "entity"             # 有 FieldName = 落库实体字段
    if form_type == "dynamic":
        return "dynamic"            # 动态表单字段，无 DB 列（不是缺失）
    return "platform"               # 余下无 DB 列、非动态 = 平台标准字段


def _parse_fields(
    items: ET.Element,
    entities: list[MetaEntity],
    inherit_roots: set[str],
    form_type: str,
) -> list[MetaField]:
    """从 Items 抽取所有字段并按 ParentId 回填层级/所属实体。"""
    # 实体索引：Id → 实体（用于按 ParentId 定位层级与归属）。
    by_id: dict[str, MetaEntity] = {e.id: e for e in entities if e.id}
    main = next((e for e in entities if e.level == "header"), None)

    fields: list[MetaField] = []
    for child in items:
        tag = child.tag
        if not _is_field_tag(tag):
            continue
        is_override = child.get("action") == "edit" and child.get("oid") is not None
        fid = child.findtext("Id") or child.get("oid")
        key = child.findtext("Key")
        db_column = child.findtext("FieldName")
        parent_id = child.findtext("ParentId")

        # 层级与所属实体：按 ParentId 回填（禁用就近匹配）。
        if parent_id and parent_id in by_id:
            owner = by_id[parent_id]
            level, entity_key = owner.level, owner.key
        elif parent_id and parent_id in inherit_roots:
            level = "header"
            entity_key = main.key if main else None
        elif parent_id is None or (main and parent_id == main.id):
            level = "header"
            entity_key = main.key if main else None
        else:
            level = "unknown"       # ParentId 指向未知实体，标 unknown 不臆造
            entity_key = None

        kind = _classify_field(
            tag=tag, key=key, db_column=db_column, parent_id=parent_id,
            is_override=is_override, inherit_roots=inherit_roots, form_type=form_type,
        )
        fields.append(
            MetaField(
                field_type=tag,
                key=key,
                name=child.findtext("Name"),
                db_column=db_column,
                id=fid,
                parent_id=parent_id,
                kind=kind,  # type: ignore[arg-type]
                level=level,  # type: ignore[arg-type]
                entity_key=entity_key,
                # 任何带 Items/ComboItem 的字段都抽下拉项（不只 ComboField，
                # 还有 MulComboField 等多选下拉）；无 ComboItem 时返回空，安全。
                combo_items=_parse_combo_items(child),
                must_input=_parse_bool(child.findtext("MustInput")) or False,
                basedata_id=child.findtext("BaseEntityId"),
            )
        )
    return fields


# ── 转换规则（ConvertRuleModel / .cr）────────────────────────────────
def _parse_convert_rule(root: ET.Element, source_file: str | None) -> MetaModel:
    """解析单据转换规则 `.cr`：单据上下游关系 + 单据转换插件。

    结构（见 samples/trans 勘探）：
        DeployMetadata/DesignMetas/DesignConvertRuleMeta
          ├─ Id / Name / Isv / Enabled / SourceEntityNumber / TargetEntityNumber  ← 关系本体
          └─ DataXml/ConvertRuleMetadata/RuleElement/ConvertRuleElement
                ├─ Name                                       规则中文名
                ├─ LinkEntityPolicy//{Source,Target}EntryKey  分录级映射（可无）
                ├─ FieldMapPolicy//FieldMaps/FieldMapItem     字段映射（计数）
                └─ PlugInPolicy//Plugins/CRPlugin/ClassName   单据转换插件（可多个）
    转换规则不是表单（无实体/字段），故 entities/fields 留空，关系挂在 model.convert，
    转换插件复用 MetaPlugin（plugin_type='convert'）走通用桥接。
    """
    meta = root.find(".//DesignConvertRuleMeta")
    assert meta is not None  # 调用方已确认存在
    rule_meta = meta.find(".//ConvertRuleMetadata")

    # 关系本体：优先取 DesignConvertRuleMeta 直接子（外层稳定），缺则退 ConvertRuleMetadata。
    def _wrap_text(tag: str) -> str | None:
        return meta.findtext(tag) or (rule_meta.findtext(tag) if rule_meta is not None else None)

    source_entity = _wrap_text("SourceEntityNumber")
    target_entity = _wrap_text("TargetEntityNumber")
    isv = _wrap_text("Isv")
    rule_id = _wrap_text("Id")
    enabled = _parse_bool(meta.findtext("Enabled"))

    # 规则中文名、分录映射、字段映射条数、转换插件：在 ConvertRuleElement 内。
    name: str | None = None
    source_entry: str | None = None
    target_entry: str | None = None
    field_map_count = 0
    plugins: list[MetaPlugin] = []
    if rule_meta is not None:
        for elem in rule_meta.iter("ConvertRuleElement"):
            name = name or elem.findtext("Name")
            source_entry = source_entry or elem.findtext(".//SourceEntryKey")
            target_entry = target_entry or elem.findtext(".//TargetEntryKey")
            field_map_count += sum(1 for _ in elem.iter("FieldMapItem"))
            for cr in elem.iter("CRPlugin"):
                cn = cr.findtext("ClassName")
                if not cn:
                    continue
                plugins.append(MetaPlugin(
                    class_name=cn,
                    plugin_type="convert",
                    source=_plugin_source(cn),  # type: ignore[arg-type]
                    description=cr.findtext("Description") or cr.findtext("DisplayName"),
                ))
    name = name or _wrap_text("Name")

    return MetaModel(
        key=rule_id,                # 规则无独立编号，用 snowflake Id 作稳定键
        name=name,
        model_type=meta.findtext("ModelType") or "ConvertRuleModel",
        form_type="convert",
        isv=isv,
        source_file=source_file,
        plugins=plugins,
        convert=ConvertInfo(
            source_entity=source_entity,
            target_entity=target_entity,
            source_entry=source_entry,
            target_entry=target_entry,
            field_map_count=field_map_count,
            enabled=enabled,
        ),
    )


# ── 顶层入口 ────────────────────────────────────────────────────────
def _first_child(elem: ET.Element | None) -> ET.Element | None:
    if elem is None:
        return None
    return elem[0] if len(elem) else None


def parse_element(
    root: ET.Element,
    *,
    template_registry: TemplateRegistry | None = None,
    source_file: str | None = None,
) -> MetaModel:
    """从已解析的 dym 根元素构建 MetaModel。"""
    # 转换规则（.cr）走独立分支：它没有 DesignFormMeta/DesignEntityMeta，结构与三类表单不同。
    if root.find(".//DesignConvertRuleMeta") is not None:
        return _parse_convert_rule(root, source_file)

    design_form = root.find(".//DesignFormMeta")
    design_entity = root.find(".//DesignEntityMeta")

    # 布局根（FormMetadata），数据根（EntityMetadata）—— 取 DataXml 的首个子元素，
    # 不写死标签名（DynamicFormModel 等也走同一入口）。
    form_meta = _first_child(design_form.find("DataXml")) if design_form is not None else None
    entity_meta = _first_child(design_entity.find("DataXml")) if design_entity is not None else None

    model_type = None
    if design_form is not None:
        model_type = design_form.findtext("ModelType")
    if not model_type and design_entity is not None:
        model_type = design_entity.findtext("ModelType")
    form_type = _FORM_TYPE_BY_MODEL.get(model_type or "", "unknown")

    # 表单标识/中文名/ISV/继承链 —— 以布局根为主，缺则退数据根。
    head = form_meta if form_meta is not None else entity_meta
    key = head.findtext("Key") if head is not None else None
    name = head.findtext("Name") if head is not None else None
    isv = head.findtext("Isv") if head is not None else None
    inherit_raw = head.findtext("InheritPath") if head is not None else None
    inherit_path = [s for s in (inherit_raw.split(",") if inherit_raw else []) if s]
    inherit_roots = set(inherit_path)

    model = MetaModel(
        key=key,
        name=name,
        model_type=model_type,
        form_type=form_type,  # type: ignore[arg-type]
        isv=isv,
        inherit_path=inherit_path,
        source_file=source_file,
    )

    # 实体 + 字段（数据模型层）。
    if entity_meta is not None:
        items = entity_meta.find("Items")
        if items is not None:
            model.entities = _parse_entities(items)
            model.fields = _parse_fields(items, model.entities, inherit_roots, form_type)

        registry = template_registry or TemplateRegistry()
        template_map = registry.for_model_type(model_type)
        ops, op_plugins, warns = _parse_operations(entity_meta, template_map)
        model.operations = ops
        model.plugins.extend(op_plugins)
        model.warnings.extend(warns)

    # 界面/列表插件（布局层）。
    if form_meta is not None:
        layout_plugins: list[MetaPlugin] = []
        _collect_layout_plugins(form_meta, "form", layout_plugins)
        # 布局插件排在操作插件前，便于阅读（form/list 在前，op 在后）。
        model.plugins = layout_plugins + model.plugins

    return model


def parse_file(
    path: str | Path,
    *,
    template_registry: TemplateRegistry | None = None,
) -> MetaModel:
    """解析单个 dym 文件为 MetaModel。"""
    p = Path(path)
    root = dym_io.parse_file(p)
    return parse_element(root, template_registry=template_registry, source_file=str(p))
