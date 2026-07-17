"""扫描可信度报告测试 —— 手段一「字段覆盖率」+ 质量分解。

造一个最小项目（2 个表单、若干字段、一条 field_access），验证：
  * 覆盖率以元数据业务字段为分母、以 field_access 命中为分子；
  * 平台/继承字段不进分母；
  * 质量分解（解析/定位/落库/命中元数据）口径正确；
  * render_coverage 文本不抛异常、含关键字段。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cosmic_kb.graph import store
from cosmic_kb.java import null_reason as nr_mod
from cosmic_kb.report import coverage


def _kb_with_fields(tmp_path: Path) -> sqlite3.Connection:
    """直接按 schema 造一个 KB，手填 field / field_access / form / binding，便于精确断言。"""
    db = tmp_path / "kb.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))

    # 两个表单，归属同模块；F1 有插件、F2 无。
    conn.executemany(
        "INSERT INTO form(key,name,form_type,module) VALUES(?,?,?,?)",
        [("f1", "单据一", "bill", "modA"), ("f2", "单据二", "bill", "modA")],
    )
    conn.execute("INSERT INTO module(name) VALUES('modA')")
    conn.execute(
        "INSERT INTO plugin(uid,form_key,class_name,plugin_type,source) "
        "VALUES('u','f1','cqspb.P','form','project')")
    # F1: 3 业务字段(entity) + 1 platform；F2: 2 业务字段。平台字段不进分母。
    fields = [
        ("f1::h::a", "f1", "h", "a", "甲", None, "T", "entity", "header"),
        ("f1::h::b", "f1", "h", "b", "乙", None, "T", "entity", "header"),
        ("f1::h::c", "f1", "h", "c", "丙", None, "T", "entity", "header"),
        ("f1::h::id", "f1", "h", "id", "主键", None, "T", "platform", "header"),
        ("f2::h::x", "f2", "h", "x", "X", None, "T", "entity", "header"),
        ("f2::h::y", "f2", "h", "y", "Y", None, "T", "entity", "header"),
    ]
    conn.executemany(
        "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        fields,
    )

    # field_access：f1.a 被写(落库,literal)、f1.b 被读(constant)；
    #   一条 form_key=NULL 的未定位写(ambiguous)；一条解析出元数据没有的 key 'zzz'。
    def fa(form, fkey, level, access, persists, res):
        nreason = None if form else nr_mod.classify(
            {"form_key": form, "field_key": fkey, "level": level,
             "via": "do.set", "key_resolution": res, "evidence": ""})
        return (form, fkey, level, None, "cqspb.P", "form", "cqspb.P",
                "ev", "ev", "transaction", access, persists, "r", "set", 1, "[]",
                res, 1.0, "P.java", "", "data_flow" if form else None, nreason,
                "local")
    accesses = [
        fa("f1", "a", "header", "write", "yes", "literal"),
        fa("f1", "b", "header", "read", "na", "constant"),
        fa(None, "a", "unknown", "write", "unknown", "ambiguous"),
        fa("f1", "zzz", "header", "write", "no", "literal"),  # 元数据没有 zzz
    ]
    conn.executemany(
        "INSERT INTO field_access VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", accesses)
    conn.execute("INSERT INTO kb_meta(key,value) VALUES('java_analysis',?)",
                 ('{"available": true, "analyzed_plugins": 1, "field_access": 4}',))
    conn.commit()
    return conn


_SCHEMA = Path(store.__file__).with_name("schema.sql")


def test_field_coverage_denominator_and_numerator(tmp_path: Path):
    conn = _kb_with_fields(tmp_path)
    try:
        c = coverage.coverage(conn)
        fc = c["field_coverage"]
        # 分母 = 5 个业务字段（platform 的 id 不计）。
        assert fc["business_total"] == 5
        # 分子 = f1.a + f1.b 命中（zzz 不在元数据、未定位的 a 算 f1.a 同一字段已计）。
        assert fc["touched"] == 2
        assert fc["rate"] == round(2 / 5, 4)
        assert fc["write_touched"] == 1  # 仅 f1.a 写命中（zzz 不算业务字段）
        assert fc["read_touched"] == 1
        # platform 计数仍如实出现在 by_kind 里。
        assert fc["by_kind"].get("platform") == 1
    finally:
        conn.close()


def test_module_and_low_coverage_form(tmp_path: Path):
    conn = _kb_with_fields(tmp_path)
    try:
        c = coverage.coverage(conn)
        fc = c["field_coverage"]
        mods = {m["module"]: m for m in fc["by_module"]}
        assert mods["modA"]["business"] == 5 and mods["modA"]["touched"] == 2
        # F1 有插件、3 字段、覆盖 2/3 → 不算低；F2 无插件 → 不纳入。
        # 把阈值场景做实：F1 覆盖率 0.67 > 0.5，故无低覆盖单据。
        keys = {f["key"] for f in fc["low_coverage_forms"]}
        assert "f2" not in keys  # 无插件不纳入
    finally:
        conn.close()


def test_quality_breakdowns(tmp_path: Path):
    conn = _kb_with_fields(tmp_path)
    try:
        c = coverage.coverage(conn)
        rq = c["resolution_quality"]
        assert rq["total"] == 4
        assert rq["reliable"] == 3  # literal×2 + constant×1
        assert rq["by_resolution"].get("ambiguous") == 1

        lq = c["location_quality"]
        assert lq["total"] == 4 and lq["unlocated_form"] == 1
        assert lq["located"] == 3  # 三条 form_key 非空且 level!=unknown

        pq = c["persist_quality"]
        assert pq["write_total"] == 3
        assert pq["persisting"] == 1 and pq["memory_only"] == 1 and pq["uncertain"] == 1

        mm = c["meta_match"]
        # 解析出的 field_key：a×2, b×1, zzz×1 = 4 条；命中元数据 = a,b 共 3 条。
        assert mm["resolved"] == 4 and mm["matched"] == 3 and mm["unmatched"] == 1
    finally:
        conn.close()


def test_render_text(tmp_path: Path):
    conn = _kb_with_fields(tmp_path)
    try:
        c = coverage.coverage(conn)
        text = coverage.render_coverage(c)
        assert "字段覆盖率" in text
        assert "扫描质量分解" in text
        assert c["verdict"]["text"] in text
    finally:
        conn.close()


def test_java_unavailable_verdict(tmp_path: Path):
    conn = _kb_with_fields(tmp_path)
    try:
        conn.execute("UPDATE kb_meta SET value='{\"available\": false}' "
                     "WHERE key='java_analysis'")
        conn.commit()
        c = coverage.coverage(conn)
        assert c["verdict"]["level"] == "blocked"
    finally:
        conn.close()
