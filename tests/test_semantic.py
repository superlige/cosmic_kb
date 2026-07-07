"""词典层验收测试 —— `semantic/dictionary.py`（`resolve_fields` 的语料底座）。

原覆盖的意图分类/低置信反问/CLI `ask` 子命令三块，随 `ask` 命令 + 其依附的
`semantic.resolver`/`context.builder` 于 2026-07 整体退役而删除（自然语言路由这层判断，
宿主大模型本来就比关键词分类器更擅长选工具，见 `docs/核心/阶段验收.md`）。同批一并清理的
死代码（`OpEntry`/`ClassEntry`/`Candidate`/`fuzzy_*`/RapidFuzz 模糊匹配）只服务 ask/resolver，
`resolve_fields` 走的是精确 key 查询，不需要模糊召回，故 `test_lexicon_class_by_name`
（测 `class_by_name`，已随 `ClassEntry` 一起删除）与依赖 `fuzzy_fields` 的用例一并删除/改写。

现只保留 `resolve_fields` 仍在用的精确查询路径：标识命中 + 同 key 跨坐标全保留不去重。
"""

from __future__ import annotations

from pathlib import Path

from cosmic_kb.graph import store
from cosmic_kb.semantic import dictionary

from _synthkb import make_kb


def _conn(tmp_path: Path):
    return store.open_kb(make_kb(tmp_path))


def test_lexicon_field_by_key(tmp_path: Path):
    """标识精确命中（`resolve_fields` 的核心查询路径）。"""
    conn = _conn(tmp_path)
    try:
        lex = dictionary.build_lexicon(conn)
        hits = lex.fields_by_key("cqkd_collateralstatus")
        assert len(hits) == 1 and hits[0].name == "抵押状态"
    finally:
        conn.close()


def test_lexicon_same_key_multi_coord(tmp_path: Path):
    """同 key「金额」跨两张单 → 词典保留两份候选，不去重、不替选。"""
    conn = _conn(tmp_path)
    try:
        lex = dictionary.build_lexicon(conn)
        hits = lex.fields_by_key("cqkd_amount")
        forms = {h.form_key for h in hits}
        assert {"cqkd_assetcard", "cqkd_contract"} <= forms
    finally:
        conn.close()
