"""识别"扩展二开"元数据，推导它扩展的原厂（vendor）实体 fnumber。

苍穹里对原厂标准单据/基础资料（`bd_customer` 等）做扩展开发时，ISV 导出的 dym 只含
扩展部分，命名习惯是 `<isv前缀>_<原厂fnumber>_ext`（如 `cqkd_bd_customer_ext` 扩展了
`bd_customer`），且元数据自己会在 `<InheritPath>` 里记一条继承链（证明"我是从某模板
派生的"）。

红线 #4：命名规律只是线索，不是证据——本模块只给"候选 fnumber"，真正确认交调用方去
底层库实际查一次（查到=确认，查不到=放弃，不臆造，见 `cosmic_kb/dbmeta/integrate.py`）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .model import MetaModel

_EXT_SUFFIX = "_ext"


def detect_extension(model: "MetaModel", isv_prefixes: Iterable[str]) -> str | None:
    """返回本模型可能扩展的原厂 fnumber 候选；两个信号缺一都返回 None。

    信号一（结构，必要条件）：`model.inherit_path` 非空——元数据自己声明了继承关系。
    信号二（命名）：`key` 匹配 `^(<isv前缀>)(.+)_ext$`，中间段即候选 fnumber。
    """
    if not model.inherit_path:
        return None
    key = model.key or ""
    if not key.endswith(_EXT_SUFFIX):
        return None
    for prefix in isv_prefixes:
        if not prefix or prefix == "(none)":
            continue
        if not key.startswith(prefix):
            continue
        candidate = key[len(prefix):-len(_EXT_SUFFIX)]
        if candidate and re.fullmatch(r"[a-z][a-z0-9_]*", candidate):
            return candidate
    return None
