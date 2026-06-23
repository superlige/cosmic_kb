"""自包含包验收：运行期资产经 importlib.resources 定位，**不依赖 parents[1] 同级目录布局**。

这组用例模拟「装进 site-packages 后」的取数路径——只走 `cosmic_kb._assets` 的 resources 访问器，
不碰 `PROJECT_ROOT`，确保资产随 wheel 走、能被 uvx/.mcpb 消费（docs/分发与多agent接入方案.md §4）。
"""

from __future__ import annotations

from cosmic_kb import _assets


def test_templates_packaged():
    """继承根模板随包：bos_billtpl / bos_basetpl 两个 dym 都在。"""
    root = _assets.templates_root()
    names = {e.name for e in root.iterdir()}
    assert {"bos_billtpl.dym", "bos_basetpl.dym"} <= names


def test_references_and_rules_packaged():
    """references（嵌套 adv/ base/）与 rules 都可枚举，且能定位到具体主题。"""
    topics = {rel for rel, _ in _assets.iter_reference_topics()}
    assert any(rel.startswith("adv/") for rel in topics)
    assert any(rel.startswith("base/") for rel in topics)
    assert "rules/anti-patterns" in topics


def test_read_topic_resolution_modes():
    """topic 解析三态：相对路径精确 / 文件名 stem / 子串。"""
    assert _assets.read_topic("base/sdk/sdk-orm-access")  # 相对路径精确
    assert _assets.read_topic("sdk-orm-access")           # 文件名 stem
    assert _assets.read_topic("nonexistent-topic-xyz") is None


def test_docs_db_optional_when_unset(monkeypatch):
    """ok-cosmic-docs.db 是可选资产：未配置环境变量且源码树无库时返回 None，不报错。"""
    monkeypatch.delenv("COSMIC_KB_DOCS_DB", raising=False)
    # 仅断言可调用、类型正确（开发树可能恰好有库，故不强求 None）。
    assert _assets.docs_db_path() is None or _assets.docs_db_path().is_file()
