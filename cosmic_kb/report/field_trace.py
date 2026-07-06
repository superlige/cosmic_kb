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
from collections import Counter
from typing import Any

from ..graph import store
from ..java import null_reason as nrmod
from ..semantic import hints
from . import dynamic_writes

# 排序权重：写优先于读；落库优先；置信度高优先。
_PERSIST_RANK = {"yes": 0, "unknown": 1, "no": 2, "na": 3}
_ACCESS_RANK = {"write": 0, "read": 1}
_LEVEL_LABEL = {"header": "表头", "entry": "分录", "subentry": "子分录",
                "basedata": "基础资料", "unknown": "未知层级"}
# field_access 取数列（all_rows 与动态写入候选共用，避免列名漂移）。
# 注：刻意不取 `evidence`（原始源码片段）——本模块/worklist/_enrich_rows/_fmt_access/Web 全不渲染它，
# 是最大单行死重，取了只会撑大返回 dict 被 MCP 截断。
_FA_COLS = (
    "form_key,field_key,level,entry_key,plugin_fqn,plugin_type,access_class,"
    "event_method,event_phase,access,persists,persist_reason,via,line,path,"
    "key_resolution,confidence,source_relpath,null_reason"
)

# ── 返回 dict 的数组上界（红线 #4：cap 后仍把真实总数留在 summary，消费方比 len 即知截断量）──
# 现有 max_list 只管 render_* 文本，MCP 拿的是裸 dict 必须在此设界，否则单字段 trace 可达 271KB 被截。
# 要看某坐标的全部明细，用 form/entry/level 收窄（精确模式天然只剩一个坐标的行）。
_CAP_WRITERS = 40          # 每坐标写入（核心价值，给得宽）
_CAP_READER_METHODS = 15   # 每坐标读取折叠成的「该读方法」清单条数
_CAP_POSSIBLE = 25         # 可能命中（层级/分录存疑）
_CAP_UNLOCATED_METHODS = 15  # 来源未定位折叠成的「反推来源方法」清单条数
_CAP_COARSE = 25           # 粗扫疑似盲点位置

# 单行投影白名单：只留 Web accessTable/possibleTable + CLI _fmt_access 实际渲染的字段。
# 丢弃 confidence/event_phase/field_key/form_key(行级)/plugin_forms(与 label 冗余)/evidence。
_SLIM_FIELDS = (
    "access", "level", "entry_key", "event_method", "persists", "persist_reason",
    "via", "line", "source_relpath", "key_resolution", "plugin_fqn", "plugin_simple",
    "plugin_type", "access_simple", "cross_class", "plugin_form_label",
    "plugin_cross_form", "semantics_topic",
)


def _slim_row(r: dict[str, Any]) -> dict[str, Any]:
    """把一条 enrich 后的 field_access 行投影成「只含渲染所需字段」的精简行。

    `path`（调用链）仅在长度 >1 时保留——Web/CLI 都只在 `len(path)>1` 时才显示调用链，
    单元素 path 是绝大多数行的常态，留着纯属占字节。
    """
    out = {k: r.get(k) for k in _SLIM_FIELDS}
    path = r.get("path")
    if path and len(path) > 1:
        out["path"] = path
    return out


def _collapse_reader_methods(rows: list[dict[str, Any]], *, cap: int) -> dict[str, Any]:
    """把读取行按 (入口类, 事件方法) 去重成「该读方法」清单（cause 无关版 worklist）。

    读取价值最低、却占膨胀大头——大模型真要弄清"谁读了它"，是去这些方法读源码，而非逐行看记录。
    故同插件同事件方法只列一处，给 count + 物理位置（≤3）+ 已焊的语义路由/归属，
    按 count 降序、cap 截断并报剩余数。形状与 dynamic_writers 同款（total/methods/capped）。
    """
    groups: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        key = (r.get("plugin_fqn"), r.get("event_method"))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "class_fqn": r.get("plugin_fqn"), "method": r.get("event_method"),
                "plugin_simple": r.get("plugin_simple"), "plugin_type": r.get("plugin_type"),
                "plugin_form_label": r.get("plugin_form_label"),
                "semantics_topic": r.get("semantics_topic"),
                "count": 0, "locations": {},
            }
        g["count"] += 1
        ac = r.get("access_class") or r.get("plugin_fqn")
        g["locations"].setdefault(ac, f"{r.get('source_relpath')}:{r.get('line')}")
    out: list[dict[str, Any]] = []
    for g in groups.values():
        out.append({
            "class_fqn": g["class_fqn"], "method": g["method"],
            "plugin_simple": g["plugin_simple"], "plugin_type": g["plugin_type"],
            "plugin_form_label": g["plugin_form_label"], "semantics_topic": g["semantics_topic"],
            "count": g["count"],
            "locations": list(g["locations"].values())[:3],
        })
    out.sort(key=lambda d: (-d["count"], d["class_fqn"] or ""))
    return {"total": len(rows), "methods": out[:cap], "capped": max(0, len(out) - cap)}


def _collapse_unlocated_methods(rows: list[dict[str, Any]], *, cap: int) -> dict[str, Any]:
    """把「来源单据未钉出（form_key=None）但确实读写本字段」的行折叠成「反推来源单据」工作单。

    与 `dynamic_writers`（B 类，字段钉不出）对称：那是钉不出"改的哪个字段"，这是钉不出"改的
    哪张单据"。确定性层数据流/元数据都没追到这个 DynamicObject 的来源实体，**交段二大模型直接
    读源码反推**（红线 #1 可读全文 / #4 不臆造）。按 (入口类, 事件方法) 去重——同方法读写
    N 个本字段的位置只列一次，给写/读分计 + 物理位置（≤3）+ 该插件注册所属单据
    `plugin_form_label`（**只读线索**：很可能来自这张单据，去源码确认，绝不自动回填 form_key）+
    语义路由。写多优先、按访问数降序，超 cap 截断并报剩余。形状同 dynamic_writers（total/methods/capped）。
    """
    groups: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        key = (r.get("plugin_fqn"), r.get("event_method"))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "class_fqn": r.get("plugin_fqn"), "method": r.get("event_method"),
                "plugin_simple": r.get("plugin_simple"), "plugin_type": r.get("plugin_type"),
                "plugin_form_label": r.get("plugin_form_label"),
                "semantics_topic": r.get("semantics_topic"),
                "writes": 0, "reads": 0, "locations": {}, "reasons": Counter(),
            }
        if r.get("access") == "write":
            g["writes"] += 1
        else:
            g["reads"] += 1
        if r.get("null_reason"):
            g["reasons"][r["null_reason"]] += 1
        ac = r.get("access_class") or r.get("plugin_fqn")
        g["locations"].setdefault(ac, f"{r.get('source_relpath')}:{r.get('line')}")
    out: list[dict[str, Any]] = []
    for g in groups.values():
        # 该方法多行的主因（取最高频）+ 人读标签，提示段二「该不该读源码反推」。
        reason = g["reasons"].most_common(1)[0][0] if g["reasons"] else None
        out.append({
            "class_fqn": g["class_fqn"], "method": g["method"],
            "plugin_simple": g["plugin_simple"], "plugin_type": g["plugin_type"],
            "plugin_form_label": g["plugin_form_label"], "semantics_topic": g["semantics_topic"],
            "null_reason": reason,
            # 成因码人读标签焊进返回值本体（MCP JSON 路径此前只有裸码，模型只能猜；
            # CLI 文本渲染一直有 nrmod.REASON_LABEL，这里补齐 JSON 侧同等信息）。
            "null_reason_label": nrmod.REASON_LABEL.get(reason, reason) if reason else None,
            "writes": g["writes"], "reads": g["reads"], "count": g["writes"] + g["reads"],
            "locations": list(g["locations"].values())[:3],
        })
    out.sort(key=lambda d: (-d["writes"], -d["count"], d["class_fqn"] or ""))
    by_reason = _reason_histogram(rows)
    return {
        "total": len(rows),
        "writes": sum(1 for r in rows if r.get("access") == "write"),
        "reads": sum(1 for r in rows if r.get("access") != "write"),
        "by_reason": by_reason,
        "reason_labels": _reason_labels(by_reason),
        "methods": out[:cap],
        "capped": max(0, len(out) - cap),
    }


def _reason_histogram(rows: list[dict[str, Any]]) -> dict[str, int]:
    """未定位行按成因计数（真实总数恒在此，不受方法 cap 影响；红线 #4 不丢数）。"""
    c = Counter(r.get("null_reason") or nrmod.UNKNOWN for r in rows)
    return dict(c.most_common())


def _reason_labels(by_reason: dict[str, int]) -> dict[str, str]:
    """`by_reason` 直方图出现过的成因码 → 中文标签（legend），随直方图一起焊进返回值，
    模型不用记码值/翻文档就能读懂 unlocated_by_reason 里每个码是什么意思。"""
    return {code: nrmod.REASON_LABEL.get(code, code) for code in by_reason}


# 动态写入候选纳入的成因（用户 2026-06-24 三项全放宽：含 unknown，未识别局部变量持 key 也算候选）。
_DYN_CAUSES = ("dynamic-loop", "concat", "external-const", "unknown")
# 动态写入候选的成因标签（key_resolution → 人读）。
_CAUSE_LABEL = {
    "dynamic-loop": "动态循环（遍历运行时/配置字段集）",
    "concat": "拼接键（运行时拼接字段标识）",
    "external-const": "外部/跨模块常量（不在扫描范围）",
    "unknown": "未识别（多为局部变量持 key）",
}


def _enrich_rows(rows: list[dict[str, Any]], plugin_home: dict[str, list]) -> None:
    """给 field_access 行补派生字段（插件简名/跨类/所属单据/跨单据），供 _fmt_access 复用。"""
    for r in rows:
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
        # 模式 B：事件方法 → 苍穹语义文档主题，焊进返回值，提示段二「判触发时机/入库先查语义，勿臆断」。
        r["semantics_topic"] = hints.event_topic(r.get("event_method"), r.get("plugin_type"))


def _collect_materials(
    conn, field_key: str, *,
    form_key: str | None = None, entry_key: str | None = None, level: str | None = None,
) -> dict[str, Any]:
    """取数 + enrich + 分桶 + 算 summary/coarse/dynamic_writers/convert_context，返回**未做
    slim/cap 的原始材料**。富投影 `field_trace()` 与紧凑投影 `trace_compact()` 共用此函数
    （红线 #6：取证逻辑只此一份，绝不重写）。group_list 各组的 writers/readers 里是 RAW 行。"""
    # entry_key 仅对分录/子分录有意义：field_access 里表头/基础资料的 entry_key 恒为 None
    # （见 schema「表头为 None」）。但 Web 的「字段定义坐标」菜单用 field 表的 entity_key 当
    # entry 传进来——而 field 表对表头字段存的是表头实体 key（非 None，见 dym_parser）。若不
    # 归一，表头钻取会用「表头实体 key」去匹配 entry_key=None 的记录而落空，精确桶为空却落到
    # 「可能命中」，于是报「该精确坐标无确定命中」，与外层「全部坐标」的分组结果自相矛盾。
    if level is not None and level not in ("entry", "subentry"):
        entry_key = None
    java = json.loads(store.get_meta(conn, "java_analysis") or "{}")

    form_names = {r["key"]: r["name"] for r in conn.execute("SELECT key,name FROM form")}
    # 扩展别名（form.is_extension=1）：内容已并入 extends 指向的原厂 form_key
    # （见 cosmic_kb/metadata/merge.py::build_extension_alias）。查询命中这类别名 key 时
    # 内容必然是空的，加一句重定向提示，别让人以为"这单据没被扫到"。
    extends_map = {r["key"]: r["extends"] for r in conn.execute(
        "SELECT key, extends FROM form WHERE is_extension=1 AND extends IS NOT NULL")}
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

    # ── 跨单据歧义闸门：裸字段（未指定 form_key）若元数据里跨 ≥2 张单据定义，直接反问 ──
    # 同单据内跨层级/分录不算歧义（那走下面 possible 桶）；已显式给 form_key 的调用不受影响。
    # 2026-07 起收窄：裸字段不再聚合列出全部单据的证据（对已知道要查哪张单据的排障者是噪音），
    # 改交更便宜的 resolve_fields 做发现，trace 只做精确坐标取证。
    if form_key is None:
        distinct_forms = {o["form_key"] for o in occurrences if o["form_key"]}
        if len(distinct_forms) > 1:
            sample = sorted(distinct_forms)[0]
            note = (f"字段「{field_key}」在 {len(distinct_forms)} 个单据都有定义"
                    f"（{'、'.join(sorted(distinct_forms))}），请指定单据后再查，如 "
                    f"\"{sample}.{field_key}\" 或加 form_key 参数。")
            return {
                "field_key": field_key, "status": "need_clarification",
                "filter": {"form_key": form_key, "entry_key": entry_key, "level": level},
                "occurrences": occurrences, "note": note,
            }

    # 取该字段的全部读写记录（不在 SQL 里按 level/entry 过滤——精确模式要在 Python 里分桶，
    # 把"层级/分录判不准"的写入归到「可能命中」而非直接丢弃，满足"不能遗漏"）。
    all_rows = [dict(r) for r in conn.execute(
        f"SELECT {_FA_COLS} FROM field_access WHERE field_key=?", (field_key,)).fetchall()]

    # ── 动态写入候选：字段 key 钉不出（动态循环/拼接/外部常量/未识别），可能正改本字段 ──
    # 这些行 field_key 为空、按 key 查不到，却可能正改本字段——静态无法确认，留给段二大模型读源码定性。
    # 三项放宽（用户 2026-06-24，避免漏掉真实写入如 ContractRefundAdjustFormPlugin）：
    #   ① 成因含 unknown（_DYN_CAUSES）——未识别局部变量持 key 也是钉不出字段的候选，不该静默丢；
    #   ② 范围 = 行 form_key ∈ 本字段单据 **∪** 插件注册在本字段单据上（form_key 判不出时兜底）——
    #      动态写入的 DynamicObject 常溯不到来源实体(form_key=None)，但插件本身注册在某单据上，
    #      只按行 form_key 收窄会把这批"越动态越漏"的写入滤光，故按 plugin_home 兜底归属；
    #   ③ 不按 level 硬过滤（在下方 Python 段）——动态写入跨层级，碰哪层未知。
    scope_forms = {o["form_key"] for o in occurrences if o["form_key"]} | {
        r["form_key"] for r in all_rows if r["form_key"]}
    # 注册在范围单据上的插件 —— 给 form_key 判不出的动态写兜底归属（plugin 表为主，binding 回落）。
    scope_plugins: set[str] = set()
    if scope_forms:
        fph0 = ",".join("?" * len(scope_forms))
        sf0 = sorted(scope_forms)
        for tbl in ("plugin", "binding"):
            scope_plugins |= {r[0] for r in conn.execute(
                f"SELECT DISTINCT class_name FROM {tbl} WHERE form_key IN ({fph0})", sf0)
                if r[0]}
    dyn_rows: list[dict[str, Any]] = []
    if scope_forms:
        fph = ",".join("?" * len(scope_forms))
        cph = ",".join("?" * len(_DYN_CAUSES))
        clause = f"form_key IN ({fph})"
        params: list[str] = list(_DYN_CAUSES) + sorted(scope_forms)
        if scope_plugins:
            pph = ",".join("?" * len(scope_plugins))
            clause = f"({clause} OR (form_key IS NULL AND plugin_fqn IN ({pph})))"
            params += sorted(scope_plugins)
        dyn_rows = [dict(r) for r in conn.execute(
            f"SELECT {_FA_COLS} FROM field_access WHERE field_key IS NULL AND access='write' "
            f"AND key_resolution IN ({cph}) AND {clause}", params).fetchall()]

    # 插件所属单据：plugin_fqn 注册在哪张/哪些单据上（plugin 表为主，binding 表回落）。
    # 这就是「被另外的哪个元数据的什么插件改了」——字段在单据 A，却可能被注册在单据 B 的插件改。
    plugin_home = _plugin_home_map(
        conn, {r["plugin_fqn"] for r in all_rows} | {r["plugin_fqn"] for r in dyn_rows}, form_names)
    _enrich_rows(all_rows, plugin_home)
    _enrich_rows(dyn_rows, plugin_home)
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
        # 注意：此处**不做** slim/cap——RAW 行留给两种投影各自设界（富投影按行 cap、紧凑投影按类合并）。

    writers = [r for r in rows if r["access"] == "write"]
    readers = [r for r in rows if r["access"] == "read"]
    # 未定位行按成因分布（信任优先）：哪些该不该顺源码反推，真实总数恒在此。按本字段**全部**
    # form_key=None 行统计（裸字段查询不拆 unlocated 桶，故不能只数 unlocated 变量）。
    unlocated_by_reason = _reason_histogram([r for r in all_rows if r["form_key"] is None])
    summary = {
        "writers": len(writers), "readers": len(readers),
        "persisting_writers": sum(1 for r in writers if r["persists"] == "yes"),
        "uncertain_writers": sum(1 for r in writers if r["persists"] == "unknown"),
        "plugins": len({r["plugin_fqn"] for r in rows}),
        "forms": len({r["form_key"] for r in rows}),
        "coords": len(group_list),
        "possible": len(possible),
        "unlocated": len(unlocated),
        "unlocated_by_reason": unlocated_by_reason,
        # 成因码 → 中文标签 legend，焊进返回值本体（此前只有裸码，模型只能凭 kebab-case 猜）。
        "unlocated_by_reason_labels": _reason_labels(unlocated_by_reason),
        # 注解反射映射写入（@…Annotation(value="key") + convertTo…DynamicObject 反射 set）命中数。
        # 仅标量（真实总数）；明细随普通 writers 行走既有「按类合并 + cap + 字节 governor」，不另起数组。
        "annotation_writers": sum(1 for r in all_rows
                                  if r["via"] == "annotation-map" and r["access"] == "write"),
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
        "coarse_only": len(shown),                                      # 仅粗扫见、非常量类（真实总数）
        "idiom": sum(1 for c in shown if c["idiom"]),                   # 其中强信号读写习语
        "const_excluded": sum(1 for c in coarse_only if c["relpath"] in const_relpaths),  # 落常量类、已剔除
        "high_rows": len(all_rows),                                     # 高精度该字段命中条数（参照）
        "locations": shown[:_CAP_COARSE],                              # 设界（真实数见 coarse_only）
    }

    # ── 动态写入候选：按用户查询单据收窄 + 按成因分桶（三项放宽：不按 level 过滤）──────────
    # 精确单据时收窄到该单据：行 form_key 命中，或 form_key 判不出但插件注册在该单据（plugin_home 兜底）。
    dyn_scoped = dyn_rows
    if form_key:
        home_on_form = {fqn for fqn, homes in plugin_home.items()
                        if any(h["form_key"] == form_key for h in homes)}
        dyn_scoped = [r for r in dyn_scoped
                      if r["form_key"] == form_key
                      or (r["form_key"] is None and r["plugin_fqn"] in home_on_form)]
    dyn_scoped.sort(key=lambda r: (r["key_resolution"], r["plugin_fqn"] or "", r["line"]))
    # 折叠成「该读方法」清单——防大模型上下文爆炸：同方法写 N 个钉不出 key 的字段只读一次。
    # 先按 _BIG_CAP 折出**完整**方法清单（分页要据此翻到第 11 条之后），共享 dict 仍只展示前 10。
    wl_full = dynamic_writes.build_method_worklist(dyn_scoped, cap=_BIG_CAP)
    dyn_full_methods = wl_full["methods"]
    dynamic_writers = {
        "total": len(dyn_scoped),
        "by_cause": {c: sum(1 for r in dyn_scoped if r["key_resolution"] == c)
                     for c in _DYN_CAUSES},
        # 成因码 → 中文标签 legend（此前 by_cause 只有裸码，且这四个码连 docstring 都没提到过）；
        # 各 method 条目自带的 cause_label 是同一份标签，这里补一份汇总级 legend 与 by_reason 对称。
        "cause_labels": {c: _CAUSE_LABEL.get(c, c) for c in _DYN_CAUSES},
        "total_methods": wl_full["total_methods"],
        "methods": dyn_full_methods[:10],
        "capped": max(0, len(dyn_full_methods) - 10),
    }
    summary["dynamic_writers"] = len(dyn_scoped)

    note = None
    if not java.get("available", True):
        note = "⚠ tree-sitter 未启用（pip install -e .[parse]），字段级分析为空。"
    elif not all_rows:
        if coarse["coarse_only"] > 0:
            # 高精度（field_access）零命中，不等于源码里真没有——粗扫（字面量词法扫描）
            # 已经在 coarse.locations 里摆了证据，只是没被结构化成 field_access 行（常见于
            # 动态拼接/反射/高精度解析不到的写法）。红线 #4：宁可提示"去人工核实"，也不能让
            # 这条能证明"源码里其实有命中"的信号被一句"未找到"盖过去（2026-07-03 修复：此前
            # 这里不看 coarse，会让大模型误判"完全没有读写"）。
            note = (f"高精度扫描（field_access）未找到任何插件读写该字段，但粗扫（源码字面量）"
                    f"发现 {coarse['coarse_only']} 处疑似命中，见 coarse.locations——很可能是"
                    "动态拼接/反射等高精度解析不到的写法，不代表源码里真没有读写，请人工核查"
                    "这些位置的源码再下结论。")
        else:
            note = ("未找到任何插件读写该字段（可能：字段名有误 / 只被平台处理 / 源码未给全 / "
                    "经常量引用未解析）。")
    elif precise and not rows and possible:
        note = "该精确坐标无确定命中，但本单据该字段有「可能命中（层级/分录存疑）」记录，见下。"
    elif precise and not rows and not possible and unlocated:
        note = "该单据无确定命中；下方「未定位单据」列出了来源判不出、但确实读写该字段的插件，供人工核对。"
    elif not precise and len(occurrences) > 1 and not form_key:
        note = (f"该字段在 {len(occurrences)} 处实体里都有定义。下面已按「单据·层级·分录」分组；"
                f"用 元数据.[分录.[子分录.]]字段 点号格式 或点定义坐标可精确定位到某层级。")

    extends_target = extends_map.get(form_key) if form_key else None
    if extends_target:
        redirect = (f"⚑ {form_key} 是扩展别名，内容已并入原厂单据 {extends_target}，"
                    f"请改查 {extends_target}.{field_key}")
        note = f"{redirect}；{note}" if note else redirect

    # 已给 form_key（精确/半精确查询）时，对外只暴露本单据(+分录/层级)范围内的定义坐标——
    # 其他单据的同名字段定义对"已经知道查哪张单据"的排障者是纯噪音（用户 2026-07-05 指出
    # occurrences 未跟上 possible 桶 2026-07 那次"裸字段不聚合列全部单据"的收窄思路）。
    # 内部 scope_forms/跨单据歧义闸门仍用上面未过滤的 occurrences（需要跨单据全貌才能判歧义/定候选范围）。
    visible_occurrences = occurrences
    if form_key:
        visible_occurrences = [o for o in occurrences if o["form_key"] == form_key]
        if entry_key:
            visible_occurrences = [o for o in visible_occurrences if o["entity_key"] == entry_key]
        if level:
            visible_occurrences = [o for o in visible_occurrences if o["level"] == level]

    # 模式 B：被查字段的已核对中文名（同 key 跨多坐标有不同名时留 None，不替选）。
    distinct_names = {o["field_name"] for o in visible_occurrences if o.get("field_name")}
    field_name = next(iter(distinct_names)) if len(distinct_names) == 1 else None

    return {
        "field_key": field_key,
        "field_name": field_name,
        "filter": {"form_key": form_key, "entry_key": entry_key, "level": level},
        "precise": precise,
        "occurrences": visible_occurrences,
        "group_list": group_list,      # 各组 writers/readers 为 RAW 行
        "possible": possible,          # RAW 行
        "unlocated": unlocated,        # RAW 行
        "summary": summary,
        "coarse": coarse,              # 已设界的粗扫盲点 dict（两投影共用）
        "dynamic_writers": dynamic_writers,  # 已折叠的动态写入候选 dict（两投影共用，methods 仅前 10）
        "dynamic_writers_full": dyn_full_methods,  # 完整折叠方法清单（仅 trace_compact 分页用）
        "java_available": java.get("available", True),
        "note": note,
    }


def field_trace(
    conn, field_key: str, *,
    form_key: str | None = None, entry_key: str | None = None, level: str | None = None,
) -> dict[str, Any]:
    """追踪一个字段的全部插件读写 + 落库判定，按实体坐标分组（**富投影**：Web/CLI/builder 用）。

    每坐标的 writers 按行 slim+cap，readers 折叠成「该读方法」清单；possible 行级 slim+cap；
    unlocated 折叠成「反推来源单据」工作单。
    真实总数恒在 summary（红线 #4）。MCP 防截断用 `trace_compact`（按类合并 + 写读拆分），不走本函数。
    """
    m = _collect_materials(conn, field_key, form_key=form_key, entry_key=entry_key, level=level)
    if m.get("status") == "need_clarification":
        return m
    for g in m["group_list"]:
        # 设界（summary 已锁真实计数）：writers 投影+cap；readers 折叠成「该读方法」清单。
        g["writers"] = [_slim_row(r) for r in g["writers"][:_CAP_WRITERS]]
        g["readers"] = _collapse_reader_methods(g["readers"], cap=_CAP_READER_METHODS)
    return {
        "field_key": m["field_key"],
        "field_name": m["field_name"],
        "filter": m["filter"],
        "precise": m["precise"],
        "occurrences": m["occurrences"],
        "groups": m["group_list"],
        # possible 投影+cap；unlocated 折叠成「反推来源单据」工作单（真实总数在 summary.possible / summary.unlocated）。
        "possible": [_slim_row(r) for r in m["possible"][:_CAP_POSSIBLE]],   # 可能命中（层级/分录存疑）
        "unlocated": _collapse_unlocated_methods(m["unlocated"], cap=_CAP_UNLOCATED_METHODS),  # 来源未定位（form_key 为空）→ 反推来源工作单
        # 顶层扁平 writers/readers 已删——与 groups[].writers/readers 重复、无消费方（Web/CLI 用 groups+summary）。
        "summary": m["summary"],
        "coarse": m["coarse"],              # 粗精度扫描命中 + 逐处互证（与高精度并列）
        "dynamic_writers": m["dynamic_writers"],  # 同单据动态写入候选（钉不出字段，交段二大模型读源码定性）
        "java_available": m["java_available"],
        "note": m["note"],
    }


# ── 紧凑投影（MCP 防截断）：写/读拆分 + 按类合并 + cap/字节 governor ──────────────────
# 32KB 是 MCP host 硬上限。per-section 行级 cap 管不住（坐标组数 + unlocated/possible 无界）。
# 故 MCP 走本投影：把"散落的行/方法"按类塌缩成"有界的类数"，并按 access 只返一侧；真实总数恒在
# summary、被 cap 截掉的数留在各节点 `capped`（红线 #4 不丢数）。
_COMPACT_CAP_CLASSES = 60     # 每个集合（坐标组/possible/unlocated）保留的类节点上限
_COMPACT_CAP_SITES = 12       # 单类内写入点上限
_COMPACT_CAP_METHODS = 12     # 单类内读取方法上限
_COMPACT_CAP_OVERVIEW = 80    # readers_overview 类条目上限
_COMPACT_CAP_GROUPS = 16      # 坐标组（单据·层级·分录）节点上限——裸字段发现态可命中十几张单据
_COMPACT_CAP_OCC = 20         # occurrences（元数据定义坐标）上限
_COMPACT_CAP_DYN = 10         # dynamic_writers 该读方法清单上限
_COMPACT_CAP_COARSE = 20      # coarse.locations 粗扫盲点上限
_BIG_CAP = 10 ** 9            # "不裁剪"哨兵（折叠出完整清单供分页 slice，再按预算逐条装入）
_COMPACT_BUDGET = 31000       # 序列化预算（host 32768 硬上限留 ~1768 字节裕量）。**必须按 host 真实序列化方式度量**：
                              # MCP 底层 `mcp/server/lowlevel/server.py` 用 `json.dumps(result, indent=2)`
                              # 发文本内容——默认 ensure_ascii=True + **indent=2 缩进**。深层嵌套结构缩进会
                              # 凭空多出 ~35% 空白（实测 compact 25KB → 缩进后 34KB），只量无缩进会严重低估、
                              # 误判「没超」而被 host 从中段硬切（红线 #4 的「永不被截断」靠 _wire_len 度量
                              # + 下方可收敛的 ladder 才成立）。


def _wire_len(obj: Any) -> int:
    """按 MCP host 真实发送方式度量序列化字节数。

    MCP 底层 server 固定用 `json.dumps(results, indent=2)`（ensure_ascii 默认 True）转文本内容，
    governor 必须用同一口径量，否则 indent 的缩进/换行空白被漏算 → 实际 wire 体积超 host 32KB 被截。
    """
    return len(json.dumps(obj, ensure_ascii=True, indent=2))


def _merge_writers_by_class(rows: list[dict[str, Any]], *, cap_classes: int, cap_sites: int
                            ) -> dict[str, Any]:
    """写行按**物理写入类**(`access_class` 回落 `plugin_fqn`)合并：类级常量字段只存一份，写入点
    （行号/落库/via 等会变的）列在 `sites`。类按写入数降序、cap_classes 截断；类内 sites cap_sites
    截断。真实总数在 `total`/各类 `count`，截断量在 `capped`/`sites_capped`。"""
    groups: dict[str | None, dict[str, Any]] = {}
    for r in rows:
        cls = r.get("access_class") or r.get("plugin_fqn")
        g = groups.get(cls)
        if g is None:
            g = groups[cls] = {
                "class_fqn": cls,
                "plugin_type": r.get("plugin_type"),
                "plugin_form_label": r.get("plugin_form_label"),
                "plugin_cross_form": r.get("plugin_cross_form"),
                "sites": [],
            }
        g["sites"].append({
            "event_method": r.get("event_method"),
            "line": r.get("line"),
            "via": r.get("via"),
            "persists": r.get("persists"),
            "persist_reason": r.get("persist_reason"),
            "key_resolution": r.get("key_resolution"),
            "source_relpath": r.get("source_relpath"),
            "semantics_topic": r.get("semantics_topic"),
        })
    classes = sorted(groups.values(), key=lambda c: (-len(c["sites"]), c["class_fqn"] or ""))
    out: list[dict[str, Any]] = []
    for c in classes[:cap_classes]:
        sites = c["sites"]
        out.append({
            "class_fqn": c["class_fqn"], "plugin_type": c["plugin_type"],
            "plugin_form_label": c["plugin_form_label"], "plugin_cross_form": c["plugin_cross_form"],
            "count": len(sites),
            "sites": sites[:cap_sites],
            "sites_capped": max(0, len(sites) - cap_sites),
        })
    return {"total": len(rows), "classes": out, "capped": max(0, len(classes) - cap_classes)}


def _merge_readers_by_class(rows: list[dict[str, Any]], *, cap_classes: int, cap_methods: int
                            ) -> dict[str, Any]:
    """读行按**类**(`access_class` 回落 `plugin_fqn`)合并，类内再按事件方法去重计数。读取价值最低，
    塌成 `{class_fqn, methods:[{method,count}], total}` 即可——要弄清谁读了它，去那几个方法读源码。
    类按读取数降序、cap_classes 截断；类内 methods cap_methods 截断。"""
    groups: dict[str | None, dict[str, Any]] = {}
    for r in rows:
        cls = r.get("access_class") or r.get("plugin_fqn")
        g = groups.get(cls)
        if g is None:
            g = groups[cls] = {
                "class_fqn": cls, "plugin_type": r.get("plugin_type"),
                "plugin_form_label": r.get("plugin_form_label"), "_methods": {},
            }
        mk = r.get("event_method")
        mrec = g["_methods"].get(mk)
        if mrec is None:
            mrec = g["_methods"][mk] = {
                "method": mk, "count": 0,
                "semantics_topic": r.get("semantics_topic"),
            }
        mrec["count"] += 1
    classes: list[dict[str, Any]] = []
    for g in groups.values():
        methods = sorted(g["_methods"].values(), key=lambda d: (-d["count"], d["method"] or ""))
        classes.append({
            "class_fqn": g["class_fqn"], "plugin_type": g["plugin_type"],
            "plugin_form_label": g["plugin_form_label"],
            "total": sum(m["count"] for m in methods),
            "methods": methods[:cap_methods],
            "methods_capped": max(0, len(methods) - cap_methods),
        })
    classes.sort(key=lambda c: (-c["total"], c["class_fqn"] or ""))
    return {"total": len(rows), "classes": classes[:cap_classes],
            "capped": max(0, len(classes) - cap_classes)}


def _readers_overview(rows: list[dict[str, Any]], *, cap: int) -> dict[str, Any]:
    """读取「仅按类计数」概览（默认视图用）：`[{class_fqn, total}]`，最省字节。"""
    cnt: dict[str | None, int] = {}
    for r in rows:
        cls = r.get("access_class") or r.get("plugin_fqn")
        cnt[cls] = cnt.get(cls, 0) + 1
    items = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0] or ""))
    return {"total": len(rows),
            "classes": [{"class_fqn": c, "total": n} for c, n in items[:cap]],
            "capped": max(0, len(items) - cap)}


def _access_rows(rows: list[dict[str, Any]], access: str | None) -> list[dict[str, Any]]:
    """按 access 过滤行集合：'read' 取读行，其余（None/'write'）取写行。"""
    want = "read" if access == "read" else "write"
    return [r for r in rows if r.get("access") == want]


def _cap_dynamic_writers(dw: dict[str, Any], cap: int) -> dict[str, Any]:
    """裁剪「该读方法」清单到 cap 条（真实总数已在 dw['total_methods']/['total']）。"""
    methods = dw.get("methods") or []
    if len(methods) <= cap:
        return dw
    return {**dw, "methods": methods[:cap], "methods_capped": max(0, len(methods) - cap)}


def _cap_coarse(coarse: dict[str, Any], cap: int) -> dict[str, Any]:
    """裁剪粗扫盲点 locations 到 cap 条（真实总数已在 coarse['coarse_only']）。"""
    locs = coarse.get("locations") or []
    if len(locs) <= cap:
        return coarse
    return {**coarse, "locations": locs[:cap], "locations_capped": max(0, len(locs) - cap)}


def _build_compact(
    m: dict[str, Any], access: str | None, *,
    cap_classes: int, cap_sites: int, cap_methods: int, cap_overview: int,
    cap_groups: int, cap_occ: int, cap_dyn: int, cap_coarse: int,
) -> dict[str, Any]:
    """从原始材料组装紧凑 dict（一档 cap 下的一次构建，governor 会按字节预算反复调用收紧）。"""
    want_write = access in (None, "write")
    want_read_detail = access == "read"
    want_read_overview = access is None
    capped_hit = False

    def _mw(rows):
        nonlocal capped_hit
        d = _merge_writers_by_class(rows, cap_classes=cap_classes, cap_sites=cap_sites)
        capped_hit = capped_hit or bool(d["capped"]) or any(c["sites_capped"] for c in d["classes"])
        return d

    def _mr(rows):
        nonlocal capped_hit
        d = _merge_readers_by_class(rows, cap_classes=cap_classes, cap_methods=cap_methods)
        capped_hit = capped_hit or bool(d["capped"]) or any(c["methods_capped"] for c in d["classes"])
        return d

    def _mu(rows):
        nonlocal capped_hit
        d = _collapse_unlocated_methods(rows, cap=cap_methods)
        capped_hit = capped_hit or bool(d["capped"])
        return d

    groups_out: list[dict[str, Any]] = []
    for g in m["group_list"]:
        node: dict[str, Any] = {
            "label": g["label"], "form_key": g["form_key"], "form_name": g["form_name"],
            "level": g["level"], "entry_key": g["entry_key"],
            "summary": g["summary"], "convert_context": g["convert_context"],
        }
        if want_write:
            node["writers"] = _mw(g["writers"])
        if want_read_detail:
            node["readers"] = _mr(g["readers"])
        elif want_read_overview:
            d = _readers_overview(g["readers"], cap=cap_overview)
            capped_hit = capped_hit or bool(d["capped"])
            node["readers_overview"] = d
        groups_out.append(node)
    # 坐标组本身无界（裸字段可命中十几张单据），是「全量调」blow up 的主因——必须能整组裁剪，
    # 否则 governor 收紧再多 per-class cap 也压不下来。真实组数恒在 summary.coords。
    groups_capped = max(0, len(groups_out) - cap_groups)
    if groups_capped:
        capped_hit = True
    groups_out = groups_out[:cap_groups]

    # occurrences（元数据定义坐标）也无界：裸字段多处定义时会膨胀，按需裁剪。真实数 = summary 旁记。
    occ = m["occurrences"]
    occ_capped = max(0, len(occ) - cap_occ)
    if occ_capped:
        capped_hit = True

    res: dict[str, Any] = {
        "field_key": m["field_key"], "field_name": m["field_name"],
        "filter": m["filter"], "precise": m["precise"], "access": access or "all",
        "occurrences": occ[:cap_occ], "occurrences_total": len(occ),
        "summary": dict(m["summary"]),
        "groups": groups_out, "groups_total": len(m["group_list"]), "groups_capped": groups_capped,
    }
    # possible：按 access 过滤后按类合并（写侧用写合并，读侧用读合并）。
    # unlocated：折叠成「反推来源单据」工作单（含 plugin_home 线索），比按类合并更省字节。
    poss, unloc = _access_rows(m["possible"], access), _access_rows(m["unlocated"], access)
    if want_read_detail:
        res["possible"] = _mr(poss)
    else:  # None / write：展示写侧
        res["possible"] = _mw(poss)
    res["unlocated"] = _mu(unloc)
    # 粗扫盲点：不分 access 一律带上（2026-07-03 修复）——此前只在 access='read' 时才挂
    # `coarse` 字段，默认/写视图（writers=0 时最需要这条线索）里连 key 都不存在；大模型据此
    # 误判"完全没有读写"，换 access='read' 重查才发现粗扫其实有命中。粗扫本就设了 cap，
    # 常驻不会明显增大返回体积。
    res["coarse"] = _cap_coarse(m["coarse"], cap_coarse)
    capped_hit = capped_hit or len(m["coarse"].get("locations", [])) > cap_coarse
    if want_write:
        dw = _cap_dynamic_writers(m["dynamic_writers"], cap_dyn)
        capped_hit = capped_hit or len(m["dynamic_writers"].get("methods", [])) > cap_dyn
        res["dynamic_writers"] = dw

    _annotate_next_cursors(res, m["precise"])
    notes = [m["note"]] if m["note"] else []
    if capped_hit:
        notes.append("部分类节点/坐标组/明细因数量过多被截断（真实总数见 summary 与各节点 total）；"
                     "被截段已带 next_cursor，用 cursor=该值再调一次可翻页**取回全部被截条目**（不丢数）；"
                     "或用 form/entry/level 收窄到单坐标、access='read'/'write' 单看一侧。")
    res["note"] = " ".join(notes) if notes else None
    res["java_available"] = m["java_available"]
    _prepend_pagination_gate(res, groups_capped)
    return res


def _prepend_pagination_gate(res: dict[str, Any], groups_capped: int) -> None:
    """把翻页完成状态提到返回体**第一个 key**（原地重排 `res`）。

    背景：此前只把 next_cursor 散落在各段深处 + 一句 `note` 提醒"翻完前禁止下结论"，
    但大模型仍会看到 `capped=0` 的段就当"数据齐了"下结论，翻页规则形同虚设——
    散落各处的 cursor 依赖大模型"自觉逐段检查"，而不是一眼可判的事实。
    这里改成顶层 `pagination.complete` 布尔 + `pending` 清单，把"要不要翻页"从
    "阅读理解题"降级成"查一个字段"，且置于返回体最前面，避免被后面的正文淹没。
    """
    pending = _collect_pending_cursors(res)
    if groups_capped:
        pending.append({"section": "groups", "next_cursor": None,
                        "hint": "坐标组数超限被截，无法翻页取全——用 form_key/entry_key/level "
                                "收窄到单坐标可看到该坐标完整内容"})
    reordered = {"pagination": pagination_gate(pending), **res}
    res.clear()
    res.update(reordered)


def pagination_gate(pending: list[dict[str, Any]]) -> dict[str, Any]:
    """构造顶层 `pagination` 门：`complete` 一眼可判，`pending` 列出每个未取全段的 next_cursor。

    `bill_view.py` 复用本函数，保证 trace/bill 两个高频取证工具的翻页信号同一套口径。
    """
    gate: dict[str, Any] = {"complete": not pending, "pending": pending}
    if pending:
        gate["instruction"] = ("pending 非空 = 数据未取全：对 next_cursor 非 null 的每一项，用 "
                               "cursor=<该值> 再调一次本工具，直至全部 next_cursor 变 null，"
                               "才能下\"未覆盖/无人读写/不存在\"等结论；中途下结论视为臆造。")
    return gate


def _pending_from_flat_cursors(res: dict[str, Any], suffix: str = "_next_cursor"
                               ) -> list[dict[str, Any]]:
    """扫顶层形如 `<段名>{suffix}` 的扁平游标字段（bill 视图用此命名法），汇总成待翻页清单。"""
    return [{"section": k[:-len(suffix)], "next_cursor": v}
            for k, v in res.items() if k.endswith(suffix) and v]


def _collect_pending_cursors(res: dict[str, Any]) -> list[dict[str, Any]]:
    """扫结果里所有仍非 null 的 `next_cursor`，汇总成待翻页清单（供顶层 `pagination` 门用）。"""
    pending: list[dict[str, Any]] = []
    for key in ("unlocated", "dynamic_writers", "possible", "coarse"):
        node = res.get(key)
        if isinstance(node, dict) and node.get("next_cursor"):
            pending.append({"section": key, "next_cursor": node["next_cursor"]})
    occ_cursor = res.get("occurrences_next_cursor")
    if occ_cursor:
        pending.append({"section": "occurrences", "next_cursor": occ_cursor})
    for g in res.get("groups") or []:
        label = g.get("label") or g.get("form_key")
        for key in ("writers", "readers", "readers_overview"):
            node = g.get(key)
            if isinstance(node, dict) and node.get("next_cursor"):
                pending.append({"section": f"{key}@{label}", "next_cursor": node["next_cursor"]})
    return pending


# ── 游标分页（红线 #4 升级：被 cap 的 worklist 不只报计数，给 next_cursor 让模型逐页取回全部）──
# 32KB 硬上限下单次装不全的段（unlocated/dynamic_writers/readers/writers/possible/coarse/occurrences），
# overview 里只展示一屏 + 带 `next_cursor`（形如 "unlocated@5"）；模型用 cursor= 该值再调一次，本工具
# 返回该段从 offset 起、预算内能装的下一页 items + 新的 next_cursor，直至 next_cursor=None（取完）。
# 这样"被截内容"对消费方**可达**，而非仅一个计数（用户 2026-06-28 指出"只通知截断=仍丢信息"）。
_PAGE_SECTIONS = ("writers", "readers", "unlocated", "dynamic_writers",
                  "possible", "coarse", "occurrences")


def _annotate_next_cursors(res: dict[str, Any], precise: bool) -> None:
    """给 overview 里被 cap 的段补 `next_cursor`，指明翻页取回被截条目的确切游标。"""
    u = res.get("unlocated")
    if isinstance(u, dict) and u.get("capped"):
        u["next_cursor"] = f"unlocated@{len(u.get('methods') or [])}"
    dw = res.get("dynamic_writers")
    if isinstance(dw, dict):
        shown = len(dw.get("methods") or [])
        if shown < (dw.get("total_methods") or shown):
            dw["next_cursor"] = f"dynamic_writers@{shown}"
    p = res.get("possible")
    if isinstance(p, dict) and p.get("capped"):
        p["next_cursor"] = f"possible@{len(p.get('classes') or [])}"
    c = res.get("coarse")
    if isinstance(c, dict) and c.get("locations_capped"):
        c["next_cursor"] = f"coarse@{len(c.get('locations') or [])}"
    occ_total = res.get("occurrences_total", 0)
    occ_shown = len(res.get("occurrences") or [])
    if occ_total > occ_shown:
        res["occurrences_capped"] = occ_total - occ_shown
        res["occurrences_next_cursor"] = f"occurrences@{occ_shown}"
    # 嵌套 writers/readers 仅单坐标(precise+单组)可分页（多组先按坐标收窄）。
    groups = res.get("groups") or []
    if precise and len(groups) == 1:
        g = groups[0]
        w = g.get("writers")
        if isinstance(w, dict):
            if w.get("capped"):
                w["next_cursor"] = f"writers@{len(w.get('classes') or [])}"
            elif any(cl.get("sites_capped") for cl in (w.get("classes") or [])):
                w["next_cursor"] = "writers@0"   # 类全展示但某类写入点被截 → 翻页取全 sites
        r = g.get("readers")
        if isinstance(r, dict):
            if r.get("capped"):
                r["next_cursor"] = f"readers@{len(r.get('classes') or [])}"
            elif any(cl.get("methods_capped") for cl in (r.get("classes") or [])):
                r["next_cursor"] = "readers@0"
        ro = g.get("readers_overview")
        if isinstance(ro, dict) and ro.get("capped"):
            ro["next_cursor"] = "readers@0"      # 概览类被截 → 翻 readers 明细页拿全


def _parse_cursor(cursor: str) -> tuple[str, int]:
    """解析 "section@offset" → (section, offset)；缺 offset 当 0，非法 offset 归 0。"""
    section, _, off = cursor.strip().partition("@")
    section = section.strip()
    try:
        offset = max(0, int(off)) if off else 0
    except ValueError:
        offset = 0
    return section, offset


def _section_full(m: dict[str, Any], access: str | None, section: str
                  ) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """返回某段的**完整（未 cap）有序条目列表** + 段级 head（与 overview 同序，保证 offset 一致）。

    不可分页/需先收窄时返回 (None, {reason})。writers/readers 是嵌套段，仅单坐标(单组)可分页。
    """
    if section == "unlocated":
        d = _collapse_unlocated_methods(_access_rows(m["unlocated"], access), cap=_BIG_CAP)
        return d["methods"], {"total_rows": d["total"], "writes": d.get("writes"),
                              "reads": d.get("reads"), "by_reason": d.get("by_reason"),
                              "reason_labels": d.get("reason_labels")}
    if section == "dynamic_writers":
        dw = m["dynamic_writers"]
        return list(m.get("dynamic_writers_full") or []), {
            "total_rows": dw.get("total"), "total_methods": dw.get("total_methods"),
            "by_cause": dw.get("by_cause"), "cause_labels": dw.get("cause_labels")}
    if section == "possible":
        rows = _access_rows(m["possible"], access)
        d = (_merge_readers_by_class(rows, cap_classes=_BIG_CAP, cap_methods=_BIG_CAP)
             if access == "read"
             else _merge_writers_by_class(rows, cap_classes=_BIG_CAP, cap_sites=_BIG_CAP))
        return d["classes"], {"total_rows": d["total"]}
    if section == "coarse":
        c = m["coarse"]
        return list(c.get("locations") or []), {"coarse_only": c.get("coarse_only"),
                                                "high_rows": c.get("high_rows")}
    if section == "occurrences":
        return list(m["occurrences"]), {"total_rows": len(m["occurrences"])}
    if section in ("writers", "readers"):
        groups = m["group_list"]
        if len(groups) != 1:
            return None, {"reason": "writers/readers 分页需先用 form/entry/level 收窄到单坐标"}
        g = groups[0]
        if section == "writers":
            d = _merge_writers_by_class(g["writers"], cap_classes=_BIG_CAP, cap_sites=_BIG_CAP)
        else:
            d = _merge_readers_by_class(g["readers"], cap_classes=_BIG_CAP, cap_methods=_BIG_CAP)
        return d["classes"], {"group": g["label"], "total_rows": d["total"]}
    return None, {"reason": f"未知或不可分页的 section: {section}（可分页：{', '.join(_PAGE_SECTIONS)}）"}


def _page_section(m: dict[str, Any], access: str | None, section: str, offset: int,
                  budget: int) -> dict[str, Any]:
    """聚焦分页：只回某一段从 offset 起、预算内能装下的下一页 items + next_cursor。"""
    base = {
        "field_key": m["field_key"], "field_name": m["field_name"],
        "filter": m["filter"], "precise": m["precise"], "access": access or "all",
    }
    items, head = _section_full(m, access, section)
    if items is None:
        return {**base, "page": {"section": section, "error": head.get("reason")}}
    total = len(items)
    offset = min(max(0, offset), total)

    def _wrap(page: list[dict[str, Any]], nxt: int) -> dict[str, Any]:
        next_cursor = f"{section}@{nxt}" if nxt < total else None
        pending = [{"section": section, "next_cursor": next_cursor}] if next_cursor else []
        return {"pagination": pagination_gate(pending), **base,
                "page": {**head, "section": section, "offset": offset,
                        "returned": len(page), "total": total, "items": page,
                        "next_cursor": next_cursor}}

    page: list[dict[str, Any]] = []
    for it in items[offset:]:
        trial = page + [it]
        if page and _wire_len(_wrap(trial, offset + len(trial))) > budget:
            break          # 至少装一条（单条即便超 budget 也给，仍远小于 32KB）
        page = trial
    return _wrap(page, offset + len(page))


def trace_compact(
    conn, field_key: str, *,
    form_key: str | None = None, entry_key: str | None = None, level: str | None = None,
    access: str | None = None, cursor: str | None = None, budget: int = _COMPACT_BUDGET,
) -> dict[str, Any]:
    """**紧凑投影**（MCP 入口，防 host 32KB 截断）：写/读拆分 + 按类合并 + cap/字节 governor + 游标分页。

    - `access='write'`：只回写入（坐标→类→写入点）+ 动态写入候选；`access='read'`：只回读取
      （类→方法）+ 粗扫盲点；默认（None）：写入明细 + 读取按类计数概览（`readers_overview`）。
    - 真实总数恒在 `summary`；类节点/明细被 cap 截断的数在各节点 `capped`/`sites_capped`/`methods_capped`。
    - governor：构完测序列化字节，超 `budget` 就逐级收紧 cap 重建，直至 ≤ budget——保证永不被截断。
    - `cursor`（形如 `"unlocated@5"`）：被 cap 的段在 overview 里带 `next_cursor`，用 cursor= 该值
      再调一次即翻到该段下一页，逐页可**取回全部被截条目**（红线 #4：不仅报计数，还可达）。
    """
    if access not in ("write", "read"):
        access = None
    m = _collect_materials(conn, field_key, form_key=form_key, entry_key=entry_key, level=level)
    if m.get("status") == "need_clarification":
        return m
    if cursor:
        section, offset = _parse_cursor(cursor)
        return _page_section(m, access, section, offset, budget)
    # cap 阶梯：从宽到窄，命中预算即返；最后一档兜底（极端情况返回最小档）。
    # 列：(类, 单类写入点, 单类读取方法, readers_overview, 坐标组, occurrences, dynamic_writers, coarse)。
    # 关键：除 per-class cap 外，**坐标组/occ/dyn/coarse 也逐档收紧**——否则裸字段「全量调」
    # 命中十几张单据时，per-class cap 再小也压不下整组体积，governor 无法收敛（旧版的洞）。
    ladder = [
        (_COMPACT_CAP_CLASSES, _COMPACT_CAP_SITES, _COMPACT_CAP_METHODS, _COMPACT_CAP_OVERVIEW,
         _COMPACT_CAP_GROUPS, _COMPACT_CAP_OCC, _COMPACT_CAP_DYN, _COMPACT_CAP_COARSE),
        (40, 8, 8, 60, 10, 16, 8, 16),
        (25, 5, 5, 40, 6, 12, 6, 10),
        (20, 4, 4, 32, 5, 10, 6, 8),   # 中间档：indent 让步长变大，多一档把预算用满、少裁次要工作单
        (15, 3, 3, 25, 4, 8, 4, 6),
        (8, 2, 2, 15, 2, 5, 2, 3),
        (5, 1, 1, 8, 1, 3, 1, 2),    # 硬底：极端情况也能塌到单组单点
    ]
    res: dict[str, Any] = {}
    for cc, cs, cm, co, cg, coc, cd, ccoarse in ladder:
        res = _build_compact(m, access, cap_classes=cc, cap_sites=cs, cap_methods=cm,
                             cap_overview=co, cap_groups=cg, cap_occ=coc, cap_dyn=cd,
                             cap_coarse=ccoarse)
        # 按 host 真实序列化方式（json.dumps indent=2, ensure_ascii=True）度量——见 _wire_len。
        if _wire_len(res) <= budget:
            return res
    return res


def parse_locator(text: str) -> tuple[str, str | None, str | None, str | None]:
    """把层级显式的点号查询解析成 (field_key, form_key, entry_key, level)，纯按段数判定：

      字段              → (field, None, None, None)   裸字段；若跨单据有歧义，trace 会反问单据
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
    if r.get("semantics_topic"):
        lines.append(f"        ⚑ 事件 {r['event_method']} 属 {r['semantics_topic']}：判触发时机/是否入库前先 cosmic_semantics('{r['semantics_topic']}')，勿凭训练知识臆断")
    path = r.get("path") or []   # 精简行单元素 path 已被剔除，按缺省处理
    if len(path) > 1:
        lines.append(f"        调用链: {' → '.join(path)}")
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

    if ft.get("status") == "need_clarification":
        lines.append("")
        lines.append(f"⚠ {ft.get('note') or '该字段在多个单据都有定义，请指定单据后再查。'}")
        return "\n".join(lines)

    s = ft["summary"]
    lines.append("")
    lines.append(
        f"【概况】实体坐标 {s.get('coords', 0)} 个，写入 {s['writers']}"
        f"（落库 {s['persisting_writers']} / 存疑 {s['uncertain_writers']}），读取 {s['readers']}，"
        f"涉及插件 {s['plugins']} / 单据 {s['forms']}"
    )
    if s.get("annotation_writers"):
        lines.append(
            f"  ⚙ 含注解反射映射写入 {s['annotation_writers']} 处（@…Annotation(value) + convertTo…"
            f"DynamicObject 反射 set，via=annotation-map；落库取决于调用方是否保存转换产物，标存疑）")
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
        rd = g.get("readers") or {}
        if rd.get("total"):
            lines.append(f"  【读】（按方法去重，共 {rd['total']} 处 → {len(rd['methods'])} 个方法）")
            for m in rd["methods"][:max_list]:
                cls = m.get("plugin_simple") or (m.get("class_fqn") or "?").rsplit(".", 1)[-1]
                lines.append(
                    f"    {cls} [{m.get('plugin_type')}]  事件 {m.get('method')}"
                    f"  ({m['count']} 处)")
                if m.get("locations"):
                    lines.append(f"        位于 {' / '.join(m['locations'])}")
                if m.get("semantics_topic"):
                    lines.append(f"        ⚑ 判触发时机/是否入库前先 cosmic_semantics('{m['semantics_topic']}')")
            if rd.get("capped"):
                lines.append(f"    …另有 {rd['capped']} 个读方法未列出（用 单据.字段 收窄、或 --json）")

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

    # 未定位单据：确实读写该字段、但来源单据未钉出 → 折叠成「反推来源单据」工作单（仿动态写候选）。
    unloc = ft.get("unlocated") or {}
    if unloc.get("total"):
        lines.append("")
        lines.append("─" * 72)
        lines.append(
            f"▼ 未定位单据（确实读写该字段，但来源单据判不出，需大模型读源码反推来源）："
            f"{unloc['total']} 处（写 {unloc.get('writes', 0)} / 读 {unloc.get('reads', 0)}）"
            f" → {len(unloc.get('methods', []))} 个方法"
        )
        by_reason = unloc.get("by_reason") or {}
        if by_reason:
            parts = [f"{nrmod.REASON_LABEL.get(k, k)}×{v}" for k, v in by_reason.items()]
            lines.append("  成因分布：" + "；".join(parts))
        lines.append("  去这几个方法读源码，反推这个 DynamicObject 来自哪张单据（按写入数降序）：")
        for m in unloc.get("methods", [])[:max_list]:
            cls = m.get("plugin_simple") or (m.get("class_fqn") or "?").rsplit(".", 1)[-1]
            home = f"  «很可能属 {m['plugin_form_label']}»" if m.get("plugin_form_label") else "  «service/未注册»"
            lines.append(
                f"  · {cls}.{m['method']} [{m.get('plugin_type')}]"
                f"  (写 {m['writes']}/读 {m['reads']}){home}")
            if m.get("null_reason"):
                lines.append(f"        成因：{nrmod.REASON_LABEL.get(m['null_reason'], m['null_reason'])}")
            if m.get("locations"):
                lines.append(f"        位于 {' / '.join(m['locations'])}")
            if m.get("semantics_topic"):
                lines.append(f"        ⚑ 判触发时机/是否入库前先 cosmic_semantics('{m['semantics_topic']}')")
        if unloc.get("capped"):
            lines.append(f"    …另有 {unloc['capped']} 个方法未列出（用 单据.字段 收窄、或 --json）")
        lines.append("    注：plugin_form_label 是插件注册单据，只是来源线索非确诊；来源单据请读源码确认，勿臆造。")

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

    # 动态写入候选：同单据内字段 key 钉不出的写入（动态循环/拼接/外部常量）——可能正改本字段，
    # 静态无法确认，交段二大模型顺锚点读源码定性是否含本字段。
    dyn = ft.get("dynamic_writers") or {}
    if dyn.get("total"):
        bc = dyn.get("by_cause") or {}
        lines.append("")
        lines.append("─" * 72)
        lines.append(
            f"▼ 动态写入候选（本单据内字段 key 钉不出，可能正改本字段，需大模型读源码定性）："
            f"{dyn['total']} 处 → {dyn.get('total_methods', 0)} 个方法"
        )
        lines.append(
            "  成因: " + "、".join(
                f"{_CAUSE_LABEL.get(c, c)} {bc.get(c, 0)}"
                for c in _DYN_CAUSES if bc.get(c)))
        lines.append("  去这几个方法读源码，判定是否含本字段（按写入数降序）：")
        for m in dyn.get("methods", [])[:max_list]:
            cls = (m["class_fqn"] or "?").rsplit(".", 1)[-1]
            lines.append(f"  · {cls}.{m['method']}  ({m['count']} 处/{m['cause_label']})")
            for w in m["writes_in"]:
                lines.append(f"        写入位于 {w['class']}  {w['anchor']}")
        if dyn.get("capped"):
            lines.append(f"    …另有 {dyn['capped']} 个方法未列出（用 dynwrites --form 过滤、或 trace --json）")
        lines.append("    注：这些写入静态钉不出具体字段，是否含本字段请让大模型按上面源码锚点读源码判定。")
    return "\n".join(lines)
