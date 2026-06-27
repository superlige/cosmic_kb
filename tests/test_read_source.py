"""模式 A 验收测试 —— read_source：读源码（野生编码正确解码）+ 按三档置信标注字段中文名+归属。

让段二大模型读源码走我们的工具而非宿主原生 reader：扫文件里出现的已知字段 key（含 `KEY_X="cqkd_x"`
的字面值，无需常量表），打元数据词典回真实中文名，并按本文件 `field_access` 解析出的**数据包来源实体**
收敛同名候选——三档置信（unique / resolved / ambiguous），杜绝把多张单据的同名字段平铺误导模型。

复用 `_synthkb`（含 `cqkd_collateralstatus「抵押状态」`、`cqkd_entry「资产明细」`），再补两个**跨单据同名
但中文名不同**的字段（消歧场景）+ 一个本文件 field_access 行 + 真实源文件 + source_args。
"""

from __future__ import annotations

from pathlib import Path

from cosmic_kb.graph import store
from cosmic_kb.report import read_source

from _synthkb import make_kb

# 源码里既有直接字面量 "cqkd_collateralstatus"，也有常量定义 KEY_ST="cqkd_collateralstatus"
# （字面值就在正文，扫文本即命中）；cqkd_fee/cqkd_rate 是跨单据同名异义字段（消歧用）；
# 还有一个非字段的普通标识 notAFieldKey（不该被标注）。
SRC = """package cqspb.am;
public class CollateralOp {
  private static final String KEY_ST = "cqkd_collateralstatus";
  public void beforeExecuteOperationTransaction(BeforeOperationArgs e) {
    DynamicObject bill = e.getDataEntities()[0];
    int notAFieldKey = 1;
    DynamicObject ht = BusinessDataServiceHelper.loadSingle(id, "cqkd_contract");
    String fee = ht.getString("cqkd_fee");
    bill.set(KEY_ST, "B");
    bill.getDynamicObjectCollection("cqkd_entry");
    DynamicObjectCollection httz = bill.getDynamicObjectCollection("cqkd_httz");
    String rate = readSomehow("cqkd_rate");
  }
}
"""

REL = "cqspb/am/CollateralOp.java"


def _kb_with_source(tmp_path: Path) -> tuple[Path, Path]:
    db = make_kb(tmp_path)
    src = tmp_path / "src"
    (src / "cqspb" / "am").mkdir(parents=True, exist_ok=True)
    (src / "cqspb" / "am" / "CollateralOp.java").write_bytes(SRC.encode("utf-8"))
    conn = store.open_kb(db)
    try:
        conn.execute("INSERT OR REPLACE INTO kb_meta(key,value) VALUES('source_args', ?)",
                     (f'{{"source_root": "{src.as_posix()}"}}',))
        # 跨单据同名异义字段（消歧场景）：cqkd_fee 在两单名字不同；cqkd_rate 同理。
        conn.executemany(
            "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [
                ("f_fee1", "cqkd_assetcard", "cqkd_assetcard", "cqkd_fee", "服务费",
                 "ffee", "AmountField", "entity", "header"),
                ("f_fee2", "cqkd_contract", "cqkd_contract", "cqkd_fee", "手续费",
                 "ffee", "AmountField", "entity", "header"),
                ("f_rate1", "cqkd_assetcard", "cqkd_assetcard", "cqkd_rate", "税率",
                 "frate", "DecimalField", "entity", "header"),
                ("f_rate2", "cqkd_contract", "cqkd_contract", "cqkd_rate", "利率",
                 "frate", "DecimalField", "entity", "header"),
                # 多选基础资料字段：getDynamicObjectCollection 取的是选中基础资料集合，不是分录（本 bug 核心）。
                ("f_httz", "cqkd_assetcard", "cqkd_assetcard", "cqkd_httz", "退租合同",
                 "fhttz", "MulBasedataField", "entity", "header"),
            ],
        )
        # 本文件 field_access 把 cqkd_fee 解析到 cqkd_contract（loadSingle 取到的合同实体）→ 应收敛。
        # cqkd_rate 故意不给 field_access（动态读取，钉不出实体）→ 应判歧义。
        conn.execute(
            "INSERT INTO field_access(form_key,field_key,level,entry_key,plugin_fqn,plugin_type,"
            "access_class,event_method,event_phase,access,persists,persist_reason,via,line,path,"
            "key_resolution,confidence,source_relpath,evidence) "
            "VALUES('cqkd_contract','cqkd_fee','header',NULL,'cqspb.am.CollateralOp','op',"
            "'cqspb.am.CollateralOp','beforeExecuteOperationTransaction','transaction','read','na',"
            "NULL,'do.getString',8,'[]','literal',0.9,?, '')",
            (REL,),
        )
        conn.commit()
    finally:
        conn.close()
    return db, src


def test_read_source_annotates_known_fields(tmp_path: Path):
    db, _src = _kb_with_source(tmp_path)
    conn = store.open_kb(db)
    try:
        d = read_source.read_source(conn, REL)
        assert d["found"] is True
        assert 'class CollateralOp' in d["content"]
        fn = d["field_names"]
        # 唯一字段：直接命中真名（直接字面量 / 常量字面值都算）。
        st = fn["cqkd_collateralstatus"]
        assert st["tier"] == "unique"
        assert st["names"] == ["抵押状态"]
        # 分录容器 key（在 entity 表）也标注，且唯一。
        assert fn["cqkd_entry"]["tier"] == "unique"
        assert fn["cqkd_entry"]["names"] == ["资产明细"]
        # 非字段标识不被臆造进标注。
        assert "notAFieldKey" not in fn
        assert d["note"]
        # 防 host 截断：标注在前、content 垫底；省略计数提到顶层。
        ks = list(d)
        assert ks.index("field_names") < ks.index("content")
        assert d["keys_omitted"] == 0
        # 坐标已瘦身：unique 坐标丢 entity_key/field_kind/parent_key，保留取值语义信号。
        coord = st["coordinates"][0]
        assert "entity_key" not in coord and "parent_key" not in coord
        assert set(coord) <= {"kind", "name", "form_key", "level",
                              "field_type", "access", "resolved_lines"}
    finally:
        conn.close()


def test_read_source_basedata_access_hint(tmp_path: Path):
    """多选基础资料字段 cqkd_httz：坐标带 field_type + access 取值语义，
    显式提示 getDynamicObjectCollection 取的是基础资料集合而非分录（堵本 bug）。"""
    db, _src = _kb_with_source(tmp_path)
    conn = store.open_kb(db)
    try:
        d = read_source.read_source(conn, REL)
        httz = d["field_names"]["cqkd_httz"]
        assert httz["tier"] == "unique"
        assert httz["names"] == ["退租合同"]
        coord = httz["coordinates"][0]
        assert coord["field_type"] == "MulBasedataField"
        assert "不是分录" in coord["access"]
        # 顶层 note 与 render 文本都点出 getDynamicObjectCollection 判别。
        assert "getDynamicObjectCollection" in d["note"]
        text = read_source.render_read_source(d)
        assert "不是分录" in text and "MulBasedataField" in text
    finally:
        conn.close()


def test_ambiguous_coords_capped(tmp_path: Path):
    """跨 >8 单据同名的 ambiguous key：候选数封顶（防 field_names 体积爆掉被 host 截断），带剩余计数。"""
    db, _src = _kb_with_source(tmp_path)
    conn = store.open_kb(db)
    try:
        # 在源码出现的 cqkd_rate 上再造 10 张单据的同名字段（共 12 张），且本文件无 field_access 解析→歧义。
        conn.executemany(
            "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(f"f_rate_x{i}", f"cqkd_form{i}", f"cqkd_form{i}", "cqkd_rate", f"利率{i}",
              "frate", "DecimalField", "entity", "header") for i in range(10)],
        )
        conn.commit()
        rate = read_source.read_source(conn, REL)["field_names"]["cqkd_rate"]
        assert rate["tier"] == "ambiguous"
        assert len(rate["coordinates"]) == 8          # 封顶
        assert rate["coordinates_capped"] > 0         # 剩余计数
        assert "resolve_fields" in rate["note"]
    finally:
        conn.close()


def test_resolved_tier_collapses_to_loaded_entity(tmp_path: Path):
    """跨单据同名字段 cqkd_fee：本文件 field_access 解析到 cqkd_contract（loadSingle 的合同实体）
    → 收敛到「手续费」，不平铺另一单的「服务费」，并附依据行号 + 标明其余单据。"""
    db, _src = _kb_with_source(tmp_path)
    conn = store.open_kb(db)
    try:
        fee = read_source.read_source(conn, REL)["field_names"]["cqkd_fee"]
        assert fee["tier"] == "resolved"
        assert fee["names"] == ["手续费"]            # 收敛到 cqkd_contract，不是 cqkd_assetcard 的「服务费」
        assert len(fee["coordinates"]) == 1
        assert fee["coordinates"][0]["form_key"] == "cqkd_contract"
        assert fee["coordinates"][0]["resolved_lines"] == [8]
        assert "cqkd_assetcard" in fee["also_in_forms"]   # 诚实告知另有同名字段
    finally:
        conn.close()


def test_ambiguous_tier_when_entity_unresolved(tmp_path: Path):
    """跨单据同名字段 cqkd_rate：本文件没有 field_access 解析到实体 → 显式标歧义、不替选、给消歧方向。"""
    db, _src = _kb_with_source(tmp_path)
    conn = store.open_kb(db)
    try:
        rate = read_source.read_source(conn, REL)["field_names"]["cqkd_rate"]
        assert rate["tier"] == "ambiguous"
        assert rate["names"] == []                        # 绝不替选某一个
        assert len(rate["coordinates"]) == 2              # 两单候选都摆出（标歧义而非平铺成已确定）
        assert "调用链" in rate["note"] and "勿默认当前单据" in rate["note"]
    finally:
        conn.close()


def test_resolved_by_metadata_reverse_is_honest(tmp_path: Path):
    """收敛依据是字段 key 反查元数据回填（form_key_source=metadata_unique）时，note 须诚实标明
    依据是字段归属、resolved_lines 仅读写所在行，不冒充数据流证明（红线 #4）。"""
    db, _src = _kb_with_source(tmp_path)
    conn = store.open_kb(db)
    try:
        # cqkd_rate 跨两单同名，本文件给一条按字段key反查回填到 cqkd_contract 的 field_access 行。
        conn.execute(
            "INSERT INTO field_access(form_key,field_key,level,entry_key,plugin_fqn,plugin_type,"
            "access_class,event_method,event_phase,access,persists,persist_reason,via,line,path,"
            "key_resolution,confidence,source_relpath,evidence,form_key_source) "
            "VALUES('cqkd_contract','cqkd_rate','header',NULL,'cqspb.am.CollateralOp','op',"
            "'cqspb.am.CollateralOp','beforeExecuteOperationTransaction','transaction','read','na',"
            "NULL,'do.get',12,'[]','literal',0.9,?, '', 'metadata_unique')",
            (REL,),
        )
        conn.commit()
        rate = read_source.read_source(conn, REL)["field_names"]["cqkd_rate"]
        assert rate["tier"] == "resolved"
        assert rate["names"] == ["利率"]                   # 收敛到 cqkd_contract
        assert "字段归属元数据反查" in rate["note"]
        assert "非数据流" in rate["note"]
    finally:
        conn.close()


def test_read_source_line_slice(tmp_path: Path):
    db, _src = _kb_with_source(tmp_path)
    conn = store.open_kb(db)
    try:
        d = read_source.read_source(conn, REL, start=3, end=3)
        assert d["lines"] == [3, 3]
        assert "KEY_ST" in d["content"]
        assert "class CollateralOp" not in d["content"]   # 第3行只含常量定义
        # 第3行的字面值 cqkd_collateralstatus 仍被标注。
        assert "cqkd_collateralstatus" in d["field_names"]
    finally:
        conn.close()


def test_read_source_rejects_path_traversal(tmp_path: Path):
    db, _src = _kb_with_source(tmp_path)
    conn = store.open_kb(db)
    try:
        d = read_source.read_source(conn, "../../../etc/passwd")
        assert d["found"] is False
        assert "越界" in d["note"]
    finally:
        conn.close()


def test_read_source_missing_file(tmp_path: Path):
    db, _src = _kb_with_source(tmp_path)
    conn = store.open_kb(db)
    try:
        d = read_source.read_source(conn, "cqspb/am/Nope.java")
        assert d["found"] is False
    finally:
        conn.close()


def test_render_read_source(tmp_path: Path):
    db, _src = _kb_with_source(tmp_path)
    conn = store.open_kb(db)
    try:
        d = read_source.read_source(conn, REL)
        text = read_source.render_read_source(d)
        assert "已核对字段名" in text and "抵押状态" in text
        assert "手续费" in text                       # resolved 档展示收敛后的名
        assert "歧义" in text                         # ambiguous 档显式标注
        assert "class CollateralOp" in text          # 源码正文带行号
    finally:
        conn.close()


def test_mcp_read_source_same_as_report(tmp_path: Path, monkeypatch):
    from cosmic_kb.mcp import server as mcp_server

    db, _src = _kb_with_source(tmp_path)
    monkeypatch.setenv("COSMIC_KB_DB", str(db))
    got = mcp_server.tool_read_source(REL)
    conn = store.open_kb(db)
    try:
        want = read_source.read_source(conn, REL)
    finally:
        conn.close()
    assert got == want
    assert "read_source" in mcp_server.TOOLS


def test_cli_source_json(tmp_path: Path, capsys):
    from cosmic_kb.cli.main import main

    db, _src = _kb_with_source(tmp_path)
    rc = main(["source", REL, "--db", str(db), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "抵押状态" in out and "field_names" in out
