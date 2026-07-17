"""触发点 → 插件事件入口回溯（`trace --kind operation` 的 `entry_chains` 段）。

苍穹项目里所有程序化调用最终由插件事件入口拉起（操作/表单/列表/任务/工作流插件的生命
周期方法由平台派发）。`operation_trigger` 只存单跳原子事实（谁的哪一行触发了操作），当
触发点埋在 service/helper 深处时，单看 `caller_class.caller_method` 不知道这条链最终从
哪个插件事件开始。本模块在查询期沿 `call_edge` 向上反向 BFS，把每个触发点回溯到插件
事件入口，输出「入口 → … → 触发点」的最短调用链（KB 仍只存单跳事实，多跳链查询期拼装）。

诚实纪律（红线 #4）：
  * 入口判定只认证据：`plugin_method` 事件行（事件表命中）= 已确认入口；调用链到顶的类
    是插件类（元数据绑定或孤儿插件基类命中）但方法不在事件表 = 插件类边界（likely，事件
    表未覆盖的插件种类如 task/workflow 常见此形态）。不凭方法名"像事件"猜入口。
  * 向上追不到静态调用方 ≠ 没有入口——反射、定时任务注册、OpenAPI、脚本等动态派发追不
    到，terminal 标 `no_static_caller`，留给 agent 读源码定性，不得解释成死代码或无入口。
  * 链上任何一条调用边是 heuristic 解析则整链降级 likely；BFS 有深度/节点/链数上限，
    截断如实标注（`chains_truncated` / `search_truncated`），不以局部结果冒充完整。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from ..java import plugin_classifier

_MAX_DEPTH = 10       # 向上回溯最大层数（超出标 depth_capped，不静默丢）
_MAX_CHAINS = 6       # 每个触发点最多带回的链数（入口链优先，超出计入 chains_truncated）
_MAX_NODES = 400      # BFS 访问节点上限（防病态调用图爆炸；触发即 search_truncated）

# terminal 呈现顺序：能证明入口的排前，追不到的排后（agent 先看最有用的链）。
_TERMINAL_RANK = {"entry": 0, "plugin_boundary": 1, "no_static_caller": 2,
                  "depth_capped": 3, "callers_all_visited": 4}


def _entry_info(conn, cls: str, method: str) -> dict[str, Any] | None:
    """插件事件入口证据：`plugin_method` 的非 helper 事件行；无则 None（不猜）。"""
    row = conn.execute(
        "SELECT event_kind, event_phase FROM plugin_method "
        "WHERE plugin_fqn=? AND method_name=? AND event_kind<>'helper'",
        (cls, method)).fetchone()
    if row is None:
        return None
    return {"class": cls, "method": method,
            "event": row["event_kind"], "phase": row["event_phase"],
            "bindings": _bindings(conn, cls)}


def _bindings(conn, cls: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT DISTINCT form_key, plugin_type, operation_key, enabled FROM plugin "
        "WHERE class_name=? ORDER BY form_key, plugin_type", (cls,)).fetchall()]


def _boundary_info(conn, cls: str) -> dict[str, Any] | None:
    """调用链到顶的类是否是插件类：元数据绑定（plugin 表）或孤儿插件基类命中
    （source_class.orphan_role='plugin'）。是 → likely 入口边界；否 → None。"""
    binds = _bindings(conn, cls)
    if binds:
        return {"class": cls, "bindings": binds, "basis": "metadata_binding"}
    row = conn.execute(
        "SELECT plugin_base FROM source_class WHERE fqn=? AND orphan_role='plugin'",
        (cls,)).fetchone()
    if row is not None:
        return {"class": cls, "plugin_base": row["plugin_base"], "basis": "plugin_base"}
    return None


# ── 注册状态反查（callers 入口可达性/死代码判定的落点）──────────────────────
#
# KB 有注册表（可确定是否注册+启用）的插件种类；调度计划(task)/开放平台(webapi)/工作流
# (workflow) 等 KB 未接入配置表，如实报 orphan_unverifiable，绝不臆造已注册或已启用。
_KB_REGISTRY_KINDS = frozenset({"form", "list", "op", "writeback", "convert"})

_STATUS_NOTE = {
    "registered_disabled": "有效绑定全部已禁用（不排除反射或运行时动态注册的其他入口）",
    "registered_enabled_unknown": "绑定的启用状态未知（NULL），无法确认是否启用",
    "orphan_unverifiable": "KB 未接入该类插件的注册配置表（如调度计划/开放平台/工作流），"
                           "无法确定是否平台注册",
    "no_registration_evidence": "既非元数据绑定插件，也非孤儿插件基类命中，无法判定注册状态",
}


def registration_status(conn, cls: str) -> dict[str, Any]:
    """单个插件类的注册状态反查：有元数据绑定 → 三态 enabled（convert 走规则级 enabled）；
    无绑定的孤儿插件类 → 按种类分「KB 未注册」与「KB 未接入该注册表，无法确定」两档；
    两者都不是 → 无证据兜底。死代码判定的落点：只有 registered_enabled 才谈得上"确认可达"。
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT form_key, plugin_type, operation_key, enabled FROM plugin "
        "WHERE class_name=? ORDER BY form_key, plugin_type", (cls,)).fetchall()]
    if rows:
        form_keys = sorted({r["form_key"] for r in rows if r["form_key"]})
        form_names: dict[str, str] = {}
        if form_keys:
            ph = ",".join("?" * len(form_keys))
            form_names = {r["key"]: r["name"] for r in conn.execute(
                f"SELECT key, name FROM form WHERE key IN ({ph})", form_keys).fetchall()}
        bindings: list[dict[str, Any]] = []
        effective: list[int | None] = []
        for r in rows:
            if r["plugin_type"] == "convert":
                rule = conn.execute(
                    "SELECT id, name, source_entity, target_entity, enabled "
                    "FROM convert_rule WHERE id=?", (r["form_key"],)).fetchone()
                if rule is None:
                    bindings.append({
                        "plugin_type": "convert", "rule_id": r["form_key"], "rule_name": None,
                        "source_entity": None, "target_entity": None,
                        "enabled": None, "enabled_source": "convert_rule",
                        "note": "转换规则悬空（convert_rule 未找到该 id），无法判定启停",
                    })
                    effective.append(None)
                else:
                    bindings.append({
                        "plugin_type": "convert", "rule_id": rule["id"],
                        "rule_name": rule["name"], "source_entity": rule["source_entity"],
                        "target_entity": rule["target_entity"], "enabled": rule["enabled"],
                        "enabled_source": "convert_rule",
                    })
                    effective.append(rule["enabled"])
            else:
                bindings.append({
                    "plugin_type": r["plugin_type"], "form_key": r["form_key"],
                    "form_name": form_names.get(r["form_key"]),
                    "operation_key": r["operation_key"], "enabled": r["enabled"],
                    "enabled_source": "plugin",
                })
                effective.append(r["enabled"])
        if any(e == 1 for e in effective):
            status = "registered_enabled"
        elif effective and all(e == 0 for e in effective):
            status = "registered_disabled"
        else:
            status = "registered_enabled_unknown"
        kind = plugin_classifier.plugin_kind(rows[0]["plugin_type"], None)[0]
        return {"class": cls, "status": status, "basis": "metadata_binding", "kind": kind,
                "bindings": bindings, "note": _STATUS_NOTE.get(status)}

    orow = conn.execute(
        "SELECT plugin_base FROM source_class WHERE fqn=? AND orphan_role='plugin'",
        (cls,)).fetchone()
    if orow is not None:
        kind = plugin_classifier.plugin_kind(None, orow["plugin_base"])[0]
        status = "orphan_unregistered" if kind in _KB_REGISTRY_KINDS else "orphan_unverifiable"
        note = (_STATUS_NOTE["orphan_unverifiable"] if status == "orphan_unverifiable" else
                f"孤儿插件类（{kind}）：KB 有该类注册表但查无绑定，元数据范围内未注册"
                "（不排除动态注册）")
        return {"class": cls, "status": status, "basis": "plugin_base", "kind": kind,
                "plugin_base": orow["plugin_base"], "bindings": [], "note": note}

    return {"class": cls, "status": "no_registration_evidence", "basis": "none", "kind": None,
            "bindings": [], "note": _STATUS_NOTE["no_registration_evidence"]}


def entry_chains(conn, cls: str, method: str, *, max_depth: int = _MAX_DEPTH,
                 max_chains: int = _MAX_CHAINS, max_nodes: int = _MAX_NODES,
                 preferred_entries: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    """从 (cls, method) 沿 call_edge 向上反向 BFS，回溯到插件事件入口。

    返回：
      status  self_entry（触发点本身就是事件入口）/ reached（至少一条链到达入口）/
              boundary_only（只到插件类边界）/ not_found（一条也证不到）
      chains  每条 {terminal, confidence, entry?, hops}；hops 自入口向下排到触发点，
              每跳的 call_line/call_relpath/call_resolution 是**该跳调用下一跳**的调用边证据。
    """
    start = (cls, method)
    self_entry = _entry_info(conn, cls, method)
    if self_entry is not None:
        return {"status": "self_entry", "entry": self_entry, "chains": []}

    # BFS 回指：child_of[父节点] = (子节点, 父调用子的最优调用边)；最短路径天然成立。
    child_of: dict[tuple[str, str], tuple[tuple[str, str], dict[str, Any]]] = {}
    depth: dict[tuple[str, str], int] = {start: 0}
    terminals: list[tuple[tuple[str, str], str, dict[str, Any] | None]] = []
    queue: deque[tuple[str, str]] = deque([start])
    search_truncated = False

    while queue:
        node = queue.popleft()
        if node != start:
            info = _entry_info(conn, *node)
            if info is not None:
                terminals.append((node, "entry", info))
                continue                      # 入口即链顶，不再向上（平台派发之上无静态边）
        if depth[node] >= max_depth:
            terminals.append((node, "depth_capped", None))
            continue
        # 同一 (caller, callee) 多个调用点取置信度最高的一条作代表边
        # （SQLite bare-column + MAX 语义：其余列取命中 MAX 的那行）。
        parents = [r for r in conn.execute(
            "SELECT caller_fqn, caller_method, line, source_relpath, resolution, "
            "MAX(confidence) AS confidence FROM call_edge "
            "WHERE target_fqn=? AND target_method=? AND caller_fqn IS NOT NULL "
            "GROUP BY caller_fqn, caller_method ORDER BY confidence DESC, caller_fqn",
            node).fetchall()
            if (r["caller_fqn"], r["caller_method"]) != node]   # 自递归不算上游
        if not parents:
            boundary = _boundary_info(conn, node[0])
            terminals.append((node, "plugin_boundary", boundary) if boundary
                             else (node, "no_static_caller", None))
            continue
        fresh = [(p["caller_fqn"], p["caller_method"], p) for p in parents
                 if (p["caller_fqn"], p["caller_method"]) not in depth]
        if not fresh:
            # 上游全部已访问：环，或汇入已探索的更短路径——如实终止本支。
            terminals.append((node, "callers_all_visited", None))
            continue
        for pfqn, pmethod, p in fresh:
            if len(depth) >= max_nodes:
                search_truncated = True
                break
            pn = (pfqn, pmethod)
            depth[pn] = depth[node] + 1
            child_of[pn] = (node, {"call_line": p["line"],
                                   "call_relpath": p["source_relpath"],
                                   "call_resolution": p["resolution"]})
            queue.append(pn)

    preferred_entries = preferred_entries or set()
    terminals.sort(key=lambda t: (
        _TERMINAL_RANK[t[1]],
        0 if t[1] == "entry" and t[0] in preferred_entries else 1,
        t[0],
    ))
    chains = [_build_chain(node, kind, extra, child_of)
              for node, kind, extra in terminals[:max_chains]]
    reached = any(c["terminal"] == "entry" for c in chains)
    boundary = any(c["terminal"] == "plugin_boundary" for c in chains)
    return {
        "status": "reached" if reached else ("boundary_only" if boundary else "not_found"),
        "chains": chains,
        "chains_truncated": max(0, len(terminals) - max_chains),
        "search_truncated": search_truncated,
    }


def _build_chain(node: tuple[str, str], terminal: str, extra: dict[str, Any] | None,
                 child_of: dict) -> dict[str, Any]:
    hops: list[dict[str, Any]] = []
    likely_edge = False
    cur = node
    while True:
        hop: dict[str, Any] = {"class": cur[0], "method": cur[1]}
        step = child_of.get(cur)
        if step is None:
            hops.append(hop)
            break
        child, edge = step
        hop.update(edge)
        if edge["call_resolution"] not in ("expr", "scope"):
            likely_edge = True
        hops.append(hop)
        cur = child
    if terminal == "entry":
        confidence = "likely" if likely_edge else "confirmed"
    elif terminal == "plugin_boundary":
        confidence = "likely"
    else:
        confidence = "unknown"                # 没证到入口，链只说明「向上追到这里断了」
    chain: dict[str, Any] = {"terminal": terminal, "confidence": confidence, "hops": hops}
    if extra is not None:
        chain["entry"] = extra
    return chain


# ── 紧凑投影（op_trace 的 MCP slim 用）───────────────────────────────────────

def slim_chains(ec: dict[str, Any], *, max_chains: int = 3, max_hops: int = 10,
                max_text: int = 512) -> dict[str, Any]:
    """MCP 紧凑形状：hops 压成 `类#方法@文件:行` 字符串路径。

    字段 trace 会把本结构放进按方法去重的目录。这里仍给链数、跳数和单字符串设硬界，
    避免病态包名/路径让单个目录项击穿 host 预算；所有裁剪均带显式计数。
    """
    if ec["status"] == "self_entry":
        e = ec["entry"]
        return {"status": "self_entry",
                "entry": {"class": _clip(e["class"], max_text),
                          "method": _clip(e["method"], max_text), "event": e["event"],
                          "phase": e["phase"],
                          "forms": [_clip(x, max_text) for x in sorted({
                              b["form_key"] for b in e["bindings"] if b["form_key"]})]}}
    chains = []
    for c in ec["chains"][:max_chains]:
        hops = c["hops"]
        shown_hops = hops[:max_hops]
        sc: dict[str, Any] = {
            "terminal": c["terminal"], "confidence": c["confidence"],
            "path": [_clip(_hop_str(h), max_text) for h in shown_hops],
        }
        if len(hops) > max_hops:
            sc["hops_capped"] = len(hops) - max_hops
        e = c.get("entry")
        if e is not None:
            sc["entry"] = {k: e[k] for k in ("event", "phase") if k in e}
            if e.get("plugin_base"):
                sc["entry"]["plugin_base"] = e["plugin_base"]
            sc["entry"]["forms"] = [_clip(x, max_text) for x in sorted({
                b["form_key"] for b in e.get("bindings", []) if b["form_key"]})]
        chains.append(sc)
    out: dict[str, Any] = {"status": ec["status"], "chains": chains}
    dropped = ec["chains_truncated"] + max(0, len(ec["chains"]) - max_chains)
    if dropped:
        out["chains_truncated"] = dropped
    if ec["search_truncated"]:
        out["search_truncated"] = True
    return out


def _hop_str(h: dict[str, Any]) -> str:
    loc = (f"@{h['call_relpath']}:{h['call_line']}"
           if h.get("call_relpath") and h.get("call_line") is not None else "")
    return f"{h['class']}#{h['method']}{loc}"


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:max(0, limit - 1)] + "…"


_TERMINAL_ZH = {"entry": "事件入口", "plugin_boundary": "插件类边界",
                "no_static_caller": "静态追不到上游", "depth_capped": "超出回溯深度",
                "callers_all_visited": "上游成环/已并入其他链"}


def render_lines(ec: dict[str, Any] | None, *, prefix: str = "     ↳ ",
                 max_chains: int = 3) -> list[str]:
    """字段/操作 trace 共用的人读入口链渲染。"""
    if not ec:
        return []
    if ec["status"] == "self_entry":
        e = ec["entry"]
        return [f"{prefix}本方法即插件事件入口：{e['class']}.{e['method']} "
                f"({e['event']}/{e['phase']})"]
    out: list[str] = []
    for chain in ec.get("chains", [])[:max_chains]:
        path = " → ".join(
            f"{h['class'].rsplit('.', 1)[-1]}#{h['method']}"
            + (f"@{h['call_line']}" if h.get("call_line") is not None else "")
            for h in chain["hops"])
        extra = chain.get("entry") or {}
        event = f" 事件={extra['event']}/{extra['phase']}" if extra.get("event") else ""
        terminal = _TERMINAL_ZH.get(chain["terminal"], chain["terminal"])
        out.append(f"{prefix}入口链[{terminal}·{chain['confidence']}]{event}: {path}")
    dropped = ec.get("chains_truncated", 0) + max(
        0, len(ec.get("chains", [])) - max_chains)
    if dropped:
        out.append(f"{prefix}…另有 {dropped} 条入口链未展开")
    return out


__all__ = ["entry_chains", "registration_status", "slim_chains", "render_lines"]
