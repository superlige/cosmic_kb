"""阶段 5 · 苍穹插件事件方法领域表 + 方法分类。

把插件类里的方法分成「生命周期事件」与「普通 helper」，并给每个事件标**落库相位**——
这是落库判定（`persistence.py`）的关键输入之一（落库 = 事件相位 × 操作类型 × 到 sink 的路径）。

落库相位（phase）：
    memory       界面插件事件：`setValue` 只改内存模型，不直接落库（除非下游显式 save）。
    transaction  操作/反写插件事务内事件：`setValue` 是否落库取决于**绑定操作的类型**
                 （save/submit/audit 等入库类→落库；donothing→需显式 save）。
    build        构建/下推阶段（转换插件 afterConvert 写目标单据包、op onPreparePropertys
                 声明加载字段）：写入随后续保存落库。
    validate     校验阶段（onAddValidators）：通常只读不写。
    none         无落库语义（注册监听、列表过滤等）。

事件名取自苍穹标准插件基类（用户 2026-06-16 提供基类清单）。判不准的方法标为 helper，
落库相位随调用它的事件入口确定（见 call_graph 的路径）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventInfo:
    name: str
    phase: str                     # memory | transaction | build | validate | none
    description: str


def _ev(name: str, phase: str, desc: str) -> tuple[str, EventInfo]:
    return name, EventInfo(name, phase, desc)


# 界面/单据插件：客户端侧，setValue 改内存模型。
_FORM = dict([
    _ev("registerListener", "none", "注册控件监听"),
    _ev("afterCreateNewData", "memory", "新建单据后初始化字段"),
    _ev("beforeBindData", "memory", "数据绑定到界面前"),
    _ev("afterBindData", "memory", "数据绑定到界面后"),
    _ev("propertyChanged", "memory", "字段值变化联动（联动逻辑核心）"),
    _ev("beforeFieldPostBack", "memory", "字段值回传前校验/改写"),
    _ev("afterFieldPostBack", "memory", "字段值回传后"),
    _ev("itemClick", "memory", "工具栏/菜单项点击"),
    _ev("click", "memory", "控件点击"),
    _ev("beforeItemClick", "memory", "工具栏项点击前"),
    _ev("beforeDoOperation", "memory", "操作触发前（界面侧拦截/改值）"),
    _ev("afterDoOperation", "memory", "操作完成后（界面侧）"),
    _ev("closedCallBack", "memory", "子页面关闭回调"),
    _ev("confirmCallBack", "memory", "确认框回调"),
    _ev("beforeClosed", "memory", "页面关闭前"),
])

# 操作服务插件：服务端侧，事务内事件改字段会随事务落库（取决于操作类型）。
_OP = dict([
    _ev("onPreparePropertys", "build", "声明操作需加载的字段"),
    _ev("onAddValidators", "validate", "添加校验器"),
    _ev("beforeExecuteOperationTransaction", "transaction", "事务内执行前改字段（入库类操作→落库）"),
    _ev("afterExecuteOperationTransaction", "transaction", "事务内执行后"),
    _ev("beginOperationTransaction", "transaction", "事务开始（事务内改字段→落库）"),
    _ev("endOperationTransaction", "transaction", "事务结束"),
    _ev("rollbackOperation", "none", "操作回滚"),
    _ev("onReturnOperation", "none", "操作返回处理"),
])

# 转换插件（BOTP 下推）：写目标单据数据包，随目标单据保存落库。
_CONVERT = dict([
    _ev("afterConvert", "build", "下推后处理目标单据字段（下推逻辑核心）"),
    _ev("beforeBuildRowMeta", "build", "构建行元数据前"),
    _ev("afterCreate", "build", "目标单据创建后"),
    _ev("beforeDoCreate", "build", "执行下推创建前"),
    _ev("afterCreateColumns", "build", "构建列后"),
])

# 反写插件：反写源单据字段，落库语义。
_WRITEBACK = dict([
    _ev("writeBack", "transaction", "反写源单据字段（落库）"),
    _ev("beforeWriteBack", "transaction", "反写前"),
    _ev("afterWriteBack", "transaction", "反写后"),
])

# 列表插件：以读/过滤为主，无字段落库语义。
_LIST = dict([
    _ev("beforeCreateListDataProvider", "none", "构建列表数据源前"),
    _ev("setFilter", "none", "设置列表过滤"),
    _ev("billListHyperLinkClick", "memory", "列表超链接点击"),
    _ev("itemClick", "none", "列表工具栏点击"),
])

# 工作流插件：审批写字段，落库语义（保守标 transaction）。
_WORKFLOW: dict[str, EventInfo] = {}

# 校验器（AbstractValidator）：操作插件 onAddValidators 里 addValidator 挂载的校验逻辑，
# 入口固定为 validate()，以读单据字段 + addErrorMessage 为主、通常不写字段（validate 相位）。
_VALIDATOR = dict([
    _ev("validate", "validate", "校验逻辑（读单据字段校验、报错；提交/审核报错的真凶）"),
])

EVENT_TABLE: dict[str, dict[str, EventInfo]] = {
    "form": _FORM, "op": _OP, "convert": _CONVERT,
    "writeback": _WRITEBACK, "list": _LIST, "workflow": _WORKFLOW,
    "validator": _VALIDATOR,
}

# 种类未知时的并集表（宽松匹配；相位取各表里该名的相位，冲突优先 transaction>build>memory）。
_PHASE_RANK = {"transaction": 3, "build": 2, "validate": 1, "memory": 1, "none": 0}
_ALL_EVENTS: dict[str, EventInfo] = {}
for _tbl in (_OP, _CONVERT, _WRITEBACK, _FORM, _LIST, _VALIDATOR):
    for _n, _info in _tbl.items():
        cur = _ALL_EVENTS.get(_n)
        if cur is None or _PHASE_RANK[_info.phase] > _PHASE_RANK[cur.phase]:
            _ALL_EVENTS[_n] = _info


def classify_method(kind: str, method_name: str) -> EventInfo | None:
    """方法是否是该种类插件的生命周期事件？是→返回 EventInfo；否（helper）→ None。

    kind 未知时用并集表宽松匹配。工作流插件无显式表时，把方法当 transaction 相位事件
    （审批写字段落库的保守处理）。
    """
    if kind == "workflow":
        # 工作流没有稳定的事件名表，方法多为重写的处理逻辑：保守按事务相位看待。
        return EventInfo(method_name, "transaction", "工作流处理（审批写字段，保守按落库看待）")
    table = EVENT_TABLE.get(kind)
    if table is not None and method_name in table:
        return table[method_name]
    if kind == "unknown" and method_name in _ALL_EVENTS:
        return _ALL_EVENTS[method_name]
    return None
