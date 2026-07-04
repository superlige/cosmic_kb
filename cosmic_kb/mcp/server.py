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
from typing import Any

from ..graph import store

DEFAULT_DB = "cosmic_kb.db"

# 段二语义层「下沉进 MCP」：任意 agent 初始化时拿到这段路由+纪律（不再只靠 Claude 私有 SKILL）。
# 多数 MCP 客户端会把 server instructions 并入系统提示。见 docs/分发与多agent接入方案.md §2。
INSTRUCTIONS = (
    "苍穹（金蝶 Cosmic）历史项目本地理解工具，供 AI 查 KB 排障。\n"
    "- 先取证后下结论：字段/单据/方法/插件相关问题一律先调工具查 KB，不凭训练记忆猜；结论带"
    "类·方法·行号等证据，标 confirmed/likely/unknown，证不到就是 unknown，禁止臆造字段名/方法名/"
    "插件名。\n"
    "- 路由：已知精确字段/单据标识用 trace/bill；已知类名+方法名查调用链用 method_calls；标识不"
    "精确、或问『某插件/操作在干嘛』用 ask。\n"
    "- 源码用宿主自带的文件读取工具读（本项目开发场景源码已是本地 UTF-8，无需专门解码）。读到的"
    "字段/实体/单据标识一律用 resolve_fields 核对真实中文名，禁止凭标识片段或命名惯例猜；已从源码"
    "字面量看到单据/分录归属时，传复合限定符做精确匹配——与 trace 同一套点号坐标写法："
    "`\"单据.字段\"`/`\"分录.字段\"`/`\"单据.分录.字段\"`，比裸 key 更准；查不到/多候选就是"
    "unknown，不替你选。**一次批量传入本轮读到的所有陌生标识，"
    "不得以\"减少工具调用\"为由只核实其中一部分、对其余标识凭字面翻译——单据/表头/分录/子分录标识"
    "与字段同标准，没有\"次要标识可以不核实\"这回事。**\n"
    "- 返回值带 next_cursor 说明内容未取全，翻完（变 null）前禁止下『不存在/未覆盖』这类结论。\n"
    "- 解释插件/事件/操作语义、判断入库时机、或遇到不认识的 kd.bos.* 符号，先调 cosmic_semantics"
    "(topic) 取苍穹语义再下结论；不确定查哪篇先空参列清单。\n"
    "- 各工具具体返回结构见其自身描述，此处只讲跨工具全局纪律。"
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


# ── 六个取证工具的纯逻辑（复用段一取证函数，绝不重写）────────────────────────
def tool_ask(question: str) -> dict[str, Any]:
    """自然语言问题 → 意图解析 → 查 KB 返回确定性证据包。标识不精确、或问『某插件/操作在干嘛』这类
    需要先定位再解释的问题用本工具；已知精确字段/单据/方法标识优先直接用 trace/bill/method_calls。
    覆盖字段谁改的/单据钻取/插件解释/操作解释/方法调用共 5 类意图。判不准返回
    `status='need_clarification'` + `candidates`，挑一个精确标识再问，禁止替用户拍板。
    """
    from ..semantic import resolver
    from ..context import builder
    from ..report import field_trace, bill_view, method_calls as mc_report

    conn = _open()
    try:
        rq = resolver.resolve(conn, question)
        result = builder.build_context(conn, rq)
        # 字段/单据/方法调用意图的 evidence 是完整富 dict，经 MCP 同样会被 host 截断——换成紧凑
        # 投影（cap + 字节 governor + 游标分页）。复用 rq，不动 builder/CLI 路径。
        if result.get("status") == "ok":
            if rq.intent == "field_who_changed":
                result["evidence"] = field_trace.trace_compact(
                    conn, rq.field_key,
                    form_key=rq.form_key, entry_key=rq.entry_key, level=rq.level)
            elif rq.intent == "bill_drilldown":
                result["evidence"] = bill_view.bill_compact(conn, rq.form_key)
            elif rq.intent == "method_calls":
                result["evidence"] = mc_report.method_calls_compact(
                    conn, rq.class_fqn, rq.method_name)
        return result
    finally:
        conn.close()


def tool_trace(
    field: str,
    form: str | None = None,
    entry: str | None = None,
    level: str | None = None,
    access: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """字段 → 哪些类的哪个事件函数读/写它、是否落库、行号、源码路径。已知精确字段/单据时用本工具
    （比 ask 更省）。`field` 支持点号坐标 `单据.字段`/`单据.分录.字段`/`单据.分录.子分录.字段`，
    裸字段列全部坐标；已知坐标建议带上（`form/entry/level` 可显式覆盖推断），更省且不裁剪。

    `access='write'`（默认含写）按类合并写入点；`access='read'`按类合并读取方法；不传时只给写入
    明细+读取按类计数概览。`coarse.coarse_only`>0 说明源码字面量有命中但未结构化，禁止当作"确实
    无人读写"，需读完整个方法核实（保存调用可能在窗口外，窄窗口没读到不等于不保存）。真实总数在
    `summary`，内容超预算靠 `next_cursor` 翻页，翻完（变 null）前禁止下"某字段无人读写/未覆盖"
    结论。

    `unlocated`：读写命中但来源单据未钉出（`dynamic_writers` 是字段本身钉不出，二者不同），带
    `null_reason` 成因码——`basedata-ref`/`dynamic-entity` 是正常 None 无需追；其余成因值得顺
    `calls` 读源码反推来源。
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


def tool_bill(form_key: str, cursor: str | None = None) -> dict[str, Any]:
    """单据钻取：操作集/插件清单/字段触达（按实体）/桥接风险。要查某字段谁改的/哪个事件/是否落库，
    对该字段改用 `trace 单据.字段`（更细，entity_touch 每行已带 trace 锚点）。

    `plugin_lanes` 按场景（操作/界面/列表/反写/转换）分流插件、给排障优先级 + 语义文档路由
    （`semantics_topic`）；只含单据绑定插件，孤儿类（无 form_key）不在此列。内容超预算靠
    `next_cursor` 翻页（`cursor=` 原样传回再调），翻完（变 null）前禁止下"某字段/插件未出现"
    这类结论。
    """
    from ..report import bill_view

    conn = _open()
    try:
        return bill_view.bill_compact(conn, form_key, cursor=cursor)
    finally:
        conn.close()


def tool_method_calls(class_fqn: str, method_name: str, cursor: str | None = None) -> dict[str, Any]:
    """类全限定名+方法名 → 该方法调用的项目内方法及位置，用于按调用链下钻导航。只回目标类全限定名
    （`target_fqn`，可再下钻）/源文件相对路径（`target_relpath`）/调用行号，不解释源码逻辑——
    要懂"在干嘛"自行读源码。只列项目内可下钻调用，平台/外部调用不回；字段落库取证用 `trace`。
    `fields.reads/writes` 只给字段 key + 行号，不附中文名——输出中文名前必须调 `resolve_fields`
    核对，不得凭命名惯例猜。
    类/方法判不准返回 `found=False` + `candidates`。

    返回值经紧凑投影防 host 32KB 截断（方法体调用多/重载方法多时按方法计 cap + 字节 governor）；
    真实总数在 `methods_total`/各方法 `calls_total`/`fields.*_total`。`next_cursor` 非 null 说明
    还有被截条目，用 cursor=该值再调一次翻页取回，翻完（变 null）前禁止下"无更多调用/字段"结论。
    """
    from ..report import method_calls

    conn = _open()
    try:
        return method_calls.method_calls_compact(conn, class_fqn, method_name, cursor=cursor)
    finally:
        conn.close()


def tool_resolve_fields(keys: list[str]) -> dict[str, Any]:
    """标识批量核对为元数据真实中文名（比 trace 便宜得多，O(1) 打词典，不查谁改了它）。覆盖字段/
    表头实体/分录/子分录/单据(表单)五类标识——不是只查字段，读源码见到任何一类标识（如
    `bill.getString("cqkd_amount")`、`.load("cqkd_invoic_apply", ...)`）都必须先调本工具核对，
    命名惯例不算证据，禁止凭字面猜中文名。`keys` 支持批量：**一次把本轮读到的所有陌生标识都传
    进去，不得因为想省调用次数就只核实一部分、对其余的凭字面翻译**——批量参数本身就是为了用一次
    调用覆盖多个标识，不是"选重要的核实、次要的跳过"的理由。已从源码字面量看到单据/分录归属时，
    key 支持复合限定符——与 `trace` 同一套点号坐标写法：`"单据.字段"`/`"分录.字段"`/
    `"单据.分录.字段"`（如 `"cqkd_zkd.cqkd_amount"`/`"cqkd_entry.cqkd_amount"`）——匹配到即唯一
    答案；限定符不含该字段时，返回值 `mismatched_form` 会给出该字段真实所在的单据/分录，不悄悄
    回退成全部候选掩盖这个信号。不带限定符时同一 key 跨多坐标（名字可能不同）全部列出、不替你选；
    钉不出回 `null`，标 unknown。
    """
    from ..report import resolve_fields

    conn = _open()
    try:
        return resolve_fields.resolve_fields(conn, keys)
    finally:
        conn.close()


def tool_cosmic_semantics(topic: str = "") -> dict[str, Any]:
    """苍穹领域语义文档查询：插件类型/事件时机/SDK 用法/入库判断/反模式黑名单。解释插件/事件/操作
    语义、判断入库时机、或遇到不认识的 kd.bos.* 符号前必须先查。不确定查哪篇先空参列清单
    （`{status:'need_topic', available_topics, grouped}`，每条带『何时用』）；命中返回
    `{topic, content}` 单篇全文。只回语义文档，源码自行读取，字段落库取证用 `trace`。
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
    "ask": tool_ask,
    "trace": tool_trace,
    "bill": tool_bill,
    "method_calls": tool_method_calls,
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
