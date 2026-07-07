"""标识 ↔ 中文名词典（`resolve_fields` 的语料底座）。

`resolve_fields` 要把源码里读到的标识（字段/实体容器/单据 key）精确核对成元数据真实中文名，
本模块从 KB 里把 form/entity/field 的「标识 ↔ 中文名」抽成可检索语料，供其做**精确 key 命中**
（不做模糊/中文名召回——模型自己从源码字面量读出精确标识后传来核对，见 `report/resolve_fields.py`
模块文档；原来服务自然语言意图解析的模糊匹配随 `ask` 命令一并于 2026-07 退役）。

设计纪律（对齐红线·证据优先）：
- **同名多义全保留**：一个 key 可能跨多张单据/多个层级出现（"cqkd_amount"到处都是），
  本层只负责"把所有候选摆出来"，**绝不替用户选一个**——消歧交给调用方。
- 纯查 KB，无副作用；KB 是契约，本层不改 schema、不碰扫描器。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldEntry:
    """一个字段在某实体坐标下的定义（同一 field_key 可能有多条，跨单据/层级）。"""

    key: str
    name: str | None
    form_key: str | None
    form_name: str | None
    entity_key: str | None
    level: str | None
    kind: str | None
    field_type: str | None = None   # XML 标签名（MulBasedataField/BasedataField/...），判 getDynamicObjectCollection 取值语义的精确信号
    uid: str | None = None                              # field 表 uid（关联 combo_items）
    ref_entity_id: str | None = None                    # 基础资料引用字段 <BaseEntityId> 原始 oid
    ref_form_key: str | None = None                     # oid 反查命中的目标单据 key（解不出为 None）
    ref_form_name: str | None = None                    # 目标单据中文名
    combo_items: tuple[tuple[str | None, str | None], ...] = ()  # 下拉选项 (value, caption) 列表


@dataclass(frozen=True)
class EntityEntry:
    """一个分录/子分录容器本身（在 `entity` 表，不是 `field` 表）。

    模型读到 `getDynamicObjectCollection("cqkd_zdfl")`——这是**分录 key** 不是字段 key，
    打 `field` 表会漏；故 resolve 要同时查 entity 表识别容器。表头实体（level=header）也照实收。
    """

    key: str
    name: str | None
    form_key: str | None
    level: str | None
    parent_key: str | None


@dataclass(frozen=True)
class FormEntry:
    key: str
    name: str | None
    form_type: str | None


class Lexicon:
    """从 KB 一次性构建的检索语料；`resolve_fields` 在其上做精确 key 定位。"""

    def __init__(self, conn) -> None:
        self.fields: list[FieldEntry] = []
        self.entities: list[EntityEntry] = []
        self.forms: list[FormEntry] = []
        self._build(conn)

    # ── 构建 ────────────────────────────────────────────────────────────────
    def _build(self, conn) -> None:
        form_names = {r["key"]: r["name"] for r in conn.execute("SELECT key,name FROM form")}

        self.forms = [
            FormEntry(r["key"], r["name"], r["form_type"])
            for r in conn.execute("SELECT key,name,form_type FROM form")
            if r["key"]
        ]
        combo_by_uid: dict[str, list[tuple[str | None, str | None]]] = {}
        for r in conn.execute("SELECT field_uid,value,caption FROM field_combo_item"):
            combo_by_uid.setdefault(r["field_uid"], []).append((r["value"], r["caption"]))

        self.fields = [
            FieldEntry(r["key"], r["name"], r["form_key"], form_names.get(r["form_key"]),
                       r["entity_key"], r["level"], r["kind"], r["field_type"],
                       r["uid"], r["ref_entity_id"], r["ref_form_key"], r["ref_form_name"],
                       tuple(combo_by_uid.get(r["uid"], ())))
            for r in conn.execute(
                "SELECT key,name,form_key,entity_key,level,kind,field_type,"
                "uid,ref_entity_id,ref_form_key,ref_form_name FROM field")
            if r["key"]
        ]
        self.entities = [
            EntityEntry(r["key"], r["name"], r["form_key"], r["level"], r["parent_key"])
            for r in conn.execute(
                "SELECT form_key,key,name,level,parent_key FROM entity")
            if r["key"]
        ]

        # ── 索引 ──
        self._fields_by_key: dict[str, list[FieldEntry]] = {}
        for f in self.fields:
            self._fields_by_key.setdefault(f.key, []).append(f)
        self._entities_by_key: dict[str, list[EntityEntry]] = {}
        for e in self.entities:
            self._entities_by_key.setdefault(e.key, []).append(e)
        self._form_by_key: dict[str, FormEntry] = {f.key: f for f in self.forms}

    # ── 精确查（标识命中）────────────────────────────────────────────────────
    def fields_by_key(self, key: str) -> list[FieldEntry]:
        return self._fields_by_key.get(key, [])

    def entities_by_key(self, key: str) -> list[EntityEntry]:
        """分录/子分录容器（在 `entity` 表）按 key 精确命中（与 fields_by_key 对称）。"""
        return self._entities_by_key.get(key, [])

    def form_by_key(self, key: str) -> FormEntry | None:
        return self._form_by_key.get(key)


def build_lexicon(conn) -> Lexicon:
    """便捷入口：从 KB 连接构建词典。"""
    return Lexicon(conn)
