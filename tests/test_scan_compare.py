"""扫描可信度报告测试 —— 手段二「粗精度扫描 vs 高精度扫描对比」。

两组测试：
  * `_populate_coarse`（建库期的粗扫侧）：源码字面量抽取 + rw-idiom 判定 + 去重 + 行号 +
    平台字段不入 + **注释跳过** + **常量名引用召回**（复用常量表）。
  * `scan_compare.compare`（报告侧）：粗扫/高精度集合分桶（both/coarse_only/high_only/
    neither）、疑似盲点清单排序与证据、verdict 与 java 未启用兜底。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from cosmic_kb.graph import store
from cosmic_kb.java.constants import ConstantTable
from cosmic_kb.report import scan_compare

_SCHEMA = Path(store.__file__).with_name("schema.sql")

# 粗扫不再依赖常量表时用的空 res（const_table=None → 退化为纯字面量扫描）。
_NO_CONST = SimpleNamespace(const_table=None)


# ── 粗扫侧：_populate_coarse ──────────────────────────────────────────────

def test_populate_coarse_literals_idiom_dedup(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))

    text = (
        "package x;\n"                            # 1
        'o.getValue("cqkd_amount");\n'            # 2  amount · rw-idiom
        'String s = "cqkd_amount";\n'             # 3  amount · literal
        'bill.set("cqkd_remark", v);\n'           # 4  remark · rw-idiom
        'load("id");\n'                           # 5  平台字段 → 不入
        'foo("cqkd_amount"); bar("cqkd_amount");\n'  # 6  同行两次 → 去重为一（literal）
    )
    scan = SimpleNamespace(ok_files=[SimpleNamespace(relpath="A.java", text=text)])
    models = [SimpleNamespace(form_type="bill", fields=[
        SimpleNamespace(key="cqkd_amount", kind="entity"),
        SimpleNamespace(key="cqkd_remark", kind="entity"),
        SimpleNamespace(key="id", kind="platform"),   # 平台字段不进粗扫
    ])]

    counts = store._populate_coarse(conn, scan, models, _NO_CONST)
    assert counts["coarse_field_hit"] == 4
    got = {(r["field_key"], r["line"], r["via"])
           for r in conn.execute("SELECT field_key,line,via FROM coarse_field_hit")}
    assert got == {
        ("cqkd_amount", 2, "rw-idiom"),
        ("cqkd_amount", 3, "literal"),
        ("cqkd_amount", 6, "literal"),   # 同行第二次被去重
        ("cqkd_remark", 4, "rw-idiom"),
    }
    conn.close()


def test_populate_coarse_no_business_fields(tmp_path: Path):
    """没有业务字段时不应抛、写 0 行。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    scan = SimpleNamespace(ok_files=[SimpleNamespace(relpath="A.java", text='x("cqkd_a");')])
    models = [SimpleNamespace(form_type="bill", fields=[
        SimpleNamespace(key="id", kind="platform")])]
    assert store._populate_coarse(conn, scan, models, _NO_CONST)["coarse_field_hit"] == 0
    conn.close()


def test_populate_coarse_skips_comments(tmp_path: Path):
    """注释里的字段标识/常量名不计入（验收硬伤①：根治注释被扫进粗扫）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))

    text = (
        'package x;\n'                              # 1
        '// 注释里写 "cqkd_amount" 不该计入\n'       # 2  行注释 → 跳过
        '/* 块注释 "cqkd_amount" 也跳过 */\n'        # 3  块注释 → 跳过
        'o.getValue("cqkd_amount");\n'             # 4  唯一真实命中（rw-idiom）
        'String url = "http://x"; // 行尾注释 "cqkd_amount"\n'  # 5  行尾注释里的也跳过
    )
    scan = SimpleNamespace(ok_files=[SimpleNamespace(relpath="A.java", text=text)])
    models = [SimpleNamespace(form_type="bill", fields=[
        SimpleNamespace(key="cqkd_amount", kind="entity")])]

    store._populate_coarse(conn, scan, models, _NO_CONST)
    got = {(r["field_key"], r["line"], r["via"])
           for r in conn.execute("SELECT field_key,line,via FROM coarse_field_hit")}
    assert got == {("cqkd_amount", 4, "rw-idiom")}    # 仅第 4 行的真实读写
    conn.close()


def test_populate_coarse_resolves_constants(tmp_path: Path):
    """常量名引用经常量表反查也算召回（验收硬伤②：常量未解析导致漏扫）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))

    ct = ConstantTable()
    ct._add("BillConst", "AMOUNT", "cqkd_amount")     # 唯一映射 → 可反查
    ct._add("BillConst", "AMBI", "cqkd_x")            # 歧义常量：同名两值 → 不归
    ct._add("OtherConst", "AMBI", "cqkd_y")
    res = SimpleNamespace(const_table=ct)

    text = (
        'package x;\n'                                   # 1
        'public static final String AMOUNT = "cqkd_amount";\n'  # 2  定义处（字面量+常量名同行）
        'o.getValue(BillConst.AMOUNT);\n'               # 3  常量名作 get 首参 → const-rw-idiom
        'foo(AMOUNT);\n'                                 # 4  裸常量名引用 → const-ref
        'o.getValue(AMBI);\n'                            # 5  歧义常量 → 不归
    )
    scan = SimpleNamespace(ok_files=[SimpleNamespace(relpath="A.java", text=text)])
    models = [SimpleNamespace(form_type="bill", fields=[
        SimpleNamespace(key="cqkd_amount", kind="entity"),
        SimpleNamespace(key="cqkd_x", kind="entity"),
        SimpleNamespace(key="cqkd_y", kind="entity")])]

    store._populate_coarse(conn, scan, models, res)
    got = {(r["field_key"], r["line"], r["via"])
           for r in conn.execute("SELECT field_key,line,via FROM coarse_field_hit")}
    # 第 2 行：字面量 "cqkd_amount" 与常量名 AMOUNT 同行同字段 → 去重保留先到（常量名 const-ref）；
    # 第 3 行：const-rw-idiom（强信号）；第 4 行：const-ref；第 5 行歧义常量不计。
    assert got == {
        ("cqkd_amount", 2, "const-ref"),
        ("cqkd_amount", 3, "const-rw-idiom"),
        ("cqkd_amount", 4, "const-ref"),
    }
    conn.close()


# ── 报告侧：compare ──────────────────────────────────────────────────────

def _kb(tmp_path: Path) -> sqlite3.Connection:
    """手填 field / field_access / coarse_field_hit，造确定的集合分桶。

    业务字段全集 U = {a,b,c,d,e}（id 为 platform，不计）。
    高精度 H = {a,b}；粗扫 C = {a,c,d}。
      → both={a} · coarse_only={c(idiom),d} · high_only={b} · neither={e}
    """
    conn = sqlite3.connect(str(tmp_path / "kb.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))

    conn.execute("INSERT INTO form(key,name,form_type,module) VALUES('f1','单据一','bill','modA')")
    fields = [
        ("f1::h::a", "f1", "h", "a", "甲", None, "T", "entity", "header"),
        ("f1::h::b", "f1", "h", "b", "乙", None, "T", "entity", "header"),
        ("f1::h::c", "f1", "h", "c", "丙", None, "T", "entity", "header"),
        ("f1::h::d", "f1", "h", "d", "丁", None, "T", "entity", "header"),
        ("f1::h::e", "f1", "h", "e", "戊", None, "T", "entity", "header"),
        ("f1::h::id", "f1", "h", "id", "主键", None, "T", "platform", "header"),
    ]
    conn.executemany("INSERT INTO field VALUES(?,?,?,?,?,?,?,?,?)", fields)

    def fa(fkey, access):
        return ("f1", fkey, "header", None, "cqspb.P", "form", "cqspb.P",
                "ev", "transaction", access, "yes" if access == "write" else "na",
                "r", "set", 1, "[]", "literal", 1.0, "P.java", "")
    conn.executemany(
        "INSERT INTO field_access VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [fa("a", "write"), fa("b", "read")])

    conn.executemany(
        "INSERT INTO coarse_field_hit VALUES(?,?,?,?)",
        [("a", "P.java", 10, "rw-idiom"),
         ("c", "P.java", 20, "rw-idiom"),  # c 含 rw-idiom → 强信号盲点
         ("c", "Q.java", 5, "literal"),    # c 第二处
         ("d", "P.java", 30, "literal")],
    )
    conn.execute("INSERT INTO kb_meta(key,value) VALUES('java_analysis','{\"available\": true}')")
    conn.commit()
    return conn


def test_compare_buckets(tmp_path: Path):
    conn = _kb(tmp_path)
    try:
        c = scan_compare.compare(conn)
        assert c["universe"] == 5
        assert c["coarse_hit"] == 3 and c["high_hit"] == 2
        assert c["both"] == 1
        assert c["coarse_only"] == 2 and c["coarse_only_idiom"] == 1
        assert c["coarse_only_literal"] == 1
        assert c["high_only"] == 1
        assert c["neither"] == 1
        assert c["agreement_rate"] == round(1 / 4, 4)
    finally:
        conn.close()


def test_compare_coarse_only_list_sorted_with_evidence(tmp_path: Path):
    conn = _kb(tmp_path)
    try:
        c = scan_compare.compare(conn)
        col = c["coarse_only_list"]
        keys = [d["key"] for d in col]
        assert keys == ["c", "d"]          # rw-idiom(c) 排在 literal(d) 前
        c_row = col[0]
        assert c_row["idiom"] is True and c_row["hits"] == 2
        assert {l["relpath"] for l in c_row["locations"]} == {"P.java", "Q.java"}
        assert c_row["forms"] == ["f1"]
        # high_only 含 b（粗扫没逮到、高精度独有）。
        assert [d["key"] for d in c["high_only_list"]] == ["b"]
    finally:
        conn.close()


def test_compare_render_text(tmp_path: Path):
    conn = _kb(tmp_path)
    try:
        c = scan_compare.compare(conn)
        text = scan_compare.render_compare(c)
        assert "粗精度扫描 vs 高精度扫描对比" in text
        assert "疑似盲点" in text
        assert c["verdict"]["text"] in text
    finally:
        conn.close()


def test_compare_java_unavailable(tmp_path: Path):
    conn = _kb(tmp_path)
    try:
        conn.execute("UPDATE kb_meta SET value='{\"available\": false}' WHERE key='java_analysis'")
        conn.commit()
        c = scan_compare.compare(conn)
        assert c["verdict"]["level"] == "blocked"
        # render 在 java 未启用时只出结论横幅、不抛。
        assert "未启用" in scan_compare.render_compare(c)
    finally:
        conn.close()
