"use strict";
// 字段级排障前端（vanilla JS，无框架、无 CDN）。数据全来自本机 /api/*。
// 验收反馈重做：字段查询以**实体坐标**分组、定义坐标可点选消歧、表格定宽不挤压。

const $ = (s) => document.querySelector(s);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
};
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path) {
  const r = await fetch(path);
  return r.json();
}

// 通用定宽表格构造（替代 chip 药丸）：headers/cols 定列，rows 给每行 {cells:[html…], onclick?, cls?}。
// 每项独占一行、列宽固定、内容换行——杜绝标签横向挤压。
function buildTable(headers, cols, rows, cls) {
  const t = el("table", "tbl " + (cls || ""));
  const cg = "<colgroup>" + cols.map((c) => `<col class='${c}'>`).join("") + "</colgroup>";
  const th = "<thead><tr>" + headers.map((h) => `<th>${h}</th>`).join("") + "</tr></thead>";
  t.innerHTML = cg + th;
  const tb = el("tbody");
  rows.forEach((r) => {
    const tr = el("tr", (r.onclick ? "clickable " : "") + (r.cls || ""));
    tr.innerHTML = r.cells.map((c) => `<td>${c}</td>`).join("");
    if (r.title) tr.title = r.title;
    if (r.onclick) tr.onclick = r.onclick;
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  return t;
}

const PERSIST = {
  yes: '<span class="b ok">✅落库</span>',
  no: '<span class="b mem">—内存</span>',
  unknown: '<span class="b warn">❓存疑</span>',
  na: "",
};
const LEVEL = { header: "表头", entry: "分录", subentry: "子分录", basedata: "基础资料", unknown: "未知层级" };

// 当前字段查询的精确过滤（点定义坐标时设置）。
let fieldFilter = { form: null, entry: null, level: null };

const simpleName = (fqn) => String(fqn || "").split(".").pop();
const pct = (v) => (v == null ? "—" : Math.round(v * 100) + "%");

// 跳到类名反查 / 单据视图（仿 goField：设模式+查询词再 run）。
function goWhois(q) {
  fieldFilter = { form: null, entry: null, level: null };
  $("#mode").value = "whois";
  $("#q").value = q;
  run();
}
function goBill(key) {
  fieldFilter = { form: null, entry: null, level: null };
  $("#mode").value = "bill";
  $("#q").value = key;
  run();
}

// ── 排障导航首屏：信任仪表 + 盲区与隐藏入口 + 可钻入导航 ─────────────────
// 不再列「高频字段」——排障是症状驱动的，全项目写得最多的字段对不上任何真实入口。
// 分页签呈现：概览/盲区/模块各占一页，避免单页过长滚动。
let navState = { ov: null, cov: null, cmp: null, moduleFilter: null };

async function loadOverview() {
  navState.ov = await api("/api/overview");
  renderOverview();
  // 可信度页签数据独立加载（不阻塞首屏概览渲染）。手段一覆盖率 + 手段二粗/高精度对比。
  api("/api/coverage").then((c) => { navState.cov = c; renderCoveragePane(); });
  api("/api/scan-compare").then((c) => { navState.cmp = c; renderCoveragePane(); });
}

function renderOverview() {
  renderOverviewPane();
  renderBlindPane();
  renderModulesPane();
}

// 页签①：信任 / 覆盖度仪表 ——「答案能信几分、盲区在哪」（红线#4 信任优先）。
function renderOverviewPane() {
  const ov = navState.ov || {};
  const o = ov.overview || {};
  const fa = ov.field_analysis || {};
  const box = $("#tab-overview");
  box.innerHTML = "";

  box.appendChild(el("h2", null, "排障导航 · 这项目长什么样、答案能信几分"));
  if (fa.available === false) {
    box.appendChild(el("p", "warn",
      "⚠ tree-sitter 未启用，字段级分析为空（落库/写入点都为 0）。pip install -e .[parse] 后重建 KB。"));
  }
  box.appendChild(el("p", "muted",
    `Java 分析 ${fa.available === false ? "⚠未启用" : "✅启用"} · ` +
    `桥接命中率 ${pct(o.bridge_hit_rate)} · 已分析插件 ${fa.analyzed_plugins || 0} · ` +
    `字段写入点 ${fa.write_total || 0}（落库 ${fa.persisting_writes || 0} / 存疑 ${fa.uncertain_writes || 0}） · ` +
    `模块 ${o.module_count != null ? o.module_count : "—"} · 单据 ${o.form_count || 0} · 字段 ${o.field_count || 0}`));
  box.appendChild(el("p", "muted",
    "上方搜索框可直接定位：字段→谁改了它、类名→绑在哪、单据→单据视图；" +
    "左右切换页签可浏览盲区与重点单据。"));
}

// 页签②：先看这里 · 盲区与隐藏入口。
function renderBlindPane() {
  const ov = navState.ov || {};
  const fa = ov.field_analysis || {};
  const risk = ov.risk || {};
  const box = $("#tab-blind");
  box.innerHTML = "";

  box.appendChild(el("h2", "warn", "先看这里 · 盲区与隐藏入口"));
  let anyBlind = false;
  const orphans = risk.plugin_orphans || [];
  if (orphans.length) {
    anyBlind = true;
    box.appendChild(el("p", "muted",
      `隐藏入口（孤儿插件 ${orphans.length}）：继承苍穹插件基类却没绑任何单据 — ` +
      `调度/WebApi/工作流等，搜单据时看不到却真改字段。点行反查它绑在哪、读写哪些字段：`));
    const rows = orphans.slice(0, 20).map((x) => ({
      cells: [esc(simpleName(x.fqn)), esc(x.plugin_base || "—"), esc(x.module || "—")],
      title: x.fqn,
      onclick: () => goWhois(simpleName(x.fqn)),
    }));
    box.appendChild(buildTable(["类", "继承基类", "模块"],
      ["c-cls", "c-base", "c-mod"], rows, "nav"));
  }
  const missing = risk.missing_plugins || [];
  if (missing.length) {
    anyBlind = true;
    box.appendChild(el("p", "muted",
      `missing 插件 ${missing.length}：元数据声明了插件类但没找到源码 — 排障会撞墙的地方。点行反查：`));
    const rows = missing.slice(0, 20).map((m) => ({
      cells: [esc(simpleName(m.class_name)), esc(m.form_key || "—"), esc(m.plugin_type || "—")],
      title: m.class_name,
      onclick: () => goWhois(simpleName(m.class_name)),
    }));
    box.appendChild(buildTable(["类", "绑定单据", "类型"],
      ["c-cls", "c-mod", "c-type"], rows, "nav"));
  }
  const ambiguous = (risk.ambiguous_bindings || []).length;
  if (fa.uncertain_writes || ambiguous) {
    anyBlind = true;
    const bits = [];
    if (fa.uncertain_writes)
      bits.push(`落库存疑写入 ${fa.uncertain_writes} 处（静态判不准是否入库 — 用 trace 看明细）`);
    if (ambiguous) bits.push(`歧义绑定 ${ambiguous} 处`);
    box.appendChild(el("p", "muted", "❓ " + bits.join(" · ")));
  }
  if (!anyBlind) box.appendChild(el("p", "muted", "未发现明显盲区。"));
}

// 页签③：从这里钻入 —— 业务模块 + 重点单据（没有具体症状时的浏览入口）。
function renderModulesPane() {
  const ov = navState.ov || {};
  const box = $("#tab-modules");
  box.innerHTML = "";

  box.appendChild(el("h2", null, "从这里钻入 · 模块与重点单据"));
  const modules = (ov.module_map && ov.module_map.modules || []).filter((m) => (m.form_count || 0) > 0);
  if (modules.length) {
    box.appendChild(el("p", "muted", "业务模块（点行只看该模块的重点单据，再点取消）："));
    const rows = modules.map((m) => ({
      cells: [esc(m.name), String(m.form_count || 0), String(m.plugin_count || 0),
        String(m.orphan_real_count || 0), pct(m.pkg_consistency)],
      cls: navState.moduleFilter === m.name ? "active" : "",
      onclick: () => {
        navState.moduleFilter = navState.moduleFilter === m.name ? null : m.name;
        renderModulesPane();
      },
    }));
    box.appendChild(buildTable(["模块", "单据", "插件", "真孤儿", "包一致度"],
      ["c-mod", "c-num", "c-num", "c-num", "c-num"], rows, "nav"));
  }

  // 重点单据：有自定义插件操作的优先，再按字段规模；受模块过滤。
  let forms = (ov.forms || []).slice();
  if (navState.moduleFilter) forms = forms.filter((f) => f.module === navState.moduleFilter);
  forms.sort((a, b) =>
    (b.op_with_plugin_count || 0) - (a.op_with_plugin_count || 0) ||
    (b.field_count || 0) - (a.field_count || 0));
  const label = navState.moduleFilter ? `重点单据 · ${navState.moduleFilter}（点行进单据视图）` : "重点单据（★含自定义插件操作优先，点行进单据视图）";
  box.appendChild(el("p", "muted", label));
  const frows = forms.slice(0, 20).map((f) => {
    const star = (f.op_with_plugin_count || 0) > 0 ? "★ " : "";
    const big = (f.field_count || 0) >= 150 ? ' <span class="b warn">巨型</span>' : "";
    return {
      cells: [star + esc(f.key) + (f.name ? `「${esc(f.name)}」` : "") + big,
        esc(f.module || "—"), String(f.field_count || 0),
        String(f.plugin_count || 0), String(f.op_with_plugin_count || 0)],
      onclick: () => goBill(f.key),
    };
  });
  box.appendChild(buildTable(["单据", "模块", "字段", "插件", "含插件操作"],
    ["c-bill", "c-mod", "c-num", "c-num", "c-num"], frows, "nav"));
}

// ── 页签：扫描可信度（手段一·字段覆盖率 + 质量分解）─────────────────────
// 红线#4 信任优先：覆盖率/可信度是一等功能。用元数据字段当分母算覆盖率，并把"覆盖率
// 这个数字怎么解读"的四个质量维度一并铺开——未覆盖≠漏扫，要看质量分解才能定信任。
const COV_LEVEL = { good: "ok", ok: "warn", low: "bad", blocked: "bad" };

// 进度条仪表：label 左、条中、右侧数值/说明。tone 控制条色（ok/warn/bad）。
function meter(label, rate, note, tone) {
  const pct = rate == null ? null : Math.round(rate * 100);
  const row = el("div", "meter");
  row.appendChild(el("div", "m-label", esc(label)));
  const track = el("div", "m-track");
  const fill = el("div", "m-fill " + (tone || meterTone(rate)));
  fill.style.width = (pct == null ? 0 : pct) + "%";
  track.appendChild(fill);
  row.appendChild(track);
  row.appendChild(el("div", "m-val", (pct == null ? "—" : pct + "%") +
    (note ? ` <span class="muted">${esc(note)}</span>` : "")));
  return row;
}
function meterTone(rate) {
  if (rate == null) return "warn";
  return rate >= 0.8 ? "ok" : rate >= 0.5 ? "warn" : "bad";
}

function renderCoveragePane() {
  const box = $("#tab-coverage");
  box.innerHTML = "";
  const c = navState.cov;
  if (!c) { box.appendChild(el("p", "muted", "可信度数据加载中…")); return; }

  const fc = c.field_coverage || {};
  const v = c.verdict || {};
  box.appendChild(el("h2", null, "扫描可信度 · 手段一「字段覆盖率」"));
  box.appendChild(el("p", "verdict " + (COV_LEVEL[v.level] || ""), esc(v.text || "")));

  // 手段一头条：字段覆盖率（大号 + 主进度条）。
  const head = el("div", "cov-hero");
  head.appendChild(el("div", "cov-big", fc.rate == null ? "—" : Math.round(fc.rate * 100) + "%"));
  const meta = el("div", "cov-meta");
  meta.appendChild(el("div", null,
    `元数据业务字段 <b>${fc.business_total || 0}</b>（分母） · ` +
    `代码观测到被读/写 <b>${fc.touched || 0}</b>（分子） · 未覆盖 ${fc.untouched || 0}`));
  meta.appendChild(el("div", "muted",
    `其中被写 ${fc.write_touched || 0} · 被读 ${fc.read_touched || 0} · ` +
    `业务字段类别 = ${(fc.business_kinds || []).join(", ")}`));
  meta.appendChild(el("div", "muted",
    "注：未覆盖 ≠ 漏扫——大量字段纯展示/纯存储，本就无插件读写。低不一定是问题，关键看下方质量分解。"));
  head.appendChild(meta);
  box.appendChild(head);

  // 字段分类计数（分母怎么来的，透明）。
  const bk = fc.by_kind || {};
  const kinds = Object.keys(bk).sort((a, b) => bk[b] - bk[a]);
  if (kinds.length) {
    box.appendChild(el("p", "muted", "字段分类计数： " +
      kinds.map((k) => `${esc(k)}×${bk[k]}`).join(" · ")));
  }

  // 质量分解四条（让覆盖率可被正确解读）。
  const rq = c.resolution_quality || {}, lq = c.location_quality || {},
    pq = c.persist_quality || {}, mm = c.meta_match || {};
  box.appendChild(el("h3", null, "扫描质量分解（覆盖率这个数字怎么解读）"));
  const mbox = el("div", "meters");
  mbox.appendChild(meter("① 字段标识解析可信", rq.reliable_rate,
    `${rq.reliable || 0}/${rq.total || 0} 字面量/常量 · 存疑 ${rq.uncertain || 0}`));
  mbox.appendChild(meter("② 来源单据定位", lq.located_rate,
    `未定位单据 ${lq.unlocated_form || 0} · 层级未知 ${lq.unknown_level || 0}`));
  mbox.appendChild(meter("③ 落库判定确定", pq.certain_rate,
    `落库 ${pq.persisting || 0} / 内存 ${pq.memory_only || 0} / 存疑 ${pq.uncertain || 0}`));
  mbox.appendChild(meter("④ 命中元数据字段", mm.match_rate,
    `对不上 ${mm.unmatched || 0}（多为平台字段/常量解析偏差）`));
  box.appendChild(mbox);

  // 上游可信度（覆盖率天花板）。
  const up = c.upstream || {};
  box.appendChild(el("p", "muted",
    `上游可信度（覆盖率天花板）：Java 分析 ${up.java_available === false ? "⚠未启用" : "✅启用"} · ` +
    `已分析插件 ${up.analyzed_plugins || 0} · 桥接命中率 ${pct(up.bridge_hit_rate)}` +
    `（missing ${up.bridge_missing || 0}）`));

  // 报告页只留汇总+判定，不再内嵌按模块/低覆盖单据明细表——排查动作回到字段/单据搜索。

  // 手段二：粗精度 vs 高精度对比（同页接续，数据到了才渲染）。
  renderComparePart(box);
}

// ── 手段二：粗精度扫描 vs 高精度扫描对比（召回底线 + 疑似盲点）─────────────
// 高精度=field_access(AST+跨类+落库)；粗扫=字面量+常量名引用(单遍词法，跳注释)。
// 粗扫见到、高精度漏掉=疑似盲点 → 点行进该字段的高精度追踪视图，对照源码位置逐处核对。
function renderComparePart(box) {
  const c = navState.cmp;
  box.appendChild(el("h2", null, "扫描可信度 · 手段二「粗精度 vs 高精度对比」"));
  if (!c) { box.appendChild(el("p", "muted", "对比数据加载中…")); return; }
  const v = c.verdict || {};
  box.appendChild(el("p", "verdict " + (COV_LEVEL[v.level] || ""), esc(v.text || "")));
  if (c.java_available === false) return;

  // 集合分桶头条：两者都见占比（粗扫为分母——召回底线）。
  const coverRate = c.coarse_hit ? c.both / c.coarse_hit : null;
  const mbox = el("div", "meters");
  mbox.appendChild(meter("两者都见（粗扫命中里高精度也覆盖）", coverRate,
    `粗扫命中 ${c.coarse_hit}（含常量名引用 ${c.coarse_const_hit || 0}） · 高精度命中 ${c.high_hit} · 全集 ${c.universe}`));
  box.appendChild(mbox);
  box.appendChild(el("p", "muted",
    `▸ 仅粗扫见（疑似盲点）<b>${c.coarse_only}</b> = 强信号 ${c.coarse_only_idiom}（★读写习语）+ 弱信号 ${c.coarse_only_literal}（多为常量定义/弱引用）　` +
    `▸ 仅高精度见（精度增量）<b>${c.high_only}</b>（字段 key 多由拼接/外部常量得到）　` +
    `▸ 两侧都没碰 ${c.neither}。注：盲点是「候选」非「确诊」——纯文本比对有误报，请跳源码核对。`));
  // 报告页只给汇总判定；逐字段的疑似盲点/精度增量明细已并入字段搜索（粗/高精度逐处互证）。
  box.appendChild(el("p", "muted",
    "逐字段排查请用上方搜索「字段(按层级) → 谁改了它」——结果会同时列高精度归因与粗扫命中、逐处互证。"));
}

// 表格内的迷你进度条（带数值）。
function miniBar(rate) {
  const pctv = rate == null ? null : Math.round(rate * 100);
  const tone = meterTone(rate);
  return `<span class="minibar"><span class="mb-fill ${tone}" style="width:${pctv == null ? 0 : pctv}%"></span></span>` +
    ` <span class="mb-val">${pctv == null ? "—" : pctv + "%"}</span>`;
}

// ── 字段追踪（旗舰）：以实体坐标分组 ───────────────────────────────
function renderField(ft) {
  const out = el("div", "card");
  out.appendChild(el("h2", null, `字段 ${esc(ft.field_key)}`));

  // 定义坐标（消歧菜单）：每个坐标一行，点行缩到该实体。
  if (ft.occurrences && ft.occurrences.length) {
    out.appendChild(el("p", "muted", "该字段属于哪个实体（定义坐标，点行缩到该实体）："));
    const rows = [{
      cells: ["<b>全部坐标</b>", "—", "—", "—", "—"],
      onclick: () => goField(ft.field_key, {}),
      cls: !hasFilter(ft.filter) ? "active" : "",
    }];
    ft.occurrences.forEach((o) => {
      // entry 仅对分录/子分录有意义；表头/基础资料字段的 entity_key 存的是表头实体 key，
      // 但 field_access 里表头 entry_key 恒为 None，传进去会匹配落空（与后端归一对齐）。
      const isEntry = o.level === "entry" || o.level === "subentry";
      const f = { form: o.form_key, entry: (isEntry && o.entity_key) || null, level: o.level };
      rows.push({
        cells: [
          esc(o.form_key) + (o.form_name ? `「${esc(o.form_name)}」` : ""),
          esc(LEVEL[o.level] || o.level),
          (o.entity_key ? esc(o.entity_key) : "—") + (o.entity_name ? `「${esc(o.entity_name)}」` : ""),
          esc(o.field_name || ""), esc(o.kind || ""),
        ],
        onclick: () => goField(ft.field_key, f),
        cls: sameFilter(ft.filter, f) ? "active" : "",
      });
    });
    out.appendChild(buildTable(
      ["单据", "层级", "分录 / 实体", "字段名", "类别"],
      ["c-form", "c-lvl", "c-entity", "c-fname", "c-kind"], rows, "occ"));
  }

  const s = ft.summary || {};
  out.appendChild(el("p", "muted",
    `实体坐标 ${s.coords || 0} 个 · 写入 ${s.writers || 0}` +
    `（落库 ${s.persisting_writers || 0} / 存疑 ${s.uncertain_writers || 0}）· ` +
    `读取 ${s.readers || 0} · 涉及插件 ${s.plugins || 0} / 单据 ${s.forms || 0}`));
  if (ft.note) out.appendChild(el("p", "warn", esc(ft.note)));

  (ft.groups || []).forEach((g) => out.appendChild(renderGroup(g)));
  if (!(ft.groups || []).length && !ft.note) {
    out.appendChild(el("p", "muted", "（当前坐标无插件读写记录）"));
  }

  // 仅粗扫见（疑似盲点，已剔除高精度也记 + 常量类定义）。
  if (ft.coarse && ft.coarse.coarse_only) out.appendChild(renderCoarse(ft.coarse));

  // 可能命中（同单据同字段、层级/分录存疑）——绝不遗漏。
  if (ft.possible && ft.possible.length) {
    const box = el("div", "group");
    const head = el("div", "ghead");
    head.appendChild(el("div", "glabel warn", "可能命中（本单据同字段，层级/分录存疑）"));
    head.appendChild(el("div", "gstat", `${ft.possible.length} 条 — 静态判不准层级/分录，供人工核对`));
    box.appendChild(head);
    const body = el("div", "gbody");
    body.appendChild(possibleTable(ft.possible));
    box.appendChild(body);
    out.appendChild(box);
  }
  // 未定位单据（来源实体判不出，但确有插件读写该字段）——直接铺开明细供人工核对。
  if (ft.unlocated && ft.unlocated.length) {
    const box = el("div", "group");
    const head = el("div", "ghead");
    head.appendChild(el("div", "glabel warn", "未定位单据（数据包来源判不出）"));
    head.appendChild(el("div", "gstat",
      `${ft.unlocated.length} 条 — 来源实体静态判不出归属单据，但确实读写该字段，供人工核对`));
    box.appendChild(head);
    const body = el("div", "gbody");
    const w = ft.unlocated.filter((r) => r.access === "write");
    const rd = ft.unlocated.filter((r) => r.access === "read");
    if (w.length) { body.appendChild(el("h4", null, "写该字段的插件事件")); body.appendChild(accessTable(w)); }
    if (rd.length) { body.appendChild(el("h4", null, "读该字段的插件事件")); body.appendChild(accessTable(rd)); }
    box.appendChild(body);
    out.appendChild(box);
  }
  return out;
}

// 仅粗扫见块：只列「粗扫见到、高精度没记、且非常量类」的疑似盲点（候选非确诊）。
function renderCoarse(coarse) {
  const box = el("div", "group");
  const head = el("div", "ghead");
  head.appendChild(el("div", "glabel warn", "仅粗扫见（疑似盲点，非常量类）"));
  head.appendChild(el("div", "gstat",
    `${coarse.coarse_only} 处（强信号 ${coarse.idiom}） · 高精度记 ${coarse.high_rows} 条` +
    (coarse.const_excluded ? ` · 已剔除常量类定义 ${coarse.const_excluded} 处` : "")));
  box.appendChild(head);
  const body = el("div", "gbody");
  const rows = (coarse.locations || []).map((l) => ({
    cells: [
      (l.idiom ? "⚡读写习语" : "弱引用"),
      esc(l.via),
      esc(l.relpath) + ":" + l.line,
    ],
    cls: "warn",
  }));
  body.appendChild(buildTable(["信号", "形态", "源码位置"],
    ["c-flag", "c-flag", "c-path"], rows, "coarse"));
  body.appendChild(el("p", "muted",
    "这些位置高精度同位置没记，是候选盲点非确诊，请跳源码核对；纯文本比对天生有误报。"));
  box.appendChild(body);
  return box;
}

function possibleTable(rows) {
  const t = el("table", "tbl acc");
  t.innerHTML =
    "<colgroup><col class='c-plugin'><col class='c-owner'><col class='c-lvl'><col class='c-event'>" +
    "<col class='c-pst'><col class='c-loc'><col class='c-path'></colgroup>" +
    "<thead><tr><th>插件类</th><th>插件所属单据</th><th>层级</th><th>事件函数</th>" +
    "<th>落库</th><th>位置</th><th>调用链 / 跨类</th></tr></thead>";
  const tb = el("tbody");
  groupByPlugin(rows).forEach((grp) => {
    grp.rows.forEach((r, i) => {
      const tr = el("tr", i === 0 ? "pgrp" : "");
      const lvl = (LEVEL[r.level] || r.level || "") + (r.entry_key ? "·" + esc(r.entry_key) : "");
      let pathHtml = (r.path && r.path.length > 1) ? esc(r.path.join(" → ")) : "";
      if (r.cross_class) pathHtml = `<span class="b cross">↳ ${esc(r.access_simple)}</span> ` + pathHtml;
      const detail =
        `<td>${lvl}</td><td>${esc(r.event_method)}</td>` +
        `<td>${PERSIST[r.persists] || esc(r.persists)}</td>` +
        `<td class="loc">${esc(r.source_relpath)}:${r.line}</td>` +
        `<td class="mono">${pathHtml}</td>`;
      if (i === 0) {
        const n = grp.rows.length;
        tr.innerHTML =
          `<td rowspan="${n}" class="ellip" title="${esc(r.plugin_fqn)}">${esc(r.plugin_simple)}</td>` +
          `<td rowspan="${n}">${ownerCell(r)}</td>` + detail;
      } else {
        tr.innerHTML = detail;
      }
      if (r.persist_reason) tr.title = r.persist_reason;
      tb.appendChild(tr);
    });
  });
  t.appendChild(tb);
  return t;
}

function renderGroup(g) {
  const box = el("div", "group");
  const head = el("div", "ghead");
  head.appendChild(el("div", "glabel", esc(g.label)));
  const gs = g.summary || {};
  head.appendChild(el("div", "gstat",
    `写 ${gs.writers || 0}（落库 ${gs.persisting || 0} / 存疑 ${gs.uncertain || 0}） · ` +
    `读 ${gs.readers || 0} · 插件 ${gs.plugins || 0}`));
  // 转换上下游(BOTP)：该实体在单据流转链上的来龙去脉（有才显示）。
  const cc = g.convert_context || {};
  if ((cc.upstream && cc.upstream.length) || (cc.downstream && cc.downstream.length)) {
    const fmt = (xs) => xs.map((x) => esc(x.entity) + (x.name ? `「${esc(x.name)}」` : "")).join("、");
    const up = (cc.upstream && cc.upstream.length) ? fmt(cc.upstream) : "—";
    const down = (cc.downstream && cc.downstream.length) ? fmt(cc.downstream) : "—";
    head.appendChild(el("div", "gconv",
      `转换上下游：上游来源单 ${up} → 本单 → 下游目标单 ${down}`));
  }
  box.appendChild(head);
  const body = el("div", "gbody");
  if (g.writers && g.writers.length) {
    body.appendChild(el("h4", null, "写该字段的插件事件（落库 > 存疑 > 内存）"));
    body.appendChild(accessTable(g.writers));
  }
  // readers 现为「按方法去重」的清单 {total, methods, capped}（防膨胀，与 trace 返回口径一致）。
  const rd = g.readers || {};
  if (rd.total) {
    body.appendChild(el("h4", null,
      `读该字段的插件事件（按方法去重，共 ${rd.total} 处 → ${(rd.methods || []).length} 个方法）`));
    body.appendChild(readerMethodsTable(rd));
  }
  box.appendChild(body);
  return box;
}

// 读取方法清单表：同插件同事件方法去重一行，给 count + 位置 + calls 锚点（去那读源码）。
function readerMethodsTable(rd) {
  const t = el("table", "tbl acc");
  t.innerHTML =
    "<thead><tr><th>插件类</th><th>类型</th><th>事件函数</th><th>读取处数</th>" +
    "<th>位置</th><th>导航</th></tr></thead>";
  const tb = el("tbody");
  (rd.methods || []).forEach((m) => {
    const cls = m.plugin_simple || (m.class_fqn || "?").split(".").pop();
    const tr = el("tr");
    tr.innerHTML =
      `<td class="ellip" title="${esc(m.class_fqn || "")}">${esc(cls)}</td>` +
      `<td>${esc(m.plugin_type || "")}</td>` +
      `<td>${esc(m.method || "")}</td>` +
      `<td>${m.count}</td>` +
      `<td class="loc">${esc((m.locations || []).join(" / "))}</td>` +
      `<td class="mono muted">${esc(m.calls || "")}</td>`;
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  if (rd.capped) {
    const cap = el("div", "muted", `…另有 ${rd.capped} 个读方法未列出（收窄到 单据.字段 看全部）`);
    const wrap = el("div", null);
    wrap.appendChild(t);
    wrap.appendChild(cap);
    return wrap;
  }
  return t;
}

// 插件所属单据单元格：标出「被另外哪个元数据的插件改了」，跨单据高亮。
function ownerCell(r) {
  if (!r.plugin_form_label) return '<span class="muted">— service/未注册</span>';
  const cross = r.plugin_cross_form ? ' <span class="b cross">跨单据</span>' : "";
  return esc(r.plugin_form_label) + cross;
}

// 按插件类合并：同一 plugin_fqn 只占一块，插件类/所属单据/类型三列跨行合并，
// 事件函数/落库/位置/调用链每条 access 一行——消除重复的插件类名，降低查看复杂度。
function accessTable(rows) {
  const t = el("table", "tbl acc");
  t.innerHTML =
    "<colgroup><col class='c-plugin'><col class='c-owner'><col class='c-type'><col class='c-event'>" +
    "<col class='c-pst'><col class='c-loc'><col class='c-path'></colgroup>" +
    "<thead><tr><th>插件类</th><th>插件所属单据</th><th>类型</th><th>事件函数</th>" +
    "<th>落库</th><th>位置</th><th>调用链 / 跨类</th></tr></thead>";
  const tb = el("tbody");
  groupByPlugin(rows).forEach((grp) => {
    grp.rows.forEach((r, i) => {
      const tr = el("tr", i === 0 ? "pgrp" : "");
      const resFlag = (r.key_resolution === "literal" || r.key_resolution === "constant")
        ? "" : ` <span class="b warn">${esc(r.key_resolution)}</span>`;
      let pathHtml = (r.path && r.path.length > 1) ? esc(r.path.join(" → ")) : "";
      if (r.cross_class) {
        pathHtml = `<span class="b cross">↳ ${esc(r.access_simple)}</span> ` + pathHtml;
      }
      const detail =
        `<td>${esc(r.event_method)}${resFlag}</td>` +
        `<td>${PERSIST[r.persists] || esc(r.persists)}</td>` +
        `<td class="loc" title="${esc(r.via)}">${esc(r.source_relpath)}:${r.line}</td>` +
        `<td class="mono">${pathHtml}</td>`;
      if (i === 0) {
        const n = grp.rows.length;
        const typeBadge = r.plugin_type === "service"
          ? '<span class="b svc">service</span>' : esc(r.plugin_type);
        tr.innerHTML =
          `<td rowspan="${n}" class="ellip" title="${esc(r.plugin_fqn)}">${esc(r.plugin_simple)}</td>` +
          `<td rowspan="${n}">${ownerCell(r)}</td>` +
          `<td rowspan="${n}">${typeBadge}</td>` + detail;
      } else {
        tr.innerHTML = detail;
      }
      if (r.persist_reason) tr.title = r.persist_reason;
      tb.appendChild(tr);
    });
  });
  t.appendChild(tb);
  return t;
}

// 按 plugin_fqn 分组，保持首次出现顺序、组内保持原排序。
function groupByPlugin(rows) {
  const order = [], byPlugin = new Map();
  (rows || []).forEach((r) => {
    if (!byPlugin.has(r.plugin_fqn)) { byPlugin.set(r.plugin_fqn, []); order.push(r.plugin_fqn); }
    byPlugin.get(r.plugin_fqn).push(r);
  });
  return order.map((fqn) => ({ fqn, rows: byPlugin.get(fqn) }));
}

function hasFilter(f) { return !!(f && (f.form_key || f.entry_key || f.level)); }
function sameFilter(applied, want) {
  return applied && (applied.form_key || null) === (want.form || null) &&
    (applied.entry_key || null) === (want.entry || null) &&
    (applied.level || null) === (want.level || null);
}

// 跳到字段查询（带可选精确过滤）。
function goField(key, f) {
  fieldFilter = { form: f.form || null, entry: f.entry || null, level: f.level || null };
  $("#mode").value = "field";
  $("#q").value = key;
  run();
}

// ── 类名反查 ─────────────────────────────────────────────────────
function renderWhois(w) {
  const out = el("div", "card");
  out.appendChild(el("h2", null, `反查 “${esc(w.query)}”`));
  if (!w.bindings.length) { out.appendChild(el("p", "warn", "没找到匹配的插件绑定（试试末段类名）。")); }
  w.bindings.forEach((b) => {
    const bad = b.status === "missing" || b.status === "ambiguous";
    out.appendChild(el("p", null,
      `<b>${esc(b.class_name)}</b> [${esc(b.plugin_type)}] → 单据 ` +
      `<a data-bill="${esc(b.form_key)}">${esc(b.form_key)}</a> ` +
      `<span class="b ${bad ? "warn" : "ok"}">${esc(b.status)}</span> ` +
      `<span class="mono muted">${esc(b.source_relpath || b.note || "")}</span>`));
  });
  if (w.touches && w.touches.length) {
    out.appendChild(el("h3", null, "这些类读写的字段"));
    const t = el("table", "tbl");
    t.innerHTML = "<thead><tr><th>字段</th><th>层级</th><th>读写</th><th>落库</th>" +
      "<th>单据</th><th>事件</th><th>位置</th></tr></thead>";
    const tb = el("tbody");
    w.touches.forEach((r) => {
      const lvl = (LEVEL[r.level] || r.level || "") + (r.entry_key ? "·" + esc(r.entry_key) : "");
      const tr = el("tr");
      tr.innerHTML =
        `<td><a data-field="${esc(r.field_key)}">${esc(r.field_key)}</a></td>` +
        `<td>${lvl}</td><td>${esc(r.access)}</td><td>${PERSIST[r.persists] || ""}</td>` +
        `<td>${esc(r.form_key || "—")}</td><td>${esc(r.event_method)}</td>` +
        `<td class="mono">${esc(r.source_relpath)}:${r.line}</td>`;
      tb.appendChild(tr);
    });
    t.appendChild(tb);
    out.appendChild(t);
  }
  return out;
}

// ── 单据视图：以实体为单位展示字段触达 ─────────────────────────────
function renderBill(bv) {
  const f = bv.form, st = bv.stats;
  const out = el("div", "card");
  out.appendChild(el("h2", null,
    `单据 ${esc(f.key)} 「${esc(f.name || "")}」 [${esc(f.form_type)}]`));
  out.appendChild(el("p", "muted",
    `实体 ${st.entity_count} · 字段 ${st.field_count} · 操作 ${st.operation_count} · ` +
    `插件 ${st.plugin_count} · 有插件触达的字段 ${st.touched_fields}`));

  if (bv.operations.length) {
    out.appendChild(el("h3", null, "操作集（★ 有自定义插件，排障优先）"));
    const ul = el("ul", "ops");
    bv.operations.forEach((o) => ul.appendChild(el("li", o.has_plugin ? "hot" : "",
      `${o.has_plugin ? "★ " : ""}${esc(o.key)} 「${esc(o.name || "")}」[${esc(o.operation_type || "?")}]`)));
    out.appendChild(ul);
  }
  if (bv.plugins.length) {
    out.appendChild(el("h3", null, "插件清单"));
    const ul = el("ul");
    bv.plugins.forEach((p) => ul.appendChild(el("li", null,
      `[${esc(p.plugin_type)}] <span class="mono">${esc(p.class_name)}</span> ` +
      `<span class="muted">(${esc(p.source)}${p.operation_key ? " ←" + esc(p.operation_key) : ""})</span>`)));
    out.appendChild(ul);
  }

  // 字段触达：按实体分组（每个实体一块，块内列字段）。
  const groups = (bv.entity_touch || []).slice().sort((a, b) => b.fields.length - a.fields.length);
  if (groups.length) {
    out.appendChild(el("h3", null, "字段触达（按实体分组，点字段追踪）"));
    groups.forEach((eg) => {
      const box = el("div", "group");
      const head = el("div", "ghead");
      const lvl = LEVEL[eg.level] || eg.level || "";
      const label = eg.entity_key
        ? `${lvl} ${esc(eg.entity_key)}${eg.entity_name ? "「" + esc(eg.entity_name) + "」" : ""}`
        : "表头";
      head.appendChild(el("div", "glabel", label));
      head.appendChild(el("div", "gstat", `${eg.fields.length} 个字段被插件读写`));
      box.appendChild(head);
      const body = el("div", "gbody");
      const rows = eg.fields.map((fld) => ({
        cells: [esc(fld.field_key), String(fld.writers), String(fld.persisting), String(fld.readers)],
        onclick: () => goField(fld.field_key, { form: f.key, entry: eg.entity_key, level: eg.level }),
      }));
      body.appendChild(buildTable(
        ["字段", "写", "落库", "读"],
        ["c-field", "c-num", "c-num", "c-num"], rows, "touch"));
      box.appendChild(body);
      out.appendChild(box);
    });
  }
  if (bv.risk_bindings && bv.risk_bindings.length) {
    out.appendChild(el("h3", "warn", "风险：project 插件未命中源码 / 歧义"));
    const ul = el("ul");
    bv.risk_bindings.forEach((b) => ul.appendChild(el("li", "warn",
      `[${esc(b.status)}] ${esc(b.class_name)} — ${esc(b.note || "")}`)));
    out.appendChild(ul);
  }
  return out;
}

// ── 调度 ─────────────────────────────────────────────────────────
async function run() {
  const mode = $("#mode").value;
  const q = $("#q").value.trim();
  const box = $("#tab-result");
  box.innerHTML = "";
  if (!q) { showTab("overview"); return; }
  // 搜索后只展示搜索结果：切到「搜索结果」页签。
  showTab("result");
  box.appendChild(el("p", "muted", "查询中…"));
  try {
    let node;
    if (mode === "field") {
      let url = "/api/field?key=" + encodeURIComponent(q);
      if (fieldFilter.form) url += "&form=" + encodeURIComponent(fieldFilter.form);
      if (fieldFilter.entry) url += "&entry=" + encodeURIComponent(fieldFilter.entry);
      if (fieldFilter.level) url += "&level=" + encodeURIComponent(fieldFilter.level);
      node = renderField(await api(url));
    } else if (mode === "whois") {
      node = renderWhois(await api("/api/whois?q=" + encodeURIComponent(q)));
    } else {
      const bv = await api("/api/bill/" + encodeURIComponent(q));
      node = bv.error ? el("p", "warn", esc(bv.error)) : renderBill(bv);
    }
    box.innerHTML = "";
    box.appendChild(node);
  } catch (e) {
    box.innerHTML = "";
    box.appendChild(el("p", "warn", "查询失败: " + esc(e.message)));
  }
}

// 结果区内的字段/单据链接：委托点击（清空精确过滤，回到该字段全部坐标）。
$("#tab-result").addEventListener("click", (ev) => {
  const fEl = ev.target.closest("[data-field]");
  const bEl = ev.target.closest("[data-bill]");
  if (fEl) { ev.preventDefault(); goField(fEl.dataset.field, {}); }
  else if (bEl) { ev.preventDefault(); $("#mode").value = "bill"; $("#q").value = bEl.dataset.bill; run(); }
});

const PLACEHOLDER = {
  field: "按层级录入：单据.字段(表头) / 单据.分录.字段 / 单据.分录.子分录.字段；裸字段=列全部坐标",
  whois: "输入类名或报错栈里的类（末段即可），回车反查",
  bill: "输入单据标识，如 cqkd_assetcard，回车查看单据视图",
};
function fieldHint() {
  $("#hint").innerHTML =
    "字段查询按层级精确定位：<b>单据.字段</b>(表头) · <b>单据.分录.字段</b> · " +
    "<b>单据.分录.子分录.字段</b>；只输字段=列出它在各实体的全部坐标（再点选/补全层级）。" +
    "结果会标出每条修改的<b>插件所属单据</b>（跨单据高亮）与该实体的<b>转换上下游</b>。";
}
// 手动改查询词或切模式时清空精确过滤。
$("#mode").addEventListener("change", () => {
  const m = $("#mode").value;
  $("#q").placeholder = PLACEHOLDER[m];
  fieldFilter = { form: null, entry: null, level: null };
  if (m === "field") fieldHint(); else $("#hint").innerHTML = "";
});
$("#q").addEventListener("input", () => { fieldFilter = { form: null, entry: null, level: null }; });
$("#go").addEventListener("click", run);
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });

// ── 页签切换 ─────────────────────────────────────────────────────
// 搜索前「搜索结果」页签禁用（无内容）；搜索后启用并自动切过去。
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tabpane").forEach((p) =>
    p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "result") $("#tab-result-btn").classList.remove("disabled");
}
document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => {
    // 「搜索结果」页签在没有结果时不可点。
    if (t.dataset.tab === "result" && t.classList.contains("disabled")) return;
    showTab(t.dataset.tab);
  }));
$("#tab-result-btn").classList.add("disabled");

fieldHint();
loadOverview();
