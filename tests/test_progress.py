"""build 进度显示（cosmic_kb/progress.py + 全管线接线）验收测试。

覆盖三层：
  * 渲染器本体：非 TTY 限频换行 / TTY 单行 \r 原地刷新 / note 不破坏进度行 / 阶段定格。
  * 阶段规划：`_plan_build_stages` 与 `_build_kb` 的 stage 调用一一对应（可选阶段按入参增删）。
  * 管线接线：scanner.scan / store.build_kb / java.analyze 收到 progress 后按预期打点，
    不注入时（NULL）保持完全静默——MCP / bootstrap 路径零输出是红线。
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

from cosmic_kb.progress import NULL, ConsoleProgress, Progress, _display_width, _fmt_elapsed


class _TtyStringIO(io.StringIO):
    """isatty()=True 的内存流，模拟交互式控制台。"""

    def isatty(self) -> bool:  # noqa: D102
        return True


class _Recorder(Progress):
    """记录式报告器：只收调用轨迹，供管线接线断言（不渲染）。"""

    def __init__(self) -> None:
        self.stages: list[str] = []
        self.labels: set[str] = set()
        self.ticks: int = 0
        self.notes: list[str] = []

    def stage(self, name: str) -> None:
        self.stages.append(name)

    def tick(self, done, total=None, unit="", label=None) -> None:
        self.ticks += 1
        if label:
            self.labels.add(label)

    def note(self, text: str) -> None:
        self.notes.append(text)


# ── 渲染器本体 ──────────────────────────────────────────────────

def test_console_progress_pipe_mode_lines():
    """非 TTY：阶段行 + 完成定格行 + 百分比 tick，全部按换行输出（日志可读）。"""
    buf = io.StringIO()
    p = ConsoleProgress(3, stream=buf)
    p.stage("源码扫描")
    p.tick(3, None, "个文件")
    p.tick(10, 10, "个文件")
    p.stage("元数据解析")
    p.finish()
    out = buf.getvalue()
    assert "[1/3] 源码扫描 …" in out
    assert "已处理 3 个文件" in out
    assert "10/10 个文件 (100%)" in out
    assert "[1/3] 源码扫描 ✓" in out          # 阶段切换时定格上一阶段
    assert "[2/3] 元数据解析 ✓" in out        # finish 定格当前阶段
    assert "总耗时" in out
    assert "\r" not in out                    # 非 TTY 绝不发控制字符


def test_console_progress_pipe_mode_throttles():
    """非 TTY：高频 tick 被限频（3s 窗口），不把日志刷爆；子步骤完成行必打。"""
    buf = io.StringIO()
    p = ConsoleProgress(1, stream=buf)
    p.stage("Java 字段级分析")
    for i in range(1, 1001):
        p.tick(i, 1000, "个文件", label="解析工程 Java")
    lines = [ln for ln in buf.getvalue().splitlines() if "解析工程 Java" in ln]
    assert len(lines) <= 3                    # 首个 tick + 完成 tick（限频窗口内其余全吞）
    assert any("1000/1000" in ln for ln in lines)


def test_console_progress_tty_inplace_and_note():
    """TTY：\r 原地刷新；note 先覆盖进度行输出提示、再把进度行绘回来。"""
    buf = _TtyStringIO()
    p = ConsoleProgress(2, stream=buf)
    p.stage("A")
    p.tick(1, 4, "个")
    p.note("中途提示")
    p.stage("B")
    p.finish()
    out = buf.getvalue()
    assert "\r[1/2] A …" in out
    assert "中途提示" in out
    # note 之后进度行被重绘（提示不吃掉进度）
    assert out.index("中途提示") < out.rindex("[1/2] A")
    assert "[1/2] A ✓" in out
    assert "[2/2] B ✓" in out


def test_console_progress_tty_wide_chars_fully_covered():
    """TTY：短行覆盖长行按**显示列宽**补空格——中文占 2 列，按 len() 补会露旧行尾巴。

    真实翻车：`[8/9] Java 字段级分析 ✓ 2m53s     回填 …    0%))`，定格行没盖住
    前一条更长的中文子步骤行。"""
    buf = _TtyStringIO()
    p = ConsoleProgress(1, stream=buf)
    p.stage("Java 字段级分析")
    p.tick(1, 3, "个方法", label="反向调用图·元数据反查回填")   # 中文长行
    p.finish()                                                  # 定格成短的「✓ 耗时」行
    segs = [s.rstrip("\n") for s in buf.getvalue().split("\r")]
    long_line = next(s for s in segs if "反查回填" in s)
    seal = next(s for s in segs if "✓" in s)
    assert _display_width(seal) >= _display_width(long_line)    # 含补白，必须完全盖住


def test_console_progress_tick_renders_on_label_switch():
    """TTY：子步骤切换必渲染，不受限频窗口影响。

    真实翻车：上一子步骤 100% 的 final tick 刚刷新限频时钟，下一长耗时子步骤的
    开场 tick 被吞，屏幕定格在旧 100% 几十秒像卡死。"""
    buf = _TtyStringIO()
    p = ConsoleProgress(1, stream=buf)
    p.stage("Java 字段级分析")
    p.tick(10, 10, "个类", label="反向调用图·建索引")    # final：必渲染并刷新限频时钟
    p.tick(0, None, label="反向调用图·固定点传播")       # 紧随其后（<0.1s），旧实现被吞
    assert "反向调用图·固定点传播" in buf.getvalue()


def test_console_progress_close_completes_open_line():
    """close：TTY 进度行没换行时补换行（异常/报错路径不串行）；已换行则无操作。"""
    buf = _TtyStringIO()
    p = ConsoleProgress(1, stream=buf)
    p.stage("A")
    assert not buf.getvalue().endswith("\n")
    p.close()
    assert buf.getvalue().endswith("\n")
    n = len(buf.getvalue())
    p.close()                                  # 幂等
    assert len(buf.getvalue()) == n


def test_null_progress_silent_except_note(capsys):
    """NULL：stage/tick 完全静默（MCP/测试路径零输出），note 走 stderr（状态提示仍可见）。"""
    NULL.stage("x")
    NULL.tick(1, 2)
    NULL.finish()
    NULL.close()
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    NULL.note("提示")
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "提示\n"


def test_fmt_elapsed():
    assert _fmt_elapsed(5.24) == "5.2s"
    assert _fmt_elapsed(75) == "1m15s"
    assert _fmt_elapsed(3700) == "1h01m"


# ── 阶段规划与 CLI 对应 ─────────────────────────────────────────

def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def test_plan_build_stages_variants():
    from cosmic_kb.cli.main import _plan_build_stages
    from cosmic_kb.graph.store import BUILD_STAGES

    base = _plan_build_stages(_args())
    assert base[0] == "源码扫描"
    assert "编译期符号解析" in base
    assert tuple(base[-3:]) == BUILD_STAGES    # 最后三段由 store.build_kb 推进，两侧共用常量

    no_sym = _plan_build_stages(_args(no_symbols=True))
    assert "编译期符号解析" not in no_sym
    assert len(no_sym) == len(base) - 1

    with_db = _plan_build_stages(_args(db_config="cosmic_db.json"))
    assert "二开元数据同步" in with_db and "原厂元数据补充" in with_db
    assert len(with_db) == len(base) + 2

    with_vendor = _plan_build_stages(_args(vendor=["bd_customer"]))
    assert "原厂元数据补充" in with_vendor and "二开元数据同步" not in with_vendor


# ── 管线接线 ────────────────────────────────────────────────────

def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _mini_project(tmp_path: Path):
    """两文件合成项目 + 单表单元数据（复用阶段4测试的最小口径）。"""
    from cosmic_kb.metadata.model import MetaEntity, MetaField, MetaModel, MetaPlugin

    _write(tmp_path / "AssetCardFormPlugin.java",
           "package cqspb.assets;\npublic class AssetCardFormPlugin {}\n")
    _write(tmp_path / "AssetCardService.java",
           "package cqspb.assets;\npublic class AssetCardService {}\n")
    models = [MetaModel(
        key="cqkd_assetcard", name="资产卡片", model_type="BillFormModel", form_type="bill",
        isv="cqkd", app_key="cqkd_assets",
        plugins=[MetaPlugin(class_name="cqspb.assets.AssetCardFormPlugin",
                            plugin_type="form", source="project")],
        entities=[MetaEntity("BillEntity", "cqkd_assetcard", "资产卡片主体",
                             "1", "header", None, "t_asset")],
        fields=[MetaField("TextField", "cqkd_name", "名称", "fname", "f1",
                          None, "entity", "header", "cqkd_assetcard")],
    )]
    return models


def test_scan_ticks_progress(tmp_path: Path):
    from cosmic_kb.ingest import scanner

    _mini_project(tmp_path)
    rec = _Recorder()
    result = scanner.scan(tmp_path, progress=rec)
    assert rec.ticks == len(result.files) > 0


def test_build_kb_stages_and_labels(tmp_path: Path):
    """store.build_kb 注入 progress：按 BUILD_STAGES 顺序推进 + 分析/粗扫子步骤有打点。"""
    from cosmic_kb.bridge import linker, namespace
    from cosmic_kb.graph import store
    from cosmic_kb.ingest import scanner
    from cosmic_kb.report import project_map

    models = _mini_project(tmp_path)
    scan = scanner.scan(tmp_path)
    index = namespace.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    mm = project_map.module_map(scan, models, bridge, index=index)

    rec = _Recorder()
    store.build_kb(scan, models, bridge, mm, tmp_path / "kb.db", index=index, progress=rec)
    assert rec.stages == list(store.BUILD_STAGES)
    # 最重阶段的关键子步骤都有名字（tree-sitter 未装时 analyze 提前返回，只有粗扫标签）
    from cosmic_kb.java.parser import is_available
    if is_available():
        assert {"解析工程 Java", "插件归因(跨类回溯)", "孤立方法补全"} <= rec.labels
    assert "粗扫交叉验证" in rec.labels


def test_build_kb_without_progress_is_silent(tmp_path: Path, capsys):
    """不注入 progress（MCP/bootstrap 路径）：build_kb 不产生任何 stdout/stderr 输出。"""
    from cosmic_kb.bridge import linker, namespace
    from cosmic_kb.graph import store
    from cosmic_kb.ingest import scanner
    from cosmic_kb.report import project_map

    models = _mini_project(tmp_path)
    scan = scanner.scan(tmp_path)
    index = namespace.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    mm = project_map.module_map(scan, models, bridge, index=index)
    capsys.readouterr()
    store.build_kb(scan, models, bridge, mm, tmp_path / "kb.db", index=index)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
