"""编排层（dbmeta/sync.py）验收测试——build --db-config 自动全量同步本项目二开元数据。

用假 DbMetaReader（monkeypatch，同 test_dbmeta_integrate.py 的 `_FakeReader` 风格）
避免真连库，只验证编排逻辑：
    ① resolve_isv：显式给的直接采信不查库；候选唯一自动用；候选 >1 靠本地前缀消歧；
       仍歧义/零候选一律 IsvAmbiguousError，绝不静默猜一个。
    ② sync_own_isv_metadata：同 key 整条替换（不是 merge_vendor_extension 的父子并集
       语义，旧字段不残留）；新 key 直接 append；转换规则同一套替换逻辑；变更查询
       固定传 since_ts=None（每次全量，2026-07-05 修复：增量只抓变更会让未变更的
       自家实体在 build 幂等重建时缺席，见 dbmeta/sync.py 模块docstring）；
       server_now_iso 必须先于变更查询调用（水位时序正确性）；库里查不到任何内容时
       notice 如实提示。
"""

from __future__ import annotations

import pytest

from cosmic_kb.dbmeta import sync
from cosmic_kb.metadata.model import MetaModel


def _model(key: str, name: str) -> MetaModel:
    return MetaModel(key=key, name=name, model_type="BillFormModel", isv="cqkd", form_type="bill")


class _FakeSyncReader:
    """假 DbMetaReader：记录调用顺序（`calls`），按预置映射返回结果，不连真库。"""

    def __init__(
        self,
        *,
        isv_counts: dict[str, int] | None = None,
        form_entity_map: dict[str, MetaModel] | None = None,
        convert_map: dict[str, MetaModel] | None = None,
        server_now: str = "2026-07-05T12:00:00",
    ) -> None:
        self._isv_counts = isv_counts or {}
        self._form_entity_map = form_entity_map or {}
        self._convert_map = convert_map or {}
        self._server_now = server_now
        self.calls: list[tuple] = []

    def __call__(self, config):  # 替代 DbMetaReader(config) 构造调用
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_isv_form_counts(self) -> dict[str, int]:
        self.calls.append(("list_isv_form_counts",))
        return dict(self._isv_counts)

    def server_now_iso(self) -> str:
        self.calls.append(("server_now_iso",))
        return self._server_now

    def list_changed_form_and_entity_keys(self, isv, since_ts):
        self.calls.append(("list_changed_form_and_entity_keys", isv, since_ts))
        return list(self._form_entity_map)

    def read_models_bulk(self, keys):
        self.calls.append(("read_models_bulk", tuple(keys)))
        return {k: self._form_entity_map[k] for k in keys if k in self._form_entity_map}

    def list_changed_convert_rule_ids(self, isv, since_ts):
        self.calls.append(("list_changed_convert_rule_ids", isv, since_ts))
        return list(self._convert_map)

    def read_convert_rules_bulk(self, fids):
        self.calls.append(("read_convert_rules_bulk", tuple(fids)))
        return {k: self._convert_map[k] for k in fids if k in self._convert_map}


# ── 1. resolve_isv ───────────────────────────────────────────────────────
def test_resolve_isv_explicit_wins_no_db_query():
    class ExplodingReader:
        def list_isv_form_counts(self):
            raise AssertionError("不该查库——explicit 给了应直接采信")

    assert sync.resolve_isv(ExplodingReader(), explicit="cqkd", local_prefixes=()) == "cqkd"


def test_resolve_isv_auto_when_single_candidate_after_excluding_kingdee():
    r = _FakeSyncReader(isv_counts={"kingdee": 500, "cqkd": 340})
    assert sync.resolve_isv(r, explicit=None, local_prefixes=()) == "cqkd"


def test_resolve_isv_disambiguates_via_local_prefix_match():
    r = _FakeSyncReader(isv_counts={"kingdee": 500, "cqkd": 340, "ysq": 12})
    assert sync.resolve_isv(r, explicit=None, local_prefixes=["cqkd_"]) == "cqkd"


def test_resolve_isv_ambiguous_even_with_local_material_when_multiple_prefixes_match():
    r = _FakeSyncReader(isv_counts={"kingdee": 500, "cqkd": 340, "ysq": 12})
    with pytest.raises(sync.IsvAmbiguousError) as exc:
        sync.resolve_isv(r, explicit=None, local_prefixes=["cqkd_", "ysq_"])
    assert "cqkd" in str(exc.value) and "ysq" in str(exc.value)


def test_resolve_isv_raises_ambiguous_error_with_candidates_and_counts_when_no_local_material():
    r = _FakeSyncReader(isv_counts={"kingdee": 500, "cqkd": 340, "ysq": 12})
    with pytest.raises(sync.IsvAmbiguousError) as exc:
        sync.resolve_isv(r, explicit=None, local_prefixes=())
    msg = str(exc.value)
    assert "cqkd" in msg and "340" in msg
    assert "ysq" in msg and "12" in msg
    assert exc.value.candidates == [("cqkd", 340), ("ysq", 12)]  # 按表单数降序


def test_resolve_isv_raises_when_zero_candidates_after_excluding_kingdee():
    r = _FakeSyncReader(isv_counts={"kingdee": 500})
    with pytest.raises(sync.IsvAmbiguousError) as exc:
        sync.resolve_isv(r, explicit=None, local_prefixes=())
    assert exc.value.candidates == []
    assert "kingdee" in str(exc.value)


# ── 2. sync_own_isv_metadata ─────────────────────────────────────────────
def test_sync_replaces_existing_key_not_merge_additively(monkeypatch):
    """回归锁：整条替换而非 merge_vendor_extension 的父子并集——旧模型独有的字段
    不能在新模型里残留。"""
    old = _model("cqkd_a", "旧版")
    new = _model("cqkd_a", "新版")
    fake = _FakeSyncReader(isv_counts={"cqkd": 1}, form_entity_map={"cqkd_a": new})
    monkeypatch.setattr(sync, "DbMetaReader", fake)

    result = sync.sync_own_isv_metadata([old], config=object(), isv="cqkd")

    assert len(result.models) == 1
    assert result.models[0] is new
    assert result.models[0].name == "新版"


def test_sync_appends_brand_new_key_not_previously_local(monkeypatch):
    keep = _model("cqkd_b", "已有")
    brand_new = _model("cqkd_c", "新建")
    fake = _FakeSyncReader(isv_counts={"cqkd": 1}, form_entity_map={"cqkd_c": brand_new})
    monkeypatch.setattr(sync, "DbMetaReader", fake)

    result = sync.sync_own_isv_metadata([keep], config=object(), isv="cqkd")

    assert {m.key for m in result.models} == {"cqkd_b", "cqkd_c"}


def test_sync_covers_convert_rule_replace_by_fid_key(monkeypatch):
    old_rule = MetaModel(key="fid1", name="旧规则", model_type="ConvertRuleModel",
                          isv="cqkd", form_type="convert")
    new_rule = MetaModel(key="fid1", name="新规则", model_type="ConvertRuleModel",
                          isv="cqkd", form_type="convert")
    fake = _FakeSyncReader(isv_counts={"cqkd": 1}, convert_map={"fid1": new_rule})
    monkeypatch.setattr(sync, "DbMetaReader", fake)

    result = sync.sync_own_isv_metadata([old_rule], config=object(), isv="cqkd")

    assert len(result.models) == 1
    assert result.models[0].name == "新规则"


def test_sync_always_queries_full_isv_key_set_with_since_ts_none(monkeypatch):
    """回归锁（2026-07-05 修复）：不管上一次同步是什么时候，变更查询固定传 since_ts=None
    （该 isv 下全量）——否则未变更的自家实体这一轮会在 models 里缺席，build 幂等重建时
    要么被 vendor 兜底误判成"原厂"，要么直接从 KB 消失（真实翻车：cqkd_ht 等 132 个）。"""
    fake = _FakeSyncReader(isv_counts={"cqkd": 1})
    monkeypatch.setattr(sync, "DbMetaReader", fake)

    sync.sync_own_isv_metadata([], config=object(), isv="cqkd")

    fe_call = next(c for c in fake.calls if c[0] == "list_changed_form_and_entity_keys")
    cr_call = next(c for c in fake.calls if c[0] == "list_changed_convert_rule_ids")
    assert fe_call == ("list_changed_form_and_entity_keys", "cqkd", None)
    assert cr_call == ("list_changed_convert_rule_ids", "cqkd", None)


def test_sync_calls_server_now_before_change_queries(monkeypatch):
    """回归锁：水位必须在变更查询之前拿到，否则同步过程中新提交的变更会被永久漏掉。"""
    fake = _FakeSyncReader(isv_counts={"cqkd": 1})
    monkeypatch.setattr(sync, "DbMetaReader", fake)

    sync.sync_own_isv_metadata([], config=object(), isv="cqkd")

    kinds = [c[0] for c in fake.calls]
    assert kinds.index("server_now_iso") < kinds.index("list_changed_form_and_entity_keys")
    assert kinds.index("server_now_iso") < kinds.index("list_changed_convert_rule_ids")


def test_sync_zero_hits_still_returns_sync_ts_and_notice(monkeypatch):
    fake = _FakeSyncReader(isv_counts={"cqkd": 1}, server_now="2026-07-05T12:00:00")
    monkeypatch.setattr(sync, "DbMetaReader", fake)

    result = sync.sync_own_isv_metadata([], config=object(), isv="cqkd")

    assert result.sync_ts == "2026-07-05T12:00:00"
    assert result.models == []
    assert any("底层库未查到任何" in n for n in result.notices)


def test_sync_resolves_isv_when_not_explicit(monkeypatch):
    fake = _FakeSyncReader(isv_counts={"kingdee": 500, "cqkd": 340})
    monkeypatch.setattr(sync, "DbMetaReader", fake)

    result = sync.sync_own_isv_metadata([], config=object(), isv=None)

    assert result.isv == "cqkd"
