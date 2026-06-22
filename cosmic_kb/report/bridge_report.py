"""阶段 3 · 桥接可信度报告（命中率 + 三态 + 孤儿 + 前缀）。

把 `BridgeResult` 渲染成"接手者一眼看懂、并据此决定信不信桥接"的报告：
- project 插件命中率多少（精确 + 按名）；
- 平台类是否都正确归外部；
- 源码里有多少孤儿类（service/util 未被绑定，阶段 4 模块识别的输入）；
- 应有源码却找不到的 / 歧义无法消歧的，如实列清单，绝不藏。

延续 report 包约定：dict 在前（供 --json / 后续入库），`render_*` 文本在后（给人看）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..bridge.linker import BridgeResult


def summary(result: BridgeResult) -> dict[str, Any]:
    """桥接结果的机器可读摘要（供 --json）。"""
    linked = result.linked
    by_name = result.linked_by_name
    external = result.external
    missing = result.missing
    ambiguous = result.ambiguous

    # 命中率以「期望有源码的 project 插件」为分母（外部/无ClassName 不计入）。
    project_total = len(linked) + len(by_name) + len(missing) + len(ambiguous)
    hit = len(linked) + len(by_name)
    hit_rate = (hit / project_total) if project_total else 0.0

    # 唯一类视角（同一类常被多个表单绑定，去重看真实覆盖面）。
    uniq_linked = {b.class_name for b in linked}
    uniq_missing = {b.class_name for b in missing}

    # 孤儿按角色：plugin=继承插件基类却未绑定（重要信号）、constant=常量类、unknown=真孤儿。
    orphan_by_role = dict(Counter(o.role for o in result.orphans))
    plugin_orphans = [o for o in result.orphans if o.role == "plugin"]
    # 插件孤儿按命中的基类聚合，便于一眼看清"哪些类型的插件没被绑定"。
    plugin_orphan_by_base = dict(Counter(o.plugin_base or "?" for o in plugin_orphans))

    return {
        "source_file_count": result.source_file_count,
        "source_type_count": result.source_type_count,
        "plugin_total": result.plugin_total,
        "project_plugin_total": project_total,
        "hit_count": hit,
        "hit_rate": round(hit_rate, 4),
        "status_counts": {
            "linked": len(linked),
            "linked_by_name": len(by_name),
            "external": len(external),
            "missing": len(missing),
            "ambiguous": len(ambiguous),
        },
        "unique_linked_classes": len(uniq_linked),
        "unique_missing_classes": len(uniq_missing),
        "orphan_count": len(result.orphans),
        "orphan_by_role": orphan_by_role,
        "plugin_orphan_count": len(plugin_orphans),
        "plugin_orphan_by_base": plugin_orphan_by_base,
        "plugin_orphans": [
            {"fqn": o.fqn, "relpath": o.relpath, "plugin_base": o.plugin_base}
            for o in plugin_orphans
        ],
        "plugin_type_counts": dict(Counter(b.plugin_type for b in result.bindings)),
        "code_prefixes": result.code_prefixes,
        "meta_prefixes": result.meta_prefixes,
        "missing": [
            {"class_name": b.class_name, "form_key": b.form_key,
             "plugin_type": b.plugin_type, "note": b.note}
            for b in missing
        ],
        "ambiguous": [
            {"class_name": b.class_name, "form_key": b.form_key,
             "candidates": b.candidates, "note": b.note}
            for b in ambiguous
        ],
        "linked_by_name": [
            {"class_name": b.class_name, "form_key": b.form_key,
             "source_relpath": b.source_relpath, "note": b.note}
            for b in by_name
        ],
        "orphans": [
            {"fqn": o.fqn, "relpath": o.relpath, "package": o.package, "role": o.role}
            for o in result.orphans
        ],
    }


def _fmt_counter(counts: dict[str, int], limit: int | None = None) -> str:
    if not counts:
        return "(无)"
    items = sorted(counts.items(), key=lambda kv: -kv[1])
    if limit:
        items = items[:limit]
    return ", ".join(f"{k}×{v}" for k, v in items)


def _trust_verdict(s: dict[str, Any]) -> str:
    """据命中率/缺失/歧义给一句信任结论。"""
    rate = s["hit_rate"]
    miss = s["status_counts"]["missing"]
    amb = s["status_counts"]["ambiguous"]
    if s["project_plugin_total"] == 0:
        return "⚠ 元数据里没有 project 插件可桥接（检查输入是否匹配该项目）。"
    if rate >= 0.95 and miss == 0 and amb == 0:
        return "✅ 桥接高度可信：project 插件几乎全部精确命中源码。"
    if rate >= 0.8:
        return f"🟡 桥接基本可信：命中 {rate:.0%}；{miss} 个未找到、{amb} 个歧义需关注。"
    return f"🔴 命中率偏低（{rate:.0%}）：多半是源码未给全或包路径与元数据不一致，请核对。"


def render(result: BridgeResult, *, max_list: int = 30) -> str:
    """桥接结果的人读报告。"""
    s = summary(result)
    sc = s["status_counts"]
    lines: list[str] = []

    lines.append("=" * 64)
    lines.append("元数据 ↔ 源码 桥接报告（阶段 3）")
    lines.append("-" * 64)
    lines.append(
        f"源码: {s['source_file_count']} 文件 / {s['source_type_count']} 顶层类型    "
        f"元数据插件: {s['plugin_total']} 条"
    )
    lines.append("")

    # 命中概览
    lines.append(
        f"project 插件: {s['project_plugin_total']} 条    "
        f"命中 {s['hit_count']} （{s['hit_rate']:.1%}）"
    )
    lines.append(
        f"  精确命中(linked)        : {sc['linked']}"
        f"   （唯一类 {s['unique_linked_classes']}）"
    )
    lines.append(f"  按类名命中(linked_by_name): {sc['linked_by_name']}")
    lines.append(f"  平台外部(external)      : {sc['external']}")
    lines.append(f"  未找到(missing)         : {sc['missing']}"
                 f"   （唯一类 {s['unique_missing_classes']}）")
    lines.append(f"  歧义(ambiguous)         : {sc['ambiguous']}")
    obr = s["orphan_by_role"]
    lines.append(
        f"  孤儿类(未被绑定)        : {s['orphan_count']}"
        f"   （插件类 {obr.get('plugin', 0)} / 常量类 {obr.get('constant', 0)}"
        f" / 真孤儿 {obr.get('unknown', 0)}）"
    )
    lines.append("")
    lines.append("插件按归属: " + _fmt_counter(s["plugin_type_counts"]))

    # 两套前缀（分别建、不混）
    lines.append("")
    lines.append("代码包前缀（管模块归属）: " + _fmt_counter(s["code_prefixes"], limit=12))
    lines.append("元数据标识前缀（管实体）: " + _fmt_counter(s["meta_prefixes"], limit=12))

    # 未找到清单（信任信号，重点列）
    if s["missing"]:
        lines.append("")
        lines.append(f"⚠ project 插件未找到源码 {len(s['missing'])} 条（前 {min(max_list, len(s['missing']))}）:")
        for m in s["missing"][:max_list]:
            lines.append(f"    - {m['class_name']}  [{m['plugin_type']}]  ← {m['form_key']}")
        if len(s["missing"]) > max_list:
            lines.append(f"    …… 另有 {len(s['missing']) - max_list} 条（--json 看全部）")

    # 歧义清单
    if s["ambiguous"]:
        lines.append("")
        lines.append(f"⚠ 类名歧义无法消歧 {len(s['ambiguous'])} 条（前 {min(max_list, len(s['ambiguous']))}）:")
        for a in s["ambiguous"][:max_list]:
            cands = ", ".join(a["candidates"][:4])
            more = " …" if len(a["candidates"]) > 4 else ""
            lines.append(f"    - {a['class_name']} ← {a['form_key']}  候选: {cands}{more}")

    # 按名命中（降级匹配，提示核对）
    if s["linked_by_name"]:
        lines.append("")
        lines.append(f"ℹ 按类名降级命中 {len(s['linked_by_name'])} 条（FQN 与源码包路径不一致，建议核对）:")
        for b in s["linked_by_name"][:max_list]:
            lines.append(f"    - {b['class_name']} → {b['source_relpath']}")

    # 插件孤儿：继承苍穹插件基类却无元数据绑定 —— 死代码/元数据未给全的重要信号，重点列。
    if s["plugin_orphans"]:
        lines.append("")
        n = min(max_list, len(s["plugin_orphans"]))
        lines.append(
            f"⚠ 继承插件基类却未被绑定 {len(s['plugin_orphans'])} 个（前 {n}）: "
            + _fmt_counter(s["plugin_orphan_by_base"], limit=8)
        )
        for o in s["plugin_orphans"][:max_list]:
            lines.append(f"    - {o['fqn']}  ({o['plugin_base']})")
        if len(s["plugin_orphans"]) > max_list:
            lines.append(f"    …… 另有 {len(s['plugin_orphans']) - max_list} 个（--json 看全部）")

    # 孤儿类样本（常量类标注 [const]，与真孤儿区分；常量类喂阶段6/9、不算阶段4风险）
    if result.orphans:
        lines.append("")
        lines.append(f"孤儿类样本（共 {len(result.orphans)}，前 {min(max_list, len(result.orphans))}）:")
        for o in result.orphans[:max_list]:
            tag = {"constant": " [const]", "plugin": " [plugin]"}.get(o.role, "")
            lines.append(f"    - {o.fqn}{tag}  ({o.relpath})")
        if len(result.orphans) > max_list:
            lines.append(f"    …… 另有 {len(result.orphans) - max_list} 个（--json 看全部）")

    lines.append("")
    lines.append(_trust_verdict(s))
    return "\n".join(lines)
