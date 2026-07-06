"""阶段 2 · 继承根模板加载（hex oid → 操作语义）。

业务 dym 里覆盖继承操作时只写 `<Operation action="edit" oid="hexid"><Plugins>...`，
**没有 Key/Name/OperationType**，这些 hex oid 指向标准操作（保存/提交/审核…）的定义
在**继承根模板**里（bos_billtpl / bos_basetpl），没随业务 dym 导出。本模块把模板解析成
`hex oid → {key, name, operation_type}` 映射表，供 dym_parser 按 oid 回填操作语义。

关键事实（见 docs/核心/阶段验收.md）：
    - **标准操作 oid 不是平台全局，每类模板各一套**：同名 save，单据是 c91d5125000033ac，
      基础资料是 b599405400001aac。→ 回填必须按 ModelType 选对应模板，不能一张表通吃。
    - 抽操作 oid 必须取 `<Operation>` 的**直接子 `<Id>`**，不能取后代第一个 Id
      （audit 等块内嵌 StatusField/Parameter 也带 <Id>，会被误取成 status）。
    - 模板可能导出为裸 `EntityMetadata`（含 Operations），也可能是 DeployMetadata 壳。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import dym_io
from .. import _assets

# ModelType → 继承根模板文件名（不带扩展名匹配，便于不同导出命名）。
_TEMPLATE_BY_MODEL: dict[str, str] = {
    "BillFormModel": "bos_billtpl",
    "BaseFormModel": "bos_basetpl",
    # DynamicFormModel 的继承根 6fb46130… 暂未提供，dynamic 操作以 unknown 兜底。
}

# 模板默认搜索目录：随包分发的 cosmic_kb/metadata/templates/（经 importlib.resources 定位，
# 装进 site-packages 也能读到）。调用方仍可传 template_dir 覆盖。本地文件系统安装下
# files() 返回的就是 Path，支持下游 .is_dir()/.glob()。
DEFAULT_TEMPLATE_DIR = _assets.templates_root()


@dataclass
class OperationDef:
    """模板里一条标准操作的语义。"""

    oid: str
    key: str | None
    name: str | None
    operation_type: str | None


def _parse_template_operations(root: Any) -> dict[str, OperationDef]:
    """从模板 XML 树抽取 oid → 操作语义。

    只认 `<Operations>` 容器的**直接子 `<Operation>`**，并取其**直接子** Id/Key/Name/
    OperationType —— 避开块内嵌套元素的 Id（坑见 docs/核心/阶段验收.md）。
    """
    result: dict[str, OperationDef] = {}
    for ops in root.iter("Operations"):
        for op in ops.findall("Operation"):
            oid = op.findtext("Id")  # 直接子 Id = 模板里该操作的 hex oid
            if not oid:
                continue
            result[oid] = OperationDef(
                oid=oid,
                key=op.findtext("Key"),
                name=op.findtext("Name"),
                operation_type=op.findtext("OperationType"),
            )
    return result


def load_template_map(path: str | Path) -> dict[str, OperationDef]:
    """解析单个模板 dym，返回 oid → OperationDef。解析失败返回空表（不抛）。"""
    try:
        root = dym_io.parse_file(Path(path))
    except Exception:
        return {}
    return _parse_template_operations(root)


class TemplateRegistry:
    """按 ModelType 管理模板映射表，惰性加载 + 缓存。

    一个 registry 对应一个模板目录；解析整包/多 dym 时复用，避免重复解析模板。
    """

    def __init__(self, template_dir: str | Path | None = None) -> None:
        self.template_dir = Path(template_dir) if template_dir else DEFAULT_TEMPLATE_DIR
        self._cache: dict[str, dict[str, OperationDef]] = {}

    def _find_template_file(self, stem: str) -> Path | None:
        if not self.template_dir.is_dir():
            return None
        # 允许 bos_billtpl.dym / bos_billtpl_xxx.dym 等命名，以 stem 前缀匹配。
        for p in sorted(self.template_dir.glob("*.dym")):
            if p.stem == stem or p.stem.startswith(stem):
                return p
        return None

    def for_model_type(self, model_type: str | None) -> dict[str, OperationDef]:
        """取某 ModelType 对应的 oid 映射表（缺模板时返回空表）。"""
        if not model_type:
            return {}
        if model_type in self._cache:
            return self._cache[model_type]
        stem = _TEMPLATE_BY_MODEL.get(model_type)
        mapping: dict[str, OperationDef] = {}
        if stem:
            tpl = self._find_template_file(stem)
            if tpl:
                mapping = load_template_map(tpl)
        self._cache[model_type] = mapping
        return mapping
