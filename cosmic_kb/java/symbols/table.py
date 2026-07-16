"""阶段 12.1 · 符号表 —— JVM 微工具产出的调用点解析结果的 Python 侧承接结构。

主键 ``(relpath, line, name)`` 与 tree-sitter 侧 ``Invocation`` 对齐（12.2 注入管线时
六个消费点都按这三元组查）；同行同名多调用（如 ``a.save(); b.save();`` 挤一行）用
**字符列 col** 消歧——对不齐时走"唯一性兜底 → 否则 None"的诚实路径，绝不猜。

resolution 三态（处处置信度）：
    expr    表达式级 .resolve() 直接成功 —— 最高置信
    scope   表达式失败、退化到 scope 类型 + 名字/参数个数唯一命中 —— 仍是确定性类型绑定
    failed  两层都解不出（reason: unsolved-symbol | ambiguous | generic-inference |
            parse-error | io-error | internal）—— 12.2 起退回 tree-sitter 名字启发式
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

# 视为"解析成功"的 resolution 值（coverage 分子）
_RESOLVED = ("expr", "scope")


@dataclass(frozen=True)
class SymbolSite:
    """一个调用点的符号解析结果（对应协议 v1 的 site 对象）。"""

    relpath: str
    line: int                    # 1-based，方法名标识符起点
    col: int                     # 1-based **字符**列（协议约定，非字节列）
    name: str                    # 方法简单名
    kind: str                    # invocation | method_reference
    resolution: str              # expr | scope | failed
    declaring: str | None = None  # 目标方法声明类 FQN（解析成功时必有）
    signature: str | None = None  # 完整签名（含参数类型）
    static: bool | None = None
    target_kind: str | None = None  # project | jar | jdk
    argc: int | None = None      # 实参个数；method_reference 无实参 → None
    reason: str | None = None    # 失败原因（resolution=failed 时必有）

    @property
    def resolved(self) -> bool:
        return self.resolution in _RESOLVED


@dataclass
class FileSymbols:
    """单文件的解析状态。status: ok | parse-error | io-error。"""

    relpath: str
    status: str
    note: str | None = None
    sites: list[SymbolSite] = field(default_factory=list)


class SymbolTable:
    """(relpath, line, name) → 调用点符号结果 的查询索引。

    由 runner 流式喂 ``file`` 事件构建（JSONL 每收一行即入表，进程中途死掉
    已收部分仍完整可用——流式即恢复边界）。
    """

    def __init__(self) -> None:
        self.files: dict[str, FileSymbols] = {}
        self.warnings: list[str] = []
        self.summary: dict | None = None
        self._index: dict[tuple[str, int, str], list[SymbolSite]] = {}

    # -- 构建 --

    def add_file_event(self, event: dict) -> None:
        """收一条协议 v1 的 file 事件。字段缺失宽容处理（微工具单点字段异常不拖垮整表）。"""
        relpath = event.get("relpath") or ""
        fs = FileSymbols(relpath=relpath, status=event.get("status") or "ok",
                         note=event.get("note"))
        for raw in event.get("sites") or []:
            try:
                site = SymbolSite(
                    relpath=relpath,
                    line=int(raw["line"]),
                    col=int(raw["col"]),
                    name=str(raw["name"]),
                    kind=str(raw.get("kind") or "invocation"),
                    resolution=str(raw.get("resolution") or "failed"),
                    declaring=raw.get("declaring"),
                    signature=raw.get("signature"),
                    static=raw.get("static"),
                    target_kind=raw.get("target_kind"),
                    argc=raw.get("argc"),
                    reason=raw.get("reason"),
                )
            except (KeyError, TypeError, ValueError):
                self.warnings.append(f"file 事件里有畸形 site，跳过（{relpath}）")
                continue
            fs.sites.append(site)
            self._index.setdefault((relpath, site.line, site.name), []).append(site)
        self.files[relpath] = fs

    # -- 查询 --

    def lookup(self, relpath: str, line: int, name: str,
               col: int | None = None) -> SymbolSite | None:
        """按 (relpath, line, name) 查调用点；同行同名多条时用 col 消歧。

        诚实路径：唯一 → 直接给；多条且 col 精确命中 → 给；多条但 col 未给/对不上
        → None（宁可退回名字启发式也不冒认）。
        """
        sites = self._index.get((relpath, line, name))
        if not sites:
            return None
        if len(sites) == 1:
            return sites[0]
        if col is not None:
            for site in sites:
                if site.col == col:
                    return site
        return None

    def sites_in_file(self, relpath: str) -> list[SymbolSite]:
        fs = self.files.get(relpath)
        return list(fs.sites) if fs else []

    def iter_sites(self) -> Iterator[SymbolSite]:
        for fs in self.files.values():
            yield from fs.sites

    # -- 统计（信任优先：覆盖率是一等产物）--

    def stats(self) -> dict:
        """返回覆盖率统计：文件数/调用点数/成功数/覆盖率/按 resolution 与失败 reason 分桶。"""
        by_resolution: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        total = 0
        resolved = 0
        for site in self.iter_sites():
            total += 1
            by_resolution[site.resolution] = by_resolution.get(site.resolution, 0) + 1
            if site.resolved:
                resolved += 1
            elif site.reason:
                by_reason[site.reason] = by_reason.get(site.reason, 0) + 1
        return {
            "files": len(self.files),
            "files_failed": sum(1 for f in self.files.values() if f.status != "ok"),
            "sites": total,
            "resolved": resolved,
            "coverage": round(resolved / total, 4) if total else 0.0,
            "by_resolution": by_resolution,
            "by_failure_reason": by_reason,
        }
