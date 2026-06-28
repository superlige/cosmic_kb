"""注解驱动 POJO↔DynamicObject 映射写入识别（A 档）验收测试。

覆盖：
  * 反射 bulk-write（convertTo…DynamicObject）经事件 BFS → 该映射类全部字段产合成写入，
    via=annotation-map、access_class=映射类、plugin_fqn=调用它的插件入口、persists=unknown；
  * KB 反验证（不臆造）：注解 value 不在元数据字段 key 集时整条丢弃，不产合成行；
  * form_key 诚实：唯一反查命中单据则回填（metadata_unique），歧义且无绑定收敛则留 None；
  * summary.annotation_writers 标量 + trace_compact 防膨胀（字节 < 32KB、不另起顶层数组）。

tree-sitter 未装则跳过（与其余 java 测试一致）。
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("tree_sitter_java")

from pathlib import Path

from cosmic_kb.bridge import linker, namespace
from cosmic_kb.graph import store
from cosmic_kb.ingest import scanner
from cosmic_kb.metadata.model import (
    MetaEntity, MetaField, MetaModel, MetaOperation, MetaPlugin,
)
from cosmic_kb.report import field_trace, project_map

# ── 源码 fixtures ──────────────────────────────────────────────────────────

# 自定义字段映射注解（其 @interface 本身无需被解析——识别走"用法 value ∈ KB key"）。
ANNO = """package cqspb.am;
import java.lang.annotation.Retention;
import java.lang.annotation.RetentionPolicy;
@Retention(RetentionPolicy.RUNTIME)
public @interface BillAnno {
  String value();
  String name();
  String type() default "head";
  String entryDoName() default "";
}
"""

# 映射 POJO：注解把 Java 字段映射到元数据 key；convertTo…DynamicObject 反射批量 set。
# cqkd_ghost 不在元数据字段集 → 必须被 KB 反验证丢弃（不臆造）。
MAPPER = """package cqspb.am;
import kd.bos.dataentity.entity.DynamicObject;
import java.lang.reflect.Field;
public class BillMapper {
  @BillAnno(value="cqkd_amt", name="金额")
  private java.math.BigDecimal amt;
  @BillAnno(value="cqkd_qs", name="未缴金额")
  private java.math.BigDecimal unpaid;
  @BillAnno(value="cqkd_entry_amt", name="分录金额", type="entry", entryDoName="cqkd_entry")
  private java.math.BigDecimal entryAmt;
  @BillAnno(value="cqkd_ghost", name="不存在的字段")
  private String ghost;

  public DynamicObject convertToBillDynamicObject(Object billType) {
    DynamicObject bill = new DynamicObject();
    Field[] fields = this.getClass().getDeclaredFields();
    for (Field field : fields) {
      BillAnno annotation = field.getAnnotation(BillAnno.class);
      if (annotation != null) {
        String value = annotation.value();
        bill.set(value, field.get(this));
      }
    }
    return bill;
  }
}
"""

# 已绑定操作插件：事务内 new BillMapper().convertTo…() → 反射写入归因到本插件入口。
MAPPER_OP = """package cqspb.am;
import kd.bos.entity.plugin.AbstractOperationServicePlugIn;
public class MapperOp extends AbstractOperationServicePlugIn {
  public void beforeExecuteOperationTransaction(BeforeOperationArgs e) {
    BillMapper m = new BillMapper();
    m.convertToBillDynamicObject(null);
  }
}
"""

# 仅被孤立补全够到的映射类（无插件入口），写一个跨两单据歧义的 key → form_key 应诚实留 None。
LONE_MAPPER = """package cqspb.am;
import kd.bos.dataentity.entity.DynamicObject;
import java.lang.reflect.Field;
public class LoneMapper {
  @BillAnno(value="cqkd_amb", name="歧义字段")
  private String amb;

  public DynamicObject toDynamicObject(Object t) {
    DynamicObject bill = new DynamicObject();
    Field[] fields = this.getClass().getDeclaredFields();
    for (Field field : fields) {
      BillAnno annotation = field.getAnnotation(BillAnno.class);
      if (annotation != null) {
        String value = annotation.value();
        bill.set(value, field.get(this));
      }
    }
    return bill;
  }
}
"""


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


def _field(key: str, entity_key: str, level: str = "header") -> MetaField:
    return MetaField("TextField", key, key, "f" + key, "id" + key, "1", "entity", level, entity_key)


def _build(tmp_path: Path):
    src = tmp_path / "src"
    _w(src / "BillAnno.java", ANNO)
    _w(src / "BillMapper.java", MAPPER)
    _w(src / "MapperOp.java", MAPPER_OP)
    _w(src / "LoneMapper.java", LONE_MAPPER)
    scan = scanner.scan(src)

    ents = [
        MetaEntity("BillEntity", "cqkd_bill", "单据头", "1", "header", None, "t"),
        MetaEntity("EntryEntity", "cqkd_entry", "分录", "2", "entry", "cqkd_bill", "te"),
    ]
    # cqkd_amt / cqkd_qs 仅属 cqkd_bill（唯一）；cqkd_amb 同时属两单据（歧义）。
    m1 = MetaModel(key="cqkd_bill", name="资产单", model_type="BillFormModel",
                   form_type="bill", isv="cqkd", app_key="cqkd_am",
                   entities=ents,
                   fields=[_field("cqkd_amt", "cqkd_bill"), _field("cqkd_qs", "cqkd_bill"),
                           _field("cqkd_entry_amt", "cqkd_entry", "entry"),
                           _field("cqkd_amb", "cqkd_bill")],
                   plugins=[MetaPlugin("cqspb.am.MapperOp", "op", "project", operation_key="submit")],
                   operations=[MetaOperation("submit", "提交", "submit", None, None, resolved_from="self")])
    m2 = MetaModel(key="cqkd_bill2", name="资产单2", model_type="BillFormModel",
                   form_type="bill", isv="cqkd", app_key="cqkd_am",
                   entities=[MetaEntity("BillEntity", "cqkd_bill2", "头", "9", "header", None, "t2")],
                   fields=[_field("cqkd_amb", "cqkd_bill2")])
    models = [m1, m2]
    index = namespace.build_index(scan)
    bridge = linker.link(scan, models, index=index)
    mm = project_map.module_map(scan, models, bridge, index=index)
    db = tmp_path / "kb.db"
    store.build_kb(scan, models, bridge, mm, db, index=index)
    return db


def _facc(conn, **where):
    cols = ("field_key", "level", "entry_key", "plugin_fqn", "access_class",
            "event_method", "access", "persists", "via", "form_key", "form_key_source")
    sql = "SELECT " + ",".join(cols) + " FROM field_access"
    if where:
        sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
    return [dict(zip(cols, r)) for r in conn.execute(sql, tuple(where.values())).fetchall()]


def test_annotation_write_visible(tmp_path: Path):
    """反射映射写入经事件 BFS 不再隐形：via/归属/落库三态。"""
    db = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rows = {r["field_key"]: r for r in _facc(conn, via="annotation-map", plugin_fqn="cqspb.am.MapperOp")}
        # 两个映射到真实字段的 key 都被写出。
        assert "cqkd_amt" in rows and "cqkd_qs" in rows
        for key in ("cqkd_amt", "cqkd_qs"):
            r = rows[key]
            assert r["access"] == "write"
            assert r["access_class"] == "cqspb.am.BillMapper"      # 物理写入类=映射类
            assert r["plugin_fqn"] == "cqspb.am.MapperOp"          # 入口=调用它的插件
            assert r["event_method"] == "beforeExecuteOperationTransaction"
            assert r["persists"] == "unknown"                      # 落库取决于调用方是否保存产物，不臆断
        # 唯一反查 → form_key 诚实回填到 cqkd_bill（依据是字段归属，标 metadata_unique）。
        assert rows["cqkd_amt"]["form_key"] == "cqkd_bill"
        assert rows["cqkd_amt"]["form_key_source"] == "metadata_unique"
    finally:
        conn.close()


def test_annotation_entry_key_uses_entry_container_member(tmp_path: Path):
    """type=entry 时 entry_key 是分录容器 key，不是字段 key。"""
    db = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rows = _facc(conn, via="annotation-map", field_key="cqkd_entry_amt")
        assert rows
        assert rows[0]["level"] == "entry"
        assert rows[0]["entry_key"] == "cqkd_entry"
    finally:
        conn.close()


def test_annotation_gate_not_in_kb(tmp_path: Path):
    """KB 反验证（不臆造）：注解 value 不在元数据字段集 → 不产合成行。"""
    db = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        ghost = _facc(conn, via="annotation-map", field_key="cqkd_ghost")
        assert ghost == []
    finally:
        conn.close()


def test_annotation_ambiguous_form_none(tmp_path: Path):
    """歧义 key 且无绑定收敛（仅孤立补全够到）→ form_key 诚实留 None。"""
    db = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rows = _facc(conn, via="annotation-map", field_key="cqkd_amb", access_class="cqspb.am.LoneMapper")
        assert rows, "孤立映射类的反射写入也应被补全，不能隐形"
        assert all(r["form_key"] is None for r in rows), "跨两单据歧义、无绑定 → 不替选、留 None"
    finally:
        conn.close()


def test_summary_scalar_and_bounded(tmp_path: Path):
    """summary.annotation_writers 标量 + trace_compact 防膨胀（不另起顶层数组、字节 < 32KB）。"""
    db = _build(tmp_path)
    conn = store.open_kb(db)
    try:
        rich = field_trace.field_trace(conn, "cqkd_qs")
        assert rich["summary"].get("annotation_writers", 0) >= 1   # 标量计数透出

        compact = field_trace.trace_compact(conn, "cqkd_qs")
        # 顶层只多了 summary 里的标量，绝不新增顶层数组/section（防膨胀约束 #4）。
        assert "annotation_writers" not in compact                 # 不在顶层
        assert isinstance(compact["summary"].get("annotation_writers"), int)
        assert len(json.dumps(compact, ensure_ascii=False)) < 32768  # MCP 硬上限内

        # 合成写入并入既有 writer 类节点（按 access_class 合并），未另起数组。
        merged = []
        for g in compact["groups"]:
            merged += [c["class_fqn"] for c in g.get("writers", {}).get("classes", [])]
        merged += [c["class_fqn"] for c in compact.get("unlocated", {}).get("methods", []) if False]
        assert any(c == "cqspb.am.BillMapper" for c in merged) or \
            any(m.get("class_fqn") == "cqspb.am.BillMapper"
                for m in compact.get("unlocated", {}).get("methods", []))
    finally:
        conn.close()
