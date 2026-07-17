"""段二 · MCP 服务器：把 `cosmic_kb` 取证命令暴露成 MCP 工具。

设计要点：
- **纯逻辑与 mcp 包装分离**：`tool_*` 是不依赖 `mcp` 的纯函数（返回与 CLI `--json` 同口径的
  dict），单测可直接调；`build_server()` / `serve()` 才 import `mcp`，故未装 `[mcp]` 时本模块
  仍可 import（测试不被可选依赖卡住）。
- **每次调用新开连接**：MCP 工具可能跨线程被调，SQLite 连接不跨线程复用，开/用/关最稳。
- KB 路径取环境变量 `COSMIC_KB_DB`，缺省 `cosmic_kb.db`（与 CLI DEFAULT_DB 一致）。
"""

from __future__ import annotations

import os
from typing import Any, Literal

from ..graph import store

DEFAULT_DB = "cosmic_kb.db"

# 段二语义层「下沉进 MCP」：任意 agent 初始化时拿到这段路由+纪律（不再只靠 Claude 私有 SKILL）。
# 多数 MCP 客户端会把 server instructions 并入系统提示。见 docs/设计方案/分发与多agent接入方案.md §2。
INSTRUCTIONS = (
    "苍穹（金蝶 Cosmic）本地取证工具。按已知起点直接路由，不把 bill 当通用前置：\n"
    "① 字段谁读写、由哪个插件事件入口触发、是否落库 → trace(\"单据.字段\", kind=\"field\")。\n"
    "② 谁调用了某操作（含操作 key/目标单据解不出、无法排除的嫌疑）、操作绑定插件或跨单据下游 → "
    "trace(\"单据.操作key\", kind=\"operation\")；已知操作坐标时直接查，一次即完整，"
    "入站触发点自带 entry_chains（回溯到插件事件入口的调用链）。\n"
    "③ 单据整体操作集、全部插件绑定、插件车道或本单据对外触发的影响面 → bill(form_key)。\n"
    "④ 字段/实体/单据标识及插件类反查 → resolve_fields；标识不精确时先核对。\n"
    "⑤ 谁调用了某 Java 方法、方法引用或死代码验证 → callers(\"Class.method\")。\n"
    "⑥ 插件事件、SDK、事务和入库语义 → cosmic_semantics(topic)。\n"
    "通用纪律：不凭字段名、类名、包名或命名习惯猜标识和绑定；源码中文注释/常量名/Javadoc "
    "不是元数据证据，输出的中文名一律以 resolve_fields 等工具返回为准，未核对的只准写"
    "「<标识>（未核对）」；结论标 "
    "confirmed/likely/unknown，证不到就是 unknown。trace/bill/callers 顶层 "
    "pagination.complete=false 时按 pending.next_cursor 翻页至完成。need_clarification、"
    "mismatched_*、invalid_request、coarse_only、unlocated、dynamic_writers 和 "
    "unresolved_inbound 都是纠错或存疑证据，不得解释成不存在。"
)


# 主题 stem → 一行「何时用」。把原 SKILL.md「苍穹语义路由」表内化进 MCP，让任意 agent
# 空参列清单时就知道「该翻哪本」，不再依赖未加载的 SKILL.md。
# 这是相对随包文档的**第二份事实源**（已接受其轻微漂移代价）；新增/改名文档时同步这里。
# 未列入的主题（如部分 sdk-*，名字自描述）走「只给名」回退，不强求每条都写。
WHEN_TO_USE = {
    # —— 插件类型（判断插件能力边界 / 事件触发时机时查）——
    "plugin-form":        "表单界面：字段联动 propertyChanged、控件可见可用、页面赋值",
    "plugin-bill":        "单据界面插件（非操作事务）",
    "plugin-list":        "列表 / 批量操作",
    "plugin-tree-list":   "树形列表",
    "plugin-operation":   "操作/审核/保存/校验/删除等事务事件（beforeDoOperation、afterExecuteOperationTransaction…）",
    "plugin-botp":        "下推 / 选单 / 转换（BOTP）",
    "plugin-writeback":   "反写 / 回写源单",
    "plugin-task":        "后台任务 / 定时调度",
    "plugin-workflow":    "工作流审批节点",
    "plugin-import":      "引入 / 导入",
    "plugin-print":       "打印",
    "plugin-openapi":     "WebApi / OpenApi 外部接口入口",
    "plugin-report-data": "报表取数",
    "plugin-report-form": "报表界面",
    # —— 原生 SDK 兜底（看不懂某 kd.bos.* 符号时）——
    "sdk-orm-access":     "ORM / QFilter / KSQL 查询",
    "sdk-dynamic-object": "原生 DynamicObject API",
    "sdk-entity-model":   "实体模型 / 数据结构",
    "sdk-tx":             "事务 TX",
    "sdk-id":             "主键 / ID 生成",
    "sdk-lock":           "分布式锁",
    "sdk-cache":          "缓存",
    # rules
    "anti-patterns":      "苍穹幻觉方法名/类名黑名单——不确定某 API 是否存在时必查",
}


def _open():
    """打开 KB（不存在/版本不符则抛错，让 LLM 看到清晰提示而非空结果）。"""
    db = os.environ.get("COSMIC_KB_DB", DEFAULT_DB)
    if not store.kb_exists(db):
        raise RuntimeError(
            f"KB 不存在或版本不符: {db}。请先在项目根运行  cosmic_kb build <源码根> <dym|zip|目录>，"
            f"或设环境变量 COSMIC_KB_DB 指向已建好的 KB。"
        )
    return store.open_kb(db)


# ── 五个取证工具的纯逻辑（复用段一取证函数，绝不重写）────────────────────────
def tool_trace(
    field: str,
    form: str | None = None,
    entry: str | None = None,
    level: str | None = None,
    access: str | None = None,
    cursor: str | None = None,
    kind: str = "field",
) -> dict[str, Any]:
    """字段或操作坐标取证；`kind` 必须显式区分，不自动猜测。

    `kind="field"`（默认）：`field` 传 `单据.字段`、`单据.分录.字段` 等精确坐标，返回读写类/方法、
    保存证据和源码行号；每个访问节点用 `entry_ref` 关联顶层按物理方法去重的 `entry_chains`
    （插件事件入口→实际读写方法，目录自身可分页，避免逐行复制导致返回爆炸）。裸 key 有歧义会返回
    `need_clarification`；`access="write"`/`"read"`
    可聚焦访问类型。`coarse_only`、`unlocated`、`dynamic_writers` 表示仍有待读源码的证据，不等于
    无人读写；note 出现 ⚡ 时可按其中的操作坐标转 `kind="operation"`。

    `kind="operation"`：`field` 传 `单据.操作key`（如 `cqkd_ht.audit`）。已知操作坐标时直接调用，
    `bill` 不是前置步骤。返回 `plugins`（绑定的操作插件/源码入口）、`triggered_by`（明确的程序化
    上游）、`unresolved_inbound`（操作 key 或目标单据未钉准、**无法静态排除是本操作**的入站嫌疑，
    `suspect_reason` 注明成因，挂不上操作坐标的表单插件外发已并入）和 `triggers_downstream`
    （跨单据下游，可能带 `next_trace`）。`triggered_by`/`unresolved_inbound` 每条附
    `entry_chains`：沿静态调用边向上回溯到**插件事件入口**的调用链（苍穹程序化调用最终从插件
    事件开始）——`terminal=entry` 已回溯到事件入口，`plugin_boundary` 到达插件类但方法不在事件表
    （likely 入口），`no_static_caller` 表示静态追不到上游（反射/定时任务/OpenAPI 派发需读源码
    定性，不等于无入口）。对某操作的程序化调用 `triggered_by`+`unresolved_inbound`
    合起来即完整，无需补查 `bill`。它只取证静态识别到的 executeOperate/invokeOperation 等程序化
    触发，不代表人工、工作流或设计器入口的完整链路。该模式忽略 `entry`/`level`/`access`。

    两种模式都遵循顶层 `pagination` + `cursor` 分页协议。
    """
    from ..report import field_trace

    if kind == "operation":
        from ..report import op_trace

        conn = _open()
        try:
            return op_trace.operation_trace_compact(conn, field, form_key=form, cursor=cursor)
        finally:
            conn.close()
    if kind != "field":
        return {"error": f"未知 kind: {kind}（可选 field/operation，纯显式不自动猜测）"}

    conn = _open()
    try:
        field_key, form_key, entry_key, lvl = field_trace.parse_locator(field)
        return field_trace.trace_compact(
            conn,
            field_key,
            form_key=form or form_key,
            entry_key=entry or entry_key,
            level=level or lvl,
            access=access,
            cursor=cursor,
        )
    finally:
        conn.close()


def tool_bill(
    form_key: str, cursor: str | None = None, profile: str = "overview",
) -> dict[str, Any]:
    """单据级整体视图：基本信息、操作集、全部插件绑定、启用状态、插件车道和桥接风险。

    从插件类或源码出发时，先用 `resolve_fields(kind="plugin")` 反查单据；需要完整单据绑定上下文再
    调本工具。已知精确操作坐标，查询绑定插件、程序化上游或跨单据下游时，直接调用
    `trace("单据.操作key", kind="operation")`，本工具不是前置步骤。查字段读写也用 `trace`。

    `profile="overview"`（默认）给概览和插件绑定；需要字段/实体触达时用 `profile="full"`，或以
    `cursor="fields@0"`/`"entity_touch@0"` 单独取段。`plugin_lanes` 给场景和语义路由；未入车道的
    插件还应检查 `disabled_plugins`。`operations[].programmatic_trigger_count` 只是操作追踪的
    发现信号；`outbound_triggers` 是本单据插件对外触发的**影响面**视图（改本单插件前评估会波及谁）
    ——查"谁调用了某操作"用 `trace(kind="operation")` 即完整（无法排除的切片已并入其
    `unresolved_inbound`），无需补查本工具。
    """
    from ..report import bill_view

    conn = _open()
    try:
        return bill_view.bill_compact(conn, form_key, cursor=cursor, profile=profile)
    finally:
        conn.close()


_Kind = Literal["field", "entity", "form", "plugin"]


def tool_resolve_fields(
    keys: list[str], kind: _Kind | list[_Kind | None] | None = None,
) -> dict[str, Any]:
    """低成本批量核对字段、实体、单据和插件类名；返回真实名称、所属坐标及可用的取值语义，
    不查询谁读写字段。

    能确定种类时传 `kind="field"`/`"entity"`/`"form"`/`"plugin"`。同批 key 分属不同层级时，
    `kind` 必须传等长列表逐项对应（不确定项填 `None`）；单个 kind 只适用于整批同层级，否则会产生
    `mismatched_kind`。同一 key 可能跨种类或坐标，不传 kind 时会列候选而不会替调用方猜选。

    字段/实体尽量传 `单据.字段` 或 `单据.分录.字段` 等精确坐标。`kind="entity"` 使用两段式
    `分录.字段` 时缺少单据前缀会返回 `invalid_request(reason="missing_form_key")`，必须改传三段式。
    限定符与真实归属冲突会返回 `mismatched_form`；命中字段可带 `combo_items` 或 `ref_entity`。

    插件简单名或全限定名使用 `kind="plugin"`，整串按类名处理，不能组装业务点号坐标。返回绑定
    单据、操作和启用态，供后续 `bill`、`trace(kind="operation")` 或源码读取使用；源码中存在但
    无法绑定时返回 `unbound_in_source`。
    """
    from ..report import resolve_fields

    conn = _open()
    try:
        return resolve_fields.resolve_fields(conn, keys, kind=kind)
    finally:
        conn.close()


def tool_callers(target: str, cursor: str | None = None) -> dict[str, Any]:
    """反查谁调用了某 Java 方法；用于跨类调用链补全、方法引用定位和死代码验证。

    `target` 写 `Class.method` 或 `完整包名.Class.method`。简单类名跨包重名时返回
    `need_clarification + candidates`，必须选完整 locator 重查。结果逐调用点给出类、方法、
    文件行列、`invocation|method_reference`、`expr|scope|heuristic` 与 confidence。

    返回始终附 `resolution_coverage`。0 结果只有在符号层 status=ok、无失败文件且覆盖率 ≥95%
    时才是“查无调用方”的强证据；符号层不可用或覆盖不足时，名字匹配口径不足以断言死代码。
    热点方法会分页；`pagination.complete=false` 时把 `next_cursor` 原样传回，直至取全。
    """
    from ..report import callers as callers_report

    conn = _open()
    try:
        return callers_report.callers_compact(conn, target, cursor=cursor)
    finally:
        conn.close()


def tool_cosmic_semantics(topic: str = "") -> dict[str, Any]:
    """苍穹领域语义文档：插件类型/事件时机/SDK 用法/入库判断/反模式黑名单。不确定查哪篇先空参
    列清单（`{status:'need_topic', available_topics, grouped}`，每条带『何时用』）；命中返回
    `{topic, content}` 全文。它不提供项目源码事实、字段读写或插件绑定；这些分别读取源码并使用
    `trace`/`bill`/`resolve_fields` 取证。
    """
    from .. import _assets

    content = _assets.read_topic(topic)
    if content is not None:
        return {"topic": topic, "content": content}

    topics = [rel for rel, _ in _assets.iter_reference_topics()]
    grouped: dict[str, list[str]] = {}
    for rel in topics:
        stem = rel.rsplit("/", 1)[-1]
        hint = WHEN_TO_USE.get(stem, "")
        line = f"{stem} — {hint}" if hint else stem  # 缺说明就只给名
        grouped.setdefault(rel.split("/", 1)[0], []).append(line)
    return {
        "status": "need_topic",
        "hint": "按『何时用』挑一个主题名（— 左边那个词），再 cosmic_semantics(topic) 取全文。",
        "available_topics": topics,  # 扁平 rel 路径清单（精确取用 / 兼容旧调用方）
        "grouped": grouped,
    }


# 工具名 → 纯逻辑函数（build_server 注册用，测试也按此遍历核对）。
TOOLS = {
    "trace": tool_trace,
    "bill": tool_bill,
    "resolve_fields": tool_resolve_fields,
    "callers": tool_callers,
    "cosmic_semantics": tool_cosmic_semantics,
}


def build_server():
    """构造 FastMCP 服务器并注册工具（此处才 import mcp，未装 [mcp] 时不影响模块 import）。"""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # noqa: BLE001
        raise RuntimeError(
            "未安装 MCP SDK。请先  pip install -e \".[mcp]\"  （或 pip install mcp）。"
        ) from e

    mcp = FastMCP("cosmic_kb", instructions=INSTRUCTIONS)
    # FastMCP 用函数签名 + docstring 生成工具 schema；显式给干净工具名（否则取 __name__ 带 tool_ 前缀）。
    for name, fn in TOOLS.items():
        mcp.tool(name=name)(fn)
    return mcp


def serve() -> int:
    """启动 MCP 服务器（stdio 传输，供 LLM 宿主以子进程方式拉起）。"""
    build_server().run()
    return 0


def main() -> int:
    """console_scripts 入口（cosmic_kb-mcp）。"""
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
