"""阶段9 验收测试 —— 语义解析层（dictionary + resolver）。

覆盖：词典构建（标识/中文名双向 + 同名多义保留）、意图分类（旗舰字段/单据/插件/操作）、
精确标识与点号坐标命中、中文名模糊召回、低置信反问（同名歧义 / 没听懂）。
"""

from __future__ import annotations

from pathlib import Path

from cosmic_kb.graph import store
from cosmic_kb.semantic import dictionary, resolver

from _synthkb import make_kb


def _conn(tmp_path: Path):
    return store.open_kb(make_kb(tmp_path))


# ── 词典 ──────────────────────────────────────────────────────────────────────
def test_lexicon_field_by_key_and_name(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        lex = dictionary.build_lexicon(conn)
        # 标识精确命中。
        hits = lex.fields_by_key("cqkd_collateralstatus")
        assert len(hits) == 1 and hits[0].name == "抵押状态"
        # 中文名模糊命中。
        cands = lex.fuzzy_fields("抵押状态")
        assert cands and cands[0].payload.key == "cqkd_collateralstatus"
    finally:
        conn.close()


def test_lexicon_same_name_multi_coord(tmp_path: Path):
    """同名字段「金额」跨两张单 → 词典保留两份候选，不去重。"""
    conn = _conn(tmp_path)
    try:
        lex = dictionary.build_lexicon(conn)
        cands = lex.fuzzy_fields("金额")
        forms = {c.payload.form_key for c in cands}
        assert {"cqkd_assetcard", "cqkd_contract"} <= forms
    finally:
        conn.close()


def test_lexicon_class_by_name(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        lex = dictionary.build_lexicon(conn)
        entries = lex.class_by_name("CollateralService")
        assert len(entries) == 1
        assert entries[0].fqn == "cqspb.assets.CollateralService"
    finally:
        conn.close()


# ── 意图分类 ──────────────────────────────────────────────────────────────────
def test_resolve_explicit_field_key(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "cqkd_collateralstatus")
        assert rq.intent == "field_who_changed"
        assert rq.field_key == "cqkd_collateralstatus"
        assert not rq.need_clarification
    finally:
        conn.close()


def test_resolve_dotted_locator(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "cqkd_assetcard.cqkd_entry.cqkd_amount")
        assert rq.intent == "field_who_changed"
        assert rq.field_key == "cqkd_amount"
        assert rq.form_key == "cqkd_assetcard"
        assert rq.entry_key == "cqkd_entry"
        assert rq.level == "entry"
    finally:
        conn.close()


def test_resolve_field_who_changed_chinese(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "资产卡片抵押状态是谁改的？")
        assert rq.intent == "field_who_changed"
        assert rq.field_key == "cqkd_collateralstatus"
    finally:
        conn.close()


def test_resolve_bill_drilldown(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "cqkd_assetcard 这张单有哪些插件？")
        assert rq.intent == "bill_drilldown"
        assert rq.form_key == "cqkd_assetcard"
    finally:
        conn.close()


def test_resolve_plugin_explain(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "CollateralService 这个类是干嘛的？")
        assert rq.intent == "plugin_explain"
        assert rq.class_fqn == "cqspb.assets.CollateralService"
    finally:
        conn.close()


def test_resolve_operation_explain(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "cqkd_assetcard 这个 audit 操作按钮影响哪些字段？")
        assert rq.intent == "operation_explain"
        assert rq.form_key == "cqkd_assetcard"
        assert rq.operation_key == "audit"
    finally:
        conn.close()


# ── 低置信反问 ────────────────────────────────────────────────────────────────
def test_resolve_ambiguous_same_name(tmp_path: Path):
    """「金额」同名跨单 → 必须反问给候选，绝不替用户选一个。"""
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "金额是谁改的？")
        assert rq.need_clarification
        assert len(rq.candidates) >= 2
        # 没有擅自落到某一个 field_key。
        assert rq.field_key is None
    finally:
        conn.close()


def test_resolve_unknown(tmp_path: Path):
    conn = _conn(tmp_path)
    try:
        rq = resolver.resolve(conn, "天气怎么样zzz")
        assert rq.intent == "unknown"
        assert rq.need_clarification
    finally:
        conn.close()


# ── CLI ask 子命令 ────────────────────────────────────────────────────────────
def test_cli_ask_registered(tmp_path: Path, capsys):
    from cosmic_kb.cli.main import main

    db = make_kb(tmp_path)
    rc = main(["ask", "cqkd_collateralstatus", "--db", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cqkd_collateralstatus" in out


def test_cli_ask_clarification_exit_code(tmp_path: Path, capsys):
    """需消歧 → 退出码 3，方便脚本/Skill 判断还要再问一轮。"""
    from cosmic_kb.cli.main import main

    db = make_kb(tmp_path)
    rc = main(["ask", "金额是谁改的？", "--db", str(db)])
    capsys.readouterr()
    assert rc == 3


def test_cli_ask_kb_missing(tmp_path: Path):
    from cosmic_kb.cli.main import main

    rc = main(["ask", "cqkd_collateralstatus", "--db", str(tmp_path / "nope.db")])
    assert rc == 2
