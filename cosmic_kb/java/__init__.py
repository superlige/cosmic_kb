"""java —— Java 静态分析层（阶段1/5/6/7）。

用 tree-sitter-java 的错误恢复治"不可编译"代码；不做类型解析、不解析依赖。
识别插件类型、事件方法、字段读写、DynamicObject 路径、入库判断。
遇到不认识的符号（kd.bos.*、SaveServiceHelper）一律当外部已知符号，用 SDK 目录解释，而非报错。

模块：parser.py(阶段1，解析状态)、ast_index.py(AST 语义遍历)、constants.py(全局常量值表)、
plugin_classifier.py / event_extractor.py / field_access.py(阶段5)、call_graph.py(阶段6 类内
调用链)、persistence.py(阶段7 落库判定)、analyze.py(编排，产出 plugin_method / field_access)。

阶段 12 起叠加 symbols/ 编译期符号解析子包（类路径发现 + JVM 微工具 + SymbolTable，
vendor/ 放随包 fat jar）：跨类调用从名字启发式升级为确定性类型绑定，解不出退回
名字匹配并标注来源（精度分级共存）；12.2 注入本包管线。
"""
