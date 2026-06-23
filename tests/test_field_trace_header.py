"""回归测试 —— 表头字段按「定义坐标」精确钻取不应落空。

复现的 bug：Web「全部坐标」视图把表头字段的写入分到 (单据, header, entry_key=None) 组、
正常显示「有写入」；但点击该坐标钻进去时，前端用 field 表的 entity_key（表头字段存的是
表头实体 key，非 None）当 entry 传入，与 field_access 里表头 entry_key 恒为 None 对不上，
精确桶为空 → 落到「可能命中」→ 报「该精确坐标无确定命中」，与外层分组自相矛盾。

修复：entry_key 仅对分录/子分录有意义，其余层级（含表头）一律归一为 None。
"""

from __future__ import annotations

from pathlib import Path

from cosmic_kb.graph import store
from cosmic_kb.report import field_trace

from _synthkb import make_kb


def _conn(tmp_path: Path):
    return store.open_kb(make_kb(tmp_path))


def test_header_field_drilldown_matches_overview(tmp_path: Path):
    """表头字段 cqkd_collateralstatus：发现态（裸字段）有写入；
    按定义坐标（form + 表头实体 key + level=header）钻取应命中同一条写入，不报无命中。"""
    conn = _conn(tmp_path)
    try:
        # 1) 发现态：列全部坐标，应见到 cqkd_assetcard 表头组里的写入。
        flat = field_trace.field_trace(conn, "cqkd_collateralstatus")
        groups = {(g["form_key"], g["level"], g["entry_key"]): g for g in flat["groups"]}
        head_group = groups[("cqkd_assetcard", "header", None)]
        assert head_group["summary"]["writers"] == 1

        # 该字段的定义坐标（消歧菜单）里，表头字段的 entity_key = 表头实体 key（非 None）。
        occ = next(o for o in flat["occurrences"] if o["level"] == "header")
        assert occ["entity_key"] == "cqkd_assetcard"

        # 2) 模拟 Web 点击定义坐标钻取：传入 entity_key 当 entry（修复前的崩点）。
        ft = field_trace.field_trace(
            conn, "cqkd_collateralstatus",
            form_key="cqkd_assetcard", entry_key=occ["entity_key"], level="header",
        )
        # 修复后：entry_key 被归一为 None，精确桶命中那条写入；不再报「无确定命中」。
        assert ft["summary"]["writers"] == 1
        assert ft["filter"]["entry_key"] is None
        assert ft["note"] != \
            "该精确坐标无确定命中，但本单据该字段有「可能命中（层级/分录存疑）」记录，见下。"
        head_grp = next(
            g for g in ft["groups"]
            if (g["form_key"], g["level"], g["entry_key"]) == ("cqkd_assetcard", "header", None)
        )
        assert head_grp["summary"]["writers"] == 1
    finally:
        conn.close()


def test_entry_field_drilldown_still_filters_by_entry(tmp_path: Path):
    """分录字段的 entry_key 过滤照常生效（修复不能误伤分录/子分录）。"""
    conn = _conn(tmp_path)
    try:
        ft = field_trace.field_trace(
            conn, "cqkd_amount",
            form_key="cqkd_assetcard", entry_key="cqkd_entry", level="entry",
        )
        assert ft["filter"]["entry_key"] == "cqkd_entry"
        # 该分录字段在 cqkd_assetcard.cqkd_entry 上有一条读取记录。
        assert ft["summary"]["readers"] == 1
    finally:
        conn.close()
