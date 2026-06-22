"""多项目支持·方案A：KB 路径解析（_resolve_db / _discover_db）优先级测试。

覆盖六级优先级：--db > COSMIC_KB_DB > 建库随源码根 > 向上发现 > 重建兜底源码根 > cwd 兜底。
"""

from __future__ import annotations

import argparse
import os

from cosmic_kb.cli.main import DEFAULT_DB, _discover_db, _resolve_db


def _ns(**kw) -> argparse.Namespace:
    kw.setdefault("db", None)
    kw.setdefault("creating", False)
    kw.setdefault("source_root", None)
    return argparse.Namespace(**kw)


def test_explicit_db_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("COSMIC_KB_DB", str(tmp_path / "env.db"))
    args = _ns(db=str(tmp_path / "explicit.db"), creating=True, source_root=str(tmp_path))
    assert _resolve_db(args) == str(tmp_path / "explicit.db")


def test_env_over_discover_and_source(monkeypatch, tmp_path):
    monkeypatch.setenv("COSMIC_KB_DB", str(tmp_path / "env.db"))
    args = _ns(creating=True, source_root=str(tmp_path / "src"))
    assert _resolve_db(args) == str(tmp_path / "env.db")


def test_build_colocates_with_source_root(monkeypatch, tmp_path):
    monkeypatch.delenv("COSMIC_KB_DB", raising=False)
    src = tmp_path / "proj" / "src"
    src.mkdir(parents=True)
    args = _ns(creating=True, source_root=str(src))
    assert _resolve_db(args) == str(src / DEFAULT_DB)


def test_read_discovers_upward(monkeypatch, tmp_path):
    monkeypatch.delenv("COSMIC_KB_DB", raising=False)
    proj = tmp_path / "proj"
    sub = proj / "a" / "b"
    sub.mkdir(parents=True)
    kb = proj / DEFAULT_DB
    kb.write_text("x", encoding="utf-8")  # 仅需文件存在
    monkeypatch.chdir(sub)
    args = _ns()  # 读类命令，无 --db / env / source_root
    assert _resolve_db(args) == str(kb)
    assert _discover_db(sub) == kb


def test_read_rebuild_fallback_to_source_root(monkeypatch, tmp_path):
    """读类命令未发现 KB，但给了 --source-root（临时重建）→ 落源码根。"""
    monkeypatch.delenv("COSMIC_KB_DB", raising=False)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    src = tmp_path / "proj" / "src"
    src.mkdir(parents=True)
    args = _ns(source_root=str(src))  # 非 creating，但有 source_root
    assert _resolve_db(args) == str(src / DEFAULT_DB)


def test_final_fallback_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("COSMIC_KB_DB", raising=False)
    cwd = tmp_path / "empty"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    args = _ns()  # 啥都没有
    assert _resolve_db(args) == DEFAULT_DB
