"""阶段 1 · 源码摄取（Ingestion）。

把工具指向一坨"野生"目录，递归发现源码文件并做编码探测，为后续解析提供
干净的输入清单。本模块**只负责发现与读取**，不解析语法（那是 java/parser.py）。

设计约束（见 docs/开发计划.md 第二节硬约束）：
    - 纯本地、零网络。
    - 代码"野生"：可能混合编码（中文项目常见 GBK/GB2312/UTF-8±BOM）。
    - 排除编译产物与二进制：target/ build/ out/ .git/ .idea/ node_modules/ *.jar ...
    - 符号链接不跟随，避免环路与越界。
    - 规模大：用生成器逐个产出，避免一次性吃满内存。

编码探测策略（务实、可解释，不追求 100% 玄学）：
    1. BOM 优先：UTF-8/UTF-16 BOM 直接定调。
    2. 尝试严格 UTF-8 解码，成功即 UTF-8（绝大多数现代文件）。
    3. 失败则借 charset-normalizer（若装了 [encoding] extra）猜测，
       中文老项目通常落在 gb18030/gbk。
    4. 仍不确定则回退 gb18030（GBK 超集，对中文老项目兜底最稳），
       并把置信度标低、附带 unknown 提示——宁可标注也不臆造。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

# ── 默认排除规则 ────────────────────────────────────────────────
# 目录名（任意层级命中即整树跳过）。编译产物 / VCS / IDE / 依赖缓存。
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    "target", "build", "out", "bin", "dist",
    ".git", ".svn", ".hg",
    ".idea", ".vscode", ".settings",
    "node_modules", "__pycache__",
    ".gradle", ".mvn",
})

# 后缀（小写）。当前阶段只摄取 Java；保留集合以便后续扩展。
DEFAULT_INCLUDE_SUFFIXES: frozenset[str] = frozenset({".java"})

# 二进制/产物后缀，显式记一份用于将来诊断；当前靠 include 白名单已足够。
BINARY_SUFFIXES: frozenset[str] = frozenset({
    ".jar", ".class", ".war", ".zip", ".gz", ".7z",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf",
    ".so", ".dll", ".exe", ".bin",
})


@dataclass(frozen=True)
class SourceFile:
    """一个被摄取的源码文件及其读取结果。

    text 为 None 表示读取/解码彻底失败（极罕见，已尽力兜底）；此时 error 给原因。
    encoding 是最终采用的编码；confidence ∈ [0,1] 表示编码判定的把握。
    """

    path: Path                    # 绝对路径
    relpath: str                  # 相对扫描根的 POSIX 风格路径（稳定、可读、可比对）
    size: int                     # 字节数
    encoding: str | None          # 最终采用的编码（如 "utf-8" / "gb18030"）
    confidence: float             # 编码判定置信度 0~1
    text: str | None              # 解码后的文本；失败为 None
    error: str | None = None      # 失败原因；正常为 None

    @property
    def ok(self) -> bool:
        return self.text is not None


@dataclass
class ScanResult:
    """一次目录扫描的汇总结果。"""

    root: Path
    files: list[SourceFile] = field(default_factory=list)
    skipped_dirs: list[str] = field(default_factory=list)   # 命中排除规则的目录（relpath）
    skipped_symlinks: list[str] = field(default_factory=list)

    @property
    def ok_files(self) -> list[SourceFile]:
        return [f for f in self.files if f.ok]

    @property
    def failed_files(self) -> list[SourceFile]:
        return [f for f in self.files if not f.ok]


# ── 编码探测 ────────────────────────────────────────────────────
_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"


def detect_encoding(raw: bytes) -> tuple[str, float]:
    """返回 (encoding, confidence)。不抛异常，总能给出一个可用编码。"""
    if raw.startswith(_BOM_UTF8):
        return "utf-8-sig", 1.0
    if raw.startswith(_BOM_UTF16_LE) or raw.startswith(_BOM_UTF16_BE):
        return "utf-16", 1.0

    # 纯 ASCII / 合法 UTF-8 —— 现代文件主流，严格解一遍最可靠。
    try:
        raw.decode("utf-8")
        return "utf-8", 1.0
    except UnicodeDecodeError:
        pass

    # 借 charset-normalizer 猜（可选依赖）。中文老项目多为 gb18030/gbk。
    try:
        from charset_normalizer import from_bytes  # type: ignore

        best = from_bytes(raw).best()
        if best is not None and best.encoding:
            enc = _normalize_cn(best.encoding)
            # charset-normalizer 的 chaos 越低越好；粗略映射到置信度。
            conf = max(0.3, min(0.95, 1.0 - float(best.chaos)))
            # 项目偏置：苍穹是大陆简体软件，元数据/源码中文几乎都是 GB 系。
            # charset-normalizer 在短样本上常把简体 GBK 误判成 big5/其它 CJK；
            # 只要 gb18030 能严格解出，就优先 gb18030（GBK 超集），并略降置信以示存疑。
            if enc != "gb18030" and _decodes_strict(raw, "gb18030"):
                if enc in {"big5", "shift-jis", "euc-jp", "euc-kr"} or not _decodes_strict(raw, enc):
                    return "gb18030", min(conf, 0.6)
            return enc, conf
    except Exception:
        pass

    # 兜底：gb18030 是 GBK 超集，对中文老项目最不易炸；置信度标低 + 由调用方按需标 unknown。
    return "gb18030", 0.3


def _decodes_strict(raw: bytes, enc: str) -> bool:
    try:
        raw.decode(enc, errors="strict")
        return True
    except (UnicodeDecodeError, LookupError):
        return False


def _normalize_cn(enc: str) -> str:
    """把 charset-normalizer 的别名收敛到 Python codec 名，中文统一用 gb18030 超集。"""
    e = enc.lower().replace("_", "-")
    if e in {"gb2312", "gbk", "gb18030", "hz-gb-2312", "x-gbk"}:
        return "gb18030"
    if e in {"big5", "big5hkscs"}:
        return "big5"
    return e


def _read_file(path: Path) -> tuple[str | None, str, float, str | None]:
    """读单个文件，返回 (text, encoding, confidence, error)。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, "unknown", 0.0, f"read error: {exc}"

    enc, conf = detect_encoding(raw)
    try:
        text = raw.decode(enc, errors="strict")
        return text, enc, conf, None
    except (UnicodeDecodeError, LookupError):
        # 探测编码仍解不动：用 gb18030 容错解码（replace），保住文本可用、标低置信。
        try:
            text = raw.decode("gb18030", errors="replace")
            return text, "gb18030", 0.2, "decoded with replacement (低置信)"
        except Exception as exc:  # 理论上不会走到
            return None, enc, 0.0, f"decode error: {exc}"


# ── 扫描 ────────────────────────────────────────────────────────
def iter_source_files(
    root: str | os.PathLike[str],
    *,
    include_suffixes: Iterable[str] = DEFAULT_INCLUDE_SUFFIXES,
    exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIRS,
    follow_symlinks: bool = False,
) -> Iterator[SourceFile]:
    """惰性遍历 root，逐个产出被摄取的源码文件。

    用 os.walk(topdown=True) 以便就地裁剪 dirnames，避免下钻被排除子树。
    """
    root_path = Path(root).resolve()
    includes = {s.lower() for s in include_suffixes}
    excludes = set(exclude_dirs)

    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=follow_symlinks):
        cur = Path(dirpath)
        # 就地裁剪：命中排除名或（不跟随时）符号链接目录都剔掉。
        kept = []
        for d in dirnames:
            if d in excludes:
                continue
            if not follow_symlinks and (cur / d).is_symlink():
                continue
            kept.append(d)
        dirnames[:] = kept

        for name in filenames:
            fpath = cur / name
            if fpath.suffix.lower() not in includes:
                continue
            if not follow_symlinks and fpath.is_symlink():
                continue
            try:
                size = fpath.stat().st_size
            except OSError:
                size = 0
            text, enc, conf, err = _read_file(fpath)
            yield SourceFile(
                path=fpath,
                relpath=fpath.relative_to(root_path).as_posix(),
                size=size,
                encoding=enc,
                confidence=conf,
                text=text,
                error=err,
            )


def scan(
    root: str | os.PathLike[str],
    *,
    include_suffixes: Iterable[str] = DEFAULT_INCLUDE_SUFFIXES,
    exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIRS,
    follow_symlinks: bool = False,
) -> ScanResult:
    """完整扫描并物化结果（含被跳过的目录/符号链接记录），便于覆盖率报告。"""
    root_path = Path(root).resolve()
    if not root_path.exists():
        raise FileNotFoundError(f"扫描根不存在: {root_path}")

    result = ScanResult(root=root_path)
    excludes = set(exclude_dirs)
    includes = {s.lower() for s in include_suffixes}

    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=follow_symlinks):
        cur = Path(dirpath)
        kept = []
        for d in dirnames:
            child = cur / d
            rel = child.relative_to(root_path).as_posix()
            if d in excludes:
                result.skipped_dirs.append(rel)
                continue
            if not follow_symlinks and child.is_symlink():
                result.skipped_symlinks.append(rel)
                continue
            kept.append(d)
        dirnames[:] = kept

        for name in filenames:
            fpath = cur / name
            if fpath.suffix.lower() not in includes:
                continue
            if not follow_symlinks and fpath.is_symlink():
                result.skipped_symlinks.append(
                    fpath.relative_to(root_path).as_posix()
                )
                continue
            try:
                size = fpath.stat().st_size
            except OSError:
                size = 0
            text, enc, conf, err = _read_file(fpath)
            result.files.append(
                SourceFile(
                    path=fpath,
                    relpath=fpath.relative_to(root_path).as_posix(),
                    size=size,
                    encoding=enc,
                    confidence=conf,
                    text=text,
                    error=err,
                )
            )

    result.files.sort(key=lambda f: f.relpath)
    return result
