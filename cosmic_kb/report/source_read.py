"""源码读取公共件：源码根解析 + 按建库同款野生编码探测读文件。

供 `read_source`（模式 A：让大模型读源码走我们的工具，自动正确解码 + 标注字段名）使用。编码
探测复用 `ingest.scanner.detect_encoding`，保证读出的行号与 KB 记录一致（红线 #2：野生
GBK/GB2312/UTF-8±BOM 混杂，原生 reader 易乱码）。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..graph import store


def resolve_source_root(conn, source_root: str | None) -> str | None:
    """源码根：入参优先，否则取建库时记入 kb_meta 的 source_args.source_root。"""
    if source_root:
        return source_root
    raw = store.get_meta(conn, "source_args")
    if not raw:
        return None
    try:
        return (json.loads(raw) or {}).get("source_root")
    except (json.JSONDecodeError, TypeError):
        return None


def read_text(root: str | None, relpath: str | None) -> tuple[str | None, str | None]:
    """按建库同款编码探测读源文件。返回 (文本, 编码名)；读不到返回 (None, None)。"""
    if not root or not relpath:
        return None, None
    p = Path(root) / relpath
    if not p.is_file():
        return None, None
    try:
        raw = p.read_bytes()
    except OSError:
        return None, None
    from ..ingest import scanner
    enc, _conf = scanner.detect_encoding(raw)
    try:
        return raw.decode(enc, errors="strict"), enc
    except (UnicodeDecodeError, LookupError):
        return raw.decode("gb18030", errors="replace"), "gb18030"


def within_root(root: str, relpath: str) -> bool:
    """防越界：relpath 解析后必须落在 root 之内（拒绝 ../ 逃逸读项目外文件）。"""
    try:
        base = Path(root).resolve()
        target = (base / relpath).resolve()
    except (OSError, ValueError):
        return False
    return target == base or base in target.parents
