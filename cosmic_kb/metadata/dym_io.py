"""阶段 2 · dym/模板 XML 的健壮读取。

dym 本质是 XML，文件头声明 `encoding="UTF-8"`，但本工具面对的是"野生"导出
（见 CLAUDE.md 硬约束），故读取时不盲信声明：先按声明/UTF-8 解析，失败再退 gb18030。
解析统一返回 ElementTree 的根 `Element`，上层只跟结构打交道、不碰字节。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

# 匹配 XML 声明，便于在改用其它编码重解时去掉它（避免与实际编码冲突）。
_DECL_RE = re.compile(rb"^\s*<\?xml[^>]*\?>", re.IGNORECASE)


def parse_bytes(raw: bytes) -> ET.Element:
    """把原始字节解析成 XML 根元素。先信任声明，失败退 gb18030 容错。"""
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        pass
    # 去掉声明后用 gb18030 容错解码再解析（中文老项目兜底）。
    body = _DECL_RE.sub(b"", raw, count=1)
    text = body.decode("gb18030", errors="replace")
    return ET.fromstring(text)


def parse_file(path: str | Path) -> ET.Element:
    """读取并解析一个 dym 文件，返回 XML 根元素。"""
    raw = Path(path).read_bytes()
    return parse_bytes(raw)
