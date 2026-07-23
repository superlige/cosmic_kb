"""底层库连接层 —— 强制只读，可扩展多数据库。

红线（用户 2026-07-02 反复强调）：**绝对只读，绝不增删改**。这里做多重防线，
任何一层单独失效都仍拦得住写入：

    1. SQL 白名单：本层只暴露 `query`，且入参 SQL 必须以 SELECT 开头，
       出现 INSERT/UPDATE/DELETE/DROP/... 关键字直接拒绝（`assert_readonly_sql`）。
    2. 会话只读：PostgreSQL 连接建好即 `SET SESSION CHARACTERISTICS ... READ ONLY`
       + psycopg2 `set_session(readonly=True, autocommit=True)`，服务端层面回绝写。
    3. 永不提交：不调用 commit；autocommit 只对 SELECT 生效，无脏数据。

驱动抽象：`MetaDbDriver` 定义"连接 + 查询"接口，`PostgresDriver`/`OracleDriver` 是实现。
新增数据库（mysql/达梦…）只需实现同接口并在 `get_driver` 注册，上层不改。

SQL 方言（`SqlDialect`）：reader 拼 SQL 时各库存在差异（占位符 `%s` vs `:1`、
批量 `= ANY(?)` vs `IN (?,?)`、空串判定、取当前时间是否要 `FROM DUAL`），这些差异
收敛进 `SqlDialect`，reader 一律用中性 `?` 占位符写 SQL，最终由 `dialect.finalize()`
落到具体方言——上层查询语句本身与库无关。每个驱动类挂 `dialect_cls`，`get_dialect`
按 `config.driver` 取，`reader` 独立于活动连接就能拿到方言（便于假驱动测试）。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Sequence

from .config import DbConfig

# 只读 SQL 校验：必须 SELECT 开头；禁止出现任何写/DDL 关键字（大小写无关、词边界匹配）。
_WRITE_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "merge", "replace", "call", "do", "copy",
    "vacuum", "reindex", "comment", "lock", "set",
)
_WRITE_RE = re.compile(r"\b(" + "|".join(_WRITE_KEYWORDS) + r")\b", re.IGNORECASE)


def assert_readonly_sql(sql: str) -> None:
    """校验一条 SQL 是纯只读 SELECT，否则抛 ValueError（第一道防线）。"""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise ValueError("空 SQL")
    # 允许 SELECT / WITH（CTE）开头的只读查询。
    head = stripped.split(None, 1)[0].lower()
    if head not in ("select", "with"):
        raise ValueError(f"只读通道仅允许 SELECT/WITH 查询，拒绝：{stripped[:40]!r}")
    # 即便以 SELECT 开头，也不允许语句里夹带写关键字（防拼接注入式写入）。
    if _WRITE_RE.search(stripped):
        raise ValueError(f"SQL 含写/DDL 关键字，只读通道拒绝：{stripped[:60]!r}")
    # 禁止多语句（分号分隔的第二条），杜绝 `SELECT 1; DELETE ...`。
    if ";" in stripped:
        raise ValueError("只读通道不允许多语句（分号）")


class SqlDialect:
    """SQL 方言（默认即 PostgreSQL/MySQL 那一套：`%s` 占位符 + 数组 `= ANY`）。

    reader 用中性 `?` 占位符写 SQL，交给 `finalize()` 翻成本方言占位符；批量成员判定
    交给 `membership()`（不同库片段与参数形态都不同）；`non_empty()`/`now_sql()` 各库
    也有差异。新增库继承本类只覆盖有差异的方法即可，reader 完全不感知。
    """

    # 单批 IN/成员判定的元素上限。PG 用数组 `= ANY(?)` 单参数、无长度限制，给个大值
    # 相当于不分批（下游按此把候选切块，PG 永远一块）。
    in_chunk_size = 100_000

    def finalize(self, sql: str) -> str:
        """把中性 `?` 占位符翻成本方言占位符。PG/MySQL 用 `%s`。"""
        return sql.replace("?", "%s")

    def membership(self, column: str, values: Sequence[Any]) -> tuple[str, tuple[Any, ...]]:
        """`column ∈ values` 的 SQL 片段 + 追加参数。

        PG 用数组 `= ANY(?)`：一个参数装整份列表，无 IN 列表长度限制，故 `in_chunk_size`
        取很大值即可。片段里的 `?` 同样由 `finalize()` 统一翻译。
        """
        return f"{column} = ANY(?)", (list(values),)

    def non_empty(self, column: str) -> str:
        """"非空字符串"判定。PG 里空串 `''` 与 NULL 不同，两者都要排除。"""
        return f"{column} IS NOT NULL AND {column} != ''"

    def now_sql(self) -> str:
        """取服务端当前时间的 SELECT 语句。"""
        return "SELECT CURRENT_TIMESTAMP"


class OracleDialect(SqlDialect):
    """Oracle 方言：占位符 `:1/:2…`、`IN (…)` 展开、空串即 NULL、`FROM DUAL`。"""

    # Oracle 单个 IN 列表最多 1000 个表达式，超了要分批（下游按此切块）。
    in_chunk_size = 1000

    def finalize(self, sql: str) -> str:
        # oracledb 位置绑定用 :1, :2 …（不认 %s / ?），按出现顺序编号。
        out: list[str] = []
        idx = 0
        for ch in sql:
            if ch == "?":
                idx += 1
                out.append(f":{idx}")
            else:
                out.append(ch)
        return "".join(out)

    def membership(self, column: str, values: Sequence[Any]) -> tuple[str, tuple[Any, ...]]:
        vals = list(values)
        if not vals:
            # Oracle 不接受空 `IN ()`，空集合退化成恒假条件（不发无意义查询更好，
            # 但 reader 的批量入口已对空输入短路，这里只是兜底不崩）。
            return "1 = 0", ()
        holes = ", ".join(["?"] * len(vals))
        return f"{column} IN ({holes})", tuple(vals)

    def non_empty(self, column: str) -> str:
        # Oracle 里空串就是 NULL，`IS NOT NULL` 已排除空串；反而 `!= ''` 恒为 UNKNOWN
        # 会把所有行都过滤掉，绝不能带上。
        return f"{column} IS NOT NULL"

    def now_sql(self) -> str:
        # Oracle 的 SELECT 必须有 FROM，取当前时间要 `FROM DUAL`。
        return "SELECT CURRENT_TIMESTAMP FROM DUAL"


class MetaDbDriver(ABC):
    """元数据库驱动接口：连接一个只读会话并执行 SELECT。"""

    # 该驱动对应的 SQL 方言类（reader 按 config.driver 取，见 get_dialect）。
    dialect_cls: type[SqlDialect] = SqlDialect

    def __init__(self, config: DbConfig) -> None:
        self.config = config

    @abstractmethod
    def connect(self) -> None:
        """建立**只读**连接。实现方必须在此把会话钉成只读。"""

    @abstractmethod
    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple]:
        """执行只读 SELECT，返回行列表。实现方须先过 `assert_readonly_sql`。"""

    @abstractmethod
    def close(self) -> None:
        """关闭连接。"""

    def __enter__(self) -> MetaDbDriver:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class PostgresDriver(MetaDbDriver):
    """PostgreSQL 只读驱动（依赖 psycopg2，属可选依赖组 `postgres`）。"""

    dialect_cls = SqlDialect

    def __init__(self, config: DbConfig) -> None:
        super().__init__(config)
        self._conn: Any = None

    def connect(self) -> None:
        try:
            import psycopg2  # 延迟导入：未装 postgres 依赖组时不影响其它功能
        except ImportError as e:  # pragma: no cover - 依赖缺失路径
            raise RuntimeError(
                "缺少 psycopg2，请安装：pip install psycopg2-binary"
                "（或在 cosmic_kb 工具目录下 pip install -e \".[postgres]\"）"
            ) from e
        cfg = self.config
        self._conn = psycopg2.connect(
            host=cfg.host,
            port=cfg.port,
            dbname=cfg.read_database,   # 连到元数据表所在库
            user=cfg.user,
            password=cfg.password,
        )
        # 第二道防线：会话级只读 + 自动提交（不留写事务窗口）。
        self._conn.set_session(readonly=True, autocommit=True)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple]:
        assert_readonly_sql(sql)                 # 第一道防线
        if self._conn is None:
            raise RuntimeError("连接未建立，请先 connect()")
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class OracleDriver(MetaDbDriver):
    """Oracle 只读驱动（依赖 python-oracledb，属可选依赖组 `oracle`）。

    python-oracledb 默认走 thin 模式：纯 Python，无需本机装 Oracle Instant Client，
    契合"本机离线跑"的定位。只读三重防线与 PG 一致：
        1. SQL 白名单（`assert_readonly_sql`，与 PG 共用）；
        2. 会话级 `SET TRANSACTION READ ONLY`（服务端拒写，best-effort：个别环境权限
           受限也不影响另外两道防线）；
        3. 永不 commit（autocommit 保持关闭，SELECT 不需要提交）。
    """

    dialect_cls = OracleDialect

    def __init__(self, config: DbConfig) -> None:
        super().__init__(config)
        self._conn: Any = None

    def connect(self) -> None:
        try:
            import oracledb  # 延迟导入：未装 oracle 依赖组时不影响其它功能
        except ImportError as e:  # pragma: no cover - 依赖缺失路径
            raise RuntimeError(
                "缺少 python-oracledb，请安装：pip install oracledb"
                "（或在 cosmic_kb 工具目录下 pip install -e \".[oracle]\"）"
            ) from e
        cfg = self.config
        # LOB 直取为 str/bytes（fdata 多为 CLOB/BLOB），交解析层健壮解码，避免拿到
        # 需 .read() 的 LOB 句柄。设在全局 defaults 上，thin 模式即时生效。
        oracledb.defaults.fetch_lobs = False
        # Oracle 无跨库查询：连的 service 就是元数据表所在库（read_database 优先回落
        # database）；schema/owner 由 config.schema 指定（Oracle 里通常是大写用户名）。
        service = cfg.read_database
        dsn = oracledb.makedsn(cfg.host, cfg.port, service_name=service)
        self._conn = oracledb.connect(user=cfg.user, password=cfg.password, dsn=dsn)
        self._conn.autocommit = False  # 第三道防线：永不自动提交
        # 第二道防线：会话/事务级只读。best-effort——权限受限时白名单+永不提交仍拦得住。
        try:
            with self._conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
        except Exception:  # pragma: no cover - 环境相关，不作为硬失败
            pass

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple]:
        assert_readonly_sql(sql)                 # 第一道防线
        if self._conn is None:
            raise RuntimeError("连接未建立，请先 connect()")
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# 驱动注册表：新增数据库在此登记即可，上层按 config.driver 取。
_DRIVERS: dict[str, type[MetaDbDriver]] = {
    "postgresql": PostgresDriver,
    "postgres": PostgresDriver,
    "pg": PostgresDriver,
    "oracle": OracleDriver,
    "oracledb": OracleDriver,
    "ora": OracleDriver,
}


def _resolve_driver_cls(config: DbConfig) -> type[MetaDbDriver]:
    """按 config.driver 取驱动类，未支持则抛统一错误。"""
    key = (config.driver or "").strip().lower()
    cls = _DRIVERS.get(key)
    if cls is None:
        raise ValueError(
            f"暂不支持的数据库类型 driver={config.driver!r}，"
            f"当前支持：{sorted(set(_DRIVERS))}"
        )
    return cls


def get_driver(config: DbConfig) -> MetaDbDriver:
    """按配置的 driver 名取对应只读驱动实例。未支持的类型抛错。"""
    return _resolve_driver_cls(config)(config)


def get_dialect(config: DbConfig) -> SqlDialect:
    """按配置的 driver 名取对应 SQL 方言实例（不建连接，供 reader 拼 SQL 用）。"""
    return _resolve_driver_cls(config).dialect_cls()
