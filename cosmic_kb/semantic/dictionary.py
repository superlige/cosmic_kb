"""阶段9 · 中文名 ↔ 标识词典（语义解析的语料底座）。

接手者提问时多半只记得**中文名**（"抵押状态""资产卡片"）或半个标识，记不全
`cqkd_collateralstatus` 这种全标识。本模块从 KB 里把 form/entity/field/plugin/operation
的「标识 ↔ 中文名」抽成可检索语料，给 resolver 做：① 标识精确命中 ② 中文名命中
③ RapidFuzz 模糊候选（未装则用标准库 difflib 降级，绝不硬依赖）。

设计纪律（对齐红线·证据优先）：
- **同名多义全保留**：一个中文名可能跨多张单据/多个层级出现（"金额"到处都是），
  本层只负责"把所有候选摆出来"，**绝不替用户选一个**——消歧交给 resolver/用户。
- 纯查 KB，无副作用；KB 是契约，本层不改 schema、不碰扫描器。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

# RapidFuzz 是可选依赖（pyproject [fuzzy]）；缺了用标准库 difflib 降级，功能不崩、只是
# 模糊召回弱一点。与 tree-sitter 缺失时字段级分析自动跳过同一套"可选增强"哲学。
try:  # pragma: no cover - 取决于环境是否装了 rapidfuzz
    from rapidfuzz import fuzz as _rf_fuzz

    _HAS_RAPIDFUZZ = True
except Exception:  # pragma: no cover
    _rf_fuzz = None
    _HAS_RAPIDFUZZ = False

import difflib


def _score(query: str, target: str) -> float:
    """两个串的相似度（0~100）。query/target 为空返回 0。

    优先 RapidFuzz 的 partial_ratio（子串友好：'抵押状态' 命中 '资产抵押状态'）；
    无 RapidFuzz 时用「子串包含强命中 + 滑窗 difflib」兜底，模拟 partial_ratio——
    否则裸 SequenceMatcher 对「短名嵌长句」（'金额' vs '金额是谁改的'）会严重低估、漏召回。
    """
    if not query or not target:
        return 0.0
    if _HAS_RAPIDFUZZ:
        return float(_rf_fuzz.partial_ratio(query, target))
    short, long = (query, target) if len(query) <= len(target) else (target, query)
    if short in long:                                   # 完整子串 → 强命中
        return 100.0
    n = len(short)
    if len(long) <= n:                                  # 等长，直接比
        return difflib.SequenceMatcher(None, query, target).ratio() * 100.0
    best = 0.0                                          # 在长串里滑同长窗口取最优
    for i in range(len(long) - n + 1):
        best = max(best, difflib.SequenceMatcher(None, short, long[i:i + n]).ratio())
    return best * 100.0


@dataclass(frozen=True)
class FieldEntry:
    """一个字段在某实体坐标下的定义（同一 field_key 可能有多条，跨单据/层级）。"""

    key: str
    name: str | None
    form_key: str | None
    form_name: str | None
    entity_key: str | None
    level: str | None
    kind: str | None


@dataclass(frozen=True)
class FormEntry:
    key: str
    name: str | None
    form_type: str | None


@dataclass
class ClassEntry:
    """一个源码类/插件类的语义条目（plugin 注册 + source_class 桥接信息合并）。"""

    fqn: str
    simple: str
    relpath: str | None = None
    plugin_types: set[str] = dc_field(default_factory=set)   # form/list/op/convert/...
    forms: set[str] = dc_field(default_factory=set)          # 注册到的单据 key
    orphan_role: str | None = None                            # plugin/constant/unknown
    plugin_base: str | None = None                            # 命中的苍穹插件基类


@dataclass(frozen=True)
class OpEntry:
    form_key: str | None
    key: str | None
    name: str | None
    operation_type: str | None
    has_plugin: bool


@dataclass
class Candidate:
    """一条带分数的候选（消歧菜单的元素）。kind=field/form/class/operation。"""

    kind: str
    score: float
    payload: Any  # FieldEntry / FormEntry / ClassEntry / OpEntry

    def label(self) -> str:
        p = self.payload
        if isinstance(p, FieldEntry):
            lvl = {"header": "表头", "entry": "分录", "subentry": "子分录",
                   "basedata": "基础资料"}.get(p.level or "", p.level or "?")
            home = f"{p.form_key}「{p.form_name}」" if p.form_name else (p.form_key or "?")
            return f"{p.key}「{p.name or ''}」 — {home} · {lvl}"
        if isinstance(p, FormEntry):
            return f"{p.key}「{p.name or ''}」 [{p.form_type or '?'}]"
        if isinstance(p, ClassEntry):
            tag = "/".join(sorted(p.plugin_types)) or (p.orphan_role or "class")
            return f"{p.simple} [{tag}] — {p.fqn}"
        if isinstance(p, OpEntry):
            return f"{p.key}「{p.name or ''}」 [{p.operation_type or '?'}] — 单据 {p.form_key}"
        return str(p)


class Lexicon:
    """从 KB 一次性构建的检索语料；resolver 在其上做意图主体定位。"""

    def __init__(self, conn) -> None:
        self.fields: list[FieldEntry] = []
        self.forms: list[FormEntry] = []
        self.classes: list[ClassEntry] = []
        self.operations: list[OpEntry] = []
        self._build(conn)

    # ── 构建 ────────────────────────────────────────────────────────────────
    def _build(self, conn) -> None:
        form_names = {r["key"]: r["name"] for r in conn.execute("SELECT key,name FROM form")}

        self.forms = [
            FormEntry(r["key"], r["name"], r["form_type"])
            for r in conn.execute("SELECT key,name,form_type FROM form")
            if r["key"]
        ]
        self.fields = [
            FieldEntry(r["key"], r["name"], r["form_key"], form_names.get(r["form_key"]),
                       r["entity_key"], r["level"], r["kind"])
            for r in conn.execute(
                "SELECT key,name,form_key,entity_key,level,kind FROM field")
            if r["key"]
        ]
        self.operations = [
            OpEntry(r["form_key"], r["key"], r["name"], r["operation_type"], bool(r["has_plugin"]))
            for r in conn.execute(
                "SELECT form_key,key,name,operation_type,has_plugin FROM operation")
        ]

        # 类：plugin 注册信息 + source_class 桥接信息按 fqn 合并。
        by_fqn: dict[str, ClassEntry] = {}
        for r in conn.execute(
            "SELECT fqn,simple,relpath,orphan_role,plugin_base FROM source_class"
        ):
            if not r["fqn"]:
                continue
            by_fqn[r["fqn"]] = ClassEntry(
                fqn=r["fqn"], simple=r["simple"] or r["fqn"].rsplit(".", 1)[-1],
                relpath=r["relpath"], orphan_role=r["orphan_role"],
                plugin_base=r["plugin_base"])
        for r in conn.execute(
            "SELECT class_name,plugin_type,form_key FROM plugin"
        ):
            cn = r["class_name"]
            if not cn:
                continue
            ce = by_fqn.get(cn)
            if ce is None:
                ce = by_fqn[cn] = ClassEntry(fqn=cn, simple=cn.rsplit(".", 1)[-1])
            if r["plugin_type"]:
                ce.plugin_types.add(r["plugin_type"])
            if r["form_key"]:
                ce.forms.add(r["form_key"])
        self.classes = list(by_fqn.values())

        # ── 索引 ──
        self._fields_by_key: dict[str, list[FieldEntry]] = {}
        for f in self.fields:
            self._fields_by_key.setdefault(f.key, []).append(f)
        self._form_by_key: dict[str, FormEntry] = {f.key: f for f in self.forms}
        self._ops_by_key: dict[str, list[OpEntry]] = {}
        for o in self.operations:
            if o.key:
                self._ops_by_key.setdefault(o.key, []).append(o)
        self._class_by_fqn: dict[str, ClassEntry] = {c.fqn: c for c in self.classes}
        self._class_by_simple: dict[str, list[ClassEntry]] = {}
        for c in self.classes:
            self._class_by_simple.setdefault(c.simple, []).append(c)

    # ── 精确查（标识命中）────────────────────────────────────────────────────
    def fields_by_key(self, key: str) -> list[FieldEntry]:
        return self._fields_by_key.get(key, [])

    def form_by_key(self, key: str) -> FormEntry | None:
        return self._form_by_key.get(key)

    def operations_by_key(self, key: str) -> list[OpEntry]:
        return self._ops_by_key.get(key, [])

    def class_by_name(self, token: str) -> list[ClassEntry]:
        """按 fqn 全等或末段类名命中（CollateralService → 该类）。"""
        if token in self._class_by_fqn:
            return [self._class_by_fqn[token]]
        return list(self._class_by_simple.get(token, []))

    # ── 模糊候选（中文名 / 半标识）────────────────────────────────────────────
    def fuzzy_fields(self, text: str, *, limit: int = 8, cutoff: float = 60.0) -> list[Candidate]:
        return self._fuzzy(text, self.fields, "field",
                           lambda f: (f.name, f.key), limit=limit, cutoff=cutoff)

    def fuzzy_forms(self, text: str, *, limit: int = 8, cutoff: float = 60.0) -> list[Candidate]:
        return self._fuzzy(text, self.forms, "form",
                           lambda f: (f.name, f.key), limit=limit, cutoff=cutoff)

    def fuzzy_classes(self, text: str, *, limit: int = 8, cutoff: float = 60.0) -> list[Candidate]:
        return self._fuzzy(text, self.classes, "class",
                           lambda c: (c.simple, c.fqn), limit=limit, cutoff=cutoff)

    def _fuzzy(self, text, entries, kind, namer, *, limit, cutoff) -> list[Candidate]:
        """对一类条目按 (中文名, 标识) 取最高分；按 (分数, 名称长度) 降序截断。

        partial_ratio 对**短名**偏心——'资产'(2字)是几乎任何含'资产'的句子的子串、分数恒 100，
        会淹没真正想问的 '抵押状态'(4字)。故同分时按**候选名长度**降序：更长 = 更具体、更可能
        是用户问的那个，排前面，让消歧菜单先列最相关项（仍不替用户拍板，只是排好序）。
        """
        text = (text or "").strip()
        if not text:
            return []
        best: list[Candidate] = []
        for e in entries:
            name, key = namer(e)
            s = max(_score(text, name or ""), _score(text, key or ""))
            if s >= cutoff:
                best.append((len(name or ""), Candidate(kind, s, e)))
        best.sort(key=lambda t: (-t[1].score, -t[0]))
        return [c for _, c in best[:limit]]


def build_lexicon(conn) -> Lexicon:
    """便捷入口：从 KB 连接构建词典。"""
    return Lexicon(conn)
