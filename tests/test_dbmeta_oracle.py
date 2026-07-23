"""Oracle 支持验收：SQL 方言（占位符/成员判定/空串/取时间）+ 驱动注册 + reader 落地。

不连真库：方言纯字符串变换可直接断言；reader 用假驱动（monkeypatch `get_driver`）验证
拼出的 SQL 经方言 finalize 后到达驱动的形状与参数，覆盖 Oracle 与 PostgreSQL 两套。
"""

from __future__ import annotations

import pytest

from cosmic_kb.dbmeta.config import DbConfig
from cosmic_kb.dbmeta.connection import (
    OracleDialect,
    OracleDriver,
    PostgresDriver,
    SqlDialect,
    get_dialect,
    get_driver,
)


# ── 1. 驱动 / 方言注册 ───────────────────────────────────────────────
def test_get_driver_oracle_aliases():
    for name in ("oracle", "oracledb", "ORA", "Oracle"):
        assert isinstance(get_driver(DbConfig(driver=name)), OracleDriver)


def test_get_dialect_maps_driver_to_dialect():
    assert isinstance(get_dialect(DbConfig(driver="postgresql")), SqlDialect)
    assert isinstance(get_dialect(DbConfig(driver="oracle")), OracleDialect)


def test_get_dialect_unknown_type_raises():
    with pytest.raises(ValueError):
        get_dialect(DbConfig(driver="mysql"))


def test_postgres_driver_keeps_default_dialect():
    assert PostgresDriver.dialect_cls is SqlDialect
    assert OracleDriver.dialect_cls is OracleDialect


# ── 2. 方言 finalize：中性 `?` → 各库占位符 ───────────────────────────
def test_pg_finalize_uses_percent_s():
    d = SqlDialect()
    assert d.finalize("SELECT a FROM t WHERE x = ? AND y > ?") == \
        "SELECT a FROM t WHERE x = %s AND y > %s"


def test_oracle_finalize_numbers_placeholders_in_order():
    d = OracleDialect()
    assert d.finalize("SELECT a FROM t WHERE x = ? AND y > ?") == \
        "SELECT a FROM t WHERE x = :1 AND y > :2"
    # 无占位符的语句原样返回。
    assert d.finalize("SELECT count(*) FROM t") == "SELECT count(*) FROM t"


# ── 3. 方言 membership：批量成员判定的片段 + 参数形态 ────────────────────
def test_pg_membership_uses_array_any_single_param():
    d = SqlDialect()
    frag, params = d.membership("fnumber", ["a", "b", "c"])
    assert frag == "fnumber = ANY(?)"
    assert params == (["a", "b", "c"],)              # 整份列表装进一个参数
    assert d.finalize(frag) == "fnumber = ANY(%s)"


def test_oracle_membership_expands_in_list_with_one_param_each():
    d = OracleDialect()
    frag, params = d.membership("fnumber", ["a", "b", "c"])
    assert frag == "fnumber IN (?, ?, ?)"
    assert params == ("a", "b", "c")                 # 每个值一个参数
    assert d.finalize(frag) == "fnumber IN (:1, :2, :3)"


def test_oracle_membership_empty_is_false_condition():
    frag, params = OracleDialect().membership("fnumber", [])
    assert frag == "1 = 0" and params == ()          # 空集合恒假，不发无意义 IN ()


# ── 4. 方言 non_empty / now_sql 的库差异 ──────────────────────────────
def test_non_empty_predicate_differs():
    # PG 空串与 NULL 不同，两者都排除；Oracle 空串即 NULL，只需 IS NOT NULL
    assert SqlDialect().non_empty("fisv") == "fisv IS NOT NULL AND fisv != ''"
    assert OracleDialect().non_empty("fisv") == "fisv IS NOT NULL"


def test_now_sql_oracle_needs_from_dual():
    assert SqlDialect().now_sql() == "SELECT CURRENT_TIMESTAMP"
    assert OracleDialect().now_sql() == "SELECT CURRENT_TIMESTAMP FROM DUAL"


def test_oracle_chunk_size_bounded_pg_unbounded():
    assert OracleDialect().in_chunk_size == 1000
    assert SqlDialect().in_chunk_size >= 100_000


# ── 5. reader 落地：Oracle 方言下 SQL 经 finalize 到达驱动的真实形状 ───────
class _FakeDriver:
    """假驱动：记录到达的 (sql, params)，按顺序弹出预置结果，不连真库。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def connect(self):
        pass

    def query(self, sql, params=()):
        self.calls.append((sql, params))
        return self._responses.pop(0) if self._responses else []

    def close(self):
        pass


def _open_reader(monkeypatch, config, responses):
    from cosmic_kb.dbmeta import reader as reader_mod

    fake = _FakeDriver(responses)
    monkeypatch.setattr(reader_mod, "get_driver", lambda cfg: fake)
    r = reader_mod.DbMetaReader(config)
    r.open()
    return r, fake


def test_reader_oracle_scalar_placeholder_is_numbered(monkeypatch):
    """单值查询：Oracle 下 `?` 应被 finalize 成 `:1`，参数原样透传。"""
    r, fake = _open_reader(monkeypatch, DbConfig(driver="oracle"), [[(b"<x/>",)]])
    try:
        r._fetch_one_fdata(r.config.form_table, "bd_customer")
    finally:
        r.close()
    sql, params = fake.calls[0]
    assert ":1" in sql and "?" not in sql and "%s" not in sql
    assert params == ("bd_customer",)


def test_reader_oracle_bulk_uses_in_list_and_expanded_params(monkeypatch):
    """批量查询：Oracle 下应是 `IN (:1, :2)` + 逐值参数，不是 PG 的 `= ANY(%s)`。"""
    responses = [
        [("bd_customer", b"<f1/>")],   # form 表
        [("bd_customer", b"<e1/>")],   # entity 表
    ]
    r, fake = _open_reader(monkeypatch, DbConfig(driver="oracle"), responses)
    try:
        r.fetch_fdata_bulk(["bd_customer", "bd_supplier"])
    finally:
        r.close()
    assert len(fake.calls) == 2
    for sql, params in fake.calls:
        assert "IN (:1, :2)" in sql
        assert "= ANY" not in sql and "%s" not in sql
        assert params == ("bd_customer", "bd_supplier")   # 逐值展开，非单个列表参数


def test_reader_oracle_now_sql_has_from_dual(monkeypatch):
    r, fake = _open_reader(monkeypatch, DbConfig(driver="oracle"), [[("2026-07-05T10:00:00",)]])
    try:
        assert r.server_now_iso() == "2026-07-05T10:00:00"
    finally:
        r.close()
    sql, _ = fake.calls[0]
    assert sql == "SELECT CURRENT_TIMESTAMP FROM DUAL"


def test_reader_oracle_bulk_chunks_over_in_limit(monkeypatch):
    """超过 Oracle IN 上限的批量要自动切块成多条 SQL（不撞 1000 表达式上限）。"""
    monkeypatch.setattr(OracleDialect, "in_chunk_size", 2)  # 缩小便于断言
    keys = ["k1", "k2", "k3", "k4", "k5"]                    # 5 个 → 每表 3 块
    responses = [[], [], [], [], [], []]                    # 2 张表 × 3 块 = 6 条
    r, fake = _open_reader(monkeypatch, DbConfig(driver="oracle"), responses)
    try:
        r.fetch_fdata_bulk(keys)
    finally:
        r.close()
    assert len(fake.calls) == 6                             # form 3 块 + entity 3 块
    # 每块参数数量 ≤ 2，且并起来正好覆盖全部 key（form 表那 3 块）。
    form_calls = fake.calls[:3]
    covered = [v for _sql, params in form_calls for v in params]
    assert covered == keys
    assert all(len(params) <= 2 for _sql, params in form_calls)


def test_reader_pg_still_emits_array_any(monkeypatch):
    """回归护栏：PostgreSQL 路径仍是单条 `= ANY(%s)` + 单列表参数，未被方言改造带偏。"""
    responses = [
        [("bd_customer", b"<f1/>")],
        [("bd_customer", b"<e1/>")],
    ]
    r, fake = _open_reader(monkeypatch, DbConfig(driver="postgresql"), responses)
    try:
        r.fetch_fdata_bulk(["bd_customer", "bd_supplier"])
    finally:
        r.close()
    assert len(fake.calls) == 2
    for sql, params in fake.calls:
        assert "= ANY(%s)" in sql and ":1" not in sql
        assert params == (["bd_customer", "bd_supplier"],)
