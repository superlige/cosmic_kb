"""bridge —— 元数据 ↔ 代码桥接层（阶段3，全项目枢纽）。

把元数据里的 ClassName 解析到源码树里真实的 .java，并分类三态：
① 绑定且有源码（正常）；② 绑定但为平台插件 kd.bos.*（外部、无源码）；
③ 源码里大量 service/util/webapi 未被任何元数据绑定（孤儿类，也要纳入）。
处理多 ISV 前缀映射（cqkd_/cqspb/kd_），产出桥接报告。

计划模块：linker.py、namespace.py。
"""
