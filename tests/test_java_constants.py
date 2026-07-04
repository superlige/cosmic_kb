"""全局常量值表 · 定义记录持久化（java_constant 地基，供 read_source 解析限定常量引用）。

真实翻车案例：`TemporaryStopCon.ENTITY` 的字面值 `cqkd_ltyz` 不出现在被分析文件的正文里，
大模型没法从文本猜出来、只能凭常量英文名瞎译中文含义。`ConstantTable.records` 把定义（含
源文件+行号）留证，供建库落进 `java_constant` 表；`resolve()`（by_class/by_field）用于
建库期字段 key 解析，两者互不干扰。
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_java")

from cosmic_kb.java import ast_index as ax
from cosmic_kb.java import constants as const_mod


def test_collect_into_records_class_relpath_line():
    src = (
        "package cqspb.bd.common.cons;\n"
        "public class TemporaryStopCon {\n"
        "  public static final String ENTITY = \"cqkd_ltyz\";\n"
        "  public static final String KEY_AMOUNT = \"cqkd_amt\";\n"
        "}\n"
    )
    root = ax.parse_tree(src)
    table = const_mod.ConstantTable()
    const_mod.collect_into(root, table, "cqspb/bd/common/cons/TemporaryStopCon.java")

    # resolve() 仍走 by_class（建库期字段 key 解析，未受影响）。
    assert table.resolve("TemporaryStopCon.ENTITY").value == "cqkd_ltyz"

    recs = {(c, n): (lit, rp, ln) for c, n, lit, rp, ln in table.records}
    lit, rp, ln = recs[("TemporaryStopCon", "ENTITY")]
    assert lit == "cqkd_ltyz"
    assert rp == "cqspb/bd/common/cons/TemporaryStopCon.java"
    assert ln == 3


def test_records_keep_every_definition_even_when_class_names_collide():
    """两个不同包下同名类各定义 ENTITY 不同字面值：records 不折叠去重，供查询侧判歧义。"""
    src_a = 'package a; public class Con {\n  public static final String ENTITY = "cqkd_a";\n}\n'
    src_b = 'package b; public class Con {\n  public static final String ENTITY = "cqkd_b";\n}\n'
    table = const_mod.ConstantTable()
    const_mod.collect_into(ax.parse_tree(src_a), table, "a/Con.java")
    const_mod.collect_into(ax.parse_tree(src_b), table, "b/Con.java")

    lits = {lit for c, n, lit, _rp, _ln in table.records if c == "Con" and n == "ENTITY"}
    assert lits == {"cqkd_a", "cqkd_b"}


def test_build_constant_table_from_scan_result(tmp_path):
    from cosmic_kb.ingest import scanner

    src = tmp_path / "src"
    src.mkdir()
    (src / "TemporaryStopCon.java").write_text(
        'package cqspb.bd; public class TemporaryStopCon {\n'
        '  public static final String ENTITY = "cqkd_ltyz";\n'
        '}\n',
        encoding="utf-8",
    )
    scan = scanner.scan(src)
    table = const_mod.build_constant_table(scan)
    recs = {(c, n): (lit, rp) for c, n, lit, rp, _ln in table.records}
    assert recs[("TemporaryStopCon", "ENTITY")] == ("cqkd_ltyz", "TemporaryStopCon.java")
