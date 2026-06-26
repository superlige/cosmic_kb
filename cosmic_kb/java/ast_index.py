"""阶段 5 · Java AST 语义遍历工具（tree-sitter，错误恢复）。

`parser.py` 只算「解析状态」（阶段1 信任门槛），不抽语义。本模块在它之上提供
**语义遍历助手**：类声明、方法声明、方法调用、字符串字面量、`static final String`
常量、局部变量声明——供 `constants / event_extractor / field_access / call_graph /
persistence` 复用。

设计约束（守红线）：
    - 野生代码、可能不可编译 —— 依赖 tree-sitter 错误恢复，partial 树照样遍历，绝不崩。
    - 只读 AST、不做类型解析、不碰外部符号（kd.bos.* 等留给后续按 SDK 解释）。
    - 行号一律 1-based，直接可对照源码（与 parser.py 的 ErrorSpan 口径一致）。
    - tree-sitter 未装时 parse_tree 返回 None，调用方据此跳过（由可信度报告如实呈现）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator

from .parser import get_parser

if TYPE_CHECKING:
    from tree_sitter import Node

# 类型声明节点。
_TYPE_DECL = {
    "class_declaration", "interface_declaration",
    "enum_declaration", "record_declaration", "annotation_type_declaration",
}
# 去泛型：反复消最内层 <...>（复用 namespace 的口径）。
_GENERIC_RE = re.compile(r"<[^<>]*>")
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def parse_tree(text: str | None) -> "Node | None":
    """解析源码文本，返回 root_node；tree-sitter 未装或无文本时返回 None。"""
    if not text:
        return None
    parser = get_parser()
    if parser is None:
        return None
    try:
        return parser.parse(text.encode("utf-8")).root_node
    except Exception:  # 极端脏数据，宁可跳过也不崩
        return None


def _text(node: "Node | None") -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _line(node: "Node") -> int:
    """节点起始行（1-based）。"""
    return node.start_point[0] + 1


def simple_type_name(ref: str) -> str | None:
    """类型引用归一成简单名：去泛型/数组/通配，取末段。复用 namespace 口径。"""
    prev = None
    while prev != ref:
        prev = ref
        ref = _GENERIC_RE.sub("", ref)
    ref = ref.replace("[]", "").replace("?", "").strip()
    if not ref:
        return None
    last = ref.rsplit(".", 1)[-1].strip()
    return last if re.fullmatch(r"[A-Za-z_$][\w$]*", last) else None


# ── 类型声明 ──────────────────────────────────────────────────────

@dataclass
class TypeDecl:
    name: str
    kind: str                      # class/interface/enum/record/annotation
    supers: list[str]              # extends + implements 的父类型简单名
    node: "Node"                   # 声明节点
    body: "Node | None"            # 类体节点（class_body/interface_body…）
    start_line: int


def _supers_of(decl: "Node") -> list[str]:
    """从 superclass / super_interfaces / extends_interfaces 子句抽父类型简单名。"""
    supers: list[str] = []
    for fname in ("superclass", "interfaces", "super_interfaces"):
        fnode = decl.child_by_field_name(fname)
        if fnode is None:
            continue
        for tnode in _walk_type_idents(fnode):
            s = simple_type_name(_text(tnode))
            if s:
                supers.append(s)
    return supers


def _walk_type_idents(node: "Node") -> Iterator["Node"]:
    """收集子树里的 type_identifier / scoped_type_identifier（父类型列表用）。"""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
            yield n
            continue  # 不再下钻泛型内部
        stack.extend(n.children)


def iter_type_declarations(root: "Node") -> Iterator[TypeDecl]:
    """遍历**顶层**类型声明（root 的直接子节点；不含内部类）。"""
    for child in root.children:
        if child.type in _TYPE_DECL:
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            yield TypeDecl(
                name=_text(name_node),
                kind=child.type.split("_", 1)[0],
                supers=_supers_of(child),
                node=child,
                body=child.child_by_field_name("body"),
                start_line=_line(child),
            )


# ── 方法声明 ──────────────────────────────────────────────────────

@dataclass
class MethodDecl:
    name: str
    param_count: int
    param_types: list[str]         # 形参类型简单名（按序；解不出留空串）
    node: "Node"
    body: "Node | None"
    start_line: int
    end_line: int


def iter_methods(type_decl: TypeDecl) -> Iterator[MethodDecl]:
    """遍历一个类型体内**直接声明**的方法（不下钻内部类的方法）。"""
    body = type_decl.body
    if body is None:
        return
    for child in body.children:
        if child.type != "method_declaration":
            continue
        name_node = child.child_by_field_name("name")
        if name_node is None:
            continue
        params = child.child_by_field_name("parameters")
        ptypes = _param_types(params)
        yield MethodDecl(
            name=_text(name_node),
            param_count=len(ptypes),
            param_types=ptypes,
            node=child,
            body=child.child_by_field_name("body"),
            start_line=_line(child),
            end_line=child.end_point[0] + 1,
        )


def _param_types(params: "Node | None") -> list[str]:
    if params is None:
        return []
    out: list[str] = []
    for p in params.named_children:
        if p.type in ("formal_parameter", "spread_parameter", "receiver_parameter"):
            t = p.child_by_field_name("type")
            out.append(simple_type_name(_text(t)) or "" if t is not None else "")
    return out


def iter_param_vars(method_node: "Node") -> Iterator[tuple[str, str]]:
    """方法形参 (变量名, 类型简单名)。供跨类调用的接收者类型解析与 DynamicObject 入参识别。"""
    params = method_node.child_by_field_name("parameters")
    if params is None:
        return
    for p in params.named_children:
        if p.type not in ("formal_parameter", "spread_parameter"):
            continue
        name_node = p.child_by_field_name("name")
        type_node = p.child_by_field_name("type")
        if name_node is not None:
            yield _text(name_node), (simple_type_name(_text(type_node)) or "")


def iter_param_vars_raw(method_node: "Node") -> Iterator[tuple[str, str, bool]]:
    """方法形参 (变量名, 类型简单名, 是否数组/变长)。

    `simple_type_name` 会把 `DynamicObject[]` 归一成 `DynamicObject`，丢掉「数组」信息——
    而数组入参（`e.getDataEntities()` 的 `DynamicObject[]`）语义是**一组表头行**，与单个数据包
    完全不同（其元素才是行）。本函数额外返回 is_array，供 field_access 把数组入参当集合处理。
    """
    params = method_node.child_by_field_name("parameters")
    if params is None:
        return
    for p in params.named_children:
        if p.type not in ("formal_parameter", "spread_parameter"):
            continue
        name_node = p.child_by_field_name("name")
        type_node = p.child_by_field_name("type")
        if name_node is None:
            continue
        is_array = p.type == "spread_parameter" or "[]" in _text(type_node) or (
            p.child_by_field_name("dimensions") is not None)
        yield _text(name_node), (simple_type_name(_text(type_node)) or ""), is_array


def iter_local_var_types(body: "Node | None") -> Iterator[tuple[str, str]]:
    """方法体内局部变量 (变量名, 声明类型简单名)。供接收者类型解析与 DynamicObject 局部识别。"""
    if body is None:
        return
    stack = [body]
    while stack:
        n = stack.pop()
        if n.type == "local_variable_declaration":
            type_node = n.child_by_field_name("type")
            simple = simple_type_name(_text(type_node)) or ""
            for vd in n.children:
                if vd.type == "variable_declarator":
                    name_node = vd.child_by_field_name("name")
                    if name_node is not None:
                        yield _text(name_node), simple
        stack.extend(n.children)


def iter_foreach_var_types(body: "Node | None") -> Iterator[tuple[str, str]]:
    """增强 for 循环变量 (变量名, 声明类型简单名)。`for (DynamicObject row : coll)` 的 row
    不在 local_variable_declaration 里，单独抽，避免 DynamicObject 行变量漏网。"""
    if body is None:
        return
    stack = [body]
    while stack:
        n = stack.pop()
        if n.type == "enhanced_for_statement":
            name_node = n.child_by_field_name("name")
            type_node = n.child_by_field_name("type")
            if name_node is not None:
                yield _text(name_node), (simple_type_name(_text(type_node)) or "")
        stack.extend(n.children)


def dynamicobject_vars(method_node: "Node") -> set[str]:
    """方法内类型为 DynamicObject 的形参 + 局部变量 + 增强 for 循环变量名集合。

    服务/工具类常把单据数据包当入参传进来再 `set(字段, 值)`，ORM load 的数组又常被 for-each
    遍历——这些接收者既不是 getModel() 也不在习语 B 的硬编码名集里，不识别会整片漏掉
    （用户 2026-06-17 反馈的 CollateralService / load+循环 即此类）。识别后按表头保守归位
    （无法静态判断它是表头还是分录行；有数据流上下文时以上下文为准）。
    """
    out: set[str] = set()
    for name, t in iter_param_vars(method_node):
        if t == "DynamicObject":
            out.add(name)
    body = method_node.child_by_field_name("body")
    for name, t in iter_local_var_types(body):
        if t == "DynamicObject":
            out.add(name)
    for name, t in iter_foreach_var_types(body):
        if t == "DynamicObject":
            out.add(name)
    return out


# 泛型集合「装 DynamicObject」的判定：`List<DynamicObject>` / `Set<DynamicObject>` /
# `Collection<DynamicObject>` 等。`<\s*DynamicObject\s*>` 精确匹配元素恰为 DynamicObject
# （故 `List<DynamicObjectCollection>` 因元素名带 Collection 不会误命中；`Map<String,DynamicObject>`
# 因 `<` 后非紧跟 DynamicObject 也不命中——保守，只认最常见的「一串单据表头行」语义）。
_DO_COLL_RE = re.compile(r"<\s*DynamicObject\s*>")


def _is_do_collection_type(type_text: str) -> bool:
    return bool(_DO_COLL_RE.search(type_text or ""))


def dynamicobject_collection_params(method_node: "Node") -> set[str]:
    """`List/Set/Collection<DynamicObject>` 形参名集合（泛型集合，语义=一组单据表头行）。

    现有 `DynamicObjectCollection`/`DynamicObject[]` 形参已分别处理；而真实项目里传查询结果
    最常用的是 `List<DynamicObject>`，三种都不是、整片漏掉来源——补这一档，使其与
    DynamicObjectCollection 同样走「实参↔形参」坐标传播。
    """
    out: set[str] = set()
    params = method_node.child_by_field_name("parameters")
    if params is None:
        return out
    for p in params.named_children:
        if p.type not in ("formal_parameter", "spread_parameter"):
            continue
        name_node = p.child_by_field_name("name")
        type_node = p.child_by_field_name("type")
        if name_node is not None and type_node is not None and _is_do_collection_type(_text(type_node)):
            out.add(_text(name_node))
    return out


def dynamicobject_collection_vars(method_node: "Node") -> set[str]:
    """`List/Set/Collection<DynamicObject>` 形参 + 局部变量名集合（泛型集合）。

    供 field_access 对「空集合 + 循环 `.add(已知实体包)` 累积」的局部集合推断元素来源实体
    （`List<DynamicObject> l=new ArrayList<>(); l.add(loadSingle(id,\"cqkd_ht\"))` → l 装 cqkd_ht）。
    """
    out: set[str] = set(dynamicobject_collection_params(method_node))
    body = method_node.child_by_field_name("body")
    if body is not None:
        stack = [body]
        while stack:
            n = stack.pop()
            if n.type == "local_variable_declaration":
                type_node = n.child_by_field_name("type")
                if type_node is not None and _is_do_collection_type(_text(type_node)):
                    for vd in n.children:
                        if vd.type == "variable_declarator":
                            name_node = vd.child_by_field_name("name")
                            if name_node is not None:
                                out.add(_text(name_node))
            stack.extend(n.children)
    return out


def iter_member_field_types(type_decl: TypeDecl) -> Iterator[tuple[str, str]]:
    """类体内字段声明 (字段名, 类型简单名)。供跨类调用解析成员变量（如注入的 service）的类型。"""
    body = type_decl.body
    if body is None:
        return
    for child in body.children:
        if child.type != "field_declaration":
            continue
        type_node = child.child_by_field_name("type")
        simple = simple_type_name(_text(type_node)) or ""
        for vd in child.children:
            if vd.type == "variable_declarator":
                name_node = vd.child_by_field_name("name")
                if name_node is not None:
                    yield _text(name_node), simple


def package_name(root: "Node") -> str | None:
    """从 root 抽 package 声明名（无则 None）。"""
    for child in root.children:
        if child.type == "package_declaration":
            for n in child.children:
                if n.type in ("scoped_identifier", "identifier"):
                    return _text(n)
    return None


# ── 方法调用 ──────────────────────────────────────────────────────

@dataclass
class Invocation:
    name: str                      # 被调方法名
    object_text: str               # 接收者表达式文本（无则 ""，如同类方法 helper(...)）
    object_node: "Node | None"
    args: list["Node"]             # 实参表达式节点（已剔标点）
    arg_count: int
    line: int
    node: "Node"


def iter_invocations(node: "Node | None") -> Iterator[Invocation]:
    """遍历子树内的所有方法调用（method_invocation），含嵌套链式调用。"""
    if node is None:
        return
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "method_invocation":
            name_node = n.child_by_field_name("name")
            obj_node = n.child_by_field_name("object")
            args_node = n.child_by_field_name("arguments")
            args = list(args_node.named_children) if args_node is not None else []
            yield Invocation(
                name=_text(name_node),
                object_text=_text(obj_node),
                object_node=obj_node,
                args=args,
                arg_count=len(args),
                line=_line(n),
                node=n,
            )
        stack.extend(n.children)


# ── 字符串字面量 ──────────────────────────────────────────────────

def string_value(node: "Node | None") -> str | None:
    """若节点是字符串字面量，返回其内容（去引号）；否则 None。

    tree-sitter-java 的 string_literal 文本含两端引号，内部可能有 escape_sequence 子节点；
    取去引号后的原文即可（字段 key 不含转义，足够用）。
    """
    if node is None or node.type != "string_literal":
        return None
    raw = _text(node)
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


def arg_string(invocation: Invocation, idx: int) -> str | None:
    """取第 idx 个实参若为字符串字面量的内容。"""
    if 0 <= idx < len(invocation.args):
        return string_value(invocation.args[idx])
    return None


def arg_identifier(invocation: Invocation, idx: int) -> str | None:
    """取第 idx 个实参若为标识符/字段访问（如 Const.F_AMT / F_AMT）的文本。"""
    if not (0 <= idx < len(invocation.args)):
        return None
    a = invocation.args[idx]
    if a.type in ("identifier", "field_access", "scoped_identifier"):
        return _text(a)
    return None


# ── static final String 常量 + 局部变量声明 ───────────────────────

def iter_const_strings(root: "Node") -> Iterator[tuple[str, str]]:
    """遍历**所有层级**的 `static final String NAME = "literal"`（含接口字段）。

    接口字段默认 public static final，无显式 modifiers 也算；类字段要求含 static+final。
    返回 (常量名, 字面值)。仅收 String 类型且初值为字符串字面量的。
    """
    # field_declaration（类字段，需 static final）；constant_declaration（接口字段，天然常量）。
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "constant_declaration":
            yield from _const_from_field(n, in_iface=True)
        elif n.type == "field_declaration":
            yield from _const_from_field(n, in_iface=False)
        stack.extend(n.children)


def _const_from_field(decl: "Node", in_iface: bool) -> Iterator[tuple[str, str]]:
    # modifiers 是子节点类型、不是命名字段，按 type 取。
    mod_text = next((_text(c) for c in decl.children if c.type == "modifiers"), "")
    is_const = in_iface or ("static" in mod_text and "final" in mod_text)
    if not is_const:
        return
    type_node = decl.child_by_field_name("type")
    if (simple_type_name(_text(type_node)) or "") != "String":
        return
    for vd in decl.children:
        if vd.type != "variable_declarator":
            continue
        name_node = vd.child_by_field_name("name")
        value_node = vd.child_by_field_name("value")
        lit = string_value(value_node)
        if name_node is not None and lit is not None:
            yield _text(name_node), lit


@dataclass
class LocalVar:
    name: str
    init: "Node | None"            # 初始化表达式（通常是一个 method_invocation）
    line: int


def iter_local_vars(body: "Node | None") -> Iterator[LocalVar]:
    """遍历方法体内的局部变量声明（local_variable_declaration）。供数据流追数据包归属。"""
    if body is None:
        return
    stack = [body]
    while stack:
        n = stack.pop()
        if n.type == "local_variable_declaration":
            for vd in n.children:
                if vd.type == "variable_declarator":
                    name_node = vd.child_by_field_name("name")
                    if name_node is not None:
                        yield LocalVar(
                            name=_text(name_node),
                            init=vd.child_by_field_name("value"),
                            line=_line(vd),
                        )
        stack.extend(n.children)
