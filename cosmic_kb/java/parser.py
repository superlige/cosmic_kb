"""阶段 1 · Java 解析（tree-sitter，错误恢复）。

把摄取层产出的源码文本喂给 tree-sitter-java，逐文件产出 AST 与"解析状态"。
本模块**只关心能不能解析、解析得有多干净**（阶段1 信任门槛），不抽取语义
（类型/事件/字段那是阶段 5+）。

设计约束（见 docs/核心/开发计划.md 第二节）：
    - 代码"野生"、可能不可编译 —— 依赖 tree-sitter 的错误恢复，绝不因语法错误而崩。
    - 不做类型解析、不解析依赖、不碰外部符号（kd.bos.* 等留给 SDK 目录解释）。
    - tree-sitter 是可选依赖（pyproject 的 [parse] extra）。未安装时不抛硬错，
      返回 available=False 的结果，由覆盖率报告如实呈现"未启用解析"。

"解析状态"定义：
    - ok       —— 解析出树且无 ERROR / MISSING 节点（干净）。
    - partial  —— 解析出树但含 ERROR / MISSING 片段（tree-sitter 已局部恢复）。
    - skipped  —— 没有文本可解析（摄取阶段读取失败）。
    - unavailable —— tree-sitter 未安装，无法解析。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 避免在未装 tree-sitter 时导入失败
    from tree_sitter import Node, Parser

# 错误片段最多记录这么多条，避免一个烂文件撑爆报告。
_MAX_ERROR_SPANS = 50


@dataclass(frozen=True)
class ErrorSpan:
    """一处解析错误片段的位置（行号 1-based，便于直接对照源码）。"""

    kind: str          # "error" | "missing"
    start_line: int
    end_line: int

    def __str__(self) -> str:
        if self.start_line == self.end_line:
            return f"{self.kind}@L{self.start_line}"
        return f"{self.kind}@L{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class JavaParseResult:
    """单文件解析结果。"""

    status: str                          # ok | partial | skipped | unavailable
    error_count: int = 0                 # ERROR 节点数
    missing_count: int = 0               # MISSING 节点数
    node_count: int = 0                  # 总节点数（粗粒度规模指标）
    line_count: int = 0                  # 源码行数
    error_spans: list[ErrorSpan] = field(default_factory=list)
    truncated_spans: bool = False        # error_spans 是否因超限被截断
    note: str | None = None              # 跳过/不可用的原因说明

    @property
    def ok(self) -> bool:
        """解析出干净的树。"""
        return self.status == "ok"

    @property
    def parsed(self) -> bool:
        """解析出了树（无论干净与否）—— ok 或 partial。"""
        return self.status in ("ok", "partial")


# ── tree-sitter 装载（惰性 + 缓存）────────────────────────────────
_parser_cache: "Parser | None" = None
_parser_load_error: str | None = None


def is_available() -> bool:
    """tree-sitter-java 是否可用（装了 [parse] extra）。"""
    return get_parser() is not None


def get_parser() -> "Parser | None":
    """惰性构建并缓存全局 Parser；未装依赖时返回 None（记录原因）。"""
    global _parser_cache, _parser_load_error
    if _parser_cache is not None:
        return _parser_cache
    if _parser_load_error is not None:
        return None
    try:
        import tree_sitter_java as tsj
        from tree_sitter import Language, Parser

        _parser_cache = Parser(Language(tsj.language()))
        return _parser_cache
    except Exception as exc:  # 未安装 / 版本不匹配 / 构建失败
        _parser_load_error = f"{type(exc).__name__}: {exc}"
        return None


def load_error() -> str | None:
    """返回上次装载失败的原因（供报告提示用户装 [parse] extra）。"""
    get_parser()
    return _parser_load_error


# ── 解析 ────────────────────────────────────────────────────────
def parse_source(text: str | None) -> JavaParseResult:
    """解析一段 Java 源码文本，返回解析状态与错误片段。

    text 为 None（摄取阶段读取失败）→ status=skipped。
    tree-sitter 不可用 → status=unavailable。
    """
    if text is None:
        return JavaParseResult(status="skipped", note="无文本（摄取阶段读取失败）")

    parser = get_parser()
    if parser is None:
        return JavaParseResult(
            status="unavailable",
            line_count=text.count("\n") + 1 if text else 0,
            note=f"tree-sitter 未启用（pip install -e .[parse]）；{_parser_load_error or ''}".strip(),
        )

    tree = parser.parse(text.encode("utf-8"))
    error_spans: list[ErrorSpan] = []
    error_count = 0
    missing_count = 0
    node_count = 0
    truncated = False

    # 显式栈遍历，避免深层 AST 触发 Python 递归上限。
    stack: list[Node] = [tree.root_node]
    while stack:
        node = stack.pop()
        node_count += 1
        is_err = node.is_error or node.type == "ERROR"
        is_missing = node.is_missing
        if is_err or is_missing:
            if is_err:
                error_count += 1
            if is_missing:
                missing_count += 1
            if len(error_spans) < _MAX_ERROR_SPANS:
                error_spans.append(
                    ErrorSpan(
                        kind="missing" if is_missing else "error",
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                )
            else:
                truncated = True
        # 仅在父节点标记 has_error 时才下钻找具体错误点，省去对干净子树的全量遍历。
        # 但 node_count 需精确 → 仍需遍历全部子节点。规模可接受（Java 文件 AST 有限）。
        stack.extend(node.children)

    status = "ok" if (error_count == 0 and missing_count == 0) else "partial"
    return JavaParseResult(
        status=status,
        error_count=error_count,
        missing_count=missing_count,
        node_count=node_count,
        line_count=text.count("\n") + 1 if text else 0,
        error_spans=error_spans,
        truncated_spans=truncated,
    )
