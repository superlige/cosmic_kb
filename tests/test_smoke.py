"""阶段 0 冒烟测试 —— 验证骨架可导入、版本可读、CLI 可跑、资产可定位。"""

from __future__ import annotations

import cosmic_kb
from cosmic_kb import _assets
from cosmic_kb.cli import main as cli_main


def test_version_string():
    assert isinstance(cosmic_kb.__version__, str)
    assert cosmic_kb.__version__.count(".") >= 1


def test_cli_version_flag(capsys):
    """`cosmic_kb --version` 通过 argparse version action 退出码 0 并打印版本。"""
    try:
        cli_main.main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out
    assert cosmic_kb.__version__ in out


def test_cli_no_args_prints_help():
    assert cli_main.main([]) == 0


def test_subpackages_importable():
    import importlib

    for name in (
        "ingest",
        "metadata",
            "java",
            "bridge",
            "graph",
            "semantic",
            "report",
            "cli",
        ):
        importlib.import_module(f"cosmic_kb.{name}")


def test_assets_resolve_via_resources():
    """运行期资产经 importlib.resources 定位（不依赖 parents[1] 同级目录布局）。"""
    topics = list(_assets.iter_reference_topics())
    assert topics, "references/rules 应作为随包数据可枚举"
    assert _assets.read_topic("anti-patterns"), "反模式黑名单应可读"
    assert _assets.templates_root().joinpath("bos_billtpl.dym")


def test_doctor_runs():
    """doctor 命令应可执行并返回 int 退出码（0 或 1，取决于 DB 是否就位）。"""
    rc = cli_main.main(["doctor"])
    assert rc in (0, 1)
