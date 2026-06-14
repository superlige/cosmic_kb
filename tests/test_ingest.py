"""阶段 1 验收测试 —— 摄取、解析、覆盖率报告、ingest 命令。

依赖 tree-sitter 的解析断言用 pytest.importorskip 守卫：未装 [parse] extra 时
只验证"不可用"路径如实降级，不让测试硬失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cosmic_kb.ingest import scanner
from cosmic_kb.java import parser as javaparser
from cosmic_kb.report import parse_coverage


# ── 摄取 / 编码 ──────────────────────────────────────────────────
def _write(p: Path, text: str, encoding: str = "utf-8") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode(encoding))


def test_scan_finds_java_excludes_build(tmp_path: Path):
    _write(tmp_path / "src" / "A.java", "class A {}")
    _write(tmp_path / "target" / "Ignore.java", "class Ignore {}")
    _write(tmp_path / "src" / "notes.txt", "hello")

    result = scanner.scan(tmp_path)
    rels = {f.relpath for f in result.files}
    assert rels == {"src/A.java"}
    assert "target" in result.skipped_dirs


def test_scan_missing_root_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        scanner.scan(tmp_path / "nope")


def test_detect_encoding_utf8_and_bom():
    assert scanner.detect_encoding(b"class A {}")[0] == "utf-8"
    assert scanner.detect_encoding(b"\xef\xbb\xbfclass A {}")[0] == "utf-8-sig"


def test_scan_reads_gbk_chinese(tmp_path: Path):
    """中文 GBK 文件应能被读出且不抛异常（编码探测兜底 gb18030）。"""
    raw = "class A { String s = \"资产卡片抵押状态\"; }".encode("gb18030")
    (tmp_path / "Z.java").write_bytes(raw)
    result = scanner.scan(tmp_path)
    assert len(result.files) == 1
    f = result.files[0]
    assert f.ok
    assert "资产卡片" in (f.text or "")


# ── 解析 ────────────────────────────────────────────────────────
def test_parse_skipped_when_no_text():
    assert javaparser.parse_source(None).status == "skipped"


def test_parse_clean_source():
    pytest.importorskip("tree_sitter_java")
    res = javaparser.parse_source("class A { void f() { int x = 1; } }")
    assert res.status == "ok"
    assert res.ok
    assert res.error_count == 0
    assert res.node_count > 0


def test_parse_broken_source_recovers():
    pytest.importorskip("tree_sitter_java")
    res = javaparser.parse_source("class A { void f( { int x = ; } }")
    assert res.status == "partial"
    assert not res.ok
    assert res.parsed
    assert res.error_count + res.missing_count > 0
    assert res.error_spans  # 有错误片段且带行号
    assert all(s.start_line >= 1 for s in res.error_spans)


# ── 覆盖率报告 ───────────────────────────────────────────────────
def test_coverage_report_counts(tmp_path: Path):
    _write(tmp_path / "Clean.java", "class Clean { void f() { int x = 1; } }")
    _write(tmp_path / "Broken.java", "class Broken { void f( { int x = ; } }")

    report = parse_coverage.analyze(str(tmp_path))
    assert report.total == 2
    assert len(report.read_ok) == 2
    assert "utf-8" in report.encoding_distribution

    text = parse_coverage.render(report)
    assert "解析覆盖率" in text
    assert "结论" in text

    d = report.to_dict()
    assert d["totals"]["files"] == 2
    assert len(d["files"]) == 2

    if javaparser.is_available():
        assert len(report.parsed_clean) == 1
        assert len(report.parsed_partial) == 1
        assert report.files_with_errors


def test_coverage_empty_dir(tmp_path: Path):
    report = parse_coverage.analyze(str(tmp_path))
    assert report.total == 0
    assert "未发现 .java" in parse_coverage.render(report)


# ── CLI ─────────────────────────────────────────────────────────
def test_cli_ingest_runs(tmp_path: Path, capsys):
    from cosmic_kb.cli import main as cli_main

    _write(tmp_path / "A.java", "class A {}")
    rc = cli_main.main(["ingest", str(tmp_path)])
    assert rc == 0
    assert "解析覆盖率" in capsys.readouterr().out


def test_cli_ingest_json(tmp_path: Path, capsys):
    import json

    from cosmic_kb.cli import main as cli_main

    _write(tmp_path / "A.java", "class A {}")
    cli_main.main(["ingest", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["files"] == 1


def test_cli_ingest_missing_path(capsys, tmp_path: Path):
    from cosmic_kb.cli import main as cli_main

    rc = cli_main.main(["ingest", str(tmp_path / "nope")])
    assert rc == 2
