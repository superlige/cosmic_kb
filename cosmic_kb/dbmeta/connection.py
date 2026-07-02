"""底层库连接层 —— 强制只读，可扩展多数据库。

红线（用户 2026-07-02 反复强调）：**绝对只读，绝不增删改**。这里做多重防线，
任何一层单独失效都仍拦得住写入：

    1. SQL 白名单：本层只暴露 `query`，且入参 SQL 必须以 SELECT 开头，
       出现 INSERT/UPDATE/DELETE/DROP/... 关键字直接拒绝（`assert_readonly_sql`）。
    2. 会话只读：PostgreSQL 连接建好即 `SET SESSION CHARACTERISTICS ... READ ONLY`
       + psycopg2 `set_session(readonly=True, autocommit=True)`，服务端层面回绝写。
    3. 永不提交：不调用 commit；autocommit 只对 SELECT 生效，无脏数据。

驱动抽象：`MetaDbDriver` 定义"连接 + 查询"接口，`PostgresDriver` 是首个实现。
新增数据库（mysql/oracle/达梦）只需实现同接口并在 `get_driver` 注册，上层不改。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

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


class MetaDbDriver(ABC):
    """元数据库驱动接口：连接一个只读会话并执行 SELECT。"""

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


# 驱动注册表：新增数据库在此登记即可，上层按 config.driver 取。
_DRIVERS: dict[str, type[MetaDbDriver]] = {
    "postgresql": PostgresDriver,
    "postgres": PostgresDriver,
    "pg": PostgresDriver,
}


def get_driver(config: DbConfig) -> MetaDbDriver:
    """按配置的 driver 名取对应只读驱动实例。未支持的类型抛错。"""
    key = (config.driver or "").strip().lower()
    cls = _DRIVERS.get(key)
    if cls is None:
        raise ValueError(
            f"暂不支持的数据库类型 driver={config.driver!r}，"
            f"当前支持：{sorted(set(_DRIVERS))}"
        )
    return cls(config)
