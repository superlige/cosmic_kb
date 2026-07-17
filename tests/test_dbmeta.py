"""底层库元数据源（dbmeta）验收测试。

四块：
    1. assemble —— 两段 fdata XML（form+entity）合成 MetaModel，用真实样例
       samples/db_xml/bd_customer_*.txt 对齐（这正是扩展单据扫不出的原厂标准单据）。
    2. 只读防线 —— assert_readonly_sql 白名单，拒绝任何写/DDL/多语句。
    3. 配置 —— DbConfig 装载、密码环境变量覆盖、read_database 回落、模板生成。
    4. reader —— 按本地扩展自身 fnumber 走 fmasterid 关联回溯原厂母体
       （`read_model_via_local_ext`，2026-07-03 修复候选 fnumber 因平台标识长度限制
       被截断导致查不到原厂元数据的问题），用假驱动验证 SQL 形状与参数，不连真库。

不连真库：assemble 与配置纯本地可测；连接层只测 SQL 白名单，不起 psycopg2；
reader 用假驱动（monkeypatch `get_driver`）验证 SQL 拼装，同样不连真库。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from cosmic_kb import _assets
from cosmic_kb.dbmeta import assemble_convert_rule, assemble_model
from cosmic_kb.dbmeta.assemble import _infer_model_type, build_deploy_root
from cosmic_kb.dbmeta.config import DbConfig, from_dict, load_config, sample_config_text
from cosmic_kb.dbmeta.connection import assert_readonly_sql, get_driver

DB_XML = _assets.PROJECT_ROOT / "samples" / "db_xml"
FORM_FDATA = DB_XML / "bd_customer_form.txt"
ENTITY_FDATA = DB_XML / "bd_customer_entity.txt"

needs_sample = pytest.mark.skipif(
    not (FORM_FDATA.exists() and ENTITY_FDATA.exists()), reason="缺 db_xml 样例"
)


# ── 1. assemble：两表 fdata → MetaModel ─────────────────────────────
@needs_sample
def test_assemble_real_sample_bd_customer():
    """真实样例：form+entity 合成后应拿到 bd_customer 完整元数据。"""
    model = assemble_model(
        FORM_FDATA.read_bytes(), ENTITY_FDATA.read_bytes(), fnumber="bd_customer"
    )
    assert model.key == "bd_customer"
    assert model.name == "客户"                 # 中文名健壮解码正确
    assert model.form_type == "basedata"        # 主实体 BaseEntity → basedata
    assert model.model_type == "BaseFormModel"  # 反推的 ModelType
    # 原厂标准字段（扩展 dym 里完全缺席的）现在拿到了，且带落库列。
    db_fields = [f for f in model.fields if f.db_column]
    assert len(db_fields) >= 30
    keys = {f.key for f in model.fields}
    assert "simplename" in keys                 # 简称，落库标准字段
    # 原厂项目插件解析到（运行时挂在 bd_customer 上）。
    proj = {p.class_name for p in model.plugins if p.source == "project"}
    assert "kd.bd.master.CustomerFormPlugin" in proj
    assert len(model.operations) > 0
    # DB fdata 只写自定义/显式覆盖操作；完全沿用根模板的预制操作也要补齐。
    assert {"submit", "audit", "refresh"} <= {o.key for o in model.operations}
    assert model.source_file == "db://bd_customer"


@needs_sample
def test_assemble_entity_only_still_parses():
    """只给 entity（缺 form 记录）：仍能解析数据模型，不崩。"""
    model = assemble_model(None, ENTITY_FDATA.read_bytes(), fnumber="bd_customer")
    assert model.form_type == "basedata"
    assert len(model.fields) > 0
    # 缺 form → 无界面插件，但操作/字段在。
    assert len(model.operations) > 0


@needs_sample
def test_assemble_form_only_no_entity():
    """只给 form（缺 entity）：拿到 UI 插件，字段为空，model_type 无从反推。"""
    model = assemble_model(FORM_FDATA.read_bytes(), None, fnumber="bd_customer")
    assert model.key == "bd_customer"
    assert model.fields == []
    assert len(model.plugins) > 0               # 界面插件仍在


def test_assemble_both_empty_raises():
    with pytest.raises(ValueError):
        assemble_model(None, None, fnumber="x")
    with pytest.raises(ValueError):
        assemble_model(b"  ", b"", fnumber="x")


def test_infer_model_type_by_main_entity_tag():
    """ModelType 反推：按 entity 主实体标签，不靠命名惯例。"""
    def _entity(tag: str) -> ET.Element:
        return ET.fromstring(f"<EntityMetadata><Items><{tag}><Id>1</Id></{tag}></Items></EntityMetadata>")

    assert _infer_model_type(_entity("BillEntity")) == "BillFormModel"
    assert _infer_model_type(_entity("BaseEntity")) == "BaseFormModel"
    assert _infer_model_type(_entity("MainEntity")) == "DynamicFormModel"
    assert _infer_model_type(None) is None
    # 无已知主实体标签 → None（form_type 记 unknown，不臆造）。
    assert _infer_model_type(_entity("EntryEntity")) is None


def test_build_deploy_root_shape():
    """合成骨架应是 parse_element 认得的 DeployMetadata/DesignMetas/DataXml 结构。"""
    form = ET.fromstring("<FormMetadata><Key>k</Key></FormMetadata>")
    entity = ET.fromstring("<EntityMetadata><Items/></EntityMetadata>")
    root = build_deploy_root(form, entity, "BaseFormModel")
    assert root.tag == "DeployMetadata"
    assert root.find(".//DesignFormMeta/ModelType").text == "BaseFormModel"
    assert root.find(".//DesignFormMeta/DataXml/FormMetadata") is not None
    assert root.find(".//DesignEntityMeta/DataXml/EntityMetadata") is not None


# ── 2. 只读防线：SQL 白名单 ─────────────────────────────────────────
@pytest.mark.parametrize("sql", [
    "SELECT fdata FROM t_meta_formdesign WHERE fnumber=%s",
    "select count(*) from t_meta_entitydesign",
    "  WITH x AS (SELECT 1) SELECT * FROM x  ",
])
def test_readonly_sql_allows_select(sql):
    assert_readonly_sql(sql)  # 不抛即通过


@pytest.mark.parametrize("sql", [
    "DELETE FROM t_meta_formdesign",
    "UPDATE t_meta_formdesign SET fdata='x'",
    "INSERT INTO t VALUES (1)",
    "DROP TABLE t_meta_formdesign",
    "TRUNCATE t_meta_formdesign",
    "ALTER TABLE t ADD c int",
    "SET default_transaction_read_only = off",
    "SELECT 1; DELETE FROM t",            # 多语句夹带写
    "SELECT * FROM t WHERE x=(DELETE FROM y)",  # 写关键字混入
    "",
])
def test_readonly_sql_rejects_writes(sql):
    with pytest.raises(ValueError):
        assert_readonly_sql(sql)


def test_get_driver_unknown_type():
    with pytest.raises(ValueError):
        get_driver(DbConfig(driver="mysql"))  # 尚未实现，明确拒绝而非静默


def test_get_driver_postgres_aliases():
    from cosmic_kb.dbmeta.connection import PostgresDriver
    for name in ("postgresql", "postgres", "PG", "PostgreSQL"):
        assert isinstance(get_driver(DbConfig(driver=name)), PostgresDriver)


# ── 3. 配置 ─────────────────────────────────────────────────────────
def test_config_from_dict_ignores_unknown_keys():
    cfg = from_dict({"host": "10.0.0.1", "port": 6432, "unknown_key": "x"})
    assert cfg.host == "10.0.0.1"
    assert cfg.port == 6432
    assert not hasattr(cfg, "unknown_key")


def test_config_read_database_fallback():
    assert DbConfig(database="main", table_database="").read_database == "main"
    assert DbConfig(database="main", table_database="metadb").read_database == "metadb"


def test_config_password_env_override(monkeypatch):
    monkeypatch.setenv("COSMIC_DB_PASSWORD", "from-env")
    cfg = from_dict({"password": "in-file"})
    assert cfg.password == "from-env"           # 环境变量优先，防明文口令进库


def test_config_redact():
    cfg = DbConfig(password="secret")
    assert cfg.to_dict()["password"] == "***"
    assert cfg.to_dict(redact=False)["password"] == "secret"


def test_load_config_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("COSMIC_DB_PASSWORD", raising=False)
    p = tmp_path / "cosmic_db.json"
    p.write_text(json.dumps({"host": "h", "port": 15432, "user": "ro"}), encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.host == "h" and cfg.port == 15432 and cfg.user == "ro"


def test_load_config_missing_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_config(None)


def test_sample_config_text_is_valid_json_body():
    text = sample_config_text()
    body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("//"))
    data = json.loads(body)
    assert data["driver"] == "postgresql"
    assert data["password"] == ""               # 模板不写明文口令


def test_init_config_roundtrip_loads(tmp_path, monkeypatch):
    """--init-config 生成的模板必须能被 load_config 直接读回（注释行不该崩）。"""
    monkeypatch.delenv("COSMIC_DB_PASSWORD", raising=False)
    p = tmp_path / "cosmic_db.json"
    p.write_text(sample_config_text(), encoding="utf-8")  # 含 // 注释头
    cfg = load_config(str(p))                              # 不该抛 JSONDecodeError
    assert cfg.driver == "postgresql"
    assert cfg.read_database == "postgres"


def test_load_config_empty_file_raises_valueerror(tmp_path):
    """空配置文件给友好 ValueError，而非裸 JSONDecodeError 堆栈。"""
    p = tmp_path / "cosmic_db.json"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(p))


def test_load_config_malformed_json_raises_valueerror(tmp_path):
    p = tmp_path / "cosmic_db.json"
    p.write_text("{ not valid json ", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(p))


# ── 4. reader：本地扩展自身 fnumber → fmasterid 关联回溯原厂母体 ─────────────
class _FakeDriver:
    """假驱动：记录收到的 (sql, params)，按调用顺序弹出预置结果行，不连真库。"""

    def __init__(self, responses: list[list[tuple]]):
        self._responses = list(responses)
        self.calls: list[tuple[str, tuple]] = []

    def connect(self) -> None:
        pass

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        self.calls.append((sql, params))
        return self._responses.pop(0) if self._responses else []

    def close(self) -> None:
        pass


def _fake_open_reader(monkeypatch, responses: list[list[tuple]]):
    """monkeypatch reader.get_driver，返回预置结果的假驱动；回传 (reader, fake_driver)。"""
    from cosmic_kb.dbmeta import reader as reader_mod

    fake = _FakeDriver(responses)
    monkeypatch.setattr(reader_mod, "get_driver", lambda config: fake)
    r = reader_mod.DbMetaReader(DbConfig())
    r.open()
    return r, fake


def test_reader_read_model_via_local_ext_builds_fmasterid_self_join(monkeypatch):
    """真实故障复现：本地扩展 key 'cqkd_cas_bankjournalf_ext'——SQL 必须用它自己的精确
    值做 WHERE（不猜候选原厂 fnumber），且是 fmasterid=fid 的自关联，取回母体真实 fnumber。"""
    form_xml = b"<FormMetadata><Key>cas_bankjournalformrpt</Key><Name>Bank Journal</Name></FormMetadata>"
    entity_xml = (
        b"<EntityMetadata><Items><BillEntity><Id>1</Id></BillEntity></Items></EntityMetadata>"
    )
    responses = [
        [(form_xml, "cas_bankjournalformrpt")],     # form 表自关联查询结果
        [(entity_xml, "cas_bankjournalformrpt")],   # entity 表自关联查询结果
    ]
    r, fake = _fake_open_reader(monkeypatch, responses)
    try:
        model = r.read_model_via_local_ext("cqkd_cas_bankjournalf_ext")
    finally:
        r.close()

    assert model.key == "cas_bankjournalformrpt"    # 母体真实标识，非猜出来的候选
    assert len(fake.calls) == 2
    for sql, params in fake.calls:
        assert params == ("cqkd_cas_bankjournalf_ext",)  # WHERE 用扩展自身精确 key
        assert "fmasterid" in sql and "fid" in sql
        assert "INNER JOIN" in sql
        assert sql.strip().upper().startswith("SELECT")


def test_reader_read_model_via_local_ext_not_found_raises_lookuperror(monkeypatch):
    """本地行不存在 / fmasterid 未指向有效母体（两张表都查不到）→ 抛错，不臆造。"""
    r, _fake = _fake_open_reader(monkeypatch, [[], []])
    try:
        with pytest.raises(LookupError):
            r.read_model_via_local_ext("cqkd_unknown_ext")
    finally:
        r.close()


def test_reader_fetch_fdata_via_local_ext_prefers_form_master_fnumber(monkeypatch):
    """两表都命中时，master_fnumber 取 form 侧优先（form_fn or entity_fn 短路求值）。"""
    responses = [
        [(b"<x/>", "master_from_form")],
        [(b"<y/>", "master_from_entity")],
    ]
    r, _fake = _fake_open_reader(monkeypatch, responses)
    try:
        _form, _entity, master_fnumber = r.fetch_fdata_via_local_ext("cqkd_x_ext")
    finally:
        r.close()
    assert master_fnumber == "master_from_form"


# ── 5. reader：批量取数（一批 key 固定 2 条 SQL，不逐个循环查库）──────────────
# 背景：用户反馈真实项目自动摄取（几十个候选）"执行速度很慢，是不是循环查库了"——
# 排查确认此前 apply_vendor_metadata 确实是逐个候选调 read_model/read_model_via_local_ext，
# 一个候选就是一次网络往返；批量方法把同一批候选各自的两张表查询合并成一条
# `WHERE fnumber = ANY(%s)`，不管候选多少个都固定 2 次往返。

def test_reader_fetch_fdata_bulk_issues_two_any_queries_not_one_per_key(monkeypatch):
    """3 个 fnumber 一起查：应该只发 2 条 SQL（form 表一条、entity 表一条），不是 6 条。"""
    responses = [
        [("bd_customer", b"<f1/>"), ("bd_supplier", b"<f2/>")],   # form 表批量结果（bd_taxrate 缺记录）
        [("bd_customer", b"<e1/>"), ("bd_taxrate", b"<e3/>")],    # entity 表批量结果（bd_supplier 缺记录）
    ]
    r, fake = _fake_open_reader(monkeypatch, responses)
    try:
        data = r.fetch_fdata_bulk(["bd_customer", "bd_supplier", "bd_taxrate"])
    finally:
        r.close()

    assert len(fake.calls) == 2   # 不是 3 个 key × 2 张表 = 6 条
    for sql, params in fake.calls:
        assert "= ANY(" in sql
        assert sql.strip().upper().startswith("SELECT")
        assert set(params[0]) == {"bd_customer", "bd_supplier", "bd_taxrate"}

    assert data["bd_customer"] == (b"<f1/>", b"<e1/>")
    assert data["bd_supplier"] == (b"<f2/>", None)     # entity 表缺记录 → None，不报错
    assert data["bd_taxrate"] == (None, b"<e3/>")       # form 表缺记录 → None


def test_reader_read_models_bulk_skips_keys_with_no_record_in_either_table(monkeypatch):
    """两表都没有的 fnumber 不出现在返回值里（调用方按 key 是否存在判断"查到/查不到"）。"""
    form_xml = b"<FormMetadata><Key>bd_customer</Key></FormMetadata>"
    responses = [
        [("bd_customer", form_xml)],   # form 表只有 bd_customer
        [],                              # entity 表两个都没有
    ]
    r, _fake = _fake_open_reader(monkeypatch, responses)
    try:
        models = r.read_models_bulk(["bd_customer", "bd_ghost"])
    finally:
        r.close()

    assert set(models) == {"bd_customer"}
    assert models["bd_customer"].key == "bd_customer"


def test_reader_fetch_fdata_via_local_ext_bulk_issues_two_any_queries(monkeypatch):
    """N 个本地扩展 key 一起走 fmasterid 关联批量查：固定 2 条 SQL，不随 N 增长。"""
    responses = [
        [("cqkd_a_ext", b"<fa/>", "vendor_a"), ("cqkd_b_ext", b"<fb/>", "vendor_b")],
        [("cqkd_a_ext", b"<ea/>", "vendor_a")],   # entity 侧只有 a 命中
    ]
    r, fake = _fake_open_reader(monkeypatch, responses)
    try:
        data = r.fetch_fdata_via_local_ext_bulk(["cqkd_a_ext", "cqkd_b_ext"])
    finally:
        r.close()

    assert len(fake.calls) == 2
    for sql, params in fake.calls:
        assert "fmasterid" in sql and "fid" in sql and "= ANY(" in sql
        assert set(params[0]) == {"cqkd_a_ext", "cqkd_b_ext"}

    assert data["cqkd_a_ext"] == (b"<fa/>", b"<ea/>", "vendor_a")
    assert data["cqkd_b_ext"] == (b"<fb/>", None, "vendor_b")


def test_reader_read_models_via_local_ext_bulk_keys_by_local_key(monkeypatch):
    """批量合成结果按本地扩展 key 索引；两表都没关联到母体的 key 不出现在结果里。"""
    form_xml = b"<FormMetadata><Key>cas_bankjournalformrpt</Key></FormMetadata>"
    responses = [
        [("cqkd_cas_bankjournalf_ext", form_xml, "cas_bankjournalformrpt")],
        [],
    ]
    r, _fake = _fake_open_reader(monkeypatch, responses)
    try:
        models = r.read_models_via_local_ext_bulk(["cqkd_cas_bankjournalf_ext", "cqkd_ghost_ext"])
    finally:
        r.close()

    assert set(models) == {"cqkd_cas_bankjournalf_ext"}
    assert models["cqkd_cas_bankjournalf_ext"].key == "cas_bankjournalformrpt"


def test_reader_bulk_methods_empty_input_short_circuits_no_query(monkeypatch):
    """空输入直接返回空字典，不发一条 SQL（`apply_vendor_metadata` 两组分组后可能有一组为空）。"""
    r, fake = _fake_open_reader(monkeypatch, [])
    try:
        assert r.fetch_fdata_bulk([]) == {}
        assert r.read_models_bulk([]) == {}
        assert r.fetch_fdata_via_local_ext_bulk([]) == {}
        assert r.read_models_via_local_ext_bulk([]) == {}
    finally:
        r.close()
    assert fake.calls == []


# ── 6. 转换规则：assemble_convert_rule（t_botp_convertrule 单表 → MetaModel）──────────
_CONVERT_RULE_FDATA = """<?xml version="1.0" encoding="UTF-8"?>
<ConvertRuleMetadata>
  <RuleElement>
    <ConvertRuleElement>
      <Name>收款单红冲</Name>
      <LinkEntityPolicy>
        <LinkEntityPolicy>
          <TargetEntryKey>cqkd_skdb</TargetEntryKey>
          <SourceEntryKey>cqkd_skdb</SourceEntryKey>
        </LinkEntityPolicy>
      </LinkEntityPolicy>
      <FieldMapPolicy>
        <FieldMapPolicy>
          <FieldMaps>
            <FieldMapItem>
              <ConvertType>SourceField</ConvertType>
              <TargetFieldKey>billno</TargetFieldKey>
              <SourceFieldKey/>
              <SumType>First</SumType>
            </FieldMapItem>
          </FieldMaps>
        </FieldMapPolicy>
      </FieldMapPolicy>
    </ConvertRuleElement>
  </RuleElement>
</ConvertRuleMetadata>"""


def test_assemble_convert_rule_builds_expected_skeleton():
    """关系本体（Id/Isv/Enabled/SourceEntityNumber/TargetEntityNumber）来自 DB 关系列，
    不是 fdata 正文——这几项必须写成 DesignConvertRuleMeta 直接子节点才能被
    _parse_convert_rule 读到（见 dym_parser.py:343-355 的 _wrap_text 优先级）。"""
    model = assemble_convert_rule(
        _CONVERT_RULE_FDATA,
        fid="2007204479732048896",
        isv="cqkd",
        enabled=True,
        source_entity="cqkd_skdb",
        target_entity="cqkd_skdb",
    )
    assert model.key == "2007204479732048896"
    assert model.form_type == "convert"
    assert model.isv == "cqkd"
    assert model.name == "收款单红冲"
    assert model.convert.source_entity == "cqkd_skdb"
    assert model.convert.target_entity == "cqkd_skdb"
    assert model.convert.source_entry == "cqkd_skdb"
    assert model.convert.target_entry == "cqkd_skdb"
    assert model.convert.field_map_count == 1
    assert model.convert.enabled is True
    assert model.source_file == "db://convertrule/2007204479732048896"


def test_assemble_convert_rule_enabled_false_roundtrips():
    """`enabled=False` 不能被当成"未给"漏掉——必须写 "false" 字面量而非省略节点。"""
    model = assemble_convert_rule(_CONVERT_RULE_FDATA, fid="x", enabled=False)
    assert model.convert.enabled is False


def test_assemble_convert_rule_empty_fdata_raises():
    with pytest.raises(ValueError):
        assemble_convert_rule("", fid="x")
    with pytest.raises(ValueError):
        assemble_convert_rule("   ", fid="x")


# ── 7. reader：增量二开元数据同步取数（isv/fmodifydate/转换规则/服务端时间）────────────
def test_reader_list_isv_form_counts_groups(monkeypatch):
    responses = [[("kingdee", 500), ("cqkd", 340)]]
    r, fake = _fake_open_reader(monkeypatch, responses)
    try:
        counts = r.list_isv_form_counts()
    finally:
        r.close()
    assert counts == {"kingdee": 500, "cqkd": 340}
    sql, params = fake.calls[0]
    assert "fisv" in sql and "GROUP BY" in sql
    assert "t_meta_formdesign" in sql
    assert params == ()


def test_reader_list_changed_keys_omits_time_filter_when_since_ts_none(monkeypatch):
    r, fake = _fake_open_reader(monkeypatch, [[("cqkd_a",), ("cqkd_b",)]])
    try:
        keys = r.list_changed_keys("t_meta_formdesign", "cqkd", None)
    finally:
        r.close()
    assert keys == ["cqkd_a", "cqkd_b"]
    sql, params = fake.calls[0]
    assert "fmodifydate" not in sql
    assert params == ("cqkd",)


def test_reader_list_changed_keys_includes_modify_time_filter_when_since_ts_given(monkeypatch):
    r, fake = _fake_open_reader(monkeypatch, [[("cqkd_a",)]])
    try:
        keys = r.list_changed_keys("t_meta_formdesign", "cqkd", "2026-07-01T00:00:00")
    finally:
        r.close()
    assert keys == ["cqkd_a"]
    sql, params = fake.calls[0]
    assert "fmodifydate" in sql and ">" in sql
    assert params == ("cqkd", "2026-07-01T00:00:00")


def test_reader_list_changed_form_and_entity_keys_unions_dedups_preserves_order(monkeypatch):
    responses = [
        [("cqkd_a",), ("cqkd_b",)],   # form_table
        [("cqkd_b",), ("cqkd_c",)],   # entity_table，cqkd_b 重复
    ]
    r, fake = _fake_open_reader(monkeypatch, responses)
    try:
        keys = r.list_changed_form_and_entity_keys("cqkd", None)
    finally:
        r.close()
    assert keys == ["cqkd_a", "cqkd_b", "cqkd_c"]   # 去重且保序
    assert len(fake.calls) == 2


def test_reader_list_changed_convert_rule_ids_uses_fid_not_fnumber(monkeypatch):
    r, fake = _fake_open_reader(monkeypatch, [[("2007204479732048896",)]])
    try:
        ids = r.list_changed_convert_rule_ids("cqkd", None)
    finally:
        r.close()
    assert ids == ["2007204479732048896"]
    sql, _params = fake.calls[0]
    assert "t_botp_convertrule" in sql
    assert "SELECT fid " in sql or sql.strip().startswith("SELECT fid")


def test_reader_read_convert_rules_bulk_assembles_via_assemble_convert_rule(monkeypatch):
    row = (
        "2007204479732048896", _CONVERT_RULE_FDATA.encode("utf-8"),
        "cqkd", True, "cqkd_skdb", "cqkd_skdb",
    )
    r, fake = _fake_open_reader(monkeypatch, [[row]])
    try:
        models = r.read_convert_rules_bulk(["2007204479732048896"])
    finally:
        r.close()
    assert set(models) == {"2007204479732048896"}
    m = models["2007204479732048896"]
    assert m.form_type == "convert"
    assert m.convert.source_entity == "cqkd_skdb"
    sql, params = fake.calls[0]
    assert "= ANY(" in sql
    assert set(params[0]) == {"2007204479732048896"}


def test_reader_read_convert_rules_bulk_empty_input_short_circuits_no_query(monkeypatch):
    r, fake = _fake_open_reader(monkeypatch, [])
    try:
        assert r.read_convert_rules_bulk([]) == {}
        assert r.fetch_convert_rule_fdata_bulk([]) == {}
    finally:
        r.close()
    assert fake.calls == []


def test_reader_server_now_iso_returns_string_as_is(monkeypatch):
    r, _fake = _fake_open_reader(monkeypatch, [[("2026-07-05T10:00:00",)]])
    try:
        assert r.server_now_iso() == "2026-07-05T10:00:00"
    finally:
        r.close()


def test_reader_server_now_iso_normalizes_datetime(monkeypatch):
    import datetime

    dt = datetime.datetime(2026, 7, 5, 10, 0, 0)
    r, _fake = _fake_open_reader(monkeypatch, [[(dt,)]])
    try:
        assert r.server_now_iso() == dt.isoformat()
    finally:
        r.close()
