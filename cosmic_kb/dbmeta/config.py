"""DB 元数据源配置 —— 连接底层库所需的信息，只读、可扩展多数据库。

配置项（用户 2026-07-02 拍板需支持）：
    driver          数据库类型，已实现 postgresql / oracle，字段留出扩展位（mysql/达梦…）
    host / port     底层库 IP / 端口（PostgreSQL 默认 5432，Oracle 默认 1521）
    database        初始数据库（PostgreSQL 连接握手用的库名；Oracle 填 service 名）
    user / password 账号口令（password 可用环境变量 COSMIC_DB_PASSWORD 覆盖，避免明文落盘）
    table_database  元数据表所在库名（PostgreSQL 不能跨库查询，实际取数连的就是它；
                    Oracle 无跨库概念，留空回落 database 即所连 service；留空则回落到 database）
    schema          表所在 schema（PostgreSQL 命名空间默认 public；Oracle 里是对象 owner，
                    通常为大写用户名，需按实际填写）

红线：本配置驱动的连接**强制只读**（见 connection.py），配置里没有、也不接受任何写入开关。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# 默认配置文件名（在 cwd / 项目根查找）。密码建议不写进文件，改用环境变量。
DEFAULT_CONFIG_NAMES = ("cosmic_db.json", ".cosmic_db.json")
# 密码环境变量：优先级高于配置文件里的 password（防明文口令进版本库）。
PASSWORD_ENV = "COSMIC_DB_PASSWORD"

# 两张设计表的表名（用户确认，固定）。做成常量便于将来按 driver 覆盖。
FORM_TABLE = "t_meta_formdesign"
ENTITY_TABLE = "t_meta_entitydesign"
# 元数据标识列 / XML 正文列（用户确认）。
NUMBER_COLUMN = "fnumber"
DATA_COLUMN = "fdata"
# 主键列 / 继承母体外键列（2026-07-03 拍板新增：本地扩展的 fnumber 可能因平台标识
# 长度限制被截断，按"截断字符串猜原厂 fnumber"不可靠；改按 fmasterid→fid 的库内
# 关系直接回溯母体行，不受命名截断影响，见 reader.py::_fetch_master_fdata）。
ID_COLUMN = "fid"
MASTER_ID_COLUMN = "fmasterid"
# 转换规则表（2026-07-05 用户实测确认，不在上面两张设计表里）。
CONVERT_RULE_TABLE = "t_botp_convertrule"
# 二开标识列 / 最后修改时间列（2026-07-05 用户实测确认三张表都有，用于增量同步判定）。
ISV_COLUMN = "fisv"
MODIFY_TIME_COLUMN = "fmodifydate"


@dataclass
class DbConfig:
    """底层库连接配置。仅承载"连哪、读什么"，不承载任何写入能力。"""

    driver: str = "postgresql"
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "postgres"
    user: str = ""
    password: str = ""
    table_database: str = ""      # 元数据表所在库；空则用 database
    schema: str = "public"

    # 表 / 列名默认取平台约定，允许配置覆盖（不同版本/ISV 定制时兜底）。
    form_table: str = FORM_TABLE
    entity_table: str = ENTITY_TABLE
    number_column: str = NUMBER_COLUMN
    data_column: str = DATA_COLUMN
    id_column: str = ID_COLUMN
    master_id_column: str = MASTER_ID_COLUMN
    convert_rule_table: str = CONVERT_RULE_TABLE
    isv_column: str = ISV_COLUMN
    modify_time_column: str = MODIFY_TIME_COLUMN

    @property
    def read_database(self) -> str:
        """实际用于读元数据表的库名（table_database 优先，回落 database）。"""
        return self.table_database or self.database

    def to_dict(self, *, redact: bool = True) -> dict:
        """导出为字典；redact=True 时隐去口令（用于日志/回显）。"""
        return {
            "driver": self.driver,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "user": self.user,
            "password": "***" if redact and self.password else self.password,
            "table_database": self.table_database,
            "schema": self.schema,
            "form_table": self.form_table,
            "entity_table": self.entity_table,
            "number_column": self.number_column,
            "data_column": self.data_column,
            "id_column": self.id_column,
            "master_id_column": self.master_id_column,
            "convert_rule_table": self.convert_rule_table,
            "isv_column": self.isv_column,
            "modify_time_column": self.modify_time_column,
        }


def from_dict(data: dict) -> DbConfig:
    """从字典构建配置，只认已知字段（未知键忽略，防手滑写坏）。密码可被环境变量覆盖。"""
    known = {f.name for f in DbConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    kwargs = {k: v for k, v in data.items() if k in known}
    cfg = DbConfig(**kwargs)
    env_pwd = os.environ.get(PASSWORD_ENV)
    if env_pwd:
        cfg.password = env_pwd
    return cfg


def find_config_file(explicit: str | Path | None = None) -> Path | None:
    """定位配置文件：显式路径优先，否则在 cwd 找默认文件名。找不到返回 None。"""
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    cwd = Path.cwd()
    for name in DEFAULT_CONFIG_NAMES:
        cand = cwd / name
        if cand.exists():
            return cand
    return None


def _strip_json_comments(text: str) -> str:
    """去掉整行 // 注释（模板自带说明行）。JSON 不支持注释，读回前先滤掉。

    只处理"整行注释"（行首去空白后以 // 开头），不动行内值里的 //（如 URL），
    避免误伤合法数据。
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("//")
    )


def load_config(explicit: str | Path | None = None) -> DbConfig:
    """加载配置文件为 DbConfig；找不到文件抛 FileNotFoundError，内容非法抛 ValueError。"""
    path = find_config_file(explicit)
    if path is None:
        raise FileNotFoundError(
            "未找到数据库配置文件。请在项目根放 cosmic_db.json，或用 --config 指定；"
            "可先跑 `cosmic_kb db-meta --init-config` 生成模板。"
        )
    raw = _strip_json_comments(path.read_text(encoding="utf-8"))
    if not raw.strip():
        raise ValueError(
            f"配置文件为空: {path}。请填写连接信息，"
            f"或删除后用 `cosmic_kb db-meta --init-config` 重新生成模板。"
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"配置文件 JSON 解析失败: {path}（{e}）。请检查格式。") from e
    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层应是一个对象(JSON dict): {path}")
    return from_dict(data)


def sample_config_text() -> str:
    """生成一份带注释说明的配置模板文本（口令留空，走环境变量）。"""
    template = {
        "driver": "postgresql",
        "host": "127.0.0.1",
        "port": 5432,
        "database": "postgres",
        "user": "readonly_user",
        "password": "",
        "table_database": "",
        "schema": "public",
    }
    header = (
        "// 苍穹底层库元数据源配置（只读）。password 建议留空、改用环境变量 "
        "COSMIC_DB_PASSWORD。\n"
        "// driver：postgresql（默认，port 5432）或 oracle（port 1521，装 pip install "
        "oracledb；database 填 service 名、schema 填对象 owner 通常为大写用户名）。\n"
        "// table_database：元数据表所在库名，留空则用 database。\n"
    )
    return header + json.dumps(template, ensure_ascii=False, indent=2)
