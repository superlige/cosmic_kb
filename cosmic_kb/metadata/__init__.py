"""metadata —— 元数据解析层（阶段2）。

把苍穹元数据变成结构化业务对象：实体/字段/分录/子分录/基础资料/操作/按钮/
插件绑定/中文名↔标识/层级。支持单个 dym 与双层 zip 整包批量解析，统一 MetaModel + JSON 快照。

模块：
    model.py            统一数据结构 MetaModel（实体/字段/分录/操作/插件）+ to_dict 快照
    dym_io.py           dym/模板 XML 的健壮读取（编码兜底）
    dym_parser.py       单 dym 三类解析器（bill/basedata/dynamic 同一入口）
    template_loader.py  继承根模板加载：hex oid → 操作语义（按 ModelType 选模板）
    package_loader.py   整包双层 zip 解析（kdpkgs.xml → dm/*.zip → metadata/*.dym）
"""
