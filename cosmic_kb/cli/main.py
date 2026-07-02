"""cosmic_kb 命令行入口。

已实现命令：
    cosmic_kb --version       # 版本输出
    cosmic_kb doctor          # 检查 skill_assets 资产接线
    cosmic_kb ingest <路径>   # 阶段1：摄取源码 + 解析覆盖率/可信度报告
    cosmic_kb meta <dym|zip|dir...>  # 阶段2：解析 dym / 整包 / 多包 / 目录元数据
    cosmic_kb bridge <源码根> <dym|zip|dir...>  # 阶段3：元数据 ClassName ↔ 源码桥接
    cosmic_kb build <源码根> <dym|zip|dir...>   # 阶段4：建/重建 Cosmic KB（SQLite+FTS5）
    cosmic_kb report map | overview             # 阶段4：项目地图 / 接手者理解报告（读 KB）
    cosmic_kb web [--port --host --open]        # 阶段4.5：本地 Web 展示（读 KB，仅本机 localhost）
    cosmic_kb ask "<自然语言问题>"              # 阶段9：NL→意图→查 KB 取证（确定性证据包）

后续阶段在此挂载更多子命令（mcp ...）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .. import __version__
from .. import _assets

# Windows 控制台默认 GBK，无法编码中文/箭头等字符 —— 统一切到 UTF-8。
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _cmd_doctor(_args: argparse.Namespace) -> int:
    print(f"# cosmic_kb {__version__}")
    print("")

    statuses = _assets.check_assets()
    for status in statuses:
        print(f"{status.label:<10}{status.name:<20}{status.detail}")

    # 只有**非可选**资产缺失才算体检失败（ok-cosmic-docs.db 运行期暂未消费，缺它不阻断）。
    missing = [s for s in statuses if not s.present and not s.optional]
    print("")
    if missing:
        print(f"缺失关键资产 {len(missing)} 项（随包数据应已就位，缺失多为安装损坏）。")
        return 1
    print("关键资产就位。")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    """阶段1：摄取源码并产出解析覆盖率/可信度报告。"""
    from ..report import parse_coverage

    try:
        report = parse_coverage.analyze(
            args.path, follow_symlinks=args.follow_symlinks
        )
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(parse_coverage.render(report, max_error_files=args.max_error_files))

    # 退出码：读取失败或解析依赖缺失时给非零，便于脚本判断信任门槛是否通过。
    if report.total == 0:
        return 1
    if report.read_failed:
        return 1
    return 0


def _cmd_meta(args: argparse.Namespace) -> int:
    """阶段2：解析 dym / 整包 zip / 多包 / 目录元数据，产出 JSON 快照 / 分类计数报告。

    入参可同时给多个：单个 .dym、单个 .zip、含多个 zip 的目录、或多个 zip 路径。
    生产项目通常一个 zip ≈ 一个业务模块 → 多包时跨模块汇总（验收补强项）。
    """
    from ..metadata import dym_parser, package_loader
    from ..metadata.template_loader import TemplateRegistry
    from ..report import meta_report

    # 1) 校验 + 展开输入：目录→其下 zip，文件按后缀归类。
    zips: list[Path] = []
    dyms: list[Path] = []
    for raw in args.paths:
        if not os.path.exists(raw):
            print(f"错误: 路径不存在: {raw}", file=sys.stderr)
            return 2
        if os.path.isdir(raw):
            found = package_loader.discover_zips(raw)
            if not found:
                print(f"警告: 目录下未发现 .zip: {raw}", file=sys.stderr)
            zips.extend(found)
        elif raw.lower().endswith(".zip"):
            zips.append(Path(raw))
        elif raw.lower().endswith((".dym", ".cr")):
            dyms.append(Path(raw))
        else:
            print(f"错误: 不支持的输入(需 .dym / .cr / .zip / 含 zip 的目录): {raw}", file=sys.stderr)
            return 2

    registry = TemplateRegistry(args.template_dir) if args.template_dir else TemplateRegistry()

    # 2) zip 优先（dym 与 zip 混传时只处理 zip，给出提示）。
    if zips:
        if dyms:
            print("提示: 多包模式仅处理 zip/目录，已忽略同时传入的 .dym 文件", file=sys.stderr)

        # 单包：保持原单包报告（信息更全：列出全部表单）。
        if len(zips) == 1:
            def _progress(done: int, total: int, _member: str) -> None:
                if not args.json and (done % 50 == 0 or done == total):
                    print(f"\r解析中 {done}/{total} …", end="", file=sys.stderr, flush=True)

            result = package_loader.load_package(
                zips[0], template_registry=registry, progress=_progress, limit=args.limit
            )
            if not args.json:
                print("", file=sys.stderr)  # 进度行收尾换行
            if args.json:
                print(json.dumps(meta_report.package_summary(result), ensure_ascii=False, indent=2))
            else:
                print(meta_report.render_package(result, max_list=args.max_list))
            return 1 if result.failed_entries and not result.ok_entries else 0

        # 多包：跨模块汇总。
        def _mp_progress(name: str, done: int, total: int, _member: str) -> None:
            if not args.json and (done % 50 == 0 or done == total):
                print(f"\r[{name}] 解析中 {done}/{total} …", end="", file=sys.stderr, flush=True)

        multi = package_loader.load_packages(
            zips, template_registry=registry, progress=_mp_progress, limit=args.limit
        )
        if not args.json:
            print("", file=sys.stderr)
        if args.json:
            print(json.dumps(meta_report.multi_package_summary(multi), ensure_ascii=False, indent=2))
        else:
            print(meta_report.render_multi_package(multi, max_list=args.max_list))
        return 1 if multi.failed_count and not multi.ok_count else 0

    # 3) 纯 dym（一个或多个）。
    if not dyms:
        print("错误: 未发现可解析的 .dym / .zip", file=sys.stderr)
        return 2

    rc = 0
    models = []
    for d in dyms:
        try:
            models.append(dym_parser.parse_file(str(d), template_registry=registry))
        except Exception as exc:
            print(f"解析失败: {d}: {type(exc).__name__}: {exc}", file=sys.stderr)
            rc = 1

    if args.json:
        # 单个保持裸对象（向后兼容）；多个汇成数组。
        payload = models[0].to_dict() if len(models) == 1 else [m.to_dict() for m in models]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for model in models:
            print(meta_report.render_model(model))
    return rc


def _collect_models(paths: list[str], registry) -> tuple[list, int]:
    """把 meta 风格的输入（.dym / .zip / 含 zip 的目录）展开成 MetaModel 列表。

    返回 (models, rc)；rc 非 0 表示输入有误（调用方据此退出）。复用阶段 2 的解析器，
    桥接只取其产物，不重复造解析逻辑。
    """
    from ..metadata import dym_parser, package_loader

    zips: list[Path] = []
    dyms: list[Path] = []
    for raw in paths:
        if not os.path.exists(raw):
            print(f"错误: 路径不存在: {raw}", file=sys.stderr)
            return [], 2
        if os.path.isdir(raw):
            zips.extend(package_loader.discover_zips(raw))
        elif raw.lower().endswith(".zip"):
            zips.append(Path(raw))
        elif raw.lower().endswith((".dym", ".cr")):
            dyms.append(Path(raw))
        else:
            print(f"错误: 不支持的输入(需 .dym / .cr / .zip / 含 zip 的目录): {raw}", file=sys.stderr)
            return [], 2

    models: list = []
    for z in zips:
        try:
            res = package_loader.load_package(z, template_registry=registry)
            models.extend(e.model for e in res.ok_entries)
        except Exception as exc:
            print(f"整包打开失败: {z}: {type(exc).__name__}: {exc}", file=sys.stderr)
    for d in dyms:
        try:
            models.append(dym_parser.parse_file(str(d), template_registry=registry))
        except Exception as exc:
            print(f"解析失败: {d}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return models, 0


def _cmd_bridge(args: argparse.Namespace) -> int:
    """阶段3：把元数据插件 ClassName 桥接到源码 .java，产出桥接可信度报告。"""
    from ..ingest import scanner
    from ..bridge import linker
    from ..metadata.template_loader import TemplateRegistry
    from ..report import bridge_report

    # 1) 源码侧：扫描项目根。
    try:
        scan_result = scanner.scan(args.source_root, follow_symlinks=args.follow_symlinks)
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    # 2) 元数据侧：展开 dym/zip/目录 → MetaModel 列表。
    registry = TemplateRegistry(args.template_dir) if args.template_dir else TemplateRegistry()
    models, rc = _collect_models(args.meta, registry)
    if rc:
        return rc
    if not models:
        print("错误: 未解析出任何元数据表单（检查 dym/zip 输入）", file=sys.stderr)
        return 2

    # 3) 桥接。
    result = linker.link(scan_result, models)

    if args.json:
        print(json.dumps(bridge_report.summary(result), ensure_ascii=False, indent=2))
    else:
        print(bridge_report.render(result, max_list=args.max_list))

    # 退出码：有 project 插件却命中率为 0 时给非零，便于脚本判断桥接是否可信。
    s = bridge_report.summary(result)
    if s["project_plugin_total"] and s["hit_count"] == 0:
        return 1
    return 0


DEFAULT_DB = "cosmic_kb.db"  # KB 文件默认名（随项目源码根落盘，便于「就近发现」）


def _discover_db(start: Path | None = None) -> Path | None:
    """从 start（默认 cwd）向上逐级查找 DEFAULT_DB，像 git 找 `.git`。

    让用户 cd 进被分析项目（或其任意子目录）后，读类命令无需 --db 即就近用对 KB。
    多项目场景下每个 KB 都随自己源码树落盘，互不干扰。
    """
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        cand = d / DEFAULT_DB
        if cand.is_file():
            return cand
    return None


def _resolve_db(args: argparse.Namespace) -> str:
    """统一解析 KB 路径。优先级：--db > COSMIC_KB_DB > 建库随源码根 > 向上发现 > cwd 兜底。

    设计目标（多项目支持·方案A）：一项目一 KB、各随源码树落盘 → 永不互相覆盖；
    cd 进项目目录即自动就近发现，免去每次手敲 --db。
    """
    if getattr(args, "db", None):  # 1) 用户显式指定，最高优先级
        return str(args.db)
    env = os.environ.get("COSMIC_KB_DB")  # 2) 环境变量（MCP 宿主/脚本常用）
    if env:
        return env
    src = getattr(args, "source_root", None)
    if getattr(args, "creating", False) and src:  # 3) build：随源码根落盘
        return str(Path(src) / DEFAULT_DB)
    found = _discover_db()  # 4) 读类命令：从 cwd 向上就近发现
    if found:
        return str(found)
    if src:  # 5) 读类命令「KB 缺失→临时重建」兜底：落到源码根，与 build 一致
        return str(Path(src) / DEFAULT_DB)
    return DEFAULT_DB  # 6) 最终兜底：cwd/cosmic_kb.db（向后兼容老用法）


def _build_kb(args: argparse.Namespace, db_path: str) -> tuple[dict | None, int]:
    """阶段4 共用：扫描 + 桥接 + 模块识别 + 灌库。返回 (计数摘要, rc)。

    源码索引只建一次，喂桥接与模块识别（守红线「规模大」，不重复解析千百文件）。
    """
    from ..ingest import scanner
    from ..bridge import linker, namespace
    from ..metadata.template_loader import TemplateRegistry
    from ..report import project_map
    from ..graph import store

    try:
        scan_result = scanner.scan(args.source_root, follow_symlinks=args.follow_symlinks)
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return None, 2

    registry = TemplateRegistry(args.template_dir) if args.template_dir else TemplateRegistry()
    models, rc = _collect_models(args.meta, registry)
    if rc:
        return None, rc
    if not models:
        print("错误: 未解析出任何元数据表单（检查 dym/zip 输入）", file=sys.stderr)
        return None, 2

    index = namespace.build_index(scan_result)
    bridge = linker.link(scan_result, models, index=index)
    mm = project_map.module_map(scan_result, models, bridge, index=index)
    counts = store.build_kb(
        scan_result, models, bridge, mm, db_path, index=index,
        source_args={"source_root": str(args.source_root), "meta": list(args.meta)},
    )
    return counts, 0


def _cmd_build(args: argparse.Namespace) -> int:
    """阶段4：建/重建 Cosmic KB（SQLite + FTS5），落盘项目地图与理解报告的数据底座。"""
    counts, rc = _build_kb(args, args.db)
    if rc:
        return rc
    print(f"✅ KB 已建好: {args.db}")
    order = ["module", "form", "entity", "field", "plugin", "convert_rule", "operation",
             "source_class", "binding", "plugin_method", "field_access", "coarse_field_hit",
             "edge", "search"]
    print("  " + "  ".join(f"{k}={counts[k]}" for k in order if k in counts))
    print("  下一步: 在该项目目录下直接  cosmic_kb trace <字段标识>   （自动就近发现此 KB）")
    print(f"          或在任意目录    cosmic_kb trace <字段标识> --db {args.db}")
    return 0


def _ensure_kb(args: argparse.Namespace) -> tuple[str | None, int]:
    """report 共用：确保 KB 就绪。不存在/版本不符时，若给了源码+元数据入参则临时重建。"""
    from ..graph import store

    if store.kb_exists(args.db):
        return args.db, 0
    if getattr(args, "source_root", None) and getattr(args, "meta", None):
        print(f"提示: KB 不存在或版本不符，按入参临时重建 {args.db} …", file=sys.stderr)
        _counts, rc = _build_kb(args, args.db)
        return (args.db, 0) if rc == 0 else (None, rc)
    print(
        f"错误: KB 不存在或版本不符: {args.db}\n"
        f"  请先运行  cosmic_kb build <源码根> <dym|zip|目录>\n"
        f"  或给本命令加  --source-root <源码根> --meta <dym|zip|目录>  以临时重建",
        file=sys.stderr,
    )
    return None, 2


def _cmd_report_map(args: argparse.Namespace) -> int:
    """阶段4：项目地图（多信号模块识别 + 包结构健康度）。"""
    from ..graph import store
    from ..report import project_map

    db, rc = _ensure_kb(args)
    if rc:
        return rc
    conn = store.open_kb(db)
    try:
        mm = project_map.load_map(conn)
        if args.json:
            print(json.dumps(mm, ensure_ascii=False, indent=2))
        else:
            print(project_map.render_map(mm, max_list=args.max_list))
    finally:
        conn.close()
    return 0


def _cmd_report_overview(args: argparse.Namespace) -> int:
    """阶段4：接手者一键理解报告（概览/模块/实体/插件/风险热点）。"""
    from ..graph import store
    from ..report import overview as overview_report

    db, rc = _ensure_kb(args)
    if rc:
        return rc
    conn = store.open_kb(db)
    try:
        ov = overview_report.overview(conn)
        if args.json:
            print(json.dumps(ov, ensure_ascii=False, indent=2))
        else:
            print(overview_report.render_overview(ov, max_list=args.max_list))
    finally:
        conn.close()
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    """信任优先：手段一「字段覆盖率」+ 扫描质量分解（读 KB）。"""
    from ..graph import store
    from ..report import coverage as coverage_report

    db, rc = _ensure_kb(args)
    if rc:
        return rc
    conn = store.open_kb(db)
    try:
        c = coverage_report.coverage(conn)
        if args.json:
            print(json.dumps(c, ensure_ascii=False, indent=2))
        else:
            print(coverage_report.render_coverage(c, max_list=args.max_list))
    finally:
        conn.close()
    return 0


def _cmd_scan_compare(args: argparse.Namespace) -> int:
    """信任优先：手段二「粗精度扫描 vs 高精度扫描对比」（读 KB）。"""
    from ..graph import store
    from ..report import scan_compare

    db, rc = _ensure_kb(args)
    if rc:
        return rc
    conn = store.open_kb(db)
    try:
        c = scan_compare.compare(conn)
        if args.json:
            print(json.dumps(c, ensure_ascii=False, indent=2))
        else:
            print(scan_compare.render_compare(c, max_list=args.max_list))
    finally:
        conn.close()
    return 0


def _cmd_dynwrites(args: argparse.Namespace) -> int:
    """信任优先：全局「动态/未定位写入」审计（字段 key 钉不出 → trace 按字段查不到）。"""
    from ..graph import store
    from ..report import dynamic_writes

    db, rc = _ensure_kb(args)
    if rc:
        return rc
    conn = store.open_kb(db)
    try:
        d = dynamic_writes.summarize(
            conn, form_key=getattr(args, "form", None), cause=getattr(args, "cause", None),
            class_fqn=getattr(args, "cls", None))
        if args.json:
            print(json.dumps(d, ensure_ascii=False, indent=2))
        else:
            print(dynamic_writes.render_dynamic_writes(d, max_list=args.max_list))
    finally:
        conn.close()
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    """字段名核对：标识 → 真实元数据中文名+坐标（防命名惯例臆断；钉不出回 null）。"""
    from ..graph import store
    from ..report import resolve_fields

    db, rc = _ensure_kb(args)
    if rc:
        return rc
    conn = store.open_kb(db)
    try:
        d = resolve_fields.resolve_fields(conn, args.keys)
        if args.json:
            print(json.dumps(d, ensure_ascii=False, indent=2))
        else:
            print(resolve_fields.render_resolve_fields(d, max_list=args.max_list))
    finally:
        conn.close()
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    """阶段5+6+7 旗舰：字段排障追踪（谁改了它·哪个事件函数·是否落库）。"""
    from ..graph import store
    from ..report import field_trace

    db, rc = _ensure_kb(args)
    if rc:
        return rc
    conn = store.open_kb(db)
    try:
        field_key, form_key, entry_key, level = field_trace.parse_locator(args.field)
        # 显式 --form/--entry/--level 覆盖点号推断。
        form_key = args.form or form_key
        entry_key = getattr(args, "entry", None) or entry_key
        level = getattr(args, "level", None) or level
        ft = field_trace.field_trace(
            conn, field_key, form_key=form_key, entry_key=entry_key, level=level)
        if args.json:
            print(json.dumps(ft, ensure_ascii=False, indent=2))
        else:
            print(field_trace.render_field_trace(ft, max_list=args.max_list))
    finally:
        conn.close()
    return 0


def _cmd_bill(args: argparse.Namespace) -> int:
    """阶段4/5：单据钻取视图（操作集 / 插件 / 字段触达 / 风险）。"""
    from ..graph import store
    from ..report import bill_view

    db, rc = _ensure_kb(args)
    if rc:
        return rc
    conn = store.open_kb(db)
    try:
        bv = bill_view.bill_view(conn, args.bill)
        if bv is None:
            print(f"错误: 单据不存在: {args.bill}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(bv, ensure_ascii=False, indent=2))
        else:
            print(bill_view.render_bill(bv, max_list=args.max_list))
    finally:
        conn.close()
    return 0


def _cmd_calls(args: argparse.Namespace) -> int:
    """方法出向调用导航：类全限定名 + 方法名 → 该方法调用的项目内方法（目标类/文件/行）。"""
    from ..graph import store
    from ..report import method_calls

    db, rc = _ensure_kb(args)
    if rc:
        return rc
    conn = store.open_kb(db)
    try:
        rd = method_calls.method_calls(
            conn, args.class_fqn, args.method, source_root=getattr(args, "source_root", None))
        if args.json:
            print(json.dumps(rd, ensure_ascii=False, indent=2))
        else:
            print(method_calls.render_method_calls(rd, max_list=args.max_list))
        if not rd.get("found"):
            # 类/方法歧义有候选 → 退出码 3（同 ask，提示"再问一轮"）；纯未命中 → 2。
            return 3 if rd.get("candidates") else 2
    finally:
        conn.close()
    return 0


def _cmd_source(args: argparse.Namespace) -> int:
    """模式 A：读项目源码（野生编码正确解码）+ 自动标注其中字段 key 的真实中文名。"""
    from ..graph import store
    from ..report import read_source

    db, rc = _ensure_kb(args)
    if rc:
        return rc
    start = end = None
    if getattr(args, "lines", None):
        try:
            lo, _, hi = args.lines.partition("-")
            start = int(lo) if lo else None
            end = int(hi) if hi else None
        except ValueError:
            print("错误: --lines 格式应为 A-B（如 30-60）", file=sys.stderr)
            return 2
    conn = store.open_kb(db)
    try:
        d = read_source.read_source(
            conn, args.relpath, source_root=getattr(args, "source_root", None),
            start=start, end=end)
        if args.json:
            print(json.dumps(d, ensure_ascii=False, indent=2))
        else:
            print(read_source.render_read_source(d, max_list=args.max_list))
        if not d.get("found"):
            return 2
    finally:
        conn.close()
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    """阶段9：自然语言提问 → 确定性证据包（NL→意图→查 KB→Context Builder）。"""
    from ..graph import store
    from ..semantic import resolver
    from ..context import builder

    db, rc = _ensure_kb(args)
    if rc:
        return rc
    conn = store.open_kb(db)
    try:
        rq = resolver.resolve(conn, args.question)
        ctx = builder.build_context(conn, rq)
        if args.json:
            print(builder.to_json(ctx))
        else:
            print(builder.render_context(ctx, max_list=args.max_list))
        # 需消歧时返回非零退出码，方便脚本/Skill 判断"还要再问一轮"。
        return 3 if ctx.get("status") == "need_clarification" else 0
    finally:
        conn.close()


def _cmd_web(args: argparse.Namespace) -> int:
    """阶段4.5：起本机 Web 服务展示项目地图/理解报告（读 KB，仅本机 localhost）。"""
    from ..web import server

    db, rc = _ensure_kb(args)
    if rc:
        return rc
    server.serve(db, host=args.host, port=args.port, open_browser=args.open)
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    """report 父命令：无子命令时打印帮助。"""
    if not getattr(args, "report_command", None):
        print("用法: cosmic_kb report {map|overview} [--db ...] [--json]", file=sys.stderr)
        return 2
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    """段二接入：起 MCP 服务器，把取证命令暴露成 MCP 工具供 LLM 宿主调用（stdio 传输）。"""
    from ..graph import store
    from ..mcp import server as mcp_server

    # 工具内部按 COSMIC_KB_DB 开库；args.db 已由 _resolve_db 解析为具体路径，直接注入。
    os.environ["COSMIC_KB_DB"] = args.db
    if not store.kb_exists(args.db):
        print(
            f"错误: KB 不存在或版本不符: {args.db}\n"
            f"  请先运行  cosmic_kb build <源码根> <dym|zip|目录>",
            file=sys.stderr,
        )
        return 2
    print(f"启动 cosmic_kb MCP 服务器（stdio）；KB={args.db} …", file=sys.stderr)
    return mcp_server.serve()


def _cmd_db_meta(args: argparse.Namespace) -> int:
    """从苍穹底层库（只读）按 fnumber 取 form+entity 元数据，合成 MetaModel。

    动机见 docs/扩展元数据识别方案.md：拿回原厂标准单据的完整字段，补齐扩展单据半盲。
    """
    from ..dbmeta import DbMetaReader, load_config, sample_config_text
    from ..dbmeta.config import find_config_file, DEFAULT_CONFIG_NAMES

    # --init-config：生成配置模板后退出（不连库）。
    if getattr(args, "init_config", False):
        target = Path(args.config) if args.config else Path(DEFAULT_CONFIG_NAMES[0])
        if target.exists():
            print(f"配置文件已存在，未覆盖: {target}", file=sys.stderr)
            return 2
        target.write_text(sample_config_text(), encoding="utf-8")
        print(f"已生成配置模板: {target}（填好后口令建议用环境变量 COSMIC_DB_PASSWORD）")
        return 0

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2

    try:
        with DbMetaReader(cfg) as reader:
            if getattr(args, "check", False):
                info = reader.ping()
                if args.json:
                    print(json.dumps(info, ensure_ascii=False, indent=2))
                else:
                    print(f"连接成功（只读）: {cfg.driver}@{cfg.host}:{cfg.port}/{cfg.read_database}")
                    for table, st in info["tables"].items():
                        if st["ok"]:
                            print(f"  {table:<22} 可读，共 {st['count']} 行")
                        else:
                            print(f"  {table:<22} 读取失败: {st['error']}")
                return 0

            if not args.fnumber:
                print("错误: 需提供元数据标识 fnumber（或用 --check 仅测连接）", file=sys.stderr)
                return 2
            model = reader.read_model(args.fnumber)
    except Exception as e:
        print(f"错误: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(model.to_dict(), ensure_ascii=False, indent=2))
        return 0

    db_fields = [f for f in model.fields if f.db_column]
    print(f"# {model.key}  {model.name or ''}  [{model.form_type}]  来源: 底层库 {cfg.read_database}")
    print(f"实体 {len(model.entities)} · 字段 {len(model.fields)}（落库 {len(db_fields)}）"
          f" · 操作 {len(model.operations)} · 插件 {len(model.plugins)}")
    proj = [p.class_name for p in model.plugins if p.source == "project"]
    if proj:
        print(f"项目插件 {len(proj)} 个: " + "、".join(proj[:8]) + (" …" if len(proj) > 8 else ""))
    if model.warnings:
        print(f"警告 {len(model.warnings)} 条（见 --json）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cosmic_kb",
        description="苍穹历史项目本地理解工具（段一：确定性扫描器）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"cosmic_kb {__version__}",
    )
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="检查 skill_assets 资产接线是否就位")
    doctor.set_defaults(func=_cmd_doctor)

    ingest = sub.add_parser(
        "ingest",
        help="阶段1：摄取项目源码，产出解析覆盖率/可信度报告",
    )
    ingest.add_argument("path", help="苍穹项目源码根目录")
    ingest.add_argument(
        "--json", action="store_true", help="输出机器可读 JSON 而非文本报告"
    )
    ingest.add_argument(
        "--follow-symlinks", action="store_true", help="跟随符号链接（默认不跟随）"
    )
    ingest.add_argument(
        "--max-error-files", type=int, default=30,
        help="文本报告中每类问题最多列出的文件数（默认 30）",
    )
    ingest.set_defaults(func=_cmd_ingest)

    meta = sub.add_parser(
        "meta",
        help="阶段2：解析 dym / 整包 zip / 多包 / 目录元数据，产出 JSON 快照/分类计数报告",
    )
    meta.add_argument(
        "paths", nargs="+",
        help="一个或多个：.dym/.cr 文件、整包 .zip、或含多个 zip 的目录（多包跨模块汇总）",
    )
    meta.add_argument(
        "--json", action="store_true", help="输出 MetaModel/整包 JSON 快照而非文本报告"
    )
    meta.add_argument(
        "--template-dir",
        help="继承根模板目录（含 bos_billtpl/bos_basetpl）；默认 samples/bos_temp",
    )
    meta.add_argument(
        "--limit", type=int, default=None,
        help="整包模式：仅解析前 N 个 dym（抽样/调试）",
    )
    meta.add_argument(
        "--max-list", type=int, default=50,
        help="整包文本报告里最多列出的表单数（默认 50；全部见 --json）",
    )
    meta.set_defaults(func=_cmd_meta)

    bridge = sub.add_parser(
        "bridge",
        help="阶段3：元数据 ClassName ↔ 源码 .java 桥接，产出桥接可信度报告",
    )
    bridge.add_argument("source_root", help="苍穹项目源码根目录")
    bridge.add_argument(
        "meta", nargs="+",
        help="一个或多个元数据输入：.dym 文件、整包 .zip、或含 zip 的目录",
    )
    bridge.add_argument(
        "--json", action="store_true", help="输出机器可读 JSON 而非文本报告"
    )
    bridge.add_argument(
        "--template-dir",
        help="继承根模板目录（含 bos_billtpl/bos_basetpl）；默认 samples/bos_temp",
    )
    bridge.add_argument(
        "--follow-symlinks", action="store_true", help="扫源码时跟随符号链接（默认不跟随）"
    )
    bridge.add_argument(
        "--max-list", type=int, default=30,
        help="文本报告中每类清单最多列出条数（默认 30；全部见 --json）",
    )
    bridge.set_defaults(func=_cmd_bridge)

    # ── 阶段4：build（建 KB）+ report（map / overview）──────────────────────
    build = sub.add_parser(
        "build",
        help="阶段4：建/重建 Cosmic KB（SQLite+FTS5）—— 项目地图/理解报告的数据底座",
    )
    build.add_argument("source_root", help="苍穹项目源码根目录")
    build.add_argument(
        "meta", nargs="+",
        help="一个或多个元数据输入：.dym 文件、整包 .zip、或含 zip 的目录",
    )
    build.add_argument(
        "--db", default=None,
        help=f"KB 文件路径（默认随源码根落盘 <源码根>/{DEFAULT_DB}）",
    )
    build.add_argument(
        "--template-dir",
        help="继承根模板目录（含 bos_billtpl/bos_basetpl）；默认 samples/bos_temp",
    )
    build.add_argument(
        "--follow-symlinks", action="store_true", help="扫源码时跟随符号链接（默认不跟随）"
    )
    build.set_defaults(func=_cmd_build, creating=True)

    report = sub.add_parser(
        "report", help="阶段4：项目地图 / 接手者理解报告（读 KB）",
    )
    report.set_defaults(func=_cmd_report)
    rsub = report.add_subparsers(dest="report_command")

    def _add_report_common(p: argparse.ArgumentParser) -> None:
        """report 子命令共用参数：--db 读 KB；可选 --source-root/--meta 用于 KB 缺失时临时重建。"""
        p.add_argument(
            "--db", default=None,
            help=f"KB 文件路径（默认从当前目录向上就近发现 {DEFAULT_DB}）",
        )
        p.add_argument("--json", action="store_true", help="输出机器可读 JSON 而非文本报告")
        p.add_argument(
            "--max-list", type=int, default=20,
            help="文本报告中每类清单最多列出条数（默认 20；全部见 --json）",
        )
        p.add_argument("--source-root", help="KB 缺失时临时重建用：项目源码根目录")
        p.add_argument(
            "--meta", nargs="+",
            help="KB 缺失时临时重建用：.dym / 整包 .zip / 含 zip 的目录",
        )
        p.add_argument("--template-dir", help="临时重建用：继承根模板目录（默认 samples/bos_temp）")
        p.add_argument(
            "--follow-symlinks", action="store_true", help="临时重建用：扫源码时跟随符号链接"
        )

    rmap = rsub.add_parser("map", help="项目地图：多信号模块识别 + 包结构健康度")
    _add_report_common(rmap)
    rmap.set_defaults(func=_cmd_report_map)

    rov = rsub.add_parser("overview", help="排障概览：字段级定位入口/规模/风险热点")
    _add_report_common(rov)
    rov.set_defaults(func=_cmd_report_overview)

    # ── 阶段5+6+7 旗舰：字段排障追踪 + 单据钻取（读 KB）─────────────────────
    trace = sub.add_parser(
        "trace", help="旗舰：输入字段标识→哪些插件的哪个事件函数改了它、是否落库",
    )
    trace.add_argument(
        "field",
        help="字段标识；支持点号精确定位 单据.分录.字段 / 单据.字段 / 分录.字段，如 "
             "cqkd_assetcard.cqkd_entry.cqkd_amount")
    trace.add_argument("--form", help="限定某单据（同字段跨单据时缩小范围）")
    trace.add_argument("--entry", help="限定某分录/子分录标识（同字段跨层级时精确定位）")
    trace.add_argument("--level", choices=["header", "entry", "subentry", "basedata"],
                       help="限定层级：表头/分录/子分录/基础资料")
    _add_report_common(trace)
    trace.set_defaults(func=_cmd_trace)

    resolve = sub.add_parser(
        "resolve",
        help="字段名核对：标识→真实元数据中文名+坐标（比 trace 便宜，防命名惯例臆断，钉不出回 null）",
    )
    resolve.add_argument(
        "keys", nargs="+",
        help="一个或多个字段/分录容器标识，如 cqkd_zjjnqk cqkd_zdfl（可批量核对）")
    _add_report_common(resolve)
    resolve.set_defaults(func=_cmd_resolve)

    coverage = sub.add_parser(
        "coverage", help="信任优先：手段一字段覆盖率（元数据为分母）+ 扫描质量分解",
    )
    _add_report_common(coverage)
    coverage.set_defaults(func=_cmd_coverage)

    scan_compare = sub.add_parser(
        "scan-compare", help="信任优先：手段二 粗精度扫描 vs 高精度扫描对比（疑似盲点/精度增量）",
    )
    _add_report_common(scan_compare)
    scan_compare.set_defaults(func=_cmd_scan_compare)

    dynwrites = sub.add_parser(
        "dynwrites",
        help="信任优先：全局动态/未定位写入审计（字段 key 钉不出→trace 按字段查不到，交段二大模型读源码定性）",
    )
    dynwrites.add_argument("--form", help="限定某单据 form_key")
    dynwrites.add_argument(
        "--cause", choices=["dynamic-loop", "concat", "external-const", "unknown",
                            "ambiguous", "dynamic"],
        help="限定成因桶")
    dynwrites.add_argument("--cls", help="限定某类全限定名（入口插件类 或 实际所在类）")
    _add_report_common(dynwrites)
    dynwrites.set_defaults(func=_cmd_dynwrites)

    bill = sub.add_parser(
        "bill", help="单据钻取：操作集/插件/字段触达/桥接风险",
    )
    bill.add_argument("bill", help="单据标识，如 cqkd_assetcard")
    _add_report_common(bill)
    bill.set_defaults(func=_cmd_bill)

    # ── 方法出向调用导航：该方法调了项目内哪些方法、各在哪个文件（供大模型接着读源码下钻）──
    calls = sub.add_parser(
        "calls", help="调用导航：类全限定名+方法名→该方法调用的项目内方法(目标类/文件/行)，供继续读源码下钻",
    )
    calls.add_argument("class_fqn", help="类全限定名（也可只给末段类名，歧义会列候选）")
    calls.add_argument("method", help="方法名（重载多个会全部列出）")
    _add_report_common(calls)
    calls.set_defaults(func=_cmd_calls)

    # ── 模式 A：读源码 + 自动标注字段中文名（让大模型读源码走本工具，原生 reader 易乱码且不标注）──
    source = sub.add_parser(
        "source", help="读项目源码（野生编码正确解码）+ 自动标注其中字段 key 的真实中文名",
    )
    source.add_argument("relpath", help="相对源码根的源文件路径，如 cqspb/am/AmDeepOp.java")
    source.add_argument("--lines", help="只读区间 A-B（1 基含端点，如 30-60）")
    _add_report_common(source)
    source.set_defaults(func=_cmd_source)

    # ── 阶段9：自然语言提问 → 确定性证据包（查 KB 取证，不调 LLM）─────────────
    ask = sub.add_parser(
        "ask", help="阶段9：自然语言提问→意图解析→查 KB 取证（字段谁改的/单据钻取/插件解释）",
    )
    ask.add_argument(
        "question",
        help="一句话提问，如 「资产卡片抵押状态是谁改的？」「cqkd_assetcard 这张单有哪些插件？」"
             "「CollateralService 这个类干嘛的？」；也可直接给标识或点号坐标")
    _add_report_common(ask)
    ask.set_defaults(func=_cmd_ask)

    # ── 阶段4.5：web（本地 localhost 展示层，读 KB）─────────────────────────
    web = sub.add_parser(
        "web", help="阶段4.5：本地 Web 展示项目地图/理解报告（仅本机 localhost，读 KB）",
    )
    web.add_argument(
        "--db", default=None,
        help=f"KB 文件路径（默认从当前目录向上就近发现 {DEFAULT_DB}）",
    )
    web.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1，仅本机可达）")
    web.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    web.add_argument("--open", action="store_true", help="启动后自动用默认浏览器打开")
    # KB 缺失时临时重建用（与 report 子命令同款）。
    web.add_argument("--source-root", help="KB 缺失时临时重建用：项目源码根目录")
    web.add_argument(
        "--meta", nargs="+", help="KB 缺失时临时重建用：.dym / 整包 .zip / 含 zip 的目录",
    )
    web.add_argument("--template-dir", help="临时重建用：继承根模板目录（默认 samples/bos_temp）")
    web.add_argument(
        "--follow-symlinks", action="store_true", help="临时重建用：扫源码时跟随符号链接"
    )
    web.set_defaults(func=_cmd_web)

    # ── 段二接入：MCP 服务器（把取证命令暴露成 MCP 工具供 LLM 宿主调用）─────────
    mcp = sub.add_parser(
        "mcp", help="段二接入：起 MCP 服务器，让 LLM 宿主挂 Skill 后调 ask/trace/bill 等取证工具",
    )
    mcp.add_argument(
        "--db", default=None,
        help=f"KB 文件路径（默认从当前目录向上就近发现 {DEFAULT_DB}）",
    )
    mcp.set_defaults(func=_cmd_mcp)

    # ── 底层库元数据源（只读）：直连平台库取原厂/扩展元数据，补齐扩展单据半盲 ─────
    db_meta = sub.add_parser(
        "db-meta",
        help="从苍穹底层库（只读）按标识取 form+entity 元数据合成 MetaModel（--check 测连接）",
    )
    db_meta.add_argument("fnumber", nargs="?", default=None, help="元数据标识（如 bd_customer）")
    db_meta.add_argument("--config", default=None, help="配置文件路径（默认就近找 cosmic_db.json）")
    db_meta.add_argument("--check", action="store_true", help="仅测只读连接 + 两表可读性，不取具体元数据")
    db_meta.add_argument("--init-config", dest="init_config", action="store_true", help="生成配置模板后退出")
    db_meta.add_argument("--json", action="store_true", help="输出 MetaModel 完整 JSON 快照")
    db_meta.set_defaults(func=_cmd_db_meta)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    # 多项目支持：把 --db 缺省解析为「随源码根/就近发现」的具体路径（无 db 属性的命令跳过）。
    if "db" in vars(args):
        args.db = _resolve_db(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
