"""阶段 4.5 验收测试 —— 本地 Web 展示层。

覆盖：handle_api 各端点（纯函数，不起服务）、静态路径穿越防护、ThreadingHTTPServer 集成
（仅绑 127.0.0.1）、CLI web 子命令注册与 KB 缺失报错。前端只验静态文件可达，不跑浏览器。
"""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

from cosmic_kb.bridge import linker, namespace
from cosmic_kb.graph import store
from cosmic_kb.ingest import scanner
from cosmic_kb.metadata.model import MetaField, MetaModel, MetaPlugin
from cosmic_kb.report import project_map
from cosmic_kb.web import server


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _build_kb(tmp_path: Path) -> Path:
    """造一个最小双模块项目 + 一个带字段的表单，建 KB，返回 db 路径。"""
    _write(tmp_path / "AssetCardFormPlugin.java",
           "package cqspb.assets;\npublic class AssetCardFormPlugin {}\n")
    _write(tmp_path / "AssetCardService.java",
           "package cqspb.assets;\npublic class AssetCardService {}\n")
    scan = scanner.scan(tmp_path)
    fields = [
        MetaField("TextField", "cqkd_name", "名称", "fname", "id1", None, "entity", "header", "cqkd_assetcard"),
        MetaField("DecimalField", "cqkd_amount", "金额", "famount", "id2", None, "entity", "header", "cqkd_assetcard"),
    ]
    models = [
        MetaModel(key="cqkd_assetcard", name="资产卡片", model_type="BillFormModel",
                  form_type="bill", isv="cqkd", app_key="cqkd_assets", fields=fields,
                  plugins=[MetaPlugin(class_name="cqspb.assets.AssetCardFormPlugin",
                                      plugin_type="form", source="project")]),
    ]
    index = namespace.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    mm = project_map.module_map(scan, models, bridge, index=index)
    db = tmp_path / "kb.db"
    store.build_kb(scan, models, bridge, mm, db, index=index)
    return db


# ── handle_api 纯函数 ────────────────────────────────────────────────────────

def test_api_overview(tmp_path: Path):
    conn = store.open_kb(_build_kb(tmp_path))
    try:
        status, payload = server.handle_api(conn, "/api/overview", {})
        assert status == 200
        assert "overview" in payload and "risk" in payload and "module_map" in payload
        # 首页「重点单据」导航要的每单据插件信号（排障导航，不是纯规模）。
        assert payload["forms"], "应有表单清单"
        assert {"plugin_count", "op_with_plugin_count"} <= set(payload["forms"][0])
    finally:
        conn.close()


def test_api_coverage(tmp_path: Path):
    """扫描可信度端点：手段一字段覆盖率 + 质量分解结构齐全。"""
    conn = store.open_kb(_build_kb(tmp_path))
    try:
        status, payload = server.handle_api(conn, "/api/coverage", {})
        assert status == 200
        assert {"field_coverage", "resolution_quality", "location_quality",
                "persist_quality", "meta_match", "upstream", "verdict"} <= set(payload)
        fc = payload["field_coverage"]
        assert {"business_total", "touched", "rate", "by_module",
                "low_coverage_forms", "by_kind"} <= set(fc)
        # 最小项目 2 个 entity 字段 → 分母为 2。
        assert fc["business_total"] == 2
    finally:
        conn.close()


def test_api_scan_compare(tmp_path: Path):
    """手段二端点：粗精度 vs 高精度对比结构齐全（端到端走真实 build_kb 的粗扫表）。"""
    conn = store.open_kb(_build_kb(tmp_path))
    try:
        status, payload = server.handle_api(conn, "/api/scan-compare", {})
        assert status == 200
        assert {"universe", "coarse_hit", "high_hit", "both", "coarse_only",
                "high_only", "neither", "coarse_only_list", "high_only_list",
                "verdict"} <= set(payload)
        # 集合恒等式：两侧并集 + 都没碰 = 全集。
        assert payload["covered_either"] + payload["neither"] == payload["universe"]
    finally:
        conn.close()


def test_api_modules(tmp_path: Path):
    conn = store.open_kb(_build_kb(tmp_path))
    try:
        status, payload = server.handle_api(conn, "/api/modules", {})
        assert status == 200
        assert "modules" in payload and "health" in payload
        assert any(m["name"] == "cqkd_assets" for m in payload["modules"])
    finally:
        conn.close()


def test_api_forms_and_detail(tmp_path: Path):
    conn = store.open_kb(_build_kb(tmp_path))
    try:
        status, payload = server.handle_api(conn, "/api/forms", {})
        assert status == 200
        keys = {f["key"] for f in payload["forms"]}
        assert "cqkd_assetcard" in keys

        status, detail = server.handle_api(conn, "/api/forms/cqkd_assetcard", {})
        assert status == 200
        assert detail["form"]["name"] == "资产卡片"
        assert {f["key"] for f in detail["fields"]} == {"cqkd_name", "cqkd_amount"}
        assert any(p["class_name"] == "cqspb.assets.AssetCardFormPlugin" for p in detail["plugins"])
        assert detail["bindings"]  # 桥接绑定有记录

        status, miss = server.handle_api(conn, "/api/forms/不存在", {})
        assert status == 404 and "error" in miss
    finally:
        conn.close()


def test_api_search(tmp_path: Path):
    conn = store.open_kb(_build_kb(tmp_path))
    try:
        status, payload = server.handle_api(conn, "/api/search", {"q": ["资产卡片"]})
        assert status == 200
        assert any(r["name"] == "资产卡片" for r in payload["results"])
        # 空 q 返回空结果、不报错。
        status, empty = server.handle_api(conn, "/api/search", {})
        assert status == 200 and empty["results"] == []
    finally:
        conn.close()


def test_api_field_and_whois(tmp_path: Path):
    """旗舰端点 /api/field 与 /api/whois 的形状（数据细节由 test_java_field 覆盖）。"""
    conn = store.open_kb(_build_kb(tmp_path))
    try:
        status, ft = server.handle_api(conn, "/api/field", {"key": ["cqkd_name"]})
        assert status == 200
        assert {"field_key", "writers", "readers", "summary", "occurrences", "groups",
                "coarse"} <= set(ft)
        # 缺 key → 400。
        status, err = server.handle_api(conn, "/api/field", {})
        assert status == 400 and "error" in err
        # 反查端点返回绑定 + 字段触达结构。
        status, w = server.handle_api(conn, "/api/whois", {"q": ["AssetCardFormPlugin"]})
        assert status == 200
        assert {"bindings", "touches"} <= set(w)
        assert any(b["class_name"].endswith("AssetCardFormPlugin") for b in w["bindings"])
    finally:
        conn.close()


def test_api_bill(tmp_path: Path):
    conn = store.open_kb(_build_kb(tmp_path))
    try:
        status, bv = server.handle_api(conn, "/api/bill/cqkd_assetcard", {})
        assert status == 200
        assert bv["form"]["name"] == "资产卡片"
        assert "operations" in bv and "field_touch" in bv
    finally:
        conn.close()


def test_api_unknown_404(tmp_path: Path):
    conn = store.open_kb(_build_kb(tmp_path))
    try:
        status, payload = server.handle_api(conn, "/api/nope", {})
        assert status == 404 and "error" in payload
    finally:
        conn.close()


# ── 静态路径穿越防护 ─────────────────────────────────────────────────────────

def test_safe_static_blocks_traversal():
    assert server._safe_static("/") is not None          # index.html 存在
    assert server._safe_static("/app.js") is not None
    assert server._safe_static("/../server.py") is None  # 越界穿越被拒
    assert server._safe_static("/nope.bin") is None       # 不存在


# ── ThreadingHTTPServer 集成（仅绑 127.0.0.1，端口 0 自动选）─────────────────

def test_server_integration(tmp_path: Path):
    db = _build_kb(tmp_path)
    httpd = server.make_server(str(db), host="127.0.0.1", port=0)
    host, port = httpd.server_address
    assert host == "127.0.0.1"  # 仅本机可达
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(base + "/api/overview", timeout=5) as r:
            assert r.status == 200
            ov = json.loads(r.read().decode("utf-8"))
            assert "overview" in ov
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert r.status == 200
            html = r.read().decode("utf-8")
            assert "苍穹项目排障" in html
    finally:
        httpd.shutdown()
        httpd.server_close()


# ── CLI 注册 ─────────────────────────────────────────────────────────────────

def test_cli_web_missing_kb_errors(tmp_path: Path):
    """KB 不存在且未给重建入参 → 报错提示先 build（不真正起服务）。"""
    import contextlib
    import io

    from cosmic_kb.cli.main import main

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = main(["web", "--db", str(tmp_path / "nope.db")])
    assert rc == 2
    assert "KB 不存在" in buf.getvalue()
