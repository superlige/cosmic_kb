"""阶段 2 · 元数据解析报告（分类计数 + 人读渲染）。

把单个 `MetaModel` 或整包 `PackageResult` 渲染成"接手者一眼能看懂"的报告：
表单是什么类型、有几个实体几层分录、字段按分类口径各多少、插件三类各多少、
操作 oid 回填情况。计数即可信度（见 CLAUDE.md「信任优先」）：哪类多少条一目了然，
解不出的 unknown 如实列出，不藏。文本给人看，dict 供 --json / 后续入库。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..metadata.model import MetaModel


def model_summary(model: MetaModel) -> dict[str, Any]:
    """单个 MetaModel 的分类计数摘要。"""
    kind_counts = Counter(f.kind for f in model.fields)
    level_counts = Counter(f.level for f in model.fields)
    type_counts = Counter(f.field_type for f in model.fields)
    plugin_type_counts = Counter(p.plugin_type for p in model.plugins)
    plugin_source_counts = Counter(p.source for p in model.plugins)
    op_resolved_counts = Counter(o.resolved_from for o in model.operations)

    entries = [e for e in model.entities if e.level == "entry"]
    subentries = [e for e in model.entities if e.level == "subentry"]

    return {
        "key": model.key,
        "name": model.name,
        "model_type": model.model_type,
        "form_type": model.form_type,
        "isv": model.isv,
        "inherit_path": model.inherit_path,
        "entity_count": len(model.entities),
        "entry_count": len(entries),
        "subentry_count": len(subentries),
        "field_total": len(model.fields),
        "field_by_kind": dict(kind_counts),
        "field_by_level": dict(level_counts),
        "field_by_type": dict(type_counts),
        "plugin_total": len(model.plugins),
        "plugin_by_type": dict(plugin_type_counts),
        "plugin_by_source": dict(plugin_source_counts),
        "operation_total": len(model.operations),
        "operation_by_resolved": dict(op_resolved_counts),
        "warning_count": len(model.warnings),
    }


def _fmt_counter(counts: dict[str, int]) -> str:
    if not counts:
        return "(无)"
    return ", ".join(f"{k}×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))


def render_model(model: MetaModel) -> str:
    """单个 MetaModel 的人读报告。"""
    s = model_summary(model)
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"表单: {s['name']} ({s['key']})")
    lines.append(f"类型: {s['model_type']} → {s['form_type']}    ISV: {s['isv']}")
    if s["inherit_path"]:
        lines.append(f"继承链: {' → '.join(s['inherit_path'])}")
    lines.append("-" * 60)

    # 实体/分录层级
    lines.append(
        f"实体  : {s['entity_count']} 个"
        f"（分录 {s['entry_count']}、子分录 {s['subentry_count']}）"
    )
    for e in model.entities:
        tag = {"header": "表头", "entry": "分录", "subentry": "子分录"}.get(e.level, e.level)
        lines.append(f"        [{tag}] {e.name or '?'} ({e.key or '?'})")

    # 字段分类
    lines.append("")
    lines.append(f"字段  : {s['field_total']} 个")
    lines.append(f"  按分类: {_fmt_counter(s['field_by_kind'])}")
    lines.append(f"  按层级: {_fmt_counter(s['field_by_level'])}")
    lines.append(f"  按类型: {_fmt_counter(s['field_by_type'])}")

    # 插件三类
    lines.append("")
    lines.append(f"插件  : {s['plugin_total']} 个")
    lines.append(f"  按归属: {_fmt_counter(s['plugin_by_type'])}")
    lines.append(f"  按来源: {_fmt_counter(s['plugin_by_source'])}")
    for p in model.plugins:
        bind = ""
        if p.plugin_type == "op":
            op = p.operation_name or p.operation_key or (
                f"oid={p.operation_oid}" if p.operation_oid else "unknown"
            )
            bind = f"  →绑定操作: {op}"
        lines.append(f"        [{p.plugin_type}/{p.source}] {p.class_name}{bind}")

    # 操作回填
    lines.append("")
    lines.append(f"操作  : {s['operation_total']} 个  回填来源: {_fmt_counter(s['operation_by_resolved'])}")

    # 存疑
    if model.warnings:
        lines.append("")
        lines.append(f"⚠ 存疑 {len(model.warnings)} 条:")
        for w in model.warnings[:20]:
            lines.append(f"    - {w}")

    return "\n".join(lines)


def render_package(result: Any, *, max_list: int = 50) -> str:
    """整包 PackageResult 的人读报告：清单 + 列出全部表单（验收第 4 条）。"""
    lines: list[str] = []
    total = len(result.entries)
    ok = len(result.ok_entries)
    failed = len(result.failed_entries)
    lines.append("=" * 60)
    lines.append(f"整包: {result.source.name}")
    if result.manifest:
        man = result.manifest
        bits = []
        if man.get("isv"):
            bits.append(f"isv={man['isv']}")
        if man.get("product"):
            bits.append(f"product={man['product']}")
        if man.get("apps"):
            bits.append(f"apps={len(man['apps'])}")
        if bits:
            lines.append("清单: " + "  ".join(bits))
    lines.append("-" * 60)
    lines.append(f"表单总数: {total}    解析成功: {ok}    失败: {failed}")

    # 按 form_type / appKey 分布
    ft = Counter(e.model.form_type for e in result.ok_entries)
    apps = Counter(e.app_key for e in result.ok_entries)
    lines.append(f"按类型: {_fmt_counter(dict(ft))}")
    lines.append(f"按应用(appKey): {_fmt_counter({k or '?': v for k, v in apps.items()})}")

    # 转换规则（单据上下游关系）单列——它不是表单，混进表单清单会误导。
    converts = [e for e in result.ok_entries if e.model.form_type == "convert"]
    forms = [e for e in result.ok_entries if e.model.form_type != "convert"]

    # 列出全部表单（量大时截断，但给出总数）。
    lines.append("")
    lines.append(f"表单清单（前 {min(max_list, len(forms))} / 共 {len(forms)}）:")
    for e in forms[:max_list]:
        m = e.model
        lines.append(
            f"  {m.key or '?':<32} {m.name or '?':<20} "
            f"[{m.form_type}] 字段{len(m.fields)} 插件{len(m.plugins)}"
        )
    if len(forms) > max_list:
        lines.append(f"  …… 另有 {len(forms) - max_list} 个（用 --json 查看全部）")

    if converts:
        lines.append("")
        lines.append(_render_convert_block(converts, max_list=max_list))

    if failed:
        lines.append("")
        lines.append(f"⚠ 解析失败 {failed} 个:")
        for e in result.failed_entries[:20]:
            lines.append(f"    - {e.member}: {e.error}")

    return "\n".join(lines)


def _render_convert_block(converts: list, *, max_list: int = 50) -> str:
    """转换规则（单据上下游关系）清单：源单据 → 目标单据 [转换插件数]。"""
    lines: list[str] = []
    n = len(converts)
    with_plugin = sum(1 for e in converts if e.model.plugins)
    lines.append(f"转换规则: {n} 条（含转换插件 {with_plugin} 条），单据上下游（前 {min(max_list, n)}）:")
    for e in converts[:max_list]:
        c = e.model.convert
        src = (c.source_entity if c else None) or "?"
        tgt = (c.target_entity if c else None) or "?"
        pn = len(e.model.plugins)
        ptag = f"  插件{pn}" if pn else ""
        lines.append(f"  {src} → {tgt}  «{e.model.name or '?'}»{ptag}")
    if n > max_list:
        lines.append(f"  …… 另有 {n - max_list} 条（用 --json 查看全部）")
    return "\n".join(lines)


def render_multi_package(multi: Any, *, max_list: int = 50) -> str:
    """多包 MultiPackageResult 的人读报告：全项目汇总 + 按模块(appKey)分布 + 各包明细。"""
    lines: list[str] = []
    total = multi.total_forms
    ok = multi.ok_count
    failed = multi.failed_count
    lines.append("=" * 60)
    lines.append(f"多包汇总: {len(multi.packages)} 个 zip")
    lines.append("-" * 60)
    lines.append(f"表单总数: {total}    解析成功: {ok}    失败: {failed}")

    # 跨包聚合：按类型 / 按模块(appKey)
    ft: Counter = Counter()
    apps: Counter = Counter()
    for pkg in multi.packages:
        for e in pkg.ok_entries:
            ft[e.model.form_type] += 1
            apps[e.app_key or "?"] += 1
    lines.append(f"按类型: {_fmt_counter(dict(ft))}")
    lines.append(f"按模块(appKey): {_fmt_counter(dict(apps))}")

    # 各包明细（一个包≈一个业务模块）
    lines.append("")
    lines.append("各包明细:")
    for pkg in multi.packages:
        pok = len(pkg.ok_entries)
        pfail = len(pkg.failed_entries)
        pft = Counter(e.model.form_type for e in pkg.ok_entries)
        papp = sorted({e.app_key for e in pkg.ok_entries if e.app_key})
        appstr = ", ".join(papp) if papp else "?"
        lines.append(f"  ▸ {pkg.source.name}")
        if pkg.manifest.get("error"):
            lines.append(f"      ✗ 整包打开失败: {pkg.manifest['error']}")
            continue
        lines.append(f"      模块(appKey): {appstr}    表单 {pok}    失败 {pfail}")
        lines.append(f"      类型: {_fmt_counter(dict(pft))}")

    # 失败明细（跨包汇总）
    all_failed = [
        (pkg.source.name, e) for pkg in multi.packages for e in pkg.failed_entries
    ]
    if all_failed:
        lines.append("")
        lines.append(f"⚠ 解析失败 {len(all_failed)} 个:")
        for name, e in all_failed[:max_list]:
            lines.append(f"    - [{name}] {e.member}: {e.error}")

    return "\n".join(lines)


def multi_package_summary(multi: Any) -> dict[str, Any]:
    """多包 dict 摘要（供 --json）：全项目聚合 + 每包完整清单（复用 package_summary）。"""
    ft: Counter = Counter()
    apps: Counter = Counter()
    for pkg in multi.packages:
        for e in pkg.ok_entries:
            ft[e.model.form_type] += 1
            apps[e.app_key or "?"] += 1
    return {
        "package_count": len(multi.packages),
        "total": multi.total_forms,
        "ok": multi.ok_count,
        "failed": multi.failed_count,
        "by_form_type": dict(ft),
        "by_app_key": dict(apps),
        "packages": [package_summary(pkg) for pkg in multi.packages],
    }


def package_summary(result: Any) -> dict[str, Any]:
    """整包 dict 摘要（供 --json）。含全部表单清单。"""
    return {
        "source": str(result.source),
        "manifest": result.manifest,
        "total": len(result.entries),
        "ok": len(result.ok_entries),
        "failed": len(result.failed_entries),
        "forms": [
            {
                "key": e.model.key,
                "name": e.model.name,
                "form_type": e.model.form_type,
                "app_key": e.app_key,
                "field_total": len(e.model.fields),
                "plugin_total": len(e.model.plugins),
                "entity_count": len(e.model.entities),
                **({"convert": e.model.convert.to_dict()} if e.model.convert else {}),
            }
            for e in result.ok_entries
        ],
        "errors": [
            {"member": e.member, "error": e.error} for e in result.failed_entries
        ],
    }
