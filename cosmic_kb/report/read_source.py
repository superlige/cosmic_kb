"""模式 A · 读项目源码并自动标注字段中文名（让段二大模型读源码走我们的工具，别用宿主原生 reader）。

起因：模式 B 把已核对字段名焊进 trace/bill/ask/method_calls 的返回值，但只在模型流经这些工具时生效。
真实样本里模型走的是「method_calls 导航 → 宿主原生 reader 直接读源码 → 字段 key 只在源码正文里出现 → 猜」。
要堵这条，唯一办法是让"读源码"这一步也走我们的工具。

凭什么模型肯用而非原生 reader？给两个原生 reader 给不了的硬价值（红线 #2 野生码）：
  ① **正确解码**：GBK/GB2312/UTF-8±BOM 混杂，原生 reader 易乱码；本工具按建库同款编码探测，行号还与 KB 对齐。
  ② **自动标注**：扫文件里出现的字段 key（含 `KEY_X = "cqkd_x"` 的字面值——它就在源码正文里，无需常量表），
     直接打元数据词典回真实中文名+坐标，引用照抄即可，不必再按拼音猜。

**消歧（2026-06-26 加固）**：苍穹里同一个 `<isv>_xxx` key 常在多张单据各有定义（名字甚至不同）。早期版本把
全部同名候选平铺给模型，反而诱导它脑补归属（严重误导）。现按**三档置信**标注，依据是本文件 `field_access`
解析出的**数据包来源实体**（`form_key`——经 ORM load/事件入参/跨实体传播解析，见 java/field_access.py）：
  · ✅ unique    —— 元数据里此 key 只属一张单据，无歧义，照抄。
  · ✅ resolved  —— 多张单据有同名字段，但**本文件**的 field_access 已把它解析到具体单据（含跨实体 load
                    的情形，如 `loadSingle(htid, KEY_CONTRACT)` 取到的合同侧字段）→ 收敛到那张单据并附依据行号。
  · ⚠️ ambiguous —— 多张单据有同名字段、本文件又没解析到具体实体（纯常量定义/动态访问）→ **显式标歧义**，
                    列候选 + 指出消歧方向（看接收变量来源 dataEntity / loadSingle / getAllSonList），绝不替选、
                    不默认当前单据（红线 #4 处处置信度 + unknown）。

不替模型"理解代码/复述逻辑"（那是它直接读的本职），只做"正确解码 + 字段名标注 + 归属消歧"。**仍是引导
非强制**：模型若坚持用原生 reader、连本工具都不调，host-agnostic 拦不住——见 docs/阶段验收.md 天花板记录。

延续 report 包约定：dict 在前（供 --json/MCP），`render_*` 文本在后。
"""

from __future__ import annotations

import re
from typing import Any

from . import resolve_fields, source_read
from .resolve_fields import _LEVEL_CN

# 字段标识 token：字母开头、含下划线的标识符（cqkd_tzrq / KEY_TZRQ 都会被切出；与 KB 已知 key 取交集才算命中）。
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


def _known_keys(conn) -> set[str]:
    """KB 里所有字段 key + 实体/分录容器 key（一次性建集合，供扫源码取交集）。"""
    keys: set[str] = set()
    for tbl in ("field", "entity"):
        keys |= {r[0] for r in conn.execute(
            f"SELECT DISTINCT key FROM {tbl} WHERE key IS NOT NULL") if r[0]}
    return keys


def _form_ctx_for_file(conn, relpath: str) -> dict[str, dict[str, list[int]]]:
    """本文件 `field_access` 解析出的「key → 数据包来源实体(form_key) → 命中行号」。

    form_key 是段一按数据包来源解析出的**实际操作实体**（ORM load 取到的实体/事件入参绑定单据/
    跨实体传播；判不出为 NULL），正是给 read_source 收敛同名候选的权威依据。NULL（未定位）不入表，
    它帮不了消歧。字段访问按 `field_key` 命中，分录容器按 `entry_key` 命中（容器 key 也要消歧）。
    """
    ctx: dict[str, dict[str, set[int]]] = {}
    for r in conn.execute(
        "SELECT field_key, entry_key, form_key, line FROM field_access "
        "WHERE source_relpath=? AND form_key IS NOT NULL",
        (relpath,),
    ):
        for k in (r["field_key"], r["entry_key"]):
            if k:
                ctx.setdefault(k, {}).setdefault(r["form_key"], set()).add(r["line"])
    return {k: {f: sorted(lines) for f, lines in forms.items()} for k, forms in ctx.items()}


def _distinct(values) -> list[str]:
    """保序去重 + 去空（中文名列表，照抄用）。"""
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


def _classify_key(
    key: str, candidates: list[dict[str, Any]] | None,
    form_ctx: dict[str, dict[str, list[int]]],
) -> dict[str, Any] | None:
    """把一个 key 的元数据候选 + 本文件解析结果，归为 unique / resolved / ambiguous 三档。"""
    if not candidates:
        return None   # 非元数据字段（token 命中但词典查不到）/ 钉不出 → 留空，不臆造

    forms = {c.get("form_key") for c in candidates if c.get("form_key")}
    # 同一单据内（含「字段 + 同名分录容器」双命中）：无跨单据歧义，照抄即可。
    if len(forms) <= 1:
        return {"tier": "unique", "names": _distinct(c.get("name") for c in candidates),
                "coordinates": candidates, "note": None}

    # 跨多张单据有同名字段 → 用本文件 field_access 解析到的实体收敛。
    resolved_forms = form_ctx.get(key, {})
    matched = [c for c in candidates if c.get("form_key") in resolved_forms]
    if matched:
        for c in matched:
            c["resolved_lines"] = resolved_forms.get(c.get("form_key"), [])
        also = sorted({c.get("form_key") for c in candidates
                       if c.get("form_key") and c.get("form_key") not in resolved_forms})
        return {
            "tier": "resolved",
            "names": _distinct(c.get("name") for c in matched),
            "coordinates": matched,
            "also_in_forms": also,
            "note": ("本文件 field_access 按数据包来源把此标识解析到上述单据；其余同名字段"
                     "未在本文件命中。" + (f"另有同名字段在：{', '.join(also)}。" if also else "")),
        }

    # 多单据同名、本文件又没解析到具体实体 → 显式歧义，给消歧方向，绝不替选。
    return {
        "tier": "ambiguous",
        "names": [],
        "coordinates": candidates,
        "note": ("歧义：元数据有多个同名字段，本文件静态分析未把此标识解析到具体实体。归属取决于"
                 "接收变量来源（当前 dataEntity / loadSingle 加载的别的实体 / getAllSonList 等），"
                 "请顺调用链消歧，勿默认当前单据。"),
    }


def read_source(
    conn, relpath: str, *,
    source_root: str | None = None,
    start: int | None = None, end: int | None = None,
    max_keys: int = 60,
) -> dict[str, Any]:
    """读源文件（野生编码正确解码）+ 按三档置信标注其中字段 key 的真实中文名与归属。

    `start/end`（1 基，含端点）可只读一段；越界路径（../ 逃逸出源码根）一律拒绝，不读项目外文件。
    `field_names` 形状：`{key: {tier, names, coordinates, note, [also_in_forms]} | None}`——
    tier ∈ unique/resolved/ambiguous，ambiguous 的 `names` 为空（需消歧，勿照抄某一个）。
    """
    root = source_read.resolve_source_root(conn, source_root)
    if not root:
        return {"found": False, "relpath": relpath,
                "note": "源码根未配置：加 --source-root <源码根>，或确认 KB 的 source_args 指向有效源码。"}
    if not source_read.within_root(root, relpath):
        return {"found": False, "relpath": relpath,
                "note": "路径越界：relpath 必须在源码根之内（拒绝 ../ 逃逸读项目外文件）。"}

    text, enc = source_read.read_text(root, relpath)
    if text is None:
        return {"found": False, "relpath": relpath, "source_root": root,
                "note": "文件不存在或读取失败。"}

    all_lines = text.splitlines()
    total = len(all_lines)
    sliced = None
    body = text
    if start or end:
        s = max(1, start or 1)
        e = min(total, end or total)
        sliced = [s, e]
        body = "\n".join(all_lines[s - 1:e])

    # 扫本段文本里出现的、KB 已知的字段/容器 key → 喂 resolve_fields 拿真实中文名+坐标（复用，不重写），
    # 再用本文件 field_access 解析出的来源实体收敛同名候选（三档置信，杜绝平铺误导）。
    known = _known_keys(conn)
    found = sorted({t for t in _TOKEN_RE.findall(body) if t in known})
    capped = max(0, len(found) - max_keys)
    keys = found[:max_keys]
    raw = resolve_fields.resolve_fields(conn, keys)["resolved"]
    form_ctx = _form_ctx_for_file(conn, relpath)
    field_names = {k: _classify_key(k, raw.get(k), form_ctx) for k in keys}

    note = ("字段名按三档置信标注：✅ 直接照抄；⚠️ ambiguous 表示本文件未解析到具体实体、有多张单据同名，"
            "需顺调用链消歧，勿默认当前单据。未列出的 `<isv>_` 标识用 resolve_fields 核对，勿按命名惯例猜。")
    if capped:
        note += f" 另有 {capped} 个命中 key 未标注（超 max_keys），按需 resolve_fields 补。"

    return {
        "found": True,
        "relpath": relpath,
        "source_root": root,
        "encoding": enc,
        "total_lines": total,
        "lines": sliced,
        "content": body,
        "field_names": field_names,   # {key: {tier, names, coordinates, note} | None}
        "note": note,
    }


def _coord_line(key: str, c: dict[str, Any], *, mark: str, suffix: str = "") -> str:
    name = c.get("name") or ""
    form = c.get("form_key") or "?"
    lvl = _LEVEL_CN.get(c.get("level") or "", c.get("level") or "?")
    kind = "字段" if c.get("kind") == "field" else "容器"
    return f"  {mark} {kind} {key}「{name}」 — {form} · {lvl}{suffix}"


def render_read_source(data: dict[str, Any], *, max_list: int = 60) -> str:
    """文本视图：先印按三档置信标注的字段名块，再印带行号的源码。"""
    if not data.get("found"):
        return f"读取失败: {data.get('relpath')} —— {data.get('note') or ''}"
    lines: list[str] = []
    rng = f"  行 {data['lines'][0]}–{data['lines'][1]}/{data['total_lines']}" if data.get("lines") \
        else f"  共 {data['total_lines']} 行"
    lines.append("=" * 72)
    lines.append(f"源码: {data['relpath']}  [{data.get('encoding')}]{rng}")
    lines.append("=" * 72)
    fn = data.get("field_names") or {}
    if fn:
        lines.append("【已核对字段名】（✅ 照抄即可；⚠️ 歧义需结合调用链消歧，勿默认当前单据）")
        for key, info in fn.items():
            if info is None:
                lines.append(f"  · {key}: null（非 KB 字段/钉不出，勿臆造）")
                continue
            tier = info["tier"]
            if tier in ("unique", "resolved"):
                for c in info["coordinates"][:max_list]:
                    rl = c.get("resolved_lines")
                    suffix = f"（本文件解析到，行 {','.join(map(str, rl))}）" if rl else ""
                    lines.append(_coord_line(key, c, mark="✅", suffix=suffix))
                if info.get("also_in_forms"):
                    lines.append(f"      （另有同名字段在 {', '.join(info['also_in_forms'])}，本文件未命中）")
            else:  # ambiguous
                forms = ", ".join(sorted({c.get("form_key") for c in info["coordinates"]
                                          if c.get("form_key")}))
                lines.append(f"  ⚠️ {key}: 歧义 — 元数据有 {len(info['coordinates'])} 个同名字段"
                             f"（{forms}），本文件未解析到具体实体，需顺调用链消歧、勿默认当前单据：")
                for c in info["coordinates"][:max_list]:
                    lines.append(_coord_line(key, c, mark="·"))
    else:
        lines.append("（本文件未出现 KB 已知字段标识）")
    if data.get("note"):
        lines.append(f"  {data['note']}")
    lines.append("─" * 72)
    # 带行号的源码（起始行号对齐切片）。
    base = (data["lines"][0] if data.get("lines") else 1)
    for i, ln in enumerate(data["content"].splitlines()):
        lines.append(f"{base + i:>5}  {ln}")
    return "\n".join(lines)
