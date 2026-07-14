"""打包地基验收（对话式安装口令的落点）：`complete` extra 固定聚合、fuzzy 已彻底退役。

对话式安装口令固定装 `cosmic-kb[complete]`，所以 complete 必须存在、且聚合段一+段二的完整能力；
同时守 §4 语义资产两处：WHEN_TO_USE 路由表不得漂移到已删除的主题。
见 docs/设计方案/分发与多agent接入方案.md §3.4 / §4。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from cosmic_kb import _assets

REPO_ROOT = Path(__file__).resolve().parents[1]


def _optional_deps() -> dict[str, list[str]]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["optional-dependencies"]


def test_complete_extra_aggregates_full_stack():
    """complete 存在，并聚合 parse/encoding/mcp/postgres（自引用形式，随各分组同步）。"""
    extras = _optional_deps()
    assert "complete" in extras, "对话式安装口令固定装 [complete]，该分组必须存在"
    joined = " ".join(extras["complete"])
    for sub in ("parse", "encoding", "mcp", "postgres"):
        assert sub in joined, f"complete 应聚合 {sub}"


def test_fuzzy_extra_fully_retired():
    """fuzzy 已随模糊解析器退役：既不作为独立分组，也不混进 complete。"""
    extras = _optional_deps()
    assert "fuzzy" not in extras, "fuzzy extra 已退役，不应再声明（否则 pip install [...,fuzzy] 会报未知 extra）"
    assert "fuzzy" not in " ".join(extras["complete"])


def test_when_to_use_keys_resolve_to_packaged_topics():
    """WHEN_TO_USE 是随包文档之外的第二份事实源——每个键都必须能定位到实际打包的主题，防漂移。"""
    from cosmic_kb.mcp.server import WHEN_TO_USE

    for stem in WHEN_TO_USE:
        assert _assets.read_topic(stem) is not None, f"WHEN_TO_USE 路由到不存在的语义主题：{stem}"
