"""DB 元数据读取 —— 按 fnumber 从两张设计表取 fdata，合成 MetaModel。

对上层（CLI / 建库）只暴露两个动作：
    fetch_fdata(fnumber)  → (form_xml, entity_xml)   两条只读 SELECT
    read_model(fnumber)   → MetaModel                 取回后交 assemble 合成

严格只读：所有 SQL 走 connection 层的白名单校验；本模块只发 SELECT。
"""

from __future__ import annotations

from ..metadata.model import MetaModel
from ..metadata.template_loader import TemplateRegistry
from .assemble import assemble_model
from .config import DbConfig
from .connection import MetaDbDriver, get_driver


def _qualified(schema: str, table: str) -> str:
    """schema 限定表名（简单标识符，仅字母数字下划线，防注入）。"""
    for part in (schema, table):
        if not part.replace("_", "").isalnum():
            raise ValueError(f"非法的 schema/表名：{part!r}")
    return f"{schema}.{table}"


class DbMetaReader:
    """底层库元数据读取器：持有只读驱动，按 fnumber 取一份完整元数据。"""

    def __init__(self, config: DbConfig, *, template_registry: TemplateRegistry | None = None) -> None:
        self.config = config
        self._driver: MetaDbDriver | None = None
        self._registry = template_registry or TemplateRegistry()

    # ── 连接生命周期 ────────────────────────────────────────────
    def open(self) -> None:
        self._driver = get_driver(self.config)
        self._driver.connect()

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def __enter__(self) -> DbMetaReader:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── 取数 ────────────────────────────────────────────────────
    def _fetch_one_fdata(self, table: str, fnumber: str) -> bytes | None:
        """从单表按 fnumber 取 fdata；无记录返回 None。"""
        assert self._driver is not None, "请先 open()"
        cfg = self.config
        rel = _qualified(cfg.schema, table)
        sql = f"SELECT {cfg.data_column} FROM {rel} WHERE {cfg.number_column} = %s"
        rows = self._driver.query(sql, (fnumber,))
        if not rows:
            return None
        value = rows[0][0]
        if value is None:
            return None
        # fdata 可能以 bytes / memoryview / str 回来，统一成 bytes 交解析层健壮解码。
        if isinstance(value, memoryview):
            return value.tobytes()
        if isinstance(value, str):
            return value.encode("utf-8")
        return bytes(value)

    def fetch_fdata(self, fnumber: str) -> tuple[bytes | None, bytes | None]:
        """取回 (form_fdata, entity_fdata)。两张表各一条只读 SELECT。"""
        form = self._fetch_one_fdata(self.config.form_table, fnumber)
        entity = self._fetch_one_fdata(self.config.entity_table, fnumber)
        return form, entity

    def read_model(self, fnumber: str) -> MetaModel:
        """按 fnumber 取回两表 fdata 并合成 MetaModel。两表皆无记录则抛错。"""
        form, entity = self.fetch_fdata(fnumber)
        if form is None and entity is None:
            raise LookupError(
                f"底层库两张设计表都没有 fnumber={fnumber!r} 的记录"
                f"（form={self.config.form_table} / entity={self.config.entity_table}）"
            )
        return assemble_model(
            form, entity, fnumber=fnumber, template_registry=self._registry
        )

    def ping(self) -> dict:
        """连接自检：确认只读会话可用，回报两张表能否 SELECT（不写任何数据）。"""
        assert self._driver is not None, "请先 open()"
        cfg = self.config
        out: dict = {"read_database": cfg.read_database, "schema": cfg.schema, "tables": {}}
        for table in (cfg.form_table, cfg.entity_table):
            rel = _qualified(cfg.schema, table)
            try:
                rows = self._driver.query(f"SELECT count(*) FROM {rel}")
                out["tables"][table] = {"ok": True, "count": rows[0][0]}
            except Exception as e:  # 表不存在/无权限都如实回报，不臆断
                out["tables"][table] = {"ok": False, "error": str(e)}
        return out
