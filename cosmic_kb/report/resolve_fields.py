"""字段名核对 · 标识 → 真实元数据中文名+坐标（防命名惯例臆断）。

起因：段二大模型读 Java 源码时靠**命名惯例猜字段中文名**翻车（`cqkd_zjjnqk` 被猜成
"资金缴纳情况"，真实是"租金缴纳情况"）。现有能查字段名的工具全是"重"的（`trace`/`bill`/`ask`，
payload 大、语义是"谁改了它"而非"它叫什么"），模型不会为确认一个中文名去调，于是走阻力最小的
路：猜。本模块补一个 O(1)、专做"标识 → 真实中文名"的轻量取证：批量传 key，直接打词典层，
回最小包，**钉不出回 `None`（诚实留白，不臆造）**。

返回形状：`{"resolved": {key: [item, ...] | None, ...}, "mismatched_form": {key: {...}, ...}}`。
同一 key 可能同时命中 `field` 表（字段定义）、`entity` 表（分录容器）、`form` 表（单据本身），
故每个 key 回扁平 list，每个 item 自带 `kind` 判别：
  · 字段命中  —— `{kind:"field", name, form_key, entity_key, level, field_kind}`
                （`field_kind` = field 表的 kind 列：entity/dynamic/basedata_prop/...）
  · 容器命中  —— `{kind:"entry"|"subentry"|"header", name, form_key, level, parent_key}`
                （`kind` 取 entity 表的 level，让模型识别"这是分录容器 key 不是字段 key"；
                覆盖表头/分录/子分录三档，与字段侧对称）
  · 单据命中  —— `{kind:"form", name, form_key, form_type}`
                （`form` 表本身；模型读到 `.load("cqkd_invoic_apply", ...)` 这类单据标识
                时同样要核实，不能因为它不是"字段"就绕过——2026-07-05 复盘：真实排障中模型
                对这类标识无工具可查，只能凭字面翻译，此处补上）

**实体限定精确匹配**（2026-07-05）：起因是 `read_source` 的"工具自动消歧"复杂度收益比不划算——
真实库量化（`docs/read_source字段名解析逻辑.md` §5）显示结构性漏判修复收益 <1.1% 且有误判风险。
改为反过来：模型自己读源码能看到 `.load("cqkd_zkd", ...)` 这类实体字面量，直接把 key 写成复合
限定符传入，工具过滤候选做精确匹配，不必再靠文件级数据流去猜。

**复合限定符语法**（2026-07-05 复盘扩展）：与 `field_trace.parse_locator` 同一套点号坐标惯例
（`单据.字段`/`分录.字段`/`单据.分录.字段`），不再只认 `单据.字段` 两段式——真实排障发现模型
习惯照搬 `trace` 的三段式写法（如 `"cqkd_invoic_apply.cqkd_invoiceentry.cqkd_invoiceid"`）或
直接传分录限定（如 `"cqkd_invoiceentry.cqkd_invoiceid"`），老逻辑只认两段式且限定符必须命中
`form` 表，两种写法全部落空返回 `null`，模型误以为字段确实没登记，其实是工具语法太窄。判定逻辑
见 `_split_qualified`；过滤后有候选就返回过滤结果；过滤后为空但全局候选非空，说明模型给的
单据/分录假设是错的，不能悄悄回退掩盖这个信号——`resolved[key]` 仍给全局候选，同时在
`mismatched_form[key]` 里诚实提示真实归属（`given_form`/`available_forms`、
`given_entry`/`available_entities`，视限定符类型出现）。

设计纪律（对齐红线）：
- **复用词典层**：打现成 `Lexicon`（field + entity 同口径），不新造解析。
- **同 key 跨多坐标全摆出、不替选**：分录字段常一个 key 在多分录各有定义、名字还可能不同，
  工具诚实返回 list，消歧靠模型读代码时的实体上下文（红线·处处 unknown）。
- **纯读 `field`/`entity`/`form` 表**，零 schema 改动，不碰代码访问侧 `field_access`（那是 trace 本职）。

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


def _split_qualified(key: str, lex) -> tuple[str | None, str | None, str] | None:
    """复合 key → `(form_key, entry_key, field_key)`；与 `field_trace.parse_locator` 同一套
    点号坐标惯例（`单据.字段`/`分录.字段`/`单据.分录.字段`/`单据.分录.子分录.字段`），模型已经
    在用 `trace` 的这套写法，`resolve_fields` 不该另立一套只认 `单据.字段` 两段式——2026-07-05
    真实排障：模型按三段式传 `"单据.分录.字段"`/两段式传 `"分录.字段"`，因为老逻辑只认两段式且
    限定符必须命中 `form` 表，两种写法全部落空返回 null。

    两段：前段命中 `form` 表按单据限定；否则命中 `entity` 表按分录/子分录限定；两处都不命中就
    不当限定符（防误切普通含点标识），返回 None 走裸 key 查询。
    三段及以上：首段=单据，倒数第二段=分录/子分录（多段时中间段仅供阅读，不参与过滤，与
    `parse_locator` 的"中段=父分录"同一取舍）；首段须命中 `form` 表才当限定符处理，否则整串
    按裸 key 查（不臆断哪段是单据）。
    """
    parts = [p for p in key.split(".") if p]
    if len(parts) < 2:
        return None
    field_key = parts[-1]
    if not field_key:
        return None
    if len(parts) == 2:
        qualifier = parts[0]
        if lex.form_by_key(qualifier) is not None:
            return qualifier, None, field_key
        if lex.entities_by_key(qualifier):
            return None, qualifier, field_key
        return None
    form_key, entry_key = parts[0], parts[-2]
    if lex.form_by_key(form_key) is None:
        return None
    return form_key, entry_key, field_key


def _items_for(key: str, lex) -> list[dict[str, Any]]:
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
    form = lex.form_by_key(key)
    if form is not None:
        items.append({
            "kind": "form",
            "name": form.name,
            "form_key": form.key,
            "form_type": form.form_type,
        })
    return items


def resolve_fields(conn, keys: list[str]) -> dict[str, Any]:
    """字段/分录容器/单据标识 → 真实元数据中文名+实体坐标。钉不出回 None（不臆造）。

    `key` 支持复合限定符——`"单据.字段"`/`"分录.字段"`/`"单据.分录.字段"`（与 `trace` 的点号
    坐标同一套惯例，模型自己从源码字面量读出单据/分录 key 时用）；限定符不匹配任何全局候选时，
    `mismatched_form[key]` 会诚实提示真实归属，不悄悄回退。
    """
    lex = build_lexicon(conn)
    resolved: dict[str, list[dict[str, Any]] | None] = {}
    mismatched: dict[str, dict[str, Any]] = {}
    for key in keys:
        qualified = _split_qualified(key, lex)
        if qualified is None:
            resolved[key] = _items_for(key, lex) or None
            continue
        form_key, entry_key, field_key = qualified
        items = _items_for(field_key, lex)

        def _matches(it: dict[str, Any]) -> bool:
            if form_key is not None and it.get("form_key") != form_key:
                return False
            if entry_key is not None and it.get("entity_key") != entry_key:
                return False
            return True

        filtered = [it for it in items if _matches(it)]
        if filtered:
            resolved[key] = filtered
        elif items:
            resolved[key] = items
            mm: dict[str, Any] = {"field_key": field_key}
            if form_key is not None:
                mm["given_form"] = form_key
                mm["available_forms"] = sorted(
                    {it["form_key"] for it in items if it.get("form_key")})
            if entry_key is not None:
                mm["given_entry"] = entry_key
                mm["available_entities"] = sorted(
                    {it["entity_key"] for it in items if it.get("entity_key")})
            mismatched[key] = mm
        else:
            resolved[key] = None
    out: dict[str, Any] = {"resolved": resolved}
    if mismatched:
        out["mismatched_form"] = mismatched
    return out


def render_resolve_fields(data: dict[str, Any], *, max_list: int = 20) -> str:
    """文本视图：逐 key 一段；命中列坐标，钉不出明确打印 null（标 unknown，勿猜）。"""
    resolved = data.get("resolved", {})
    mismatched = data.get("mismatched_form", {})
    if not resolved:
        return "（未传入任何字段标识）"
    lines: list[str] = []
    for key, items in resolved.items():
        if not items:
            lines.append(f"{key}: null（钉不出，标 unknown，勿猜）")
            continue
        mm = mismatched.get(key)
        if mm:
            given = []
            avail = []
            if "given_form" in mm:
                given.append(f"单据「{mm['given_form']}」")
                avail.append(f"单据: {', '.join(mm['available_forms'])}")
            if "given_entry" in mm:
                given.append(f"分录「{mm['given_entry']}」")
                avail.append(f"分录: {', '.join(mm['available_entities'])}")
            lines.append(f"{key}: ⚠ 限定的{'+'.join(given)}下未找到该字段，"
                         f"它实际出现在 {'；'.join(avail)}（以下列全部候选）")
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
            elif it.get("kind") == "form":
                ftype = f" [{it['form_type']}]" if it.get("form_type") else ""
                lines.append(f"  · 单据 {key}「{name}」{ftype}")
            else:
                parent = it.get("parent_key")
                phint = f" ← {parent}" if parent else ""
                lines.append(f"  · 容器 {key}「{name}」 — {form} · {lvl_cn}{phint}{access}")
        if len(items) > max_list:
            lines.append(f"  …（共 {len(items)} 条坐标，全部见 --json）")
    return "\n".join(lines)
