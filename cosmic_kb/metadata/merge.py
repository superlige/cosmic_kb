"""原厂（vendor）元数据 × 本地扩展元数据合并（纯函数、零 IO）。

用户拍板（2026-07-02）：
    ① 原厂**字段 + 操作**并入 KB；原厂**插件**（无源码，对 KB 无意义）一律不要。
    ② 合并后以**原厂 key**为主，本地扩展元数据合并进它名下。

`dym_parser._parse_fields` 在解析时就把每个字段的 `entity_key` 烘焙成"所属实体的 key"；
对表单顶层字段而言这就等于该模型自己的表头实体 key。两个模型的表头实体各有各的 key
（原厂 `bd_customer` / 扩展 `cqkd_bd_customer_ext`），合并时必须把扩展表头字段的
`entity_key`（以及分录的 `parent_id`）**重写**指到原厂表头，否则字段挂错实体、按原厂
key 查询照样看不到扩展内容。
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import MetaEntity, MetaModel


def strip_vendor_plugins(vendor: "MetaModel") -> "MetaModel":
    """清空原厂模型里"无源码、追不到执行体"的插件（用户拍板 2026-07-02）。

    唯一例外：`enabled is False`（元数据里明确 `<Enabled>false</Enabled>`）的原厂插件
    予以保留——"这个钩子当前不会执行"是元数据本身给出的确定性信号，与有没有源码无关；
    真实翻车案例：`kd.cf.lgc.ht.opplugin.AdjustAmountOpplugin` 在某单据原厂层被禁用，
    旧逻辑整条丢弃后，KB 里若还留有该类名的其它绑定（如扩展继承下来的同名条目、
    未携带 Enabled 标签）会被误报成 `enabled=null`（未知），而不是"已确认禁用"。
    """
    keep = [p for p in vendor.plugins if p.enabled is False]
    if len(keep) == len(vendor.plugins):
        return vendor
    return replace(vendor, plugins=keep)


def _header_entity(model: "MetaModel") -> "MetaEntity | None":
    return next((e for e in model.entities if e.level == "header"), None)


def merge_vendor_extension(vendor: "MetaModel", extensions: list["MetaModel"]) -> "MetaModel":
    """以原厂 key 为主 key，把 0..N 个本地扩展模型合并进它名下。

    不改 vendor/extensions 入参本身（dataclasses.replace 返回新对象）。合并结果：
        key/form_type/model_type/isv/app_key 取 vendor 侧（原厂权威）
        entities  = vendor.entities + 扩展实体（去掉扩展自己的表头行，分录重新挂到原厂表头）
        fields    = vendor.fields + 扩展字段（entity_key 指向扩展表头的重写成原厂表头 key）
        operations = vendor.operations + 全部扩展的 operations（原厂标准操作一并保留语义）
        plugins   = vendor 侧（通常已被 `strip_vendor_plugins` 收窄到仅剩 enabled=False 的
                    确认禁用条目，其余无源码插件已丢弃）+ 全部扩展 plugins
        is_extension=False, extends=None（它现在是"最终权威内容"，不是别名）
    """
    vendor_header = _header_entity(vendor)
    entities = list(vendor.entities)
    fields = list(vendor.fields)
    operations = list(vendor.operations)
    plugins: list = list(vendor.plugins)
    warnings = list(vendor.warnings)
    seen_field_keys = {(f.entity_key, f.key) for f in fields if f.key}
    sources = [vendor.source_file] if vendor.source_file else []

    for ext in extensions:
        ext_header = _header_entity(ext)

        for e in ext.entities:
            if ext_header is not None and e is ext_header:
                continue  # 表头就是同一行（原厂那份权威），不重复落一条
            if ext_header is not None and vendor_header is not None and e.parent_id == ext_header.id:
                entities.append(replace(e, parent_id=vendor_header.id))
            else:
                entities.append(e)

        for f in ext.fields:
            entity_key = f.entity_key
            if ext_header is not None and vendor_header is not None and f.entity_key == ext_header.key:
                entity_key = vendor_header.key
            dedup_key = (entity_key, f.key)
            if f.key and dedup_key in seen_field_keys:
                # 正常不该发生（原厂/扩展字段命名空间天然不重叠，用户已确认）；
                # 真出现时保留扩展侧（本地人工维护、更具体），丢弃原厂侧同名条目，不静默。
                warnings.append(
                    f"字段 key 冲突：{f.key}（实体 {entity_key}）原厂与扩展 {ext.key!r} 都有定义，"
                    "保留扩展侧、丢弃原厂侧同名字段"
                )
                fields = [x for x in fields if not (x.entity_key == entity_key and x.key == f.key)]
            fields.append(replace(f, entity_key=entity_key) if entity_key != f.entity_key else f)
            if f.key:
                seen_field_keys.add(dedup_key)

        operations.extend(ext.operations)
        plugins.extend(ext.plugins)
        warnings.extend(ext.warnings)
        if ext.source_file:
            sources.append(ext.source_file)

    return replace(
        vendor,
        entities=entities,
        fields=fields,
        operations=operations,
        plugins=plugins,
        warnings=warnings,
        source_file=" + ".join(sources) if sources else vendor.source_file,
        is_extension=False,
        extends=None,
    )


def build_extension_alias(extension: "MetaModel", vendor_key: str) -> "MetaModel":
    """扩展原 key 留一条空壳表单行：内容全清空，只留 is_extension/extends，
    供 trace/bill 在查询命中它时给出"已并入原厂 key"的重定向提示。"""
    return replace(
        extension,
        entities=[],
        fields=[],
        plugins=[],
        operations=[],
        is_extension=True,
        extends=vendor_key,
    )
