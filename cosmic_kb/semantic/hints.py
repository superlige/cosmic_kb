"""段二语义增强：把"事件→语义文档路由"焊进取证工具返回值。

起因（2026-06-25 真实样本）：段二大模型读 Java 源码时凭训练知识臆断事件触发时机/是否入库，
不查苍穹语义文档（`propertyChanged` 没核 plugin-form）。靠 MCP `INSTRUCTIONS` 软约束压不住
模型的自信先验——规则在场、模型知道、还是绕过去。

改法：**在模型本来就要走的导航工具（trace / bill / ask）的返回里内联语义
路由**——模型必定读到，被提示"断触发时机/入库前先 cosmic_semantics"。host-agnostic：不依赖
任何宿主钩子，只焊在我们自己工具的返回值（唯一所有 MCP host 都一定读的硬信息）。

（原还包含"已核对字段名"自动标注一半——2026-07-05 随 read_source MCP 工具退役一并砍掉，改为
模型自己读源码识别字段/实体 key 后调 `resolve_fields` 精确核对，见 `docs/核心/阶段验收.md` 对应条目。）

本模块是纯逻辑（只读 KB / 纯映射），report 与 context 层都复用，不反向依赖 mcp。
事件→主题映射与 `mcp.server.WHEN_TO_USE` 同一套主题名（已接受其轻微漂移代价，见 server 注释）。
"""

from __future__ import annotations

from ..java import event_extractor

# 事件方法名 → 苍穹语义文档主题 stem（cosmic_semantics(topic) 取全文）。
# 事件方法名是强信号，与具体 plugin_type 无关也能定；但 beforeDoOperation/afterDoOperation/
# itemClick 这类方法名在不同 plugin_type 下语义不同（下方 event_topic() 的 Tier 0 用
# event_extractor 权威校验后才会用到这张表兜底），本表不再收录它们，避免硬编码出与
# event_extractor 矛盾的默认值。
_EVENT_TOPIC = {
    # —— 表单界面事件（字段联动 / 赋值 / 监听）——
    "propertyChanged": "plugin-form",
    "afterCreateNewData": "plugin-form",
    "afterBindData": "plugin-form",
    "beforeBindData": "plugin-form",
    "registerListener": "plugin-form",
    "afterAddRow": "plugin-form",
    "beforeDeleteRow": "plugin-form",
    "click": "plugin-form",
    # —— 操作 / 事务事件（保存/提交/审核/校验，是否入库高发区）——
    "beforeExecuteOperationTransaction": "plugin-operation",
    "beginOperationTransaction": "plugin-operation",
    "afterExecuteOperationTransaction": "plugin-operation",
    "afterOperationTransaction": "plugin-operation",
    "onAddValidators": "plugin-operation",
    "onPreparePropertys": "plugin-operation",
    "validate": "plugin-operation",
    # —— 列表 / 报表 ——
    "beforePackageData": "plugin-list",
    "setFilter": "plugin-list",
    "filterContainerInit": "plugin-list",
}

# plugin_type → 主题（事件方法名兜不住时的回落；与 dym 解析出的插件类型口径一致）。
_PLUGIN_TYPE_TOPIC = {
    "form": "plugin-form",
    "bill": "plugin-bill",
    "list": "plugin-list",
    "tree-list": "plugin-tree-list",
    "operation": "plugin-operation",
    "op": "plugin-operation",
    "convert": "plugin-botp",
    "botp": "plugin-botp",
    "writeback": "plugin-writeback",
    "task": "plugin-task",
    "workflow": "plugin-workflow",
    "import": "plugin-import",
    "print": "plugin-print",
    "openapi": "plugin-openapi",
    "webapi": "plugin-openapi",
    "validator": "plugin-operation",
}


# ── 轴 A · 场景/插件类型车道（bill 单据绑定插件分流）─────────────────────────
# 顾问真实排障是「动作优先」：报某动作出问题 → 进对应单据该场景的插件。轴 A 把单据绑定插件
# 先按"属于哪类场景"切开（外层分组，对每个插件无条件适用）。plugin_type → (lane_id, 中文label,
# 一句触发场景语义)；列表顺序即排障优先级（op+form 主力在前）。语义句取自 references/base/plugin/*.md。
# 注意：只覆盖单据绑定的 5 类 plugin_type；validator/task/report 等孤儿无 form_key、不进 plugin 表，
# 归后续"孤儿类型目录旁路"，不在本车道词表里（诚实降级为"单据绑定插件的分流"，非完整清单）。
PLUGIN_LANE_ORDER = ["op", "form", "list", "writeback", "convert"]
_PLUGIN_LANE: dict[str, tuple[str, str, str]] = {
    "op": ("operation", "操作插件",
           "单据预制操作(save/submit/audit…)的服务端事务逻辑；报「保存/提交/审核报错、之后某值变了」看这里"),
    "form": ("form", "界面插件",
             "界面打开/字段联动/按钮点击等 UI 交互；报「打开就错、选了X带出Y、点按钮没反应」看这里"),
    "list": ("list", "列表插件",
             "列表查询/过滤/列/行交互；报「列表数据/过滤/列不对」看这里"),
    "writeback": ("writeback", "反写插件",
                  "下游→上游数量金额回写/超额检查；报「上游没回写、超额放过/卡住」看这里"),
    "convert": ("convert", "转换插件",
                "下推/选单生成目标单据字段映射；报「下推带不出/带错、分单合单」看这里"),
}


def plugin_lane(plugin_type: str | None) -> tuple[str, str, str]:
    """plugin_type → (lane_id, 中文label, 一句触发场景语义)。词表外的类型归 other 兜底，不吞。"""
    if plugin_type and plugin_type in _PLUGIN_LANE:
        return _PLUGIN_LANE[plugin_type]
    return ("other", "其他插件", "")


def event_topic(event_method: str | None = None, plugin_type: str | None = None) -> str | None:
    """事件方法/插件类型 → 苍穹语义文档主题 stem；无可路由（纯工具方法等）返回 None。

    Tier 0：`plugin_type` 落在 `event_extractor.EVENT_TABLE` 的种类集合里时，用
    `event_extractor.classify_method()` 权威校验该方法名是否真是这个种类插件的生命周期事件——
    这解决了 `beforeDoOperation`/`afterDoOperation`（表单插件界面侧回调，从未出现在操作插件
    事务事件表）、`itemClick`（表单工具栏 vs 列表工具栏语义不同）这类"方法名相同、
    不同 plugin_type 下语义不同"的场景，比全局方法名强信号表更可信。
    Tier 1/2：Tier 0 未命中（种类未知、或该方法不是该种类的生命周期事件）时，回落到方法名
    强信号表 `_EVENT_TOPIC`，再回落到 plugin_type 表 `_PLUGIN_TYPE_TOPIC`。
    """
    if plugin_type and event_method and plugin_type in event_extractor.EVENT_TABLE:
        if event_extractor.classify_method(plugin_type, event_method) is not None:
            topic = _PLUGIN_TYPE_TOPIC.get(plugin_type)
            if topic:
                return topic
    if event_method and event_method in _EVENT_TOPIC:
        return _EVENT_TOPIC[event_method]
    if plugin_type and plugin_type in _PLUGIN_TYPE_TOPIC:
        return _PLUGIN_TYPE_TOPIC[plugin_type]
    return None


def semantics_pointer(event_method: str | None = None, plugin_type: str | None = None) -> str | None:
    """人读提示串：解释触发时机/是否入库前先查这篇语义文档。无可路由返回 None。"""
    topic = event_topic(event_method, plugin_type)
    if not topic:
        return None
    return f"判触发时机/是否入库前先 cosmic_semantics('{topic}')，勿凭训练知识臆断"


