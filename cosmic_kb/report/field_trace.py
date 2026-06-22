"""阶段 5+6+7 旗舰 · 字段排障追踪（输入字段 → 谁改了它、在哪个事件函数、是否落库）。

这是工具的核心价值（用户 2026-06-17）：消灭"把元数据里一堆插件全路径逐个复制到代码里翻"。

**按实体坐标精确定位**（用户 2026-06-17 验收反馈）：同一字段标识可能出现在不同单据、
不同层级（表头/分录/子分录）、不同分录里。只按裸标识列「所有插件」毫无价值。故本报告：
  1. 先列字段在元数据里的**定义坐标**（单据·层级·分录）——这是消歧菜单；
  2. 把读写记录**按坐标 (单据, 层级, 分录) 分组**，每组各自列插件/事件/落库；
  3. 支持 form/entry/level 过滤，缩到用户真正想看的那个实体。

延续 report 包约定：dict 在前（供 --json / Web），`render_*` 文本在后（给人看）。
"""

from __future__ import annotations

import json
from typing import Any

from ..graph import store

# 排序权重：写优先于读；落库优先；置信度高优先。
_PERSIST_RANK = {"yes": 0, "unknown": 1, "no": 2, "na": 3}
_ACCESS_RANK = {"write": 0, "read": 1}
_LEVEL_LABEL = {"header": "表头", "entry": "分录", "subentry": "子分录",
                "basedata": "基础资料", "unknown": "未知层级"}


def field_trace(
    conn, field_key: str, *,
    form_key: str | None = None, entry_key: str | None = None, level: str | None = None,
) -> dict[str, Any]:
    """追踪一个字段的全部插件读写 + 落库判定，按实体坐标分组。"""
    java = json.loads(store.get_meta(conn, "java_analysis") or "{}")

    form_names = {r["key"]: r["name"] for r in conn.execute("SELECT key,name FROM form")}
    entity_names = {
        (r["form_key"], r["key"]): r["name"]
        for r in conn.execute("SELECT form_key,key,name FROM entity")
    }

    # 字段在元数据中的定义坐标（可能跨表单/层级）——消歧菜单。
    occurrences = [
        {
            "form_key": r["form_key"], "form_name": form_names.get(r["form_key"]),
            "field_name": r["name"], "level": r["level"],
            "entity_key": r["entity_key"],
            "entity_name": entity_names.get((r["form_key"], r["entity_key"])),
            "kind": r["kind"],
        }
        for r in conn.execute(
            "SELECT form_key,name,level,entity_key,kind FROM field WHERE key=? "
            "ORDER BY form_key,level", (field_key,),
        ).fetchall()
    ]

    # 取该字段的全部读写记录（不在 SQL 里按 level/entry 过滤——精确模式要在 Python 里分桶，
    # 把"层级/分录判不准"的写入归到「可能命中」而非直接丢弃，满足"不能遗漏"）。
    all_rows = [dict(r) for r in conn.execute(
        "SELECT form_key,field_key,level,entry_key,plugin_fqn,plugin_type,access_class,"
        "event_method,event_phase,access,persists,persist_reason,via,line,path,"
        "key_resolution,confidence,source_relpath,evidence FROM field_access WHERE field_key=?",
        (field_key,)).fetchall()]
    # 插件所属单据：plugin_fqn 注册在哪张/哪些单据上（plugin 表为主，binding 表回落）。
    # 这就是「被另外的哪个元数据的什么插件改了」——字段在单据 A，却可能被注册在单据 B 的插件改。
    plugin_home = _plugin_home_map(conn, {r["plugin_fqn"] for r in all_rows}, form_names)
    for r in all_rows:
        r["path"] = json.loads(r["path"]) if r["path"] else []
        r["plugin_simple"] = (r["plugin_fqn"] or "").rsplit(".", 1)[-1]
        r["access_simple"] = (r["access_class"] or "").rsplit(".", 1)[-1]
        r["cross_class"] = bool(r["access_class"]) and r["access_class"] != r["plugin_fqn"]
        homes = plugin_home.get(r["plugin_fqn"], [])
        r["plugin_forms"] = homes
        r["plugin_form_label"] = _home_label(homes)
        # 跨单据修改：插件所属单据里有任意一个不等于本记录的来源单据（form_key）。
        r["plugin_cross_form"] = bool(homes) and r["form_key"] is not None and \
            all(h["form_key"] != r["form_key"] for h in homes)
    all_rows.sort(key=_row_sort_key)

    # ── 分桶：精确命中 / 可能命中(存疑) / 未定位单据 ──────────────────────
    precise = level is not None

    def _is_exact(r: dict[str, Any]) -> bool:
        if form_key and r["form_key"] != form_key:
            return False
        if level and r["level"] != level:
            return False
        if entry_key and r["entry_key"] != entry_key:
            return False
        return True

    if precise or form_key:
        rows = [r for r in all_rows if _is_exact(r)]
        # 可能命中：同单据同字段、但层级/分录不精确匹配（含层级判不准的）。
        possible = [r for r in all_rows if r["form_key"] == form_key and not _is_exact(r)] \
            if form_key else []
        unlocated = [r for r in all_rows if r["form_key"] is None]
    else:
        rows, possible, unlocated = all_rows, [], []

    # ── 按实体坐标 (form_key, level, entry_key) 分组（精确桶）────────────────
    groups: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        ck = (r["form_key"], r["level"], r["entry_key"])
        g = groups.get(ck)
        if g is None:
            g = groups[ck] = {
                "form_key": r["form_key"], "form_name": form_names.get(r["form_key"]),
                "level": r["level"], "entry_key": r["entry_key"],
                "entry_name": entity_names.get((r["form_key"], r["entry_key"])),
                "label": _coord_label(r["form_key"], form_names.get(r["form_key"]),
                                      r["level"], r["entry_key"],
                                      entity_names.get((r["form_key"], r["entry_key"]))),
                "writers": [], "readers": [],
            }
        (g["writers"] if r["access"] == "write" else g["readers"]).append(r)

    group_list = sorted(groups.values(), key=lambda g: (
        0 if g["form_key"] else 1,                       # 已定位单据的在前
        -len(g["writers"]), g["form_key"] or "", g["level"] or "",
    ))
    # 转换上下游缓存（按单据查 convert_rule，组间复用）：本实体在 BOTP 链上的来龙去脉。
    convert_cache: dict[str | None, dict[str, list]] = {}
    for g in group_list:
        g["summary"] = {
            "writers": len(g["writers"]), "readers": len(g["readers"]),
            "persisting": sum(1 for r in g["writers"] if r["persists"] == "yes"),
            "uncertain": sum(1 for r in g["writers"] if r["persists"] == "unknown"),
            "plugins": len({r["plugin_fqn"] for r in g["writers"] + g["readers"]}),
        }
        g["convert_context"] = _convert_context(conn, g["form_key"], form_names, convert_cache)

    writers = [r for r in rows if r["access"] == "write"]
    readers = [r for r in rows if r["access"] == "read"]
    summary = {
        "writers": len(writers), "readers": len(readers),
        "persisting_writers": sum(1 for r in writers if r["persists"] == "yes"),
        "uncertain_writers": sum(1 for r in writers if r["persists"] == "unknown"),
        "plugins": len({r["plugin_fqn"] for r in rows}),
        "forms": len({r["form_key"] for r in rows}),
        "coords": len(group_list),
        "possible": len(possible),
        "unlocated": len(unlocated),
    }
    # ── 粗精度扫描命中（coarse_field_hit 词法扫描）——只留「仅粗扫见」疑似盲点 ──
    # 高精度命中已在上方 writers/readers 分组完整呈现，再列「高精度也记」是冗余，故剔除；
    # 锚点用 (relpath, line)：与 coarse_field_hit.relpath、field_access.source_relpath 同为「相对
    # 源码根」路径，可直接比对。再剔除常量类（orphan_role='constant'）里的命中——那只是常量
    # 定义（如 `static final String X = "cqkd_x"`），不是真实读写，对排障无意义。
    high_locs = {(r["source_relpath"], r["line"]) for r in all_rows}
    const_relpaths = {
        r["relpath"] for r in conn.execute(
            "SELECT relpath FROM source_class WHERE orphan_role='constant' AND relpath IS NOT NULL")
    }
    all_coarse = [
        {"relpath": r["relpath"], "line": r["line"], "via": r["via"],
         "idiom": r["via"] in ("rw-idiom", "const-rw-idiom"),
         "in_high": (r["relpath"], r["line"]) in high_locs}
        for r in conn.execute(
            "SELECT relpath,line,via FROM coarse_field_hit WHERE field_key=? "
            "ORDER BY CASE WHEN via IN ('rw-idiom','const-rw-idiom') THEN 0 ELSE 1 END, relpath, line",
            (field_key,),
        ).fetchall()
    ]
    coarse_only = [c for c in all_coarse if not c["in_high"]]            # 去掉「高精度也记」
    shown = [c for c in coarse_only if c["relpath"] not in const_relpaths]  # 再剔除常量类
    coarse = {
        "coarse_only": len(shown),                                      # 仅粗扫见、非常量类（实际展示数）
        "idiom": sum(1 for c in shown if c["idiom"]),                   # 其中强信号读写习语
        "const_excluded": sum(1 for c in coarse_only if c["relpath"] in const_relpaths),  # 落常量类、已剔除
        "high_rows": len(all_rows),                                     # 高精度该字段命中条数（参照）
        "locations": shown,
    }

    note = None
    if not java.get("available", True):
        note = "⚠ tree-sitter 未启用（pip install -e .[parse]），字段级分析为空。"
    elif not all_rows:
        note = ("未找到任何插件读写该字段（可能：字段名有误 / 只被平台处理 / 源码未给全 / "
                "经常量引用未解析）。")
    elif precise and not rows and possible:
        note = "该精确坐标无确定命中，但本单据该字段有「可能命中（层级/分录存疑）」记录，见下。"
    elif precise and not rows and not possible and unlocated:
        note = "该单据无确定命中；下方「未定位单据」列出了来源判不出、但确实读写该字段的插件，供人工核对。"
    elif not precise and len(occurrences) > 1 and not form_key:
        note = (f"该字段在 {len(occurrences)} 处实体里都有定义。下面已按「单据·层级·分录」分组；"
                f"用 元数据.[分录.[子分录.]]字段 点号格式 或点定义坐标可精确定位到某层级。")

    return {
        "field_key": field_key,
        "filter": {"form_key": form_key, "entry_key": entry_key, "level": level},
        "precise": precise,
        "occurrences": occurrences,
        "groups": group_list,
        "possible": possible,          # 可能命中（同单据同字段，层级/分录存疑）
        "unlocated": unlocated,        # 来源未定位（form_key 为空）
        "writers": writers,            # 扁平（精确桶内），向后兼容
        "readers": readers,
        "summary": summary,
        "coarse": coarse,              # 粗精度扫描命中 + 逐处互证（与高精度并列）
        "java_available": java.get("available", True),
        "note": note,
    }


def parse_locator(text: str) -> tuple[str, str | None, str | None, str | None]:
    """把层级显式的点号查询解析成 (field_key, form_key, entry_key, level)，纯按段数判定：

      字段              → (field, None, None, None)   裸字段=发现态，列全部坐标
      单据.字段          → (field, 单据, None, "header")
      单据.分录.字段      → (field, 单据, 分录, "entry")
      单据.分录.子分录.字段 → (field, 单据, 子分录, "subentry")   （中段=父分录，仅供阅读）
    """
    parts = [p for p in text.strip().split(".") if p]
    if len(parts) >= 4:
        return parts[-1], parts[0], parts[-2], "subentry"
    if len(parts) == 3:
        return parts[2], parts[0], parts[1], "entry"
    if len(parts) == 2:
        return parts[1], parts[0], None, "header"
    return (parts[0] if parts else text.strip()), None, None, None


def _plugin_home_map(
    conn, fqns: set[str | None], form_names: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """plugin_fqn → 它注册所属的单据列表 [{form_key, form_name}]（去重、按 key 排序）。

    以 `plugin` 表为主（含 form/list/op/convert 各类注册）；该表查不到的回落 `binding` 表。
    查不到任何归属（service / 未注册的项目类）→ 不在返回 dict 里，调用方按"无归属"处理。
    """
    keys = sorted(f for f in fqns if f)
    if not keys:
        return {}
    ph = ",".join("?" * len(keys))
    home: dict[str, set[str]] = {}
    for r in conn.execute(
        f"SELECT DISTINCT class_name, form_key FROM plugin WHERE class_name IN ({ph})", keys,
    ).fetchall():
        if r["form_key"]:
            home.setdefault(r["class_name"], set()).add(r["form_key"])
    # binding 表回落（plugin 表没记到的桥接绑定）。
    for r in conn.execute(
        f"SELECT DISTINCT class_name, form_key FROM binding WHERE class_name IN ({ph})", keys,
    ).fetchall():
        if r["form_key"]:
            home.setdefault(r["class_name"], set()).add(r["form_key"])
    return {
        fqn: [{"form_key": fk, "form_name": form_names.get(fk)} for fk in sorted(fks)]
        for fqn, fks in home.items()
    }


def _home_label(homes: list[dict[str, Any]]) -> str | None:
    """插件所属单据的人读串；无归属返回 None（前端显示 service/未注册）。"""
    if not homes:
        return None
    return "、".join(
        (h["form_key"] or "?") + (f"「{h['form_name']}」" if h.get("form_name") else "")
        for h in homes
    )


def _convert_context(
    conn, form_key: str | None, form_names: dict[str, str],
    cache: dict[str | None, dict[str, list]],
) -> dict[str, list]:
    """本单据在转换规则(BOTP)里的上下游：upstream=作为目标单的来源单，downstream=作为源单的目标单。"""
    if form_key is None:
        return {"upstream": [], "downstream": []}
    if form_key in cache:
        return cache[form_key]
    upstream = [
        {"entity": r["source_entity"], "name": form_names.get(r["source_entity"]), "rule": r["name"]}
        for r in conn.execute(
            "SELECT DISTINCT source_entity,name FROM convert_rule WHERE target_entity=?", (form_key,),
        ).fetchall() if r["source_entity"]
    ]
    downstream = [
        {"entity": r["target_entity"], "name": form_names.get(r["target_entity"]), "rule": r["name"]}
        for r in conn.execute(
            "SELECT DISTINCT target_entity,name FROM convert_rule WHERE source_entity=?", (form_key,),
        ).fetchall() if r["target_entity"]
    ]
    ctx = {"upstream": upstream, "downstream": downstream}
    cache[form_key] = ctx
    return ctx


def _row_sort_key(r: dict[str, Any]):
    return (
        _ACCESS_RANK.get(r["access"], 9),
        _PERSIST_RANK.get(r["persists"], 9),
        -(r["confidence"] or 0),
        r["plugin_fqn"] or "",
    )


def _coord_label(form_key, form_name, level, entry_key, entry_name) -> str:
    """一个实体坐标的人读标签。"""
    if not form_key:
        return f"（未定位到具体单据）{_LEVEL_LABEL.get(level, level)}" + (
            f"·{entry_key}" if entry_key else "")
    head = f"单据 {form_key}" + (f"「{form_name}」" if form_name else "")
    lvl = _LEVEL_LABEL.get(level, level)
    if entry_key:
        return f"{head} · {lvl} {entry_key}" + (f"「{entry_name}」" if entry_name else "")
    return f"{head} · {lvl}"


_PERSIST_LABEL = {"yes": "✅落库", "no": "—内存", "unknown": "❓存疑", "na": ""}


def _fmt_access(r: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    persist = _PERSIST_LABEL.get(r["persists"], r["persists"] or "")
    res_flag = "" if r["key_resolution"] in ("literal", "constant") else f" [{r['key_resolution']}]"
    where = r["access_simple"] if r["cross_class"] else ""
    cross = f"  ↳跨类 {where}" if r["cross_class"] else ""
    home = r.get("plugin_form_label")
    home_str = (f"  «{home}»" + ("⚠跨单据" if r.get("plugin_cross_form") else "")) if home \
        else "  «service/未注册»"
    lines.append(
        f"    {r['plugin_simple']:<26} [{r['plugin_type']}]{home_str}  事件 {r['event_method']}"
        f"  {persist}{cross}"
    )
    lines.append(f"        {r['via']}  {r['source_relpath']}:{r['line']}{res_flag}")
    if len(r["path"]) > 1:
        lines.append(f"        调用链: {' → '.join(r['path'])}")
    if r["persist_reason"]:
        lines.append(f"        落库依据: {r['persist_reason']}")
    return lines


def render_field_trace(ft: dict[str, Any], *, max_list: int = 50) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    flt = ft.get("filter") or {}
    suffix = ""
    if flt.get("form_key"):
        suffix += f"  «{flt['form_key']}»"
    if flt.get("entry_key"):
        suffix += f"  分录={flt['entry_key']}"
    if flt.get("level"):
        suffix += f"  层级={flt['level']}"
    lines.append(f"字段排障追踪: {ft['field_key']}{suffix}")
    lines.append("=" * 72)

    if ft["occurrences"]:
        lines.append("【字段定义坐标】（同名字段可能跨实体；下面按坐标分组）")
        for o in ft["occurrences"]:
            ek = f"·{o['entity_key']}" if o["entity_key"] else ""
            en = f"「{o['entity_name']}」" if o.get("entity_name") else ""
            lines.append(
                f"  «{o['form_key'] or '?'}»{('「'+o['form_name']+'」') if o.get('form_name') else ''}  "
                f"{o['field_name'] or ''} [{_LEVEL_LABEL.get(o['level'], o['level'])}{ek}{en}] ({o['kind']})"
            )
    s = ft["summary"]
    lines.append("")
    lines.append(
        f"【概况】实体坐标 {s.get('coords', 0)} 个，写入 {s['writers']}"
        f"（落库 {s['persisting_writers']} / 存疑 {s['uncertain_writers']}），读取 {s['readers']}，"
        f"涉及插件 {s['plugins']} / 单据 {s['forms']}"
    )
    if ft["note"]:
        lines.append(f"  {ft['note']}")

    for g in ft["groups"]:
        gs = g["summary"]
        lines.append("")
        lines.append("─" * 72)
        lines.append(f"▼ {g['label']}")
        lines.append(
            f"  写 {gs['writers']}（落库 {gs['persisting']} / 存疑 {gs['uncertain']}）"
            f"  读 {gs['readers']}  插件 {gs['plugins']}"
        )
        cc = g.get("convert_context") or {}
        if cc.get("upstream") or cc.get("downstream"):
            up = "、".join(x["entity"] for x in cc.get("upstream", [])) or "—"
            down = "、".join(x["entity"] for x in cc.get("downstream", [])) or "—"
            lines.append(f"  转换上下游(BOTP): 上游来源单 {up} → 本单 → 下游目标单 {down}")
        if g["writers"]:
            lines.append("  【写】（落库 > 存疑 > 内存）")
            for r in g["writers"][:max_list]:
                lines.extend(_fmt_access(r))
        if g["readers"]:
            lines.append("  【读】")
            for r in g["readers"][:max_list]:
                lines.extend(_fmt_access(r))

    if not ft["groups"] and not ft.get("possible") and not ft.get("unlocated"):
        lines.append("")
        lines.append("（该坐标无插件读写记录）")

    possible = ft.get("possible") or []
    if possible:
        lines.append("")
        lines.append("─" * 72)
        lines.append(f"▼ 可能命中（本单据同字段、层级/分录存疑，前 {min(max_list, len(possible))}）")
        for r in possible[:max_list]:
            loc = f"{_LEVEL_LABEL.get(r['level'], r['level'])}" + (f"·{r['entry_key']}" if r["entry_key"] else "")
            lines.append(f"  [{loc}]")
            lines.extend(_fmt_access(r))

    unlocated = ft.get("unlocated") or []
    if unlocated:
        lines.append("")
        lines.append(f"▼ 未定位单据（来源实体判不出，但确实读写该字段，共 {len(unlocated)}）")
        for r in unlocated[:max_list]:
            lines.extend(_fmt_access(r))

    # 粗精度扫描命中（词法扫描）——只列「仅粗扫见」疑似盲点，已剔除常量类定义。
    coarse = ft.get("coarse") or {}
    if coarse.get("coarse_only"):
        excluded = coarse.get("const_excluded") or 0
        lines.append("")
        lines.append("─" * 72)
        lines.append(
            f"▼ 仅粗扫见（疑似盲点，非常量类）：{coarse['coarse_only']} 处"
            f"（强信号 {coarse['idiom']}） · 高精度记 {coarse['high_rows']} 条"
            + (f" · 已剔除常量类定义 {excluded} 处" if excluded else "")
        )
        for loc in coarse["locations"][:max_list]:
            flag = "⚡读写习语" if loc["idiom"] else "  弱引用"
            lines.append(
                f"    {flag}  {loc['relpath']}:{loc['line']} ({loc['via']})")
        extra = coarse["coarse_only"] - min(coarse["coarse_only"], max_list)
        if extra > 0:
            lines.append(f"    …另有 {extra} 处未列出")
        lines.append("    注：盲点是候选非确诊，纯文本比对有误报，请跳源码核对。")
    return "\n".join(lines)
