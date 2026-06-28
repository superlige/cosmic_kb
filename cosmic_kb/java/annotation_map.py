"""阶段 10 增补 · 注解驱动 POJO↔DynamicObject 映射写入识别。

野生苍穹老项目里有一类**手写的注解驱动 ORM**：POJO 字段上标
`@ContractBillAnnotation(value="cqkd_qs", name="未缴金额", type="head"|"entry", entryDoName=...)`，
再用反射方法（`convertToBillDynamicObject()`：循环 `getDeclaredFields()` 做
`bill.set(annotation.value(), field.get(this))`）把整批字段写进 DynamicObject。

字段 key 藏在**注解里的静态字面量**、不出现在 `.set()` 实参位置，旗舰 `trace` 的字面量扫描
完全看不见 → 真实读写隐形（false negative，踩红线 #4）。本模块把这类映射识别出来，供
`analyze` 在事件 BFS 走到反射方法时**合成 FieldAccess**，让 `trace` 不再隐形。

边界纪律（守红线）：
  - **不硬编码注解名**（红线 #1 通用性）：注解类型由**用法**自动发现——只要某注解的 `value`
    成员字面量命中 KB 字段 key 集，就认它是字段映射注解（KB 反验证，不臆造）。
  - 只认 `value` ∈ 已知字段 key 的映射；解不出/不命中一律丢弃。
  - 反射方法的 `set()` 是 `if(property!=null)` 条件写、且来源单据多由入参 `type` 决定 → 合成行
    **来源(form_key)留 None、落库留 unknown、置信取低档**，交后续 form_key 反查 / 段二大模型定性。
  - 写方向（convertTo…/updateTo…）可挂；**读方向**多在构造器 `BillDo(DynamicObject)` 里，
    `new X(...)` 不是 method_invocation、不入调用图，A 档不覆盖（诚实留白）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ast_index as ax


@dataclass
class FieldMapping:
    """映射类里一个被注解的字段 → 元数据 key 的登记。"""

    java_field: str
    field_key: str
    anno: str                      # 注解简单名（仅备注用）
    level: str                     # header | entry（按注解 type，仅 best-effort，后续元数据回填会校正）
    entry_key: str | None          # type=entry 时的分录容器 key（来自 entryDoName/entryKey 等；未知则 None）
    line: int                      # 注解所在行（指向映射类的字段声明）


@dataclass
class MapperClass:
    """一个注解映射类（如 BillDo）：映射字段集 + 反射 bulk-write 方法名集。"""

    fqn: str
    simple: str
    mappings: list[FieldMapping] = field(default_factory=list)
    write_methods: set[str] = field(default_factory=set)


def _is_bulk_write_method(md: "ax.MethodDecl") -> bool:
    """方法体是否是「反射遍历字段 + 对 DynamicObject 批量 set」的 bulk-write。

    判据：含反射标志（getDeclaredFields / getAnnotation）+ 一处 `X.set(...)`（接收者非 `field`，
    即写进 DynamicObject 而非 `field.set(this,...)` 的反向读）。够强即可，宁缺毋滥。
    """
    has_refl = False
    has_do_set = False
    for inv in ax.iter_invocations(md.body):
        if inv.name in ("getDeclaredFields", "getAnnotation"):
            has_refl = True
        elif inv.name == "set" and inv.object_text.strip() not in ("", "field"):
            has_do_set = True
    return has_refl and has_do_set


class AnnotationMapIndex:
    """全项目注解映射登记：fqn → MapperClass，并能按 (类, 方法) 产合成写入访问。"""

    def __init__(self, mappers: dict[str, MapperClass]) -> None:
        self.mappers = mappers

    def synth_accesses(self, fqn: str, method: str):
        """若 (fqn, method) 是某映射类的 bulk-write 反射方法，产出该类全部映射字段的合成写入。

        返回 `list[field_access.FieldAccess]`（延迟 import 避免环依赖）。否则返回 []。
        """
        mc = self.mappers.get(fqn)
        if mc is None or method not in mc.write_methods:
            return []
        from .field_access import FieldAccess
        out = []
        for mp in mc.mappings:
            out.append(FieldAccess(
                field_key=mp.field_key, level=mp.level, entry_key=mp.entry_key,
                entity=None, access="write", via="annotation-map", line=mp.line,
                key_resolution="annotation-map", confidence=0.6,
                note=f"@{mp.anno} 反射映射写入（{mc.simple}.{method}）", receiver_var=None,
            ))
        return out

    def __bool__(self) -> bool:
        return bool(self.mappers)


def _level_of(anno_type: str | None) -> tuple[str, bool]:
    """注解 type 成员 → (level, is_entry)。head/缺省=header；entry=分录容器。"""
    if (anno_type or "head").strip().lower() == "entry":
        return "entry", True
    return "header", False


def _entry_key_of(members: dict[str, str]) -> str | None:
    """注解里的分录容器 key。不同项目命名不一，只取显式成员；没有就留 None，交元数据回填。"""
    for name in ("entryDoName", "entryKey", "entryEntity", "entityKey", "entry"):
        value = members.get(name)
        if value:
            return value
    return None


def build_index(pg, known_keys: frozenset[str] | set[str]) -> AnnotationMapIndex:
    """从 ProjectGraph 扫全项目类，登记注解映射类（value ∈ known_keys）+ 其 bulk-write 反射方法。"""
    mappers: dict[str, MapperClass] = {}
    for fqn, node in pg.classes.items():
        mappings: list[FieldMapping] = []
        for fname, anns in ax.iter_field_annotations(node.type_decl):
            for an in anns:
                key = an.members.get("value")
                if not key or key not in known_keys:   # KB 反验证：value 必须是已知字段 key
                    continue
                level, is_entry = _level_of(an.members.get("type"))
                mappings.append(FieldMapping(
                    java_field=fname, field_key=key, anno=an.name, level=level,
                    entry_key=_entry_key_of(an.members) if is_entry else None, line=an.line,
                ))
                break   # 一个字段取首个命中的映射注解即可
        if not mappings:
            continue
        write_methods = {md.name for md in node.cg.method_decls if _is_bulk_write_method(md)}
        if not write_methods:
            continue   # 有映射但没反射写方法 → 写不出去，不登记（宁缺毋滥）
        mappers[fqn] = MapperClass(fqn=fqn, simple=node.simple,
                                   mappings=mappings, write_methods=write_methods)
    return AnnotationMapIndex(mappers)
