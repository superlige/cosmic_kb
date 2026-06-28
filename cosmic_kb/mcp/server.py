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
    "这是苍穹（金蝶 Cosmic）历史项目本地理解工具。问『某字段谁改的 / 某单据有哪些操作和插件 / "
    "某方法调了什么 / 某插件干嘛的』，先调取证工具再下结论：字段级用 trace，单据钻取用 bill，"
    "方法出向调用导航用 method_calls。**知道精确字段/单据/方法标识就直接用 trace/bill/method_calls；"
    "ask 只是判不准意图时的自然语言兜底**（判不准会回 need_clarification 候选，请挑精确标识再问、"
    "绝不替用户拍板）。\n"
    "结论纪律：每条都要带 类·方法·行号 等证据，并标注 confirmed / likely / unknown 三态；"
    "缺保存链路一律判 unknown，**绝不臆造字段名/方法名/插件名**。源码全文请直接读本机源文件，"
    "取证工具只回最小证据包。\n"
    "【动态写入】部分代码对运行时/配置决定的动态字段集做泛化读写（循环/拼接/外部常量），静态钉不出"
    "具体字段。trace 返回值里的 `dynamic_writers` 是**按方法去重的「该读方法」清单**（带 calls 导航 + "
    "写入位置）：要把『谁改了 X』答全，就对清单里的方法逐个 calls/读本机源码，判定它是否真碰了 X（"
    "不臆造）。\n"
    "【未定位来源】trace 返回值里的 `unlocated` 是**「反推来源单据」工作单**（与 dynamic_writers 对称："
    "那是钉不出『改哪个字段』，这是钉不出『改哪张单据』）——这些读写**确实碰了被查字段**，只是来源"
    "DynamicObject 来自哪张单据没钉出（form_key=None）。每条按方法去重、带 calls 导航与 `plugin_form_label`"
    "（该插件注册单据，**只是来源线索非确诊**）：要确认它操作的是哪张单据，顺 calls/读本机源码反推，"
    "**绝不把 plugin_form_label 当成已确定来源**。每条/每段还带 `null_reason`（成因码）告诉你**该不该追**："
    "`basedata-ref`（读基础资料自身字段）/`dynamic-entity` 是**正确 None**（无需追）；`basedata-write-suspect`"
    "（写到基础资料——苍穹不保存基础资料，疑似扫描误绑）**不是**正确 None，应继续追/待修扫描器；"
    "`helper-caller-unknown`/`local-or-container-source` 值得顺 calls 读源码反推；`model-context` 多为未注册"
    "表单插件。全量成因分布在 `summary.unlocated_by_reason`（真实总数恒在此）。\n"
    "【trace 写读拆分 + 按类合并】trace 默认回**写入明细（坐标→类→写入点）+ 读取仅按类计数概览**"
    "（`readers_overview`）；要读取明细就再调一次 `trace(field, access='read')`（类→方法，顺 `calls` "
    "去读源码）；只看写入用 `access='write'`。写入/读取都**按类合并**（同一类只出现一次，行号/落库等"
    "列在该类 `sites`/`methods`），别再把同类逐行展开。**真实总数恒在 `summary`**，被 cap 截掉的数在"
    "各节点 `capped`/`sites_capped`/`methods_capped`/`groups_capped`（红线 #4 不丢数）。**知道哪张单据"
    "就带坐标查**——`trace('单据.字段')` 或 `form=`：裸字段会命中全部单据并按坐标组裁剪（真实组数在"
    "`groups_total`），带坐标既省返回又免二次往返。\n"
    "【trace 翻页取回被截条目（别只读计数）】某段被 cap 时会带 `next_cursor`（如 `unlocated@4`/"
    "`readers@20`/`dynamic_writers@4`）。要看被截掉的条目，**把该 `next_cursor` 原样作 `cursor=` 再调一次** "
    "`trace(field, ..., cursor='unlocated@4')`，返回 `page.items`（该段下一页）+ 新 `page.next_cursor`，"
    "循环到 `next_cursor` 为 null 即把该段**全部条目取全**（不是只有计数）。可分页：writers/readers/"
    "unlocated/dynamic_writers/possible/coarse/occurrences（writers/readers 需先用 form/entry/level 收窄到单坐标）。\n"
    "【bill 紧凑投影 + 翻页】bill 返回为防截断的紧凑投影：每字段被触达的**逐条事件已折叠为「写/落库/读」"
    "计数**——要看『某字段谁改的/在哪个事件函数/是否落库』，对该字段用 `trace 单据.字段`（entity_touch 每行"
    "已带 `trace` 锚点）。各列表真实总数在 `*_total`，被 cap 截掉的段带 `*_next_cursor`（如 `fields@60`/"
    "`entity_touch@80`）；要取回被截条目，把该值原样作 `cursor=` 再调 `bill(form, cursor=该值)`，循环到 "
    "`next_cursor` 为 null。可分页：fields/operations/plugins/bindings/entities/entity_touch。\n"
    "【强制】凡需要解释『某插件/事件/操作在做什么』、判断『是否入库』、确认『插件类型或事件触发"
    "时机』、或读到不认识的 kd.bos.* 等平台符号时，**必须先调 cosmic_semantics(topic) 取苍穹语义"
    "再下结论**，不得仅凭读源码臆断时机与入库——这类领域语义模型易记错，是幻觉高发区。"
    "（纯定位字段坐标 trace、纯调用链导航 method_calls 等不涉及语义解释的取证，无需先调"
    " cosmic_semantics，避免空耗。）不确定该取哪一篇时，先 cosmic_semantics(\"\") 列清单——"
    "每条都带『何时用』说明，按它挑主题名再取全文。\n"
    "【字段名纪律】凡在源码中引用 `<isv>_前缀` 字段标识并要陈述其中文名/业务含义，**必须先 "
    "resolve_fields 批量比对元数据**——命名惯例（`zjjnqk` 是租金还是资金？）不算证据，是幻觉高发区。"
    "resolve_fields 比 trace 便宜得多（O(1) 打词典，只回名字+坐标），读一段代码就顺手批量核一次；"
    "回 null 的字段标 unknown，不猜。\n"
    "【返回值已带证据，直接采用】trace/bill/ask/method_calls 的返回里已**内联**两样东西，请直接用、"
    "勿覆写：① 字段旁的 `field_name`/中文名是已核对的真名，引用时照抄，不得按命名惯例改写；"
    "② 事件方法旁的 `semantics_topic`（如 plugin-form/plugin-operation）指明该去哪篇语义文档——"
    "凡要解释该事件「在干嘛/何时触发/是否入库」，先 cosmic_semantics(该 topic) 再下结论。"
    "字段无 `field_name`（null）或事件无 `semantics_topic` 时，标 unknown，不臆造。"
    "method_calls 还回该方法的 `fields`（本方法读写字段+已核对名+是否落库），引用其字段名照抄。\n"
    "【读源码优先用 read_source】要读项目源文件，**优先调 read_source（不要用宿主原生 reader）**："
    "野生码（GBK/GB2312/UTF-8±BOM）原生 reader 易乱码、且不标字段名；read_source 正确解码、行号对齐 KB，"
    "并自动回 `field_names`（本文件出现的字段标识→真实中文名，已按本文件数据包来源做归属消歧）。"
    "`field_names[key].tier`：unique/resolved 的 `names` 可直接照抄；**tier=ambiguous 表示多张单据有"
    "同名字段、本文件未解析到具体实体——别默认当前单据，按 note 顺调用链消歧**。"
    "未列出的 `<isv>_` 标识 resolve_fields 核对，绝不按命名惯例/拼音猜。"
    "**`getDynamicObjectCollection(key)` 取分录行还是多选基础资料集合，取决于 key 是什么——"
    "看坐标的 `field_type`/`access`（基础资料字段≠分录），别凭 API 名断定是分录。** "
    "read_source 为防截断的紧凑投影：标注 `field_names` 在前，源码正文 `content` 按预算填充；"
    "`content` 未读全时带 `content_next_cursor`（如 `content@120`），把该值原样作 `cursor=` 再调 "
    "`read_source(relpath, cursor=该值)` 即从该行**续读至文件末尾**，循环到 `next_cursor` 为 null 读完"
    "（要限定上界改用 `end_line`）；`field_names` 超档同带 `field_names_next_cursor` 可翻页取全。"
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
def tool_ask(question: str) -> dict[str, Any]:
    """自然语言提问 → 意图解析 → 查 KB 取确定性证据包。**兜底路由**：已知精确字段/单据/方法标识时
    优先直接用 trace/bill/method_calls（更稳），ask 只用于意图判不准、需要先消歧的自然语言提问。

    覆盖旗舰意图：字段谁改的 / 单据钻取 / 插件解释 / 操作解释。判不准时返回
    `status='need_clarification'` + `candidates` 候选——请挑一个精确标识或用
    `单据.字段` 点号坐标再问，绝不替用户拍板。
    """
    from ..semantic import resolver
    from ..context import builder
    from ..report import field_trace, bill_view

    conn = _open()
    try:
        rq = resolver.resolve(conn, question)
        result = builder.build_context(conn, rq)
        # 字段/单据意图的 evidence 是完整富 dict，经 MCP 同样会被 host 截断——换成紧凑投影
        # （cap + 字节 governor + 游标分页）。复用 rq，不动 builder/CLI 路径。
        if result.get("status") == "ok":
            if rq.intent == "field_who_changed":
                result["evidence"] = field_trace.trace_compact(
                    conn, rq.field_key,
                    form_key=rq.form_key, entry_key=rq.entry_key, level=rq.level)
            elif rq.intent == "bill_drilldown":
                result["evidence"] = bill_view.bill_compact(conn, rq.form_key)
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
    """旗舰直查：字段 → 哪些类的哪个事件函数读/写它、是否落库、行号、源码路径。

    `field` 支持点号坐标 `单据.字段` / `单据.分录.字段` / `单据.分录.子分录.字段`（裸字段=
    列全部定义坐标）；`form/entry/level` 可显式覆盖点号推断。
    **优先带上坐标（`单据.字段` 或 `form=`）再查**：裸字段会命中该字段的全部单据，返回值会按
    坐标组（`groups`）裁剪——真实组数在 `groups_total`、被截组数在 `groups_capped`，并不丢数，但
    要看全某张单据的明细仍需带坐标重查。已知是哪张单据/哪级分录就一次性带上，省一轮往返。

    **写/读拆分（`access`）+ 按类合并（防 host 32KB 截断）**：
    - `access='write'`（或默认含写入）：写入按**坐标 → 类 → 写入点**合并——同一类只出现一次，
      类级信息（类型/所属单据）只存一份，行号/落库等列在该类的 `sites`。
    - `access='read'`：读取按**类 → 方法**合并（`{class_fqn, methods:[{method, count, calls}], total}`）；
      要弄清"谁读了它"就顺 `calls` 去那几个方法读源码。
    - **默认（不传 access）**：写入明细 + 读取**仅按类计数概览** `readers_overview`（最省）；要读取
      明细就再调一次 `access='read'`。
    **真实总数恒在 `summary`**，被 cap 截断的数在各节点 `capped`/`sites_capped`/`methods_capped`/
    `groups_capped`（红线 #4 不丢数）。

    **游标分页（`cursor`）——被 cap 的内容一条不丢、全部可取回**：某段被截时，它会带一个
    `next_cursor`（如 `"unlocated@4"` / `"readers@20"` / `"dynamic_writers@4"`）。**要看被截掉的条目，
    不要只读计数——把该 `next_cursor` 原样作为 `cursor=` 再调一次本工具**，即返回该段从该 offset 起、
    预算内能装下的下一页 `page.items` + 新的 `page.next_cursor`；循环直到 `next_cursor` 为 `null` 即取完
    全部。可分页段：writers / readers / unlocated / dynamic_writers / possible / coarse / occurrences
    （writers/readers 需先用 `form/entry/level` 收窄到单坐标）。也可改用 `form/entry/level` 收窄、
    或 `access='read'`/`'write'` 单看一侧来减小单次返回。

    返回里 `unlocated` 是**「反推来源单据」工作单**：确实读写该字段、但来源单据未钉出（form_key=None）的
    读写，按方法去重 + `calls` 导航 + `plugin_form_label`（插件注册单据，仅来源线索非确诊）——顺 calls
    读源码反推它操作哪张单据，勿把 plugin_form_label 当确定来源。与 `dynamic_writers`（字段钉不出）区分。
    每段带 `null_reason` 成因码 + `summary.unlocated_by_reason` 直方图：`basedata-ref`（读基础资料自身字段）/
    `dynamic-entity` 是正确 None（无需追）；`basedata-write-suspect`（写到基础资料，疑似扫描误绑）应继续追；
    `helper-caller-unknown`/`local-or-container-source` 值得读源码反推。
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
    """单据钻取：操作集 / 插件清单 / 字段触达（按实体）/ 桥接风险。

    **紧凑投影（防 host 32KB 截断）**：每字段被触达的逐条事件已折叠为「写/落库/读」计数——要看
    『某字段谁改的/在哪个事件函数/是否落库』，对该字段用 `trace 单据.字段`（entity_touch 每行已带 trace 锚点）。
    各列表真实总数在 `*_total`；被 cap 截掉的段带 `*_next_cursor`。

    **游标分页（`cursor`）——被 cap 的条目一条不丢、全部可取回**：某段被截时它带一个
    `next_cursor`（如 `"fields@60"` / `"entity_touch@80"` / `"plugins@40"`）。要看被截掉的条目，
    **把该值原样作 `cursor=` 再调一次本工具** `bill(form_key, cursor='fields@60')`，返回 `page.items`
    （该段下一页）+ 新的 `page.next_cursor`；循环到 `next_cursor` 为 `null` 即把该段取全。
    可分页段：fields / operations / plugins / bindings / entities / entity_touch。
    """
    from ..report import bill_view

    conn = _open()
    try:
        return bill_view.bill_compact(conn, form_key, cursor=cursor)
    finally:
        conn.close()


def tool_method_calls(class_fqn: str, method_name: str) -> dict[str, Any]:
    """方法出向调用导航：类全限定名 + 方法名 → 该方法调用的**项目内**方法及位置。**只导航不解释**——
    「这个方法在干嘛」请顺返回的 `target_relpath` 用 read_source 读源码自己判，本工具不复述源码逻辑。

    给一个能直接读本机源码的大模型：读到方法体里 `xxxService.doX()`，本工具确定性回答
    「`doX` 定义在项目里哪个类、哪个源文件」（多 ISV 前缀野生码上盲 grep 易命中错类）。
    每条给 调用名 + 目标类全限定名（`target_fqn`，可再对它 `method_calls` 逐层下钻）+
    目标源码相对路径（`target_relpath`，去这个文件接着读）+ 调用行号。
    **只列项目内可下钻调用**——平台/外部调用、`equals`/常量、源码全文与字段落库取证一律不回
    （源码请大模型直接读源文件做完整理解；字段落库取证用 `trace`）。**本工具只回最小导航包**。
    类/方法判不准时返回 `found=False` + `candidates`，请挑全限定名/正确方法名再查。
    """
    from ..report import method_calls

    conn = _open()
    try:
        return method_calls.method_calls(conn, class_fqn, method_name)
    finally:
        conn.close()


def tool_resolve_fields(keys: list[str]) -> dict[str, Any]:
    """字段标识 → 真实元数据中文名+实体坐标（比对元数据、防命名惯例臆断）。

    **边界**：手上只有一串字段 key、**并不在读某个源文件**时才用本工具；**已经在读源码**用 read_source
    即可（它已自动回 `field_names`，无需再调本工具）。批量传 key，回 `{"resolved": {key: [{...}] | null}}`。
    比 trace 便宜得多（O(1) 打词典，只回名字+坐标，不查谁改了它）——命名惯例（`zjjnqk` 是租金还是
    资金？）不算证据，必须比对。
    - 字段命中：`{kind:"field", name, form_key, entity_key, level, field_kind, field_type, access}`。
      `access` 是派生取值语义：**多选基础资料字段（MulBasedataField）也用 `getDynamicObjectCollection()`
      取选中的基础资料集合，不是分录行**——取分录还是基础资料取决于 key，别凭 API 名当分录。
    - 分录容器命中（读到 `getDynamicObjectCollection("cqkd_zdfl")` 这类**分录 key**）：
      `{kind:"entry"/"subentry"/"header", name, form_key, level, parent_key, access}`（access 标"分录容器"）。
    同一 key 跨多坐标（多分录各有定义、名字可能不同）→ 回 list 全摆出，**不替你选**，
    消歧靠你读代码时的实体上下文。**钉不出回 `null`——标 unknown，绝不臆造。**
    """
    from ..report import resolve_fields

    conn = _open()
    try:
        return resolve_fields.resolve_fields(conn, keys)
    finally:
        conn.close()


def tool_read_source(
    relpath: str, start_line: int | None = None, end_line: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """读项目源码（野生编码正确解码）+ 自动标注其中字段 key 的真实中文名。**读源码优先用本工具**
    （而非宿主原生 reader）；它已自动回 `field_names`，读源码时无需再单独调 resolve_fields 核名。

    凭什么用它而非宿主原生 reader：① 野生码（GBK/GB2312/UTF-8±BOM 混杂）原生 reader 易乱码，本工具按
    建库同款编码探测正确解码、行号还与 KB 对齐；② **自动标注** `field_names`——扫文件里出现的字段标识
    （含 `KEY_X = "cqkd_x"` 的字面值，它就在源码里），打元数据词典回真实中文名+坐标，并按本文件数据包
    来源做**归属消歧**（三档：unique/resolved 可照抄 `names`；**ambiguous=多单据同名、本文件未解析到实体，
    别默认当前单据，按 note 顺调用链消歧**）。引用字段中文名**照抄 `field_names`**，未列出的 `<isv>_` 标识
    用 resolve_fields 核对，**绝不按命名惯例/拼音猜**。坐标带 `field_type`/`access`：`getDynamicObjectCollection(key)`
    取分录行还是多选基础资料集合取决于 key——基础资料字段（MulBasedataField）≠分录，**别凭 API 名断定是分录**。
    `start_line/end_line`（1 基含端点）可只读一段（大文件按区间读）；越界路径（../ 逃逸出源码根）会被拒。
    本工具只做"正确解码 + 字段名标注"，代码逻辑理解由你直接读返回的 `content`。

    **紧凑投影 + 游标分页（防 host 32KB 截断）**：标注 `field_names` 在前（高价值、有界），源码正文
    `content` 按字节预算填充。`content` 未读全时带 `content_next_cursor`（如 `"content@120"`）——把该值
    原样作 `cursor=` 再调一次本工具 `read_source(relpath, cursor='content@120')`，即从该行**续读至文件
    末尾**（逐页 `page.content` + 新 `next_cursor`，到 `null` 读完）；要限定上界改用 `end_line` 重调。
    `field_names` 超档时带 `field_names_next_cursor`，同法翻页取回全部标注（红线 #4：被截内容可达、不丢）。
    """
    from ..report import read_source

    conn = _open()
    try:
        return read_source.read_source_compact(
            conn, relpath, start=start_line, end=end_line, cursor=cursor)
    finally:
        conn.close()


def tool_cosmic_semantics(topic: str = "") -> dict[str, Any]:
    """苍穹领域语义文档查询：插件类型/事件时机/SDK 用法/DynamicObject 路径/入库判断/反模式黑名单。
    **不确定该取哪一篇时，先空参 `cosmic_semantics("")` 列清单**——返回每个主题名 + 一行『何时用』，
    照它挑一个 topic 再调取全文。

    随包语义文档（`cosmic_kb/semantics/`，分发改造后下沉进包）按主题取一篇 markdown 全文，
    让**任意 MCP agent**（不止 Claude）都能拿到苍穹纪律与领域知识。
    - `topic` 命中（相对路径 / 文件名 stem / 子串，如 `plugin-base`、`anti-patterns`、`sdk-orm-access`）
      → 返回 `{topic, content}`（单篇全文）。
    - `topic` 空或未命中 → 返回 `{status:'need_topic', available_topics:[...], grouped:{...}}`，
      请先在清单里挑一个再调。
    **本工具只回一篇语义文档**；项目源码全文请大模型直接读本机源文件，字段落库取证用 `trace`。
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
    "read_source": tool_read_source,
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
