"""dbmeta —— 苍穹底层库元数据源（只读）。

动机（docs/设计方案/扩展元数据识别方案.md）：ISV 导出的 dym 只含扩展部分，原厂标准单据
（bd_customer 等）的完整元数据不在包里，导致扩展单据字段级排障"结构性半盲"。
但平台底层库里躺着**全部** form/entity 元数据；直连库把这两张表的 fdata 取回、
拼回 MetaModel，即可拿到原厂标准字段，补齐三个硬伤。

红线（不违背本地优先 #1）：**强制只读**（多重防线见 connection.py），只取元数据、
不落公网，取回结果照旧只进本机 KB。DB 在架构上就是"另一种 dym 来源"。

模块：
    config.py      连接配置 DbConfig（ip/端口/初始库/账号/口令/表所在库/schema），可扩展多库
    connection.py  只读驱动抽象 + PostgresDriver（SQL 白名单 + 会话只读 + 永不提交）
    reader.py      按 fnumber 取两表 fdata → 合成 MetaModel；ping 自检
    assemble.py    两段 fdata XML 套回 DeployMetadata 骨架 → 复用 metadata.parse_element（零改动）
    discover.py    发现代码库里引用到、本地元数据没有的候选原厂 key（三类确定性信号：
                   扩展母体 / ORM 查询 / 操作执行，命中即必摄取）
    integrate.py   把 --vendor 指定的原厂 fnumber 拉取/合并进 build/bridge 的 models 列表
    sync.py        build --db-config 自动全量同步本项目自己（二开）ISV 当前的 form/entity/
                   转换规则内容（同 key 整条替换，非 vendor 合并语义）
"""

from .assemble import assemble_convert_rule, assemble_model
from .config import DbConfig, load_config, sample_config_text
from .discover import VendorCandidate, discover_candidates, known_keys_from_db, isv_prefixes_from_db
from .integrate import apply_vendor_metadata
from .reader import DbMetaReader
from .sync import IsvAmbiguousError, SyncResult, resolve_isv, sync_own_isv_metadata

__all__ = [
    "DbConfig",
    "DbMetaReader",
    "IsvAmbiguousError",
    "SyncResult",
    "VendorCandidate",
    "apply_vendor_metadata",
    "assemble_convert_rule",
    "assemble_model",
    "discover_candidates",
    "known_keys_from_db",
    "isv_prefixes_from_db",
    "load_config",
    "resolve_isv",
    "sample_config_text",
    "sync_own_isv_metadata",
]
