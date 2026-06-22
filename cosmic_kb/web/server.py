"""阶段 4.5 · 本地 Web 服务（标准库 http.server）。

把阶段 4 的 KB 当数据底座，开一个**只读、仅本机**的 HTTP 服务：`/api/*` 把现成报告 dict
原样吐 JSON，其余路径发 `static/` 下的最小单页前端。守红线见 `web/__init__.py`。

设计要点：
  * **路由抽成纯函数** `handle_api(conn, path, query)`——与 socket 解耦，单测可直接调（不起服务）。
  * **每请求开/关 KB 连接**：sqlite 连接非线程安全，`ThreadingHTTPServer` 多线程下不能共享。
  * **后端零新增**：直接复用 overview / project_map / store.search，不重算、不改 KB。
"""

from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..graph import store
from ..report import bill_view as bill_report
from ..report import coverage as coverage_report
from ..report import field_trace as field_report
from ..report import overview as overview_report
from ..report import project_map
from ..report import scan_compare as scan_compare_report

_STATIC_DIR = Path(__file__).with_name("static")

# 静态文件后缀 → content-type（够用即可，全本地、无需 mimetypes 大全）。
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

# 表单清单 SQL：与 report/overview.py 同款口径（按字段规模降序，接手者先看大单据）。
_FORMS_SQL = (
    "SELECT f.key,f.name,f.form_type,f.module,"
    "       (SELECT COUNT(*) FROM entity e WHERE e.form_key=f.key) entity_count,"
    "       (SELECT COUNT(*) FROM field fl WHERE fl.form_key=f.key) field_count "
    "FROM form f ORDER BY field_count DESC"
)


def _forms(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(_FORMS_SQL).fetchall()]


def _whois(conn: sqlite3.Connection, q: str) -> dict[str, Any]:
    """类名/关键词反查：这个类绑在哪个单据的什么操作、触达哪些字段、源码在哪。

    支持贴全限定名或末段类名（报错栈里常见）。
    """
    like = f"%{q}%"
    bindings = [dict(r) for r in conn.execute(
        "SELECT class_name,form_key,plugin_type,status,source_relpath,confidence,note "
        "FROM binding WHERE class_name LIKE ? ORDER BY status,class_name LIMIT 100", (like,),
    ).fetchall()]
    # 该类（们）触达的字段（写优先）。
    fqns = sorted({b["class_name"] for b in bindings})
    touches: list[dict[str, Any]] = []
    if fqns:
        ph = ",".join("?" * len(fqns))
        touches = [dict(r) for r in conn.execute(
            f"SELECT plugin_fqn,form_key,field_key,level,entry_key,access,persists,"
            f"access_class,event_method,line,source_relpath "
            f"FROM field_access WHERE plugin_fqn IN ({ph}) "
            f"ORDER BY access,persists,field_key LIMIT 300", fqns,
        ).fetchall()]
    return {"query": q, "bindings": bindings, "touches": touches}


def handle_api(conn: sqlite3.Connection, path: str, query: dict[str, list[str]]) -> tuple[int, Any]:
    """纯函数路由：返回 (http_status, payload)。payload 由调用方序列化为 JSON。

    path 是已去掉 query 的请求路径（如 `/api/bill/cqkd_assetcard`）。
    """
    if path == "/api/overview":
        return 200, overview_report.overview(conn)
    if path == "/api/coverage":
        return 200, coverage_report.coverage(conn)
    if path == "/api/scan-compare":
        return 200, scan_compare_report.compare(conn)
    if path == "/api/field":
        key = (query.get("key") or [""])[0].strip()
        if not key:
            return 400, {"error": "缺少字段标识参数 key"}
        # 点号格式（元数据.[分录.[子分录.]]字段）→ 解析出层级坐标；显式参数覆盖。
        field_key, form, entry, level = field_report.parse_locator(key)
        form = (query.get("form") or [None])[0] or form
        entry = (query.get("entry") or [None])[0] or entry
        level = (query.get("level") or [None])[0] or level
        return 200, field_report.field_trace(
            conn, field_key, form_key=form or None, entry_key=entry or None, level=level or None)
    if path == "/api/whois":
        q = (query.get("q") or [""])[0].strip()
        if not q:
            return 200, {"query": "", "bindings": [], "touches": []}
        return 200, _whois(conn, q)
    if path == "/api/modules":
        return 200, project_map.load_map(conn)
    if path == "/api/forms":
        return 200, {"forms": _forms(conn)}
    if path.startswith("/api/bill/"):
        key = unquote(path[len("/api/bill/"):])
        detail = bill_report.bill_view(conn, key)
        if detail is None:
            return 404, {"error": f"单据不存在: {key}"}
        return 200, detail
    if path.startswith("/api/forms/"):  # 兼容旧路径，等价单据详情
        key = unquote(path[len("/api/forms/"):])
        detail = bill_report.bill_view(conn, key)
        if detail is None:
            return 404, {"error": f"单据不存在: {key}"}
        return 200, detail
    if path == "/api/search":
        q = (query.get("q") or [""])[0].strip()
        if not q:
            return 200, {"query": "", "results": []}
        rows = store.search(conn, q)
        return 200, {"query": q, "results": [dict(r) for r in rows]}
    return 404, {"error": f"未知端点: {path}"}


def _safe_static(rel: str) -> Path | None:
    """把 URL 路径映射到 static 目录内的真实文件，做路径穿越防护。

    返回 None 表示越界或不存在（调用方发 404），绝不泄露 static 之外的文件。
    """
    rel = rel.lstrip("/")
    if not rel:
        rel = "index.html"
    target = (_STATIC_DIR / rel).resolve()
    try:
        target.relative_to(_STATIC_DIR.resolve())
    except ValueError:
        return None  # 越界（../ 穿越）
    if not target.is_file():
        return None
    return target


class _Handler(BaseHTTPRequestHandler):
    """只读 GET 服务：/api/* 查 KB 吐 JSON，其余发 static/ 静态文件。"""

    server_version = "cosmic_kb-web"
    db_path: str = ""  # 由 serve() 注入到子类

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, target: Path) -> None:
        body = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 约定大写)
        parts = urlsplit(self.path)
        path = parts.path
        if path.startswith("/api/"):
            try:
                conn = store.open_kb(self.db_path)
                try:
                    status, payload = handle_api(conn, path, parse_qs(parts.query))
                finally:
                    conn.close()
            except Exception as exc:  # KB 损坏/查询异常：以 500 JSON 反馈，不让服务崩
                self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            self._send_json(status, payload)
            return

        target = _safe_static(path)
        if target is None:
            self._send_json(404, {"error": f"未找到: {path}"})
            return
        self._send_file(target)

    def log_message(self, fmt: str, *args: Any) -> None:
        """精简日志：只在 stderr 打一行，避免刷屏（也便于测试静默）。"""
        import sys
        sys.stderr.write(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}\n")


def make_server(db_path: str, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """建 server（绑定但不阻塞）。供 serve() 与测试复用。"""
    handler = type("_BoundHandler", (_Handler,), {"db_path": str(db_path)})
    return ThreadingHTTPServer((host, port), handler)


def serve(
    db_path: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    """起本机 Web 服务并阻塞，Ctrl+C 干净退出。默认仅绑 127.0.0.1（本地离线红线）。"""
    httpd = make_server(db_path, host=host, port=port)
    url = f"http://{host}:{port}"
    print(f"✅ Cosmic KB Web 已启动: {url}")
    print(f"   KB: {db_path}")
    print("   仅本机可达（127.0.0.1），Ctrl+C 停止。")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
