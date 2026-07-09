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
    "苍穹（金蝶 Cosmic）本地理解工具，聚焦两个场景：\n"
    "① 字段级排障（谁改的/哪个事件/是否落库）→ trace。\n"
    "② 插件源码解读（绑定哪个单据/哪个操作/何时触发）→ 必须先用 bill 核对绑定关系，禁止凭类名/"
    "包名/命名习惯猜；手头只有插件类名、没有 form_key 时，先 resolve_fields(kind=\"plugin\") 反查"
    "绑定单据，再拿 form_key 去调 bill。\n"
    "通用纪律：\n"
    "- 先取证后下结论，不凭训练记忆猜字段名/方法名/插件名；结论标 confirmed/likely/unknown，"
    "证不到就是 unknown。\n"
    "- 标识不精确（只有中文名/命名惯例）时先用 resolve_fields 核对成精确 key，再调 trace/bill。\n"
    "- resolve_fields 调用前必须自己从源码上下文（`.loadSingle`/`BusinessDataServiceHelper.load`/"
    "`getDynamicObjectCollection` 等调用里的实体标识、分录容器 key 字面量）把标识组装成点号坐标"
    "（`单据.字段`/`分录.字段`/`单据.分录.字段`）再传参，不得只传裸 key 甩给工具猜；确实判断不出"
    "归属时才允许裸 key。`kind=\"entity\"` 的两段式必须带单据前缀，否则工具直接拒绝"
    "（`invalid_request`）。`kind=\"plugin\"` 例外：类名不必组装点号坐标，整串按类名处理。\n"
    "- 一次批量传入本轮读到的所有陌生标识，不得只核实一部分、其余凭字面翻译；批量里的 key 分属"
    "不同层级（如同时有单据号/分录容器/字段）时，`kind` 必须传与 keys 等长的列表逐位对应"
    "（如 `[\"form\",\"entity\",\"field\"]`），不得传单个字符串广播——那会把不匹配的层级全部"
    "错判成 `mismatched_kind`。\n"
    "- 下拉/枚举字段看 `combo_items`、引用字段看 `ref_entity` 后才能下结论，不得凭存储值或字段"
    "命名猜含义。\n"
    "- trace/bill 返回体先查顶层 `pagination.complete`，为 false 时按 `pending` 里的 "
    "`next_cursor` 逐段翻页直至 `complete=true` 再下结论；某段 `capped=0` 不代表全部段都取全。\n"
    "- 插件/事件/操作语义、入库时机、不认识的 kd.bos.* 符号，先查 cosmic_semantics(topic)。"
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


# ── 四个取证工具的纯逻辑（复用段一取证函数，绝不重写）────────────────────────
def tool_trace(
    field: str,
    form: str | None = None,
    entry: str | None = None,
    level: str | None = None,
    access: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """字段 → 谁读/写它、哪个事件函数、是否落库、源码行号。`field` 支持点号坐标 `单据.字段`/
    `单据.分录.字段`/`单据.分录.子分录.字段`，已知坐标建议带上（比裸字段更省更准）。裸字段跨单据
    有歧义时返回 `need_clarification`（`occurrences` 列候选），加 `form` 或点号坐标再查；消歧更省
    的办法是先调 `resolve_fields`。

    `access="write"`（默认，含写）按类合并写入点；`"read"` 按类合并读取方法；不传只给写入明细+
    读取按类计数概览。`coarse.coarse_only>0` 说明有命中但未结构化，不能当"无人读写"，需读完整个
    方法核实。

    `unlocated`（读写命中但源单据未钉出）与 `dynamic_writers`（字段本身钉不出）都带成因标签
    （`null_reason_label`/`cause_label`）；`basedata-ref`/`dynamic-entity` 是正常 None，其余成因
    值得读源码反推。分页协议见 server instructions。
    """
    from ..report import field_trace

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
    """单据钻取：基本信息/操作集/插件清单+绑定/桥接风险。**插件源码解读场景的必查工具**——判断一段
    插件源码绑定在哪个单据/操作/何时触发，先调本工具核对，禁止凭类名/包名猜。查字段谁改的用
    `trace 单据.字段`；批量核对字段中文名用 `resolve_fields`。

    `profile="overview"`（默认）只给概览+插件绑定，不含 `fields`/`entity_touch`（多半用不上）；
    需要时传 `profile="full"`，或直接 `cursor="fields@0"`/`"entity_touch@0"` 单独翻出。

    `plugin_lanes` 按场景（操作/界面/列表/反写/转换）分流插件、给排障优先级+语义路由
    （`semantics_topic`），只含单据绑定插件。某插件不在 `plugin_lanes` 里未必是没查全——先查
    `disabled_plugins`（Enabled=false，当前不会被执行）是否有它，别当成扫描遗漏。分页协议见
    server instructions。
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
    """标识批量核对为元数据真实中文名（O(1) 打词典，比 trace 便宜，不查谁改了它）。覆盖字段/
    表头实体/分录/子分录/单据(表单)/插件类名六类——读到任意一类标识都必须先核对，命名惯例不算证据。

    能确定种类时传 `kind`（`"field"`/`"entity"`/`"form"`/`"plugin"`）缩小候选：单据号字面量（如
    `.loadSingle(id, "cqkd_ht")`）→ `"form"`；分录/子分录容器 key（如
    `getDynamicObjectCollection("cqkd_entry")`）→ `"entity"`；字段读写（如
    `getString("cqkd_amount")`）→ `"field"`。同一 key 可能同时是单据号和字段 key，不传则三路
    全查、需自己再筛。

    **本批 `keys` 分属不同层级时，`kind` 必须传与 `keys` 等长的列表逐位对应**（如
    `keys=["cqkd_ht","cqkd_zdgl","cqkd_qs"]` 分别是单据号/分录容器/字段三个不同层级，须传
    `kind=["form","entity","field"]`），不得传单个字符串广播——单个 `kind` 只在整批 key 确定
    同属一个层级时才用（如批量核对一串字段名都传 `kind="field"`）。传单个字符串却混入不同层级
    的 key 会导致部分 key 落入 `mismatched_kind`（诚实报错，但等于白跑一次）。列表某位不确定就填
    `None`（该位置三路全查）；列表长度与 `keys` 不一致会报错拒绝。

    `kind="entity"` 的两段式「分录.字段」必须带单据前缀（`"单据.分录.字段"`），否则返回
    `invalid_request`（`reason="missing_form_key"`），不会替你从全局候选里挑一个。

    点号坐标限定符（`"单据.字段"`/`"分录.字段"`/`"单据.分录.字段"`）命中即唯一答案；限定符与
    该字段实际归属不符时返回 `mismatched_form`（给出真实归属，不静默回退成全部候选）。裸 key
    跨多坐标时全部列出，钉不出回 `null`。

    命中自动带取值语义：下拉/枚举字段给 `combo_items`（存储值→中文），引用字段给 `ref_entity`
    （目标单据，查不到给 `ref_entity_id` 原始 oid）——核对后才能下结论，不得凭存储值/字段命名
    自己猜含义。批量与限定符组装规则见 server instructions。

    只有插件类名（简单名或全限定名均可）时传 `kind="plugin"`——这就是 `bill` 要求先核对绑定
    关系时那个"从类名核对绑定"的入口：整串 `key` 按类名处理，**不组装**点号坐标（那是给
    字段/实体/单据用的限定符协议，会跟类名自带的包名点号冲突）。返回其绑定的单据/操作/启用态，
    供后续 `bill(form_key)`/`trace` 使用；不确定的绑定不会被工具替你挑选，多个候选会全部列出。
    查不到绑定但类确实存在（是插件子类）时给 `unbound_in_source`，不是静默返回 null。
    """
    from ..report import resolve_fields

    conn = _open()
    try:
        return resolve_fields.resolve_fields(conn, keys, kind=kind)
    finally:
        conn.close()


def tool_cosmic_semantics(topic: str = "") -> dict[str, Any]:
    """苍穹领域语义文档：插件类型/事件时机/SDK 用法/入库判断/反模式黑名单。不确定查哪篇先空参
    列清单（`{status:'need_topic', available_topics, grouped}`，每条带『何时用』）；命中返回
    `{topic, content}` 全文。只回语义文档，源码自行读取，字段落库取证用 `trace`。
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
