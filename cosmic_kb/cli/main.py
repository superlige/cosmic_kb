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


def _apply_vendor_metadata_cli(args: argparse.Namespace, models: list, scan_result):
    """build/bridge 共用：给了 --db-config 就自动按三信号（扩展/ORM查询/操作执行）发现
    并拉取原厂元数据；`--vendor` 仍可手动追加（并集，去重）。

    两者都不给 → 原样返回 models（零改动，纯 opt-in）；只给 --vendor 不给 --db-config
    → 报错（拉取需要连接配置）。出错时返回非零 rc（int），调用方按
    `isinstance(结果, int)` 判断是否要直接返回。
    """
    vendor_fnumbers = list(getattr(args, "vendor", None) or [])
    db_config_path = getattr(args, "db_config", None)
    if not vendor_fnumbers and not db_config_path:
        return models
    if not db_config_path:
        print("错误: 给了 --vendor 需配合 --db-config 才能连库拉取", file=sys.stderr)
        return 2

    from ..bridge import namespace
    from ..dbmeta import apply_vendor_metadata, discover_candidates
    from ..dbmeta.config import load_config
    from ..dbmeta.discover import known_keys_from_models

    known_keys = known_keys_from_models(models)
    isv_prefixes = set(namespace.discover_meta_prefixes(models))
    candidates = discover_candidates(
        models=models, scan_result=scan_result,
        known_keys=known_keys, isv_prefixes=isv_prefixes,
    )
    manual_set = set(vendor_fnumbers)
    auto_candidates = [c for c in candidates if c.key not in known_keys and c.key not in manual_set]
    fnumbers = vendor_fnumbers + [c.key for c in auto_candidates]
    if not fnumbers:
        return models

    try:
        db_cfg = load_config(db_config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    try:
        models, notices = apply_vendor_metadata(models, fnumbers, db_cfg)
    except Exception as exc:
        print(f"错误: 原厂元数据合并失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if auto_candidates:
        print(f"自动摄取（{len(auto_candidates)} 个，三信号发现）:", file=sys.stderr)
        for c in auto_candidates:
            ev = f"  证据: {c.evidence[0]}" if c.evidence else ""
            print(f"  {c.key}（信号={'+'.join(c.sources) or '?'}）{ev}", file=sys.stderr)
    if vendor_fnumbers:
        print(f"手动指定: {', '.join(vendor_fnumbers)}", file=sys.stderr)
    for note in notices:
        print(f"提示: {note}", file=sys.stderr)
    return models


def _sync_own_isv_metadata_cli(args: argparse.Namespace, models: list):
    """build 专用：`--db-config` 给了就自动按本项目二开 ISV 同步 form/entity/转换规则
    的当前完整内容，整条替换进 `models`（同 key 覆盖，新 key 追加）。

    每次都全量同步（不再区分"增量/全量"，`dbmeta/sync.py` 模块docstring 有完整原因）——
    `build_kb` 幂等重建只用这一轮 `models` 建库，只抓变更子集会让未变更的自家实体这一轮
    缺席，进而被 vendor 兜底机制误判成"原厂只读引用"或直接从 KB 里消失。

    没给 `--db-config` 时不触发（返回 `(models, None, None)`，纯 opt-in，同
    `_apply_vendor_metadata_cli` 的约定）。出错时返回非零 rc（int），调用方按
    `isinstance(结果, int)` 判断是否要直接返回——这里返回值是三元组或 int，
    调用方用 `isinstance(outcome, int)` 分支。
    """
    db_config_path = getattr(args, "db_config", None)
    if not db_config_path:
        return models, None, None

    from ..bridge import namespace
    from ..dbmeta import sync as sync_mod
    from ..dbmeta.config import load_config

    try:
        db_cfg = load_config(db_config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    local_prefixes = set(namespace.discover_meta_prefixes(models))

    try:
        result = sync_mod.sync_own_isv_metadata(
            models, db_cfg,
            isv=getattr(args, "isv", None),
            local_prefixes=local_prefixes,
        )
    except sync_mod.IsvAmbiguousError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"错误: 二开元数据同步失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    for note in result.notices:
        print(f"提示: {note}", file=sys.stderr)
    return result.models, result.isv, result.sync_ts


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

    rc = _apply_vendor_metadata_cli(args, models, scan_result)
    if isinstance(rc, int):
        return rc
    models = rc

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
    # 空 models 的检查挪到下面——纯 DB 冷启动建库（--db-config 给了、meta 为空）
    # 允许起步时 models 为空，交给下面的同步步骤去填。

    sync_isv = sync_ts = None
    if getattr(args, "db_config", None):
        outcome = _sync_own_isv_metadata_cli(args, models)
        if isinstance(outcome, int):
            return None, outcome
        models, sync_isv, sync_ts = outcome

    vendor_fnumbers = getattr(args, "vendor", None)
    result = _apply_vendor_metadata_cli(args, models, scan_result)
    if isinstance(result, int):
        return None, result
    models = result

    if not models:
        print(
            "错误: 未解析出任何元数据表单（检查 dym/zip 输入，或 --db-config 未同步到任何记录）",
            file=sys.stderr,
        )
        return None, 2

    index = namespace.build_index(scan_result)
    bridge = linker.link(scan_result, models, index=index)
    mm = project_map.module_map(scan_result, models, bridge, index=index)
    source_args = {"source_root": str(args.source_root), "meta": list(args.meta)}
    if vendor_fnumbers:
        source_args["vendor_fnumbers"] = list(vendor_fnumbers)
    if sync_ts:
        source_args["dbmeta_last_sync_ts"] = sync_ts
        source_args["dbmeta_isv"] = sync_isv
    counts = store.build_kb(
        scan_result, models, bridge, mm, db_path, index=index,
        source_args=source_args,
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
    kind = args.kind
    if kind and len(kind) > 1:
        kind = [None if k == "none" else k for k in kind]  # 逐位对应 keys，"none" 占位表示该位不限定
    elif kind:
        kind = kind[0]
    else:
        kind = None
    try:
        d = resolve_fields.resolve_fields(conn, args.keys, kind=kind)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(resolve_fields.render_resolve_fields(d, max_list=args.max_list))
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
        # 跨单据歧义需反问时返回非零退出码，方便脚本/Skill 判断"还要指定单据再查"。
        return 3 if ft.get("status") == "need_clarification" else 0
    finally:
        conn.close()


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


def _cmd_db_meta_discover(args: argparse.Namespace) -> int:
    """三类确定性信号预览（不连 DB、不摄取）：扩展母体 / ORM 查询 / 操作执行，分列 + 证据行号。

    `build`/`bridge` 给了 `--db-config` 会自动跑同一套发现并直接摄取；本命令是它的干跑版，
    供人工先看一眼会摄取哪些 key、依据是什么（红线 #4：证据先摆出来）。
    """
    from ..bridge import namespace
    from ..dbmeta import discover_candidates
    from ..dbmeta.discover import isv_prefixes_from_db, known_keys_from_db, known_keys_from_models
    from ..ingest import scanner
    from ..metadata.template_loader import TemplateRegistry

    try:
        scan_result = scanner.scan(args.discover, follow_symlinks=args.follow_symlinks)
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2

    db_path = args.db if args.db and Path(args.db).is_file() else None

    # 已知 key 全集 + ISV 前缀：--meta 给了才有信号①（ext 检测需 inherit_path，KB 里没存）；
    # --db 只用于补 known_keys（含 field 表）与 ISV 前缀（KB 建过一次就是权威来源）。
    known_keys: set[str] = set()
    isv_prefixes: set[str] = set()
    models: list = []
    if args.meta:
        registry = TemplateRegistry(args.template_dir) if args.template_dir else TemplateRegistry()
        models, rc = _collect_models(args.meta, registry)
        if rc:
            return rc
        known_keys = known_keys_from_models(models)
        isv_prefixes = set(namespace.discover_meta_prefixes(models))
    if db_path:
        known_keys |= known_keys_from_db(db_path)
        if not isv_prefixes:
            isv_prefixes = set(isv_prefixes_from_db(db_path))
    if not args.meta and not db_path:
        print("提示: 未给 --meta 也未找到可用 KB，信号①(扩展母体)不可用，"
              "信号②③(ORM查询/操作执行)未按本项目已知 key/ISV 前缀过滤，噪声会更多", file=sys.stderr)

    candidates = discover_candidates(
        models=models, scan_result=scan_result,
        known_keys=known_keys, isv_prefixes=isv_prefixes,
    )

    if args.json:
        print(json.dumps([c.to_dict() for c in candidates], ensure_ascii=False, indent=2))
        return 0

    if not candidates:
        print("未发现候选原厂 key（三类确定性信号均未命中：扩展母体/ORM 查询/操作执行）")
        return 0
    print(f"候选原厂 key（{len(candidates)} 个必摄取候选，按 ext 优先 / orm+op 命中数降序）：")
    for c in candidates[:50]:
        ext = f"  ext←{c.ext_source}" if c.ext_source else ""
        ev = f"  证据: {c.evidence[0]}" if c.evidence else ""
        print(f"  {c.key:<28} 信号={'+'.join(c.sources) or '?':<10} "
              f"orm={c.orm_hits:<3} op={c.op_hits:<3}{ext}{ev}")
    if len(candidates) > 50:
        print(f"  …其余 {len(candidates) - 50} 个见 --json")
    print("下一步: cosmic_kb build <源码根> <meta...> --db-config <配置>  会自动摄取以上全部"
          "（--vendor 可再手动追加其他 fnumber）")
    return 0


def _cmd_db_meta(args: argparse.Namespace) -> int:
    """从苍穹底层库（只读）按 fnumber 取 form+entity 元数据，合成 MetaModel。

    动机见 docs/设计方案/扩展元数据识别方案.md：拿回原厂标准单据的完整字段，补齐扩展单据半盲。
    """
    from ..dbmeta import DbMetaReader, load_config, sample_config_text
    from ..dbmeta.config import find_config_file, DEFAULT_CONFIG_NAMES

    if getattr(args, "discover", None):
        return _cmd_db_meta_discover(args)

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


def _resolve_skill_agents(args: argparse.Namespace) -> tuple[list[str], bool]:
    """Resolve the skill adapter selection and report whether auto detection was used."""
    from ..skills.installer import detect_agents, resolve_agents

    requested = args.agent or ["auto"]
    auto = requested == ["auto"]
    detected = detect_agents(project=Path(args.project), scope=args.scope) if auto else None
    return resolve_agents(requested, detected=detected), auto


def _print_skill_result(payload: dict) -> None:
    """Render human-readable install/status output; JSON is handled by the caller."""
    action = {"install": "安装", "status": "检查", "uninstall": "卸载"}[payload["command"]]
    print(f"# Cosmic KB Skills {action}结果")
    print(f"范围: {payload['scope']}  项目: {payload['project']}")
    if payload.get("dry_run"):
        print("模式: dry-run（未写入文件）")
    print("")
    for agent in payload["agents"]:
        print(f"[{agent['agent']}] {agent['status']}  {agent['root']}")
        for skill in agent["skills"]:
            detail = f"  {skill['name']}: {skill['status']} → {skill['path']}"
            if skill.get("error"):
                detail += f" ({skill['error']})"
            print(detail)
        for index, step in enumerate(agent.get("manual_steps", []), 1):
            print(f"  TRAE 手动步骤 {index}: {step}")
        print("")


def _cmd_skill_install(args: argparse.Namespace) -> int:
    from ..skills.installer import SkillResourceError, install

    try:
        agents, auto = _resolve_skill_agents(args)
        if not agents:
            message = "未自动检测到 CodeBuddy、Qoder 或 TRAE；请使用 --agent all 或显式指定宿主。"
            if args.json:
                print(json.dumps({"command": "install", "error": "no_agent_detected",
                                  "message": message}, ensure_ascii=False, indent=2))
            else:
                print(f"错误: {message}", file=sys.stderr)
            return 2
        payload, rc = install(
            agents, scope=args.scope, project=Path(args.project), dry_run=args.dry_run
        )
        payload["selection"] = "auto" if auto else "explicit"
    except (ValueError, SkillResourceError) as exc:
        if args.json:
            print(json.dumps({"command": "install", "error": type(exc).__name__,
                              "message": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"错误: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_skill_result(payload)
    return rc


def _cmd_skill_status(args: argparse.Namespace) -> int:
    from ..skills.installer import SkillResourceError, status

    try:
        agents, auto = _resolve_skill_agents(args)
        if not agents:
            message = "未自动检测到 CodeBuddy、Qoder 或 TRAE；请使用 --agent all 或显式指定宿主。"
            if args.json:
                print(json.dumps({"command": "status", "error": "no_agent_detected",
                                  "message": message}, ensure_ascii=False, indent=2))
            else:
                print(f"错误: {message}", file=sys.stderr)
            return 2
        payload, rc = status(agents, scope=args.scope, project=Path(args.project))
        payload["selection"] = "auto" if auto else "explicit"
    except (ValueError, SkillResourceError) as exc:
        if args.json:
            print(json.dumps({"command": "status", "error": type(exc).__name__,
                              "message": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"错误: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_skill_result(payload)
    return rc


def _cmd_skill_uninstall(args: argparse.Namespace) -> int:
    from ..skills.installer import uninstall

    try:
        agents, auto = _resolve_skill_agents(args)
        if not agents:
            message = "未自动检测到 CodeBuddy、Qoder 或 TRAE；请使用 --agent all 或显式指定宿主。"
            if args.json:
                print(json.dumps({"command": "uninstall", "error": "no_agent_detected",
                                  "message": message}, ensure_ascii=False, indent=2))
            else:
                print(f"错误: {message}", file=sys.stderr)
            return 2
        payload, rc = uninstall(
            agents, scope=args.scope, project=Path(args.project), dry_run=args.dry_run
        )
        payload["selection"] = "auto" if auto else "explicit"
    except ValueError as exc:
        if args.json:
            print(json.dumps({"command": "uninstall", "error": type(exc).__name__,
                              "message": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"错误: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_skill_result(payload)
    return rc


def _print_bootstrap_plan(payload: dict) -> None:
    env = payload["environment"]
    print("# Cosmic KB Bootstrap · plan（只读探测）")
    print(f"项目: {payload['project']}")
    print(f"运行时: Python {env['python_version']} @ {env['python']}  · cosmic-kb {env['package_version']}")
    print(f"命中宿主: {', '.join(env['detected_agents']) or '（无）'}  → 选定: {', '.join(env['selected_agents']) or '（无）'}")
    kb = payload["kb"]
    print(f"KB: {kb['path']}  {'已存在' if kb['exists'] else '待建'}")
    cand = payload["candidates"]
    print(f"源码根候选: {', '.join(cand['source_roots']) or '（未探到）'}")
    print(f"元数据: db_config={cand['db_config'] or '无'}  文件={len(cand['metadata_files'])} 个")
    if payload["questions"]:
        print("\n需确认（apply 前回答）:")
        for q in payload["questions"]:
            print(f"  ? [{q['id']}] {q['ask']}")
    print("\n将执行:")
    for i, act in enumerate(payload["planned_actions"], 1):
        print(f"  {i}. {act}")
    if payload["manual_actions"]:
        print("\n人工须知:")
        for act in payload["manual_actions"]:
            print(f"  · {act}")


def _print_bootstrap_apply(payload: dict) -> None:
    print("# Cosmic KB Bootstrap · apply")
    print(f"项目: {payload['project']}  · KB: {payload['kb']}  · 元数据: {payload['metadata_mode']}")
    if payload.get("dry_run"):
        print("模式: dry-run（未写入）")
    print("")
    for step in payload["steps"]:
        line = f"  [{step['step']}] {step['status']}"
        for key in ("kb", "path", "reason", "rc"):
            if key in step:
                line += f"  {key}={step[key]}"
        print(line)
    summary = payload["summary"]
    print("")
    print("✅ 全部完成" if summary["ok"] else f"⚠ 未完成步骤: {', '.join(summary['failed_steps'])}")
    for line in payload["reconnect"]:
        print(f"  重连: {line}")


def _cmd_bootstrap_plan(args: argparse.Namespace) -> int:
    from ..bootstrap import orchestrator

    payload = orchestrator.plan(
        args.project, source_root=args.source_root, meta=args.meta,
        db_config=args.db_config, agents=args.agent,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_bootstrap_plan(payload)
    return 0


def _cmd_bootstrap_apply(args: argparse.Namespace) -> int:
    from ..bootstrap import orchestrator

    payload, rc = orchestrator.apply(
        args.project, source_root=args.source_root, meta=args.meta,
        db_config=args.db_config, isv=args.isv, vendor=args.vendor,
        template_dir=args.template_dir, follow_symlinks=args.follow_symlinks,
        agents=args.agent, force_mcp=args.force_mcp,
        prompt_db_password=args.prompt_db_password, run_coverage=args.coverage,
        rebuild=args.rebuild, dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif "error" in payload:
        print(f"错误: {payload.get('message', payload['error'])}", file=sys.stderr)
    else:
        _print_bootstrap_apply(payload)
    return rc


def _cmd_bootstrap_status(args: argparse.Namespace) -> int:
    from ..bootstrap import orchestrator

    payload = orchestrator.status(
        args.project, source_root=args.source_root, db=args.kb_db, agents=args.agent,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("# Cosmic KB Bootstrap · status")
        print(f"项目: {payload['project']}  · KB: {payload['kb']['path']} {'✓' if payload['kb']['exists'] else '✗'}")
        m = payload["manifest"]
        print(f"install.json: {'✓ ' + str(m['version']) if m['exists'] else '✗ 缺失（需用安装口令启动 Bootstrap）'}")
        print(f"Skills: {payload['skills'] or '（无命中宿主）'}")
        print(f"MCP 已注册: {'✓' if payload['mcp'].get('registered') else '✗'}")
        print(f"下一步: {payload['next_step'] or '（全部就绪）'}")
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

    # ── Agent Skill 分发：CodeBuddy / Qoder 直接安装，TRAE 生成官方导入包 ─────
    skill = sub.add_parser("skill", help="安装、检查或卸载 CodeBuddy、Qoder、TRAE 的 Cosmic KB Skills")
    skill.set_defaults(func=lambda _args: (skill.print_help() or 0))
    skill_sub = skill.add_subparsers(dest="skill_command")

    def _add_skill_common(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--agent", nargs="+", choices=["auto", "all", "codebuddy", "qoder", "trae"],
            default=["auto"],
            help="目标宿主，可多选；默认 auto 自动检测，all 表示全部",
        )
        command_parser.add_argument(
            "--scope", choices=["user", "project"], default="user",
            help="操作范围（默认 user；TRAE 始终使用用户级导入包）",
        )
        command_parser.add_argument(
            "--project", default=str(Path.cwd()),
            help="project 范围的项目根目录（默认当前目录）",
        )
        command_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    skill_install = skill_sub.add_parser("install", help="安装两份 Cosmic KB Skills（同名文件直接更新）")
    _add_skill_common(skill_install)
    skill_install.add_argument("--dry-run", action="store_true", help="仅显示目标，不创建或写入文件")
    skill_install.set_defaults(func=_cmd_skill_install)

    skill_status = skill_sub.add_parser("status", help="按 SHA-256 检查 Skill 是否缺失或过期")
    _add_skill_common(skill_status)
    skill_status.set_defaults(func=_cmd_skill_status)

    skill_uninstall = skill_sub.add_parser("uninstall", help="卸载两份 Cosmic KB Skills，不影响其他 Skill")
    _add_skill_common(skill_uninstall)
    skill_uninstall.add_argument("--dry-run", action="store_true", help="仅显示目标，不删除文件")
    skill_uninstall.set_defaults(func=_cmd_skill_uninstall)

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
    bridge.add_argument(
        "--vendor", nargs="+", default=None, metavar="FNUMBER",
        help="手动追加拉取的原厂 fnumber（如 bd_customer），需配合 --db-config；"
             "三类确定性信号（扩展/ORM查询/操作执行）命中的 key 给了 --db-config 就自动拉取，"
             "本项只补充自动发现漏掉的（见 cosmic_kb db-meta --discover 预览）",
    )
    bridge.add_argument(
        "--db-config", default=None,
        help="dbmeta 连接配置文件路径（默认就近找 cosmic_db.json，同 db-meta --config）；"
             "给了即自动按三信号发现并摄取代码库引用到的原厂实体",
    )
    bridge.set_defaults(func=_cmd_bridge)

    # ── 阶段4：build（建 KB）+ report（map / overview）──────────────────────
    build = sub.add_parser(
        "build",
        help="阶段4：建/重建 Cosmic KB（SQLite+FTS5）—— 项目地图/理解报告的数据底座",
    )
    build.add_argument("source_root", help="苍穹项目源码根目录")
    build.add_argument(
        "meta", nargs="*",
        help="一个或多个元数据输入：.dym 文件、整包 .zip、或含 zip 的目录；"
             "配合 --db-config 时可省略（纯 DB 二开同步建库）",
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
    build.add_argument(
        "--vendor", nargs="+", default=None, metavar="FNUMBER",
        help="手动追加拉取的原厂 fnumber（如 bd_customer），需配合 --db-config；"
             "三类确定性信号（扩展/ORM查询/操作执行）命中的 key 给了 --db-config 就自动拉取，"
             "本项只补充自动发现漏掉的（见 cosmic_kb db-meta --discover 预览）",
    )
    build.add_argument(
        "--db-config", default=None,
        help="dbmeta 连接配置文件路径（默认就近找 cosmic_db.json，同 db-meta --config）；"
             "给了即自动按三信号发现并摄取代码库引用到的原厂实体，"
             "同时自动全量同步本项目自己（二开）ISV 当前的 form/entity/转换规则内容",
    )
    build.add_argument(
        "--isv", default=None, metavar="ISV",
        help="显式指定本项目二开 ISV（跳过自动发现/消歧）；不给且候选唯一时自动使用，"
             "候选 >1 个且无法用本地元数据消歧时报错列出候选（各自表单数），需手动指定",
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
        help="标识核对：字段/表头实体/分录/子分录/单据(表单)/插件类名标识→真实元数据中文名+坐标"
             "（比 trace 便宜，防命名惯例臆断，钉不出回 null）",
    )
    resolve.add_argument(
        "keys", nargs="+",
        help="一个或多个字段/分录容器/单据标识，如 cqkd_zjjnqk cqkd_zdfl cqkd_invoic_apply"
             "（可批量核对；支持复合限定符精确匹配，与 trace 同一套点号坐标写法："
             "单据.字段 / 分录.字段 / 单据.分录.字段）")
    resolve.add_argument(
        "--kind", nargs="+", choices=["field", "entity", "form", "plugin", "none"],
        help="只返回某一种候选（field=字段/entity=分录容器/form=单据/plugin=插件类名反查绑定），"
             "避免字段名与单据/分录 key 同名时混入噪声；只传一个值时广播给全部 keys（要求整批同层级）。"
             "keys 分属不同层级时传与 keys 等长的多个值逐位对应（如 --kind form entity field），"
             "某位不确定填 none（该位三路全查，不广播限定）；"
             "kind=entity 时两段式「分录.字段」限定符若无单据前缀会被拒绝（invalid_request），"
             "需要写「单据.分录.字段」三段式；"
             "kind=plugin 时对应 key 按插件类名（简单名/全限定名）处理，不走点号坐标限定符协议")
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

    # ── 模式 A：读源码 + 自动标注字段中文名（让大模型读源码走本工具，原生 reader 易乱码且不标注）──
    source = sub.add_parser(
        "source", help="读项目源码（野生编码正确解码）+ 自动标注其中字段 key 的真实中文名",
    )
    source.add_argument("relpath", help="相对源码根的源文件路径，如 cqspb/am/AmDeepOp.java")
    source.add_argument("--lines", help="只读区间 A-B（1 基含端点，如 30-60）")
    _add_report_common(source)
    source.set_defaults(func=_cmd_source)

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
        "mcp", help="段二接入：起 MCP 服务器，让 LLM 宿主挂 Skill 后调 trace/bill 等取证工具",
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
    db_meta.add_argument(
        "--discover", metavar="SOURCE_ROOT", default=None,
        help="候选原厂 key 发现（干跑，不连 DB、不摄取）：扫该源码根，按三类确定性信号"
             "（扩展母体/ORM查询/操作执行）列出必摄取候选 + 证据行号；"
             "build/bridge 给 --db-config 会自动跑同一套发现并直接摄取",
    )
    db_meta.add_argument(
        "--db", default=None,
        help="--discover 用：已建好的 KB 路径（推荐必给）——已知 key（含 field 表）/ISV 前缀"
             "直接从 KB 现算，通常不必再另给 --meta",
    )
    db_meta.add_argument(
        "--meta", nargs="+", default=None,
        help="--discover 用：本地 .dym/整包 .zip/含 zip 的目录——信号①(扩展母体)必须靠它"
             "（ext 检测需 InheritPath，KB 里没存），也可在 KB 之外补充已知 key/ISV 前缀",
    )
    db_meta.add_argument(
        "--template-dir", default=None,
        help="--discover --meta 用：继承根模板目录（含 bos_billtpl/bos_basetpl）；默认 samples/bos_temp",
    )
    db_meta.add_argument(
        "--follow-symlinks", action="store_true", help="--discover 用：扫源码时跟随符号链接"
    )
    db_meta.set_defaults(func=_cmd_db_meta)

    # ── 对话式安装 Bootstrap 编排器（plan/apply/status）──────────────────────
    bootstrap = sub.add_parser(
        "bootstrap",
        help="对话式安装编排：plan 只读探测 → apply 建库+装Skill+注册MCP+四工具校验 → status 查进度",
    )
    bootstrap.set_defaults(func=lambda _args: (bootstrap.print_help() or 0))
    bsub = bootstrap.add_subparsers(dest="bootstrap_command")

    def _add_bootstrap_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--agent", nargs="+", choices=["auto", "all", "codebuddy", "qoder", "trae"],
            default=None, help="目标宿主，可多选；默认 auto 自动检测",
        )
        p.add_argument("--project", default=str(Path.cwd()), help="项目根（默认当前目录）")
        p.add_argument("--json", action="store_true", help="输出机器可读 JSON（agent 首选）")

    bs_plan = bsub.add_parser("plan", help="只读探测：环境/候选/已有产物/冲突 + questions/actions")
    _add_bootstrap_common(bs_plan)
    bs_plan.add_argument("--source-root", dest="source_root", default=None, help="Java 源码根")
    bs_plan.add_argument("--meta", nargs="+", default=None, help="dym/cr/整包zip 或含它们的目录")
    bs_plan.add_argument("--db-config", dest="db_config", default=None, help="dbmeta 连接配置（cosmic_db.json）")
    bs_plan.set_defaults(func=_cmd_bootstrap_plan)

    bs_apply = bsub.add_parser("apply", help="按序建库+装Skill+注册MCP+校验（每步幂等、可续跑）")
    _add_bootstrap_common(bs_apply)
    bs_apply.add_argument("source_root", help="Java 源码根")
    bs_apply.add_argument("meta", nargs="*", help="dym/cr/整包zip 或目录（配合 --db-config 可省略）")
    bs_apply.add_argument("--db-config", dest="db_config", default=None, help="dbmeta 连接配置（直连库建库）")
    bs_apply.add_argument("--isv", default=None, help="显式指定本项目二开 ISV（跳过自动消歧）")
    bs_apply.add_argument("--vendor", nargs="+", default=None, metavar="FNUMBER", help="手动追加拉取的原厂 fnumber")
    bs_apply.add_argument("--template-dir", dest="template_dir", default=None, help="继承根模板目录")
    bs_apply.add_argument("--follow-symlinks", dest="follow_symlinks", action="store_true", help="扫源码跟随符号链接")
    bs_apply.add_argument(
        "--prompt-db-password", dest="prompt_db_password", action="store_true",
        help="终端隐藏输入底层库口令（单进程用完只进环境变量，绝不写入任何文件）",
    )
    bs_apply.add_argument("--force-mcp", dest="force_mcp", action="store_true", help="同名 MCP 配置冲突时先备份再替换")
    bs_apply.add_argument("--coverage", action="store_true", help="doctor 后附带跑一次覆盖率")
    bs_apply.add_argument("--rebuild", action="store_true", help="KB 已存在也强制重建（默认续跑跳过）")
    bs_apply.add_argument("--dry-run", action="store_true", help="只演练不写入")
    bs_apply.set_defaults(func=_cmd_bootstrap_apply)

    bs_status = bsub.add_parser("status", help="读 install.json + 各步产物，报告进度与下一步")
    _add_bootstrap_common(bs_status)
    bs_status.add_argument("--source-root", dest="source_root", default=None, help="Java 源码根（定位 KB 用）")
    # dest 特意不叫 db：避免 main() 的通用 _resolve_db 从 cwd 抢先发现别的 KB，覆盖 --source-root。
    bs_status.add_argument("--db", dest="kb_db", default=None, help="KB 路径（默认按 --project/--source-root 就近发现）")
    bs_status.set_defaults(func=_cmd_bootstrap_status)

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
