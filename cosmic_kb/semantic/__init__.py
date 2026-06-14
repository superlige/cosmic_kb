"""semantic —— 语义解析层（阶段9）。

把用户自然语言转成真实项目查询：中文名↔标识词典、NL→意图、RapidFuzz 候选打分、
低置信反问。解决"用户必须输入精确字段标识"的问题，但不让 LLM 硬猜字段。

计划模块：dictionary.py、resolver.py。
"""
