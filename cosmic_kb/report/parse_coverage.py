"""阶段 1 · 解析可信度 / 覆盖率报告。

这是**第一个信任门槛**（见 docs/开发计划.md 硬约束「信任优先」）：用户先看这份
报告——扫了多少文件、读取/解析成功率、编码分布、哪些文件有错误片段——再决定
信不信这个工具、给不给更多代码。所以报告必须**如实**：宁可标 unknown/低置信，
也不掩盖失败。

本模块把摄取层（ingest.scanner）与解析层（java.parser）串起来，产出结构化报告，
并提供人类可读的文本渲染与机器可读的 dict（供 --json / 后续入库）。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ingest import scanner
from ..ingest.scanner import ScanResult, SourceFile
from ..java import parser as javaparser
from ..java.parser import JavaParseResult

# 低于此置信度的编码判定，在报告里单独列出供人工复核。
LOW_CONFIDENCE_THRESHOLD = 0.5


@dataclass
class FileCoverage:
    """单文件的摄取 + 解析合并视图。"""

    relpath: str
    size: int
    encoding: str | None
    confidence: float
    read_ok: bool
    read_error: str | None
    parse: JavaParseResult


@dataclass
class CoverageReport:
    """一次扫描的覆盖率/可信度汇总。"""

    root: Path
    files: list[FileCoverage] = field(default_factory=list)
    skipped_dirs: list[str] = field(default_factory=list)
    skipped_symlinks: list[str] = field(default_factory=list)
    parser_available: bool = True
    parser_note: str | None = None

    # ── 派生统计 ──────────────────────────────────────────────
    @property
    def total(self) -> int:
        return len(self.files)

    @property
    def read_ok(self) -> list[FileCoverage]:
        return [f for f in self.files if f.read_ok]

    @property
    def read_failed(self) -> list[FileCoverage]:
        return [f for f in self.files if not f.read_ok]

    @property
    def parsed_clean(self) -> list[FileCoverage]:
        return [f for f in self.files if f.parse.status == "ok"]

    @property
    def parsed_partial(self) -> list[FileCoverage]:
        return [f for f in self.files if f.parse.status == "partial"]

    @property
    def files_with_errors(self) -> list[FileCoverage]:
        return [f for f in self.files if f.parse.error_spans or f.parse.status == "partial"]

    @property
    def low_confidence(self) -> list[FileCoverage]:
        return [
            f for f in self.files
            if f.read_ok and f.confidence < LOW_CONFIDENCE_THRESHOLD
        ]

    @property
    def encoding_distribution(self) -> dict[str, int]:
        c: Counter[str] = Counter()
        for f in self.read_ok:
            c[f.encoding or "unknown"] += 1
        return dict(c.most_common())

    @property
    def total_lines(self) -> int:
        return sum(f.parse.line_count for f in self.files)

    def to_dict(self) -> dict[str, Any]:
        """机器可读快照（供 --json 与后续入库）。"""
        return {
            "root": str(self.root),
            "parser_available": self.parser_available,
            "parser_note": self.parser_note,
            "totals": {
                "files": self.total,
                "read_ok": len(self.read_ok),
                "read_failed": len(self.read_failed),
                "parsed_clean": len(self.parsed_clean),
                "parsed_partial": len(self.parsed_partial),
                "lines": self.total_lines,
            },
            "encoding_distribution": self.encoding_distribution,
            "skipped_dirs": self.skipped_dirs,
            "skipped_symlinks": self.skipped_symlinks,
            "files": [
                {
                    "relpath": f.relpath,
                    "size": f.size,
                    "encoding": f.encoding,
                    "confidence": round(f.confidence, 3),
                    "read_ok": f.read_ok,
                    "read_error": f.read_error,
                    "parse_status": f.parse.status,
                    "error_count": f.parse.error_count,
                    "missing_count": f.parse.missing_count,
                    "error_spans": [str(s) for s in f.parse.error_spans],
                    "error_spans_truncated": f.parse.truncated_spans,
                }
                for f in self.files
            ],
        }


# ── 构建 ────────────────────────────────────────────────────────
def build_from_scan(scan_result: ScanResult) -> CoverageReport:
    """对一份扫描结果逐文件解析，汇总成覆盖率报告。"""
    available = javaparser.is_available()
    report = CoverageReport(
        root=scan_result.root,
        skipped_dirs=list(scan_result.skipped_dirs),
        skipped_symlinks=list(scan_result.skipped_symlinks),
        parser_available=available,
        parser_note=None if available else (javaparser.load_error() or "tree-sitter 未安装"),
    )
    for sf in scan_result.files:
        parse = javaparser.parse_source(sf.text)
        report.files.append(_to_coverage(sf, parse))
    return report


def analyze(root: str, **scan_kwargs: Any) -> CoverageReport:
    """便捷入口：扫描 root 并产出覆盖率报告。"""
    scan_result = scanner.scan(root, **scan_kwargs)
    return build_from_scan(scan_result)


def _to_coverage(sf: SourceFile, parse: JavaParseResult) -> FileCoverage:
    return FileCoverage(
        relpath=sf.relpath,
        size=sf.size,
        encoding=sf.encoding,
        confidence=sf.confidence,
        read_ok=sf.ok,
        read_error=sf.error,
        parse=parse,
    )


# ── 文本渲染 ─────────────────────────────────────────────────────
def _pct(part: int, whole: int) -> str:
    if whole == 0:
        return "—"
    return f"{part / whole * 100:.1f}%"


def render(report: CoverageReport, *, max_error_files: int = 30) -> str:
    """渲染人类可读的覆盖率报告。"""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("解析覆盖率 / 可信度报告")
    lines.append("=" * 60)
    lines.append(f"项目根     : {report.root}")
    if not report.parser_available:
        lines.append(f"⚠ 解析未启用: {report.parser_note}")
        lines.append("  → 仅做摄取统计；安装解析依赖: pip install -e .[parse]")
    lines.append("")

    total = report.total
    lines.append(f"扫描文件   : {total}")
    lines.append(
        f"读取成功   : {len(report.read_ok)} ({_pct(len(report.read_ok), total)})"
        f"   读取失败: {len(report.read_failed)}"
    )
    if report.parser_available:
        lines.append(
            f"解析干净   : {len(report.parsed_clean)} ({_pct(len(report.parsed_clean), total)})"
            f"   含错误片段: {len(report.parsed_partial)}"
        )
    lines.append(f"代码行数   : {report.total_lines}")
    lines.append("")

    # 编码分布
    lines.append("编码分布:")
    dist = report.encoding_distribution
    if dist:
        for enc, n in dist.items():
            lines.append(f"  {enc:<14}{n}")
    else:
        lines.append("  （无）")
    lines.append("")

    # 低置信编码
    low = report.low_confidence
    if low:
        lines.append(f"⚠ 低置信编码（<{LOW_CONFIDENCE_THRESHOLD}）{len(low)} 个，建议人工核对乱码:")
        for f in low[:max_error_files]:
            lines.append(f"  {f.confidence:.2f}  {f.encoding:<10}{f.relpath}")
        if len(low) > max_error_files:
            lines.append(f"  …… 另有 {len(low) - max_error_files} 个")
        lines.append("")

    # 读取失败
    if report.read_failed:
        lines.append(f"✗ 读取失败 {len(report.read_failed)} 个:")
        for f in report.read_failed[:max_error_files]:
            lines.append(f"  {f.relpath}  ({f.read_error})")
        lines.append("")

    # 含错误片段
    if report.parser_available:
        errs = report.parsed_partial
        if errs:
            lines.append(f"△ 含解析错误片段 {len(errs)} 个（tree-sitter 已局部恢复，行号对照源码）:")
            for f in errs[:max_error_files]:
                spans = ", ".join(str(s) for s in f.parse.error_spans)
                if f.parse.truncated_spans:
                    spans += ", …"
                lines.append(
                    f"  {f.relpath}  [E{f.parse.error_count}/M{f.parse.missing_count}]  {spans}"
                )
            if len(errs) > max_error_files:
                lines.append(f"  …… 另有 {len(errs) - max_error_files} 个")
            lines.append("")

    # 跳过目录
    if report.skipped_dirs:
        shown = ", ".join(report.skipped_dirs[:10])
        more = f" …(+{len(report.skipped_dirs) - 10})" if len(report.skipped_dirs) > 10 else ""
        lines.append(f"跳过目录   : {len(report.skipped_dirs)} ({shown}{more})")

    # 信任结论
    lines.append("")
    lines.append("-" * 60)
    if total == 0:
        lines.append("结论: 未发现 .java 文件，请确认项目路径。")
    elif not report.parser_available:
        lines.append("结论: 已完成摄取；装上 [parse] extra 后可评估解析可信度。")
    else:
        clean_rate = len(report.parsed_clean) / total
        if clean_rate >= 0.95:
            lines.append(f"结论: 解析干净率 {_pct(len(report.parsed_clean), total)} —— 可信度高，可放心给全量代码。")
        elif clean_rate >= 0.8:
            lines.append(f"结论: 解析干净率 {_pct(len(report.parsed_clean), total)} —— 整体可信，错误片段多为局部，可继续。")
        else:
            lines.append(f"结论: 解析干净率偏低 {_pct(len(report.parsed_clean), total)} —— 请先排查上方错误/编码问题。")
    return "\n".join(lines)
