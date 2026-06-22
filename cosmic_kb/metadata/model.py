"""阶段 2 · 统一元数据模型 `MetaModel`。

三类 dym（单据 BillFormModel / 基础资料 BaseFormModel / 动态表单 DynamicFormModel）
解析后统一成本模块的数据结构：表单 → 实体 → 分录层级 → 字段 → 类型 → 中文名↔标识 →
插件 → 操作。KB 是契约（见 CLAUDE.md 六条硬约束之"两段式解耦"），所以这里的
`to_dict()` JSON 快照就是阶段 2 对外的产物形态，字段命名力求稳定、自解释。

派生哲学：处处置信度 + unknown —— 解不出的（如继承操作 oid 无模板可查）一律标
`unknown`，绝不臆造（见 CLAUDE.md）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ── 字段分类口径（用户 2026-06-14 拍板：分类全保留、各自标注）──────────
#   entity        有 FieldName（落库实体字段）
#   dynamic       动态表单字段，无 DB 列（不是缺失，是这类元数据本就不落库）
#   basedata_prop 基础资料带出引用字段（BasedataPropField，Key 不唯一）
#   platform      平台标准字段（BillNoField / BillStatusField 等，随模板继承而来）
#   inherited     继承父模板的字段（ParentId 指向 InheritPath 根，或 action=edit 覆盖型）
FieldKind = Literal["entity", "dynamic", "basedata_prop", "platform", "inherited", "unknown"]

# ── 字段在表单中的层级 ──────────────────────────────────────────────
FieldLevel = Literal["header", "entry", "subentry", "unknown"]

# ── 表单类型（form_type，归一 ModelType）────────────────────────────
# 阶段 2 主攻 bill/basedata/dynamic 三类；整包里还会遇到报表/卡片/参数等其它
# ModelType，如实标注（report/widget/param）而非含糊的 unknown，解不出才记 unknown。
# convert = 转换规则（ConvertRuleModel）：不是表单，而是单据上下游关系，复用本模型承载
# （见 ConvertInfo），区别在 form_type 与 model.convert 字段。
FormType = Literal[
    "bill", "basedata", "dynamic", "report", "widget", "param", "convert", "unknown"
]

# ── 插件归属（writeback 反写插件 / convert 单据转换插件）──────────────
PluginType = Literal["form", "list", "op", "writeback", "convert", "unknown"]

# ── 插件来源 ────────────────────────────────────────────────────────
PluginSource = Literal["project", "platform", "unknown"]


@dataclass
class ComboItem:
    """下拉/枚举项（ComboField/Items/ComboItem）。"""

    caption: str | None       # 中文显示文本
    value: str | None         # 存储值


@dataclass
class MetaField:
    """一个字段。唯一键用 `id` 或 `(entity_key, key)` 复合键 —— 单用 key 不可靠
    （BasedataPropField 的 key 全是 'basedatapropfield'，跨实体也可能重名）。"""

    field_type: str               # 字段类型 = XML 标签名（TextField/ComboField/...）
    key: str | None               # 标识（<Key>），可能为 None（覆盖型字段只有 oid）
    name: str | None              # 中文名（<Name>）
    db_column: str | None         # 数据库列名（<FieldName>，动态表单为 None）
    id: str | None                # 内部主键（<Id>）；覆盖型字段取 oid 属性
    parent_id: str | None         # <ParentId>：所属实体的 Id；表头字段为 None
    kind: FieldKind               # 字段分类口径
    level: FieldLevel             # 层级：表头/分录/子分录
    entity_key: str | None        # 所属实体的 Key（回填得到）
    combo_items: list[ComboItem] = field(default_factory=list)
    must_input: bool = False
    basedata_id: str | None = None  # 基础资料/组织字段引用的实体 Id（BaseEntityId）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "field_type": self.field_type,
            "key": self.key,
            "name": self.name,
            "db_column": self.db_column,
            "id": self.id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "level": self.level,
            "entity_key": self.entity_key,
            "must_input": self.must_input,
        }
        if self.combo_items:
            d["combo_items"] = [
                {"caption": c.caption, "value": c.value} for c in self.combo_items
            ]
        if self.basedata_id:
            d["basedata_id"] = self.basedata_id
        return d


@dataclass
class MetaEntity:
    """一个实体：主实体（表头）/ 分录 / 子分录。"""

    entity_tag: str               # BillEntity/BaseEntity/MainEntity/EntryEntity/SubEntryEntity
    key: str | None               # <Key>
    name: str | None              # <Name>
    id: str | None                # <Id> 或 oid 属性
    level: FieldLevel             # header / entry / subentry
    parent_id: str | None         # 分录/子分录的父实体 Id（表头为 None）
    table_name: str | None        # <TableName>

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_tag": self.entity_tag,
            "key": self.key,
            "name": self.name,
            "id": self.id,
            "level": self.level,
            "parent_id": self.parent_id,
            "table_name": self.table_name,
        }


@dataclass
class MetaOperation:
    """一个操作（按钮行为）。继承覆盖型只带 oid，需经模板回填出 key/name。"""

    key: str | None               # 操作标识（save/submit/...）；解不出记 None
    name: str | None              # 中文名
    operation_type: str | None    # OperationType（save/audit/donothing/...）
    id: str | None                # 操作自身 Id（自定义操作）
    oid: str | None               # 继承覆盖型的 oid（hex），用于模板回填
    resolved_from: str | None = None  # 'self' | 'template' | 'unknown'，记录来源以示可信度

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "operation_type": self.operation_type,
            "id": self.id,
            "oid": self.oid,
            "resolved_from": self.resolved_from,
        }


@dataclass
class MetaPlugin:
    """一个插件绑定。完整保留 `<ClassName>` 全限定名 —— 它是阶段 3 桥接源码的
    唯一钥匙（见 CLAUDE.md「已拍板关键决策」），绝不只截末段类名。"""

    class_name: str | None        # 全限定名（<ClassName>）；平台插件无 ClassName 时取 oid
    plugin_type: PluginType       # form / list / op / writeback
    source: PluginSource          # project(cqkd./cqspb.) / platform(kd.*)
    description: str | None = None
    enabled: bool | None = None
    # 操作插件专属：绑定到哪个操作
    operation_key: str | None = None
    operation_name: str | None = None
    operation_oid: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "class_name": self.class_name,
            "plugin_type": self.plugin_type,
            "source": self.source,
            "description": self.description,
            "enabled": self.enabled,
        }
        if self.plugin_type == "op":
            d["operation_key"] = self.operation_key
            d["operation_name"] = self.operation_name
            d["operation_oid"] = self.operation_oid
        return d


@dataclass
class ConvertInfo:
    """转换规则（ConvertRuleModel / `.cr`）特有信息：单据上下游关系。

    转换规则不是表单，没有实体/字段，本质是「源单据→目标单据」的一条 BOTP 关系，
    可绑定单据转换插件（AbstractConvertPlugIn 子类，进 MetaModel.plugins，
    plugin_type='convert'）。这里只承载关系本体与映射规模，插件走通用 plugins 桥接。
    """

    source_entity: str | None      # 源单据(上游) 标识（SourceEntityNumber）
    target_entity: str | None      # 目标单据(下游) 标识（TargetEntityNumber）
    source_entry: str | None = None  # 源分录 key（LinkEntityPolicy/SourceEntryKey），表头级转换为 None
    target_entry: str | None = None  # 目标分录 key（LinkEntityPolicy/TargetEntryKey）
    field_map_count: int = 0       # 字段映射条数（FieldMapItem 计数，规模线索）
    enabled: bool | None = None    # 规则是否启用（Enabled）

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_entity": self.source_entity,
            "target_entity": self.target_entity,
            "source_entry": self.source_entry,
            "target_entry": self.target_entry,
            "field_map_count": self.field_map_count,
            "enabled": self.enabled,
        }


@dataclass
class MetaModel:
    """单个 dym 解析后的完整模型。"""

    key: str | None               # 表单标识（cqkd_assetcard）
    name: str | None              # 中文名（资产卡片）
    model_type: str | None        # 原始 ModelType（BillFormModel/...）
    form_type: FormType           # 归一类型 bill/basedata/dynamic
    isv: str | None               # 元数据 ISV 标识（cqkd），仅作报告产物，不作定位依据
    app_key: str | None = None    # 所属应用标识（appKey）；整包按目录回填，单 dym 为 None。
    # appKey 是阶段 4 模块识别的主锚（平台应用级标识，不受开发者包路径风格影响），
    # 比"代码包前缀"可靠 —— 见 docs/开发计划.md 阶段 4「模块识别（多信号）」。
    inherit_path: list[str] = field(default_factory=list)  # 继承链（根→直接父）
    entities: list[MetaEntity] = field(default_factory=list)
    fields: list[MetaField] = field(default_factory=list)
    plugins: list[MetaPlugin] = field(default_factory=list)
    operations: list[MetaOperation] = field(default_factory=list)
    source_file: str | None = None  # 来源 dym 路径（相对/绝对，便于追溯）
    warnings: list[str] = field(default_factory=list)  # 解析过程中的存疑提示
    convert: "ConvertInfo | None" = None  # 仅 form_type=='convert' 时有值（单据上下游关系）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "key": self.key,
            "name": self.name,
            "model_type": self.model_type,
            "form_type": self.form_type,
            "isv": self.isv,
            "app_key": self.app_key,
            "inherit_path": self.inherit_path,
            "source_file": self.source_file,
            "entities": [e.to_dict() for e in self.entities],
            "fields": [f.to_dict() for f in self.fields],
            "operations": [o.to_dict() for o in self.operations],
            "plugins": [p.to_dict() for p in self.plugins],
            "warnings": self.warnings,
        }
        if self.convert is not None:
            d["convert"] = self.convert.to_dict()
        return d
