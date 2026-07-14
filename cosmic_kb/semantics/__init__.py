"""苍穹语义文档资产包 —— `cosmic_semantics` 工具与 Claude skill 共用的**单一源**。

这里只放**随包分发的数据文件**（`references/` 苍穹插件/SDK 文档 + `rules/` 反模式黑名单），
不含逻辑。它们经 `importlib.resources` 定位（见 `cosmic_kb/_assets.py`），故工具被 `pip install`
进 site-packages（非 `-e` 可编辑安装）后仍能读到——这是「自包含 MCP 包」的地基。

为什么从旧版 Skill 下沉到这里（对齐 docs/设计方案/分发与多agent接入方案.md §2）：
- 段二语义层若只绑在某一宿主的 Skill 上，则无法跨宿主复用；下沉进包后，任意 MCP agent
  调 `cosmic_semantics(topic)` 即可拿到同一份苍穹领域知识。
- `cosmic-kb-understand` 和 `cosmic-kb-setup` 只编排工作流，语义文档统一从本包取，避免维护漂移。
"""
