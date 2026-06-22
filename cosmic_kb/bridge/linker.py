"""阶段 3 · 元数据 ↔ 代码桥接器（全项目枢纽）。

把元数据每个插件的 `<ClassName>` 全限定名定位到源码树里真实的 `.java`，并对绑定
做分类。唯一定位键是 ClassName 全限定名（已拍板：**不靠 ISV/前缀猜路径**，见
CLAUDE.md「已拍板关键决策」）。

绑定状态（status）——处处置信度、解不出标 unknown，绝不臆造：
    linked          project 插件，FQN 精确命中源码（高可信）。
    linked_by_name  FQN 没命中，但按末段类名唯一命中（中可信，note 记降级原因）。
    external        平台插件（kd.*），本就无源码，归外部、不报缺失。
    missing         project 插件，源码里找不到 —— 重要信号：可能源码没给全。
    ambiguous       末段类名多处重名、无法消歧 → unknown，列入歧义清单。

另两类产物：
    孤儿类  源码里有、却没被任何元数据插件绑定的类（service/util/webapi…），
            是阶段 4 模块识别/风险热点的输入，必须纳入。
    前缀    两套前缀（代码包前缀 / 元数据标识前缀）自动发现统计，仅作报告产物。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Literal

from . import namespace

if TYPE_CHECKING:
    from ..ingest.scanner import ScanResult
    from ..metadata.model import MetaModel

BindStatus = Literal[
    "linked", "linked_by_name", "external", "missing", "ambiguous"
]


@dataclass
class Binding:
    """一条插件绑定的桥接结果（粒度：一个 ClassName × 一个绑定它的表单）。"""

    class_name: str                # 元数据 <ClassName> 全限定名（原样保留）
    plugin_type: str               # form / list / op / writeback
    plugin_source: str             # 元数据标注的来源：project / platform / unknown
    form_key: str | None           # 绑定它的表单标识
    form_name: str | None          # 表单中文名
    status: BindStatus
    source_relpath: str | None = None  # 命中的源码文件相对路径（命中才有）
    confidence: float = 0.0        # 0~1：精确命中 1.0、按名命中 0.6、未命中/外部按语义
    candidates: list[str] = field(default_factory=list)  # 歧义时的候选文件清单
    note: str | None = None        # 降级/未命中原因，示可信度


# 孤儿角色（未被任何元数据插件绑定的源码类的轻量标签）：
#   plugin   = 继承了苍穹插件基类（传递闭包）的插件实现类，却无元数据绑定 —— 重要信号
#              （死代码 / 元数据未给全），区别于普通 service/util。
#   constant = 常量/标识定义类（喂阶段6/9，阶段4风险不计）。
#   unknown  = 待定（真孤儿，阶段4风险关注）。
OrphanRole = Literal["plugin", "constant", "unknown"]

# 常量类信号（轻量、只认包名/类名，不解析字段体——那是阶段5/6）。
#   包名段含 cons/const/...；或类名以 Const/Constant(s) 结尾。
# 注：曾含短后缀 Con/Cons，但真实项目抽查显示所有 *Con 常量类都住在 cons/const 包里、
# 已被包名信号命中，短后缀贡献为 0 却带来「业务类恰好叫 XxxCon 被误标」的潜在假阳，
# 故收紧去掉，只保留强后缀 Const*（见会话记录 2026-06-16）。
_CONST_PKG_SEG = frozenset({"cons", "const", "consts", "constant", "constants"})
_CONST_NAME_RE = re.compile(r"Const(?:ant)?s?$")


def _orphan_role(simple: str, package: str | None, plugin_base: str | None) -> OrphanRole:
    """按继承/包名/类名给孤儿打轻量角色标签。

    优先级：plugin（继承苍穹插件基类，最强信号）> constant（常量类）> unknown（真孤儿）。
    """
    if plugin_base:
        return "plugin"
    if package:
        segs = {s.lower() for s in package.split(".")}
        if segs & _CONST_PKG_SEG:
            return "constant"
    if _CONST_NAME_RE.search(simple):
        return "constant"
    return "unknown"


@dataclass
class OrphanClass:
    """源码里未被任何元数据插件绑定的类（孤儿）。"""

    fqn: str
    relpath: str
    package: str | None
    role: OrphanRole = "unknown"
    plugin_base: str | None = None  # role=='plugin' 时记命中的苍穹插件基类，便于核对


@dataclass
class BridgeResult:
    """桥接全量结果。"""

    bindings: list[Binding] = field(default_factory=list)
    orphans: list[OrphanClass] = field(default_factory=list)
    source_file_count: int = 0     # 索引到的源码文件数
    source_type_count: int = 0     # 索引到的源码顶层类型数
    plugin_total: int = 0          # 元数据插件绑定总条数（含跨表单重复）
    code_prefixes: dict[str, int] = field(default_factory=dict)
    meta_prefixes: dict[str, int] = field(default_factory=dict)

    # ── 便捷分组（按 status）──────────────────────────────────────
    def _by(self, status: BindStatus) -> list[Binding]:
        return [b for b in self.bindings if b.status == status]

    @property
    def linked(self) -> list[Binding]:
        return self._by("linked")

    @property
    def linked_by_name(self) -> list[Binding]:
        return self._by("linked_by_name")

    @property
    def external(self) -> list[Binding]:
        return self._by("external")

    @property
    def missing(self) -> list[Binding]:
        return self._by("missing")

    @property
    def ambiguous(self) -> list[Binding]:
        return self._by("ambiguous")


def _iter_plugins(models: Iterable["MetaModel"]):
    """展开所有模型的插件，带上所属表单上下文。"""
    for m in models:
        for p in m.plugins:
            yield m, p


def _external_reason(class_name: str | None, plugin_source: str) -> tuple[str, float] | None:
    """判定一个插件是否属"外部、无源码"。返回 (原因, 置信度)，否则 None。

    Java 类名约定 PascalCase，故末段全小写或内嵌平台 SDK 路径的，根本不是项目源码
    类，而是平台模板/平台 API 引用 —— 归外部比报 missing 更诚实（用户 2026-06-16 拍板）。
    """
    if not class_name:
        return ("插件无 ClassName（平台占位/oid 绑定）", 0.8)
    if class_name.startswith(("kd.", "kd_")):
        return ("平台 SDK 类（kd.*）", 1.0)
    if ".kd.bos." in class_name:
        return ("内嵌平台 SDK 引用（含 .kd.bos.）", 0.9)
    simple = class_name.rsplit(".", 1)[-1]
    # 末段全小写（无任何大写字母）→ 非 Java 类名，疑似平台模板/元数据引用。
    if simple and not any(c.isupper() for c in simple):
        return ("疑似平台模板/非 Java 类引用（末段全小写）", 0.9)
    if plugin_source == "platform":
        return ("元数据标注为平台插件", 0.8)
    return None


def link(
    scan_result: "ScanResult",
    models: Iterable["MetaModel"],
    *,
    index: "namespace.SourceIndex | None" = None,
) -> BridgeResult:
    """执行桥接：源码索引 × 元数据插件 → 分类绑定 + 孤儿 + 前缀。

    index：可传入已建好的源码命名空间索引复用——阶段 4 的 build 流程会先建一次索引
    同时喂桥接与模块识别，避免对成百上千文件重复解析（守红线「规模大」）。None 则自建。
    """
    models = list(models)
    if index is None:
        index = namespace.build_index(scan_result)

    result = BridgeResult(
        source_file_count=len(index.units),
        source_type_count=sum(len(u.all_types) for u in index.units),
        code_prefixes=namespace.discover_code_prefixes(index),
        meta_prefixes=namespace.discover_meta_prefixes(models),
    )

    # 记录所有「被命中的源码 FQN」，命中后从孤儿集合里剔除。
    bound_fqns: set[str] = set()

    for model, plugin in _iter_plugins(models):
        result.plugin_total += 1
        cn = plugin.class_name
        b = Binding(
            class_name=cn or "(无 ClassName)",
            plugin_type=plugin.plugin_type,
            plugin_source=plugin.source,
            form_key=model.key,
            form_name=model.name,
            status="external",
        )

        # 1) 平台插件 / 无 ClassName / 非 Java 类引用：归外部，不期望源码。
        ext = _external_reason(cn, plugin.source)
        if ext is not None:
            b.status = "external"
            b.note, b.confidence = ext
            result.bindings.append(b)
            continue

        # 2) project 插件：FQN 精确命中优先。
        exact = index.lookup_fqn(cn)
        if exact:
            unit = exact[0]
            b.status = "linked"
            b.confidence = 1.0
            b.source_relpath = unit.relpath
            if len(exact) > 1:
                b.candidates = [u.relpath for u in exact]
                b.note = f"FQN 命中 {len(exact)} 个文件（取首个；可能重复包）"
            bound_fqns.add(cn)
            result.bindings.append(b)
            continue

        # 3) FQN 不命中 → 按末段类名兜底（可能源码包路径与元数据 ClassName 不一致）。
        simple = cn.rsplit(".", 1)[-1]
        by_name = index.lookup_simple(simple)
        if len(by_name) == 1:
            unit = by_name[0]
            matched_fqn = f"{unit.package}.{simple}" if unit.package else simple
            b.status = "linked_by_name"
            b.confidence = 0.6
            b.source_relpath = unit.relpath
            b.note = f"FQN 未命中，按类名唯一匹配到 {matched_fqn}"
            bound_fqns.add(matched_fqn)
            result.bindings.append(b)
            continue
        if len(by_name) > 1:
            b.status = "ambiguous"
            b.confidence = 0.2
            b.candidates = sorted({u.relpath for u in by_name})
            b.note = f"类名 {simple} 在 {len(by_name)} 处出现，无法消歧（标 unknown）"
            result.bindings.append(b)
            continue

        # 4) 彻底找不到：project 插件却无源码 —— 重要信任信号。
        b.status = "missing"
        b.confidence = 0.0
        b.note = "project 插件，源码树未找到对应 FQN/类名（可能源码未给全）"
        result.bindings.append(b)

    # 孤儿：所有源码顶层 FQN 中，未被任何绑定命中的。打轻量角色标签。
    # 先做插件基类传递闭包：识别「继承了苍穹插件基类」的类型简单名（含经过项目中间基类）。
    plugin_simple = namespace.resolve_plugin_classes(index)
    for unit in index.units:
        for fqn, simple in zip(unit.all_fqns, unit.all_types):
            if fqn not in bound_fqns:
                base = plugin_simple.get(simple)
                result.orphans.append(
                    OrphanClass(
                        fqn=fqn, relpath=unit.relpath, package=unit.package,
                        role=_orphan_role(simple, unit.package, base),
                        plugin_base=base,
                    )
                )

    return result
