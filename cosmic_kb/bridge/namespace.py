"""阶段 3 · 源码命名空间索引 + 前缀自动发现。

桥接的源码侧：把阶段 1 摄取到的每个 `.java` 抽出 `package` 与顶层类型名，拼出
**全限定名（FQN）**，建成索引 —— 这是 `linker.py` 拿元数据 `<ClassName>` 来精确
定位源码文件的查找表。

抽取策略（用户 2026-06-16 拍板：**正则为主、tree-sitter 兜底**）：
    1. 正则抽 `package`（行首 `package x.y.z;`）。
    2. 顶层类型：先剥注释/字符串噪声，再按花括号深度扫描，只收 depth==0 处声明的
       `class/interface/enum/record`，避免把内部类、注释里的伪声明误当顶层。
    3. 正则一无所获（极野生的文件）→ 回退 java/parser.py 的 tree-sitter 兜底。

为什么不一上来就 tree-sitter：桥接只需 package + 顶层类名，正则够快够稳，成百上千
文件不必每个都走 AST（那是阶段 5 的活）。

前缀两套、分别建（见 CLAUDE.md「已拍板关键决策」，绝不混）：
    - **代码包前缀**（如 `cqspb`）：Java 包路径首段，管模块归属。
    - **元数据标识前缀**（如 `cqkd_`）：表单/字段标识前缀，管实体归属。
两者由工具从数据里自动发现并统计，仅作报告产物，不作桥接定位依据（ISV 不可靠）。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from ..ingest.scanner import ScanResult, SourceFile
    from ..metadata.model import MetaModel

# 行首 package 声明：`package com.foo.bar;`（允许前导空白与注解极少见，按主流写法）。
_PACKAGE_RE = re.compile(r"^\s*package\s+([A-Za-z_$][\w.$]*)\s*;", re.MULTILINE)

# 类型声明关键字 + 标识符（在已剥噪声的文本上按花括号深度过滤顶层）。
_TYPE_DECL_RE = re.compile(
    r"\b(class|interface|enum|record|@interface)\s+([A-Za-z_$][\w$]*)"
)

# 注释 / 字符串 / 字符字面量：剥掉以免里头的 `class X` 之类被误当声明。
_NOISE_RE = re.compile(
    r"""
      //[^\n]*               # 行注释
    | /\*.*?\*/              # 块注释（含 javadoc）
    | "(?:\\.|[^"\\])*"      # 双引号字符串
    | '(?:\\.|[^'\\])'       # 字符字面量
    """,
    re.DOTALL | re.VERBOSE,
)


@dataclass
class SourceUnit:
    """一个源码文件抽出的命名空间信息。

    primary_fqn 取「package + 文件主类名」：Java 规定 public 类名必须等于文件名，
    苍穹插件类都是 public，故这是最可靠的桥接目标键。all_fqns 收齐文件内所有顶层
    类型（含 package-private 的次类），供孤儿统计与歧义兜底。
    """

    relpath: str                  # 相对源码根的 POSIX 路径（来自 SourceFile.relpath）
    package: str | None           # 包名；缺 package 声明为 None（默认包）
    primary_type: str             # 文件主类型名（取文件名 stem）
    primary_fqn: str              # 主 FQN = package.primary_type（无包则裸类名）
    all_types: list[str] = field(default_factory=list)  # 文件内全部顶层类型名
    all_fqns: list[str] = field(default_factory=list)   # 对应 FQN 列表
    extracted_by: str = "regex"   # regex | tree-sitter | filename，记录可信度来源


@dataclass
class SourceIndex:
    """全项目源码命名空间索引。"""

    units: list[SourceUnit] = field(default_factory=list)
    by_fqn: dict[str, list[SourceUnit]] = field(default_factory=dict)
    by_simple: dict[str, list[SourceUnit]] = field(default_factory=dict)

    def lookup_fqn(self, fqn: str) -> list[SourceUnit]:
        return self.by_fqn.get(fqn, [])

    def lookup_simple(self, simple: str) -> list[SourceUnit]:
        return self.by_simple.get(simple, [])


def _strip_noise(text: str) -> str:
    """把注释/字符串/字符字面量替换成等长占位，保住花括号位置不偏。"""
    def _repl(m: re.Match[str]) -> str:
        # 用空格替换、保留换行，使行号与花括号深度都不受影响。
        return "".join("\n" if ch == "\n" else " " for ch in m.group(0))

    return _NOISE_RE.sub(_repl, text)


def _file_stem(relpath: str) -> str:
    name = relpath.replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-5] if name.lower().endswith(".java") else name


def _top_level_types(cleaned: str) -> list[str]:
    """在剥噪声后的文本上，按花括号深度收集 depth==0 处声明的类型名。

    扫描时逐字符跟踪 `{}` 深度；遇到类型声明关键字且当前深度为 0 视为顶层。
    深度在遇到声明后、进入其类体前仍为 0，故用「声明出现时的深度」判定。
    """
    import bisect

    names: list[str] = []
    # 预扫所有声明位置，再用花括号深度过滤——比逐字符状态机简洁且够准。
    decls = [(m.start(), m.group(1), m.group(2)) for m in _TYPE_DECL_RE.finditer(cleaned)]
    if not decls:
        return names

    # 预计算每个花括号事件的位置与「该事件之后」的深度，供二分查询。
    brace_pos: list[int] = []
    depth_after: list[int] = []
    depth = 0
    for i, ch in enumerate(cleaned):
        if ch == "{":
            depth += 1
            brace_pos.append(i)
            depth_after.append(depth)
        elif ch == "}":
            depth -= 1
            brace_pos.append(i)
            depth_after.append(depth)

    for start, _kw, name in decls:
        # idx 之前最后一次花括号事件后的深度即声明处深度；无事件则深度 0。
        j = bisect.bisect_left(brace_pos, start) - 1
        depth_before = depth_after[j] if j >= 0 else 0
        if depth_before == 0:
            names.append(name)
    return names


def _extract_unit(sf: "SourceFile") -> SourceUnit:
    """从单个 SourceFile 抽 package + 顶层类型，正则为主、tree-sitter 兜底。"""
    stem = _file_stem(sf.relpath)
    text = sf.text or ""

    pkg_match = _PACKAGE_RE.search(text)
    package = pkg_match.group(1) if pkg_match else None

    cleaned = _strip_noise(text)
    types = _top_level_types(cleaned)
    extracted_by = "regex"

    # 正则一无所获（极野生/编码后仍残缺）→ tree-sitter 兜底抽 package + 顶层类型。
    if not types:
        ts_pkg, ts_types = _tree_sitter_extract(text)
        if ts_types:
            package = package or ts_pkg
            types = ts_types
            extracted_by = "tree-sitter"

    # 仍无类型：退到「文件名即类名」（Java public 类约定），标低可信来源。
    if not types:
        types = [stem]
        extracted_by = "filename"

    # 主类型：优先与文件名一致的那个（public 类约定）；否则取第一个声明。
    primary = stem if stem in types else types[0]

    def _fqn(simple: str) -> str:
        return f"{package}.{simple}" if package else simple

    all_fqns = [_fqn(t) for t in types]
    return SourceUnit(
        relpath=sf.relpath,
        package=package,
        primary_type=primary,
        primary_fqn=_fqn(primary),
        all_types=types,
        all_fqns=all_fqns,
        extracted_by=extracted_by,
    )


def _tree_sitter_extract(text: str) -> tuple[str | None, list[str]]:
    """tree-sitter 兜底：抽 package_declaration 与顶层类型声明名。

    未装 [parse] extra 时返回空，由调用方退到文件名兜底。
    """
    try:
        from ..java.parser import get_parser
    except Exception:
        return None, []
    parser = get_parser()
    if parser is None or not text:
        return None, []
    try:
        tree = parser.parse(text.encode("utf-8"))
    except Exception:
        return None, []

    root = tree.root_node
    package: str | None = None
    types: list[str] = []
    _DECL = {
        "class_declaration", "interface_declaration",
        "enum_declaration", "record_declaration",
        "annotation_type_declaration",
    }
    for child in root.children:  # 顶层：只看 root 的直接子节点
        if child.type == "package_declaration":
            for n in child.children:
                if n.type in ("scoped_identifier", "identifier"):
                    package = n.text.decode("utf-8", "replace")
        elif child.type in _DECL:
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                types.append(name_node.text.decode("utf-8", "replace"))
    return package, types


def build_index(scan_result: "ScanResult") -> SourceIndex:
    """从阶段 1 的 ScanResult 建源码命名空间索引（只用读成功的文件）。"""
    index = SourceIndex()
    for sf in scan_result.ok_files:
        unit = _extract_unit(sf)
        index.units.append(unit)
        for fqn in unit.all_fqns:
            index.by_fqn.setdefault(fqn, []).append(unit)
        for simple in unit.all_types:
            index.by_simple.setdefault(simple, []).append(unit)
    return index


# ── 前缀自动发现（两套，分别建，仅作报告产物）──────────────────────

def discover_code_prefixes(index: SourceIndex) -> dict[str, int]:
    """代码包前缀：取每个源码包路径首段，计数。无包归 '(default)'。"""
    counter: Counter[str] = Counter()
    for unit in index.units:
        head = unit.package.split(".")[0] if unit.package else "(default)"
        counter[head] += 1
    return dict(counter.most_common())


def discover_meta_prefixes(models: Iterable["MetaModel"]) -> dict[str, int]:
    """元数据标识前缀：取表单 key 的 `xxx_` 前缀（首个下划线前），计数。

    如 cqkd_assetcard → cqkd_。无下划线的 key 归 '(none)'。
    """
    counter: Counter[str] = Counter()
    for m in models:
        key = m.key or ""
        if "_" in key:
            counter[key.split("_", 1)[0] + "_"] += 1
        else:
            counter["(none)"] += 1
    return dict(counter.most_common())
