"""底层库元数据源（dbmeta）验收测试。

三块：
    1. assemble —— 两段 fdata XML（form+entity）合成 MetaModel，用真实样例
       samples/db_xml/bd_customer_*.txt 对齐（这正是扩展单据扫不出的原厂标准单据）。
    2. 只读防线 —— assert_readonly_sql 白名单，拒绝任何写/DDL/多语句。
    3. 配置 —— DbConfig 装载、密码环境变量覆盖、read_database 回落、模板生成。

不连真库：assemble 与配置纯本地可测；连接层只测 SQL 白名单，不起 psycopg2。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from cosmic_kb import _assets
from cosmic_kb.dbmeta import assemble_model
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
