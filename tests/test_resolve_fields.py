"""字段名核对工具测试：标识 → 真实元数据中文名+坐标，钉不出回 None（不臆造）。

合成 KB（`_synthkb.make_kb`）已含所需样本：
- `cqkd_collateralstatus`「抵押状态」表头字段（单坐标）。
- `cqkd_amount`「金额」跨两单据（assetcard·entry + contract·header）→ 同 key 多坐标。
- `cqkd_entry`「资产明细」分录容器（在 entity 表，不在 field 表）。
"""

from __future__ import annotations

import json
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
    # field_kind 码人读标签焊进返回值本体（此前裸码 "entity"，模型不知道是什么意思）。
    assert it["field_kind"] == "entity"
    assert it["field_kind_label"] == resolve_fields._FIELD_KIND_LABEL["entity"]


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


# ── 实体限定精确匹配（2026-07-05，起因见 docs/参考手册/read_source字段名解析逻辑.md §5）─────────
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


# ── 下拉选项 + 基础资料引用实体类型（2026-07-05 增强）─────────────────────────────
# 起因：字段坐标只告诉模型"这是下拉/这是引用"，没告诉存储值的真实含义、引用的是哪张实体，
# 模型只能凭猜。resolve_fields 顺带把已建库的 combo_items/ref_entity 焊进字段命中条目。

def test_resolve_combo_items(conn):
    """下拉字段命中带 combo_items（存储值→中文含义），不用再猜枚举含义。"""
    conn.execute(
        "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("u_combo", "cqkd_assetcard", "cqkd_assetcard", "cqkd_isvalid", "是否有效",
         "fisvalid", "ComboField", "entity", "header"),
    )
    conn.executemany(
        "INSERT INTO field_combo_item(field_uid,value,caption) VALUES(?,?,?)",
        [("u_combo", "1", "是"), ("u_combo", "0", "否")],
    )
    conn.commit()
    items = resolve_fields.resolve_fields(conn, ["cqkd_isvalid"])["resolved"]["cqkd_isvalid"]
    assert items and len(items) == 1
    combo = sorted((c["value"], c["caption"]) for c in items[0]["combo_items"])
    assert combo == [("0", "否"), ("1", "是")]


def test_resolve_ref_entity_resolved(conn):
    """基础资料引用字段命中目标单据（本次建库范围内可反查）→ ref_entity 给出 form_key+name。"""
    conn.execute(
        "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level,"
        "ref_entity_id,ref_form_key,ref_form_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u_ref", "cqkd_assetcard", "cqkd_assetcard", "cqkd_orgproperty", "所属组织",
         "forg", "OrgField", "entity", "header", "orgoid123", "cqkd_org", "组织"),
    )
    conn.commit()
    items = resolve_fields.resolve_fields(conn, ["cqkd_orgproperty"])["resolved"]["cqkd_orgproperty"]
    assert items and len(items) == 1
    assert items[0]["ref_entity"] == {"form_key": "cqkd_org", "name": "组织"}
    assert "ref_entity_id" not in items[0]


def test_resolve_ref_entity_id_when_unresolved(conn):
    """基础资料引用字段查不到目标单据 → 退化为 ref_entity_id（原始 oid，诚实留痕不猜）。"""
    conn.execute(
        "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level,"
        "ref_entity_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("u_ref2", "cqkd_assetcard", "cqkd_assetcard", "cqkd_customer", "客户",
         "fcust", "BasedataField", "entity", "header", "unknown-oid"),
    )
    conn.commit()
    items = resolve_fields.resolve_fields(conn, ["cqkd_customer"])["resolved"]["cqkd_customer"]
    assert items and len(items) == 1
    assert items[0]["ref_entity_id"] == "unknown-oid"
    assert "ref_entity" not in items[0]


def test_resolve_plain_field_has_no_combo_or_ref_keys(conn):
    """普通标量字段（无下拉/无引用）：combo_items/ref_entity/ref_entity_id 三个 key 都不出现
    （零增量验证——本次增强不该给不相关字段多塞任何东西）。"""
    items = resolve_fields.resolve_fields(conn, ["cqkd_collateralstatus"])["resolved"][
        "cqkd_collateralstatus"]
    it = items[0]
    assert "combo_items" not in it
    assert "ref_entity" not in it
    assert "ref_entity_id" not in it


def test_render_combo_items_and_ref_entity(conn):
    """文本视图对下拉选项/引用实体分别追加可读行。"""
    conn.execute(
        "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level,"
        "ref_entity_id,ref_form_key,ref_form_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("u_ref3", "cqkd_assetcard", "cqkd_assetcard", "cqkd_orgproperty", "所属组织",
         "forg", "OrgField", "entity", "header", "orgoid123", "cqkd_org", "组织"),
    )
    conn.execute(
        "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("u_combo2", "cqkd_assetcard", "cqkd_assetcard", "cqkd_isvalid", "是否有效",
         "fisvalid", "ComboField", "entity", "header"),
    )
    conn.execute(
        "INSERT INTO field_combo_item(field_uid,value,caption) VALUES(?,?,?)",
        ("u_combo2", "1", "是"),
    )
    conn.commit()
    d = resolve_fields.resolve_fields(conn, ["cqkd_orgproperty", "cqkd_isvalid"])
    text = resolve_fields.render_resolve_fields(d)
    assert "→ 引用 cqkd_org「组织」" in text
    assert "取值: 1=是" in text


# ── issue 4：kind 过滤 ────────────────────────────────────────────────────────

def test_resolve_kind_form_excludes_same_key_field_noise(conn):
    """`kind="form"`：cqkd_assetcard 既是单据 key 又是表头实体 key，指定 kind=form 后
    只返回单据候选，不再混入 header 容器命中的噪声。"""
    items = resolve_fields.resolve_fields(
        conn, ["cqkd_assetcard"], kind="form")["resolved"]["cqkd_assetcard"]
    assert items and {it["kind"] for it in items} == {"form"}


def test_resolve_kind_field_excludes_entry_and_form(conn):
    """`kind="field"`：只返回字段候选，不含分录容器/单据候选。"""
    items = resolve_fields.resolve_fields(
        conn, ["cqkd_collateralstatus"], kind="field")["resolved"]["cqkd_collateralstatus"]
    assert items and all(it["kind"] == "field" for it in items)


def test_resolve_kind_entity_excludes_field(conn):
    """`kind="entity"`：分录容器 key 只返回容器候选。"""
    items = resolve_fields.resolve_fields(
        conn, ["cqkd_entry"], kind="entity")["resolved"]["cqkd_entry"]
    assert items and all(it["kind"] in ("header", "entry", "subentry") for it in items)


# ── kind 逐 key 列表（2026-07-08，真实翻车复盘）────────────────────────────────
# 起因：模型批量传入单据号/分录容器/字段三个不同层级的 key，却用单个 kind="field" 广播给
# 全部三个，导致前两个全部落入 mismatched_kind（诚实报错但等于白跑一次）。补上「kind 传
# 与 keys 等长列表、逐位对应」的能力，从接口形状上让这类广播错误可以被结构性避免。

def test_resolve_kind_list_per_key_mixed_levels(conn):
    """`kind=["form","entity","field"]` 逐位对应三个不同层级的 key，一次全部命中，
    不再需要靠单个 kind 字符串broadcast 导致的 mismatched_kind。"""
    d = resolve_fields.resolve_fields(
        conn, ["cqkd_assetcard", "cqkd_entry", "cqkd_collateralstatus"],
        kind=["form", "entity", "field"],
    )
    assert "mismatched_kind" not in d
    assert {it["kind"] for it in d["resolved"]["cqkd_assetcard"]} == {"form"}
    assert {it["kind"] for it in d["resolved"]["cqkd_entry"]} == {"entry"}
    assert {it["kind"] for it in d["resolved"]["cqkd_collateralstatus"]} == {"field"}


def test_resolve_kind_list_none_placeholder_means_unrestricted(conn):
    """列表某位传 `None`：该位置三路全查，不限定种类（与不传 kind 时该 key 的行为一致）。"""
    d = resolve_fields.resolve_fields(
        conn, ["cqkd_assetcard", "cqkd_collateralstatus"], kind=["form", None],
    )
    assert {it["kind"] for it in d["resolved"]["cqkd_assetcard"]} == {"form"}
    assert {it["kind"] for it in d["resolved"]["cqkd_collateralstatus"]} == {"field"}


def test_resolve_kind_list_length_mismatch_raises(conn):
    """`kind` 列表长度与 `keys` 不一致：拒绝静默截断/循环补齐，直接 ValueError。"""
    with pytest.raises(ValueError, match="长度"):
        resolve_fields.resolve_fields(
            conn, ["cqkd_assetcard", "cqkd_entry"], kind=["form", "entity", "field"])


def test_resolve_kind_scalar_broadcast_still_reproduces_old_mismatch(conn):
    """回归护栏：单个 kind 字符串仍按老行为广播给全部 key（不该悄悄变成逐位），
    混入不同层级时仍会触发 mismatched_kind——证明列表能力是新增的选项而非破坏旧用法。"""
    d = resolve_fields.resolve_fields(
        conn, ["cqkd_assetcard", "cqkd_entry"], kind="form")
    assert "cqkd_entry" in d["mismatched_kind"]


def test_mcp_tool_resolve_fields_kind_list_per_key(tmp_path: Path, monkeypatch):
    """MCP `tool_resolve_fields(kind=[...])` 逐 key 列表穿透，与 report 层同口径。"""
    from cosmic_kb.mcp import server as mcp_server

    db = make_kb(tmp_path)
    monkeypatch.setenv("COSMIC_KB_DB", str(db))
    got = mcp_server.tool_resolve_fields(
        ["cqkd_assetcard", "cqkd_entry", "cqkd_collateralstatus"],
        kind=["form", "entity", "field"],
    )
    assert "mismatched_kind" not in got
    assert {it["kind"] for it in got["resolved"]["cqkd_entry"]} == {"entry"}


def test_cli_resolve_kind_list_per_key(tmp_path: Path, capsys):
    """CLI `resolve --kind form entity field` 多值逐位对应，穿透到 report 层。"""
    from cosmic_kb.cli.main import main

    db = make_kb(tmp_path)
    rc = main([
        "resolve", "cqkd_assetcard", "cqkd_entry", "cqkd_collateralstatus",
        "--db", str(db), "--json", "--kind", "form", "entity", "field",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "mismatched_kind" not in data
    assert {it["kind"] for it in data["resolved"]["cqkd_entry"]} == {"entry"}


def test_cli_resolve_kind_list_length_mismatch_reports_error(tmp_path: Path, capsys):
    """CLI 侧长度不一致：打印错误信息、返回码 2，不崩栈。"""
    from cosmic_kb.cli.main import main

    db = make_kb(tmp_path)
    rc = main([
        "resolve", "cqkd_assetcard", "cqkd_entry",
        "--db", str(db), "--json", "--kind", "form", "entity", "field",
    ])
    assert rc == 2
    assert "长度" in capsys.readouterr().err


def test_cli_resolve_kind_list_none_placeholder(tmp_path: Path, capsys):
    """CLI `--kind form none`：第二位 none 占位表示不限定种类。"""
    from cosmic_kb.cli.main import main

    db = make_kb(tmp_path)
    rc = main([
        "resolve", "cqkd_assetcard", "cqkd_collateralstatus",
        "--db", str(db), "--json", "--kind", "form", "none",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert {it["kind"] for it in data["resolved"]["cqkd_assetcard"]} == {"form"}
    assert {it["kind"] for it in data["resolved"]["cqkd_collateralstatus"]} == {"field"}


def test_cli_resolve_kind_form(tmp_path: Path, capsys):
    """CLI `resolve --kind form` 参数穿透。"""
    from cosmic_kb.cli.main import main

    db = make_kb(tmp_path)
    rc = main(["resolve", "cqkd_assetcard", "--db", str(db), "--json", "--kind", "form"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    items = data["resolved"]["cqkd_assetcard"]
    assert {it["kind"] for it in items} == {"form"}


def test_mcp_tool_resolve_fields_kind_param(tmp_path: Path, monkeypatch):
    """MCP `tool_resolve_fields(kind=...)` 参数穿透，与 report 层同口径。"""
    from cosmic_kb.mcp import server as mcp_server

    db = make_kb(tmp_path)
    monkeypatch.setenv("COSMIC_KB_DB", str(db))
    got = mcp_server.tool_resolve_fields(["cqkd_assetcard"], kind="form")
    items = got["resolved"]["cqkd_assetcard"]
    assert {it["kind"] for it in items} == {"form"}


# ── issue 5：三段式限定符命中容器（分录/子分录）不应误入 mismatched_form ──────────

def test_resolve_three_part_qualifier_hits_subentry_container_no_mismatch(conn):
    """真实翻车场景：三段式限定符命中的是子分录**容器 key**本身（不是字段），此前 `_matches`
    统一按 `entity_key` 比较，容器命中项没有这个键、`.get()` 拿到 None，永远判不匹配，
    导致明明命中却被塞进 `mismatched_form`。修复后按 kind 分支用 `parent_key` 比较。"""
    conn.execute(
        "INSERT INTO form(key,name,form_type,model_type,isv,app_key,module,source_dym) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("cqkd_ht", "合同", "bill", "BillFormModel", "cqkd", "cqkd_assets",
         "cqkd_assets", "ht.dym"),
    )
    conn.executemany(
        "INSERT INTO entity(form_key,key,name,level,parent_key,table_name) VALUES(?,?,?,?,?,?)",
        [
            ("cqkd_ht", "cqkd_ht", "合同主体", "header", None, "t_ht"),
            ("cqkd_ht", "cqkd_zdgl", "账单管理", "entry", "cqkd_ht", "t_zdgl"),
            ("cqkd_ht", "cqkd_zdzfltk", "账单支付流通款", "subentry", "cqkd_zdgl", "t_zdzfltk"),
        ],
    )
    conn.commit()
    key = "cqkd_ht.cqkd_zdgl.cqkd_zdzfltk"
    d = resolve_fields.resolve_fields(conn, [key])
    resolved = d["resolved"][key]
    assert resolved and len(resolved) == 1
    assert resolved[0]["kind"] == "subentry" and resolved[0]["parent_key"] == "cqkd_zdgl"
    assert key not in d.get("mismatched_form", {})


# ── issue 6：平台/继承字段特定单据下 mismatch 时带 note，不当硬警告 ──────────────

def test_resolve_platform_field_mismatch_gets_note(conn):
    """auditdate 类平台字段只在某张单据的元数据里登记为 platform；换一张没登记的单据查，
    不应当成"限定符写错"来硬警告，而是带 note 说明"随模板继承、未逐单据登记"。"""
    conn.execute(
        "INSERT INTO form(key,name,form_type,model_type,isv,app_key,module,source_dym) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("cqkd_tzjezd", "台账结转单", "bill", "BillFormModel", "cqkd", "cqkd_assets",
         "cqkd_assets", "tz.dym"),
    )
    conn.execute(
        "INSERT INTO field(uid,form_key,entity_key,key,name,db_column,field_type,kind,level) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("u_audit", "cqkd_assetcard", "cqkd_assetcard", "auditdate", "审核日期",
         "fauditdate", "DateField", "platform", "header"),
    )
    conn.commit()
    key = "cqkd_tzjezd.auditdate"
    d = resolve_fields.resolve_fields(conn, [key])
    mm = d["mismatched_form"][key]
    assert "note" in mm and "平台标准字段" in mm["note"]
    text = resolve_fields.render_resolve_fields(d)
    assert "提示" in text and "ℹ" in text


def test_cli_resolve_json(tmp_path: Path, capsys):
    """CLI resolve --json 跑通，输出含解析结果。"""
    from cosmic_kb.cli.main import main

    db = make_kb(tmp_path)
    rc = main(["resolve", "cqkd_collateralstatus", "cqkd_nope", "--db", str(db), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "抵押状态" in out and "resolved" in out


# ── mismatched_kind：指定 kind 查不到时反查其它词典，诊断"种类给错了"（issue 4）──────────

def test_resolve_mismatched_kind_field_but_actually_entity(conn):
    """cqkd_entry 实为分录容器 key（entity 表），却传 kind="field" 去查：
    resolve_fields 应反查到它实际是 entry，给出 mismatched_kind 诊断，resolved 仍给候选
    （不是 None——种类猜错不代表这个 key 钉不出）。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_entry"], kind="field")
    mm = d["mismatched_kind"]["cqkd_entry"]
    assert mm["requested_kind"] == "field"
    assert mm["actual_kinds"] == ["entry"]
    assert mm["candidates"] and mm["candidates"][0]["kind"] == "entry"
    assert "qualifier_matches" not in mm   # 裸 key，没有限定符可诊断
    assert d["resolved"]["cqkd_entry"] == mm["candidates"]
    assert "mismatched_form" not in d      # 与 mismatched_form 互斥触发


def test_resolve_mismatched_kind_form_but_actually_header_entity(conn):
    """cqkd_assetcard 既是单据 key 又是表头实体 key；kind="field" 两者都不是字段，
    应反查到 actual_kinds 含 form/header 两种真实种类。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_assetcard"], kind="field")
    mm = d["mismatched_kind"]["cqkd_assetcard"]
    assert mm["requested_kind"] == "field"
    assert set(mm["actual_kinds"]) == {"form", "header"}


def test_resolve_mismatched_kind_with_qualifier_gives_qualifier_matches(conn):
    """带限定符的 key 种类给错时，额外给 qualifier_matches——诊断"限定符本身对不对"这层，
    与 mismatched_form 是纵向两级但这里只走种类这一级（种类都不对，不需要再判单据对不对）。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_assetcard.cqkd_entry"], kind="field")
    mm = d["mismatched_kind"]["cqkd_assetcard.cqkd_entry"]
    assert mm["requested_kind"] == "field"
    assert mm["actual_kinds"] == ["entry"]
    assert mm["qualifier_matches"] and mm["qualifier_matches"][0]["form_key"] == "cqkd_assetcard"


def test_resolve_mismatched_kind_absent_when_kind_correct(conn):
    """种类给对了：不触发 mismatched_kind，行为与之前一致（回归护栏）。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_collateralstatus"], kind="field")
    assert "mismatched_kind" not in d


def test_resolve_mismatched_kind_absent_when_truly_unknown(conn):
    """反查也找不到全局候选（真的钉不出）：不产出 mismatched_kind 噪声，老实回 None。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_totally_unknown_xyz"], kind="field")
    assert "mismatched_kind" not in d
    assert d["resolved"]["cqkd_totally_unknown_xyz"] is None


# ── kind="entity" 两段式分录限定符 fail-closed（2026-07-07，真实翻车复盘）────────────
# 模型传 {"keys":["分录.子分录"],"kind":"entity"}（无单据前缀）时，_matches 在 form_key 为
# None 时不按单据过滤，若该 (parent_key,key) 组合恰好只在别的单据下存在，会返回看似钉准实则
# 未经单据校验的单条候选。这一具体分支硬拒绝，不再走"全摆出不替选"的老路径。

def test_resolve_entity_kind_two_segment_without_form_is_rejected(conn):
    """`kind="entity"` + 两段式「分录.子分录」（无单据前缀）：直接拒绝，不给候选。"""
    conn.execute(
        "INSERT INTO entity(form_key,key,name,level,parent_key,table_name) "
        "VALUES(?,?,?,?,?,?)",
        ("cqkd_assetcard", "cqkd_subentry", "抵押物明细", "subentry", "cqkd_entry", "t_sub"),
    )
    conn.commit()
    key = "cqkd_entry.cqkd_subentry"
    d = resolve_fields.resolve_fields(conn, [key], kind="entity")
    inv = d["invalid_request"][key]
    assert inv["reason"] == "missing_form_key"
    assert inv["entry_key"] == "cqkd_entry" and inv["field_key"] == "cqkd_subentry"
    assert "hint" in inv and inv["hint"]
    assert d["resolved"][key] is None
    assert "mismatched_form" not in d or key not in d["mismatched_form"]
    assert "mismatched_kind" not in d or key not in d["mismatched_kind"]


def test_resolve_entity_kind_three_segment_still_works(conn):
    """三段式「单据.分录.子分录」+ `kind="entity"`：带单据前缀不受拒绝规则影响，正常放行。"""
    conn.execute(
        "INSERT INTO entity(form_key,key,name,level,parent_key,table_name) "
        "VALUES(?,?,?,?,?,?)",
        ("cqkd_assetcard", "cqkd_subentry", "抵押物明细", "subentry", "cqkd_entry", "t_sub"),
    )
    conn.commit()
    key = "cqkd_assetcard.cqkd_entry.cqkd_subentry"
    d = resolve_fields.resolve_fields(conn, [key], kind="entity")
    assert "invalid_request" not in d
    items = d["resolved"][key]
    assert items and len(items) == 1
    assert items[0]["kind"] == "subentry" and items[0]["parent_key"] == "cqkd_entry"


def test_resolve_entity_kind_bare_key_unaffected(conn):
    """裸 key（无点号）+ `kind="entity"`：不触发拒绝规则，行为与之前一致。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_entry"], kind="entity")
    assert "invalid_request" not in d
    items = d["resolved"]["cqkd_entry"]
    assert items and all(it["kind"] in ("header", "entry", "subentry") for it in items)


def test_render_invalid_request(conn):
    """文本视图对 invalid_request 给出 ⛔ 标记与 hint 原文。"""
    conn.execute(
        "INSERT INTO entity(form_key,key,name,level,parent_key,table_name) "
        "VALUES(?,?,?,?,?,?)",
        ("cqkd_assetcard", "cqkd_subentry", "抵押物明细", "subentry", "cqkd_entry", "t_sub"),
    )
    conn.commit()
    key = "cqkd_entry.cqkd_subentry"
    d = resolve_fields.resolve_fields(conn, [key], kind="entity")
    text = resolve_fields.render_resolve_fields(d)
    assert "⛔" in text
    assert d["invalid_request"][key]["hint"] in text


def test_cli_resolve_entity_invalid_request(tmp_path: Path, capsys):
    """CLI `resolve --kind entity` 对两段式无单据前缀的输入，输出里带 invalid_request。"""
    from cosmic_kb.cli.main import main

    db = make_kb(tmp_path)
    conn2 = store.open_kb(db)
    conn2.execute(
        "INSERT INTO entity(form_key,key,name,level,parent_key,table_name) "
        "VALUES(?,?,?,?,?,?)",
        ("cqkd_assetcard", "cqkd_subentry", "抵押物明细", "subentry", "cqkd_entry", "t_sub"),
    )
    conn2.commit()
    conn2.close()
    rc = main([
        "resolve", "cqkd_entry.cqkd_subentry", "--db", str(db), "--json", "--kind", "entity",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["invalid_request"]["cqkd_entry.cqkd_subentry"]["reason"] == "missing_form_key"


def test_mcp_tool_resolve_fields_entity_invalid_request(tmp_path: Path, monkeypatch):
    """MCP `tool_resolve_fields(kind="entity")` 拒绝行为与 report 层同口径。"""
    from cosmic_kb.mcp import server as mcp_server

    db = make_kb(tmp_path)
    conn2 = store.open_kb(db)
    conn2.execute(
        "INSERT INTO entity(form_key,key,name,level,parent_key,table_name) "
        "VALUES(?,?,?,?,?,?)",
        ("cqkd_assetcard", "cqkd_subentry", "抵押物明细", "subentry", "cqkd_entry", "t_sub"),
    )
    conn2.commit()
    conn2.close()
    monkeypatch.setenv("COSMIC_KB_DB", str(db))
    got = mcp_server.tool_resolve_fields(["cqkd_entry.cqkd_subentry"], kind="entity")
    assert got["invalid_request"]["cqkd_entry.cqkd_subentry"]["reason"] == "missing_form_key"


def test_render_mismatched_kind(conn):
    """文本视图对 mismatched_kind 给出可读提示，点出请求种类与实际种类。"""
    d = resolve_fields.resolve_fields(conn, ["cqkd_entry"], kind="field")
    text = resolve_fields.render_resolve_fields(d)
    assert "field" in text and "entry" in text and "⚠" in text


def test_cli_resolve_kind_mismatch(tmp_path: Path, capsys):
    """CLI `resolve --kind field` 对实为容器 key 的输入，输出里带种类纠错提示。"""
    from cosmic_kb.cli.main import main

    db = make_kb(tmp_path)
    rc = main(["resolve", "cqkd_entry", "--db", str(db), "--json", "--kind", "field"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["mismatched_kind"]["cqkd_entry"]["requested_kind"] == "field"


def test_mcp_tool_resolve_fields_kind_mismatch(tmp_path: Path, monkeypatch):
    """MCP `tool_resolve_fields(kind=...)` 种类纠错穿透，与 report 层同口径。"""
    from cosmic_kb.mcp import server as mcp_server

    db = make_kb(tmp_path)
    monkeypatch.setenv("COSMIC_KB_DB", str(db))
    got = mcp_server.tool_resolve_fields(["cqkd_entry"], kind="field")
    assert got["mismatched_kind"]["cqkd_entry"]["requested_kind"] == "field"


# ── kind="plugin"：插件类名反查绑定单据（真实断层：只有类名、无 form_key 时 bill 用不了）──

def test_resolve_plugin_exact_fqn_hits_plugin_table(conn):
    """① 精确全限定名命中 `plugin` 表：fixture 里 `CollateralOp` 同时在 plugin+binding 两表，
    plugin 表优先，带出 form_key/operation_key/operation_name/enabled。"""
    items = resolve_fields.resolve_fields(
        conn, ["cqspb.assets.CollateralOp"], kind="plugin")["resolved"]["cqspb.assets.CollateralOp"]
    assert items and len(items) == 1
    it = items[0]
    assert it["kind"] == "plugin"
    assert it["class_name"] == "cqspb.assets.CollateralOp"
    assert it["form_key"] == "cqkd_assetcard" and it["form_name"] == "资产卡片"
    assert it["operation_key"] == "audit" and it["operation_name"] == "审核"
    assert it["enabled"] is True
    assert "binding_status" not in it


def test_resolve_plugin_simple_name_falls_back(conn):
    """② 裸简单类名（无包名）：精确匹配落空后退化为按末段类名过滤，结果与全限定名一致。"""
    items = resolve_fields.resolve_fields(
        conn, ["CollateralOp"], kind="plugin")["resolved"]["CollateralOp"]
    assert items and len(items) == 1
    assert items[0]["class_name"] == "cqspb.assets.CollateralOp"
    assert items[0]["form_key"] == "cqkd_assetcard"


def test_resolve_plugin_binding_only_fallback(conn):
    """③ `plugin` 表零命中、只在 `binding` 表桥接上：`plugin_type`/`operation_key` 留空
    （不臆造未登记的元数据运行信息），`binding_status`/`confidence` 有值。"""
    conn.execute(
        "INSERT INTO binding(class_name,form_key,plugin_type,status,source_relpath,confidence,note) "
        "VALUES(?,?,?,?,?,?,?)",
        ("cqspb.assets.ConvertHook", "cqkd_assetcard", "convert", "linked_by_name",
         "cqspb/assets/ConvertHook.java", 0.8, ""),
    )
    conn.commit()
    items = resolve_fields.resolve_fields(
        conn, ["cqspb.assets.ConvertHook"], kind="plugin")["resolved"]["cqspb.assets.ConvertHook"]
    assert items and len(items) == 1
    it = items[0]
    assert it["form_key"] == "cqkd_assetcard"
    assert it["plugin_type"] is None and it["operation_key"] is None
    assert it["binding_status"] == "linked_by_name" and it["confidence"] == 0.8


def test_resolve_plugin_unbound_in_source(conn):
    """④ `plugin`/`binding` 两表都查不到，但 `source_class` 确认类存在且是插件子类：
    `resolved[key]` 仍是 `None`，`unbound_in_source[key]` 诚实标注源文件位置+插件基类。"""
    conn.execute(
        "INSERT INTO source_class(fqn,simple,package,relpath,module,is_orphan,orphan_role,plugin_base) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("cqspb.assets.OrphanValidator", "OrphanValidator", "cqspb.assets",
         "cqspb/assets/OrphanValidator.java", "cqkd_assets", 1, "plugin", "AbstractValidator"),
    )
    conn.commit()
    d = resolve_fields.resolve_fields(conn, ["cqspb.assets.OrphanValidator"], kind="plugin")
    assert d["resolved"]["cqspb.assets.OrphanValidator"] is None
    unb = d["unbound_in_source"]["cqspb.assets.OrphanValidator"]
    assert unb["relpath"] == "cqspb/assets/OrphanValidator.java"
    assert unb["plugin_base"] == "AbstractValidator"
    assert "hint" in unb and unb["hint"]


def test_resolve_plugin_completely_unknown_class(conn):
    """⑤ 类名连 `source_class` 里都没有（可能记错类名）：`resolved[key]` 为 `None`，
    不产出 `unbound_in_source` 噪声（与④区分：这里连类本身都钉不出）。"""
    d = resolve_fields.resolve_fields(conn, ["NoSuchPlugin"], kind="plugin")
    assert d["resolved"]["NoSuchPlugin"] is None
    assert "unbound_in_source" not in d


def test_render_resolve_plugin_and_unbound(conn):
    """⑥ `render_resolve_fields` 对 `kind="plugin"` 命中项与 `unbound_in_source` 都能正常渲染
    不报错，关键字段出现在输出文本里。"""
    conn.execute(
        "INSERT INTO source_class(fqn,simple,package,relpath,module,is_orphan,orphan_role,plugin_base) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("cqspb.assets.OrphanValidator", "OrphanValidator", "cqspb.assets",
         "cqspb/assets/OrphanValidator.java", "cqkd_assets", 1, "plugin", "AbstractValidator"),
    )
    conn.commit()
    d = resolve_fields.resolve_fields(
        conn, ["cqspb.assets.CollateralOp", "cqspb.assets.OrphanValidator"], kind="plugin")
    text = resolve_fields.render_resolve_fields(d)
    assert "cqspb.assets.CollateralOp" in text and "cqkd_assetcard" in text
    assert "审核" in text
    assert "cqspb.assets.OrphanValidator" in text and "AbstractValidator" in text


def test_cli_resolve_kind_plugin(tmp_path: Path, capsys):
    """CLI `resolve --kind plugin` 参数穿透。"""
    from cosmic_kb.cli.main import main

    db = make_kb(tmp_path)
    rc = main([
        "resolve", "cqspb.assets.CollateralOp", "--db", str(db), "--json", "--kind", "plugin",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    items = data["resolved"]["cqspb.assets.CollateralOp"]
    assert items[0]["form_key"] == "cqkd_assetcard"


def test_mcp_tool_resolve_fields_kind_plugin(tmp_path: Path, monkeypatch):
    """MCP `tool_resolve_fields(kind="plugin")` 与 report 层同口径。"""
    from cosmic_kb.mcp import server as mcp_server

    db = make_kb(tmp_path)
    monkeypatch.setenv("COSMIC_KB_DB", str(db))
    got = mcp_server.tool_resolve_fields(["cqspb.assets.CollateralOp"], kind="plugin")
    items = got["resolved"]["cqspb.assets.CollateralOp"]
    assert items[0]["form_key"] == "cqkd_assetcard" and items[0]["operation_key"] == "audit"
