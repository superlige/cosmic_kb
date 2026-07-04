"""CLI 模式 A 验收测试 —— read_source：读源码（野生编码正确解码）+ 按三档置信标注字段中文名+归属。

`cosmic_kb source` 终端排障用：扫文件里出现的已知字段 key（含 `KEY_X="cqkd_x"` 的字面值，无需
常量表），打元数据词典回真实中文名，并按本文件 `field_access` 解析出的**数据包来源实体**收敛同名
候选——三档置信（unique / resolved / ambiguous），杜绝把多张单据的同名字段平铺误导。

MCP `read_source` 工具已于 2026-07-05 退役（段二改为宿主自带 reader + `resolve_fields` 精确核对，
见 `docs/阶段验收.md` 对应条目），本文件只保留 CLI 富模式覆盖。

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


# ── 限定常量引用解析（TemporaryStopCon.ENTITY 真实翻车案例）──────────────────
# 字面值 cqkd_ltyz 不出现在这段源码正文里，只有靠查 java_constant 表才能解出，
# 堵"模型凭常量英文名猜中文单据名"的口子。Boolean.TRUE 是非项目常量，验证静默不标注。
CONST_SRC = """package cqspb.am;
public class TemporaryStopTask {
  public void execute() {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, TemporaryStopCon.ENTITY);
    boolean flag = Boolean.TRUE;
  }
}
"""

CONST_REL = "cqspb/am/TemporaryStopTask.java"


def _kb_with_constant_ref(tmp_path: Path) -> tuple[Path, Path]:
    db = make_kb(tmp_path)
    src = tmp_path / "src"
    (src / "cqspb" / "am").mkdir(parents=True, exist_ok=True)
    (src / "cqspb" / "am" / "TemporaryStopTask.java").write_bytes(CONST_SRC.encode("utf-8"))
    conn = store.open_kb(db)
    try:
        conn.execute("INSERT OR REPLACE INTO kb_meta(key,value) VALUES('source_args', ?)",
                     (f'{{"source_root": "{src.as_posix()}"}}',))
        conn.execute(
            "INSERT INTO entity(form_key,key,name,level,parent_key,table_name) VALUES(?,?,?,?,?,?)",
            ("cqkd_ltyz", "cqkd_ltyz", "临时收入", "header", None, "t_ltyz"),
        )
        conn.execute(
            "INSERT INTO java_constant VALUES(?,?,?,?,?)",
            ("TemporaryStopCon", "ENTITY", "cqkd_ltyz",
             "cqspb/bd/common/cons/TemporaryStopCon.java", 12),
        )
        conn.commit()
    finally:
        conn.close()
    return db, src


def test_read_source_resolves_qualified_constant(tmp_path: Path):
    db, _src = _kb_with_constant_ref(tmp_path)
    conn = store.open_kb(db)
    try:
        d = read_source.read_source(conn, CONST_REL)
        assert "cqkd_ltyz" not in d["content"]   # 字面值确实不在正文里，全靠常量表解出
        entry = d["field_names"]["TemporaryStopCon.ENTITY"]
        assert entry["tier"] == "unique"
        assert entry["names"] == ["临时收入"]     # 不是凭英文名猜的"临停单"
        rc = entry["resolved_constant"]
        assert rc["value"] == "cqkd_ltyz"
        assert rc["defined_in"] == "cqspb/bd/common/cons/TemporaryStopCon.java"
        assert rc["line"] == 12
        assert "Boolean.TRUE" not in d["field_names"]   # 非项目常量，静默不标注（不是噪音源）
        text = read_source.render_read_source(d)
        assert "临时收入" in text and "常量引用" in text
    finally:
        conn.close()


def test_read_source_constant_ambiguous_multi_literal(tmp_path: Path):
    """同一 类.常量 在项目内被多处定义、字面值不同：标歧义，不擅自选一个。"""
    db, _src = _kb_with_constant_ref(tmp_path)
    conn = store.open_kb(db)
    try:
        conn.execute(
            "INSERT INTO java_constant VALUES(?,?,?,?,?)",
            ("TemporaryStopCon", "ENTITY", "cqkd_other", "other/TemporaryStopCon.java", 5),
        )
        conn.commit()
        entry = read_source.read_source(conn, CONST_REL)["field_names"]["TemporaryStopCon.ENTITY"]
        assert entry["tier"] == "ambiguous"
        assert entry["names"] == []
        assert "多处定义" in entry["note"]
    finally:
        conn.close()


def test_read_source_constant_value_not_in_kb_skipped(tmp_path: Path):
    """常量能解出字面值，但字面值不是 KB 已知字段/实体 key：超出标注范围，不臆造标注。"""
    db, _src = _kb_with_constant_ref(tmp_path)
    conn = store.open_kb(db)
    try:
        conn.execute("DELETE FROM java_constant")
        conn.execute(
            "INSERT INTO java_constant VALUES(?,?,?,?,?)",
            ("TemporaryStopCon", "ENTITY", "not_a_kb_key", "x.java", 1),
        )
        conn.commit()
        fn = read_source.read_source(conn, CONST_REL)["field_names"]
        assert "TemporaryStopCon.ENTITY" not in fn
    finally:
        conn.close()


# ── 常量条目截断优先级（2026-07-03 真实翻车复盘二）─────────────────────────────
# 线上真实样本：一个 394 行文件同时命中 58 个已知 key（含若干 `类.常量` 限定引用），
# 紧凑投影按预算只能装下前 25 条——而 `read_source()` 早先把常量条目**追加在字典最后**
# （先塞普通字面量 key，再塞常量），大文件里普通 key 数量一多，预算耗尽在常量条目之前，
# 结果恰恰是"字面值根本不在正文里、模型最需要工具帮忙"的常量引用被静默截断、大模型转而
# 凭常量类英文名瞎猜中文含义（真实翻车：TemporaryStopCon 猜成"临停单"，实际"临时收入"）。
# 修复：常量条目排在 field_names 字典**最前**，只要 cap 档位 ≥ 常量条目数（几乎总成立，
# 常量引用天然少）就不会被截断——即便预算紧到 ladder 兜底到最窄档 cap=1，留下的也该是常量条目。

def test_read_source_orders_constant_entries_before_plain_keys(tmp_path: Path):
    """`field_names` 字典插入顺序：常量条目在前、普通字面量 key 在后（截断安全的前提）。"""
    db, _src = _kb_with_constant_ref(tmp_path)
    conn = store.open_kb(db)
    try:
        conn.execute(
            "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("f_a", "cqkd_ltyz", "cqkd_ltyz", "aaaaaaaa", "字段A", "fa", "TextField",
             "entity", "header"),
        )
        conn.commit()
        src_dir = _src / "cqspb" / "am"
        big_src = CONST_SRC.replace(
            "boolean flag = Boolean.TRUE;",
            'boolean flag = Boolean.TRUE;\n    String v = "aaaaaaaa";',
        )
        (src_dir / "TemporaryStopTask.java").write_bytes(big_src.encode("utf-8"))
        d = read_source.read_source(conn, CONST_REL)
        keys = list(d["field_names"])
        # "aaaaaaaa" 字母序本该排最前，但常量条目必须仍排在它前面（截断优先级修复核心）。
        assert keys.index("TemporaryStopCon.ENTITY") < keys.index("aaaaaaaa")
    finally:
        conn.close()


# ── 表单标识兜底（2026-07-04 第三轮真实翻车复测）───────────────────────────────
# 真实样本：`BusinessDataServiceHelper.load("cqkd_invoic_apply", ...)` 里的字面量是**表单标识**
# （`form.key`），本 fixture 里表头实体 key 与表单 key 不同（真实常态——老版本 `_known_keys` 只查
# field/entity 两表，根本扫不到这个 token），逼大模型凭标识片段谐音瞎猜表单中文名
# （cqkd_invoic_apply → "开票申请"、cqkd_contractbill → "合同账单"，均为臆造，非元数据事实）。

FORM_ONLY_SRC = """package cqspb.am;
public class InvoiceApplyTask {
  public void execute() {
    DynamicObject bill = BusinessDataServiceHelper.loadSingle(id, "cqkd_invoic_apply");
  }
}
"""

FORM_ONLY_REL = "cqspb/am/InvoiceApplyTask.java"


def _kb_with_form_only_key(tmp_path: Path) -> tuple[Path, Path]:
    db = make_kb(tmp_path)
    src = tmp_path / "src"
    (src / "cqspb" / "am").mkdir(parents=True, exist_ok=True)
    (src / "cqspb" / "am" / "InvoiceApplyTask.java").write_bytes(FORM_ONLY_SRC.encode("utf-8"))
    conn = store.open_kb(db)
    try:
        conn.execute("INSERT OR REPLACE INTO kb_meta(key,value) VALUES('source_args', ?)",
                     (f'{{"source_root": "{src.as_posix()}"}}',))
        # 表单 key 与表头实体 key 不同（真实常态）：field/entity 两表都查不到 cqkd_invoic_apply，
        # 唯有 form 表兜底才能标出真名。
        conn.execute(
            "INSERT INTO form(key,name,form_type,model_type,isv,app_key,module,source_dym) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("cqkd_invoic_apply", "开票申请", "bill", "BillFormModel", "cqkd", "cqkd_assets",
             "cqkd_assets", "i.dym"),
        )
        conn.commit()
    finally:
        conn.close()
    return db, src


def test_read_source_annotates_form_key_literal(tmp_path: Path):
    """`.load("cqkd_invoic_apply")` 里的表单字面量：field/entity 都查不到时兜底查 form 表标注。"""
    db, _src = _kb_with_form_only_key(tmp_path)
    conn = store.open_kb(db)
    try:
        d = read_source.read_source(conn, FORM_ONLY_REL)
        entry = d["field_names"]["cqkd_invoic_apply"]
        assert entry["tier"] == "unique"
        assert entry["names"] == ["开票申请"]
        assert entry["coordinates"][0]["kind"] == "form"
        text = read_source.render_read_source(d)
        assert "开票申请" in text and "单据" in text
    finally:
        conn.close()


def test_read_source_form_key_ambiguous_multi_name(tmp_path: Path):
    """同一表单 key 在项目内对应多个不同中文名（罕见）：标歧义，不擅自选一个。"""
    db, _src = _kb_with_form_only_key(tmp_path)
    conn = store.open_kb(db)
    try:
        conn.execute(
            "INSERT INTO form(key,name,form_type,model_type,isv,app_key,module,source_dym) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("cqkd_invoic_apply", "开票申请单", "bill", "BillFormModel", "cqkd", "cqkd_assets2",
             "cqkd_assets2", "i2.dym"),
        )
        conn.commit()
        entry = read_source.read_source(conn, FORM_ONLY_REL)["field_names"]["cqkd_invoic_apply"]
        assert entry["tier"] == "ambiguous"
        assert entry["names"] == []
        assert "多个不同名" in entry["note"]
    finally:
        conn.close()


def test_read_source_field_entity_priority_over_form(tmp_path: Path):
    """key 若已能按字段/分录容器分类（如 cqkd_collateralstatus），不受表单兜底影响。"""
    db, _src = _kb_with_source(tmp_path)
    conn = store.open_kb(db)
    try:
        st = read_source.read_source(conn, REL)["field_names"]["cqkd_collateralstatus"]
        assert st["tier"] == "unique"
        assert st["coordinates"][0]["kind"] == "field"   # 不是被表单兜底改写成 "form"
    finally:
        conn.close()


def test_cli_source_json(tmp_path: Path, capsys):
    from cosmic_kb.cli.main import main

    db, _src = _kb_with_source(tmp_path)
    rc = main(["source", REL, "--db", str(db), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "抵押状态" in out and "field_names" in out
