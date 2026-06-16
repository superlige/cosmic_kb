"""cosmic_kb 命令行入口。

已实现命令：
    cosmic_kb --version       # 版本输出
    cosmic_kb doctor          # 检查 skill_assets 资产接线
    cosmic_kb ingest <路径>   # 阶段1：摄取源码 + 解析覆盖率/可信度报告
    cosmic_kb meta <dym|zip|dir...>  # 阶段2：解析 dym / 整包 / 多包 / 目录元数据
    cosmic_kb bridge <源码根> <dym|zip|dir...>  # 阶段3：元数据 ClassName ↔ 源码桥接

后续阶段在此挂载更多子命令（report / ask ...）。
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
    print(f"# project root: {_assets.PROJECT_ROOT}")
    print("")

    statuses = _assets.check_assets()
    for status in statuses:
        print(f"{status.label:<8}{status.name:<18}{status.path}")

    missing = [s for s in statuses if not s.present]
    print("")
    if missing:
        print(f"缺失资产 {len(missing)} 项。")
        if any(s.name == "ok-cosmic-docs.db" for s in missing):
            print("提示：把 ok-cosmic-docs.db 放到 skill_assets/ 下以启用 SDK 文档查询。")
        return 1
    print("所有关键资产就位。")
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
        elif raw.lower().endswith(".dym"):
            dyms.append(Path(raw))
        else:
            print(f"错误: 不支持的输入(需 .dym / .zip / 含 zip 的目录): {raw}", file=sys.stderr)
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
        elif raw.lower().endswith(".dym"):
            dyms.append(Path(raw))
        else:
            print(f"错误: 不支持的输入(需 .dym / .zip / 含 zip 的目录): {raw}", file=sys.stderr)
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


# 仍未实现的占位子命令，列出来让骨架自描述、也避免 AI 误调不存在的命令。
_PLANNED = [
    ("report", "阶段4  项目地图 / 接手者理解报告"),
    ("java", "阶段5-7  Java 行为 / 字段路径 / 入库判断"),
    ("ask", "阶段9  自然语言问答（查 KB 取证）"),
]


def _cmd_plan(_args: argparse.Namespace) -> int:
    print("规划中的子命令（随阶段填充，详见 docs/开发计划.md）：")
    for name, desc in _PLANNED:
        print(f"  {name:<10}{desc}")
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

    plan = sub.add_parser("plan", help="列出规划中的子命令与对应开发阶段")
    plan.set_defaults(func=_cmd_plan)

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
        help="一个或多个：.dym 文件、整包 .zip、或含多个 zip 的目录（多包跨模块汇总）",
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
