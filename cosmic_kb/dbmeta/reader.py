"""DB 元数据读取 —— 按 fnumber 从两张设计表取 fdata，合成 MetaModel。

对上层（CLI / 建库）暴露两组动作：
    fetch_fdata(fnumber)  → (form_xml, entity_xml)   单个 fnumber，两条只读 SELECT
    read_model(fnumber)   → MetaModel                 取回后交 assemble 合成
    ……_bulk 系列          → 批量版本，见下方"批量取数"

批量取数（2026-07-03 拍板新增，见 `docs/阶段验收.md` 增补条目）：build/bridge 自动摄取
一次通常有几十个候选 fnumber，若逐个 `read_model`/`read_model_via_local_ext` 循环调用，
就是"N 个候选 = N×2 次网络往返"——候选一多、DB 又不在本机，摄取会被网络延迟拖得很慢
（红线 #3：规模大，要性能）。`fetch_fdata_bulk`/`fetch_fdata_via_local_ext_bulk` 把同一
批候选各自的两张表查询各自合并成**一条** `WHERE fnumber = ANY(%s)`，无论候选多少个，
每次 `apply_vendor_metadata` 只发固定 4 条 SQL（有本地扩展的一组 2 条 + 无本地扩展的
一组 2 条），不是 O(N)。

严格只读：所有 SQL 走 connection 层的白名单校验；本模块只发 SELECT。
"""

from __future__ import annotations

from typing import Iterable

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


def _to_bytes(value: object) -> bytes:
    """fdata 可能以 bytes / memoryview / str 回来，统一成 bytes 交解析层健壮解码。"""
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, str):
        return value.encode("utf-8")
    return bytes(value)  # type: ignore[arg-type]


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

    # ── 取数（单个 fnumber）────────────────────────────────────────
    def _fetch_one_fdata(self, table: str, fnumber: str) -> bytes | None:
        """从单表按 fnumber 取 fdata；无记录返回 None。"""
        assert self._driver is not None, "请先 open()"
        cfg = self.config
        rel = _qualified(cfg.schema, table)
        sql = f"SELECT {cfg.data_column} FROM {rel} WHERE {cfg.number_column} = %s"
        rows = self._driver.query(sql, (fnumber,))
        if not rows or rows[0][0] is None:
            return None
        return _to_bytes(rows[0][0])

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

    def _fetch_master_fdata(self, table: str, local_key: str) -> tuple[bytes | None, str | None]:
        """按本地扩展**自身**的 fnumber（精确值，来自它在库里的真实行，不是猜出来的）
        在同一张表内自关联 `fmasterid = fid`，直接取原厂母体行的 fdata + 真实 fnumber。

        比"按命名规律截取候选原厂 fnumber 去直查"更可靠：本地扩展 key 若因平台标识
        长度限制被截断，`<isv>_<候选>_ext` 反推出的候选就是错的（如
        `cqkd_cas_bankjournalf_ext` 反推出 `cas_bankjournalf`，但真实原厂标识其实是
        `cas_bankjournalformrpt`）；`fmasterid` 是库内主键关系，不受命名截断影响。
        """
        assert self._driver is not None, "请先 open()"
        cfg = self.config
        rel = _qualified(cfg.schema, table)
        sql = (
            f"SELECT t2.{cfg.data_column}, t2.{cfg.number_column} "
            f"FROM {rel} t1 INNER JOIN {rel} t2 "
            f"ON t1.{cfg.master_id_column} = t2.{cfg.id_column} "
            f"WHERE t1.{cfg.number_column} = %s"
        )
        rows = self._driver.query(sql, (local_key,))
        if not rows or rows[0][0] is None:
            return None, (rows[0][1] if rows else None)
        value, master_fnumber = rows[0]
        return _to_bytes(value), master_fnumber

    def fetch_fdata_via_local_ext(self, local_key: str) -> tuple[bytes | None, bytes | None, str | None]:
        """按本地扩展自身 fnumber，两张表各自走 fmasterid 关联取原厂母体 fdata。

        返回 `(form_fdata, entity_fdata, master_fnumber)`；`master_fnumber` 取两表中
        任一命中的母体真实 fnumber（两表理应指向同一母体，取到即可，不强求两表都命中）。
        """
        form, form_fn = self._fetch_master_fdata(self.config.form_table, local_key)
        entity, entity_fn = self._fetch_master_fdata(self.config.entity_table, local_key)
        return form, entity, form_fn or entity_fn

    def read_model_via_local_ext(self, local_key: str) -> MetaModel:
        """按本地扩展自身 fnumber，通过 fmasterid 关联找到原厂母体并合成 MetaModel。

        本地行不存在、fmasterid 为空、或母体行不存在（两表都取不到）则抛错——调用方
        （`integrate.apply_vendor_metadata`）按候选 fnumber 兜底或如实提示，不臆造。
        """
        form, entity, master_fnumber = self.fetch_fdata_via_local_ext(local_key)
        if form is None and entity is None:
            raise LookupError(
                f"本地扩展 fnumber={local_key!r} 未能通过 fmasterid 关联到原厂母体"
                f"（本地行不存在、fmasterid 为空，或母体行不存在；"
                f"form={self.config.form_table} / entity={self.config.entity_table}）"
            )
        return assemble_model(
            form, entity, fnumber=master_fnumber or local_key, template_registry=self._registry
        )

    # ── 取数（批量：一批 fnumber/local_key 只发 2 条 SQL，不逐个循环）─────
    def _fetch_bulk_fdata(self, table: str, keys: list[str]) -> dict[str, bytes]:
        """从单表一次性按 `fnumber = ANY(%s)` 取回一批 fdata，key→fdata。"""
        assert self._driver is not None, "请先 open()"
        cfg = self.config
        rel = _qualified(cfg.schema, table)
        sql = f"SELECT {cfg.number_column}, {cfg.data_column} FROM {rel} WHERE {cfg.number_column} = ANY(%s)"
        rows = self._driver.query(sql, (list(keys),))
        return {fn: _to_bytes(value) for fn, value in rows if value is not None}

    def fetch_fdata_bulk(self, fnumbers: Iterable[str]) -> dict[str, tuple[bytes | None, bytes | None]]:
        """批量取 (form_fdata, entity_fdata)：每张表只发一条 `ANY(%s)` SELECT，
        不管 `fnumbers` 有多少个，固定 2 次网络往返（替代逐个 `fetch_fdata` 循环）。
        """
        keys = list(dict.fromkeys(fnumbers))  # 去重且保序，避免同 key 出现在 IN 列表里两次
        if not keys:
            return {}
        cfg = self.config
        form_map = self._fetch_bulk_fdata(cfg.form_table, keys)
        entity_map = self._fetch_bulk_fdata(cfg.entity_table, keys)
        return {fn: (form_map.get(fn), entity_map.get(fn)) for fn in keys}

    def read_models_bulk(self, fnumbers: Iterable[str]) -> dict[str, MetaModel]:
        """批量按 fnumber 取回并各自合成 MetaModel；两表都没有的 fnumber 不出现在返回值里
        （调用方按 key 是否存在判断"查到/查不到"，语义等价于逐个 `read_model` 抛
        `LookupError`，但只发 2 条 SQL）。"""
        data = self.fetch_fdata_bulk(fnumbers)
        out: dict[str, MetaModel] = {}
        for fn, (form, entity) in data.items():
            if form is None and entity is None:
                continue
            out[fn] = assemble_model(form, entity, fnumber=fn, template_registry=self._registry)
        return out

    def _fetch_master_fdata_bulk(self, table: str, local_keys: list[str]) -> dict[str, tuple[bytes, str]]:
        """单表一次性按本地 key 批量走 `fmasterid = fid` 自关联，取回一批母体 fdata。"""
        assert self._driver is not None, "请先 open()"
        cfg = self.config
        rel = _qualified(cfg.schema, table)
        sql = (
            f"SELECT t1.{cfg.number_column}, t2.{cfg.data_column}, t2.{cfg.number_column} "
            f"FROM {rel} t1 INNER JOIN {rel} t2 "
            f"ON t1.{cfg.master_id_column} = t2.{cfg.id_column} "
            f"WHERE t1.{cfg.number_column} = ANY(%s)"
        )
        rows = self._driver.query(sql, (list(local_keys),))
        return {
            local_key: (_to_bytes(value), master_fnumber)
            for local_key, value, master_fnumber in rows
            if value is not None
        }

    def fetch_fdata_via_local_ext_bulk(
        self, local_keys: Iterable[str]
    ) -> dict[str, tuple[bytes | None, bytes | None, str | None]]:
        """批量版 `fetch_fdata_via_local_ext`：每张表一条 `ANY(%s)` 自关联 SELECT，
        固定 2 次网络往返，不随本地扩展 key 数量线性增长。
        """
        keys = list(dict.fromkeys(local_keys))
        if not keys:
            return {}
        form_map = self._fetch_master_fdata_bulk(self.config.form_table, keys)
        entity_map = self._fetch_master_fdata_bulk(self.config.entity_table, keys)
        out: dict[str, tuple[bytes | None, bytes | None, str | None]] = {}
        for lk in keys:
            form_hit = form_map.get(lk)
            entity_hit = entity_map.get(lk)
            form_fdata, form_fn = form_hit if form_hit else (None, None)
            entity_fdata, entity_fn = entity_hit if entity_hit else (None, None)
            out[lk] = (form_fdata, entity_fdata, form_fn or entity_fn)
        return out

    def read_models_via_local_ext_bulk(self, local_keys: Iterable[str]) -> dict[str, MetaModel]:
        """批量版 `read_model_via_local_ext`：本地 key→合成后的原厂母体 MetaModel；
        两表都没查到母体的本地 key 不出现在返回值里（同 `read_models_bulk` 的"存在即
        查到"语义），只发 2 条 SQL。"""
        data = self.fetch_fdata_via_local_ext_bulk(local_keys)
        out: dict[str, MetaModel] = {}
        for lk, (form, entity, master_fnumber) in data.items():
            if form is None and entity is None:
                continue
            out[lk] = assemble_model(
                form, entity, fnumber=master_fnumber or lk, template_registry=self._registry
            )
        return out

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
