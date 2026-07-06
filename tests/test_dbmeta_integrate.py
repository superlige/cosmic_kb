"""编排层（dbmeta/integrate.py::apply_vendor_metadata）验收测试。

用假 DbMetaReader（monkeypatch）避免真连库——只验证编排逻辑：
    ① 有匹配本地扩展 → 走 fmasterid 关联批量查（`read_models_via_local_ext_bulk`，按扩展
       自身精确 fnumber 查，不按候选 fnumber 猜）→ 合并 + 留别名行（别名指向母体真实 key）
    ② 无匹配本地扩展 → 走候选 fnumber 批量直查（`read_models_bulk`）→ 原厂模型原样并入
       （无插件）
    ③ 底层库查不到 → 跳过、如实提示，不中断整体 build
    ④ 不给 --vendor（空列表）→ 完全不碰 DB，原样返回
    ⑤ 本地扩展 key 被截断导致候选 fnumber 猜错也不受影响——因为压根不用候选 fnumber
       去查，只用它做本地扩展分组的 dict key（见 `test_apply_vendor_metadata_uses_ext_own_key_not_guessed_candidate`）
    ⑥ 批量查询：无论候选/扩展有多少个，`apply_vendor_metadata` 只应各调一次
       `read_models_bulk`/`read_models_via_local_ext_bulk`（不逐个循环查库，见
       `test_apply_vendor_metadata_calls_bulk_reader_methods_once_regardless_of_candidate_count`）
"""

from __future__ import annotations

import argparse

from cosmic_kb import dbmeta as dbmeta_pkg
from cosmic_kb.cli.main import _apply_vendor_metadata_cli
from cosmic_kb.dbmeta import integrate
from cosmic_kb.dbmeta.discover import VendorCandidate
from cosmic_kb.metadata.model import MetaEntity, MetaField, MetaModel, MetaPlugin


def _vendor_model(fnumber: str) -> MetaModel:
    header = MetaEntity("BaseEntity", fnumber, "原厂单", "h1", "header", None, "t_x")
    return MetaModel(
        key=fnumber, name="原厂单", model_type="BaseFormModel", form_type="basedata", isv=None,
        entities=[header],
        fields=[MetaField("TextField", "name", "名称", "fname", "f1", "h1", "platform", "header", fnumber)],
        plugins=[MetaPlugin(class_name="kd.bos.form.plugin.Foo", plugin_type="form", source="platform")],
        operations=[],
        source_file=f"db://{fnumber}",
    )


class _FakeReader:
    """假 DbMetaReader：
        `fetch_map` —— `read_models_bulk(fnumbers)` 用（无本地扩展命中，按候选 fnumber
                        批量直查）。
        `ext_map`   —— `read_models_via_local_ext_bulk(local_keys)` 用，键是本地扩展
                        **自身**的精确 key（模拟按 fmasterid 关联批量查到母体）。
    映射里没有的 key 不出现在批量返回值里（同真实 `DbMetaReader` 批量方法的"存在即查到"
    语义）。额外记录每个批量方法被调用的次数/参数，供测试断言"确实是批量查、不是循环"。
    """

    def __init__(self, fetch_map: dict[str, MetaModel] | None = None, ext_map: dict[str, MetaModel] | None = None):
        self._fetch_map = fetch_map or {}
        self._ext_map = ext_map or {}
        self.bulk_calls: list[tuple] = []
        self.ext_bulk_calls: list[tuple] = []

    def __call__(self, config):  # 替代 DbMetaReader(config) 构造调用
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read_models_bulk(self, fnumbers) -> dict[str, MetaModel]:
        keys = list(fnumbers)
        self.bulk_calls.append(tuple(keys))
        return {fn: self._fetch_map[fn] for fn in keys if fn in self._fetch_map}

    def read_models_via_local_ext_bulk(self, local_keys) -> dict[str, MetaModel]:
        keys = list(local_keys)
        self.ext_bulk_calls.append(tuple(keys))
        return {lk: self._ext_map[lk] for lk in keys if lk in self._ext_map}


def _extension_model() -> MetaModel:
    ext_header = MetaEntity("BaseEntity", "cqkd_bd_customer_ext", "扩展", "eh1", "header", None, None)
    return MetaModel(
        key="cqkd_bd_customer_ext", name=None, model_type=None, form_type="basedata", isv="cqkd",
        inherit_path=["root1"], entities=[ext_header],
        fields=[MetaField("TextField", "cqkd_x", "扩展字段", "fx", "f1", "eh1",
                          "entity", "header", "cqkd_bd_customer_ext")],
        plugins=[MetaPlugin(class_name="cqspb.CustomerExtPlugin", plugin_type="form", source="project")],
    )


def test_apply_vendor_metadata_merges_matching_extension(monkeypatch):
    """候选 fnumber（"bd_customer"）只用来在 fnumbers 列表里定位；实际查库改按扩展自身
    精确 key（"cqkd_bd_customer_ext"）走 `read_model_via_local_ext`——即便 fetch_map
    压根没有候选 fnumber 这个键，只要 ext_map 命中扩展自身 key，合并照样成功。"""
    ext = _extension_model()
    monkeypatch.setattr(
        integrate, "DbMetaReader",
        _FakeReader(ext_map={"cqkd_bd_customer_ext": _vendor_model("bd_customer")}),
    )

    models, notices = integrate.apply_vendor_metadata([ext], ["bd_customer"], config=object())

    keys = {m.key for m in models}
    assert keys == {"bd_customer", "cqkd_bd_customer_ext"}   # 合并模型 + 扩展别名，原扩展被替换
    merged = next(m for m in models if m.key == "bd_customer")
    assert merged.is_extension is False and merged.extends is None
    assert any(f.key == "cqkd_x" for f in merged.fields)      # 扩展字段并入
    assert any(f.key == "name" for f in merged.fields)        # 原厂字段仍在
    assert [p.class_name for p in merged.plugins] == ["cqspb.CustomerExtPlugin"]  # 只有扩展插件

    alias = next(m for m in models if m.key == "cqkd_bd_customer_ext")
    assert alias.is_extension is True and alias.extends == "bd_customer"
    assert alias.fields == [] and alias.plugins == []

    assert any("已并入 1 个本地扩展模型" in n for n in notices)
    assert any("母体真实标识='bd_customer'" in n for n in notices)


def test_apply_vendor_metadata_uses_ext_own_key_not_guessed_candidate(monkeypatch):
    """真实故障场景复现：本地扩展 key 因平台标识长度限制被截断，命名规律反推出的候选
    fnumber（"cas_bankjournalf"）是错的，真实原厂标识是"cas_bankjournalformrpt"。
    `fetch_map` 里故意只放"错误候选"会查不到的原厂 key（模拟直查会失败），
    `ext_map` 按扩展自身精确 key 放"真实母体"——验证走的是 ext_map 这条路径，
    合并结果的 key 是母体真实标识而非猜错的候选。"""
    ext_header = MetaEntity("BaseEntity", "cqkd_cas_bankjournalf_ext", "扩展", "eh1", "header", None, None)
    ext = MetaModel(
        key="cqkd_cas_bankjournalf_ext", name=None, model_type=None, form_type="bill", isv="cqkd",
        inherit_path=["root1"], entities=[ext_header], fields=[], plugins=[],
    )
    monkeypatch.setattr(
        integrate, "DbMetaReader",
        _FakeReader(
            fetch_map={},  # 猜错的候选 "cas_bankjournalf" 直查必查不到
            ext_map={"cqkd_cas_bankjournalf_ext": _vendor_model("cas_bankjournalformrpt")},
        ),
    )

    models, notices = integrate.apply_vendor_metadata([ext], ["cas_bankjournalf"], config=object())

    keys = {m.key for m in models}
    assert "cas_bankjournalformrpt" in keys      # 真实母体 key 并入，而非猜错的候选
    assert "cas_bankjournalf" not in keys
    assert any("母体真实标识='cas_bankjournalformrpt'" in n for n in notices)


def test_apply_vendor_metadata_no_ext_match_but_local_same_key_merges_not_duplicates(monkeypatch):
    """真实故障复现（2026-07-03）：手动 `--vendor` 指定的 fnumber 命中本地已有 key，但
    这个本地模型没有 `_ext` 命名/InheritPath，`detect_extension` 识别不出它是扩展，于是
    落进"无本地扩展命中"分支。旧代码在这里直接 `result.append(vendor)`，导致同一个 key
    在 `models` 里出现两份，建库后该单据下的字段被写入两遍（`trace` 返回两条一模一样的
    occurrence）。修复后应识别出本地同 key 模型、走 merge 并入而不是重复追加。"""
    local_header = MetaEntity("BillEntity", "sim_original_bill", "所有开票申请单", "h1", "header", None, "t_sim")
    local_model = MetaModel(
        key="sim_original_bill", name="所有开票申请单", model_type="BillFormModel",
        form_type="bill", isv="cqkd",
        inherit_path=None,  # 没有继承链 → detect_extension 判不出是扩展
        entities=[local_header],
        fields=[MetaField("TextField", "textfield1", "扩展字段1", "ftext1", "f1", "h1",
                          "entity", "header", "sim_original_bill")],
        plugins=[MetaPlugin(class_name="cqspb.InvoiceExtPlugin", plugin_type="form", source="project")],
    )
    monkeypatch.setattr(
        integrate, "DbMetaReader",
        _FakeReader({"sim_original_bill": _vendor_model("sim_original_bill")}),
    )

    models, notices = integrate.apply_vendor_metadata(
        [local_model], ["sim_original_bill"], config=object(),
    )

    matches = [m for m in models if m.key == "sim_original_bill"]
    assert len(matches) == 1   # 不能有两份同 key 模型
    merged = matches[0]
    text_fields = [f for f in merged.fields if f.key == "textfield1"]
    assert len(text_fields) == 1   # 本地字段也不能被重复计入
    assert any(f.key == "name" for f in merged.fields)   # 原厂字段仍并入
    assert [p.class_name for p in merged.plugins] == ["cqspb.InvoiceExtPlugin"]  # 本地插件被保留
    assert any("本地已存在 1 个同 key 模型" in n for n in notices)


def test_apply_vendor_metadata_same_vendor_discovered_via_two_candidates_merges_once(monkeypatch):
    """真实故障复现（2026-07-05）：本地扩展 key 因平台标识长度限制被截断
    （`cqkd_sim_original_bil_ext`），`detect_extension` 猜出的候选（信号①）也随之截断成
    "sim_original_bil"；但源码里 ORM 调用直接写的是完整未截断字面量
    "sim_original_bill"（信号②），三信号发现把这当成两个独立候选，`fnumbers` 里同时有
    "sim_original_bil"（走 exts 分支）和 "sim_original_bill"（走 plain 分支），两者
    fmasterid 关联/直查解出的真实母体 key 其实是同一个（"sim_original_bill"）。

    旧代码没有识别"两个候选、同一个真实母体"，plain 分支会把 exts 分支刚合并出的模型当
    成"本地已有同 key 模型"再合并一次——`merge_vendor_extension` 的实体去重只按对象身份
    跳过表头行，不按 key 去重分录/子分录，导致该单据的分录在最终 KB 里重复一份；同时
    vendor 自己的字段被当成"扩展字段"跟 vendor 自己再比一次，触发虚假的"字段 key 冲突"
    警告。修复后不管两个候选谁先被处理，最终都应只有一份模型、分录不重复、无虚假警告。"""
    def _vendor_with_entry() -> MetaModel:
        header = MetaEntity("BaseEntity", "sim_original_bill", "原始单", "h1", "header", None, "t_sim")
        entry = MetaEntity("BillEntity", "sim_original_bill_entry", "分录", "e1", "entry", "h1", "t_sim_entry")
        return MetaModel(
            key="sim_original_bill", name="原始单", model_type="BillFormModel",
            form_type="bill", isv=None,
            entities=[header, entry],
            fields=[
                MetaField("TextField", "name", "名称", "fname", "f1", "h1", "platform", "header", "sim_original_bill"),
                MetaField("TextField", "amt", "金额", "famt", "f2", "e1", "platform", "entry", "sim_original_bill"),
            ],
            plugins=[MetaPlugin(class_name="kd.bos.form.plugin.Foo", plugin_type="form", source="platform")],
            source_file="db://sim_original_bill",
        )

    def _fresh_ext() -> MetaModel:
        ext_header = MetaEntity("BillEntity", "cqkd_sim_original_bil_ext", "扩展", "eh1", "header", None, None)
        return MetaModel(
            key="cqkd_sim_original_bil_ext", name=None, model_type=None, form_type="bill", isv="cqkd",
            inherit_path=["root1"], entities=[ext_header],
            fields=[MetaField("TextField", "cqkd_x", "扩展字段", "fx", "fext1", "eh1",
                              "entity", "header", "cqkd_sim_original_bil_ext")],
            plugins=[MetaPlugin(class_name="cqspb.InvoiceExtPlugin", plugin_type="form", source="project")],
        )

    for order in (["sim_original_bil", "sim_original_bill"], ["sim_original_bill", "sim_original_bil"]):
        monkeypatch.setattr(
            integrate, "DbMetaReader",
            _FakeReader(
                fetch_map={"sim_original_bill": _vendor_with_entry()},
                ext_map={"cqkd_sim_original_bil_ext": _vendor_with_entry()},
            ),
        )

        models, notices = integrate.apply_vendor_metadata([_fresh_ext()], order, config=object())

        matches = [m for m in models if m.key == "sim_original_bill"]
        assert len(matches) == 1, order      # 不能有两份同 key 模型
        merged = matches[0]
        entry_entities = [e for e in merged.entities if e.key == "sim_original_bill_entry"]
        assert len(entry_entities) == 1, order   # 分录不能重复
        assert len(merged.fields) == 3, order    # name + amt + cqkd_x，各恰好一份
        assert merged.warnings == [], order      # 不应触发虚假的"字段 key 冲突"
        assert [p.class_name for p in merged.plugins] == ["cqspb.InvoiceExtPlugin"], order
        assert any("跳过重复合并" in n or "为同一实体" in n for n in notices), order


def test_apply_vendor_metadata_no_matching_extension_adds_reference_only(monkeypatch):
    monkeypatch.setattr(integrate, "DbMetaReader", _FakeReader({"bd_supplier": _vendor_model("bd_supplier")}))

    models, notices = integrate.apply_vendor_metadata([], ["bd_supplier"], config=object())

    assert len(models) == 1
    assert models[0].key == "bd_supplier"
    assert models[0].plugins == []   # 原厂插件已清空
    assert any("未匹配到本地扩展" in n for n in notices)


def test_apply_vendor_metadata_not_found_skips_and_notifies(monkeypatch):
    monkeypatch.setattr(integrate, "DbMetaReader", _FakeReader({}))

    models, notices = integrate.apply_vendor_metadata([], ["nope"], config=object())

    assert models == []
    assert any("未查到" in n for n in notices)


def test_apply_vendor_metadata_calls_bulk_reader_methods_once_regardless_of_candidate_count(monkeypatch):
    """批量查询验收：不管候选/扩展有多少个，只应各调一次批量方法——不是"每个候选一次
    循环查库"（这正是用户反馈"摄取执行速度很慢，是不是循环查库了"要修的问题）。"""
    ext_a = _extension_model()  # key=cqkd_bd_customer_ext → 候选 bd_customer
    ext_b_header = MetaEntity("BaseEntity", "cqkd_bd_period_ext", "扩展2", "eh2", "header", None, None)
    ext_b = MetaModel(
        key="cqkd_bd_period_ext", name=None, model_type=None, form_type="basedata", isv="cqkd",
        inherit_path=["root2"], entities=[ext_b_header], fields=[], plugins=[],
    )
    fake = _FakeReader(
        fetch_map={
            "bd_supplier": _vendor_model("bd_supplier"),
            "bd_material": _vendor_model("bd_material"),
            "bd_taxrate": _vendor_model("bd_taxrate"),
        },
        ext_map={
            "cqkd_bd_customer_ext": _vendor_model("bd_customer"),
            "cqkd_bd_period_ext": _vendor_model("bd_period"),
        },
    )
    monkeypatch.setattr(integrate, "DbMetaReader", fake)

    models, notices = integrate.apply_vendor_metadata(
        [ext_a, ext_b],
        ["bd_customer", "bd_period", "bd_supplier", "bd_material", "bd_taxrate"],
        config=object(),
    )

    assert len(fake.bulk_calls) == 1         # 3 个无扩展命中候选，只发一次批量直查
    assert set(fake.bulk_calls[0]) == {"bd_supplier", "bd_material", "bd_taxrate"}
    assert len(fake.ext_bulk_calls) == 1     # 2 个扩展命中候选，只发一次批量关联查
    assert set(fake.ext_bulk_calls[0]) == {"cqkd_bd_customer_ext", "cqkd_bd_period_ext"}

    keys = {m.key for m in models}
    assert {"bd_customer", "bd_period", "bd_supplier", "bd_material", "bd_taxrate"} <= keys
    assert len(notices) == 5


def test_apply_vendor_metadata_empty_fnumbers_is_noop():
    models_in = [_extension_model()]
    models_out, notices = integrate.apply_vendor_metadata(models_in, [], config=object())
    assert models_out is models_in
    assert notices == []


# ── CLI 编排层（cli/main.py::_apply_vendor_metadata_cli）：自动摄取（三信号）+ 手动 --vendor 并集 ──
#
# discover_candidates 本身的信号准确性在 test_dbmeta_discover.py 单测；这里只测编排逻辑
# （auto ∪ manual、无 --db-config 零改动、只给 --vendor 报错、无候选也无手动时不连库），
# 用假 discover_candidates + 假 DbMetaReader（沿用上面的 _FakeReader）隔离真实发现/连库。

def _fake_discover(candidates: list[VendorCandidate]):
    def _inner(*, models=None, scan_result=None, known_keys=None, isv_prefixes=None):
        return candidates
    return _inner


def test_apply_vendor_metadata_cli_auto_union_manual(monkeypatch, capsys):
    candidates = [
        VendorCandidate(key="bd_customer", ext_source="cqkd_bd_customer_ext"),
        VendorCandidate(key="bd_supplier", orm_hits=2, evidence=["A.java:10"]),
    ]
    monkeypatch.setattr(dbmeta_pkg, "discover_candidates", _fake_discover(candidates))
    monkeypatch.setattr(integrate, "DbMetaReader", _FakeReader({
        "bd_customer": _vendor_model("bd_customer"),
        "bd_supplier": _vendor_model("bd_supplier"),
    }))
    monkeypatch.setattr("cosmic_kb.dbmeta.config.load_config", lambda path: object())

    args = argparse.Namespace(vendor=["bd_customer"], db_config="fake.json")
    result = _apply_vendor_metadata_cli(args, [], object())

    assert isinstance(result, list)
    assert {m.key for m in result} == {"bd_customer", "bd_supplier"}
    err = capsys.readouterr().err
    assert "自动摄取" in err and "bd_supplier" in err
    assert "手动指定: bd_customer" in err


def test_apply_vendor_metadata_cli_auto_without_manual_vendor(monkeypatch, capsys):
    candidates = [VendorCandidate(key="bd_supplier", orm_hits=1)]
    monkeypatch.setattr(dbmeta_pkg, "discover_candidates", _fake_discover(candidates))
    monkeypatch.setattr(integrate, "DbMetaReader", _FakeReader({
        "bd_supplier": _vendor_model("bd_supplier"),
    }))
    monkeypatch.setattr("cosmic_kb.dbmeta.config.load_config", lambda path: object())

    args = argparse.Namespace(vendor=None, db_config="fake.json")
    result = _apply_vendor_metadata_cli(args, [], object())

    assert {m.key for m in result} == {"bd_supplier"}
    err = capsys.readouterr().err
    assert "自动摄取" in err
    assert "手动指定" not in err


def test_apply_vendor_metadata_cli_noop_without_vendor_or_db_config():
    models_in = [_extension_model()]
    args = argparse.Namespace(vendor=None, db_config=None)
    result = _apply_vendor_metadata_cli(args, models_in, object())
    assert result is models_in   # 两者都不给：纯 opt-in，完全不碰 DB


def test_apply_vendor_metadata_cli_vendor_without_db_config_errors(capsys):
    args = argparse.Namespace(vendor=["bd_customer"], db_config=None)
    result = _apply_vendor_metadata_cli(args, [], object())
    assert result == 2
    assert "需配合 --db-config" in capsys.readouterr().err


def test_apply_vendor_metadata_cli_db_config_no_candidates_and_no_vendor_is_noop(monkeypatch):
    # --db-config 给了，但三信号无命中且未手动 --vendor → fnumbers 为空，不该连库
    # （不 monkeypatch load_config：若误连库会因假路径 FileNotFoundError 而暴露）。
    monkeypatch.setattr(dbmeta_pkg, "discover_candidates", _fake_discover([]))
    models_in: list = []
    args = argparse.Namespace(vendor=None, db_config="fake.json")
    result = _apply_vendor_metadata_cli(args, models_in, object())
    assert result is models_in
