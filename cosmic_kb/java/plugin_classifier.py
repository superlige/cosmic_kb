"""阶段 5 · 插件种类判定。

把一个项目插件类归到苍穹插件**种类**（form/list/op/convert/writeback/workflow），决定
适用哪套事件方法表（`event_extractor`）以及落库相位模型。

两路证据，互相印证（处处置信度）：
    1. **元数据注册的 plugin_type**（最可靠）：桥接已知插件以何种身份注册（form/list/op/
       writeback/convert）——平台按注册位置决定它跑哪类事件，这是第一依据。
    2. **源码父类**（佐证 / 兜底）：继承的苍穹插件基类（沿用桥接 `_COSMIC_PLUGIN_BASES`
       的传递闭包结果）。plugin_type 缺失或为 unknown 时据父类推断。
"""

from __future__ import annotations

# 苍穹插件基类简单名 → 种类。
_BASE_TO_KIND: dict[str, str] = {
    "AbstractFormPlugin": "form", "AbstractMobFormPlugin": "form",
    "AbstractBillPlugIn": "form", "AbstractMobBillPlugIn": "form",
    "AbstractBasePlugIn": "form",
    "AbstractListPlugin": "list", "AbstractTreeListPlugin": "list",
    "StandardTreeListPlugin": "list", "AbstractMobListPlugin": "list",
    "AbstractOperationServicePlugIn": "op",
    "AbstractConvertPlugIn": "convert",
    "AbstractWriteBackPlugIn": "writeback",
    "BatchImportPlugin": "import",
    "AbstractBillWebApiPlugin": "webapi", "IBillWebApiPlugin": "webapi",
    "AbstractWebApiPlugin": "webapi",
    "AbstractTask": "task",
    "AbstractPrintServicePlugin": "print", "AbstractPrintPlugin": "print",
    "IWorkflowPlugin": "workflow",
}

# 元数据 plugin_type → 种类（多数同名，convert/writeback/list/op 直通）。
_PTYPE_TO_KIND: dict[str, str] = {
    "form": "form", "list": "list", "op": "op",
    "writeback": "writeback", "convert": "convert",
}


def plugin_kind(plugin_type: str | None, plugin_base: str | None) -> tuple[str, float, str]:
    """判定插件种类。返回 (kind, confidence, evidence)。

    kind 为 'unknown' 时调用方退到「按所有事件名宽松匹配」。
    """
    if plugin_type and plugin_type in _PTYPE_TO_KIND:
        kind = _PTYPE_TO_KIND[plugin_type]
        if plugin_base and _BASE_TO_KIND.get(plugin_base) not in (None, kind):
            # 注册身份与父类不一致：以注册身份为准，但降一点置信度并留证据。
            return kind, 0.8, f"元数据注册为 {plugin_type}（父类 {plugin_base} 不完全一致）"
        return kind, 0.95, f"元数据注册为 {plugin_type}"
    if plugin_base and plugin_base in _BASE_TO_KIND:
        return _BASE_TO_KIND[plugin_base], 0.7, f"按父类 {plugin_base} 推断"
    return "unknown", 0.0, "插件种类未知（无注册类型、父类未命中苍穹基类）"
