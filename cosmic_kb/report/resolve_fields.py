"""字段名核对 · 标识 → 真实元数据中文名+坐标（防命名惯例臆断）。

起因：段二大模型读 Java 源码时靠**命名惯例猜字段中文名**翻车（`cqkd_zjjnqk` 被猜成
"资金缴纳情况"，真实是"租金缴纳情况"）。现有能查字段名的工具全是"重"的（`trace`/`bill`/`ask`，
payload 大、语义是"谁改了它"而非"它叫什么"），模型不会为确认一个中文名去调，于是走阻力最小的
路：猜。本模块补一个 O(1)、专做"标识 → 真实中文名"的轻量取证：批量传 key，直接打词典层，
回最小包，**钉不出回 `None`（诚实留白，不臆造）**。

返回形状：`{"resolved": {key: [item, ...] | None, ...}}`。同一 key 可能同时命中 `field` 表
（字段定义）与 `entity` 表（分录容器），故每个 key 回扁平 list，每个 item 自带 `kind` 判别：
  · 字段命中  —— `{kind:"field", name, form_key, entity_key, level, field_kind}`
                （`field_kind` = field 表的 kind 列：entity/dynamic/basedata_prop/...）
  · 容器命中  —— `{kind:"entry"|"subentry"|"header", name, form_key, level, parent_key}`
                （`kind` 取 entity 表的 level，让模型识别"这是分录容器 key 不是字段 key"）

设计纪律（对齐红线）：
- **复用词典层**：打现成 `Lexicon`（field + entity 同口径），不新造解析。
- **同 key 跨多坐标全摆出、不替选**：分录字段常一个 key 在多分录各有定义、名字还可能不同，
  工具诚实返回 list，消歧靠模型读代码时的实体上下文（红线·处处 unknown）。
- **纯读 `field`/`entity` 表**，零 schema 改动，不碰代码访问侧 `field_access`（那是 trace 本职）。

延续 report 包约定：dict 在前（供 --json / MCP），`render_*` 文本在后。
"""

from __future__ import annotations

from typing import Any

from ..semantic.dictionary import build_lexicon

# 层级 → 中文（与 semantic/dictionary.py:Candidate.label 同一套映射，保持文案一致）。
_LEVEL_CN = {"header": "表头", "entry": "分录", "subentry": "子分录", "basedata": "基础资料"}

# 分录容器取值语义（与字段侧 _access_hint 形成对照，强化"容器 vs 多选基础资料"二选一判别）。
_ENTRY_ACCESS = "分录容器——getDynamicObjectCollection() 取的是分录行集合（逐行 get(i)）"


def _access_hint(field_type: str | None) -> str | None:
    """字段 XML 标签名 → getDynamicObject(Collection) 的取值语义（中文）。判不出回 None（不臆造）。

    起因：模型见 `getDynamicObjectCollection(key)` 默认当"分录"，但多选基础资料字段
    （MulBasedataField）也用它取选中的基础资料集合——取分录还是基础资料，取决于 key 是什么。
    `field_type` 是精确信号：含 Basedata 即基础资料类，Mul 前缀即多选（取集合）。标量字段
    （Text/Amount/Combo…）本就不走 getDynamicObject*，不强加语义。
    """
    ft = field_type or ""
    if "Basedata" not in ft and "BaseData" not in ft:
        return None
    if ft.startswith("Mul"):
        return "多选基础资料字段——getDynamicObjectCollection() 取的是选中的基础资料对象集合，不是分录行"
    return "基础资料字段——getDynamicObject() 取关联的基础资料对象，不是分录"


def resolve_fields(conn, keys: list[str]) -> dict[str, Any]:
    """字段/分录容器标识 → 真实元数据中文名+实体坐标。钉不出回 None（不臆造）。"""
    lex = build_lexicon(conn)
    resolved: dict[str, list[dict[str, Any]] | None] = {}
    for key in keys:
        items: list[dict[str, Any]] = []
        for f in lex.fields_by_key(key):
            items.append({
                "kind": "field",
                "name": f.name,
                "form_key": f.form_key,
                "entity_key": f.entity_key,
                "level": f.level,
                "field_kind": f.kind,
                "field_type": f.field_type,     # XML 标签名：判 getDynamicObjectCollection 取值语义的精确信号
                "access": _access_hint(f.field_type),  # 派生取值语义（基础资料 vs None），堵"凭 API 名当分录"
            })
        for e in lex.entities_by_key(key):
            items.append({
                "kind": e.level or "entry",  # entry/subentry/header：让模型识别这是容器不是字段
                "name": e.name,
                "form_key": e.form_key,
                "level": e.level,
                "parent_key": e.parent_key,
                "access": _ENTRY_ACCESS,
            })
        resolved[key] = items or None
    return {"resolved": resolved}


def render_resolve_fields(data: dict[str, Any], *, max_list: int = 20) -> str:
    """文本视图：逐 key 一段；命中列坐标，钉不出明确打印 null（标 unknown，勿猜）。"""
    resolved = data.get("resolved", {})
    if not resolved:
        return "（未传入任何字段标识）"
    lines: list[str] = []
    for key, items in resolved.items():
        if not items:
            lines.append(f"{key}: null（钉不出，标 unknown，勿猜）")
            continue
        lines.append(f"{key}:")
        for it in items[:max_list]:
            name = it.get("name") or ""
            form = it.get("form_key") or "?"
            lvl_cn = _LEVEL_CN.get(it.get("level") or "", it.get("level") or "?")
            access = f"  〔{it['access']}〕" if it.get("access") else ""
            if it.get("kind") == "field":
                ft = f" · {it['field_type']}" if it.get("field_type") else (
                    f" · {it['field_kind']}" if it.get("field_kind") else "")
                lines.append(f"  · 字段 {key}「{name}」 — {form} · {lvl_cn}{ft}{access}")
            else:
                parent = it.get("parent_key")
                phint = f" ← {parent}" if parent else ""
                lines.append(f"  · 容器 {key}「{name}」 — {form} · {lvl_cn}{phint}{access}")
        if len(items) > max_list:
            lines.append(f"  …（共 {len(items)} 条坐标，全部见 --json）")
    return "\n".join(lines)
