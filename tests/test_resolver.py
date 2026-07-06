"""阶段9 · resolver.py 字段跨单据歧义反问测试。

背景：`trace` 裸字段查询不再聚合列出全部单据的证据（对已知道查哪张单据的排障者是噪音，
交更便宜的 resolve_fields 做发现）；`resolver.resolve()` 是 `ask`/MCP `ask` 的入口，必须在
"字段谁改的"意图产出 `form_key=None` 之前就先反问，而不是让聚合行为在 field_trace 里悄悄发生。
本文件覆盖三处会产出 `form_key=None` 的 `field_who_changed` 构造点，以及既有但此前未覆盖的
`plugin_explain` 类名歧义反问分支（同一套 need_clarification 形状，顺手补测防回归）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cosmic_kb.graph import store
from cosmic_kb.semantic import resolver

from _synthkb import make_kb

_SCHEMA_PATH = Path(store.__file__).with_name("schema.sql")


def _mini_kb(tmp_path: Path) -> Path:
    """比 _synthkb 更小的自建 KB：同 field_key 跨两单据但中文名不同（供模糊单强候选场景，
    _synthkb 里的 cqkd_amount 两处同名"金额"，天然打平分数，测不出单强候选分支）+ 两个
    同末段类名不同包的类（供 plugin_explain 歧义回归）。"""
    db = tmp_path / "mini.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.executemany(
        "INSERT INTO form(key,name,form_type) VALUES(?,?,?)",
        [("cqkd_fa", "单据甲", "bill"), ("cqkd_fb", "单据乙", "bill")],
    )
    conn.executemany(
        "INSERT INTO field(uid,form_key,entity_key,key,name,kind,level) "
        "VALUES(?,?,?,?,?,?,?)",
        [
            ("m1", "cqkd_fa", "cqkd_fa", "cqkd_dupkey", "唯一好名字", "entity", "header"),
            ("m2", "cqkd_fb", "cqkd_fb", "cqkd_dupkey", "完全不同名", "entity", "header"),
        ],
    )
    conn.executemany(
        "INSERT INTO source_class(fqn,simple,package,relpath,module,is_orphan,orphan_role,"
        "plugin_base) VALUES(?,?,?,?,?,?,?,?)",
        [
            ("pkg.a.CollateralOp", "CollateralOp", "pkg.a", "pkg/a/CollateralOp.java",
             "m", 1, None, None),
            ("pkg.b.CollateralOp", "CollateralOp", "pkg.b", "pkg/b/CollateralOp.java",
             "m", 1, None, None),
        ],
    )
    conn.commit()
    conn.close()
    return db


# ── 三处会产出 form_key=None 的 field_who_changed 构造点：跨单据歧义必须反问 ──────────

def test_bare_locator_cross_form_field_needs_clarification(tmp_path):
    """点号定位分支（纯 field_key 文本，无点号）：cqkd_amount 跨 cqkd_assetcard/cqkd_contract。"""
    db = make_kb(tmp_path)
    conn = store.open_kb(db)
    try:
        rq = resolver.resolve(conn, "cqkd_amount")
    finally:
        conn.close()
    assert rq.intent == "field_who_changed"
    assert rq.need_clarification is True
    assert rq.form_key is None
    forms = {c.payload.form_key for c in rq.candidates}
    assert forms == {"cqkd_assetcard", "cqkd_contract"}


def test_token_in_question_cross_form_field_needs_clarification(tmp_path):
    """字段谁改的（默认）分支：问句里带裸字段 token、未提单据。"""
    db = make_kb(tmp_path)
    conn = store.open_kb(db)
    try:
        rq = resolver.resolve(conn, "cqkd_amount 是谁改的")
    finally:
        conn.close()
    assert rq.intent == "field_who_changed"
    assert rq.need_clarification is True
    forms = {c.payload.form_key for c in rq.candidates}
    assert forms == {"cqkd_assetcard", "cqkd_contract"}


def test_fuzzy_single_strong_candidate_cross_form_field_needs_clarification(tmp_path):
    """_resolve_fuzzy 单强候选分支：中文名精确命中其中一个坐标，但该字段 key 本身跨单据。"""
    db = _mini_kb(tmp_path)
    conn = store.open_kb(db)
    try:
        rq = resolver.resolve(conn, "唯一好名字是谁改的")
    finally:
        conn.close()
    assert rq.intent == "field_who_changed"
    assert rq.need_clarification is True
    forms = {c.payload.form_key for c in rq.candidates}
    assert forms == {"cqkd_fa", "cqkd_fb"}


# ── 不应误伤的场景 ────────────────────────────────────────────────────────────

def test_bare_field_single_coordinate_not_ambiguous(tmp_path):
    """字段只有单一坐标（cqkd_collateralstatus 仅属 cqkd_assetcard）→ 直接定位，不反问。"""
    db = make_kb(tmp_path)
    conn = store.open_kb(db)
    try:
        rq = resolver.resolve(conn, "cqkd_collateralstatus")
    finally:
        conn.close()
    assert rq.intent == "field_who_changed"
    assert rq.need_clarification is False
    assert rq.field_key == "cqkd_collateralstatus"


def test_field_with_form_mentioned_not_ambiguous(tmp_path):
    """问句同时提到单据 key：即使字段本身跨单据，也按提及的单据缩小范围，不反问。"""
    db = make_kb(tmp_path)
    conn = store.open_kb(db)
    try:
        rq = resolver.resolve(conn, "cqkd_amount cqkd_assetcard 谁改的")
    finally:
        conn.close()
    assert rq.intent == "field_who_changed"
    assert rq.need_clarification is False
    assert rq.field_key == "cqkd_amount"
    assert rq.form_key == "cqkd_assetcard"


# ── plugin_explain 既有歧义反问分支（同一套 need_clarification 形状，顺手补覆盖防回归）──

def test_plugin_explain_ambiguous_class_name_needs_clarification(tmp_path):
    """同末段类名、不同包 → 反问，候选覆盖两个全限定名。"""
    db = _mini_kb(tmp_path)
    conn = store.open_kb(db)
    try:
        rq = resolver.resolve(conn, "CollateralOp 是干嘛的")
    finally:
        conn.close()
    assert rq.intent == "plugin_explain"
    assert rq.need_clarification is True
    fqns = {c.payload.fqn for c in rq.candidates}
    assert fqns == {"pkg.a.CollateralOp", "pkg.b.CollateralOp"}


def test_plugin_explain_fqn_in_sentence_hits_exact_no_clarification(tmp_path):
    """末段类名 CollateralOp 本身歧义，但问句里给了完整 FQN → 应精确命中，不再反问。"""
    db = _mini_kb(tmp_path)
    conn = store.open_kb(db)
    try:
        rq = resolver.resolve(conn, "pkg.a.CollateralOp 是干嘛的")
    finally:
        conn.close()
    assert rq.intent == "plugin_explain"
    assert rq.need_clarification is False
    assert rq.class_fqn == "pkg.a.CollateralOp"


def test_plugin_explain_bare_fqn_hits_exact_no_clarification(tmp_path):
    """纯 FQN 单值输入（无自然语言修饰，会先过 _looks_like_locator 快路径）也应精确命中。"""
    db = _mini_kb(tmp_path)
    conn = store.open_kb(db)
    try:
        rq = resolver.resolve(conn, "pkg.b.CollateralOp")
    finally:
        conn.close()
    assert rq.intent == "plugin_explain"
    assert rq.need_clarification is False
    assert rq.class_fqn == "pkg.b.CollateralOp"
