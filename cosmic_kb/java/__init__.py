"""java —— Java 静态分析层（阶段1/5/6/7）。

用 tree-sitter-java 的错误恢复治"不可编译"代码；不做类型解析、不解析依赖。
识别插件类型、事件方法、字段读写、DynamicObject 路径、入库判断。
遇到不认识的符号（kd.bos.*、SaveServiceHelper）一律当外部已知符号，用 SDK 目录解释，而非报错。

计划模块：parser.py(阶段1)、plugin_classifier.py / event_extractor.py / field_access.py(阶段5)、
path_tracer.py(阶段6)、persistence.py(阶段7)。
"""
