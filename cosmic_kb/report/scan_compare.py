"""扫描可信度报告 · 手段二「粗精度扫描 vs 高精度扫描对比」。

红线 #4「信任优先」。手段一（`coverage.py`）用元数据当分母量「覆盖了多少字段」；本报告
换一个角度回答接手者第二个信任问题：**「我那套又慢又复杂的高精度扫描，会不会静默漏掉字段？」**

两侧定义：
  - **高精度扫描** = `field_access`（AST + 跨类回溯 + 轻量数据流 + 落库判定，带实体坐标）。
    贵、准，但链路长、规则多，理论上可能在某些写法上漏归因。
  - **粗精度扫描** = `coarse_field_hit`（单遍 Java 词法扫描，跳过注释/字符串内部：把业务字段
    标识当**字符串字面量**、或解析回该字段的**唯一常量名引用**搜出来，不解析坐标/落库/调用链）。
    便宜、笨，但**召回高**——字段标识以字面量出现、或经常量引用（`getValue(BillConst.X)`），它都抓得到。

把两侧一比即得交叉验证（均锚定「业务字段」全集 U = entity/dynamic/basedata_prop，与覆盖率分母同口径）：
  - **两者都见**：一致，互证可信。
  - **仅粗扫见（coarse_only）**：字段标识在源码里出现了，高精度却没记任何 field_access ——
    **疑似盲点（召回风险）**，是本报告最有价值的产物，值得人工核对。其中**强信号**
    （`rw-idiom`/`const-rw-idiom`：字面量或常量名正好作 get/set/getValue/setValue 首参）最该
    先看；**弱信号**（`literal`/`const-ref`：只是出现，可能是 `load("实体")` 实参、常量定义等，
    未必是漏检）。**诚实声明：coarse_only 是「候选」不是「确诊」**——纯文本比对天生有误报，
    不臆造成「确定漏扫」。
  - **仅高精度见（high_only）**：粗扫连字面量带常量名都没逮到（字段 key 多由字符串拼接/外部
    常量得到）。这是高精度扫描**优于一把 grep**的精度增量。

延续 report 包约定：dict 在前（供 --json / Web `/api/scan-compare`），`render_*` 文本在后。
"""

from __future__ import annotations

from typing import Any

from .coverage import BUSINESS_KINDS  # 业务字段口径与手段一保持单一来源

# coarse_only 证据：每个疑似盲点字段最多带几处源码位置。验收要求「不能遗漏」，故放宽到
# 能覆盖盲点的全部证据（真实库强信号盲点仅个位数～十几个，命中处也有限）；超出则 hits 给出
# 总数、web/CLI 标「+N」。
MAX_LOCATIONS = 50


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def compare(conn) -> dict[str, Any]:
    """从 KB 连接组装「粗扫 vs 高精度」对比 dict（信任手段二）。"""
    import json

    java = json.loads(_meta(conn, "java_analysis") or "{}")
    java_available = java.get("available", True)

    placeholders = ",".join("?" * len(BUSINESS_KINDS))

    # ── 业务字段全集 U + 名称/归属单据（一个 key 可能跨多单据，名取其一、单据汇总）──
    field_name: dict[str, str] = {}
    field_forms: dict[str, set[str]] = {}
    for r in conn.execute(
        f"SELECT key, name, form_key FROM field "
        f"WHERE kind IN ({placeholders}) AND key IS NOT NULL",
        BUSINESS_KINDS,
    ):
        k = r["key"]
        field_name.setdefault(k, r["name"] or "")
        field_forms.setdefault(k, set()).add(r["form_key"])
    universe = set(field_name)

    # ── 高精度侧 H：被 field_access 记到的业务字段 ────────────────────────────
    high = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT field_key FROM field_access WHERE field_key IS NOT NULL")
        if r[0] in universe
    }

    # ── 粗扫侧 C：命中（按字段聚合命中处数 + 是否含强信号读写习语 + 是否含常量名引用）──
    #   证据排序：强信号（rw-idiom/const-rw-idiom）优先——盲点被标「读写习语」时，展示的位置
    #   就该是那处读写调用，而非该字段在常量类里的定义（弱信号），让 flag 与证据一致、用户
    #   跳过去就能核对。
    coarse_hits: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT field_key, relpath, line, via FROM coarse_field_hit "
        "ORDER BY field_key, "
        "CASE WHEN via IN ('rw-idiom','const-rw-idiom') THEN 0 ELSE 1 END, relpath, line"
    ):
        k = r["field_key"]
        if k not in universe:
            continue
        agg = coarse_hits.setdefault(
            k, {"count": 0, "idiom": False, "const": False, "locations": []})
        agg["count"] += 1
        if r["via"] in ("rw-idiom", "const-rw-idiom"):
            agg["idiom"] = True
        if r["via"] in ("const-rw-idiom", "const-ref"):
            agg["const"] = True
        if len(agg["locations"]) < MAX_LOCATIONS:
            agg["locations"].append(
                {"relpath": r["relpath"], "line": r["line"], "via": r["via"]})
    coarse = set(coarse_hits)

    both = coarse & high
    coarse_only = coarse - high
    high_only = high - coarse
    covered_either = coarse | high
    neither = universe - covered_either

    # ── 疑似盲点清单（rw-idiom 优先、命中处多者优先；最该人工核对）──────────────
    coarse_only_list = [
        {
            "key": k,
            "name": field_name.get(k) or None,
            "forms": sorted(f for f in field_forms.get(k, ()) if f),
            "hits": coarse_hits[k]["count"],
            "idiom": coarse_hits[k]["idiom"],
            "const": coarse_hits[k]["const"],   # 命中里含「常量名引用」（非纯字面量）
            "locations": coarse_hits[k]["locations"],
        }
        for k in coarse_only
    ]
    coarse_only_list.sort(key=lambda d: (not d["idiom"], -d["hits"], d["key"]))
    coarse_only_idiom = sum(1 for d in coarse_only_list if d["idiom"])
    # 含常量名引用的命中字段数（体现「复用常量表后多召回了多少靠常量引用的字段」）。
    coarse_const_hit = sum(1 for a in coarse_hits.values() if a["const"])

    # ── 精度增量清单（高精度独有 = 多为常量解析触达，体现高精度优于纯 grep）─────
    high_only_list = sorted(
        ({"key": k, "name": field_name.get(k) or None,
          "forms": sorted(f for f in field_forms.get(k, ()) if f)}
         for k in high_only),
        key=lambda d: d["key"],
    )

    result = {
        "java_available": java_available,
        "universe": len(universe),
        "coarse_hit": len(coarse),
        "coarse_const_hit": coarse_const_hit,              # 粗扫命中里含常量名引用的字段数
        "high_hit": len(high),
        "both": len(both),
        "coarse_only": len(coarse_only),
        "coarse_only_idiom": coarse_only_idiom,            # 强信号盲点（读写习语）
        "coarse_only_literal": len(coarse_only) - coarse_only_idiom,  # 弱信号（多为常量定义/弱引用）
        "high_only": len(high_only),
        "covered_either": len(covered_either),
        "neither": len(neither),
        "agreement_rate": _rate(len(both), len(covered_either)),
        "coarse_only_list": coarse_only_list,
        "high_only_list": high_only_list,
    }
    result["verdict"] = _verdict(result)
    return result


def _meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kb_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _verdict(c: dict[str, Any]) -> dict[str, str]:
    """据「粗扫见到、高精度漏掉」的占比给一句召回信任结论。"""
    if not c["java_available"]:
        return {"level": "blocked",
                "text": "⚠ tree-sitter 未启用，高精度扫描为空，无从对比。"
                        "请 pip install -e .[parse] 后重建 KB。"}
    if c["coarse_hit"] == 0:
        return {"level": "low",
                "text": "粗扫未在源码里命中任何业务字段标识（字面量或常量名引用都没逮到，"
                        "字段 key 多由拼接/外部常量得到？）——对比无参照，建议核对常量表覆盖。"}
    # 召回信任以**强信号盲点**（rw-idiom/const-rw-idiom：字段标识/常量名用在 get/set 里、
    # 高精度却没记）为准——仅以字面量/常量名出现的弱信号多是该字段在常量类里的**定义**
    # （非真实读写），不该主导红绿灯。
    strong = c["coarse_only_idiom"]
    weak = c["coarse_only_literal"]
    strong_rate = strong / max(c["high_hit"], 1)
    if strong == 0:
        lvl, head = "good", "✅ 高精度扫描召回稳"
    elif strong_rate <= 0.02:
        lvl, head = "ok", "🟡 高精度扫描召回基本可靠"
    else:
        lvl, head = "low", "🔴 高精度扫描疑似盲点偏多"
    return {"level": lvl, "text": (
        f"{head}：粗扫命中 {c['coarse_hit']} 个业务字段（其中 {c['coarse_const_hit']} 个靠常量名引用召回），"
        f"高精度覆盖到 {c['both']}（{c['both'] / c['coarse_hit']:.0%}）。强信号疑似盲点 {strong} 个"
        f"（字段标识/常量名用在 get/set 里、高精度却没记，最该人工核对）；"
        f"另有 {weak} 个仅以字面量/常量名出现（多为常量定义/弱引用）。"
        f"高精度独有 {c['high_only']}（字段 key 多由拼接/外部常量得到，粗扫逮不到）。"
        f"提示：盲点是「候选」非「确诊」——纯文本比对有误报，请跳源码核对。")}


# ── 人读文本（CLI）──────────────────────────────────────────────────────────

def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.0%}"


def _bar(v: float | None, width: int = 24) -> str:
    if v is None:
        return "[" + "·" * width + "]"
    fill = round(v * width)
    return "[" + "█" * fill + "·" * (width - fill) + "]"


def render_compare(c: dict[str, Any], *, max_list: int = 20) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("扫描可信度报告 · 手段二「粗精度扫描 vs 高精度扫描对比」")
    lines.append("=" * 72)
    lines.append(c["verdict"]["text"])
    lines.append("")

    if not c["java_available"]:
        return "\n".join(lines)

    # 集合分桶概览
    cover = _rate(c["both"], c["coarse_hit"])
    lines.append("【两侧对比】高精度=field_access(AST+跨类+落库) · 粗扫=字面量+常量名引用(召回底线)")
    lines.append(
        f"  业务字段全集 {c['universe']}　|　粗扫命中 {c['coarse_hit']}"
        f"（含常量名引用 {c['coarse_const_hit']}）　高精度命中 {c['high_hit']}"
    )
    lines.append(
        f"  {_bar(cover, 16)} {_pct(cover)}  两者都见 {c['both']}"
        f"（粗扫命中里高精度也覆盖到的占比）")
    lines.append(
        f"  ▸ 仅粗扫见（疑似盲点）{c['coarse_only']}　= 强信号 {c['coarse_only_idiom']}"
        f"（★读写习语，最该核对）+ 弱信号 {c['coarse_only_literal']}（多为常量定义/弱引用）")
    lines.append(
        f"  ▸ 仅高精度见（精度增量）{c['high_only']}　"
        f"（字段 key 多由拼接/外部常量得到，纯文本抓不到 → 高精度的价值）")
    lines.append(
        f"  ▸ 两侧都没碰 {c['neither']}　（纯展示/纯存储字段，正常）")
    lines.append("  注：盲点是「候选」非「确诊」——纯文本比对天生有误报，请跳源码核对。")

    # 疑似盲点清单（旗舰）
    col = c["coarse_only_list"]
    if col:
        lines.append("")
        lines.append(
            f"【疑似盲点：粗扫见到、高精度没记】（强信号读写习语优先，前 "
            f"{min(max_list, len(col))}/{len(col)}；源码位置全列、勿遗漏）：")
        for d in col[:max_list]:
            flag = "⚡读写习语" if d["idiom"] else "  弱引用"
            form_kind = "常量名" if d.get("const") else "字面量"
            forms = ",".join(d["forms"][:3]) + ("…" if len(d["forms"]) > 3 else "")
            nm = f"「{d['name']}」" if d.get("name") else ""
            lines.append(
                f"  {flag}({form_kind}) {(d['key'] or '?'):<28}{nm:<14} "
                f"命中 {d['hits']} 处 «{forms or '?'}»")
            for loc in d["locations"]:           # 全列，落实「不能遗漏」
                lines.append(f"        ↳ {loc['relpath']}:{loc['line']} ({loc['via']})")
            extra = d["hits"] - len(d["locations"])
            if extra > 0:
                lines.append(f"        ↳ …另有 {extra} 处未列出")

    # 精度增量清单（点到为止）
    hol = c["high_only_list"]
    if hol:
        lines.append("")
        lines.append(
            f"【精度增量：高精度独有（常量解析触达）】（前 {min(max_list, len(hol))}/{len(hol)}）：")
        for d in hol[:max_list]:
            forms = ",".join(d["forms"][:3]) + ("…" if len(d["forms"]) > 3 else "")
            nm = f"「{d['name']}」" if d.get("name") else ""
            lines.append(f"  {(d['key'] or '?'):<28}{nm:<14} «{forms or '?'}»")

    return "\n".join(lines)
