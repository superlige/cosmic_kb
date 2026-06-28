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

**防 host 截断（2026-06-27）**：MCP host 对序列化后的 tool result 整体砍尾部。早期 dict 把 `content`
排在 `field_names` 之前、尾部正好是不可替代的标注——被砍的恰是该留的，模型退回猜名。现在两手：
① **标注在前、源码垫底**——`content` 排到 dict 最末，被截断时牺牲可重读的源码而非标注；命中 key 超
`max_keys` 的省略数提到顶层 `keys_omitted`（截断也能从头部看到）。② **坐标瘦身**——每条坐标只留标名/
消歧必需字段、每个 key 的候选数封顶（`max_coords`），消化 ambiguous 档几十条同名候选平铺的体积大头。

延续 report 包约定：dict 在前（供 --json/MCP），`render_*` 文本在后。
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import resolve_fields, source_read
# 复用 trace 的「host 口径字节度量 + 游标解析 + 预算/哨兵」单一事实源（红线 #6：度量逻辑只此一份）。
from .field_trace import _wire_len, _parse_cursor, _COMPACT_BUDGET, _BIG_CAP
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


def _form_ctx_for_file(conn, relpath: str) -> dict[str, dict[str, dict[str, Any]]]:
    """本文件 `field_access` 解析出的「key → 数据包来源实体(form_key) → {行号, 来源种类}」。

    form_key 是段一按数据包来源解析出的**实际操作实体**（ORM load 取到的实体/事件入参绑定单据/
    跨实体传播；判不出为 NULL），正是给 read_source 收敛同名候选的权威依据。NULL（未定位）不入表，
    它帮不了消歧。字段访问按 `field_key` 命中，分录容器按 `entry_key` 命中（容器 key 也要消歧）。

    `form_key_source` 区分来源依据：`data_flow`=数据流证明；`metadata_*`=字段 key 反查元数据回填
    （依据是字段归属、非数据流行号）。同 (key, form) 多行混源时以 data_flow 为准（更强）。
    """
    ctx: dict[str, dict[str, dict[str, Any]]] = {}
    for r in conn.execute(
        "SELECT field_key, entry_key, form_key, line, form_key_source FROM field_access "
        "WHERE source_relpath=? AND form_key IS NOT NULL",
        (relpath,),
    ):
        src = r["form_key_source"] or "data_flow"
        for k in (r["field_key"], r["entry_key"]):
            if k:
                cell = ctx.setdefault(k, {}).setdefault(r["form_key"], {"lines": set(), "srcs": set()})
                cell["lines"].add(r["line"])
                cell["srcs"].add(src)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for k, forms in ctx.items():
        out[k] = {}
        for f, cell in forms.items():
            src = "data_flow" if "data_flow" in cell["srcs"] else sorted(cell["srcs"])[0]
            out[k][f] = {"lines": sorted(cell["lines"]), "src": src}
    return out


def _distinct(values) -> list[str]:
    """保序去重 + 去空（中文名列表，照抄用）。"""
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


# 每个 key 最多内联多少条坐标（封顶防爆——ambiguous 档常几十条同名候选平铺，是 field_names 体积大头）。
_MAX_COORDS = 8


def _trim_coord(c: dict[str, Any]) -> dict[str, Any]:
    """坐标瘦身：只留标名/消歧/取值语义必需字段，丢 entity_key/field_kind/parent_key 等纯体积字段。

    保留 `field_type`+`access`（判 getDynamicObjectCollection 取值语义的载荷信号，短、必留）与
    `resolved_lines`（resolved 档注入的本文件命中行号，消歧依据）。完整坐标见 resolve_fields。
    """
    out = {"kind": c.get("kind"), "name": c.get("name"),
           "form_key": c.get("form_key"), "level": c.get("level")}
    for opt in ("field_type", "access", "resolved_lines"):
        if c.get(opt):
            out[opt] = c[opt]
    return out


def _cap_coords(coords: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """裁剪每条坐标 + 候选数封顶。返回 (裁剪后列表, 被截断的剩余数)。"""
    trimmed = [_trim_coord(c) for c in coords]
    return trimmed[:_MAX_COORDS], max(0, len(trimmed) - _MAX_COORDS)


def _classify_key(
    key: str, candidates: list[dict[str, Any]] | None,
    form_ctx: dict[str, dict[str, list[int]]],
) -> dict[str, Any] | None:
    """把一个 key 的元数据候选 + 本文件解析结果，归为 unique / resolved / ambiguous 三档。

    坐标统一走 `_cap_coords` 瘦身+封顶（防 field_names 体积爆掉被 host 截断）；超出封顶时
    带 `coordinates_capped` 计数，note 提示全部坐标见 resolve_fields。
    """
    if not candidates:
        return None   # 非元数据字段（token 命中但词典查不到）/ 钉不出 → 留空，不臆造

    forms = {c.get("form_key") for c in candidates if c.get("form_key")}
    # 同一单据内（含「字段 + 同名分录容器」双命中）：无跨单据歧义，照抄即可。
    if len(forms) <= 1:
        coords, capped = _cap_coords(candidates)
        out = {"tier": "unique", "names": _distinct(c.get("name") for c in candidates),
               "coordinates": coords, "note": None}
        if capped:
            out["coordinates_capped"] = capped
        return out

    # 跨多张单据有同名字段 → 用本文件 field_access 解析到的实体收敛。
    resolved_forms = form_ctx.get(key, {})
    matched = [c for c in candidates if c.get("form_key") in resolved_forms]
    if matched:
        srcs: set[str] = set()
        for c in matched:
            info = resolved_forms.get(c.get("form_key"), {})
            c["resolved_lines"] = info.get("lines", [])
            srcs.add(info.get("src") or "data_flow")
        also = sorted({c.get("form_key") for c in candidates
                       if c.get("form_key") and c.get("form_key") not in resolved_forms})
        also_note = f"另有同名字段在：{', '.join(also)}。" if also else ""
        # 全凭字段归属元数据反查收敛时，诚实标明依据非数据流（resolved_lines 仅读写所在行）。
        if srcs and all(s.startswith("metadata") for s in srcs):
            note = ("本文件按**字段归属元数据反查**把此标识收敛到上述单据（数据流未追到来源；"
                    "resolved_lines 仅为读写所在行，非数据流来源行）。" + also_note)
        else:
            note = "本文件 field_access 按数据包来源把此标识解析到上述单据；其余同名字段未在本文件命中。"
            if any(s.startswith("metadata") for s in srcs):
                note += "（其中部分单据依据是字段归属元数据反查而非数据流。）"
            note += also_note
        coords, capped = _cap_coords(matched)
        out = {
            "tier": "resolved",
            "names": _distinct(c.get("name") for c in matched),
            "coordinates": coords,
            "also_in_forms": also,
            "note": note,
        }
        if capped:
            out["coordinates_capped"] = capped
        return out

    # 多单据同名、本文件又没解析到具体实体 → 显式歧义，给消歧方向，绝不替选。
    coords, capped = _cap_coords(candidates)
    out = {
        "tier": "ambiguous",
        "names": [],
        "coordinates": coords,
        "note": ("歧义：元数据有多个同名字段，本文件静态分析未把此标识解析到具体实体。归属取决于"
                 "接收变量来源（当前 dataEntity / loadSingle 加载的别的实体 / getAllSonList 等），"
                 "请顺调用链消歧，勿默认当前单据。"),
    }
    if capped:
        out["coordinates_capped"] = capped
        out["note"] += f" 另有 {capped} 条同名坐标未列出，全部见 resolve_fields。"
    return out


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
            "需顺调用链消歧，勿默认当前单据。未列出的 `<isv>_` 标识用 resolve_fields 核对，勿按命名惯例猜。"
            " getDynamicObjectCollection(key) 取分录行还是多选基础资料集合，取决于 key——看坐标 "
            "field_type/access，勿凭 API 名断定是分录。"
            " 注：`content`（源码正文）排在返回末尾——若 host 截断了它，重读更窄的 --lines 区间即可；"
            "`field_names` 在前不会丢。")
    if capped:
        note += f" 另有 {capped} 个命中 key 未标注（超 max_keys），见顶层 keys_omitted，按需 resolve_fields 补。"

    # 字段顺序刻意安排：标注（field_names）+ keys_omitted 在前，content 垫底。
    # host 砍尾部时牺牲的是可重读的源码，不可替代的字段标注存活（见模块 docstring「防 host 截断」）。
    return {
        "found": True,
        "relpath": relpath,
        "source_root": root,
        "encoding": enc,
        "total_lines": total,
        "lines": sliced,
        "keys_omitted": capped,
        "field_names": field_names,   # {key: {tier, names, coordinates, note, [coordinates_capped]} | None}
        "note": note,
        "content": body,
    }


# ── 紧凑投影（MCP 防截断）：标注在前 + content 按字节预算填充 + 游标分页 ────────────────
# 富 read_source 把整文件 `content` 整串返回——大文件（或大区间）经 MCP 会被 host 在 32KB 处从
# 中段**硬切**，被砍的恰是不可重读的尾部。早期靠「标注在前、content 垫底」只能保住标注、源码仍被
# 静默截断。本投影与 trace_compact/bill_compact 同款治理：
#   ① 标注优先——`field_names`（高价值、有界）排在前，按档保住；
#   ② content 弹性填充——按 host 口径 `_wire_len` 把源码行装到预算上限，未读全带 `content_next_cursor`；
#   ③ 游标分页——`read_source(relpath, cursor='content@120')` 从该行**续读至文件末尾**，逐页取回；
#      `field_names` 超档同法翻页（红线 #4：被截内容可达，不只报计数）。
# 富 read_source 不动（CLI/Web 走终端/HTTP 无 32KB 限制，仍用富投影）。
_RS_PAGE_SECTIONS = ("content", "field_names")
# field_names 展示档（从宽到窄）：留出 content 最低余量，field_names 是高价值有界部分、优先保住。
_RS_FIELD_CAPS = (60, 40, 25, 15, 8, 4, 1)
_RS_MIN_CONTENT_BUDGET = 4000   # 至少给源码正文留这么多字节（否则只见标注不见码）

_RS_COMPACT_NOTE = (
    "紧凑投影（防 MCP 32KB 截断）：字段名标注 `field_names` 在前（高价值、有界），源码正文 `content` "
    "按字节预算填充。`content` 未读全时带 `content_next_cursor`（如 `content@120`）——把该值原样作 "
    "`cursor=` 再调 `read_source(relpath, cursor=该值)` 即从该行**续读至文件末尾**（逐页 `page.content` + "
    "新 `next_cursor`，到 null 读完）；要限定上界改用 `end_line` 重调。`field_names` 超档时带 "
    "`field_names_next_cursor`（同法翻页取回全部标注）。字段名按三档置信：✅ unique/resolved 照抄；"
    "⚠️ ambiguous 多单据同名、本文件未解析到实体，勿默认当前单据，按各 key 的 note 顺调用链消歧。"
    " getDynamicObjectCollection(key) 取分录行还是多选基础资料集合取决于 key——看坐标 field_type/access，"
    "勿凭 API 名断定是分录。"
)


def _content_str_bytes(base_bytes: int, content: str) -> int:
    """已知空 content 时的 base 字节，反推填入 content 后的总字节。

    content 是返回 dict 里**唯一的弹性 scalar**：indent=2 不会缩进字符串值内部、换行在 JSON 里转义为
    `\\n`，故换 content 值不改其余字段的序列化字节——总字节 = base_bytes - len('""') + len(json.dumps(content))。
    这样二分行数只需对 content 串做 json.dumps，避免每步整 dict 重新序列化（O(n²)→O(n log n)）。
    """
    return base_bytes - 2 + len(json.dumps(content, ensure_ascii=True))


def _rs_overview(
    rich: dict[str, Any], fn_shown: dict[str, Any], *, fn_omitted: int,
    s: int, e: int, total: int, content: str, shown_to: int, content_more: bool,
) -> dict[str, Any]:
    """组装紧凑 overview dict（标注在前、content 垫底；按需补 next_cursor）。"""
    res: dict[str, Any] = {
        "found": True,
        "relpath": rich["relpath"],
        "source_root": rich.get("source_root"),
        "encoding": rich.get("encoding"),
        "total_lines": total,
        "lines": [s, shown_to] if total else None,
        "keys_omitted": fn_omitted,
        "field_names": fn_shown,
        "note": _RS_COMPACT_NOTE,
    }
    if fn_omitted:
        res["field_names_next_cursor"] = f"field_names@{len(fn_shown)}"
    if content_more:
        res["content_capped_lines"] = max(0, e - shown_to)
        res["content_next_cursor"] = f"content@{shown_to + 1}"
    res["content"] = content   # 垫底：host 截尾时牺牲可续读的源码，不可替代的标注存活
    return res


def _build_rs_compact(rich: dict[str, Any], start: int | None, end: int | None,
                      budget: int) -> dict[str, Any]:
    """一次构建紧凑 overview：先选 field_names 展示档（留 content 余量），再按字节预算二分填 content。"""
    # 用 splitlines() 与富层 total_lines 同口径切行（split("\n") 会因文件尾换行多出空行、错位）。
    all_lines = rich["content"].splitlines()
    total = len(all_lines)
    s = max(1, start or 1)
    e = min(total, end or total) if total else 0
    window = all_lines[s - 1:e] if total else []
    fn_full = list(rich["field_names"].items())

    # 选 field_names 展示档：阶梯从宽到窄，留出 content 最低余量（标注优先保住）。
    fn_cap = min(_RS_FIELD_CAPS[-1], len(fn_full))
    for cap in _RS_FIELD_CAPS:
        cap = min(cap, len(fn_full))
        probe = _rs_overview(rich, dict(fn_full[:cap]), fn_omitted=len(fn_full) - cap,
                             s=s, e=e, total=total, content="", shown_to=e, content_more=True)
        if _wire_len(probe) <= budget - _RS_MIN_CONTENT_BUDGET:
            fn_cap = cap
            break
    fn_shown = dict(fn_full[:fn_cap])
    fn_omitted = len(fn_full) - fn_cap

    # content 弹性填充：base（content=""，worst-case 带 cursor）字节定后，二分窗口内最大可容行数。
    base_bytes = _wire_len(_rs_overview(
        rich, fn_shown, fn_omitted=fn_omitted, s=s, e=e, total=total,
        content="", shown_to=e, content_more=True))
    lo, hi = 0, len(window)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _content_str_bytes(base_bytes, "\n".join(window[:mid])) <= budget:
            lo = mid
        else:
            hi = mid - 1
    k = lo if lo or not window else 1   # 至少给一行（单行即便超 budget 也给，仍远小于 32KB host 上限）
    shown = window[:k]
    content_more = k < len(window)
    shown_to = (s + k - 1) if k else e
    return _rs_overview(rich, fn_shown, fn_omitted=fn_omitted, s=s, e=e, total=total,
                        content="\n".join(shown), shown_to=shown_to, content_more=content_more)


def _rs_page_content(rich: dict[str, Any], offset: int, budget: int) -> dict[str, Any]:
    """content 续读：从绝对行号 offset 起按预算装入源码行，**续读至文件末尾**（逐页可取全）。"""
    all_lines = rich["content"].splitlines()   # 与 total_lines 同口径，见 _build_rs_compact
    total = len(all_lines)
    start = min(max(1, offset or 1), total + 1)
    base = {"found": True, "relpath": rich["relpath"], "encoding": rich.get("encoding"),
            "total_lines": total}

    def _wrap(k: int) -> dict[str, Any]:
        end_idx = start - 1 + k
        return {**base, "page": {
            "section": "content", "from_line": start,
            "to_line": (start + k - 1) if k else start - 1, "total_lines": total,
            "content": "\n".join(all_lines[start - 1:end_idx]),
            "next_cursor": (f"content@{end_idx + 1}" if end_idx < total else None)}}

    avail = max(0, total - (start - 1))
    base_bytes = _wire_len(_wrap(0))
    lo, hi = 0, avail
    while lo < hi:
        mid = (lo + hi + 1) // 2
        body = "\n".join(all_lines[start - 1:start - 1 + mid])
        if _content_str_bytes(base_bytes, body) <= budget:
            lo = mid
        else:
            hi = mid - 1
    k = lo if lo or not avail else 1
    return _wrap(k)


def _rs_page_field_names(rich: dict[str, Any], offset: int, budget: int) -> dict[str, Any]:
    """field_names 翻页：从 offset 起按预算装入已分类的字段标注条目（`{key, info}`）。"""
    items = [{"key": k, "info": v} for k, v in rich["field_names"].items()]
    total = len(items)
    offset = min(max(0, offset), total)
    base = {"found": True, "relpath": rich["relpath"]}

    def _wrap(page: list[dict[str, Any]], nxt: int) -> dict[str, Any]:
        return {**base, "page": {"section": "field_names", "offset": offset,
                "returned": len(page), "total": total, "items": page,
                "next_cursor": (f"field_names@{nxt}" if nxt < total else None)}}

    page: list[dict[str, Any]] = []
    for it in items[offset:]:
        trial = page + [it]
        if page and _wire_len(_wrap(trial, offset + len(trial))) > budget:
            break          # 至少装一条（单条即便超 budget 也给，仍远小于 32KB）
        page = trial
    return _wrap(page, offset + len(page))


def read_source_compact(
    conn, relpath: str, *,
    source_root: str | None = None,
    start: int | None = None, end: int | None = None,
    cursor: str | None = None, budget: int = _COMPACT_BUDGET,
) -> dict[str, Any]:
    """**紧凑投影**（MCP 入口，防 host 32KB 截断）：标注在前 + content 按字节预算填充 + 游标分页。

    - overview：`field_names`（按档保住）+ 窗口 `[start,end]`（默认整文件）内预算填得下的 `content`；
      未读全带 `content_next_cursor`，`field_names` 超档带 `field_names_next_cursor`。
    - `cursor`（`"content@120"` / `"field_names@60"`）：把 overview 给出的 next_cursor 原样传回即翻页——
      content 从该行**续读至文件末尾**，field_names 续取后续标注；循环到 `next_cursor` 为 null 即取全。
    - governor 按 host 真实序列化口径（`_wire_len` = json.dumps indent=2）度量，保证永不被截断（红线 #4）。
    """
    # 取整文件富材料（max_keys 放开 → 全部字段分类，供分页 slice；whole-file 让游标无状态一致）。
    rich = read_source(conn, relpath, source_root=source_root, max_keys=_BIG_CAP)
    if not rich.get("found"):
        return rich   # 错误 dict 很小，直接回
    if cursor:
        section, offset = _parse_cursor(cursor)
        if section == "content":
            return _rs_page_content(rich, offset, budget)
        if section == "field_names":
            return _rs_page_field_names(rich, offset, budget)
        return {"found": True, "relpath": relpath, "page": {
            "section": section,
            "error": f"未知或不可分页的 section: {section}（可分页：{', '.join(_RS_PAGE_SECTIONS)}）"}}
    return _build_rs_compact(rich, start, end, budget)


def _coord_line(key: str, c: dict[str, Any], *, mark: str, suffix: str = "") -> str:
    name = c.get("name") or ""
    form = c.get("form_key") or "?"
    lvl = _LEVEL_CN.get(c.get("level") or "", c.get("level") or "?")
    kind = "字段" if c.get("kind") == "field" else "容器"
    ft = f" · {c['field_type']}" if c.get("field_type") else ""
    access = f"  〔{c['access']}〕" if c.get("access") else ""
    return f"  {mark} {kind} {key}「{name}」 — {form} · {lvl}{ft}{suffix}{access}"


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
            capped = info.get("coordinates_capped")
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
                lines.append(f"  ⚠️ {key}: 歧义 — 元数据有多个同名字段"
                             f"（{forms}），本文件未解析到具体实体，需顺调用链消歧、勿默认当前单据：")
                for c in info["coordinates"][:max_list]:
                    lines.append(_coord_line(key, c, mark="·"))
            if capped:
                lines.append(f"      （另 {capped} 条坐标未列出，全部见 resolve_fields）")
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
