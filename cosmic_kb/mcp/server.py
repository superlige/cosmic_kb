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
    """自然语言提问 → 意图解析 → 查 KB 取确定性证据包。

    覆盖旗舰意图：字段谁改的 / 单据钻取 / 插件解释 / 操作解释。判不准时返回
    `status='need_clarification'` + `candidates` 候选——请挑一个精确标识或用
    `单据.字段` 点号坐标再问，绝不替用户拍板。
    """
    from ..semantic import resolver
    from ..context import builder

    conn = _open()
    try:
        rq = resolver.resolve(conn, question)
        return builder.build_context(conn, rq)
    finally:
        conn.close()


def tool_trace(
    field: str,
    form: str | None = None,
    entry: str | None = None,
    level: str | None = None,
) -> dict[str, Any]:
    """旗舰直查：字段 → 哪些插件的哪个事件函数读/写它、是否落库、行号、源码路径。

    `field` 支持点号坐标 `单据.字段` / `单据.分录.字段` / `单据.分录.子分录.字段`（裸字段=
    列全部定义坐标）；`form/entry/level` 可显式覆盖点号推断。
    """
    from ..report import field_trace

    conn = _open()
    try:
        field_key, form_key, entry_key, lvl = field_trace.parse_locator(field)
        return field_trace.field_trace(
            conn,
            field_key,
            form_key=form or form_key,
            entry_key=entry or entry_key,
            level=level or lvl,
        )
    finally:
        conn.close()


def tool_bill(form_key: str) -> dict[str, Any]:
    """单据钻取：操作集 / 插件清单 / 字段触达（按实体）/ 桥接风险。"""
    from ..report import bill_view

    conn = _open()
    try:
        bv = bill_view.bill_view(conn, form_key)
        return bv if bv is not None else {"error": f"单据不存在: {form_key}"}
    finally:
        conn.close()


def tool_coverage() -> dict[str, Any]:
    """信任优先·手段一：字段覆盖率（元数据为分母）+ 四维扫描质量分解。"""
    from ..report import coverage

    conn = _open()
    try:
        return coverage.coverage(conn)
    finally:
        conn.close()


def tool_scan_compare() -> dict[str, Any]:
    """信任优先·手段二：粗精度(源码字面量) vs 高精度(field_access) 对比 → 疑似盲点/精度增量。"""
    from ..report import scan_compare

    conn = _open()
    try:
        return scan_compare.compare(conn)
    finally:
        conn.close()


# 工具名 → 纯逻辑函数（build_server 注册用，测试也按此遍历核对）。
TOOLS = {
    "ask": tool_ask,
    "trace": tool_trace,
    "bill": tool_bill,
    "coverage": tool_coverage,
    "scan_compare": tool_scan_compare,
}


def build_server():
    """构造 FastMCP 服务器并注册工具（此处才 import mcp，未装 [mcp] 时不影响模块 import）。"""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # noqa: BLE001
        raise RuntimeError(
            "未安装 MCP SDK。请先  pip install -e \".[mcp]\"  （或 pip install mcp）。"
        ) from e

    mcp = FastMCP("cosmic_kb")
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
