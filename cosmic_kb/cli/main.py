"""cosmic_kb 命令行入口。

已实现命令：
    cosmic_kb --version       # 版本输出
    cosmic_kb doctor          # 检查 skill_assets 资产接线
    cosmic_kb ingest <路径>   # 阶段1：摄取源码 + 解析覆盖率/可信度报告

后续阶段在此挂载更多子命令（meta / bridge / report / ask ...）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

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


# 仍未实现的占位子命令，列出来让骨架自描述、也避免 AI 误调不存在的命令。
_PLANNED = [
    ("meta", "阶段2  解析 dym / 整包 zip 元数据"),
    ("bridge", "阶段3  元数据 ClassName ↔ 源码文件桥接"),
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
