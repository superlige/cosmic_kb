"""read_source 紧凑投影（防 MCP 32KB 截断）测试：标注在前 + content 按字节预算填充 + 游标分页。

与 test_trace_compact / test_bill_compact 同思路：富 read_source 把整文件 `content` 整串返回，大文件
经 host 会被从中段硬切（被砍的恰是不可重读的尾部）。read_source_compact 把 content 按 `_wire_len`
预算填到上限、未读全给 `content_next_cursor`，`field_names` 超档给 `field_names_next_cursor`，让消费方
逐页取回全部（红线 #4：不仅报计数、还可达）。
"""

from __future__ import annotations

from pathlib import Path

from cosmic_kb.graph import store
from cosmic_kb.report import read_source as R
from cosmic_kb.report.field_trace import _wire_len, _COMPACT_BUDGET, _parse_cursor

from _synthkb import make_kb

REL = "cqspb/am/Big.java"
N_KEYS = 120          # 远超 field_names 单档 cap → 必然超档分页
FILLER = 1400         # 拉爆文件体积（> 32KB）→ 必然 content 分页


def _big_src() -> str:
    lines = ["package cqspb.am;", "public class Big {", "  void m(DynamicObject bill, Object v) {"]
    for i in range(N_KEYS):
        lines.append(f'    bill.set("cqkd_g{i:03d}", v);   // 填充注释 padding padding padding')
    for j in range(FILLER):
        lines.append(f"    int filler{j:04d} = {j}; // 拉长文件体积 padding padding padding padding")
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


def _kb_big(tmp_path: Path) -> Path:
    db = make_kb(tmp_path)
    src = tmp_path / "src"
    (src / "cqspb" / "am").mkdir(parents=True, exist_ok=True)
    (src / "cqspb" / "am" / "Big.java").write_bytes(_big_src().encode("utf-8"))
    conn = store.open_kb(db)
    try:
        conn.execute("INSERT OR REPLACE INTO kb_meta(key,value) VALUES('source_args', ?)",
                     (f'{{"source_root": "{src.as_posix()}"}}',))
        # 每个 cqkd_gNNN 唯一归属一张单据 → unique 档，标注小而可数。
        conn.executemany(
            "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(f"ug{i}", f"cqkd_form{i:03d}", f"cqkd_form{i:03d}", f"cqkd_g{i:03d}", f"字段{i}",
              f"fg{i}", "TextField", "entity", "header") for i in range(N_KEYS)],
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _conn(db: Path):
    return store.open_kb(db)


def test_overview_under_budget_and_gives_cursors(tmp_path: Path):
    """大文件 + 多字段 → overview 仍 ≤ 预算，且 content / field_names 都给翻页游标（标注在前、内容垫底）。"""
    db = _kb_big(tmp_path)
    conn = _conn(db)
    try:
        ov = R.read_source_compact(conn, REL)
    finally:
        conn.close()
    assert ov["found"] is True
    assert _wire_len(ov) <= _COMPACT_BUDGET
    # 标注在前、content 垫底（host 截尾牺牲可续读的源码、保住标注）。
    ks = list(ov)
    assert ks.index("field_names") < ks.index("content")
    # 文件 > 32KB → content 必然未读全、给续读游标。
    assert ov["content_next_cursor"].startswith("content@")
    assert ov["content_capped_lines"] > 0
    # 120 个已知 key 超 field_names 单档 → 给标注翻页游标。
    assert ov["field_names_next_cursor"].startswith("field_names@")
    assert ov["keys_omitted"] > 0
    # 展示出来的标注是已核对真名（unique 档照抄）。
    shown = ov["field_names"]
    any_key = next(iter(shown))
    assert shown[any_key]["tier"] == "unique"


def test_content_pagination_retrieves_whole_file(tmp_path: Path):
    """content 逐页续读至文件末尾，拼回应与富 read_source 整文件 content 逐字节一致（一行不丢）。"""
    db = _kb_big(tmp_path)
    conn = _conn(db)
    try:
        # 紧凑投影按 splitlines() 切行（规整尾换行）→ 拼回与 splitlines 口径比对。
        full = "\n".join(R.read_source(conn, REL, max_keys=10 ** 9)["content"].splitlines())
        parts, cur, pages = [], "content@1", 0
        while cur:
            r = R.read_source_compact(conn, REL, cursor=cur)
            pg = r["page"]
            assert pg["section"] == "content"
            parts.append(pg["content"])
            cur = pg["next_cursor"]
            pages += 1
            assert _wire_len(r) < 32768
            assert pages < 10000, "翻页未收敛"
    finally:
        conn.close()
    assert "\n".join(parts) == full
    assert pages > 1, "大文件应分多页"


def test_content_pagination_tiny_budget_multipage(tmp_path: Path):
    """极小 budget 下 content 仍分多页、逐页 ≤ 32KB、每页至少一行、合起来不丢。"""
    db = _kb_big(tmp_path)
    conn = _conn(db)
    try:
        # 紧凑投影按 splitlines() 切行（规整尾换行）→ 拼回与 splitlines 口径比对。
        full = "\n".join(R.read_source(conn, REL, max_keys=10 ** 9)["content"].splitlines())
        rich = R.read_source(conn, REL, max_keys=10 ** 9)
        parts, start, pages = [], 1, 0
        while True:
            r = R._rs_page_content(rich, start, budget=900)
            pg = r["page"]
            parts.append(pg["content"])
            assert pg["content"], "每页至少一行"
            pages += 1
            if not pg["next_cursor"]:
                break
            start = _parse_cursor(pg["next_cursor"])[1]
            assert pages < 100000
    finally:
        conn.close()
    assert "\n".join(parts) == full
    assert pages > 5, "极小预算应分很多页"


def test_field_names_pagination_retrieves_all(tmp_path: Path):
    """field_names 被超档截断的标注可逐页取回全部（一条不丢），每条带已核对名。"""
    db = _kb_big(tmp_path)
    conn = _conn(db)
    try:
        rich = R.read_source(conn, REL, max_keys=10 ** 9)
        total = len(rich["field_names"])
        got, cur, pages = [], "field_names@0", 0
        while cur:
            r = R.read_source_compact(conn, REL, cursor=cur)
            pg = r["page"]
            assert pg["section"] == "field_names"
            got += pg["items"]
            cur = pg["next_cursor"]
            pages += 1
            assert _wire_len(r) < 32768
            assert pages < 10000, "翻页未收敛"
    finally:
        conn.close()
    assert len(got) == total >= N_KEYS
    keys = {it["key"] for it in got}
    assert "cqkd_g000" in keys and "cqkd_g119" in keys


def test_line_window_respected_then_content_cursor(tmp_path: Path):
    """指定 start/end 窗口：overview content 从窗口起始填充；窗口装不下时给续读游标。"""
    db = _kb_big(tmp_path)
    conn = _conn(db)
    try:
        ov = R.read_source_compact(conn, REL, start=4, end=4)
        # 第 4 行是首个 bill.set("cqkd_g000") → 窗口很小，整窗装得下、无续读游标。
        assert ov["lines"][0] == 4
        assert 'cqkd_g000' in ov["content"]
        assert ov.get("content_next_cursor") is None
    finally:
        conn.close()


def test_unknown_section_errors(tmp_path: Path):
    """未知/不可分页段 → page.error 引导（不静默返回空）。"""
    db = _kb_big(tmp_path)
    conn = _conn(db)
    try:
        r = R.read_source_compact(conn, REL, cursor="nope@0")
    finally:
        conn.close()
    assert "error" in r["page"]


def test_missing_file_returns_small_dict(tmp_path: Path):
    """文件不存在 → 直接回富层的小错误 dict（不进分页/governor）。"""
    db = _kb_big(tmp_path)
    conn = _conn(db)
    try:
        r = R.read_source_compact(conn, "cqspb/am/Nope.java")
    finally:
        conn.close()
    assert r["found"] is False
