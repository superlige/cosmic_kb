"""graph —— 知识图谱存储层（阶段4/8）。

SQLite 图谱 + FTS5 + JSON 快照，幂等重建。节点：entity/field/plugin/class/method/module；
边：绑定、引用、下推(BOTP)、审核回写、跨单据创建等业务流关系。KB 即两段之间的契约。

计划模块：schema.sql、store.py(阶段4)、biz_flow.py(阶段8)。
"""
