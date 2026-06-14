"""skill_assets 资产定位 —— 复用现成 references 与 ok-cosmic-docs.db。

阶段 0 的"资产复用"在这里集中收口：所有对 references 目录、SDK 文档库
(ok-cosmic-docs.db)、苍穹语义 Skill 的路径解析都走本模块，避免散落硬编码。

布局假设（项目根 = 本包的上一级目录）：

    <project_root>/
    ├── cosmic_kb/            # 本包（段一扫描器）
    ├── comic-understand-long/  # 苍穹语义理解 Skill（段二语义层 + references/rules）
    └── skill_assets/         # 复用资产：ok-cosmic-docs.db 等
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# cosmic_kb/_assets.py -> cosmic_kb -> <project_root>
PROJECT_ROOT = Path(__file__).resolve().parents[1]

SKILL_DIR = PROJECT_ROOT / "comic-understand-long"
REFERENCES_DIR = SKILL_DIR / "references"
RULES_DIR = SKILL_DIR / "rules"
SKILL_MD = SKILL_DIR / "SKILL.md"

ASSETS_DIR = PROJECT_ROOT / "skill_assets"
DOCS_DB = ASSETS_DIR / "ok-cosmic-docs.db"


@dataclass(frozen=True)
class AssetStatus:
    """单个资产的存在性检查结果。"""

    name: str
    path: Path
    present: bool

    @property
    def label(self) -> str:
        return "OK" if self.present else "MISSING"


def check_assets() -> list[AssetStatus]:
    """返回所有关键资产的存在性检查结果，供 `cosmic_kb doctor` 使用。"""

    targets = {
        "skill": SKILL_DIR,
        "skill.md": SKILL_MD,
        "references": REFERENCES_DIR,
        "rules": RULES_DIR,
        "skill_assets": ASSETS_DIR,
        "ok-cosmic-docs.db": DOCS_DB,
    }
    return [AssetStatus(name, path, path.exists()) for name, path in targets.items()]
