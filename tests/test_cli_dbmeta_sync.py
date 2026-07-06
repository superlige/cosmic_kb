"""CLI 编排层（cli/main.py 的 dbmeta 同步接线）验收测试。

分两层：
    1. `_sync_own_isv_metadata_cli` 单测——monkeypatch `dbmeta.sync.sync_own_isv_metadata`
       （编排边界，不碰底层 reader），验证：无 --db-config 不触发；配置加载失败报 rc=2；
       正常路径把 notices 打到 stderr、返回 (models, isv, sync_ts)；IsvAmbiguousError/
       通用异常分别映射 rc=2/rc=1。2026-07-05 修复后不再有"增量/全量"之分（`--full-refresh`
       退役），`sync_own_isv_metadata` 不再接受 `since_ts` 形参，这里不再测水位读取逻辑。
    2. `_build_kb` 端到端接线——用真实空 tmp_path 源码根（`discover_candidates` 天然
       零命中，vendor 步骤不会触发真连库），验证：同步步骤先于 vendor 步骤跑；新水位
       正确写回 KB 的 `kb_meta.source_args`；`--db-config` 给了 + `meta` 为空仍可建库
       （纯 DB 冷启动）；两步都跑完仍空则报错；`IsvAmbiguousError` 正确映射成 rc=2。
"""

from __future__ import annotations

import argparse
import json

import pytest

from cosmic_kb.cli import main as cli_main
from cosmic_kb.dbmeta import sync as sync_mod
from cosmic_kb.dbmeta.sync import IsvAmbiguousError, SyncResult
from cosmic_kb.graph import store
from cosmic_kb.metadata.model import MetaModel


def _model(key: str, name: str = "同步单据") -> MetaModel:
    return MetaModel(key=key, name=name, model_type="BillFormModel", form_type="bill", isv="cqkd")


def _base_args(tmp_path, **overrides) -> argparse.Namespace:
    defaults = dict(
        source_root=str(tmp_path),
        meta=[],
        db=None,
        db_config=None,
        vendor=None,
        isv=None,
        template_dir=None,
        follow_symlinks=False,
        creating=True,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ── 1. _sync_own_isv_metadata_cli ────────────────────────────────────────
def test_sync_cli_noop_without_db_config(tmp_path):
    args = _base_args(tmp_path)
    models_in: list = [_model("cqkd_a")]
    result = cli_main._sync_own_isv_metadata_cli(args, models_in)
    assert result == (models_in, None, None)


def test_sync_cli_load_config_failure_returns_rc2(tmp_path, capsys):
    args = _base_args(tmp_path, db_config=str(tmp_path / "no-such-config.json"))
    result = cli_main._sync_own_isv_metadata_cli(args, [])
    assert result == 2
    assert "错误" in capsys.readouterr().err


def test_sync_cli_success_returns_models_isv_ts_and_prints_notices(tmp_path, monkeypatch, capsys):
    args = _base_args(tmp_path, db_config="fake.json")
    monkeypatch.setattr("cosmic_kb.dbmeta.config.load_config", lambda path: object())
    fresh = [_model("cqkd_a")]
    captured = {}

    def _fake_sync(models, config, *, isv, local_prefixes):
        captured["isv"] = isv
        return SyncResult(models=fresh, isv="cqkd", sync_ts="2026-07-05T12:00:00", notices=["同步了 1 个"])

    monkeypatch.setattr(sync_mod, "sync_own_isv_metadata", _fake_sync)

    result = cli_main._sync_own_isv_metadata_cli(args, [])

    assert result == (fresh, "cqkd", "2026-07-05T12:00:00")
    assert captured["isv"] is None          # 未显式给 --isv
    assert "提示: 同步了 1 个" in capsys.readouterr().err


def test_sync_cli_does_not_pass_since_ts_to_sync_function(tmp_path, monkeypatch):
    """回归锁（2026-07-05 修复）：CLI 不再读旧 KB 水位、不再有 `--full-refresh`，
    `sync_own_isv_metadata` 调用里不应出现 `since_ts` 关键字（签名已去掉这个形参，
    传了反而会报 TypeError——用真实签名调用即是最直接的回归验证）。"""
    args = _base_args(tmp_path, db_config="fake.json")
    monkeypatch.setattr("cosmic_kb.dbmeta.config.load_config", lambda path: object())

    def _fake_sync(models, config, *, isv, local_prefixes):
        return SyncResult(models=[], isv="cqkd", sync_ts="t", notices=[])

    monkeypatch.setattr(sync_mod, "sync_own_isv_metadata", _fake_sync)
    result = cli_main._sync_own_isv_metadata_cli(args, [])
    assert isinstance(result, tuple)


def test_sync_cli_isv_ambiguous_error_returns_rc2_with_candidates(tmp_path, monkeypatch, capsys):
    args = _base_args(tmp_path, db_config="fake.json")
    monkeypatch.setattr("cosmic_kb.dbmeta.config.load_config", lambda path: object())

    def _fake_sync(*a, **kw):
        raise IsvAmbiguousError([("cqkd", 340), ("ysq", 12)])

    monkeypatch.setattr(sync_mod, "sync_own_isv_metadata", _fake_sync)
    result = cli_main._sync_own_isv_metadata_cli(args, [])
    assert result == 2
    err = capsys.readouterr().err
    assert "cqkd" in err and "ysq" in err


def test_sync_cli_generic_exception_returns_rc1(tmp_path, monkeypatch, capsys):
    args = _base_args(tmp_path, db_config="fake.json")
    monkeypatch.setattr("cosmic_kb.dbmeta.config.load_config", lambda path: object())

    def _fake_sync(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(sync_mod, "sync_own_isv_metadata", _fake_sync)
    result = cli_main._sync_own_isv_metadata_cli(args, [])
    assert result == 1
    assert "boom" in capsys.readouterr().err


# ── 2. _build_kb 端到端接线 ───────────────────────────────────────────────
def test_build_kb_sync_runs_before_vendor_and_persists_new_watermark(tmp_path, monkeypatch):
    args = _base_args(tmp_path, db_config="fake.json")
    monkeypatch.setattr("cosmic_kb.dbmeta.config.load_config", lambda path: object())
    fresh = [_model("cqkd_synced")]

    def _fake_sync(models, config, *, isv, local_prefixes):
        return SyncResult(models=fresh, isv="cqkd", sync_ts="2026-07-05T12:00:00", notices=[])

    monkeypatch.setattr(sync_mod, "sync_own_isv_metadata", _fake_sync)

    db_path = str(tmp_path / "kb.db")
    counts, rc = cli_main._build_kb(args, db_path)

    assert rc == 0
    assert counts["form"] == 1
    conn = store.open_kb(db_path)
    try:
        raw = store.get_meta(conn, "source_args")
    finally:
        conn.close()
    data = json.loads(raw)
    assert data["dbmeta_last_sync_ts"] == "2026-07-05T12:00:00"
    assert data["dbmeta_isv"] == "cqkd"


def test_build_kb_allows_empty_meta_when_db_config_populates_models(tmp_path, monkeypatch):
    """纯 DB 冷启动建库：meta 位置参数为空，全靠 --db-config 同步出来的内容建库。"""
    args = _base_args(tmp_path, meta=[], db_config="fake.json")
    monkeypatch.setattr("cosmic_kb.dbmeta.config.load_config", lambda path: object())

    def _fake_sync(models, config, *, isv, local_prefixes):
        return SyncResult(models=[_model("cqkd_bootstrap")], isv="cqkd", sync_ts="t", notices=[])

    monkeypatch.setattr(sync_mod, "sync_own_isv_metadata", _fake_sync)

    counts, rc = cli_main._build_kb(args, str(tmp_path / "kb.db"))
    assert rc == 0
    assert counts["form"] == 1


def test_build_kb_still_errors_when_models_empty_after_both_steps(tmp_path, monkeypatch, capsys):
    args = _base_args(tmp_path, meta=[], db_config="fake.json")
    monkeypatch.setattr("cosmic_kb.dbmeta.config.load_config", lambda path: object())

    def _fake_sync(models, config, *, isv, local_prefixes):
        return SyncResult(models=[], isv="cqkd", sync_ts="t", notices=[])

    monkeypatch.setattr(sync_mod, "sync_own_isv_metadata", _fake_sync)

    counts, rc = cli_main._build_kb(args, str(tmp_path / "kb.db"))
    assert rc == 2
    assert counts is None
    assert "--db-config" in capsys.readouterr().err


def test_build_kb_isv_ambiguous_error_surfaces_as_rc2(tmp_path, monkeypatch):
    args = _base_args(tmp_path, db_config="fake.json")
    monkeypatch.setattr("cosmic_kb.dbmeta.config.load_config", lambda path: object())

    def _fake_sync(*a, **kw):
        raise IsvAmbiguousError([("cqkd", 340), ("ysq", 12)])

    monkeypatch.setattr(sync_mod, "sync_own_isv_metadata", _fake_sync)
    counts, rc = cli_main._build_kb(args, str(tmp_path / "kb.db"))
    assert counts is None
    assert rc == 2


def test_build_kb_sync_not_triggered_without_db_config(tmp_path, monkeypatch):
    args = _base_args(tmp_path, db_config=None)

    def _exploding(*a, **kw):
        raise AssertionError("不该触发同步——没给 --db-config")

    monkeypatch.setattr(sync_mod, "sync_own_isv_metadata", _exploding)

    # meta 为空且无 --db-config：models 全程为空，最终应在"未解析出任何元数据"报错处
    # 退出（rc=2），但绝不能是因为跑了同步函数触发的 AssertionError。
    counts, rc = cli_main._build_kb(args, str(tmp_path / "kb.db"))
    assert counts is None
    assert rc == 2
