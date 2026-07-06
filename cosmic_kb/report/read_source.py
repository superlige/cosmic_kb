"""CLI 模式 A · 读项目源码并自动标注字段中文名（终端/人工排障用，`cosmic_kb source`）。

给两个宿主原生 reader 给不了的硬价值（红线 #2 野生码）：
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

**限定常量引用解析（2026-07-03）**：真实翻车案例——`TemporaryStopCon.ENTITY` 的字面值 `cqkd_ltyz`
根本不出现在被分析文件的正文里（只有常量类自己的文件才有），②的"扫正文字面值"救不了，模型转而凭
`TemporaryStopCon` 的英文语义把它猜成"临停单"（真实是"临时收入"）。现在源码正文里再扫一遍 `类.常量`
形式的限定引用，查建库期持久化的 `java_constant` 表（`java/constants.py:ConstantTable.records`）解出
字面值，解出后按同一套三档规则标注、`field_names` 就挂在该表达式本身（如 `"TemporaryStopCon.ENTITY"`），
附 `resolved_constant`（字面值+定义文件/行号）。解不出（非项目常量，如 `Boolean.TRUE`）静默跳过、不当
噪音；同名类在工程里被多处定义出不同字面值时标 ambiguous，不擅自选一个。

**表单标识兜底（2026-07-04）**：真实翻车复测——`BusinessDataServiceHelper.load("cqkd_invoic_apply", ...)`
里的字面量是**表单标识**（`form.key`），当该单据的表头实体 key 不等于表单 key 时（常见——entity 表另有
自己的 key，不像凑巧同名的样例那样两者相等），老版本 `_known_keys`/分类逻辑只查 `field`/`entity` 两表，
根本扫不到这个 token，模型只能凭标识片段谐音瞎猜表单中文名（`cqkd_invoic_apply`→"开票申请"、
`cqkd_contractbill`→"合同账单"，均为臆造）。现在 `_known_keys` 把 `form.key` 也纳入扫描候选，字段/
容器分类钉不出时再查 `form` 表兜底（`_classify_form_key`），命中标 `coordinates[].kind="form"`；同
key 多义（罕见）标 ambiguous、不擅自选一个。

**MCP 层退役（2026-07-05）**：本模块原有一层专供 MCP 的紧凑投影（两步取证协议 + 字节预算分页），
随 MCP `read_source` 工具一并下线——段二改为宿主自带 reader 读源码 + `resolve_fields` 精确核对
（见 `docs/参考手册/read_source字段名解析逻辑.md` 顶部说明、`docs/核心/阶段验收.md` 对应条目）。本文件只保留
CLI `cosmic_kb source` 用到的富模式（`read_source()`，全文盲扫 + 三档消歧），不再区分 `keys` 档位。

延续 report 包约定：dict 在前（供 --json），`render_*` 文本在后。
"""

from __future__ import annotations

import re
from typing import Any

from . import resolve_fields, source_read
from .resolve_fields import _LEVEL_CN

# 字段标识 token：字母开头、含下划线的标识符（cqkd_tzrq / KEY_TZRQ 都会被切出；与 KB 已知 key 取交集才算命中）。
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")

# 限定常量引用候选：`Xxx.CONST_NAME`（Java 常量惯例——常量名全大写+下划线；用它把噪音过滤到
# 可接受范围，真假仍靠查 java_constant 表判定，见 _lookup_constants）。
_CONST_REF_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_$]*)\.([A-Z][A-Z0-9_]*)\b")
# 候选对上限（防病态文件把 WHERE IN 参数撑爆——SQLite 单条语句变量数有上限）。
_MAX_CONST_REFS = 300


def _find_constant_refs(body: str) -> set[tuple[str, str]]:
    """扫源码正文里的 `Xxx.CONST_NAME` 限定引用候选，供查 `java_constant` 表核实。"""
    pairs = {(m.group(1), m.group(2)) for m in _CONST_REF_RE.finditer(body)}
    if len(pairs) > _MAX_CONST_REFS:
        pairs = set(sorted(pairs)[:_MAX_CONST_REFS])
    return pairs


def _lookup_constants(conn, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """把候选 `(类, 常量名)` 对里真是项目常量的解析出字面值；不是（平台/枚举/误判）的静默丢弃。

    同一 (类, 常量名) 若工程里有多处定义、字面值不同 → 标 `ambiguous`（不擅自选一个，红线 #4）；
    否则回 `{value, defined_in, line}`（唯一定义时；多处定义但字面值相同也算唯一，取任一证据）。
    """
    if not pairs:
        return {}
    classes = sorted({c for c, _ in pairs})
    names = sorted({n for _, n in pairs})
    qc = ",".join("?" * len(classes))
    qn = ",".join("?" * len(names))
    rows = conn.execute(
        f"SELECT class_name, const_name, literal, source_relpath, line FROM java_constant "
        f"WHERE class_name IN ({qc}) AND const_name IN ({qn})",
        [*classes, *names],
    ).fetchall()
    by_pair: dict[tuple[str, str], list[tuple[str, str | None, int | None]]] = {}
    for r in rows:
        key = (r["class_name"], r["const_name"])
        if key in pairs:
            by_pair.setdefault(key, []).append((r["literal"], r["source_relpath"], r["line"]))
    out: dict[str, Any] = {}
    for key, defs in by_pair.items():
        lits = sorted({d[0] for d in defs})
        if len(lits) == 1:
            lit, relpath, line = defs[0]
            out[key] = {"value": lit, "defined_in": relpath, "line": line, "ambiguous": False}
        else:
            out[key] = {"value": None, "ambiguous": True, "candidates": lits}
    return out


def _known_keys(conn) -> set[str]:
    """KB 里所有字段 key + 实体/分录容器 key + 表单 key（一次性建集合，供扫源码取交集）。

    表单 key（`form.key`）2026-07-04 补入：真实翻车案例——`BusinessDataServiceHelper.load(
    "cqkd_invoic_apply", ...)` 里的字面量是**表单标识**，当表头实体 key 不等于表单 key（常见，
    entity 表另有自己的 key）时，老版本只查 field/entity 两表根本扫不到它，模型只能凭标识片段
    谐音瞎猜表单中文名（`cqkd_invoic_apply`→"开票申请"、`cqkd_contractbill`→"合同账单"）。
    """
    keys: set[str] = set()
    for tbl in ("field", "entity", "form"):
        keys |= {r[0] for r in conn.execute(
            f"SELECT DISTINCT key FROM {tbl} WHERE key IS NOT NULL") if r[0]}
    return keys


def _known_form_rows(conn) -> dict[str, list[Any]]:
    """表单 key → 该 key 对应的 `form` 表行（一般唯一；同 key 多行且中文名不同则判歧义）。"""
    out: dict[str, list[Any]] = {}
    for r in conn.execute("SELECT key, name, form_type FROM form WHERE key IS NOT NULL"):
        out.setdefault(r["key"], []).append(r)
    return out


def _classify_form_key(key: str, rows: list[Any] | None) -> dict[str, Any] | None:
    """把一个表单标识的候选行归为 unique/ambiguous（无 resolved 档——表单不像字段那样靠本文件
    `field_access` 收敛同名候选，同 key 多义本就罕见，钉不出就诚实留歧义，不擅自选一个）。
    """
    if not rows:
        return None   # 非表单 key（token 命中但 form 表查不到）→ 留给字段/常量分支或整体 None
    names = _distinct(r["name"] for r in rows)
    coords = [{"kind": "form", "name": r["name"], "form_key": key,
               "form_type": r["form_type"], "level": None} for r in rows]
    if len(names) <= 1:
        return {"tier": "unique", "names": names, "coordinates": coords[:1], "note": None}
    return {
        "tier": "ambiguous", "names": [], "coordinates": coords,
        "note": (f"标识 {key} 在项目内对应多个不同名的表单定义（{', '.join(names)}），"
                 "无法确定具体是哪一张，勿猜、勿默认某一个。"),
    }


def _looks_form_only(candidates: list[dict[str, Any]] | None) -> bool:
    """resolve_fields 可能把 form key 作为 kind=form 候选返回；此时用 form 表完整行保留多名歧义。"""
    return bool(candidates) and all(c.get("kind") == "form" for c in candidates)


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
    for opt in ("field_type", "access", "resolved_lines", "form_type"):
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
    `coordinates[].kind` 除 field/header/entry/subentry 外还有 `"form"`——表单标识本身命中
    `form` 表时用（如 `.load("xxx")` 里的字面量），是该表单的中文名，不是字段名。

    全文盲扫：正文里所有 token 与 KB 全量已知 key 取交集。量大天然有噪音（常见字段 key 本身是
    `remark`/`amount`/`org` 这类通用英文词，会撞到无关的局部变量），但胜在人眼浏览方便。
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

    # 全文盲扫：正文 token 与 KB 全量已知 key 取交集，再喂 resolve_fields 拿真实中文名+坐标
    # （复用，不重写），用本文件 field_access 解析出的来源实体收敛同名候选（三档置信，杜绝平铺误导）。
    known = _known_keys(conn)
    found = sorted({t for t in _TOKEN_RE.findall(body) if t in known})
    capped = max(0, len(found) - max_keys)
    scan_keys = found[:max_keys]
    # 限定常量引用（`TemporaryStopCon.ENTITY` 这类）：字面值不在源码正文里，靠查 java_constant
    # 表解析回字面值，再和普通 key 走同一套三档分类——堵"字面值扫不到、模型凭常量英文名瞎猜"。
    const_pairs = _find_constant_refs(body)

    const_lookup = _lookup_constants(conn, const_pairs) if const_pairs else {}
    const_values = {v["value"] for v in const_lookup.values() if v.get("value")}

    raw = resolve_fields.resolve_fields(conn, sorted(set(scan_keys) | const_values))["resolved"]
    form_ctx = _form_ctx_for_file(conn, relpath)
    form_rows = _known_form_rows(conn)

    # 常量引用条目**排在最前**（2026-07-03 真实翻车复盘）：紧凑投影按字节预算裁剪 field_names 时
    # （`_build_rs_compact` 的 `fn_full[:cap]`），字典迭代顺序=插入顺序——若常量条目排在普通字面量
    # key 后面，大文件（几十个普通 key）会把预算耗尽在前面，常量条目永远够不到、被静默截断，而
    # 恰恰是常量引用（字面值不在正文里）模型最需要、最不该丢的标注。常量数量天然少（受
    # `_MAX_CONST_REFS` 封顶），排最前不会挤占太多预算，却能保证几乎不被截断。
    const_field_names: dict[str, Any] = {}
    for (cls, const), info in const_lookup.items():
        expr = f"{cls}.{const}"
        if info.get("ambiguous"):
            const_field_names[expr] = {
                "tier": "ambiguous", "names": [], "coordinates": [],
                "note": (f"常量 {expr} 在项目内被多处定义、字面值不同"
                         f"（{', '.join(info['candidates'])}），无法确定具体取值，勿猜、勿默认某一个。"),
            }
            continue
        candidates = raw.get(info["value"])
        if _looks_form_only(candidates):
            classified = _classify_form_key(info["value"], form_rows.get(info["value"]))
        else:
            classified = _classify_key(info["value"], candidates, form_ctx)
        if classified is None:
            continue   # 常量已解出字面值，但字面值不是 KB 已知字段/实体 key（超出本工具标注范围）
        classified = dict(classified)
        classified["resolved_constant"] = {
            "value": info["value"], "defined_in": info.get("defined_in"), "line": info.get("line"),
        }
        const_field_names[expr] = classified

    # 表单标识兜底（2026-07-04）：先按字段/分录容器分类；钉不出（不在 field/entity 表）再查
    # form 表——`BusinessDataServiceHelper.load("xxx", ...)` 里的字面量常是表单 key 而非字段 key。
    plain_field_names: dict[str, Any] = {}
    for k in scan_keys:
        candidates = raw.get(k)
        if _looks_form_only(candidates):
            classified = _classify_form_key(k, form_rows.get(k))
        else:
            classified = _classify_key(k, candidates, form_ctx)
            if classified is None:
                classified = _classify_form_key(k, form_rows.get(k))
        plain_field_names[k] = classified

    field_names = {**const_field_names, **plain_field_names}

    note = ("字段名按三档置信标注：✅ 直接照抄；⚠️ ambiguous 表示本文件未解析到具体实体、有多张单据同名，"
            "需顺调用链消歧，勿默认当前单据。命中的表单标识（如 `.load(\"xxx\")` 里的字面量）同样标注"
            "（`coordinates[].kind=\"form\"` 时是该表单本身的中文名，不是字段名）。"
            " `类.常量`形式的限定引用（如 XxxCon.ENTITY）可直接作为 key 传入，会查项目常量表解析出"
            "字面值再标注——字段名即挂在该表达式本身（如 `field_names[\"XxxCon.ENTITY\"]`），带"
            " resolved_constant 标明解出的字面值+定义位置；解不出/歧义的同样不臆造，勿凭常量英文名"
            "猜中文含义。"
            " getDynamicObjectCollection(key) 取分录行还是多选基础资料集合，取决于 key——看坐标 "
            "field_type/access，勿凭 API 名断定是分录。")
    note += (" 注：`content`（源码正文）排在返回末尾——若 host 截断了它，重读更窄的 --lines 区间即可；"
              "`field_names` 在前不会丢。未列出的 `<isv>_` 标识用 resolve_fields 核对，勿按命名惯例猜。")
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


def _coord_line(key: str, c: dict[str, Any], *, mark: str, suffix: str = "") -> str:
    name = c.get("name") or ""
    if c.get("kind") == "form":
        ft = f" [{c['form_type']}]" if c.get("form_type") else ""
        return f"  {mark} 单据 {key}「{name}」{ft}{suffix}"
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
            rc = info.get("resolved_constant")
            if rc:
                where = f" @ {rc['defined_in']}:{rc['line']}" if rc.get("defined_in") else ""
                lines.append(f"  【常量引用】{key} = \"{rc['value']}\"{where}")
            if tier in ("unique", "resolved"):
                for c in info["coordinates"][:max_list]:
                    rl = c.get("resolved_lines")
                    suffix = f"（本文件解析到，行 {','.join(map(str, rl))}）" if rl else ""
                    lines.append(_coord_line(key, c, mark="✅", suffix=suffix))
                if info.get("also_in_forms"):
                    lines.append(f"      （另有同名字段在 {', '.join(info['also_in_forms'])}，本文件未命中）")
            elif not info["coordinates"]:  # 常量本身多处定义、字面值不同（无坐标可摆）
                lines.append(f"  ⚠️ {key}: {info.get('note') or '歧义，勿猜'}")
            elif info["coordinates"][0].get("kind") == "form":  # 表单标识多义（罕见），用自带 note
                lines.append(f"  ⚠️ {key}: {info.get('note') or '歧义，勿猜'}")
                for c in info["coordinates"][:max_list]:
                    lines.append(_coord_line(key, c, mark="·"))
            else:  # ambiguous（元数据多单据同名，本文件未解析到实体）
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
