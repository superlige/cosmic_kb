"""未定位成因（null_reason）分类——单一真源。

一条 `field_access` 若 `form_key=None`（来源单据没钉出），它**为什么** None 是信任优先（红线 #4）
的关键信息：让段二/人类知道「这行该不该继续追」。本模块把成因归成**有限、互斥、优先级归因**的
结构化码，作为「定稿层（analyze）」与「只读消费方（trace/coverage/web）」共用的唯一口径。

设计要点：
- 大多数成因可直接从结构化列推（field_key / key_resolution / level / via / form_key），不靠解析中文。
- 仅 3 个成因（dynamic-entity / helper-caller-unknown / local-or-container-source）需要看引擎留下的
  note——但匹配的是**本模块自己定义的命名常量**（受控词表，非自由文本），field_access 发射时也引用
  同一组常量，改文案只改这里、两端自动同步，不会失配。
- form_key 已定位（非 None）→ 不打成因（返回 None）。被回填救活的行也走这条，reason 清空。
"""

from __future__ import annotations

from typing import Any

# ── 成因码（有限集）────────────────────────────────────────────────────────
FIELD_KEY_UNDETERMINABLE = "field-key-undeterminable"  # 字段 key 本身钉不出，来源问题无意义
BASEDATA_REF = "basedata-ref"                           # **读取**基础资料引用对象自身字段，无业务单据坐标（正确 None）
BASEDATA_WRITE_SUSPECT = "basedata-write-suspect"      # **写到**基础资料引用对象：苍穹不保存基础资料→疑似扫描误绑，应继续追
DYNAMIC_ENTITY = "dynamic-entity"                       # ORM 实体名是运行时变量/拼接（正确 None）
HELPER_CALLER_UNKNOWN = "helper-caller-unknown"         # DO 入参，调用方未安全收敛；可读源码反推
MODEL_CONTEXT = "model-context"                         # getModel()/模型形参写入，插件未绑定/来源未传入
LOCAL_OR_CONTAINER_SOURCE = "local-or-container-source"  # 本地 new/Map/返回值等，来源未识别（其他）
UNKNOWN = "unknown"                                     # 兜底：无证据/老路径，先补证据再归因

ALL_REASONS = (
    FIELD_KEY_UNDETERMINABLE, BASEDATA_REF, BASEDATA_WRITE_SUSPECT, DYNAMIC_ENTITY,
    HELPER_CALLER_UNKNOWN, MODEL_CONTEXT, LOCAL_OR_CONTAINER_SOURCE, UNKNOWN,
)

# 「正确 None」的成因：本就无业务单据坐标 / 运行时才知道，**不应诱导段二去硬追**。
# 注意：`basedata-ref` 只含**读取**基础资料自身字段；**写到**基础资料是 `basedata-write-suspect`
# （苍穹不会"取基础资料再 save"，出现即扫描误判），它**不在**此集合——必须继续追，不可标"无需追"。
CORRECT_NONE_REASONS = frozenset({BASEDATA_REF, DYNAMIC_ENTITY})

# 人读标签 + 是否值得段二顺源码反推（供 trace/coverage/web 导航提示）。
REASON_LABEL = {
    FIELD_KEY_UNDETERMINABLE: "字段 key 本身钉不出（动态/拼接/外部常量/歧义）——来源讨论无意义",
    BASEDATA_REF: "读取基础资料引用对象自身的字段，无业务单据坐标（正确 None，无需追）",
    BASEDATA_WRITE_SUSPECT: "写到基础资料引用对象——苍穹不会取基础资料再保存，疑似来源变量被误绑、"
                            "真实来源单据未定位，应继续追（扫描器待修，非正确 None）",
    DYNAMIC_ENTITY: "ORM 实体名是运行时变量/拼接，静态不可钉（正确 None，无需追）",
    HELPER_CALLER_UNKNOWN: "helper 的 DynamicObject 入参来源未安全收敛——可顺 calls 读源码反推",
    MODEL_CONTEXT: "getModel()/模型形参写入，但插件未注册绑定单据——读源码/补元数据可定",
    LOCAL_OR_CONTAINER_SOURCE: "本地 new/Map/返回值等容器来源未识别——可顺 calls 读源码反推",
    UNKNOWN: "暂无足够证据归因——先补证据再判断",
}

# 字段 key 钉不出的 key_resolution（来源讨论无意义，优先归 field-key-undeterminable）。
_UNDETERMINABLE_KEY_RES = frozenset({
    "dynamic", "dynamic-loop", "concat", "external-const", "unknown", "ambiguous",
})

# ── 引擎 note 命名常量（field_access 发射时引用同一组，受控词表）────────────────────
NOTE_DYNAMIC_ENTITY = "ORM 实体实参为动态表达式，无法静态解析来源单据"
NOTE_HELPER_CALLER_UNKNOWN = "DynamicObject 入参，调用方未知，来源单据/层级未定位"
NOTE_HELPER_CALLER_UNKNOWN_ARR = "DynamicObject[] 入参，调用方未知，来源单据未定位"
NOTE_SOURCE_UNIDENTIFIED = "数据包来源未识别（非入参/非 ORM 查询），层级/来源单据未定位"
NOTE_BASEDATA = "基础资料引用包，写入归该基础资料实体"


def _get(row: Any, key: str) -> Any:
    """同时支持 dict / sqlite3.Row / dataclass 对象取值。"""
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]            # sqlite3.Row 支持下标
    except (TypeError, KeyError, IndexError):
        return getattr(row, key, None)


def classify(row: Any) -> str | None:
    """给一条 field_access（dict / Row / FieldAccessRow）算未定位成因。

    form_key 已定位 → 返回 None（不打成因）。否则按优先级归一个互斥成因码。
    """
    if _get(row, "form_key") is not None:
        return None

    field_key = _get(row, "field_key")
    key_res = _get(row, "key_resolution")
    # ① 字段 key 钉不出：来源讨论无意义，优先归此（凌驾来源类成因）。
    if field_key is None or key_res in _UNDETERMINABLE_KEY_RES:
        return FIELD_KEY_UNDETERMINABLE

    # ② 基础资料引用包：按 access 分流（结构化层级即可判）。
    #   · 读：读基础资料自身字段，本就无业务单据坐标 → 正确 None（无需追）。
    #   · 写：苍穹不会"取基础资料再 save"，写到基础资料即扫描误判（接收者被误绑成基础资料引用，
    #        真实来源单据未定位）→ 标 suspect，**不**归正确 None，督促继续追/修扫描器（红线 #4）。
    if _get(row, "level") == "basedata":
        if _get(row, "access") == "write":
            return BASEDATA_WRITE_SUSPECT
        return BASEDATA_REF

    evidence = _get(row, "evidence") or ""
    # ③ 引擎结构化标记（匹配本模块命名常量，非自由文本解析）。
    if NOTE_DYNAMIC_ENTITY in evidence:
        return DYNAMIC_ENTITY
    if NOTE_HELPER_CALLER_UNKNOWN in evidence or NOTE_HELPER_CALLER_UNKNOWN_ARR in evidence:
        return HELPER_CALLER_UNKNOWN
    if NOTE_SOURCE_UNIDENTIFIED in evidence:
        return LOCAL_OR_CONTAINER_SOURCE

    # ④ 模型 API 写入但来源未传入（多为未注册表单插件，getModel() 无绑定单据）。
    via = _get(row, "via") or ""
    if via.startswith("model."):
        return MODEL_CONTEXT

    # ⑤ 兜底。
    return UNKNOWN
