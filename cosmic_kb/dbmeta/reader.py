"""DB 元数据读取 —— 按 fnumber 从两张设计表取 fdata，合成 MetaModel。

对上层（CLI / 建库）暴露两组动作：
    fetch_fdata(fnumber)  → (form_xml, entity_xml)   单个 fnumber，两条只读 SELECT
    read_model(fnumber)   → MetaModel                 取回后交 assemble 合成
    ……_bulk 系列          → 批量版本，见下方"批量取数"

批量取数（2026-07-03 拍板新增，见 `docs/核心/阶段验收.md` 增补条目）：build/bridge 自动摄取
一次通常有几十个候选 fnumber，若逐个 `read_model`/`read_model_via_local_ext` 循环调用，
就是"N 个候选 = N×2 次网络往返"——候选一多、DB 又不在本机，摄取会被网络延迟拖得很慢
（红线 #3：规模大，要性能）。`fetch_fdata_bulk`/`fetch_fdata_via_local_ext_bulk` 把同一
批候选各自的两张表查询各自合并成一条成员判定查询（PostgreSQL `= ANY(?)` 单参数、
无长度限制；Oracle `IN (...)` 有 1000 上限，按方言的 `in_chunk_size` 自动切块），
候选量小时每次 `apply_vendor_metadata` 只发固定 4 条 SQL，不是 O(N)。

方言无关：本模块拼 SQL 一律用中性 `?` 占位符 + `SqlDialect`（占位符/成员判定/空串/
取时间的库差异都收敛在方言里），由 `_query` 统一 finalize 后执行，reader 不感知具体库。

严格只读：所有 SQL 走 connection 层的白名单校验；本模块只发 SELECT。
"""

from __future__ import annotations

from typing import Iterable

from ..metadata.model import MetaModel
from ..metadata.template_loader import TemplateRegistry
from .assemble import assemble_convert_rule, assemble_model
from .config import DbConfig
from .connection import MetaDbDriver, SqlDialect, get_dialect, get_driver


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


def _to_optional_bool(value: object) -> bool | None:
    """把 DB 回来的 fenabled 归一成 bool|None：可能是原生 bool、0/1、或
    "true"/"false"/"1"/"0" 字符串，驱动不同实现回来的 Python 类型不保证一致。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "t", "yes")
    return bool(value)


class DbMetaReader:
    """底层库元数据读取器：持有只读驱动，按 fnumber 取一份完整元数据。"""

    def __init__(self, config: DbConfig, *, template_registry: TemplateRegistry | None = None) -> None:
        self.config = config
        self._driver: MetaDbDriver | None = None
        # 方言按 config.driver 取，独立于活动连接——拼 SQL 只依赖它，假驱动测试也拿得到。
        self._dialect: SqlDialect = get_dialect(config)
        self._registry = template_registry or TemplateRegistry()

    # ── 连接生命周期 ────────────────────────────────────────────
    def open(self) -> None:
        self._driver = get_driver(self.config)
        self._driver.connect()

    # ── SQL 执行 / 分批 helper（方言差异统一收口于此）────────────────────
    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        """把中性 `?` 占位符 SQL 交方言 finalize 后执行。所有取数都走这里，
        reader 其余代码只写库无关的 `?` 占位符。"""
        assert self._driver is not None, "请先 open()"
        return self._driver.query(self._dialect.finalize(sql), params)

    def _chunks(self, keys: list[str]) -> Iterable[list[str]]:
        """按方言的 IN/成员判定上限把候选切块（PG 上限极大 → 永远一块；Oracle 1000 一块）。"""
        size = self._dialect.in_chunk_size
        for i in range(0, len(keys), size):
            yield keys[i:i + size]

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
        cfg = self.config
        rel = _qualified(cfg.schema, table)
        sql = f"SELECT {cfg.data_column} FROM {rel} WHERE {cfg.number_column} = ?"
        rows = self._query(sql, (fnumber,))
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
        cfg = self.config
        rel = _qualified(cfg.schema, table)
        sql = (
            f"SELECT t2.{cfg.data_column}, t2.{cfg.number_column} "
            f"FROM {rel} t1 INNER JOIN {rel} t2 "
            f"ON t1.{cfg.master_id_column} = t2.{cfg.id_column} "
            f"WHERE t1.{cfg.number_column} = ?"
        )
        rows = self._query(sql, (local_key,))
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
        """从单表按成员判定一次性取回一批 fdata，key→fdata。

        PG 一条 `= ANY(?)` 全查；Oracle 按 1000 一批 `IN (...)` 切块，无论多少个 key
        对 PG 仍是 1 条 SQL、对 Oracle 是 ⌈N/1000⌉ 条（成员判定方言差异见 SqlDialect）。
        """
        cfg = self.config
        rel = _qualified(cfg.schema, table)
        out: dict[str, bytes] = {}
        for chunk in self._chunks(keys):
            frag, params = self._dialect.membership(cfg.number_column, chunk)
            sql = f"SELECT {cfg.number_column}, {cfg.data_column} FROM {rel} WHERE {frag}"
            rows = self._query(sql, params)
            out.update({fn: _to_bytes(value) for fn, value in rows if value is not None})
        return out

    def fetch_fdata_bulk(self, fnumbers: Iterable[str]) -> dict[str, tuple[bytes | None, bytes | None]]:
        """批量取 (form_fdata, entity_fdata)：每张表一条成员判定 SELECT（PG 无长度限制→
        固定 2 次网络往返；Oracle 按 1000 上限切块），替代逐个 `fetch_fdata` 循环。
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
        """单表按本地 key 批量走 `fmasterid = fid` 自关联，取回一批母体 fdata（按方言分批）。"""
        cfg = self.config
        rel = _qualified(cfg.schema, table)
        out: dict[str, tuple[bytes, str]] = {}
        for chunk in self._chunks(local_keys):
            frag, params = self._dialect.membership(f"t1.{cfg.number_column}", chunk)
            sql = (
                f"SELECT t1.{cfg.number_column}, t2.{cfg.data_column}, t2.{cfg.number_column} "
                f"FROM {rel} t1 INNER JOIN {rel} t2 "
                f"ON t1.{cfg.master_id_column} = t2.{cfg.id_column} "
                f"WHERE {frag}"
            )
            rows = self._query(sql, params)
            out.update({
                local_key: (_to_bytes(value), master_fnumber)
                for local_key, value, master_fnumber in rows
                if value is not None
            })
        return out

    def fetch_fdata_via_local_ext_bulk(
        self, local_keys: Iterable[str]
    ) -> dict[str, tuple[bytes | None, bytes | None, str | None]]:
        """批量版 `fetch_fdata_via_local_ext`：每张表一条成员判定自关联 SELECT（PG 固定
        2 次往返，Oracle 按 1000 上限切块），不随本地扩展 key 数量线性增长。
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

    # ── 本项目二开元数据同步（2026-07-05 拍板，见 dbmeta/sync.py）───────────
    def list_isv_form_counts(self) -> dict[str, int]:
        """按 fisv 分组统计表单表里的表单数（只查 form_table，够消歧提示用）。

        不在这里排除 kingdee 等平台内建 ISV——排除策略是业务决策，属于
        `sync.py::resolve_isv`，这里只如实取数。
        """
        cfg = self.config
        rel = _qualified(cfg.schema, cfg.form_table)
        sql = (
            f"SELECT {cfg.isv_column}, count(*) FROM {rel} "
            f"WHERE {self._dialect.non_empty(cfg.isv_column)} "
            f"GROUP BY {cfg.isv_column}"
        )
        rows = self._query(sql)
        return {isv: count for isv, count in rows}

    def list_changed_keys(self, table: str, isv: str, since_ts: str | None) -> list[str]:
        """某表按 isv（+ 可选 since_ts）圈定"变更/全量"key 列表。

        `since_ts=None` 不追加时间过滤，天然退化成"该 isv 下全量"——`sync.py` 固定传
        `None`（每次全量同步，见其模块docstring），此处仍保留 `since_ts` 形参只是复用同一
        条查询语句，不是给调用方挑"增量/全量"用的开关。
        """
        cfg = self.config
        rel = _qualified(cfg.schema, table)
        sql = f"SELECT {cfg.number_column} FROM {rel} WHERE {cfg.isv_column} = ?"
        params: tuple = (isv,)
        if since_ts is not None:
            sql += f" AND {cfg.modify_time_column} > ?"
            params = (isv, since_ts)
        rows = self._query(sql, params)
        return [r[0] for r in rows if r[0] is not None]

    def list_changed_form_and_entity_keys(self, isv: str, since_ts: str | None) -> list[str]:
        """form_table/entity_table 各自变更 key 的并集（去重保序）。"""
        form_keys = self.list_changed_keys(self.config.form_table, isv, since_ts)
        entity_keys = self.list_changed_keys(self.config.entity_table, isv, since_ts)
        return list(dict.fromkeys([*form_keys, *entity_keys]))

    def list_changed_convert_rule_ids(self, isv: str, since_ts: str | None) -> list[str]:
        """同 `list_changed_keys`，但固定查 `convert_rule_table`、取 `id_column`
        （这张表没有 `fnumber`，标识是 `fid`）。"""
        cfg = self.config
        rel = _qualified(cfg.schema, cfg.convert_rule_table)
        sql = f"SELECT {cfg.id_column} FROM {rel} WHERE {cfg.isv_column} = ?"
        params: tuple = (isv,)
        if since_ts is not None:
            sql += f" AND {cfg.modify_time_column} > ?"
            params = (isv, since_ts)
        rows = self._query(sql, params)
        return [r[0] for r in rows if r[0] is not None]

    def _fetch_convert_rule_rows_bulk(self, fids: list[str]) -> dict[str, dict]:
        """按 `fid` 成员判定取回一批转换规则行（含 fdata 与关系本体列；按方言分批）。

        `fenabled`/`fsourceentitynumber`/`ftargetentitynumber` 是这张表的专属列，
        直接写死列名（不做成 `DbConfig` 字段——只有 `convert_rule_table`/
        `isv_column`/`modify_time_column` 需要跨表可配置）。
        """
        cfg = self.config
        rel = _qualified(cfg.schema, cfg.convert_rule_table)
        out: dict[str, dict] = {}
        for chunk in self._chunks(list(fids)):
            frag, params = self._dialect.membership(cfg.id_column, chunk)
            sql = (
                f"SELECT {cfg.id_column}, {cfg.data_column}, {cfg.isv_column}, "
                f"fenabled, fsourceentitynumber, ftargetentitynumber "
                f"FROM {rel} WHERE {frag}"
            )
            rows = self._query(sql, params)
            for fid, fdata, isv, enabled, source_entity, target_entity in rows:
                if fdata is None:
                    continue
                out[fid] = {
                    "fdata": _to_bytes(fdata),
                    "isv": isv,
                    "enabled": _to_optional_bool(enabled),
                    "source_entity": source_entity,
                    "target_entity": target_entity,
                }
        return out

    def fetch_convert_rule_fdata_bulk(self, fids: Iterable[str]) -> dict[str, bytes]:
        """薄封装：只取 fdata（配套测试/诊断用）。"""
        keys = list(dict.fromkeys(fids))
        if not keys:
            return {}
        rows = self._fetch_convert_rule_rows_bulk(keys)
        return {fid: row["fdata"] for fid, row in rows.items()}

    def read_convert_rules_bulk(self, fids: Iterable[str]) -> dict[str, MetaModel]:
        """批量按 fid 取回并各自合成 MetaModel（转换规则不像表单/实体拆两张表，
        `t_botp_convertrule` 单表就有完整关系本体 + fdata，一次 SELECT 够了）。"""
        keys = list(dict.fromkeys(fids))
        if not keys:
            return {}
        rows = self._fetch_convert_rule_rows_bulk(keys)
        out: dict[str, MetaModel] = {}
        for fid, row in rows.items():
            out[fid] = assemble_convert_rule(
                row["fdata"],
                fid=fid,
                isv=row["isv"],
                enabled=row["enabled"],
                source_entity=row["source_entity"],
                target_entity=row["target_entity"],
                template_registry=self._registry,
            )
        return out

    def server_now_iso(self) -> str:
        """DB 服务端当前时间（非客户端本机时间，避免时钟偏差污染增量同步水位）。

        `sync.py` 必须在发起变更查询**之前**调用这个方法记录水位——否则同步过程中
        新提交的变更会被这一轮漏掉、且下一轮的 since_ts 已经晚于它，永久漏同步。
        """
        rows = self._query(self._dialect.now_sql())
        value = rows[0][0]
        if isinstance(value, str):
            return value
        return value.isoformat()

    def ping(self) -> dict:
        """连接自检：确认只读会话可用，回报两张表能否 SELECT（不写任何数据）。"""
        cfg = self.config
        out: dict = {"read_database": cfg.read_database, "schema": cfg.schema, "tables": {}}
        for table in (cfg.form_table, cfg.entity_table):
            rel = _qualified(cfg.schema, table)
            try:
                rows = self._query(f"SELECT count(*) FROM {rel}")
                out["tables"][table] = {"ok": True, "count": rows[0][0]}
            except Exception as e:  # 表不存在/无权限都如实回报，不臆断
                out["tables"][table] = {"ok": False, "error": str(e)}
        return out
