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

from . import ast_index as ax
from . import call_graph as cgmod
from . import event_extractor as events
from . import field_access as fa
from . import persistence as persist
from . import plugin_classifier as classifier
from . import project_graph as pgmod

if TYPE_CHECKING:
    from ..bridge.linker import BridgeResult
    from ..bridge.namespace import SourceIndex
    from ..ingest.scanner import ScanResult
    from ..metadata.model import MetaModel
    from .constants import ConstantTable
    from .project_graph import ClassNode, ProjectGraph

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
    evidence: str | None = None


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


def analyze(
    scan_result: "ScanResult",
    models: Iterable["MetaModel"],
    bridge_result: "BridgeResult",
    index: "SourceIndex",
) -> AnalysisResult:
    """对项目做字段级分析（插件跨类归因 + 全量孤立补全）。"""
    from .parser import is_available

    result = AnalysisResult()
    if not is_available():
        result.available = False
        return result

    models = list(models)
    pg = pgmod.build_project_graph(scan_result, index)
    const = pg.const
    known_entities = _known_entities(models)

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

    # ── 第①轮：绑定插件归因（跨类回溯 + 来源实体传播）─────────────────────
    for m in models:
        for p in m.plugins:
            if not p.class_name:
                continue
            b = binding_idx.get((m.key, p.class_name, p.plugin_type))
            if b is None or b.status not in ("linked", "linked_by_name"):
                continue
            node = pg.classes.get(p.class_name)
            if node is None:
                result.skipped_no_source += 1
                continue
            if p.plugin_type == "convert" and m.convert is not None:
                entry_form = m.convert.target_entity or m.key
                convert_source = m.convert.source_entity
            else:
                entry_form, convert_source = m.key, None
            op_type = op_type_by.get((m.key, p.operation_key)) if p.plugin_type == "op" else None
            _analyze_bound_plugin(
                pg, node, p.plugin_type, entry_form, convert_source, op_type,
                const, known_entities, plugin_base, covered, result,
            )
            bound_fqns.add(p.class_name)
            result.analyzed_plugin_count += 1

    # ── 第①.5 轮：未绑定的苍穹插件基类（调度 AbstractTask / WebApi / 工作流…）作跨类入口 ──
    for fqn, node in pg.classes.items():
        base = plugin_base.get(node.simple)
        if base and fqn not in bound_fqns:
            _analyze_unbound_plugin(pg, node, base, const, known_entities, covered, result)
            result.analyzed_plugin_count += 1

    # ── 第②轮：全量孤立补全（其余普通 service/util 类，扁平）────────────────────
    for fqn, node in pg.classes.items():
        if _analyze_standalone(pg, node, const, known_entities, plugin_base, covered, result):
            result.standalone_class_count += 1

    result.field_accesses = _dedup(result.field_accesses)
    result.const_table = const          # 暴露给粗扫侧复用（信任手段二）
    return result


def _dedup(rows: list[FieldAccessRow]) -> list[FieldAccessRow]:
    """去重：同一插件类被绑定到多个操作时会把同一处读写重复归因（口径一致），收敛成一条。

    去重键保留所有有区分意义的维度（来源单据/层级/分录/入口插件/物理类/事件/行/读写/落库结论），
    故多单据消歧、不同落库结论等仍各自保留，只消掉真正逐字重复的记录。
    """
    seen: set[tuple] = set()
    out: list[FieldAccessRow] = []
    for r in rows:
        k = (r.form_key, r.field_key, r.level, r.entry_key, r.plugin_fqn, r.access_class,
             r.event_method, r.line, r.access, r.persists)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _known_entities(models: list["MetaModel"]) -> frozenset[str]:
    """全部已知实体/单据标识（form key + 实体 key + 转换上下游），供 ORM 实参校验。"""
    out: set[str] = set()
    for m in models:
        if m.key:
            out.add(m.key)
        for e in m.entities:
            if e.key:
                out.add(e.key)
        if m.convert is not None:
            for x in (m.convert.source_entity, m.convert.target_entity):
                if x:
                    out.add(x)
    return frozenset(out)


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
    return frozenset(n for n, t in ax.iter_param_vars(method_node) if t == "DynamicObjectCollection")


# 携带「绑定单据」的模型/视图/插件形参类型：调用方传 getModel()/getView()/this 时，被调形参即绑该单据。
_MODEL_TYPES = frozenset({
    "IDataModel", "IBillModel", "IFormView", "IBillView", "BillView",
    "AbstractFormPlugin", "AbstractBillPlugIn", "IPageCache",
})
_STR_LIT = re.compile(r'^"([^"]*)"$')


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
    """把一个调用解析成项目内 (fqn, 方法名)：本类方法 or 可解析跨类方法；否则 None。"""
    recv = inv.object_text.strip()
    if recv in ("", "this") and inv.name in node.cg.methods and inv.name != method:
        return (node.fqn, inv.name)
    return pg._resolve_target(node, method, inv)


def _callee_prop(pg: "ProjectGraph", target, inv: ax.Invocation,
                 caller_ctx: dict[str, tuple], caller_env: "fa._Env") -> _Prop:
    """按「实参↔形参位置」把调用点实参的信息传给被调形参：

      · DO/集合形参 → 完整坐标 (level, entry_key, entity, is_collection)；
      · 模型/视图形参 → 绑定单据（getModel()/getView()/this 携带的）；
      · String 形参 → 字面值（调用方传的分录/字段 key 常量）。

    只传"有信息"的项；裸 (header,None,None) 坐标不传，让被调用方按默认推断。
    """
    prop = _Prop()
    tnode = pg.classes[target[0]]
    md = tnode.cg.methods.get(target[1])
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

    def __init__(self, pg: "ProjectGraph", const, known: frozenset[str],
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

    def local_seed(self, node: "ClassNode", method: str, stack: tuple = ()) -> dict[str, tuple]:
        """本方法内由「项目方法返回值」赋值的局部变量 → 其返回坐标。"""
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
            rc = self._return_ctx(tgt[0], tgt[1], stack)
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
            coll_params=_coll_params(md.node), local_seed=seed,
        )
        rc = fa.method_return_ctx(md.body, env)
        self.memo[key] = rc
        return rc


def _seg(pg: "ProjectGraph", caller_fqn: str, target) -> str:
    """调用链路径段：本类调用记方法名，跨类记 Simple.method。"""
    if target[0] == caller_fqn:
        return target[1]
    return f"{pg.classes[target[0]].simple}.{target[1]}"


def _walk_event(
    pg: "ProjectGraph", plugin_fqn: str, event_method: str,
    entry_form: str | None, convert_source: str | None, const, known: frozenset[str],
):
    """从事件方法跨类 BFS，逐节点抽字段读写 + 传播来源实体。

    返回 (records, seen)：records=[(fqn, method, path, accesses)]，seen={(fqn, method)}。
    """
    records: list[tuple[str, str, list[str], list]] = []
    seen: set[tuple[str, str]] = {(plugin_fqn, event_method)}
    q: deque = deque([(plugin_fqn, event_method, [event_method], _Prop())])
    resolver = _RetResolver(pg, const, known, plugin_fqn, entry_form, convert_source)
    while q:
        fqn, method, path, prop = q.popleft()
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
            model_entities=prop.model_entities, str_params=prop.str_params,
            local_seed=resolver.local_seed(node, method),
        )
        accesses, ctx_map = fa.analyze_method(md.body, env)
        records.append((fqn, method, path, accesses))
        if len(path) > _MAX_DEPTH:
            continue
        for inv in ax.iter_invocations(md.body):
            tgt = _resolve_call(pg, node, method, inv)
            if tgt is None or tgt in seen:
                continue
            seen.add(tgt)
            cp = _callee_prop(pg, tgt, inv, ctx_map, env)
            q.append((tgt[0], tgt[1], path + [_seg(pg, fqn, tgt)], cp))
    return records, seen


def _analyze_bound_plugin(
    pg: "ProjectGraph", node: "ClassNode", plugin_type: str, entry_form: str | None,
    convert_source: str | None, op_type: str | None, const, known: frozenset[str],
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
    const, known: frozenset[str], covered: set[tuple[str, str]], result: AnalysisResult,
) -> None:
    """从一个入口（事件/根方法）跨类回溯，归集字段读写 + 落库判定 + 写入 result。"""
    records, seen = _walk_event(pg, plugin_fqn, event_method, entry_form, convert_source, const, known)
    sink_reachable = any(
        persist.find_sinks(pg.classes[f].cg.methods[m].body)
        for (f, m) in seen if m in pg.classes[f].cg.methods
    )
    has_ext = pg.has_unresolved_external([pgmod.CrossReach(f, m, []) for (f, m) in seen])
    for fqn, method, path, accesses in records:
        covered.add((fqn, method))
        rnode = pg.classes[fqn]
        for acc in accesses:
            if acc.access == "write":
                v = persist.verdict(phase, op_type, sink_reachable, has_external=has_ext)
                persists, reason = v.persists, v.reason
                conf = round(min(acc.confidence, v.confidence), 3)
            else:
                persists, reason, conf = "na", None, acc.confidence
            result.field_accesses.append(FieldAccessRow(
                form_key=acc.entity, field_key=acc.field_key, level=acc.level,
                entry_key=acc.entry_key, plugin_fqn=plugin_fqn, plugin_type=plugin_type,
                access_class=fqn, event_method=event_method, event_phase=phase,
                access=acc.access, persists=persists, persist_reason=reason,
                via=acc.via, line=acc.line, path=path, key_resolution=acc.key_resolution,
                confidence=conf, source_relpath=rnode.relpath, evidence=acc.note,
            ))


def _analyze_unbound_plugin(
    pg: "ProjectGraph", node: "ClassNode", base: str, const, known: frozenset[str],
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
    pg: "ProjectGraph", node: "ClassNode", const, known: frozenset[str],
    plugin_base: dict[str, str], covered: set[tuple[str, str]], result: AnalysisResult,
) -> bool:
    """补全未被插件覆盖的项目类方法的字段读写（来源实体能由 ORM 判出就用，否则 None）。"""
    base = plugin_base.get(node.simple)
    kind, _c, _e = classifier.plugin_kind(None, base)
    is_plugin = kind != "unknown"
    plugin_type = kind if is_plugin else "service"
    emitted = False
    resolver = _RetResolver(pg, const, known, node.fqn, None, None)
    for name, md in node.cg.methods.items():
        if (node.fqn, name) in covered:
            continue
        env = fa._Env(
            const=const, default_entity=None, known_entities=known,
            do_vars=ax.dynamicobject_vars(md.node), do_params=_do_params(md.node),
            do_array_params=_do_array_params(md.node),
            coll_params=_coll_params(md.node), local_seed=resolver.local_seed(node, name),
        )
        accesses, _ = fa.analyze_method(md.body, env)
        if not accesses:
            continue
        info = events.classify_method(kind, name) if is_plugin else None
        phase = info.phase if info else "unknown"
        self_sink = bool(persist.find_sinks(md.body))
        for acc in accesses:
            if acc.access == "write":
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
                access_class=node.fqn, event_method=name, event_phase=phase,
                access=acc.access, persists=persists, persist_reason=reason,
                via=acc.via, line=acc.line, path=[name], key_resolution=acc.key_resolution,
                confidence=acc.confidence, source_relpath=node.relpath, evidence=acc.note,
            ))
            emitted = True
    return emitted
