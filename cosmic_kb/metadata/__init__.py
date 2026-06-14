"""metadata —— 元数据解析层（阶段2）。

把苍穹元数据变成结构化业务对象：实体/字段/分录/子分录/基础资料/操作/按钮/
插件绑定/中文名↔标识/层级。支持单个 dym 与双层 zip 整包批量解析，统一 MetaModel + JSON 快照。

计划模块：dym_parser.py、package_loader.py、model.py。
"""
