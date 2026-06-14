"""ingest —— 源码摄取层（阶段1）。

把工具指向一坨"野生"目录：递归发现 .java、编码自动探测（GBK/GB2312/UTF-8/BOM）、
排除 target/build/.git/*.jar、处理符号链接，产出文件清单。绝不依赖编译或依赖解析。

计划模块：scanner.py（扫描）、encoding.py（编码探测）。
"""
