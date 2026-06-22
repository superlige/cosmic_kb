"""web —— 阶段 4.5 · 本地 Web 展示层（纯表现层）。

把阶段 4 的项目地图/理解报告从终端文本搬进浏览器：接手者在自己电脑上起一个
**本机 localhost 服务**、用自己的浏览器浏览模块/表单/字段/风险热点。

守红线（CLAUDE.md 第 1 条「本地离线」）：
  * 默认仅绑 `127.0.0.1`，不监听对外网卡；KB/源码/报告绝不外传、前端不引任何 CDN。
  * **后端逻辑零新增**——直接复用阶段 4 的 KB 查询（report.overview / report.project_map /
    graph.store.search），端点把现成 dict 原样吐 JSON，**不改扫描器、不改 KB schema**。
  * 标准库 `http.server` 实现，零新依赖、开箱即跑（不引 FastAPI、不加可选依赖组）。

计划模块：server.py（HTTP 服务 + `/api/*` 路由）、static/（最小单页前端）。
"""
