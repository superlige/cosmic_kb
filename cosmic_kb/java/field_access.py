"""阶段 5（+返工）· 字段读写抽取（两种写入习语 + 层级 + 所属分录 + **数据包来源实体**）。

苍穹里"改字段"有两套写法，本模块都要认，并定位**层级（表头/分录/子分录）+ 所属分录 key +
数据包来源实体（哪张单据/实体）**：

习语 A·界面插件 `getModel().setValue(...)` —— 按**实参个数判层级**（操作的是当前模型=插件
    绑定单据）：
    setValue(key, val) → 表头；+row → 分录；+row+sub → 子分录；getValue 同理为读。

习语 B·DynamicObject 数据包树形赋值（操作/转换/服务类常用）—— 靠**方法内轻量数据流**追每个
    局部/形参/循环变量绑定到哪个数据包，并判定**来源实体**：
    数据包来源（用户 2026-06-17 口径）：
      · 事件入参 `e.getDataEntities()[0]` / `this` → 来源 = 插件绑定单据（由 analyze 注入
        default_entity；转换插件注入目标单）。
      · ORM 加载 `BusinessDataServiceHelper.load("实体", …)` / `loadSingle` / `newDynamicObject`
        / `QueryServiceHelper.query` → 来源 = 实参里的**实体标识**（优先取已知实体）。
      · 转换插件 `extendedDataEntity.getDataEntity()` → 目标单（=default_entity）；
        `getValue(CONVERT_SOURCE)` → 源单（=convert_source）。
    层级/分录：`getDynamicObjectCollection("k")` / `getModel().getEntryEntity("k")` 取分录集合，
    其行再取一层集合 = 子分录；`getDynamicObject("k")` = 基础资料包。集合/行/子行沿链继承来源实体。

字段 key 与实体实参一律经 `constants` 解析（字面量 / 常量引用）。处处置信度、解不出标 unknown。
跨方法（同类 helper / 跨类 service）的来源实体由 analyze 按「实参↔形参」传播注入 param_entities。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import ast_index as ax
from .constants import KeyResolution

if TYPE_CHECKING:
    from tree_sitter import Node
    from .constants import ConstantTable

# 模型写/读 API（getModel() 上的方法）。
_MODEL_WRITE = {"setValue", "setItemValueByID", "setItemValueByNumber"}
_MODEL_READ = {"getValue", "getItemValueByID", "getItemValueByNumber"}
# DynamicObject 写/读 API。
_DO_WRITE = {"set"}
_DO_READ = {
    "get", "getString", "getInt", "getLong", "getBigDecimal", "getDecimal",
    "getDate", "getBoolean", "getDynamicObject",
}

# 从初始化表达式文本里识别数据包来源（轻量、配合 tree-sitter 取实参）。
# 分录/子分录集合、基础资料包、分录的 key 实参——不再只认字符串字面量，连**常量引用**
# （`getDynamicObjectCollection(ContractCon.ENTRY9)`）与**跨方法传来的 String 形参**
# （`getDynamicObjectCollection(periodBillKey)`，调用方传 ContractCon.ENTRY9）一并捕获后解析，
# 否则真实项目里大量用常量/形参当分录 key 的写入会整片判不出层级/分录（用户 2026-06-17 反馈）。
# 注意 `getDynamicObject(` 的 `(` 紧跟，绝不会误匹配 `getDynamicObjectCollection(`。
_GET_COLL_ARG_RE = re.compile(r'getDynamicObjectCollection\(\s*([^,()]+?)\s*\)')
_GET_DO_ARG_RE = re.compile(r'getDynamicObject\(\s*([^,()]+?)\s*\)')
# 待办二习语：`new DynamicObject(coll.getDynamicObjectType())` —— 新行元素归属该集合所代表的分录实体。
_NEW_DO_OF_COLL_RE = re.compile(
    r'new\s+DynamicObject\s*\(\s*(\w+)\s*\.\s*getDynamicObjectType\s*\(\s*\)\s*\)')
_GET_ENTRY_ARG_RE = re.compile(r'getEntryEntity\(\s*([^,()]+?)\s*\)')   # model.getEntryEntity(分录)
_STR_LIT_RE = re.compile(r'^"([^"]*)"$')
# ORM 集合（返回某实体的表头行集合/数组）：load/loadFromCache/query。
_LOAD_COLL_RE = re.compile(
    r'\b(?:BusinessDataServiceHelper\s*\.\s*load(?:FromCache)?'
    r'|QueryServiceHelper\s*\.\s*query)\s*\(')
# ORM 单个数据包（单条表头）：loadSingle/loadSingleFromCache/newDynamicObject/queryOne。
_LOAD_SINGLE_RE = re.compile(r'\b(?:loadSingle(?:FromCache)?|newDynamicObject|queryOne)\s*\(')
# 转换插件源单行：getValue(CONVERT_SOURCE) / getValue(...SOURCE...)。
_GETVALUE_SOURCE_RE = re.compile(r'getValue\(\s*\w*SOURCE\w*\b')
# 事件数据实体（表头包）取法：getDataEntity()/getDataEntities()[i]/getBizDataEntity()/
# extendedDataEntity.getDataEntity()（转换目标单）。注意 getDataEntity 不是 getDataEntities
# 的子串（…tity vs …titi），必须分别匹配。
_EVENT_HEADER_RE = re.compile(r'\bget(?:Biz)?DataEntit(?:y|ies)\b')
# 流式派生集合的来源集合变量：Arrays.stream(X) / X.stream()。
_STREAM_SRC_RE = re.compile(r'Arrays\s*\.\s*stream\(\s*(\w+)|\b(\w+)\s*\.\s*stream\(')
_STREAM_TERM_RE = re.compile(r'\.\s*(?:collect|toList|toSet|toArray)\b')
# 字符串字面量 / 常量风格标识符（实体实参候选）。
_STR_RE = re.compile(r'"([^"]+)"')
_CONST_IDENT_RE = re.compile(r'\b([A-Z][A-Z0-9_]{2,})\b')
# 取数据包根包的硬编码名（事件数据实体常见命名），来源按 default_entity。
_ROOT_NAMES = {"bill", "dataentity", "billobj", "info", "obj", "dynamicobject", "data"}


@dataclass
class _Env:
    """一次方法分析的注入上下文。"""

    const: "ConstantTable"
    default_entity: str | None = None        # 模型/事件入参包的来源实体（插件绑定单据/转换目标单）
    convert_source: str | None = None        # 转换插件源单实体
    # 形参名→完整数据包坐标 (level, entry_key, entity, is_collection)，跨方法/跨类传播（按实参↔形参）。
    param_ctx: dict[str, tuple] = field(default_factory=dict)
    known_entities: frozenset[str] = frozenset()  # 元数据已知实体/单据集（校验 ORM 实参）
    do_vars: frozenset[str] = frozenset()    # DynamicObject 形参/局部/循环变量名（兜底识别）
    do_params: frozenset[str] = frozenset()  # 其中的 DynamicObject 形参名（无 init，先于循环播种）
    do_array_params: frozenset[str] = frozenset()  # DynamicObject[] 数组/变长形参（一组表头行，按集合处理）
    coll_params: frozenset[str] = frozenset()  # DynamicObjectCollection 形参名（其元素行继承传播坐标）
    # List/Set/Collection<DynamicObject> 形参 + 局部变量名：泛型集合，元素=单据表头行。
    # 局部集合若由「空集合 + 循环 .add(已知实体包)」建起，按 add 的实体包推断元素来源实体。
    do_coll_vars: frozenset[str] = frozenset()
    # 模型/视图形参名→其绑定单据：跨类把 getModel()/getView()/this 携带的绑定单据传进来，
    # 使 service 里的 `model.getDataEntity()` 能定位来源单据（否则 default_entity 跨类为 None）。
    model_entities: dict[str, str] = field(default_factory=dict)
    # String 形参名→其字面值：调用方传来的分录/字段 key 常量，供 getDynamicObjectCollection(形参) 解析。
    str_params: dict[str, str] = field(default_factory=dict)
    # 局部变量名→完整数据包坐标 (level, entry_key, entity, is_collection)：调用方法返回的数据包
    # （`bill = this.writeBillToEntry(...)` 返回模型某分录行）由 analyze 按被调方法的「返回上下文」
    # 预解析后注入——否则 helper 造好行/集合再 return 给调用方的写入会整片判不出来源单据。
    local_seed: dict[str, tuple] = field(default_factory=dict)
    # 方法内**迭代变量名**集合（for-each 行变量 / lambda 形参，含 map.forEach((k,v)->) 的 k）：
    # 当字段 key 实参是其中之一，说明是「对运行时/配置决定的动态字段集做泛化写入」（钉不出唯一字段）。
    iter_vars: frozenset[str] = frozenset()
    # 局部变量名→其初始化表达式节点：用于判「拼接键」（`String setKey = CON.X + "_" + type`）。
    local_inits: dict = field(default_factory=dict)


@dataclass
class FieldAccess:
    field_key: str | None          # 解析出的字段 key；解不出为 None
    level: str                     # header | entry | subentry | basedata | unknown
    entry_key: str | None          # 所属分录/子分录 key（表头为 None）
    entity: str | None             # 数据包来源实体/单据标识（判不出为 None）
    access: str                    # read | write
    via: str                       # model.setValue | do.set | model.getValue | do.get ...
    line: int
    key_resolution: str            # literal | constant | ambiguous | unknown | dynamic
    confidence: float
    note: str | None = None
    receiver_var: str | None = None  # 接收者基变量名（do.* 路径填；供 analyze 做同对象共现交集回填）


@dataclass
class _Ctx:
    """一个变量绑定的数据包上下文。"""

    level: str                     # header | entry | subentry | basedata
    entry_key: str | None
    entity: str | None = None      # 数据包来源实体/单据标识
    is_collection: bool = False    # True=集合（其元素才是行，row level = 本 level）
    note: str | None = None        # 层级/来源靠保守推断时的说明（降置信）


def analyze_method(
    method_body: "Node | None", env: _Env,
) -> tuple[list[FieldAccess], dict[str, tuple[str, str | None, str | None]]]:
    """抽取一个方法体内的字段读写 + 返回「变量→完整坐标 (level, entry_key, entity)」映射。

    ctx_map 供调用方按「实参↔形参」做跨方法/跨类的**全坐标**传播（层级/分录/来源一并传）。
    """
    if method_body is None:
        return [], {}
    model_vars, doc_ctx, coll_ctx = _build_contexts(method_body, env)
    # 每方法注入：迭代变量集 + 局部变量初值表（供 null-key 写入按成因细分：动态循环/拼接键）。
    env.iter_vars = frozenset(_iter_var_names(method_body))
    env.local_inits = {lv.name: lv.init for lv in ax.iter_local_vars(method_body) if lv.init is not None}
    out: list[FieldAccess] = []
    for inv in ax.iter_invocations(method_body):
        rec = _classify_access(inv, model_vars, doc_ctx, coll_ctx, env)
        if rec is not None:
            out.append(rec)
    # 行/根包 → 4 元坐标（is_collection=False）；集合 → is_collection=True，供调用方按
    # 「实参↔形参」把行 or 集合的完整坐标传给被调形参（集合形参的元素行继承之）。
    ctx_map: dict[str, tuple] = {
        v: (c.level, c.entry_key, c.entity, False) for v, c in doc_ctx.items()
    }
    for v, c in coll_ctx.items():
        ctx_map.setdefault(v, (c.level, c.entry_key, c.entity, True))
    return out, ctx_map


def method_return_ctx(method_body: "Node | None", env: _Env) -> tuple | None:
    """解析一个方法的「返回数据包上下文」：return 的表达式落在哪张单据/层级/分录上。

    供 analyze 把 `localvar = this.helper(...)` 的 localvar 绑到 helper 的返回坐标（最常见的是
    helper 在模型上 createNewEntryRow 后 `return bills.get(row)` —— 返回的是模型某分录行）。
    多个 return 上下文不一致或解不出 → 返回 None（不臆造）。
    """
    if method_body is None:
        return None
    model_vars, doc_ctx, coll_ctx = _build_contexts(method_body, env)
    found: tuple | None = None
    for expr in _return_exprs(method_body):
        ctx = _resolve_expr_ctx(expr, model_vars, doc_ctx, coll_ctx, env)
        if ctx is None:
            continue
        if found is None:
            found = ctx
        elif found != ctx:
            return None
    return found


def _return_exprs(body: "Node") -> list["Node"]:
    """收集方法体内 return 语句的返回表达式（跳过 lambda/匿名类体内的 return，它们不返回本方法）。"""
    out: list["Node"] = []
    stack = [body]
    while stack:
        n = stack.pop()
        if n.type in ("lambda_expression", "class_body"):
            continue
        if n.type == "return_statement" and n.named_children:
            out.append(n.named_children[0])
        stack.extend(n.children)
    return out


def _resolve_expr_ctx(
    node: "Node", model_vars: set[str], doc_ctx: dict[str, _Ctx],
    coll_ctx: dict[str, _Ctx], env: _Env,
) -> tuple | None:
    """把一个表达式解析成完整坐标 4 元组：本地变量 / 集合取行（get/addNew/iterator）。"""
    text = ax._text(node).strip()
    base = re.sub(r"\[.*?\]$", "", text.split(".", 1)[0].split("(", 1)[0].strip())
    if "." not in text and "(" not in text:        # 裸变量
        if base in doc_ctx:
            c = doc_ctx[base]
            return (c.level, c.entry_key, c.entity, False)
        if base in coll_ctx:
            c = coll_ctx[base]
            return (c.level, c.entry_key, c.entity, True)
        return None
    if re.search(r"\.get\(|\.iterator\b|\.addNew\b", text) and base in coll_ctx:
        c = coll_ctx[base]                          # 集合取行：返回元素行坐标
        return (c.level, c.entry_key, c.entity, False)
    return None


def extract_field_access(
    method_body: "Node | None", const: "ConstantTable",
    *, do_vars: set[str] | None = None, default_entity: str | None = None,
    convert_source: str | None = None,
    param_ctx: dict[str, tuple[str, str | None, str | None]] | None = None,
    known_entities: frozenset[str] | None = None,
) -> list[FieldAccess]:
    """抽取一个方法体内的全部字段读写（便捷封装；只要 accesses）。"""
    env = _Env(
        const=const, default_entity=default_entity, convert_source=convert_source,
        param_ctx=param_ctx or {}, known_entities=known_entities or frozenset(),
        do_vars=frozenset(do_vars or ()),
    )
    return analyze_method(method_body, env)[0]


def _resolve_entity_arg(text: str, env: _Env) -> tuple[str | None, str | None]:
    """从一段调用文本里解析「实体标识」实参（ORM load/query 的实体名）。返回 (实体, note)。

    取字符串字面量与常量风格标识符做候选，优先落在元数据已知实体集里的；都不在则取首个并标注。
    """
    cands: list[str] = list(_STR_RE.findall(text))
    for ident in _CONST_IDENT_RE.findall(text):
        kr = env.const.resolve(ident)
        if kr.value:
            cands.append(kr.value)
    known = [c for c in cands if c in env.known_entities]
    if known:
        return known[0], None
    if cands:
        return cands[0], "来源实体未在元数据中确认（可能跨模块/外部实体）"
    return None, "ORM 实体实参为动态表达式，无法静态解析来源单据"


def _resolve_key_token(token: str, env: "_Env") -> tuple[str | None, str | None]:
    """把分录/基础资料 key 实参解析成字面值：字符串字面量 / 跨方法传来的 String 形参 / 常量引用。

    返回 (key, note)；解不出 key=None 并给出说明（API 名已证明是分录集合，层级仍可信，只是分录
    key 未定位——降置信、走存疑桶，不臆造一个 key）。
    """
    token = token.strip()
    m = _STR_LIT_RE.match(token)
    if m:
        return m.group(1), None
    if token in env.str_params:
        return env.str_params[token], None
    kr = env.const.resolve(token)
    if kr.value:
        return kr.value, None
    return None, "分录 key 为变量/外部常量，无法静态解析（层级可信，分录 key 未定位）"


def _model_entity(base_var: str, env: "_Env") -> str | None:
    """取模型/视图/事件包接收者的来源实体：跨类传来的模型形参用其绑定单据，否则回落
    default_entity（插件绑定单据；事件入参 getDataEntity/getModel/this 即走此回落）。"""
    b = re.sub(r"\[.*?\]$", "", base_var.split("(", 1)[0].strip())
    if b in env.model_entities:
        return env.model_entities[b]
    return env.default_entity


def _build_contexts(body: "Node", env: _Env) -> tuple[set[str], dict[str, _Ctx], dict[str, _Ctx]]:
    """方法内轻量数据流：扫局部变量声明 + 增强 for，建 {模型变量集, 数据包变量→上下文}。

    定点迭代到稳定（上下文有依赖链：集合→行→子集合→子行；load 数组→行）。
    """
    model_vars: set[str] = set()
    coll_ctx: dict[str, _Ctx] = {}   # 集合变量 → 其元素行的上下文
    doc_ctx: dict[str, _Ctx] = {}    # 行/根/基础资料数据包变量 → 上下文

    locals_ = list(ax.iter_local_vars(body))
    foreach = _foreach_pairs(body)   # [(行变量, 集合变量)]
    pend_names = {lv.name for lv in locals_} | {row for row, _ in foreach}

    # DynamicObject 形参先于定点迭代播种（形参在方法体内无 init，其完整坐标可能由调用方传播注入）
    # ——使后续 `param.getDynamicObjectCollection(...)` 继承到正确的层级/分录/来源。
    for v in env.do_params:
        if v not in doc_ctx:
            pc = env.param_ctx.get(v)
            if pc:
                doc_ctx[v] = _Ctx(pc[0], pc[1], pc[2])   # 传播来的完整坐标，置信
            else:
                doc_ctx[v] = _Ctx("header", None, None,
                                  note="DynamicObject 入参，调用方未知，来源单据/层级未定位")

    # DynamicObjectCollection 形参：调用方传来的若是集合坐标，则其元素行继承之（供 for-each/lambda
    # 取行）——修复「插件取 dataEntity.getDynamicObjectCollection(分录) 后整集合传给 service 改字段」
    # 时 service 内分录归属丢失的问题。
    for v in env.coll_params:
        if v not in coll_ctx and v not in doc_ctx:
            pc = env.param_ctx.get(v)
            if pc and len(pc) > 3 and pc[3]:
                coll_ctx[v] = _Ctx(pc[0], pc[1], pc[2], is_collection=True)

    # DynamicObject[] 数组/变长入参：语义是**一组行**（最常见的是 e.getDataEntities() 传进来的
    # 表头数组），按集合播种——其 for-each 行 / `entities[i]` 元素才继承坐标。调用方传来完整坐标
    # 就用之（含层级/分录/来源单据），否则按表头集合、来源未定位（不臆造）。
    for v in env.do_array_params:
        if v in coll_ctx or v in doc_ctx:
            continue
        pc = env.param_ctx.get(v)
        if pc:
            coll_ctx[v] = _Ctx(pc[0], pc[1], pc[2], is_collection=True)
        else:
            coll_ctx[v] = _Ctx("header", None, None, is_collection=True,
                               note="DynamicObject[] 入参，调用方未知，来源单据未定位")

    # 调用返回数据包注入：`localvar = this.helper(...)` 的 helper 返回某行/集合，analyze 已按
    # 被调方法的返回上下文解析成完整坐标。先于定点迭代播种，使后续对该变量的取分录/取行/set 继承之。
    for v, c in env.local_seed.items():
        if v in doc_ctx or v in coll_ctx:
            continue
        if len(c) > 3 and c[3]:
            coll_ctx[v] = _Ctx(c[0], c[1], c[2], is_collection=True)
        else:
            doc_ctx[v] = _Ctx(c[0], c[1], c[2])

    def _pending(base_var: str) -> bool:
        return base_var in pend_names and base_var not in doc_ctx and base_var not in coll_ctx

    def _base_entity(base_var: str) -> str | None:
        """取某基包变量的来源实体（数据流已知 / 模型即默认实体 / 否则 None）。"""
        if base_var in doc_ctx:
            return doc_ctx[base_var].entity
        if base_var in model_vars or "getModel()" in base_var or base_var == "this":
            return env.default_entity
        return None

    # 泛型集合 `.add(x)`/`.addAll(x)` 累积：预扫每个 List/Set<DynamicObject> 变量被 add 进来的
    # 实参基变量（含内联 ORM `add(loadSingle(id,"实体"))`），供定点迭代里按元素来源实体收敛。
    add_args: dict[str, list[tuple[str, str | None]]] = {}
    if env.do_coll_vars:
        for inv in ax.iter_invocations(body):
            if inv.name not in ("add", "addAll") or not inv.args:
                continue
            recv = re.sub(r"\[.*?\]$", "", inv.object_text.split(".", 1)[0].split("(", 1)[0].strip())
            if recv not in env.do_coll_vars:
                continue
            argtxt = ax._text(inv.args[0]).strip()
            argbase = re.sub(r"\[.*?\]$", "", argtxt.split(".", 1)[0].split("(", 1)[0].strip())
            inline_ent: str | None = None
            if _LOAD_SINGLE_RE.search(argtxt) or _LOAD_COLL_RE.search(argtxt):
                inline_ent, _ = _resolve_entity_arg(argtxt, env)   # 内联 add(loadSingle(...,"实体"))
            add_args.setdefault(recv, []).append((argbase, inline_ent))

    def _accum_coll_entity(bases: list[tuple[str, str | None]]) -> tuple[str | None, bool]:
        """由 add 进来的实参集合推断元素来源实体。返回 (实体, pending)。

        所有 add 的实参一致解析到**同一个**已知实体才采纳（红线 #4：来源混杂/解不出留 None 不臆造）；
        仍有实参待定（其声明在本方法、尚未解析）则 pending=True，下一轮再判。
        """
        ents: set[str | None] = set()
        for base, inline_ent in bases:
            if inline_ent is not None:
                ents.add(inline_ent)
            elif base in doc_ctx:
                ents.add(doc_ctx[base].entity)
            elif base in coll_ctx:
                ents.add(coll_ctx[base].entity)
            elif base in pend_names:
                return None, True
            else:
                ents.add(None)
        if len(ents) == 1 and None not in ents:
            return next(iter(ents)), False
        return None, False

    for _ in range(len(locals_) + len(foreach) + 2):
        changed = False
        for lv in locals_:
            if lv.init is None or lv.name in doc_ctx or lv.name in coll_ctx:
                continue
            init_text = ax._text(lv.init)
            base_var = init_text.split(".", 1)[0].strip()

            # 流式派生集合：Arrays.stream(X)…collect / X.stream()…collect → 继承 X 的元素上下文。
            # **必须先于 getDynamicObjectCollection 判定**：stream 的 lambda 体内常含
            # `o.getDynamicObjectCollection(子分录)`，若让下面的正则在整段 init 文本里命中它，会把
            # 整个 stream 结果误判成一次取分录集合、丢掉来源单据（真实项目 ContractUpdateAssetOp
            # 的 `bills.stream().filter(o->o.getDynamicObjectCollection(..)).collect()` 即此坑）。
            m_sd = _STREAM_SRC_RE.search(init_text)
            if m_sd and _STREAM_TERM_RE.search(init_text):
                src = m_sd.group(1) or m_sd.group(2)
                if _pending(src):
                    continue                              # 源集合未解析完，下一轮再来
                if src in coll_ctx:
                    c = coll_ctx[src]
                    coll_ctx[lv.name] = _Ctx(c.level, c.entry_key, c.entity,
                                             is_collection=True, note=c.note)
                    changed = True
                continue          # stream 结果整体消费，绝不落到下面的取分录/取行误判

            # 分录/子分录集合：getDynamicObjectCollection(k) / model.getEntryEntity(k)
            # （k 可为字面量/常量/传播来的 String 形参）。
            m_coll = _GET_COLL_ARG_RE.search(init_text)
            m_entry = _GET_ENTRY_ARG_RE.search(init_text)
            if m_coll or m_entry:
                if m_coll and _pending(base_var):
                    continue
                key, knote = _resolve_key_token((m_coll or m_entry).group(1), env)
                if m_entry:                                  # model.getEntryEntity → 该模型单据分录
                    coll_ctx[lv.name] = _Ctx("entry", key, _model_entity(base_var, env),
                                             is_collection=True, note=knote)
                    changed = True
                    continue
                base_is_entry_row = (
                    base_var in doc_ctx and doc_ctx[base_var].level in ("entry", "subentry")
                )
                lvl = "subentry" if base_is_entry_row else "entry"
                coll_ctx[lv.name] = _Ctx(lvl, key, _base_entity(base_var),
                                         is_collection=True, note=knote)
                changed = True
                continue

            # ORM 集合（load/query → 表头行集合）。
            if _LOAD_COLL_RE.search(init_text):
                ent, note = _resolve_entity_arg(init_text, env)
                coll_ctx[lv.name] = _Ctx("header", None, ent, is_collection=True, note=note)
                changed = True
                continue
            # ORM 单个数据包（loadSingle/newDynamicObject → 单条表头）。
            if _LOAD_SINGLE_RE.search(init_text):
                ent, note = _resolve_entity_arg(init_text, env)
                doc_ctx[lv.name] = _Ctx("header", None, ent, note=note)
                changed = True
                continue
            # 转换插件源单行集合：getValue(CONVERT_SOURCE)。
            if env.convert_source and _GETVALUE_SOURCE_RE.search(init_text):
                coll_ctx[lv.name] = _Ctx("header", None, env.convert_source, is_collection=True)
                changed = True
                continue

            # 基础资料包：getDynamicObject(字段)。
            m_do = _GET_DO_ARG_RE.search(init_text)
            if m_do:
                bd_key, _kn = _resolve_key_token(m_do.group(1), env)
                doc_ctx[lv.name] = _Ctx("basedata", bd_key, _base_entity(base_var),
                                        note="基础资料引用包，写入归该基础资料实体")
                changed = True
                continue
            # 事件数据实体（表头）：getDataEntity()/getDataEntities()[i]/getBizDataEntity() / this。
            # getDataEntities()（数组、无下标）→ 表头行**集合**，供 for-each/lambda 取行；
            # getDataEntity() / getDataEntities()[i] → 单个表头包。
            if _EVENT_HEADER_RE.search(init_text) or init_text.strip() == "this":
                txt = init_text.strip()
                # 接收者可能是跨类传来的 model 形参（model.getDataEntity()），来源取其绑定单据。
                ent = _model_entity(base_var, env)
                if re.search(r"getDataEntities\b", txt) and not re.search(r"getDataEntities\(\)\s*\[", txt):
                    coll_ctx[lv.name] = _Ctx("header", None, ent, is_collection=True)
                else:
                    doc_ctx[lv.name] = _Ctx("header", None, ent)
                changed = True
                continue
            if init_text.rstrip().endswith("getModel()"):
                model_vars.add(lv.name)
                changed = True
                continue
            # 待办二习语：`new DynamicObject(coll.getDynamicObjectType())` —— 新行的类型取自某集合，
            # 元素即归该集合所代表的分录实体（与 addNew() 同理，集合未解析则留 None，红线 #4）。
            m_new = _NEW_DO_OF_COLL_RE.search(init_text)
            if m_new:
                coll_var = m_new.group(1)
                if _pending(coll_var):
                    continue
                if coll_var in coll_ctx:
                    c = coll_ctx[coll_var]
                    doc_ctx[lv.name] = _Ctx(c.level, c.entry_key, c.entity, note=c.note)
                    changed = True
                continue

            # 行变量：引用某集合 .get(i) / .iterator() / .addNew()（新增行同样是该集合的元素行）。
            if re.search(r"\bget\(|\.iterator\b|\.addNew\b", init_text):
                if _pending(base_var):
                    continue
                if base_var in coll_ctx:
                    c = coll_ctx[base_var]
                    doc_ctx[lv.name] = _Ctx(c.level, c.entry_key, c.entity, note=c.note)
                    changed = True
                    continue

        for cv, bases in add_args.items():  # 泛型集合：由 .add(已知实体包) 推断元素来源实体
            if cv in coll_ctx or cv in doc_ctx:
                continue
            ent, pend = _accum_coll_entity(bases)
            if pend or ent is None:
                continue
            coll_ctx[cv] = _Ctx("header", None, ent, is_collection=True,
                                note="泛型集合，元素来源由 add(已知实体包) 推断")
            changed = True

        for row_var, coll_var in foreach:  # for-each 行继承集合元素上下文（含 load 数组）
            if row_var not in doc_ctx and coll_var in coll_ctx:
                c = coll_ctx[coll_var]
                doc_ctx[row_var] = _Ctx(c.level, c.entry_key, c.entity, note=c.note)
                changed = True
        if not changed:
            break

    # Lambda 行变量：coll.forEach(o -> o.set(...)) / Arrays.stream(coll).forEach(o -> …)
    # —— o 是 lambda 形参，既非局部声明也非 for-each 变量，不识别会整片漏（用户 2026-06-17
    # 反馈的 CollateralService.exStartCollateral 即此类）。绑定到来源集合的元素上下文。
    for params, coll_var in _lambda_pairs(body):
        if coll_var in coll_ctx:
            c = coll_ctx[coll_var]
            for pn in params:
                doc_ctx.setdefault(pn, _Ctx(c.level, c.entry_key, c.entity, note=c.note))

    # DynamicObject 局部/循环变量：上面没认出明确数据流的（非入参/非 ORM/非事件包），按表头
    # 保守归位、来源未定位（形参已先播种）。诚实留证据，供精确查询走"可能命中（存疑）"桶。
    for v in env.do_vars - env.do_params - env.do_array_params:
        if v not in doc_ctx and v not in coll_ctx and v not in model_vars:
            doc_ctx[v] = _Ctx("header", None, None,
                              note="数据包来源未识别（非入参/非 ORM 查询），层级/来源单据未定位")
    return model_vars, doc_ctx, coll_ctx


def _lambda_pairs(body: "Node") -> list[tuple[list[str], str]]:
    """收集 lambda 行变量：返回 [(形参名列表, 来源集合变量)]。

    只认作为 forEach/map/peek/filter 等流/集合方法实参的 lambda（其形参=集合元素）；来源集合
    取该方法调用的接收者里的基集合变量（Arrays.stream(X) → X；X.stream()…/ X.forEach → X）。
    """
    out: list[tuple[list[str], str]] = []
    stack = [body]
    while stack:
        n = stack.pop()
        if n.type == "lambda_expression":
            params = _lambda_param_names(n)
            coll = _lambda_collection_var(n)
            if params and coll:
                out.append((params, coll))
        stack.extend(n.children)
    return out


def _lambda_param_names(node: "Node") -> list[str]:
    pnode = node.child_by_field_name("parameters")
    if pnode is None:
        return []
    if pnode.type == "identifier":
        return [ax._text(pnode)]
    names: list[str] = []
    for c in pnode.named_children:
        if c.type == "identifier":
            names.append(ax._text(c))
        elif c.type in ("formal_parameter", "spread_parameter"):
            nn = c.child_by_field_name("name")
            if nn is not None:
                names.append(ax._text(nn))
    return names


def _lambda_collection_var(node: "Node") -> str | None:
    """从 lambda 所在的方法调用接收者里提取来源集合变量。"""
    p = node.parent
    while p is not None and p.type != "method_invocation":
        if p.type in ("block", "method_declaration", "lambda_expression"):
            return None
        p = p.parent
    if p is None:
        return None
    recv = ax._text(p.child_by_field_name("object"))
    m = re.search(r'Arrays\s*\.\s*stream\(\s*(\w+)', recv)
    if m:
        return m.group(1)
    base = recv.split(".", 1)[0].split("(", 1)[0].strip()
    return base or None


def _foreach_pairs(body: "Node") -> list[tuple[str, str]]:
    """收集增强 for：for (DynamicObject row : coll) → [(row, coll)]。"""
    out: list[tuple[str, str]] = []
    stack = [body]
    while stack:
        n = stack.pop()
        if n.type == "enhanced_for_statement":
            name_node = n.child_by_field_name("name")
            iter_node = n.child_by_field_name("value")
            if name_node is not None and iter_node is not None:
                out.append((
                    ax._text(name_node),
                    ax._text(iter_node).split(".", 1)[0].strip(),
                ))
        stack.extend(n.children)
    return out


def _iter_var_names(body: "Node") -> set[str]:
    """方法内**迭代变量名**集合：for-each 行变量 + 所有 lambda 形参（含 `map.forEach((k,v)->)` 的 k）。

    当字段 key 实参恰是其中之一，说明这是「循环遍历一个运行时/配置决定的字段集合做泛化读写」——
    每轮 key 不同，静态钉不出唯一字段（如 `for(String f:coll) set(f,..)`、`map.forEach((k,v)->set(k,..))`）。
    """
    names = {row for row, _ in _foreach_pairs(body)}
    stack = [body]
    while stack:
        n = stack.pop()
        if n.type == "lambda_expression":
            names.update(_lambda_param_names(n))
        stack.extend(n.children)
    return names


def _concat_known_parts(node: "Node", env: "_Env") -> list[str]:
    """从一个二元拼接表达式里收集**静态可知的段**：字符串字面量 + 能解析回字面值的常量引用。

    供「拼接键」给出已知前缀（`CON.HANGUP_LATEST + "_" + type` → 已知段 [hangup_latest, _]），
    帮段二大模型缩小阅读范围；运行时变量段（type）静态留空，不臆造。
    """
    parts: list[str] = []
    has_string = [False]

    def _walk(n: "Node") -> None:
        if n.type == "string_literal":
            has_string[0] = True
            parts.append(ax.string_value(n) or "")
            return                                  # 叶子，不再下钻
        if n.type in ("identifier", "field_access", "scoped_identifier"):
            kr = env.const.resolve(ax._text(n))     # 整体解析；命中即收，不下钻避免内层重复计
            if kr.value:
                parts.append(kr.value)
            return
        for c in n.children:                        # 按源码顺序遍历（不用 stack 反序）
            _walk(c)

    _walk(node)
    return parts if has_string[0] else []


def _refine_null_key(inv: ax.Invocation, kr: "KeyResolution | None", env: "_Env") -> "KeyResolution | None":
    """把「解不出字段 key」的访问按成因细分 key_resolution（field_key 仍为 None，诚实不臆造）。

    成因四态（处处置信度·红线 #4）：
      · concat        —— 字段 key 由字符串拼接而成（`CON.X + "_" + v` 或局部变量持拼接结果）。
      · dynamic-loop  —— key 是方法内迭代变量，循环写一个运行时/配置决定的字段集（泛化写入）。
      · external-const—— key 是未命中常量表的 `UPPER_CONST`/`类.常量`（跨模块/外部常量）。
      · unknown       —— 其余（多为小写局部变量），保持现状不强分。
    已解析的（literal/constant/ambiguous）与无法归因的非拼接表达式原样返回（后者 DO 路径仍跳过）。
    """
    # 实参是表达式（既非字面量也非标识符）：只认「字符串拼接」，其余原样返回 None（DO 路径据此跳过）。
    if kr is None:
        arg = inv.args[0] if inv.args else None
        if arg is not None and arg.type == "binary_expression":
            known = _concat_known_parts(arg, env)
            note = "拼接键，运行时拼接" + (f"（已知段 {'+'.join(known)}）" if known else "")
            return KeyResolution(None, "concat", 0.3, note=note)
        return None
    if kr.kind != "unknown":          # literal / constant / ambiguous 不动
        return kr
    ident = ax.arg_identifier(inv, 0) or ""
    last = ident.rsplit(".", 1)[-1]
    if ident in env.iter_vars:
        return KeyResolution(None, "dynamic-loop", 0.3,
                             note=f"动态循环写入：迭代变量 {ident}，字段集由运行时/配置/元数据决定")
    init = env.local_inits.get(ident)
    if init is not None and init.type == "binary_expression":
        known = _concat_known_parts(init, env)
        if known:                     # 局部变量持拼接结果（`String setKey = CON.X + "_" + v`）
            return KeyResolution(None, "concat", 0.3,
                                 note=f"拼接键 {ident}（已知段 {'+'.join(known)}）")
    if re.fullmatch(r"[A-Z][A-Z0-9_]+", last):
        return KeyResolution(None, "external-const", 0.3,
                             note=f"外部/跨模块常量 {ident}（不在扫描范围，未命中常量表）")
    return kr                         # 小写局部变量等 → 保持 unknown


def _is_model_receiver(object_text: str, model_vars: set[str]) -> bool:
    t = object_text.strip()
    return t.endswith("getModel()") or t in model_vars


def _classify_access(
    inv: ax.Invocation, model_vars: set[str], doc_ctx: dict[str, _Ctx],
    coll_ctx: dict[str, _Ctx], env: _Env,
) -> FieldAccess | None:
    name = inv.name
    obj = inv.object_text.strip()

    # ── 习语 A：模型 API（getModel().setValue/getValue）—— 操作当前模型=绑定单据 ──────
    if name in _MODEL_WRITE or name in _MODEL_READ:
        if not _is_model_receiver(obj, model_vars):
            return None
        kr = _refine_null_key(inv, env.const.resolve_arg(inv, 0), env)
        is_write = name in _MODEL_WRITE
        if is_write:
            level = {2: "header", 3: "entry", 4: "subentry"}.get(inv.arg_count, "unknown")
        else:
            level = {1: "header", 2: "entry", 3: "subentry"}.get(inv.arg_count, "unknown")
        return _make(kr, level, None, env.default_entity,
                     "write" if is_write else "read", f"model.{name}", inv.line)

    # ── 习语 B：DynamicObject 数据包 set/get ──────────────────────────────
    if name in _DO_WRITE or name in _DO_READ:
        ctx = _resolve_do_ctx(obj, doc_ctx, coll_ctx, env)
        if ctx is None:
            return None
        kr = _refine_null_key(inv, env.const.resolve_arg(inv, 0), env)
        if kr is None:   # 既非字段 key、又非可归因的拼接/动态表达式 → 不记录（保持原行为）
            return None
        is_write = name in _DO_WRITE
        # 接收者基变量名（去数组下标）：供 analyze 的同对象共现交集回填按变量分组求交。
        recv = re.sub(r"\[.*?\]$", "", obj.split(".", 1)[0].strip().split("(", 1)[0].strip())
        return _make(kr, ctx.level, ctx.entry_key, ctx.entity,
                     "write" if is_write else "read", f"do.{name}", inv.line,
                     ctx_note=ctx.note, receiver_var=recv or None)
    return None


def _resolve_do_ctx(
    object_text: str, doc_ctx: dict[str, _Ctx], coll_ctx: dict[str, _Ctx], env: _Env,
) -> _Ctx | None:
    """判定一个 DynamicObject set/get 的接收者上下文（层级 + 分录 key + 来源实体）。"""
    base_raw = object_text.split(".", 1)[0].strip().split("(", 1)[0].strip()
    has_index = bool(re.search(r"\[.*?\]$", base_raw))
    base = re.sub(r"\[.*?\]$", "", base_raw)   # 去数组下标 entities[0] → entities
    if base in doc_ctx:
        return doc_ctx[base]
    # `entities[i].set(...)`：数组/集合下标取元素行，继承该集合的元素坐标。
    if has_index and base in coll_ctx:
        c = coll_ctx[base]
        return _Ctx(c.level, c.entry_key, c.entity, note=c.note)
    m = _GET_COLL_ARG_RE.search(object_text)
    if m:
        key, knote = _resolve_key_token(m.group(1), env)
        return _Ctx("entry", key, _model_entity(base, env), note=knote)
    if base.lower() in _ROOT_NAMES:
        return _Ctx("header", None, env.default_entity)
    return None


def _make(kr, level: str, entry_key: str | None, entity: str | None,
          access: str, via: str, line: int, *, ctx_note: str | None = None,
          receiver_var: str | None = None) -> FieldAccess:
    if kr is None:
        return FieldAccess(None, level, entry_key, entity, access, via, line, "dynamic", 0.3,
                           note=_join_note("字段 key 为表达式/拼接，无法静态解析", ctx_note),
                           receiver_var=receiver_var)
    conf = kr.confidence * (0.7 if ctx_note else 1.0)
    return FieldAccess(kr.value, level, entry_key, entity, access, via, line,
                       kr.kind, round(conf, 3), note=_join_note(kr.note, ctx_note),
                       receiver_var=receiver_var)


def _join_note(*notes: str | None) -> str | None:
    parts = [n for n in notes if n]
    return "；".join(parts) if parts else None
