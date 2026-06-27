"""信任优先 · trace 返回设界（防 MCP 截断）验收测试。

起因：单字段 trace 一次返回 271KB 被 MCP 客户端截断——确定性层没对返回 dict 设界。
修复：① 删每行死重列（evidence 等）+ 白名单投影；② 顶层扁平 writers/readers 删除（与 groups 重复）；
③ 所有数组在 dict 里 cap，readers 折叠成「该读方法」清单；真实总数始终留在 summary（红线 #4 不丢数）。

本测覆盖：
  ① `_slim_row` 投影——丢 evidence/confidence 等，path 仅 len>1 时保留；
  ② `_collapse_reader_methods`——按 (类,方法) 去重 + cap + capped + locations≤3 + 真实 total；
  ③ 集成形状——groups[].readers 是清单 dict、行内无 evidence、顶层无 writers/readers、各数组 ≤ cap。
"""

from __future__ import annotations

from pathlib import Path

from cosmic_kb.graph import store
from cosmic_kb.report import field_trace

from _synthkb import make_kb


# ── ① _slim_row 投影 ─────────────────────────────────────────────────────────
def test_slim_row_drops_dead_weight():
    """精简行只留渲染所需字段，丢 evidence/confidence/event_phase/field_key/form_key/plugin_forms。"""
    full = {
        "access": "write", "level": "header", "entry_key": None,
        "event_method": "beforeDoOperation", "persists": "yes", "persist_reason": "save",
        "via": "do.set", "line": 41, "source_relpath": "a/B.java", "key_resolution": "literal",
        "plugin_fqn": "p.X", "plugin_simple": "X", "plugin_type": "op",
        "access_simple": "S", "cross_class": True, "plugin_form_label": "f「名」",
        "plugin_cross_form": False, "semantics_topic": "plugin-operation",
        # —— 应被丢弃的死重 ——
        "evidence": "整段源码片段" * 50, "confidence": 0.9, "event_phase": "transaction",
        "field_key": "cqkd_x", "form_key": "cqkd_a", "plugin_forms": [{"form_key": "f"}],
        "path": ["only-one"],
    }
    slim = field_trace._slim_row(full)
    for dropped in ("evidence", "confidence", "event_phase", "field_key", "form_key", "plugin_forms"):
        assert dropped not in slim, f"{dropped} 应被剔除"
    # 单元素 path 不保留（绝大多数行的常态，纯占字节）。
    assert "path" not in slim
    # 渲染所需字段悉数保留。
    assert slim["semantics_topic"] == "plugin-operation"
    assert slim["access_simple"] == "S" and slim["plugin_cross_form"] is False


def test_slim_row_keeps_multi_element_path():
    """调用链 path 长度 >1（跨类下钻）时保留——Web/CLI 只在此时渲染。"""
    slim = field_trace._slim_row({"path": ["A.m", "B.helper", "C.save"]})
    assert slim["path"] == ["A.m", "B.helper", "C.save"]


# ── ② _collapse_reader_methods 去重 + cap ────────────────────────────────────
def _read_row(plugin, method, cls=None, line=1):
    return {
        "plugin_fqn": plugin, "event_method": method, "access_class": cls or plugin,
        "plugin_simple": plugin.rsplit(".", 1)[-1], "plugin_type": "form",
        "plugin_form_label": None, "semantics_topic": "plugin-form",
        "source_relpath": "a/B.java", "line": line,
    }


def test_collapse_readers_dedups_by_method_and_keeps_total():
    """同 (类,方法) 多处读取去重成一条，count 累加；total 是去重前真实读取处数。"""
    rows = [_read_row("p.A", "m1", line=i) for i in range(5)] + \
           [_read_row("p.A", "m2"), _read_row("p.B", "m1")]
    out = field_trace._collapse_reader_methods(rows, cap=15)
    assert out["total"] == 7                 # 真实读取处数（红线 #4 不丢数）
    assert len(out["methods"]) == 3          # 去重成 3 个 (类,方法)
    top = out["methods"][0]                  # 按 count 降序
    assert top["count"] == 5 and top["method"] == "m1"
    assert top["calls"] == "calls p.A m1"
    assert len(top["locations"]) <= 3        # 物理位置至多 3 处
    assert out["capped"] == 0


def test_collapse_readers_caps_methods():
    """方法数超 cap → methods 截断、capped 记剩余、total 仍为全部读取处数。"""
    rows = [_read_row(f"p.C{i}", "m") for i in range(20)]
    out = field_trace._collapse_reader_methods(rows, cap=15)
    assert len(out["methods"]) == 15
    assert out["capped"] == 5
    assert out["total"] == 20


# ── ③ 集成形状（真实 synth KB）──────────────────────────────────────────────
def test_trace_dict_is_bounded(tmp_path: Path):
    conn = store.open_kb(make_kb(tmp_path))
    try:
        ft = field_trace.field_trace(conn, "cqkd_collateralstatus")
    finally:
        conn.close()
    # 顶层扁平 writers/readers 已删（与 groups 重复、无消费方）。
    assert "writers" not in ft and "readers" not in ft
    # 各坐标：writers 是 cap 后的精简行 list、readers 是「该读方法」清单 dict。
    for g in ft["groups"]:
        assert isinstance(g["writers"], list)
        assert len(g["writers"]) <= field_trace._CAP_WRITERS
        for w in g["writers"]:
            assert "evidence" not in w          # 死重列不外泄
        rd = g["readers"]
        assert set(rd) == {"total", "methods", "capped"}
        assert len(rd["methods"]) <= field_trace._CAP_READER_METHODS
    # 其余数组也设界。
    assert len(ft["possible"]) <= field_trace._CAP_POSSIBLE
    assert len(ft["unlocated"]) <= field_trace._CAP_UNLOCATED
    assert len(ft["coarse"]["locations"]) <= field_trace._CAP_COARSE
    # 真实总数仍在 summary（不丢数）。
    assert "writers" in ft["summary"] and "readers" in ft["summary"]
