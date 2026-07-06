"""阶段 2.2 · 整包双层 zip 解析。

整包结构（见 docs/核心/阶段验收.md「整包 zip（双层）结构勘探结论」）：

    外层 zip
     ├─ kdpkgs.xml                        部署清单（isv/product/app/版本）
     └─ dm/*.zip                          内层 zip（type=dm）
           └─ datamodel/<版本>/main/<appKey>/metadata/
                 ├─ *.dym                 表单元数据（单包约 758 个）
                 └─ *.{zh_CN,...}.dymx    多语言资源（本阶段作可选补充，暂不取）

规模大（758 量级，见硬约束「规模大」）→ 逐个解析、带进度回调、单个失败不拖垮整包。
本阶段产出"全部表单清单 + 每表单计数"，满足验收第 4 条（列出全部表单）。
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import dym_io
from .dym_parser import parse_element
from .model import MetaModel
from .template_loader import TemplateRegistry

# 内层 metadata 目录下的元数据：dym（表单）+ cr（转换规则）。
# 排除多语言资源 .dymx / .crx（本阶段不取）。
_META_SUFFIXES = (".dym", ".cr")


@dataclass
class PackageEntry:
    """整包内单个 dym 的解析结果（成功为 model，失败为 error）。"""

    member: str               # 在内层 zip 中的成员路径
    app_key: str | None       # 从路径 metadata/ 上一级目录推断的 appKey（模块线索）
    model: MetaModel | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.model is not None


@dataclass
class PackageResult:
    """整包解析汇总。"""

    source: Path
    entries: list[PackageEntry] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)  # kdpkgs.xml 摘要

    @property
    def ok_entries(self) -> list[PackageEntry]:
        return [e for e in self.entries if e.ok]

    @property
    def failed_entries(self) -> list[PackageEntry]:
        return [e for e in self.entries if not e.ok]


@dataclass
class MultiPackageResult:
    """多个整包的解析汇总（生产项目：一个 zip ≈ 一个业务模块，常多包并存）。"""

    packages: list[PackageResult] = field(default_factory=list)

    @property
    def total_forms(self) -> int:
        return sum(len(p.entries) for p in self.packages)

    @property
    def ok_count(self) -> int:
        return sum(len(p.ok_entries) for p in self.packages)

    @property
    def failed_count(self) -> int:
        return sum(len(p.failed_entries) for p in self.packages)


def _parse_manifest(raw: bytes) -> dict[str, Any]:
    """从 kdpkgs.xml 抽取部署清单摘要（isv/product/app/版本）。容错，不抛。"""
    try:
        root = dym_io.parse_bytes(raw)
    except Exception:
        return {}
    info: dict[str, Any] = {}
    for tag in ("isv", "Isv", "productNumber", "ProductNumber"):
        val = root.findtext(f".//{tag}")
        if val:
            info.setdefault("isv" if tag.lower() == "isv" else "product", val)
    # 应用清单：收集所有 appNumber/bizappId 之类线索（不同导出标签可能不一）。
    apps = [e.text for e in root.iter() if e.tag.lower() in ("appnumber", "bizappnumber") and e.text]
    if apps:
        info["apps"] = sorted(set(apps))
    return info


def _app_key_from_member(member: str) -> str | None:
    """从 `datamodel/<版本>/main/<appKey>/metadata/x.dym` 推断 appKey。"""
    parts = member.replace("\\", "/").split("/")
    if "metadata" in parts:
        i = parts.index("metadata")
        if i >= 1:
            return parts[i - 1]
    return None


def _is_meta_member(name: str) -> bool:
    return name.lower().endswith(_META_SUFFIXES)


def _iter_inner_zips(outer: zipfile.ZipFile):
    """产出外层 zip 里 dm/ 下的内层 zip 字节。"""
    for name in outer.namelist():
        low = name.lower()
        if low.endswith(".zip") and ("/dm/" in low or low.startswith("dm/")):
            yield name, outer.read(name)


def load_package(
    path: str | Path,
    *,
    template_registry: TemplateRegistry | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    limit: int | None = None,
) -> PackageResult:
    """解析整包双层 zip。

    progress(done, total, member)：可选进度回调（758 量级时供 CLI 打点）。
    limit：仅解析前 N 个 dym（调试/抽样用），None 为全量。
    """
    p = Path(path)
    result = PackageResult(source=p)
    registry = template_registry or TemplateRegistry()

    with zipfile.ZipFile(p) as outer:
        # 1) 部署清单。
        for name in outer.namelist():
            if name.lower().endswith("kdpkgs.xml"):
                result.manifest = _parse_manifest(outer.read(name))
                break

        # 2) 收集内层所有元数据成员（dym 表单 + cr 转换规则），先列清单便于报总数/进度。
        pending: list[tuple[bytes, str]] = []  # (元数据字节, 成员路径)
        for _inner_name, inner_bytes in _iter_inner_zips(outer):
            with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner:
                for member in inner.namelist():
                    if _is_meta_member(member):
                        pending.append((inner.read(member), member))

        # 2b) 单层 zip 兜底：无双层 dm 结构（如单独导出的 metadata/ 再打包，见 samples/trans）
        # 时，直接扫外层 zip 自身的 dym/cr 成员，避免整包零产出。
        if not pending:
            for member in outer.namelist():
                if _is_meta_member(member):
                    pending.append((outer.read(member), member))

        total = len(pending) if limit is None else min(limit, len(pending))
        for idx, (dym_bytes, member) in enumerate(pending):
            if limit is not None and idx >= limit:
                break
            app_key = _app_key_from_member(member)
            entry = PackageEntry(member=member, app_key=app_key)
            try:
                root = dym_io.parse_bytes(dym_bytes)
                entry.model = parse_element(
                    root, template_registry=registry, source_file=member
                )
                # 回填 appKey —— 它是阶段 4 模块识别的主锚，须随 model 一起流转，
                # 不能在 _collect_models 扁平化（丢弃 PackageEntry）时丢失。
                entry.model.app_key = app_key
            except Exception as exc:  # 单个 dym 出错不拖垮整包
                entry.error = f"{type(exc).__name__}: {exc}"
            result.entries.append(entry)
            if progress:
                progress(idx + 1, total, member)

    return result


def discover_zips(path: str | Path, *, recursive: bool = False) -> list[Path]:
    """把输入路径解析成 zip 清单：目录→其下 .zip（默认不递归，按名排序）；文件→自身。"""
    p = Path(path)
    if p.is_dir():
        pattern = "**/*.zip" if recursive else "*.zip"
        return sorted(z for z in p.glob(pattern) if z.is_file())
    return [p]


def load_packages(
    paths: list[str | Path],
    *,
    template_registry: TemplateRegistry | None = None,
    progress: Callable[[str, int, int, str], None] | None = None,
    limit: int | None = None,
) -> MultiPackageResult:
    """逐个解析多个整包 zip，汇总成 MultiPackageResult。

    单包失败不拖垮其它包：单个 zip 打不开时记一条 PackageResult（无 entries）跳过。
    progress(pkg_name, done, total, member)：比单包多带一个包名，供 CLI 区分进度。
    """
    registry = template_registry or TemplateRegistry()
    multi = MultiPackageResult()
    for path in paths:
        name = Path(path).name

        def _pkg_progress(done: int, total: int, member: str, _name: str = name) -> None:
            if progress:
                progress(_name, done, total, member)

        try:
            res = load_package(
                path,
                template_registry=registry,
                progress=_pkg_progress if progress else None,
                limit=limit,
            )
        except Exception as exc:  # 整个 zip 打不开（损坏/非 zip）也不拖垮其它包
            res = PackageResult(source=Path(path))
            res.manifest = {"error": f"{type(exc).__name__}: {exc}"}
        multi.packages.append(res)
    return multi
