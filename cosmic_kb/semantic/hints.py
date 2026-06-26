"""段二语义增强（模式 B）：把"已核对字段名"与"事件→语义文档路由"焊进取证工具返回值。

起因（2026-06-25 真实样本）：段二大模型读 Java 源码时
  ① 按命名惯例猜字段中文名翻车（`cqkd_zjjnqk` 猜成"资金"，真实"租金"）；
  ② 凭训练知识臆断事件触发时机/是否入库，不查苍穹语义文档（`propertyChanged` 没核 plugin-form）。
靠 MCP `INSTRUCTIONS` 软约束压不住模型的自信先验——规则在场、模型知道、还是绕过去。

模式 B 不去"强制模型调用"（对自主 agent 几乎不可达），而是换目标：**在模型本来就要走的导航
工具（trace / bill / ask / method_calls）的返回里内联核对结果与语义路由**——模型必定读到，
想按命名惯例改写都没机会，也被提示"断触发时机/入库前先 cosmic_semantics"。host-agnostic：
不依赖任何宿主钩子，只焊在我们自己工具的返回值（唯一所有 MCP host 都一定读的硬信息）。

本模块是纯逻辑（只读 KB / 纯映射），report 与 context 层都复用，不反向依赖 mcp。
事件→主题映射与 `mcp.server.WHEN_TO_USE` 同一套主题名（已接受其轻微漂移代价，见 server 注释）。
"""

from __future__ import annotations

from typing import Any

# 事件方法名 → 苍穹语义文档主题 stem（cosmic_semantics(topic) 取全文）。
# 事件方法名是强信号：propertyChanged 必是表单联动、beforeDoOperation 必是操作事务，
# 与具体 plugin_type 无关也能定。下方 _PLUGIN_TYPE_TOPIC 只在事件方法名兜不住时回落。
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
    "itemClick": "plugin-form",
    # —— 操作 / 事务事件（保存/提交/审核/校验，是否入库高发区）——
    "beforeDoOperation": "plugin-operation",
    "afterDoOperation": "plugin-operation",
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
}


def event_topic(event_method: str | None = None, plugin_type: str | None = None) -> str | None:
    """事件方法/插件类型 → 苍穹语义文档主题 stem；无可路由（纯工具方法等）返回 None。"""
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


class FieldNames:
    """字段/容器标识 → 真实元数据中文名的批量索引（模式 B：内联进工具返回，杜绝命名惯例臆断）。

    `get(key, form_key)`：先按 (form_key, key) 精确取（单据内 key→名唯一）；缺单据上下文时
    回落到"全局唯一名"——同一 key 跨单据有**多个不同名**时返回 None（诚实留白，不替选不臆造）。
    """

    def __init__(self, by_form: dict[tuple[str | None, str], str], by_key: dict[str, str]) -> None:
        self._by_form = by_form
        self._by_key = by_key

    def get(self, key: str, form_key: str | None = None) -> str | None:
        if form_key is not None:
            name = self._by_form.get((form_key, key))
            if name:
                return name
        return self._by_key.get(key)


def build_field_names(conn) -> FieldNames:
    """扫 field + entity 两表建名字索引（覆盖分录容器 key）。同 resolve_fields 口径，只取名。"""
    by_form: dict[tuple[str | None, str], str] = {}
    name_sets: dict[str, set[str]] = {}
    for r in conn.execute("SELECT form_key,key,name FROM field WHERE name IS NOT NULL AND key IS NOT NULL"):
        by_form[(r["form_key"], r["key"])] = r["name"]
        name_sets.setdefault(r["key"], set()).add(r["name"])
    for r in conn.execute("SELECT form_key,key,name FROM entity WHERE name IS NOT NULL AND key IS NOT NULL"):
        by_form.setdefault((r["form_key"], r["key"]), r["name"])
        name_sets.setdefault(r["key"], set()).add(r["name"])
    # 全局回落只保留"唯一名"的 key：多名歧义时宁可不给（让消费者显示裸 key），绝不替选。
    by_key = {k: next(iter(v)) for k, v in name_sets.items() if len(v) == 1}
    return FieldNames(by_form, by_key)


def annotate_field(item: dict[str, Any], names: FieldNames, *, key_field: str = "field_key",
                   form_field: str = "form_key", out_field: str = "field_name") -> dict[str, Any]:
    """给一行 field_access dict 原地补 `field_name`（已核对名；钉不出留 None）。返回同一 dict。"""
    key = item.get(key_field)
    if key:
        item[out_field] = names.get(key, item.get(form_field))
    return item
