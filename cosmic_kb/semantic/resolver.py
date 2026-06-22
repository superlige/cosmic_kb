"""阶段9 · 自然语言 → 查询意图（确定性，无 LLM）。

把接手者的一句话（"资产卡片抵押状态是谁改的？"）解析成 KB 可执行的查询：
**意图**（旗舰=字段谁改的 / 单据钻取 / 插件·操作解释）+ **主体**（落到具体 field_key /
form_key / 类 fqn / 操作 key）+ **置信度**。判不准就**返回候选菜单反问**，绝不硬猜——
对齐红线·证据优先（宁标 unknown 不臆造）。真正的自然语言推理交给段二 Skill；本层只做
确定性的"听懂问的是哪个东西"。

意图取值：
    field_who_changed  旗舰：某字段被哪些插件/事件改、是否落库
    bill_drilldown     单据钻取：这张单的操作集/插件/字段触达/风险
    plugin_explain     插件/类解释：这个类干嘛的、读写哪些字段
    operation_explain  操作解释：这个操作按钮影响哪些字段（需单据上下文消歧）
    ambiguous          听懂了类型但主体有多个候选 → 反问
    unknown            没在 KB 里找到任何可定位的主体 → 反问/提示
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Any

from . import dictionary
from .dictionary import Candidate, Lexicon
from ..report.field_trace import parse_locator

# ── 意图关键词（中文为主，命中即置该意图的信号位）────────────────────────────
_KW_WHO = ("谁改", "谁修改", "谁动", "谁写", "谁赋值", "改了", "修改", "写入", "设置",
           "赋值", "在哪改", "在哪设置", "落库", "入库", "改的", "被改", "谁设置")
_KW_BILL = ("单据", "这张单", "这个单", "这单", "操作集", "钻取", "单据视图",
            "有哪些操作", "哪些插件", "哪些字段", "整张单", "概览")
_KW_PLUGIN = ("干嘛", "干什么", "做什么", "是什么", "什么作用", "啥用", "解释一下",
              "这个类", "这个插件", "这个服务", "这个工具", "读写哪些", "影响哪些字段")
_KW_OP = ("这个操作", "操作按钮", "按钮", "提交时", "保存时", "审核时", "点这个",
          "执行时", "这个按钮")

# 标识/类名 token：cqkd_xxx（小写下划线）或 CamelCase 类名。
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class ResolvedQuery:
    """resolver 的统一产物，喂给 context.builder 取证。"""

    intent: str
    raw: str
    confidence: float = 0.0
    note: str | None = None
    need_clarification: bool = False
    # 主体（按意图取用，其余为 None）
    field_key: str | None = None
    form_key: str | None = None
    entry_key: str | None = None
    level: str | None = None
    class_fqn: str | None = None
    operation_key: str | None = None
    # 反问候选（ambiguous/unknown 时给消歧菜单）
    candidates: list[Candidate] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent, "raw": self.raw, "confidence": round(self.confidence, 3),
            "note": self.note, "need_clarification": self.need_clarification,
            "field_key": self.field_key, "form_key": self.form_key,
            "entry_key": self.entry_key, "level": self.level,
            "class_fqn": self.class_fqn, "operation_key": self.operation_key,
            "candidates": [{"kind": c.kind, "score": round(c.score, 1), "label": c.label()}
                           for c in self.candidates],
        }


def _has(text: str, kws) -> bool:
    return any(k in text for k in kws)


def _looks_like_locator(text: str) -> bool:
    """单 token 的点号定位（cqkd_a.cqkd_b.cqkd_c），整串无空白/中文。"""
    t = text.strip()
    return bool(t) and not re.search(r"\s", t) and re.fullmatch(r"[A-Za-z0-9_.]+", t) is not None


def resolve(conn, text: str, lexicon: Lexicon | None = None) -> ResolvedQuery:
    """把一句话解析成 ResolvedQuery。lexicon 可外部预建复用（批量问答省构建开销）。"""
    lex = lexicon or dictionary.build_lexicon(conn)
    raw = text or ""
    text = raw.strip()
    if not text:
        return ResolvedQuery("unknown", raw, note="空输入。")

    flag_who, flag_bill = _has(text, _KW_WHO), _has(text, _KW_BILL)
    flag_plugin, flag_op = _has(text, _KW_PLUGIN), _has(text, _KW_OP)

    # ── 0. 纯点号定位（用户直接给坐标，最强信号）──────────────────────────────
    if _looks_like_locator(text):
        fkey, form, entry, level = parse_locator(text)
        if lex.fields_by_key(fkey):
            return ResolvedQuery(
                "field_who_changed", raw, confidence=0.97,
                field_key=fkey, form_key=form, entry_key=entry, level=level,
                note="点号坐标精确定位。")

    # ── 1. 抽取标识/类名 token，按 KB 精确归类 ────────────────────────────────
    tokens = _TOKEN_RE.findall(text)
    field_hits = [t for t in tokens if lex.fields_by_key(t)]
    form_hits = [t for t in tokens if lex.form_by_key(t)]
    class_hits: list[tuple[str, Any]] = [
        (t, lex.class_by_name(t)) for t in tokens if lex.class_by_name(t)]
    op_hits = [t for t in tokens if lex.operations_by_key(t)
               and not lex.fields_by_key(t) and not lex.form_by_key(t)]

    # ── 2. 操作解释（需单据上下文消歧；操作 key 多张单复用）─────────────────────
    if flag_op and op_hits and form_hits:
        return ResolvedQuery(
            "operation_explain", raw, confidence=0.85,
            form_key=form_hits[0], operation_key=op_hits[0],
            note="操作按钮 → 该单据下绑定的插件与字段触达。")

    # ── 3. 类/插件解释（命中类名 + 解释类关键词，或无字段/单据竞争信号）──────────
    if class_hits and (flag_plugin or (not field_hits and not flag_who)):
        token, entries = class_hits[0]
        if len(entries) == 1:
            return ResolvedQuery(
                "plugin_explain", raw, confidence=0.9, class_fqn=entries[0].fqn,
                note="类名精确命中。")
        # 同末段类名多个（不同包）→ 反问。
        return ResolvedQuery(
            "plugin_explain", raw, confidence=0.5, need_clarification=True,
            note=f"末段类名 {token} 命中 {len(entries)} 个不同包的类，请指定全限定名。",
            candidates=[Candidate("class", 100.0, e) for e in entries])

    # ── 4. 单据钻取（命中单据 key + 单据类关键词，或无字段信号）────────────────
    if form_hits and (flag_bill or not field_hits):
        return ResolvedQuery(
            "bill_drilldown", raw, confidence=0.9, form_key=form_hits[0],
            note="单据标识精确命中。")

    # ── 5. 字段谁改的（旗舰，默认）：命中字段 key ──────────────────────────────
    if field_hits:
        fkey = field_hits[0]
        # 若同时命中单据 key，作为坐标过滤（缩小到该单据，但不锁层级——交 field_trace 按
        # 该单据全坐标分组，避免把分录字段误挤进「可能命中」桶）。
        form = form_hits[0] if form_hits else None
        return ResolvedQuery(
            "field_who_changed", raw, confidence=0.92,
            field_key=fkey, form_key=form,
            note="字段标识精确命中。" + ("已按提及的单据缩小范围。" if form else ""))

    # ── 6. 没有精确标识 → 中文名模糊召回 + 意图倾向 ────────────────────────────
    return _resolve_fuzzy(lex, raw, text, flag_who, flag_bill, flag_plugin)


def _resolve_fuzzy(lex, raw, text, flag_who, flag_bill, flag_plugin) -> ResolvedQuery:
    """无精确标识：按意图倾向在对应语料里做模糊召回，单一强候选则定位，否则反问。"""
    field_c = lex.fuzzy_fields(text)
    form_c = lex.fuzzy_forms(text)
    class_c = lex.fuzzy_classes(text)

    # 意图倾向决定优先看哪一类候选；都没倾向时按"字段优先（旗舰）"。
    if flag_bill:
        ordered = [("bill_drilldown", form_c), ("field_who_changed", field_c),
                   ("plugin_explain", class_c)]
    elif flag_plugin:
        ordered = [("plugin_explain", class_c), ("field_who_changed", field_c),
                   ("bill_drilldown", form_c)]
    else:  # flag_who 或无倾向 → 旗舰字段优先
        ordered = [("field_who_changed", field_c), ("bill_drilldown", form_c),
                   ("plugin_explain", class_c)]

    primary_intent, primary = next(((i, c) for i, c in ordered if c), (None, []))
    if not primary:
        return ResolvedQuery(
            "unknown", raw, confidence=0.0, need_clarification=True,
            note="没在 KB 里找到匹配的字段/单据/类。换个中文名或贴上标识（如 cqkd_xxx）再试。")

    top = primary[0]
    second = primary[1].score if len(primary) > 1 else 0.0
    # 单一强候选（高分且与次名拉开差距）→ 直接定位；否则摆候选菜单反问。
    if top.score >= 85.0 and (top.score - second) >= 8.0:
        rq = ResolvedQuery(primary_intent, raw, confidence=top.score / 100.0,
                           note=f"中文名模糊命中（{top.score:.0f} 分）。")
        _fill_subject(rq, top)
        return rq

    return ResolvedQuery(
        primary_intent, raw, confidence=top.score / 100.0, need_clarification=True,
        note="有多个相近候选，请挑一个精确标识：",
        candidates=primary[:8])


def _fill_subject(rq: ResolvedQuery, cand: Candidate) -> None:
    """把命中的候选回填到 ResolvedQuery 的主体字段。"""
    p = cand.payload
    if cand.kind == "field":
        rq.field_key = p.key
    elif cand.kind == "form":
        rq.form_key = p.key
    elif cand.kind == "class":
        rq.class_fqn = p.fqn
