"""字段名核对工具测试：标识 → 真实元数据中文名+坐标，钉不出回 None（不臆造）。

合成 KB（`_synthkb.make_kb`）已含所需样本：
- `cqkd_collateralstatus`「抵押状态」表头字段（单坐标）。
- `cqkd_amount`「金额」跨两单据（assetcard·entry + contract·header）→ 同 key 多坐标。
- `cqkd_entry`「资产明细」分录容器（在 entity 表，不在 field 表）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cosmic_kb.graph import store
from cosmic_kb.report import resolve_fields

from _synthkb import make_kb


@pytest.fixture()
def conn(tmp_path: Path):
    db = make_kb(tmp_path)
    c = store.open_kb(db)
    yield c
    c.close()


def test_resolve_single_field_name(conn):
    """① 字段名解析：抵押状态原样返回，不靠命名惯例。"""
    r = resolve_fields.resolve_fields(conn, ["cqkd_collateralstatus"])["resolved"]
    items = r["cqkd_collateralstatus"]
    assert items and len(items) == 1
    it = items[0]
    assert it["kind"] == "field" and it["name"] == "抵押状态"
    assert it["form_key"] == "cqkd_assetcard" and it["level"] == "header"


def test_resolve_same_key_multi_coords(conn):
    """② 同 key 跨多坐标：金额在两张单各一份，全摆出不替选。"""
    items = resolve_fields.resolve_fields(conn, ["cqkd_amount"])["resolved"]["cqkd_amount"]
    coords = {(it["form_key"], it["level"]) for it in items}
    assert coords == {("cqkd_assetcard", "entry"), ("cqkd_contract", "header")}
    assert all(it["name"] == "金额" for it in items)


def test_resolve_entry_container_key(conn):
    """③ 分录容器 key（entity 表）：cqkd_entry 能解析，kind=entry + parent_key。"""
    items = resolve_fields.resolve_fields(conn, ["cqkd_entry"])["resolved"]["cqkd_entry"]
    assert items and len(items) == 1
    it = items[0]
    assert it["kind"] == "entry" and it["name"] == "资产明细"
    assert it["form_key"] == "cqkd_assetcard" and it["parent_key"] == "cqkd_assetcard"


def test_resolve_subentry_container_key(conn):
    """③b 子分录容器 key（entity 表 level=subentry）：与分录同一代码路径，kind=subentry。"""
    conn.execute(
        "INSERT INTO entity(form_key,key,name,level,parent_key,table_name) "
        "VALUES(?,?,?,?,?,?)",
        ("cqkd_assetcard", "cqkd_subentry", "抵押物明细", "subentry", "cqkd_entry", "t_sub"),
    )
    conn.commit()
    items = resolve_fields.resolve_fields(conn, ["cqkd_subentry"])["resolved"]["cqkd_subentry"]
    assert items and len(items) == 1
    it = items[0]
    assert it["kind"] == "subentry" and it["name"] == "抵押物明细"
    assert it["form_key"] == "cqkd_assetcard" and it["parent_key"] == "cqkd_entry"


# ── 单据(表单)中文名解析（2026-07-05 复盘：真实排障中模型对纯表单标识无工具可查，只能凭字面
# 翻译，如 cqkd_invoic_apply 被猜成"开票申请"——`resolve_fields` 补上 form 表查询）───────────

def test_resolve_form_name_only(conn):
    """④ 纯表单标识（无对应字段/实体记录，模拟 .load("cqkd_invoic_apply", ...) 场景）：
    kind=form，给出真实中文名，不再让模型凭字面翻译。"""
    conn.execute(
        "INSERT INTO form(key,name,form_type,model_type,isv,app_key,module,source_dym) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("cqkd_invoic_apply", "开票申请单", "bill", "BillFormModel", "cqkd", "cqkd_assets",
         "cqkd_assets", "i.dym"),
    )
    conn.commit()
    items = resolve_fields.resolve_fields(conn, ["cqkd_invoic_apply"])["resolved"]["cqkd_invoic_apply"]
    assert items and len(items) == 1
    it = items[0]
    assert it["kind"] == "form" and it["name"] == "开票申请单"
    assert it["form_key"] == "cqkd_invoic_apply" and it["form_type"] == "bill"


def test_resolve_form_and_header_entity_coexist(conn):
    """⑤ 同一 key 既是单据 key 又是表头实体 key（fixture 里 cqkd_assetcard 两处同名）：
    两条都摆出、互不覆盖——单据的"业务对象名"和表头实体的"容器名"是两回事。"""
    items = resolve_fields.resolve_fields(conn, ["cqkd_assetcard"])["resolved"]["cqkd_assetcard"]
    kinds = {it["kind"] for it in items}
    assert kinds == {"form", "header"}
    form_item = next(it for it in items if it["kind"] == "form")
    header_item = next(it for it in items if it["kind"] == "header")
    assert form_item["name"] == "资产卡片" and form_item["form_type"] == "bill"
    assert header_item["name"] == "资产卡片主体"


def test_render_form_kind(conn):
    """render_resolve_fields 对 kind=form 单独分支渲染，不误套容器格式（缺 level/parent_key）。"""
    conn.execute(
        "INSERT INTO form(key,name,form_type,model_type,isv,app_key,module,source_dym) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("cqkd_invoic_apply", "开票申请单", "bill", "BillFormModel", "cqkd", "cqkd_assets",
         "cqkd_assets", "i.dym"),
    )
    conn.commit()
    d = resolve_fields.resolve_fields(conn, ["cqkd_invoic_apply"])
    text = resolve_fields.render_resolve_fields(d)
    assert "单据 cqkd_invoic_apply「开票申请单」" in text and "[bill]" in text


def test_resolve_access_hint_by_field_type(conn):
    """字段坐标带 field_type + 派生 access 取值语义（堵"getDynamicObjectCollection 当分录"）：
    - 多选基础资料(MulBasedataField) → access 含"多选基础资料"/"不是分录行"。
    - 单选基础资料(BasedataField) → access 含"基础资料字段"。
    - 标量字段(ComboField，如 cqkd_collateralstatus) → access 为 None（不强加语义）。
    - 分录容器(entity 表) → access 含"分录容器"。"""
    conn.executemany(
        "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        [
            ("u_mul", "cqkd_assetcard", "cqkd_assetcard", "cqkd_httz", "退租合同",
             "fhttz", "MulBasedataField", "entity", "header"),
            ("u_one", "cqkd_assetcard", "cqkd_assetcard", "cqkd_org", "核算组织",
             "forg", "BasedataField", "entity", "header"),
        ],
    )
    conn.commit()
    r = resolve_fields.resolve_fields(
        conn, ["cqkd_httz", "cqkd_org", "cqkd_collateralstatus", "cqkd_entry"])["resolved"]
    mul = r["cqkd_httz"][0]
    assert mul["field_type"] == "MulBasedataField"
    assert "多选基础资料" in mul["access"] and "不是分录行" in mul["access"]
    assert "基础资料字段" in r["cqkd_org"][0]["access"]
    assert r["cqkd_collateralstatus"][0]["access"] is None     # 标量字段不强加语义
    assert "分录容器" in r["cqkd_entry"][0]["access"]            # 容器侧镜像提示


def test_resolve_unknown_returns_none(conn):
    """④ 钉不出回 None（诚实留白，不臆造）。"""
    r = resolve_fields.resolve_fields(conn, ["cqkd_nope"])["resolved"]
    assert r["cqkd_nope"] is None


def test_resolve_batch_mixed(conn):
    """批量混合：一次传字段+容器+未知，各自正确归位。"""
    r = resolve_fields.resolve_fields(
        conn, ["cqkd_collateralstatus", "cqkd_entry", "cqkd_nope"])["resolved"]
    assert r["cqkd_collateralstatus"][0]["kind"] == "field"
    assert r["cqkd_entry"][0]["kind"] == "entry"
    assert r["cqkd_nope"] is None


def test_render_marks_null(conn):
    """文本视图明确标 null（提示标 unknown 勿猜），命中列坐标。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_collateralstatus", "cqkd_nope"])
    text = resolve_fields.render_resolve_fields(d)
    assert "抵押状态" in text
    assert "null" in text and "cqkd_nope" in text


def test_mcp_tool_same_as_report(tmp_path: Path, monkeypatch):
    """⑤ MCP 工具与 report 函数同口径（不重写取证逻辑）。"""
    from cosmic_kb.mcp import server as mcp_server

    db = make_kb(tmp_path)
    monkeypatch.setenv("COSMIC_KB_DB", str(db))
    got = mcp_server.tool_resolve_fields(["cqkd_amount", "cqkd_entry"])
    c = store.open_kb(db)
    try:
        want = resolve_fields.resolve_fields(c, ["cqkd_amount", "cqkd_entry"])
    finally:
        c.close()
    assert got == want


# ── 实体限定精确匹配（2026-07-05，起因见 docs/read_source字段名解析逻辑.md §5）─────────
# 模型自己读源码看到 `.load("cqkd_contract", ...)` 这类实体字面量后，直接传
# "form_key.field_key" 精确查库，不必再靠工具做文件级数据流推断去猜归属。

def test_resolve_qualified_key_exact_match(conn):
    """`"cqkd_contract.cqkd_amount"`：cqkd_amount 跨两单同名，限定符收敛到 cqkd_contract 那一条。"""
    r = resolve_fields.resolve_fields(conn, ["cqkd_contract.cqkd_amount"])["resolved"]
    items = r["cqkd_contract.cqkd_amount"]
    assert items and len(items) == 1
    assert items[0]["form_key"] == "cqkd_contract" and items[0]["level"] == "header"


def test_resolve_qualified_key_mismatch_is_honest(conn):
    """限定符是真实单据，但该单据下没有这个字段：不悄悄回退掩盖，诚实给 mismatched_form 提示
    该字段真实所在单据；resolved 仍给全局候选（不是 None），方便模型看出假设错在哪（红线 #4）。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_contract.cqkd_collateralstatus"])
    mm = d.get("mismatched_form", {}).get("cqkd_contract.cqkd_collateralstatus")
    assert mm == {
        "given_form": "cqkd_contract", "field_key": "cqkd_collateralstatus",
        "available_forms": ["cqkd_assetcard"],
    }
    resolved = d["resolved"]["cqkd_contract.cqkd_collateralstatus"]
    assert resolved and resolved[0]["form_key"] == "cqkd_assetcard"


def test_resolve_qualified_prefix_not_a_form_falls_back_to_plain_key(conn):
    """`.` 前缀若不是真实 form_key，不当限定符处理（防误切普通含点标识）——整串当裸 key 查，
    该字面 key 本就不在 field/entity 表里，钉不出回 None。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_nope.cqkd_collateralstatus"])
    assert d["resolved"]["cqkd_nope.cqkd_collateralstatus"] is None
    assert "mismatched_form" not in d


def test_resolve_qualified_key_no_dot_unaffected(conn):
    """不带限定符的裸 key 行为不受本次扩展影响：同 key 跨单据仍全摆出。"""
    items = resolve_fields.resolve_fields(conn, ["cqkd_amount"])["resolved"]["cqkd_amount"]
    assert {it["form_key"] for it in items} == {"cqkd_assetcard", "cqkd_contract"}


def test_render_marks_mismatch(conn):
    """文本视图对 mismatch 给出可读提示，点出限定单据与真实所在单据。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_contract.cqkd_collateralstatus"])
    text = resolve_fields.render_resolve_fields(d)
    assert "cqkd_contract" in text and "cqkd_assetcard" in text
    assert "⚠" in text


# ── 分录/单据.分录 复合限定符（2026-07-05 真实排障复盘）─────────────────────────────
# 模型习惯照搬 trace 的点号坐标写法（单据.分录.字段/分录.字段），老版 _split_qualified 只认
# 单据.字段两段式且限定符须命中 form 表，这两种写法全部落空返回 null（模型误判"字段未登记"）。
# 补上 entity 表限定符 + 三段式（单据.分录.字段），与 field_trace.parse_locator 同一套惯例。

def test_resolve_entry_qualified_key_exact_match(conn):
    """`"cqkd_entry.cqkd_amount"`（分录限定，不带单据）：cqkd_amount 跨两坐标同名，
    限定符命中 entity 表按 entity_key 收敛到分录那一条。"""
    items = resolve_fields.resolve_fields(conn, ["cqkd_entry.cqkd_amount"])["resolved"][
        "cqkd_entry.cqkd_amount"]
    assert items and len(items) == 1
    assert items[0]["form_key"] == "cqkd_assetcard" and items[0]["level"] == "entry"


def test_resolve_entry_qualified_mismatch_is_honest(conn):
    """分录限定符是真实分录，但该分录下没有这个字段：诚实给 mismatched_form（given_entry/
    available_entities），resolved 仍给全局候选，不是 None。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_entry.cqkd_collateralstatus"])
    mm = d.get("mismatched_form", {}).get("cqkd_entry.cqkd_collateralstatus")
    assert mm == {
        "given_entry": "cqkd_entry", "field_key": "cqkd_collateralstatus",
        "available_entities": ["cqkd_assetcard"],
    }
    resolved = d["resolved"]["cqkd_entry.cqkd_collateralstatus"]
    assert resolved and resolved[0]["entity_key"] == "cqkd_assetcard"


def test_resolve_three_part_qualifier_exact_match(conn):
    """`"单据.分录.字段"` 三段式（与 trace 的 `单据.分录.字段` 坐标同一惯例）：
    同时按 form_key + entity_key 收敛，模型可以直接照搬 trace 的写法。"""
    items = resolve_fields.resolve_fields(
        conn, ["cqkd_assetcard.cqkd_entry.cqkd_amount"])["resolved"][
        "cqkd_assetcard.cqkd_entry.cqkd_amount"]
    assert items and len(items) == 1
    assert items[0]["form_key"] == "cqkd_assetcard" and items[0]["level"] == "entry"


def test_resolve_three_part_qualifier_mismatch_reports_both(conn):
    """三段式里分录段给错：mismatched_form 同时带 given_form/given_entry 与两套 available，
    不悄悄只报其中一项掩盖另一项也不对的事实。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_assetcard.cqkd_nope_entry.cqkd_amount"])
    mm = d["mismatched_form"]["cqkd_assetcard.cqkd_nope_entry.cqkd_amount"]
    assert mm["given_form"] == "cqkd_assetcard" and mm["given_entry"] == "cqkd_nope_entry"
    assert set(mm["available_forms"]) == {"cqkd_assetcard", "cqkd_contract"}
    assert set(mm["available_entities"]) == {"cqkd_entry", "cqkd_contract"}


def test_cli_resolve_json(tmp_path: Path, capsys):
    """CLI resolve --json 跑通，输出含解析结果。"""
    from cosmic_kb.cli.main import main

    db = make_kb(tmp_path)
    rc = main(["resolve", "cqkd_collateralstatus", "cqkd_nope", "--db", str(db), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "抵押状态" in out and "resolved" in out
