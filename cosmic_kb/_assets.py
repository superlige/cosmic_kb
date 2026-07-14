"""资产定位 —— 苍穹语义文档（references/rules）、继承根模板。

阶段 0 的"资产复用"在这里集中收口；**分发改造后（docs/设计方案/分发与多agent接入方案.md §4）**，
运行期资产一律走 `importlib.resources`，不再靠"项目根 = 本包上一级目录"的同级目录假设。
这样工具被 `pip install`（非 `-e` 可编辑安装）进 site-packages 后仍能读到随包数据，
是"自包含 MCP 包 / uvx / .mcpb"能消费的前提。

随包数据（package-data，见 pyproject.toml）：
    cosmic_kb/semantics/references/   苍穹插件 + SDK 语义文档（cosmic_semantics 工具回传源）
    cosmic_kb/semantics/rules/        反模式 / 幻觉名黑名单
    cosmic_kb/metadata/templates/     继承根模板（bos_billtpl / bos_basetpl，操作 oid 回填用）
    cosmic_kb/skills/*/SKILL.md        CodeBuddy / Qoder / TRAE 通用工作流 Skill
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
try:  # 3.11+ 在此；3.10 回退到 importlib.abc
    from importlib.resources.abc import Traversable
except ImportError:  # pragma: no cover
    from importlib.abc import Traversable
from pathlib import Path
from typing import Iterator

# cosmic_kb/_assets.py -> cosmic_kb -> <project_root>。
# 仅供**源码树/开发态**定位测试数据（samples/）用，**不可**作运行期资产路径——
# 装进 site-packages 后这个 parents[1] 指向无关目录。运行期资产走下方 importlib.resources。
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ── 运行期资产：importlib.resources 定位（随 wheel 走，不依赖同级目录布局）──────
def references_root() -> Traversable:
    """苍穹语义 references 目录（base/plugin/ 插件类型 + base/sdk/ 原生 SDK）。"""
    return files("cosmic_kb.semantics") / "references"


def rules_root() -> Traversable:
    """苍穹反模式 rules 目录。"""
    return files("cosmic_kb.semantics") / "rules"


def templates_root() -> Traversable:
    """继承根模板目录（bos_billtpl / bos_basetpl，供 template_loader 操作 oid 回填）。"""
    return files("cosmic_kb.metadata") / "templates"


def _walk_md(root: Traversable, prefix: str = "") -> Iterator[tuple[str, Traversable]]:
    """递归遍历 Traversable 下的所有 .md，产出 (相对路径不含扩展名, 文件 Traversable)。"""
    try:
        entries = list(root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return
    for entry in sorted(entries, key=lambda e: e.name):
        rel = f"{prefix}{entry.name}"
        if entry.is_dir():
            yield from _walk_md(entry, prefix=f"{rel}/")
        elif entry.name.endswith(".md"):
            yield rel[: -len(".md")], entry


def iter_reference_topics() -> Iterator[tuple[str, Traversable]]:
    """遍历所有语义主题（references + rules），产出 (相对路径不含扩展名, 文件)。

    相对路径首段即分组（base / rules…），供 cosmic_semantics 按组列清单。
    """
    yield from _walk_md(references_root(), prefix="")
    yield from _walk_md(rules_root(), prefix="rules/")


def read_topic(topic: str) -> str | None:
    """按主题名取一篇语义文档全文。

    匹配策略（宁缺毋滥，但容忍调用方只给文件名）：先按相对路径精确命中，再按**文件名 stem**
    精确命中（如 `plugin-base` / `anti-patterns`），最后按子串。命不中返回 None，由调用方列清单。
    """
    if not topic:
        return None
    key = topic.strip().removesuffix(".md")
    items = list(iter_reference_topics())
    # 1) 相对路径精确  2) 文件名 stem 精确  3) 子串兜底
    for matcher in (
        lambda rel: rel == key,
        lambda rel: rel.rsplit("/", 1)[-1] == key,
        lambda rel: key in rel,
    ):
        for rel, trav in items:
            if matcher(rel):
                return trav.read_text(encoding="utf-8")
    return None


@dataclass(frozen=True)
class AssetStatus:
    """单个资产的存在性检查结果（随包数据缺失即视为安装损坏）。"""

    name: str
    present: bool
    detail: str

    @property
    def label(self) -> str:
        return "OK" if self.present else "MISSING"


def _traversable_ok(trav: Traversable) -> bool:
    try:
        return trav.is_dir()
    except (FileNotFoundError, NotADirectoryError):
        return False


def check_assets() -> list[AssetStatus]:
    """返回所有关键资产的存在性检查结果，供 `cosmic_kb doctor` 使用。"""

    ref_n = sum(1 for _ in iter_reference_topics())
    tpl_ok = _traversable_ok(templates_root())

    return [
        AssetStatus("semantics", _traversable_ok(references_root()),
                    f"references+rules 共 {ref_n} 篇语义文档（随包）"),
        AssetStatus("templates", tpl_ok,
                    "继承根模板 bos_billtpl/bos_basetpl（随包，操作 oid 回填用）"),
    ]
