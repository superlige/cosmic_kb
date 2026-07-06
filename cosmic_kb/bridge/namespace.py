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

# 苍穹插件基类清单（用户 2026-06-16 提供）。源码类只要继承链（传递闭包）里命中其一，
# 即「实现了插件」。用于把未被元数据绑定的孤儿里、真正的插件实现类单独区分出来
# （是死代码/元数据未给全的重要信号，区别于普通 service/util）。
_COSMIC_PLUGIN_BASES = frozenset({
    "AbstractFormPlugin", "AbstractMobFormPlugin",
    "AbstractBillPlugIn", "AbstractMobBillPlugIn",
    "AbstractBasePlugIn",
    "AbstractListPlugin", "AbstractTreeListPlugin", "StandardTreeListPlugin",
    "AbstractMobListPlugin",
    "AbstractOperationServicePlugIn", "AbstractConvertPlugIn", "AbstractWriteBackPlugIn",
    # 校验器：通过操作插件 onAddValidators 的 e.addValidator(new XxxValidator()) 在代码里挂载，
    # 不进元数据绑定 → 此前一律落成 orphan_role='unknown'、plugin_type='service'，bill/plugin
    # 看不到。它们承载"提交/审核报错"的关键校验逻辑（读单据字段 + addErrorMessage），是排障真凶，
    # 必须识别为插件（kind=validator），见 docs/参考手册/动作车道词表.md 附录 B。
    "AbstractValidator",
    "AbstractPrintServicePlugin", "AbstractPrintPlugin",
    "AbstractReportListDataPlugin", "AbstractReportTreeDataPlugin", "AbstractReportFormPlugin",
    "BatchImportPlugin",
    "AbstractBillWebApiPlugin",
    "AbstractTask",
    "IWorkflowPlugin",
})

# 类型声明关键字 + 标识符（在已剥噪声的文本上按花括号深度过滤顶层）。
_TYPE_DECL_RE = re.compile(
    r"\b(class|interface|enum|record|@interface)\s+([A-Za-z_$][\w$]*)"
)

# 父类型子句：extends / implements 后的类型名列表（在声明头到类体 `{` 之间抽取）。
_EXTENDS_RE = re.compile(r"\bextends\s+([\w.$<>,\s\[\]?&]+?)(?=\bimplements\b|$)")
_IMPLEMENTS_RE = re.compile(r"\bimplements\s+([\w.$<>,\s\[\]?&]+?)$")
# 去泛型：反复消最内层 <...>，直到稳定（泛型可嵌套）。
_GENERIC_RE = re.compile(r"<[^<>]*>")

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
    # 顶层类型名 → 其直接父类型简单名列表（extends + implements）。供插件基类传递闭包识别。
    type_supers: dict[str, list[str]] = field(default_factory=dict)


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


def _simple_name(ref: str) -> str | None:
    """把一个类型引用归一成简单名：去泛型/数组/通配，取末段（FQN→末类名）。"""
    ref = ref.strip()
    prev = None
    while prev != ref:  # 反复消最内层泛型 <...>
        prev = ref
        ref = _GENERIC_RE.sub("", ref)
    ref = ref.replace("[]", "").replace("?", "").strip()
    if not ref:
        return None
    last = ref.rsplit(".", 1)[-1].strip()
    return last if re.fullmatch(r"[A-Za-z_$][\w$]*", last) else None


def _supers_from_header(segment: str) -> list[str]:
    """从声明头（声明关键字到类体 `{` 之间）抽 extends/implements 的父类型简单名。"""
    supers: list[str] = []
    for rx in (_EXTENDS_RE, _IMPLEMENTS_RE):
        m = rx.search(segment)
        if not m:
            continue
        for part in m.group(1).split(","):
            simple = _simple_name(part)
            if simple:
                supers.append(simple)
    return supers


def _top_level_types(cleaned: str) -> list[tuple[str, list[str]]]:
    """在剥噪声后的文本上，按花括号深度收集 depth==0 处声明的 (类型名, 父类型简单名列表)。

    扫描时逐字符跟踪 `{}` 深度；遇到类型声明关键字且当前深度为 0 视为顶层。
    深度在遇到声明后、进入其类体前仍为 0，故用「声明出现时的深度」判定。
    父类型从声明头（声明起点到其类体首个 `{`）里抽 extends/implements。
    """
    import bisect

    out: list[tuple[str, list[str]]] = []
    # 预扫所有声明位置，再用花括号深度过滤——比逐字符状态机简洁且够准。
    decls = [(m.start(), m.end(), m.group(1), m.group(2)) for m in _TYPE_DECL_RE.finditer(cleaned)]
    if not decls:
        return out

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

    for start, end, _kw, name in decls:
        # idx 之前最后一次花括号事件后的深度即声明处深度；无事件则深度 0。
        j = bisect.bisect_left(brace_pos, start) - 1
        depth_before = depth_after[j] if j >= 0 else 0
        if depth_before == 0:
            body = cleaned.find("{", end)
            header = cleaned[end:body] if body != -1 else cleaned[end:end + 200]
            out.append((name, _supers_from_header(header)))
    return out


def _extract_unit(sf: "SourceFile") -> SourceUnit:
    """从单个 SourceFile 抽 package + 顶层类型，正则为主、tree-sitter 兜底。"""
    stem = _file_stem(sf.relpath)
    text = sf.text or ""

    pkg_match = _PACKAGE_RE.search(text)
    package = pkg_match.group(1) if pkg_match else None

    cleaned = _strip_noise(text)
    typed = _top_level_types(cleaned)  # [(名, 父类型简单名列表), ...]
    extracted_by = "regex"

    # 正则一无所获（极野生/编码后仍残缺）→ tree-sitter 兜底抽 package + 顶层类型。
    if not typed:
        ts_pkg, ts_types = _tree_sitter_extract(text)
        if ts_types:
            package = package or ts_pkg
            typed = ts_types
            extracted_by = "tree-sitter"

    # 仍无类型：退到「文件名即类名」（Java public 类约定），标低可信来源。
    if not typed:
        typed = [(stem, [])]
        extracted_by = "filename"

    types = [t for t, _ in typed]
    type_supers = {t: supers for t, supers in typed if supers}
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
        type_supers=type_supers,
    )


def _tree_sitter_extract(text: str) -> tuple[str | None, list[tuple[str, list[str]]]]:
    """tree-sitter 兜底：抽 package_declaration 与顶层类型声明名（含父类型简单名）。

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
    types: list[tuple[str, list[str]]] = []
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
                # superclass / interfaces 子句：从节点文本里抽简单名（复用正则口径）。
                supers: list[str] = []
                for field_name in ("superclass", "interfaces"):
                    fnode = child.child_by_field_name(field_name)
                    if fnode is not None:
                        supers.extend(_supers_from_header(
                            " " + fnode.text.decode("utf-8", "replace")
                        ))
                types.append((name_node.text.decode("utf-8", "replace"), supers))
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


def resolve_plugin_classes(index: SourceIndex) -> dict[str, str]:
    """识别「继承（传递闭包）了苍穹插件基类」的源码类型简单名 → 命中的基类名。

    很多项目有自己的中间基类（如 `XxxBasePlugin extends AbstractBillPlugIn`），故不能只看
    直接父类，要沿源码内 `extends/implements` 链向上找：只要祖先里出现任一苍穹插件基类，
    该类型即判为插件实现类。按**简单名**匹配（extends 子句多为非全限定名，与全工程「正则
    为主」口径一致；同名冲突概率低，命中即记录基类便于报告核对）。
    """
    # 全工程 简单名 → 直接父类型简单名集合（跨文件汇总）。
    supers: dict[str, set[str]] = {}
    for u in index.units:
        for simple, sup_list in u.type_supers.items():
            supers.setdefault(simple, set()).update(sup_list)

    resolved: dict[str, str] = {}     # 简单名 → 命中的苍穹基类

    def _base_of(simple: str, seen: set[str]) -> str | None:
        if simple in resolved:
            return resolved[simple]
        if simple in seen:           # 防环（A extends B、B extends A 之类的脏数据）
            return None
        seen.add(simple)
        for sup in supers.get(simple, ()):  # 直接父：基类本身命中
            if sup in _COSMIC_PLUGIN_BASES:
                resolved[simple] = sup
                return sup
        for sup in supers.get(simple, ()):  # 否则递归向上（经过项目中间基类）
            base = _base_of(sup, seen)
            if base is not None:
                resolved[simple] = base
                return base
        return None

    for simple in list(supers):
        _base_of(simple, set())
    return resolved


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
        # 转换规则的 key 是 snowflake Id，非表单标识，不参与元数据标识前缀统计。
        if m.form_type == "convert":
            continue
        key = m.key or ""
        if "_" in key:
            counter[key.split("_", 1)[0] + "_"] += 1
        else:
            counter["(none)"] += 1
    return dict(counter.most_common())
