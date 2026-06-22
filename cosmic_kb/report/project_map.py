"""阶段 4 · 项目地图（多信号模块识别）。

接手陌生苍穹老项目，第一需求是"这项目长什么样、有哪几个业务模块"。但**不能**靠
"代码包路径的某个层级"硬切模块——野生历史项目的包路径常按开发者拉，不按业务模块
（A 全放 `cqspb.assets`、B 塞 `cqspb.zhangsan`、C 用 `com.company.xxx`），任何单一
全局层级都会给出**自信但错**的划分，违背"信任优先 + 宁可标 unknown 也不臆造"。

故本模块用**多信号 + 置信度**（用户 2026-06-16 拍板）：

  主锚 = 元数据 appKey  ── 平台在应用级赋的标识，不受开发者包风格影响（阶段 2 已证明
                          能干净分组）。表单按 appKey 归模块；其**桥接命中的源码类**继承该模块。
  辅证 = 代码包前缀     ── 仅用于①把无绑定的真孤儿类归到模块，②交叉校验。绝不作主切分依据。
  置信度 = 包结构一致度 ── 模块的绑定类是否住在"单一模块独占"的包里；低则说明代码按开发者
                          散落、与应用划分对不上 → 如实降级、摆"包结构健康度"证据让接手者判断。

孤儿归类（决策 #2）：真孤儿仅当其包前缀命中"某模块独占的包"才归入，否则进 `未归类` 桶
交人判断，不强凑。

延续 report 包约定：dict 在前（供 --json / 入库），`render_*` 文本在后（给人看）。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any, Iterable

from ..bridge import namespace

if TYPE_CHECKING:
    from ..bridge.linker import BridgeResult
    from ..bridge.namespace import SourceIndex
    from ..ingest.scanner import ScanResult
    from ..metadata.model import MetaModel

# 特殊桶（非真实 appKey 模块）。
MOD_UNKNOWN = "unknown"        # 表单无 appKey（多为单 dym 输入）
MOD_UNCLASSIFIED = "未归类"    # 孤儿类包前缀与任何模块独占包都对不上

_DEFAULT_PKG = "(default)"     # 默认包（无 package 声明）


def _pkg_prefixes(package: str) -> list[str]:
    """一个包名的所有前缀（由短到长）：a.b.c → [a, a.b, a.b.c]。"""
    segs = package.split(".")
    return [".".join(segs[:i]) for i in range(1, len(segs) + 1)]


def _assign_orphan(package: str | None, owned_prefix: dict[str, str]) -> str | None:
    """孤儿归类：从孤儿包名由长到短取前缀，命中"某模块独占的包前缀"即归入。

    owned_prefix：{包前缀 → 独占它的模块}。关键是**独占**——一个前缀只有当它仅出现在
    单一模块的绑定类里才算独占。这样既能把 `cqkd.am.assets.botp.X` 归到 assets（因
    `cqkd.am.assets` 为 assets 独有），又不会把跨模块共享的包（如 `cqspb.common`，你担心的
    多开发者散落场景）硬塞——共享前缀映射到多模块、不独占、留未归类。取**最长**匹配最稳妥。
    """
    if not package:
        return None
    for prefix in reversed(_pkg_prefixes(package)):  # 由长到短，取最具体的独占前缀
        if prefix in owned_prefix:
            return owned_prefix[prefix]
    return None


def module_map(
    scan_result: "ScanResult",
    models: Iterable["MetaModel"],
    bridge_result: "BridgeResult",
    *,
    index: "SourceIndex | None" = None,
) -> dict[str, Any]:
    """多信号模块识别。返回 dict（供 --json / 入库 / render_map）。

    index：可复用 build 流程已建好的源码索引，避免重复解析（None 则自建）。
    """
    if index is None:
        index = namespace.build_index(scan_result)
    models = list(models)

    # ── 1) appKey 锚定：表单 → 模块（转换规则单独归集，不当表单计数）──────────
    form_module: dict[str, str] = {}
    module_forms: dict[str, list] = defaultdict(list)
    module_converts: dict[str, list] = defaultdict(list)
    for m in models:
        mod = m.app_key or MOD_UNKNOWN
        if m.key:
            form_module[m.key] = mod  # 转换规则也登记，供桥接/入库按 key 找模块
        if m.form_type == "convert":
            module_converts[mod].append(m)
        else:
            module_forms[mod].append(m)

    # ── 2) 绑定命中的源码文件 → 模块（按绑定它的表单；多表单取多数）────────
    relpath_votes: dict[str, list[str]] = defaultdict(list)
    for b in bridge_result.bindings:
        if b.source_relpath and b.form_key in form_module:
            relpath_votes[b.source_relpath].append(form_module[b.form_key])
    relpath_module = {
        rp: Counter(v).most_common(1)[0][0] for rp, v in relpath_votes.items()
    }

    # ── 3) 包 → 模块集合（仅用绑定类），判定"单一模块独占的包"────────────
    unit_by_relpath = {u.relpath: u for u in index.units}
    pkg_modules: dict[str, set[str]] = defaultdict(set)         # 完整包 → 模块集（散落诊断 + 一致度）
    prefix_modules: dict[str, set[str]] = defaultdict(set)      # 全部包前缀 → 模块集（孤儿归类）
    bound_class_module: dict[str, str] = {}     # 源码 fqn → 模块
    module_pkg_counter: dict[str, Counter] = defaultdict(Counter)
    module_owned_hits: dict[str, int] = defaultdict(int)
    module_bound_total: dict[str, int] = defaultdict(int)

    for rp, mod in relpath_module.items():
        u = unit_by_relpath.get(rp)
        if u is None:
            continue
        if u.package:
            pkg_modules[u.package].add(mod)
            for prefix in _pkg_prefixes(u.package):
                prefix_modules[prefix].add(mod)
        for fqn in u.all_fqns:
            bound_class_module[fqn] = mod
        module_pkg_counter[mod][u.package or _DEFAULT_PKG] += 1
        module_bound_total[mod] += 1

    # 独占前缀：仅出现在单一模块绑定类里的包前缀，作孤儿归类依据（最长匹配）。
    owned_prefix = {p: next(iter(mods)) for p, mods in prefix_modules.items() if len(mods) == 1}
    # 散落包：同一完整包被多模块共用（你担心的多开发者混放场景）—— 进健康度诊断。
    scattered_packages = sorted(pkg for pkg, mods in pkg_modules.items() if len(mods) > 1)
    # 一致度命中：绑定类住在"未被多模块共用"的完整包里的比例（共享包拉低一致度）。
    for rp, mod in relpath_module.items():
        u = unit_by_relpath.get(rp)
        if u is not None and u.package and len(pkg_modules[u.package]) == 1:
            module_owned_hits[mod] += 1

    # ── 4) 孤儿归类：真孤儿仅在命中独占包时归入，否则未归类 ───────────────
    class_module: dict[str, str] = dict(bound_class_module)
    unclassified: list[dict[str, Any]] = []
    orphan_real_by_module: Counter = Counter()
    const_by_module: Counter = Counter()
    for o in bridge_result.orphans:
        mod = _assign_orphan(o.package, owned_prefix)
        target = mod if mod is not None else MOD_UNCLASSIFIED
        class_module[o.fqn] = target
        if o.role == "unknown":          # 真孤儿（阶段 4 风险关注）
            if mod is None:
                unclassified.append(
                    {"fqn": o.fqn, "relpath": o.relpath, "package": o.package}
                )
            else:
                orphan_real_by_module[mod] += 1
        else:                            # 常量类：不计风险，单独计数
            if mod is not None:
                const_by_module[mod] += 1

    # ── 5) 汇总每模块统计 ─────────────────────────────────────────────────
    class_count_by_module: Counter = Counter(class_module.values())
    modules: list[dict[str, Any]] = []
    all_module_names = set(module_forms) | set(module_converts) | set(class_count_by_module)
    all_module_names.discard(MOD_UNCLASSIFIED)  # 未归类单独成桶，最后补

    for name in all_module_names:
        forms = module_forms.get(name, [])
        converts = module_converts.get(name, [])
        entity_count = sum(len(m.entities) for m in forms)
        # 插件数含转换规则绑定的转换插件（它们也是模块的插件实现）。
        plugin_count = sum(len(m.plugins) for m in forms) + sum(len(m.plugins) for m in converts)
        bound_total = module_bound_total.get(name, 0)
        consistency = (
            round(module_owned_hits.get(name, 0) / bound_total, 4)
            if bound_total else None
        )
        dominant = (
            module_pkg_counter[name].most_common(1)[0][0]
            if module_pkg_counter.get(name) else None
        )
        # 置信度：有源码命中→取包结构一致度；无命中→仅元数据佐证，置 None 并注明。
        if bound_total:
            confidence = consistency
            evidence = f"appKey={name}；主导包 {dominant}；{bound_total} 类绑定命中"
        else:
            confidence = None
            evidence = f"appKey={name}；无源码绑定命中（仅元数据佐证）"
        modules.append({
            "name": name,
            "app_key": None if name == MOD_UNKNOWN else name,
            "dominant_package": dominant,
            "pkg_consistency": consistency,
            "form_count": len(forms),
            "convert_count": len(converts),
            "entity_count": entity_count,
            "plugin_count": plugin_count,
            "class_count": class_count_by_module.get(name, 0),
            "orphan_real_count": orphan_real_by_module.get(name, 0),
            "const_count": const_by_module.get(name, 0),
            "confidence": confidence,
            "evidence": evidence,
        })

    # 排序：真实模块按源码类数降序，特殊桶 unknown 沉底。
    modules.sort(key=lambda d: (d["name"] == MOD_UNKNOWN, -d["class_count"], d["name"]))

    # 未归类桶（只有孤儿、无表单/无独占包）。
    if class_count_by_module.get(MOD_UNCLASSIFIED):
        modules.append({
            "name": MOD_UNCLASSIFIED,
            "app_key": None,
            "dominant_package": None,
            "pkg_consistency": None,
            "form_count": 0,
            "convert_count": 0,
            "entity_count": 0,
            "plugin_count": 0,
            "class_count": class_count_by_module.get(MOD_UNCLASSIFIED, 0),
            "orphan_real_count": len(unclassified),
            "const_count": 0,
            "confidence": None,
            "evidence": "孤儿类包前缀与任何模块独占包都不一致，待人工判断归属",
        })

    # ── 6) 包结构健康度（让接手者判断"分得对不对"的依据）──────────────────
    total_bound = sum(module_bound_total.values())
    total_owned = sum(module_owned_hits.values())
    overall = round(total_owned / total_bound, 4) if total_bound else None
    real_modules = [m for m in modules if m["app_key"]]
    health = {
        "overall_consistency": overall,
        "module_count": len(real_modules),
        "unclassified_count": len(unclassified),
        "scattered_package_count": len(scattered_packages),
        "scattered_packages": scattered_packages,
        "verdict": _health_verdict(overall, len(scattered_packages), len(unclassified)),
    }

    return {
        "modules": modules,
        "health": health,
        "form_module": form_module,
        "class_module": class_module,
        "unclassified": unclassified,
    }


def load_map(conn) -> dict[str, Any]:
    """从 KB 重建 render_map 所需的地图 dict（report 读 DB 路径用；不含庞大的 class_module）。"""
    import json

    from ..graph import store

    modules = [dict(r) for r in conn.execute(
        "SELECT name,app_key,dominant_package,pkg_consistency,form_count,entity_count,"
        "plugin_count,class_count,orphan_real_count,confidence,evidence FROM module"
    ).fetchall()]
    # 转换规则数按模块从 convert_rule 表回填（module 表未存此列，单独聚合）。
    convert_by_module = {
        r["module"]: r["n"] for r in conn.execute(
            "SELECT module, COUNT(*) n FROM convert_rule GROUP BY module")
    }
    for m in modules:
        m["convert_count"] = convert_by_module.get(m["name"], 0)
    # 排序复刻 module_map：真实模块按类数降序、unknown 沉底、未归类垫底。
    modules.sort(key=lambda d: (
        d["name"] == MOD_UNCLASSIFIED, d["name"] == MOD_UNKNOWN,
        -(d["class_count"] or 0), d["name"]))
    health = json.loads(store.get_meta(conn, "health") or "{}")
    unclassified = [
        {"fqn": r["fqn"], "relpath": r["relpath"], "package": r["package"]}
        for r in conn.execute(
            "SELECT fqn,relpath,package FROM source_class "
            "WHERE module=? AND is_orphan=1 AND orphan_role='unknown' ORDER BY fqn",
            (MOD_UNCLASSIFIED,),
        ).fetchall()
    ]
    return {"modules": modules, "health": health, "unclassified": unclassified}


def _health_verdict(overall: float | None, scattered: int, unclassified: int) -> str:
    """据包结构一致度给一句"模块划分可信吗"的结论。"""
    if overall is None:
        return "⚠ 无源码绑定命中，模块仅按 appKey 列出，未能用代码包交叉校验。"
    if overall >= 0.9 and scattered == 0:
        return "✅ 包结构清晰、与应用划分一致，模块划分高度可信。"
    if overall >= 0.6:
        return (
            f"🟡 模块划分基本可信（包结构一致度 {overall:.0%}）："
            f"{scattered} 个包跨模块、{unclassified} 个真孤儿未归类，见下方诊断。"
        )
    return (
        f"🔴 包结构按开发者拉、与应用划分对不上（一致度 {overall:.0%}）："
        "模块划分仅供参考，请结合下方散落包/未归类清单人工核对。"
    )


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.0%}"
    return str(v)


def render_map(mm: dict[str, Any], *, max_list: int = 30) -> str:
    """项目地图人读报告：模块清单 + 包结构健康度诊断。"""
    h = mm["health"]
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("项目地图（阶段 4 · 多信号模块识别）")
    lines.append("-" * 70)
    lines.append(
        f"业务模块: {h['module_count']} 个    "
        f"包结构一致度: {_fmt(h['overall_consistency'])}    "
        f"未归类真孤儿: {h['unclassified_count']}    "
        f"跨模块散落包: {h['scattered_package_count']}"
    )
    lines.append("")
    lines.append("模块清单（按源码类数降序）:")
    header = (
        f"  {'模块(appKey)':<22}{'表单':>5}{'转换':>5}{'实体':>5}{'插件':>5}"
        f"{'源码类':>7}{'真孤儿':>7}{'一致度':>8}"
    )
    lines.append(header)
    for m in mm["modules"]:
        lines.append(
            f"  {(m['name'] or '?'):<22}{m['form_count']:>5}{m.get('convert_count', 0):>5}"
            f"{m['entity_count']:>5}{m['plugin_count']:>5}{m['class_count']:>7}"
            f"{m['orphan_real_count']:>7}{_fmt(m['pkg_consistency']):>8}"
        )
        if m["dominant_package"]:
            lines.append(f"      主导包: {m['dominant_package']}")

    # 包结构健康度诊断
    lines.append("")
    lines.append("包结构健康度诊断:")
    lines.append(f"  {h['verdict']}")
    if h["scattered_packages"]:
        lines.append("")
        n = min(max_list, len(h["scattered_packages"]))
        lines.append(f"  跨模块散落包 {len(h['scattered_packages'])} 个（前 {n}，同一包被多模块共用）:")
        for pkg in h["scattered_packages"][:max_list]:
            lines.append(f"    - {pkg}")
        if len(h["scattered_packages"]) > max_list:
            lines.append(f"    …… 另有 {len(h['scattered_packages']) - max_list} 个（--json 看全部）")
    if mm["unclassified"]:
        lines.append("")
        n = min(max_list, len(mm["unclassified"]))
        lines.append(f"  未归类真孤儿 {len(mm['unclassified'])} 个（前 {n}，包前缀与任何模块独占包都对不上）:")
        for o in mm["unclassified"][:max_list]:
            lines.append(f"    - {o['fqn']}  ({o['relpath']})")
        if len(mm["unclassified"]) > max_list:
            lines.append(f"    …… 另有 {len(mm['unclassified']) - max_list} 个（--json 看全部）")

    return "\n".join(lines)
