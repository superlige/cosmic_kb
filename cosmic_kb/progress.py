"""build 全程进度显示（硬约束 #3「规模大」派生要求：要性能、进度）。

为什么单独一个顶层模块：进度要横跨 ingest / java / graph / cli 四个子包，放任何
一个子包里都会造出跨包反向依赖；本模块只依赖标准库，谁都能安全 import。

分层约定（守两段式解耦）：
  * 库层（scanner / java.analyze / store.build_kb）只认这里的 ``Progress`` 协议，
    默认拿 ``NULL``——stage/tick 完全静默，MCP / bootstrap / 测试路径零输出零成本；
    ``note`` 默认直印 stderr（状态提示对所有调用方都该可见，且绝不污染 stdout——
    MCP stdio 协议、``--json`` 输出都走 stdout，进度类信息一律 stderr）。
  * CLI ``build`` 注入 ``ConsoleProgress`` 才有「[k/N] 阶段名 · 子步骤: done/total (pct%)」
    的渲染：TTY 单行 ``\\r`` 原地刷新；重定向/管道时退化为限频换行打印，日志不刷屏。
"""

from __future__ import annotations

import sys
import time
import unicodedata


class Progress:
    """进度协议 + 静默默认实现（NULL 对象模式，库层不必判空）。"""

    def stage(self, name: str) -> None:
        """进入下一阶段（阶段序号由渲染器自己数，调用方只报名字）。"""

    def tick(self, done: int, total: int | None = None, unit: str = "",
             label: str | None = None) -> None:
        """阶段内进度打点。total 未知传 None（只报「已处理 N」）；
        label 为阶段内子步骤名（如「插件归因」），同一阶段可多个子步骤接力。
        done=0 且 total=None 表示「刚进入该子步骤」，只亮名字不报数。"""

    def note(self, text: str) -> None:
        """阶段内一次性状态提示（保留在滚动输出里，不被进度行覆盖）。"""
        print(text, file=sys.stderr)

    def finish(self) -> None:
        """全部阶段完成（打总耗时）。"""

    def close(self) -> None:
        """兜底收尾：若 TTY 进度行没换行，补一个换行（异常/错误路径防串行）。"""


NULL = Progress()


def _display_width(text: str) -> int:
    """终端显示列宽（不是字符数）。

    TTY 短行覆盖长行时按列宽补空格：中文等全角字符占 2 列，按 ``len()`` 补会少补、
    露出旧行尾巴（真实翻车：「✓ 2m53s   回填 … 0%))」）。宽度歧义类（East Asian
    Ambiguous，如 ·、…、✓）在中文 Windows 控制台通常也渲染 2 列，一并按 2 算——
    多补几个空格无害，少补就残尾。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 1
               for ch in text)


def _fmt_elapsed(seconds: float) -> str:
    """耗时人话化：61.0 → "1m01s"。进度行空间有限，用紧凑记法不用中文单位。"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


class ConsoleProgress(Progress):
    """控制台渲染器。

    TTY：每阶段一行，``\\r`` 原地刷新（渲染限频 0.1s，避免高频 tick 拖慢分析本身）；
    阶段切换时把上一行定格成「✓ 耗时」。非 TTY（重定向/CI）：限频 3s 换行打印，
    外加每个子步骤的完成行，既能看到活着、又不把日志刷爆。
    """

    _TTY_INTERVAL = 0.1
    _PIPE_INTERVAL = 3.0

    def __init__(self, total_stages: int, *, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._total = max(1, total_stages)
        self._idx = 0
        self._name = ""
        self._started = time.monotonic()
        self._stage_started = self._started
        try:
            self._tty = bool(self._stream.isatty())
        except Exception:
            self._tty = False
        self._last_render = 0.0
        self._last_label: str | None = None   # 上次渲染的子步骤名（切换时必渲染，防被限频吞掉）
        self._line = ""          # 当前未换行的进度行（TTY 覆盖重绘 / note 重现用）
        self._prev_cols = 0      # 上一次渲染的显示列宽（短行覆盖长行时补空格清尾巴）

    # ── 渲染原语 ────────────────────────────────────────────────
    def _write(self, text: str, *, newline: bool) -> None:
        if self._tty:
            pad = " " * max(0, self._prev_cols - _display_width(text))
            self._stream.write("\r" + text + pad + ("\n" if newline else ""))
            self._prev_cols = 0 if newline else _display_width(text)
        else:
            self._stream.write(text + "\n")
        self._stream.flush()
        self._line = "" if newline else text

    def _head(self) -> str:
        return f"[{self._idx}/{self._total}] {self._name}"

    def _seal(self) -> None:
        """把当前阶段行定格成「✓ 耗时」并换行（阶段切换 / finish 共用）。"""
        if not self._idx:
            return
        elapsed = _fmt_elapsed(time.monotonic() - self._stage_started)
        self._write(f"{self._head()} ✓ {elapsed}", newline=True)

    # ── Progress 协议 ───────────────────────────────────────────
    def stage(self, name: str) -> None:
        self._seal()
        self._idx = min(self._idx + 1, self._total)
        self._name = name
        self._stage_started = time.monotonic()
        self._last_render = 0.0
        self._last_label = None
        self._write(f"{self._head()} …", newline=not self._tty)

    def tick(self, done: int, total: int | None = None, unit: str = "",
             label: str | None = None) -> None:
        now = time.monotonic()
        final = total is not None and total > 0 and done >= total
        # 子步骤切换必渲染：上一子步骤 100% 的 final tick 刚刷新过 _last_render，
        # 新子步骤的开场 tick 若被限频吞掉，屏幕会定格在旧 100% 上直到下个窗口——
        # 长耗时子步骤（固定点传播等）看起来就像卡死（真实翻车：100% 停几十秒）。
        switched = label != self._last_label
        interval = self._TTY_INTERVAL if self._tty else self._PIPE_INTERVAL
        if not final and not switched and now - self._last_render < interval:
            return
        self._last_render = now
        self._last_label = label
        head = self._head() + (f" · {label}" if label else "")
        u = f" {unit}" if unit else ""
        if total:
            pct = min(100, done * 100 // total)
            body = f": {done}/{total}{u} ({pct}%)"
        elif done:
            body = f": 已处理 {done}{u}"
        else:
            body = " …"
        # TTY 原地刷新；非 TTY 只在限频窗口/子步骤完成时各打一行。
        self._write(head + body, newline=not self._tty)

    def note(self, text: str) -> None:
        if self._tty and self._line:
            cur = self._line
            self._write(text, newline=True)     # 覆盖当前进度行输出提示
            self._write(cur, newline=False)     # 再把进度行绘回来
        else:
            self._stream.write(text + "\n")
            self._stream.flush()

    def finish(self) -> None:
        self._seal()
        total = _fmt_elapsed(time.monotonic() - self._started)
        self._stream.write(f"进度: 全部 {self._total} 个阶段完成，总耗时 {total}\n")
        self._stream.flush()
        self._idx = 0
        self._line = ""

    def close(self) -> None:
        if self._tty and self._line:
            self._stream.write("\n")
            self._stream.flush()
            self._line = ""
            self._prev_len = 0
