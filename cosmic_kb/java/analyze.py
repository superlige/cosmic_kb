"""阶段 5+6（类内+跨类）+7（返工）· 字段级排障分析编排。

产出两类记录：
  * plugin_method —— 已绑定插件类里的方法（事件/helper）+ 落库相位 + 行号。
  * field_access  —— 字段读写：字段 key/层级/所属分录、**数据包来源实体**、入口事件函数、
    跨类调用路径、是否落库。

两轮分析（用户 2026-06-17）：
  ① **插件归因（跨类回溯 + 来源实体传播）**：对每个已桥接插件，从事件方法出发跨类 BFS，把沿途
     （含 service/工具类）字段读写归因到「触发它的插件 + 事件」；数据包来源实体按用户口径判定
     （事件入参=绑定单据；ORM load=实参实体；转换=目标/源单），并按「实参↔形参」一路传播进
     被调方法 —— 字段读写的 form_key = 数据包来源实体（判不出则 None，归「未定位」，不臆造）。
  ② **全量孤立补全**：第①轮没覆盖到的项目类方法仍逐一抽取；来源实体能由 ORM load 判出就用，
     否则 None。

每条记录带 access_class（读写**物理所在**类）与 plugin_fqn（**入口**插件/类）。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from . import annotation_map as annmod
from . import ast_index as ax
from . import call_edges as cemod
from . import call_graph as cgmod
from . import event_extractor as events
from . import field_access as fa
from . import null_reason as nrmod
from . import op_trigger as otmod
from . import persistence as persist
from . import plugin_classifier as classifier
from . import project_graph as pgmod

if TYPE_CHECKING:
    from ..bridge.linker import BridgeResult
    from ..bridge.namespace import SourceIndex
    from ..ingest.scanner import ScanResult
    from ..metadata.model import MetaModel
    from ..progress import Progress
    from .constants import ConstantTable
    from .project_graph import ClassNode, ProjectGraph
    from .symbols import SymbolTable

_MAX_DEPTH = 8


@dataclass
class PluginMethod:
    plugin_fqn: str
    method_name: str
    event_kind: str
    event_phase: str
    start_line: int
    end_line: int
    source_relpath: str


@dataclass
class FieldAccessRow:
    form_key: str | None           # 数据包来源实体（判不出为 None=未定位到具体单据）
    field_key: str | None
    level: str
    entry_key: str | None
    plugin_fqn: str                # 入口插件/类
    plugin_type: str               # form/list/op/writeback/convert/service
    access_class: str              # 读写物理所在类 FQN（跨类时≠plugin_fqn）
    event_method: str
    event_phase: str
    access: str
    persists: str
    persist_reason: str | None
    via: str
    line: int
    path: list[str]
    key_resolution: str
    confidence: float
    source_relpath: str
    access_method: str | None = None  # 读写物理所在方法（与 access_class 组成入口回溯坐标）
    evidence: str | None = None
    form_key_source: str | None = None  # form_key 来源：data_flow / metadata_*（反查回填）/ None
    # 未定位成因：form_key=None 时**为何** None（信任优先，红线 #4）。由 _finalize_null_reason 在全部
    # 回填后定稿，取值见 java/null_reason.py；form_key 已定位则为 None（被回填救活的行也清空）。
    null_reason: str | None = None
    # 调用边精度：local（无跨类）/ symbol（跨类边均由符号确认）/ heuristic（均为名字兜底）/
    # mixed（同一路径同时含 symbol 与 heuristic）。
    edge_source: str = "local"
    # 接收者基变量名：仅供 _backfill_form_key 做同对象共现交集分组，不落库（store INSERT 不含它）。
    receiver_var: str | None = None


@dataclass
class AnalysisResult:
    field_accesses: list[FieldAccessRow] = field(default_factory=list)
    plugin_methods: list[PluginMethod] = field(default_factory=list)
    analyzed_plugin_count: int = 0
    standalone_class_count: int = 0
    skipped_no_source: int = 0
    available: bool = True
    # 全工程常量值表（常量名→字面值）。供粗扫侧把常量引用也算作召回（信任手段二），
    # 复用这里已建好的表、避免重复解析全工程 Java。tree-sitter 未装时为 None。
    const_table: "ConstantTable | None" = None
    # 程序化操作触发点（隐藏坑 #1）：executeOperate/invokeOperation 调用点 → 目标单据.操作。
    operation_triggers: list["otmod.OperationTriggerRow"] = field(default_factory=list)
    # 阶段 12.3：全量调用点事实（项目/平台/类内/method_reference/failed 均保留）。
    call_edges: list["cemod.CallEdgeRow"] = field(default_factory=list)


def analyze(
    scan_result: "ScanResult",
    models: Iterable["MetaModel"],
    bridge_result: "BridgeResult",
    index: "SourceIndex",
    symbols: "SymbolTable | None" = None,
    progress: "Progress | None" = None,
) -> AnalysisResult:
    """对项目做字段级分析（插件跨类归因 + 全量孤立补全）。

    progress：可选进度报告器。本函数是 build 里最重的一段（成百上千文件 × 多轮
    分析），按子步骤接力打点（解析工程 Java → 插件归因 → 孤立补全 → 各回填），
    不注入时完全静默（MCP/测试路径零输出）。
    """
    from ..progress import NULL
    from .parser import is_available

    progress = progress or NULL
    result = AnalysisResult()
    if not is_available():
        result.available = False
        return result

    models = list(models)
    pg = pgmod.build_project_graph(scan_result, index, symbols=symbols, progress=progress)
    # 与后续字段分析复用同一棵 ProjectGraph；只抽一次，不为反查工具重复扫描源码。
    progress.tick(0, None, label="收集全量调用边")
    result.call_edges = cemod.collect_call_edges(pg)
    const = pg.const
    known_entities = _known_entities(models)

    # 注解驱动映射（POJO↔DynamicObject）索引：value 命中 KB 字段 key 才登记（KB 反验证，不臆造）。
    # 挂到 pg 上，供 _walk_event / _analyze_standalone 走到反射 bulk-write 方法时合成 FieldAccess。
    field_idx = _field_form_index(models)
    pg.annmap = annmod.build_index(pg, frozenset(field_idx))

    from ..bridge import namespace
    plugin_base = namespace.resolve_plugin_classes(index)

    binding_idx = {
        (b.form_key, b.class_name, b.plugin_type): b for b in bridge_result.bindings
    }
    op_type_by: dict[tuple[str | None, str | None], str | None] = {}
    for m in models:
        for op in m.operations:
            op_type_by[(m.key, op.key)] = op.operation_type

    covered: set[tuple[str, str]] = set()
    bound_fqns: set[str] = set()
    # 类 FQN → 其绑定的（去重）来源单据集：供第②轮全量补全时，给「已绑定插件但未被事件 BFS
    # 覆盖到」的方法回落本类的绑定单据（插件实例就绑这张单据，getModel()/根包写入即作用其模型；
    # 绑多张单据则歧义、留 None 不臆造）。只收 linked/linked_by_name 的绑定。
    bound_entity: dict[str, set[str]] = {}

    # ── 第①轮：绑定插件归因（跨类回溯 + 来源实体传播）─────────────────────
    #    先收任务清单再逐个跑：进度打点需要知道总数（"还差多少"），收集本身只是字典查找、零成本。
    bound_tasks = []
    for m in models:
        for p in m.plugins:
            if not p.class_name:
                continue
            b = binding_idx.get((m.key, p.class_name, p.plugin_type))
            if b is None or b.status not in ("linked", "linked_by_name"):
                continue
            bound_tasks.append((m, p))
    for done, (m, p) in enumerate(bound_tasks, 1):
        progress.tick(done, len(bound_tasks), "个插件", label="插件归因(跨类回溯)")
        node = pg.classes.get(p.class_name)
        if node is None:
            result.skipped_no_source += 1
            continue
        if p.plugin_type == "convert" and m.convert is not None:
            entry_form = m.convert.target_entity or m.key
            convert_source = m.convert.source_entity
        else:
            entry_form, convert_source = m.key, None
        if entry_form:
            bound_entity.setdefault(node.fqn, set()).add(entry_form)
        op_type = op_type_by.get((m.key, p.operation_key)) if p.plugin_type == "op" else None
        _analyze_bound_plugin(
            pg, node, p.plugin_type, entry_form, convert_source, op_type,
            const, known_entities, plugin_base, covered, result,
        )
        bound_fqns.add(p.class_name)
        result.analyzed_plugin_count += 1

    # ── 第①.5 轮：未绑定的苍穹插件基类（调度 AbstractTask / WebApi / 工作流…）作跨类入口 ──
    unbound = [(fqn, node, base) for fqn, node in pg.classes.items()
               if (base := plugin_base.get(node.simple)) and fqn not in bound_fqns]
    for done, (fqn, node, base) in enumerate(unbound, 1):
        progress.tick(done, len(unbound), "个类", label="未绑定插件入口")
        _analyze_unbound_plugin(pg, node, base, const, known_entities, covered, result)
        result.analyzed_plugin_count += 1

    # ── 第②轮：全量孤立补全（其余普通 service/util 类，扁平）────────────────────
    for done, (fqn, node) in enumerate(pg.classes.items(), 1):
        progress.tick(done, len(pg.classes), "个类", label="孤立方法补全")
        if _analyze_standalone(pg, node, const, known_entities, plugin_base,
                               bound_entity, covered, result):
            result.standalone_class_count += 1

    # ── 孤立方法反向调用图回填（doc §5 #1）：唯一调用方实参来源沿「实参↔形参」传播 ──────────
    #    排在元数据兜底**之前**——反向调用图给的是真实数据流来源（实参确实携带该来源），强度高于
    #    「字段 key 反查元数据」，先定、metadata 只补它没救到的。
    _backfill_reverse_calls(result, pg, const, known_entities, bound_entity, progress=progress)

    # ── 字段 key 反查元数据回填 form_key（待办一：数据流追不到来源时的硬约束兜底）──────────
    progress.tick(0, None, label="元数据反查回填")
    _backfill_form_key(result, field_idx, bound_entity)

    # ── 程序化操作触发点采集（隐藏坑 #1）：独立全量扫，不依赖插件 BFS ────────────────
    #    放在第①轮之后——invokeOperation 的目标单据要靠 bound_entity（本类唯一绑定单据）。
    progress.tick(0, None, label="程序化触发点采集")
    result.operation_triggers = otmod.collect_triggers(pg, bound_entity)

    result.field_accesses = _dedup(result.field_accesses)
    # ── 未定位成因定稿（信任优先）：全部回填之后，给仍 form_key=None 的行打结构化成因 ──────────
    _finalize_null_reason(result)
    result.const_table = const          # 暴露给粗扫侧复用（信任手段二）
    return result


def _dedup(rows: list[FieldAccessRow]) -> list[FieldAccessRow]:
    """去重：同一插件类被绑定到多个操作时会把同一处读写重复归因（口径一致），收敛成一条。

    去重键保留所有有区分意义的维度（来源单据/层级/分录/入口插件/物理类/事件/行/读写/落库结论），
    故多单据消歧、不同落库结论等仍各自保留，只消掉真正逐字重复的记录。
    """
    seen: dict[tuple, FieldAccessRow] = {}
    out: list[FieldAccessRow] = []
    for r in rows:
        k = (r.form_key, r.field_key, r.level, r.entry_key, r.plugin_fqn, r.access_class,
             r.access_method, r.event_method, r.line, r.access, r.persists)
        previous = seen.get(k)
        if previous is not None:
            source_atoms = {
                "local": set(), "symbol": {"symbol"}, "heuristic": {"heuristic"},
                "mixed": {"symbol", "heuristic"},
            }
            previous.edge_source = _edge_grade(
                source_atoms.get(previous.edge_source, {"heuristic"})
                | source_atoms.get(r.edge_source, {"heuristic"}))
            continue
        seen[k] = r
        out.append(r)
    return out


def _finalize_null_reason(result: AnalysisResult) -> None:
    """未定位成因定稿（信任优先，红线 #4）：全部回填**之后**，给每条 form_key=None 的行打结构化成因。

    单一真源在 `java/null_reason.classify`（定稿层与 trace/coverage/web 共用同口径）：form_key 已定位
    （含被反向调用图/元数据回填救活的行）→ 成因清空（None）；仍 None → 按优先级归一个互斥成因码。
    """
    for r in result.field_accesses:
        r.null_reason = nrmod.classify(r)


def _field_form_index(
    models: list["MetaModel"],
) -> dict[str, list[tuple[str | None, str | None, str | None]]]:
    """字段 key → 它在元数据里出现的全部 (form_key, entity_key, level)。

    供 form_key 解不出时反查来源实体（待办一的物理硬约束：一个 DynamicObject 不可能
    `.set("cqkd_xxx")`，除非它的实体类型声明了该字段 key）。与 `field` 表口径一致（同源 `m.fields`）。

    排除 `kind='platform'`（苍穹平台通用系统字段，如 `id/name/number/status/org/creator/
    createtime/modifier/modifytime`）：这类字段语义在任意实体间通用，"某 key 在当前已知
    元数据里只归一张单据"这件事一旦发生在通用系统字段上，往往只是因为**还没并入**其它同样
    带这个字段的实体（如原厂元数据只选择性并入了被代码引用到的少数实体，见
    `cosmic_kb/dbmeta/integrate.py`），不是真的语义唯一——若不排除，会让毫不相关的孤立 helper
    被"看似确定实则臆造"地误判定位（红线 #4）。排除后这类字段只要数据流本身能追到来源仍会
    正常定位，只是这条反查兜底对它们失效——追不到就诚实留 None，不是回归。
    """
    idx: dict[str, list[tuple[str | None, str | None, str | None]]] = {}
    for m in models:
        for f in m.fields:
            if not f.key or f.kind == "platform":
                continue
            idx.setdefault(f.key, []).append((m.key, f.entity_key, f.level))
    return idx


def _backfill_form_key(
    result: AnalysisResult,
    field_idx: dict[str, list[tuple[str | None, str | None, str | None]]],
    bound_entity: dict[str, set[str]],
) -> None:
    """字段 key 反查元数据回填 form_key（待办一）：数据流追不到 DO 来源时，用被读写的字段 key
    反推来源实体。三层逐级塌缩，仍解不出留 None（红线 #4：宁标未定位不臆造）。

    ① 唯一反查：字段 key 在元数据只归一个单据 → 直接定 form_key（物理硬约束，高置信）。
    ② 绑定收敛：归多单据时，与「写它的 access_class / 入口插件的绑定单据」取交，唯一 → 定它。
    ③ 同对象共现交集：同一接收者变量在同方法连读写多字段 → 候选 form 集合取交集，唯一 → 定它。
    回填只改 form_key=None 的行，并打独立 form_key_source + evidence 备注，明示依据是字段归属
    （元数据反推）而非数据流证明。
    """
    def cand(field_key: str | None) -> set[str]:
        return {f for f, _e, _l in field_idx.get(field_key or "", []) if f}

    def assign(row: FieldAccessRow, form: str, source: str, note: str, conf_cap: float) -> None:
        row.form_key = form
        row.form_key_source = source
        # 该 form 下该字段的坐标唯一时一并回填 level/entry_key（表头 entry_key 归 None）。
        coords = {(e, lv) for f, e, lv in field_idx.get(row.field_key or "", []) if f == form}
        if len(coords) == 1:
            ent_key, lvl = next(iter(coords))
            if lvl:
                row.level = lvl
            row.entry_key = None if lvl == "header" else ent_key
        row.confidence = round(min(row.confidence, conf_cap), 3)
        row.evidence = (f"{row.evidence} | {note}" if row.evidence else note)

    todo = [r for r in result.field_accesses
            if r.form_key is None and r.field_key and cand(r.field_key)]

    # ③ 预计算同对象共现交集：按 (源文件, 物理类, 入口事件, 接收者变量) 分组，对组内**全部**字段
    #    （含 ① 即将唯一定下的兄弟字段）的候选 form 集合取交集——一个接收者变量只代表一个数据包，
    #    其来源实体须满足它读写的所有字段。交集为单元素即该变量的来源单据。
    groups: dict[tuple, list[FieldAccessRow]] = {}
    for r in todo:
        if r.receiver_var:
            groups.setdefault((r.source_relpath, r.access_class, r.event_method, r.receiver_var),
                              []).append(r)
    group_form: dict[tuple, str] = {}
    for gk, grp in groups.items():
        inter: set[str] | None = None
        for r in grp:
            inter = cand(r.field_key) if inter is None else (inter & cand(r.field_key))
        if inter and len(inter) == 1:
            group_form[gk] = next(iter(inter))

    for r in todo:
        forms = cand(r.field_key)
        if len(forms) == 1:                                   # ① 唯一反查
            assign(r, next(iter(forms)), "metadata_unique",
                   "form_key 由字段 key 反查元数据推得（数据流未追到，字段归属唯一·物理硬约束）", 0.9)
            continue
        bound = bound_entity.get(r.access_class, set()) | bound_entity.get(r.plugin_fqn, set())
        inter = forms & bound
        if len(inter) == 1:                                   # ② 绑定收敛
            assign(r, next(iter(inter)), "metadata_binding",
                   "form_key 由字段 key 反查元数据 + 绑定插件归属收敛推得（数据流未追到）", 0.7)
            continue
        gk = (r.source_relpath, r.access_class, r.event_method, r.receiver_var)
        gf = group_form.get(gk) if r.receiver_var else None   # ③ 同对象共现交集
        if gf and gf in forms:
            assign(r, gf, "metadata_cooccur",
                   "form_key 由字段 key 反查元数据 + 同对象共现字段交集收敛推得（数据流未追到）", 0.7)


@dataclass(frozen=True)
class _ReverseCall:
    caller_fqn: str
    caller_method: str
    invocation: "ax.Invocation"
    source: str                         # local | symbol | heuristic


def _build_reverse_calls(
    pg: "ProjectGraph",
    progress: "Progress | None" = None,
) -> dict[tuple[str, str], list[_ReverseCall]]:
    """全项目反向调用边索引：(目标类FQN, 目标方法) → [(调用方FQN, 调用方方法, 调用点), …]。

    遍历每个类的**全部重载**方法体，对每个调用用现成的 `_resolve_call` 解析到项目内目标
    （本类方法 / 可解析跨类方法；受者类型解不出就不收=宁缺毋滥）。自调用（递归）`_resolve_call`
    已天然返回 None（`inv.name != method`），不会进索引。
    """
    callers: dict[tuple[str, str], list[_ReverseCall]] = {}
    for done, (fqn, node) in enumerate(pg.classes.items(), 1):
        if progress is not None:
            progress.tick(done, len(pg.classes), "个类", label="反向调用图·建索引")
        for md in node.cg.method_decls:
            if md.body is None:
                continue
            for inv in ax.iter_invocations(md.body, include_refs=True):
                tgt = _resolve_call(pg, node, md.name, inv)
                if tgt is None:
                    continue
                callers.setdefault(tgt.key, []).append(
                    _ReverseCall(fqn, md.name, inv, tgt.source))
    return callers


def _all_call_edges(
    callers: dict[tuple[str, str], list[_ReverseCall]],
) -> list[tuple[tuple[str, str], tuple[str, str], "ax.Invocation", str]]:
    """把反向索引摊平成 [(caller, target, invocation)]，供固定点传播逐边重算。"""
    out: list[tuple[tuple[str, str], tuple[str, str], "ax.Invocation", str]] = []
    for target, sites in callers.items():
        for site in sites:
            out.append(((site.caller_fqn, site.caller_method), target,
                        site.invocation, site.source))
    return out


def _has_propagable_param(method_node) -> bool:
    """方法形参里是否有「能从调用方实参携带来源」的项：DO / DO[] / 集合 / 模型视图 / String。

    没有这类形参，反向回填就无入口可传播——直接跳过，省掉建调用方 env 的开销。
    """
    if (_do_params(method_node) or _do_array_params(method_node)
            or _coll_params(method_node) or _model_params(method_node)):
        return True
    return any(t == "String" for _n, t in ax.iter_param_vars(method_node))


def _standalone_env(
    pg: "ProjectGraph", node: "ClassNode", md: "ax.MethodDecl",
    const, known: dict[str, str | None], default_entity: str | None,
) -> "fa._Env":
    """按 `_analyze_standalone` 同口径为单个方法（重载）建分析 env（不注入 prop）。

    供反向回填解析「调用方 ctx_map」（default_entity=调用方唯一绑定单据）与「重跑 helper」
    （default_entity=None，来源全靠传入的 prop）共用。
    """
    resolver = _RetResolver(pg, const, known, node.fqn, default_entity, None)
    return fa._Env(
        const=const, default_entity=default_entity, known_entities=known,
        do_vars=ax.dynamicobject_vars(md.node), do_params=_do_params(md.node),
        do_array_params=_do_array_params(md.node),
        coll_params=_coll_params(md.node),
        do_coll_vars=frozenset(ax.dynamicobject_collection_vars(md.node)),
        model_params=_model_params(md.node),
        local_seed=resolver.local_seed(node, md.name, md=md),
    )


def _prop_nonempty(prop: _Prop) -> bool:
    return bool(prop.param_ctx or prop.model_entities or prop.str_params)


def _prop_signature(prop: _Prop) -> tuple:
    """可哈希签名：只有完全一致的来源传播才允许多调用点合并。"""
    return (
        tuple(sorted((k, tuple(v)) for k, v in prop.param_ctx.items())),
        tuple(sorted(prop.model_entities.items())),
        tuple(sorted(prop.str_params.items())),
    )


def _apply_prop(env: "fa._Env", prop: _Prop) -> None:
    env.param_ctx = dict(prop.param_ctx)
    env.model_entities = dict(prop.model_entities)
    env.str_params = dict(prop.str_params)


def _method_default_entity(bound_entity: dict[str, set[str]], fqn: str) -> str | None:
    ents = bound_entity.get(fqn)
    return next(iter(ents)) if ents and len(ents) == 1 else None


def _method_label(pg: "ProjectGraph", key: tuple[str, str]) -> str:
    node = pg.classes.get(key[0])
    return f"{node.simple if node else key[0]}.{key[1]}"


@dataclass
class _PropInfo:
    prop: _Prop
    depth: int
    site_count: int
    labels: tuple[str, ...]
    edge_sources: frozenset[str] = frozenset()


def _propagate_reverse_props(
    pg: "ProjectGraph", callers: dict[tuple[str, str], list[_ReverseCall]],
    const, known: dict[str, str | None], bound_entity: dict[str, set[str]],
    wanted: set[tuple[str, str]],
    progress: "Progress | None" = None,
) -> dict[tuple[str, str], _PropInfo]:
    """沿可解析调用边做保守固定点传播。

    每轮用调用方当前已知 prop 重跑方法体，再用 `_callee_prop` 推出目标形参来源。
    对同一目标方法，必须**全部调用点**都推出非空且完全一致的 prop 才采纳；任一未知/冲突
    都不传播，避免把多入口 helper 错归到某一张单据。
    """
    relevant = set(wanted)
    queue = list(wanted)
    while queue:
        cur = queue.pop(0)
        for site in callers.get(cur, []):
            ck = (site.caller_fqn, site.caller_method)
            if ck not in relevant:
                relevant.add(ck)
                queue.append(ck)
    scoped_callers = {k: v for k, v in callers.items() if k in relevant}
    edges = [e for e in _all_call_edges(scoped_callers) if e[0] in relevant and e[1] in relevant]
    if not edges:
        return {}
    infos: dict[tuple[str, str], _PropInfo] = {}
    limit = min(max(len(edges) + 2, 2), 64)

    # 固定点传播是 analyze 里最后一段重活（轮数 × 边数，每条边重跑方法体分析，真实项目
    # 可达几十秒），逐边打累计数——轮数因提前收敛不可预知，报「已处理 N 条边」不报百分比。
    evaluated = 0
    for _ in range(limit):
        proposals: dict[tuple[str, str], list[tuple[_Prop, int, str, frozenset[str]]]] = {}
        for caller, target, inv, edge_source in edges:
            evaluated += 1
            if progress is not None:
                progress.tick(evaluated, None, "条边", label="反向调用图·固定点传播")
            caller_node = pg.classes.get(caller[0])
            target_node = pg.classes.get(target[0])
            if caller_node is None or target_node is None:
                continue
            caller_md = caller_node.cg.methods.get(caller[1])
            target_md = target_node.cg.methods.get(target[1])
            if (caller_md is None or caller_md.body is None or target_md is None
                    or target_md.body is None or not _has_propagable_param(target_md.node)):
                continue
            caller_env = _standalone_env(
                pg, caller_node, caller_md, const, known,
                _method_default_entity(bound_entity, caller[0]),
            )
            caller_info = infos.get(caller)
            if caller_info is not None:
                _apply_prop(caller_env, caller_info.prop)
            _, caller_ctx = fa.analyze_method(caller_md.body, caller_env)
            prop = _callee_prop(pg, target, inv, caller_ctx, caller_env)
            if not _prop_nonempty(prop):
                continue
            depth = (caller_info.depth + 1) if caller_info is not None else 1
            sources = (caller_info.edge_sources if caller_info is not None else frozenset())
            if edge_source != "local":
                sources = sources | {edge_source}
            proposals.setdefault(target, []).append(
                (prop, depth, _method_label(pg, caller), sources))

        new_infos: dict[tuple[str, str], _PropInfo] = {}
        for target, sites in scoped_callers.items():
            vals = proposals.get(target, [])
            if len(vals) != len(sites):
                continue
            sigs = {_prop_signature(p) for p, _d, _label, _sources in vals}
            if len(sigs) != 1:
                continue
            labels = tuple(sorted({_label for _p, _d, _label, _sources in vals}))
            new_infos[target] = _PropInfo(
                prop=vals[0][0],
                depth=max(d for _p, d, _label, _sources in vals),
                site_count=len(sites),
                labels=labels,
                edge_sources=frozenset().union(
                    *(sources for _p, _d, _label, sources in vals)),
            )
        if {k: (_prop_signature(v.prop), v.depth, v.site_count, v.labels, v.edge_sources)
                for k, v in new_infos.items()} == {
                    k: (_prop_signature(v.prop), v.depth, v.site_count, v.labels, v.edge_sources)
                    for k, v in infos.items()
                }:
            return new_infos
        infos = new_infos
    return infos


def _backfill_reverse_calls(
    result: AnalysisResult, pg: "ProjectGraph", const, known: dict[str, str | None],
    bound_entity: dict[str, set[str]],
    progress: "Progress | None" = None,
) -> None:
    """孤立方法反向调用图回填（doc §5 #1）。

    对 form_key=None 的孤立 helper，沿全项目可解析调用边做固定点传播：唯一调用方链式可传播；
    多调用方必须全部推出同一个来源才传播；0 调用方、来源未知、来源冲突、解析不到调用边都留 None。

    只动 form_key=None 行，绝不改写已定位行；form_key_source 独立标 `reverse_callgraph`。
    """
    none_rows = [r for r in result.field_accesses if r.form_key is None and r.field_key]
    if not none_rows:
        return
    callers = _build_reverse_calls(pg, progress=progress)
    if progress is not None:
        progress.tick(0, None, label="反向调用图·固定点传播")
    # 按 (物理类, 物理方法) 分组。standalone 行的 event_method 即物理方法名；_emit_event 行的
    # event_method 是入口事件名，在 helper 类里多查不到方法（md=None）而自然跳过——本回填只针对
    # 「孤立方法 DO 入参」桶（standalone 行）。
    groups: dict[tuple[str, str], list[FieldAccessRow]] = {}
    for r in none_rows:
        groups.setdefault((r.access_class, r.event_method), []).append(r)
    infos = _propagate_reverse_props(pg, callers, const, known, bound_entity, set(groups),
                                     progress=progress)

    for (fqn, method), rows in groups.items():
        node = pg.classes.get(fqn)
        if node is None:
            continue
        md = node.cg.methods.get(method)        # 重载取首个（与 _resolve_call/_callee_prop 同口径）
        if md is None or md.body is None or not _has_propagable_param(md.node):
            continue
        info = infos.get((fqn, method))
        if info is None:
            continue
        # 带 prop 重跑 helper（default_entity=None，来源全靠传入的 prop）。
        helper_env = _standalone_env(pg, node, md, const, known, None)
        _apply_prop(helper_env, info.prop)
        new_accs, _ = fa.analyze_method(md.body, helper_env)
        located = {(a.line, a.field_key, a.access): a for a in new_accs if a.entity is not None}
        if not located:
            continue
        if info.site_count == 1 and info.depth == 1:
            kind = f"唯一调用方 {info.labels[0]}"
            cap = 0.85
        elif info.site_count == 1:
            kind = f"唯一调用方链式传播 {info.labels[0]}，深度 {info.depth}"
            cap = 0.80
        else:
            kind = f"多调用方来源一致传播 {', '.join(info.labels)}，深度 {info.depth}"
            cap = 0.80
        note = f"form_key 由{kind}沿「实参↔形参」传播推得（反向调用图）"
        for r in rows:
            acc = located.get((r.line, r.field_key, r.access))
            if acc is None:
                continue
            r.form_key = acc.entity
            r.level = acc.level
            r.entry_key = acc.entry_key
            r.form_key_source = "reverse_callgraph"
            r.edge_source = _edge_grade(info.edge_sources)
            r.evidence = f"{r.evidence} | {note}" if r.evidence else note
            r.confidence = round(min(r.confidence, cap), 3)


def _known_entities(models: list["MetaModel"]) -> dict[str, str | None]:
    """全部已知实体/单据标识（form key + 实体 key + 转换上下游）→ 层级。

    供 ORM 实参校验（`in` 判断，dict 与原 frozenset 等价）+ `dataEntity.set(key,...)`
    整体赋值层级判定（只有 entry/subentry 才有意义，表单 key/表头实体/转换上下游一律
    None）。同一 key 若跨表单出现层级冲突（理论存在，未见真实样本），保守置 None，
    退化回 header 兜底，不猜错。
    """
    levels: dict[str, set[str | None]] = {}

    def _add(key: str | None, level: str | None) -> None:
        if key:
            levels.setdefault(key, set()).add(level)

    for m in models:
        _add(m.key, None)
        for e in m.entities:
            _add(e.key, e.level if e.level in ("entry", "subentry") else None)
        if m.convert is not None:
            for x in (m.convert.source_entity, m.convert.target_entity):
                _add(x, None)
    return {k: (next(iter(vs)) if len(vs) == 1 else None) for k, vs in levels.items()}


def _do_params(method_node) -> frozenset[str]:
    """单个 DynamicObject 形参（不含数组）：其完整坐标由调用方按「实参↔形参」传播注入。"""
    return frozenset(
        n for n, t, arr in ax.iter_param_vars_raw(method_node)
        if t == "DynamicObject" and not arr)


def _do_array_params(method_node) -> frozenset[str]:
    """DynamicObject[] 数组/变长形参：语义是一组表头行（按集合处理，元素才是行）。"""
    return frozenset(
        n for n, t, arr in ax.iter_param_vars_raw(method_node)
        if t == "DynamicObject" and arr)


def _coll_params(method_node) -> frozenset[str]:
    """集合形参：DynamicObjectCollection ∪ List/Set/Collection<DynamicObject>（统一走坐标传播）。"""
    base = {n for n, t in ax.iter_param_vars(method_node) if t == "DynamicObjectCollection"}
    return frozenset(base | ax.dynamicobject_collection_params(method_node))


# 携带「绑定单据」的模型/视图/插件形参类型：调用方传 getModel()/getView()/this 时，被调形参即绑该单据。
_MODEL_TYPES = frozenset({
    "IDataModel", "IBillModel", "IFormView", "IBillView", "BillView",
    "AbstractFormPlugin", "AbstractBillPlugIn", "IPageCache",
})
_STR_LIT = re.compile(r'^"([^"]*)"$')


def _model_params(method_node) -> frozenset[str]:
    """模型/视图类型形参名（IDataModel/IBillModel/IFormView…）：其上的 setValue/getValue 即对绑定
    单据的字段读写。供 field_access 把这些形参直接当模型接收者——否则 helper(IDataModel model) 里
    `model.setValue(...)` 的写入整片漏（用户反馈：收益高、样本多、误报低）。"""
    return frozenset(n for n, t in ax.iter_param_vars(method_node) if t in _MODEL_TYPES)


@dataclass
class _Prop:
    """跨方法/跨类按「实参↔形参」传给被调方法的上下文（只传有信息的项）。"""

    param_ctx: dict[str, tuple] = field(default_factory=dict)     # DO/集合形参 → 完整坐标
    model_entities: dict[str, str] = field(default_factory=dict)  # 模型/视图形参 → 绑定单据
    str_params: dict[str, str] = field(default_factory=dict)      # String 形参 → 字面值（分录/字段 key）


def _arg_model_entity(arg: str, base: str, env: "fa._Env") -> str | None:
    """调用实参所携带的模型/视图绑定单据：getModel()/getView()/this → 插件绑定单据；
    已是已知模型形参变量 → 其单据。"""
    if re.search(r"getModel\(\)|getView\(\)", arg) or arg == "this":
        return env.default_entity
    return env.model_entities.get(base)


def _arg_str_value(arg: str, env: "fa._Env") -> str | None:
    """调用实参若为字符串字面量 / 传播来的 String 形参 / 常量引用，解析成字面值。"""
    m = _STR_LIT.match(arg)
    if m:
        return m.group(1)
    if arg in env.str_params:
        return env.str_params[arg]
    return env.const.resolve(arg).value


def _resolve_call(pg: "ProjectGraph", node: "ClassNode", method: str, inv: ax.Invocation):
    """把调用解析成 ``ResolvedCall``：本类 local / 跨类 symbol 或 heuristic。"""
    recv = inv.object_text.strip()
    if recv in ("", "this") and inv.name in node.cg.methods and inv.name != method:
        return pgmod.ResolvedCall(node.fqn, inv.name, "local")
    target = pg._resolve_target(node, method, inv)
    if target is not None and target.fqn == node.fqn:
        return pgmod.ResolvedCall(target.fqn, target.method, "local")
    return target


def _edge_grade(sources: Iterable[str]) -> str:
    """把一条路径/传播链的逐边来源收敛成 schema v18 四档。"""
    cross = set(sources) - {"local"}
    if not cross:
        return "local"
    if cross == {"symbol"}:
        return "symbol"
    if cross == {"heuristic"}:
        return "heuristic"
    return "mixed"


def _callee_prop(pg: "ProjectGraph", target, inv: ax.Invocation,
                 caller_ctx: dict[str, tuple], caller_env: "fa._Env") -> _Prop:
    """按「实参↔形参位置」把调用点实参的信息传给被调形参：

      · DO/集合形参 → 完整坐标 (level, entry_key, entity, is_collection)；
      · 模型/视图形参 → 绑定单据（getModel()/getView()/this 携带的）；
      · String 形参 → 字面值（调用方传的分录/字段 key 常量）。

    只传"有信息"的项；裸 (header,None,None) 坐标不传，让被调用方按默认推断。
    """
    prop = _Prop()
    tfqn, tmethod = target.key if hasattr(target, "key") else target
    tnode = pg.classes[tfqn]
    md = tnode.cg.methods.get(tmethod)
    if md is None:
        return prop
    params = list(ax.iter_param_vars(md.node))
    for i, (pname, ptype) in enumerate(params):
        if i >= len(inv.args):
            break
        arg = ax._text(inv.args[i]).strip()
        base = re.sub(r"\[.*?\]$", "", arg.split(".", 1)[0].split("(", 1)[0].strip())
        c = caller_ctx.get(arg) or caller_ctx.get(base)
        if c and (c[2] is not None or c[1] is not None or c[0] != "header"):
            prop.param_ctx[pname] = c
        if ptype in _MODEL_TYPES:
            ent = _arg_model_entity(arg, base, caller_env)
            if ent:
                prop.model_entities[pname] = ent
        elif ptype == "String":
            val = _arg_str_value(arg, caller_env)
            if val:
                prop.str_params[pname] = val
    return prop


def _init_invocation(init) -> "ax.Invocation | None":
    """取局部变量初始化表达式里的（最外层）方法调用：`bill = this.writeBillToEntry(...)`。"""
    if init is None:
        return None
    for inv in ax.iter_invocations(init):
        return inv          # init 为 method_invocation 时首个即最外层调用
    return None


class _RetResolver:
    """方法「返回数据包坐标」解析器：把 `localvar = this.helper(...)` 的 localvar 绑到 helper
    的返回坐标。只跟进项目内可解析调用；按被调方法所在帧（插件类=绑定单据，否则 None）取
    default_entity 重算其返回上下文。memo 避免重复解析，stack 防递归环。
    """

    def __init__(self, pg: "ProjectGraph", const, known: dict[str, str | None],
                 plugin_fqn: str, entry_form: str | None, convert_source: str | None) -> None:
        self.pg = pg
        self.const = const
        self.known = known
        self.plugin_fqn = plugin_fqn
        self.entry_form = entry_form
        self.convert_source = convert_source
        self.memo: dict[tuple, tuple | None] = {}

    def _frame(self, fqn: str) -> tuple[str | None, str | None]:
        if fqn == self.plugin_fqn:
            return self.entry_form, self.convert_source
        return None, None

    def local_seed(self, node: "ClassNode", method: str, stack: tuple = (),
                   md: "ax.MethodDecl | None" = None) -> dict[str, tuple]:
        """本方法内由「项目方法返回值」赋值的局部变量 → 其返回坐标。

        传 `md` 时按该具体重载的方法体取局部变量（standalone 补扫逐个重载用；不传则按名取首个，
        保留事件 BFS 既有口径）。
        """
        if md is None:
            md = node.cg.methods.get(method)
        if md is None or md.body is None:
            return {}
        seed: dict[str, tuple] = {}
        for lv in ax.iter_local_vars(md.body):
            inv = _init_invocation(lv.init)
            if inv is None:
                continue
            tgt = _resolve_call(self.pg, node, method, inv)
            if tgt is None:
                continue
            rc = self._return_ctx(tgt.fqn, tgt.method, stack)
            if rc is not None:
                seed[lv.name] = rc
        return seed

    def _return_ctx(self, fqn: str, method: str, stack: tuple) -> tuple | None:
        de, cs = self._frame(fqn)
        key = (fqn, method, de)
        if key in self.memo:
            return self.memo[key]
        if key in stack:
            return None                 # 递归环：保守不解
        node = self.pg.classes.get(fqn)
        md = node.cg.methods.get(method) if node else None
        if md is None or md.body is None:
            self.memo[key] = None
            return None
        self.memo[key] = None           # 占位，防同链重入
        seed = self.local_seed(node, method, stack + (key,))
        env = fa._Env(
            const=self.const, default_entity=de, convert_source=cs, known_entities=self.known,
            do_vars=ax.dynamicobject_vars(md.node), do_params=_do_params(md.node),
            do_array_params=_do_array_params(md.node),
            coll_params=_coll_params(md.node),
            do_coll_vars=frozenset(ax.dynamicobject_collection_vars(md.node)),
            local_seed=seed,
        )
        rc = fa.method_return_ctx(md.body, env)
        self.memo[key] = rc
        return rc


def _seg(pg: "ProjectGraph", caller_fqn: str, target) -> str:
    """调用链路径段：本类调用记方法名，跨类记 Simple.method。"""
    if target.fqn == caller_fqn:
        return target.method
    return f"{pg.classes[target.fqn].simple}.{target.method}"


def _walk_event(
    pg: "ProjectGraph", plugin_fqn: str, event_method: str,
    entry_form: str | None, convert_source: str | None, const, known: dict[str, str | None],
):
    """从事件方法跨类 BFS，逐节点抽字段读写 + 传播来源实体。

    返回 (records, seen)：records=[(fqn, method, path, edge_source, accesses)]。
    """
    records: list[tuple[str, str, list[str], str, list]] = []
    seen: set[tuple[str, str]] = {(plugin_fqn, event_method)}
    q: deque = deque([(plugin_fqn, event_method, [event_method], _Prop(), frozenset())])
    resolver = _RetResolver(pg, const, known, plugin_fqn, entry_form, convert_source)
    while q:
        fqn, method, path, prop, edge_sources = q.popleft()
        node = pg.classes.get(fqn)
        md = node.cg.methods.get(method) if node else None
        if md is None:
            continue
        default_entity = entry_form if fqn == plugin_fqn else None
        env = fa._Env(
            const=const, default_entity=default_entity,
            convert_source=convert_source if fqn == plugin_fqn else None,
            param_ctx=prop.param_ctx, known_entities=known,
            do_vars=ax.dynamicobject_vars(md.node), do_params=_do_params(md.node),
            do_array_params=_do_array_params(md.node), coll_params=_coll_params(md.node),
            do_coll_vars=frozenset(ax.dynamicobject_collection_vars(md.node)),
            model_params=_model_params(md.node),
            model_entities=prop.model_entities, str_params=prop.str_params,
            local_seed=resolver.local_seed(node, method),
        )
        accesses, ctx_map = fa.analyze_method(md.body, env)
        annmap = getattr(pg, "annmap", None)
        if annmap:                                  # 反射映射 bulk-write：合成该映射类全部字段写入
            accesses = accesses + annmap.synth_accesses(fqn, method)
        records.append((fqn, method, path, _edge_grade(edge_sources), accesses))
        if len(path) > _MAX_DEPTH:
            continue
        for inv in ax.iter_invocations(md.body, include_refs=True):
            tgt = _resolve_call(pg, node, method, inv)
            key = tgt.key if tgt is not None else None
            if key is None or key in seen:
                continue
            seen.add(key)
            cp = _callee_prop(pg, tgt, inv, ctx_map, env)
            next_sources = edge_sources | ({tgt.source} if tgt.source != "local" else set())
            q.append((tgt.fqn, tgt.method, path + [_seg(pg, fqn, tgt)], cp,
                      frozenset(next_sources)))
    return records, seen


def _analyze_bound_plugin(
    pg: "ProjectGraph", node: "ClassNode", plugin_type: str, entry_form: str | None,
    convert_source: str | None, op_type: str | None, const, known: dict[str, str | None],
    plugin_base: dict[str, str], covered: set[tuple[str, str]], result: AnalysisResult,
) -> None:
    base = plugin_base.get(node.simple)
    kind, _conf, _ev = classifier.plugin_kind(plugin_type, base)
    cg = node.cg

    for name, md in cg.methods.items():
        info = events.classify_method(kind, name)
        result.plugin_methods.append(PluginMethod(
            plugin_fqn=node.fqn, method_name=name,
            event_kind=info.name if info else "helper",
            event_phase=info.phase if info else "helper",
            start_line=md.start_line, end_line=md.end_line, source_relpath=node.relpath,
        ))

    for name in list(cg.methods):
        info = events.classify_method(kind, name)
        if info is None:
            continue
        _emit_event(pg, node.fqn, plugin_type, name, info.phase, entry_form, convert_source,
                    op_type, const, known, covered, result)


def _emit_event(
    pg: "ProjectGraph", plugin_fqn: str, plugin_type: str, event_method: str, phase: str,
    entry_form: str | None, convert_source: str | None, op_type: str | None,
    const, known: dict[str, str | None], covered: set[tuple[str, str]], result: AnalysisResult,
) -> None:
    """从一个入口（事件/根方法）跨类回溯，归集字段读写 + 落库判定 + 写入 result。"""
    records, seen = _walk_event(pg, plugin_fqn, event_method, entry_form, convert_source, const, known)
    sink_reachable = any(
        persist.find_sinks(pg.classes[f].cg.methods[m].body, symbols=pg.symbols,
                           relpath=pg.classes[f].relpath)
        for (f, m) in seen if m in pg.classes[f].cg.methods
    )
    has_ext = pg.has_unresolved_external([pgmod.CrossReach(f, m, []) for (f, m) in seen])
    for fqn, method, path, edge_source, accesses in records:
        covered.add((fqn, method))
        rnode = pg.classes[fqn]
        for acc in accesses:
            if acc.via == "annotation-map":
                # 注解反射映射写入：是否落库取决于调用方是否保存转换产物（未证），不蹭链路 sink 结论。
                persists, reason = "unknown", "注解反射映射写入(条件 set)；落库取决于调用方是否保存转换产物—未证"
                conf = round(min(acc.confidence, 0.6), 3)
            elif acc.access == "write":
                v = persist.verdict(phase, op_type, sink_reachable, has_external=has_ext)
                persists, reason = v.persists, v.reason
                conf = round(min(acc.confidence, v.confidence), 3)
            else:
                persists, reason, conf = "na", None, acc.confidence
            result.field_accesses.append(FieldAccessRow(
                form_key=acc.entity, field_key=acc.field_key, level=acc.level,
                entry_key=acc.entry_key, plugin_fqn=plugin_fqn, plugin_type=plugin_type,
                access_class=fqn, access_method=method,
                event_method=event_method, event_phase=phase,
                access=acc.access, persists=persists, persist_reason=reason,
                via=acc.via, line=acc.line, path=path, key_resolution=acc.key_resolution,
                confidence=conf, source_relpath=rnode.relpath, evidence=acc.note,
                form_key_source="data_flow" if acc.entity else None,
                receiver_var=acc.receiver_var,
                edge_source=edge_source,
            ))


def _analyze_unbound_plugin(
    pg: "ProjectGraph", node: "ClassNode", base: str, const, known: dict[str, str | None],
    covered: set[tuple[str, str]], result: AnalysisResult,
) -> None:
    """未绑定元数据但继承苍穹插件基类的类（调度 AbstractTask / WebApi / 工作流 等）作跨类入口。

    入口方法 = 事件名 ∪ 调用图根方法（覆盖 task.execute、webapi 公共服务方法）；来源全靠 ORM
    + 跨类传播（default_entity=None），落库随 sink 可达判定（task/webapi 常 load→改→save 入库）。
    """
    kind, _c, _e = classifier.plugin_kind(None, base)
    cg = node.cg
    entries = set(cgmod.roots(cg))
    for name in cg.methods:
        if events.classify_method(kind, name) is not None:
            entries.add(name)
    for name in sorted(entries):
        if (node.fqn, name) in covered:
            continue
        info = events.classify_method(kind, name)
        phase = info.phase if info else "none"
        _emit_event(pg, node.fqn, kind, name, phase, None, None, None,
                    const, known, covered, result)


def _analyze_standalone(
    pg: "ProjectGraph", node: "ClassNode", const, known: dict[str, str | None],
    plugin_base: dict[str, str], bound_entity: dict[str, set[str]],
    covered: set[tuple[str, str]], result: AnalysisResult,
) -> bool:
    """补全未被插件覆盖的项目类方法的字段读写（来源实体能由 ORM 判出就用，否则 None）。

    该类若是**已绑定插件**（仅某些方法未被事件 BFS 覆盖到），用其**唯一绑定单据**作
    default_entity——插件实例绑这张单据，方法内 getModel()/根包写入即作用其模型，归因到该单据
    是绑定契约而非臆造。绑多张单据则歧义、回落 None（红线 #4：宁标未定位不臆造）。
    """
    base = plugin_base.get(node.simple)
    kind, _c, _e = classifier.plugin_kind(None, base)
    is_plugin = kind != "unknown"
    plugin_type = kind if is_plugin else "service"
    ents = bound_entity.get(node.fqn)
    base_entity = next(iter(ents)) if ents and len(ents) == 1 else None
    emitted = False
    resolver = _RetResolver(pg, const, known, node.fqn, base_entity, None)
    # 逐个**重载**补扫：covered 按方法名记录，而事件 BFS 只够得着 `cg.methods[name]`（首个重载），
    # 故只跳过「那个被分析过的重载」，其余同名重载（BFS 永远碰不到）必须在此覆盖——否则
    # `floorInit(IDataModel)` 这类重载里的 `model.getValue("cqkd_ssfq")` 整片漏（用户 2026-06-27）。
    for md in node.cg.method_decls:
        name = md.name
        if node.cg.methods.get(name) is md and (node.fqn, name) in covered:
            continue
        env = fa._Env(
            const=const, default_entity=base_entity, known_entities=known,
            do_vars=ax.dynamicobject_vars(md.node), do_params=_do_params(md.node),
            do_array_params=_do_array_params(md.node),
            coll_params=_coll_params(md.node),
            do_coll_vars=frozenset(ax.dynamicobject_collection_vars(md.node)),
            model_params=_model_params(md.node),
            local_seed=resolver.local_seed(node, name, md=md),
        )
        accesses, _ = fa.analyze_method(md.body, env)
        annmap = getattr(pg, "annmap", None)
        if annmap:                                  # 映射类仅被孤立补全够到时（无插件入口）也别隐形
            accesses = accesses + annmap.synth_accesses(node.fqn, name)
        if not accesses:
            continue
        info = events.classify_method(kind, name) if is_plugin else None
        phase = info.phase if info else "unknown"
        self_sink = bool(persist.find_sinks(md.body, symbols=pg.symbols,
                                            relpath=node.relpath))
        for acc in accesses:
            if acc.via == "annotation-map":
                persists, reason = "unknown", "注解反射映射写入(条件 set)；落库取决于调用方是否保存转换产物—未证"
            elif acc.access == "write":
                if self_sink:
                    persists, reason = "yes", "本方法内含显式落库 sink（save/executeOperate/…）"
                else:
                    persists = "unknown"
                    reason = "未被已分析插件调用，落库取决于调用方（service/工具类写入）"
            else:
                persists, reason = "na", None
            result.field_accesses.append(FieldAccessRow(
                form_key=acc.entity, field_key=acc.field_key, level=acc.level,
                entry_key=acc.entry_key, plugin_fqn=node.fqn, plugin_type=plugin_type,
                access_class=node.fqn, access_method=name,
                event_method=name, event_phase=phase,
                access=acc.access, persists=persists, persist_reason=reason,
                via=acc.via, line=acc.line, path=[name], key_resolution=acc.key_resolution,
                confidence=acc.confidence, source_relpath=node.relpath, evidence=acc.note,
                form_key_source="data_flow" if acc.entity else None,
                receiver_var=acc.receiver_var,
            ))
            emitted = True
    return emitted
