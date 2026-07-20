"""cosmic_kb —— 苍穹历史项目本地理解工具（段一：确定性扫描器）。

定位：不是分析库，而是"接手陌生苍穹老项目时，在本机上跑的项目理解工具"。
本包负责"段一"——纯本地、确定性地扫描元数据与 Java 源码，建立项目知识库(KB)
并产出可信度/覆盖率报告。AI 理解层（段二）经工具查 KB 取证，并可直接读本机源码全文做完整理解。

KB 是两段之间的契约：段一写 KB，段二读 KB。

子包与开发阶段映射（详见 ../docs/核心/开发计划.md）：
    ingest    阶段1  源码摄取：扫目录、编码探测、排除 target/build/jar
    metadata  阶段2  元数据解析：dym/zip → 实体/字段/分录/绑定
    java      阶段1/5/6/7  Java 静态分析：tree-sitter、事件、字段访问、路径、入库
    bridge    阶段3  元数据↔代码桥接：ClassName → 源码文件，三态分类
    graph     阶段4/8  知识图谱存储(SQLite+FTS5)、业务流边
    semantic  阶段9  语义解析：NL→意图、中文名↔标识词典、模糊候选
    context   阶段9  Context Builder：组装带证据的 AI 上下文
    report    阶段1/4  覆盖率报告、项目地图、接手者理解报告
    cli       全阶段  命令行入口（含 `cosmic_kb --version` 冒烟命令）

阶段 0 仅搭好以上骨架与随包资产接线（semantics/templates）；各子包的实现随阶段填充。
"""

from __future__ import annotations

__version__ = "0.2.1"

__all__ = ["__version__"]
