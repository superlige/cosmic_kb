# cosmic_kb —— 苍穹老项目本地排障导航工具

> 接手一个陌生的金蝶云苍穹（Cosmic）老项目，本工具**指向它的源码和元数据，纯本地扫一遍**，
> 建成一个知识库（KB）。之后你的 AI agent（Claude Code / Codex / CodeBuddy / Qoder / Trae …）
> 接上这个 KB，就能回答「这个字段是谁改的、在哪个插件的哪个事件函数、改完落不落库、源码第几行」
> 这类问题——所有结论都带类/方法/事件/行号证据，判不准就标 `unknown`，**绝不臆造**。

纯本地运行、不外传源码；专治「老旧」苍穹项目。

---

## 能做什么？

核心两件事，对应老项目排障最常见的两个场景：

**① 字段级排障**：字段出了问题（"这个值是谁改的、改完落没落库"），不用自己先从元数据翻出
一堆插件全路径、再逐个肉眼翻源码拼证据——直接查 KB 拿到"插件/方法/事件/是否落库/源码行号"
这条完整证据链。元数据和 Java 源码已经**一起解析进同一份 KB**；扩展单据的原厂继承字段也能
自动补齐，不会结构性半盲。

**② 源码解析核对**：反过来，大模型拿到一段陌生源码/插件类（"这段代码是干什么的"），**不能
只凭命名习惯和通用 Java/Spring 经验瞎猜业务含义**——必须能核对清楚：这个插件绑定在哪个单据
的哪个页面上、挂在哪个操作上、什么时候触发；代码里出现的字段/单据英文标识对应的真实中文名
是什么；用到的苍穹私有插件类型/生命周期/SDK 是什么用法。这三层核对靠的是把苍穹这套私有二开
框架的**元数据事实**和**领域知识**（插件类型/事件时机/原厂 SDK 用法）也备进同一份 KB——防止
通用 agent 见到苍穹代码套一套普通 Java 经验硬编业务逻辑，猜错了还不自知。

四个取证工具，覆盖上面两个场景（工具本身**不调用大模型、不下结论**，只产出确定性证据，讲成
人话靠你接的 agent）：

- **`trace "单据.字段"`** —— 一次列出所有读写它的插件/方法/事件/是否落库/源码行号。
  > 没有时：先从元数据翻出这张单绑定的一堆插件全路径逐个肉眼找，还得自己判落没落库；跨单据
  > 间接写入元数据翻不出，只能全局搜源码；单据标识魔法值/常量混用搜都搜不全，几十个插件翻到崩溃。
- **`bill "单据标识"`** —— 一次列出表单插件/操作/每个操作绑定的插件，把源码里看到的插件类对回
  元数据的绑定关系，不用凭类名/包名猜它干什么。
  
  > 没有时：设计器里这三处分散在不同页面，来回切好几个视图才能拼起来，还不知道插件绑定位置。
- **`resolve_fields`** —— 把源码里字段/分录/单据的英文标识一次核对出真实中文名 + 元数据定义
  （分录层级、字段类型、下拉/枚举取值中文含义、基础资料引用指向的目标单据），查不到/有歧义如实说。
  > 没有时：读到 `cqkd_zkd`、`srctransid` 只能按拼音瞎猜（真实翻车：「转款单」被猜成"转账单"），
  > 下拉字段的 `1`/`2` 枚举、基础资料引用哪张单据更是只能瞎蒙。
- **`cosmic_semantics`** —— 查权威文档核对苍穹私有插件类型、事件触发时机、原厂 SDK 用法、
  入库判断规则、反模式黑名单。
  > 没有时：见到 `AbstractBillPlugIn`、`afterCreateNewData`、`BusinessDataServiceHelper` 这些苍穹
  > 私有类型，通用大模型没学过，只能套普通 Java 经验硬猜，猜错也意识不到。

## 什么原理？

**两段式解耦，KB 是契约：**

1. **段一·本地确定性扫描器**：指向你的 Java 源码 + 元数据（dym/cr/zip 或直连底层库只读现取），
   在本机离线扫一遍，把元数据事实、Java 字段级读写、苍穹领域语义**一起解析进同一份 KB**
   （`cosmic_kb.db`，SQLite 图谱 + 全文索引）。不调大模型、不依赖代码可编译或依赖解析。
2. **段二·AI 理解层**：你的 agent 通过 MCP 直调上面四个确定性取证工具查 KB，带**类·方法·事件·
   行号**证据作答。

派生哲学：**处处置信度 + 证据行号 + unknown**——老项目分析天生不完整，判得准标 `confirmed`，
判不准标 `unknown`，宁可标 unknown 也**绝不臆造**字段名/方法名/业务含义。

**为什么要接 agent 而不是自己看返回**：`trace` 命中多个插件/坐标时一次就是几十条 JSON（类名、
方法名、行号、置信度……），肉眼扫读很累也容易看漏。正常用法是让 agent 帮你调工具、读证据、
组织成"谁改的/在哪/落不落库"这样的中文结论——所以下面的安装以"接上 agent"为默认路径。

---

## 怎么安装？对话式完成：装工具 → 建/重建 KB → 配 MCP → 验证

**最省事：把下面这段版本固定的「安装口令」整段发给你的 agent**（Claude Code、Codex CLI、
CodeBuddy、Qoder…）。它会用用户级隔离运行时装好**固定版本**的包，再跑 `cosmic_kb bootstrap`
一条龙**装工具 → 建/重建 KB → 注册 MCP → 校验四个工具可用**；你只需在它反问时确认参数、在
终端隐藏输入数据库口令：

<!-- INSTALL-TOKEN:START —— 由 scripts/make_dist.ps1 按版本自动生成，请勿手改 -->
```text
请为当前项目安装并初始化 cosmic-kb==0.1.6。
1) 仅从 https://pypi.org/simple 安装，用 %USERPROFILE%\.cosmic_kb\runtime 用户级隔离环境（不污染系统 Python / 项目 venv）；缺 Python 3.10+ 先征得我同意再装，无 winget 则停止并给我官方安装入口。
2) 装固定版本 cosmic-kb[complete]（含 parse/encoding/mcp/postgres）。
3) 运行该环境里的 cosmic_kb bootstrap plan --project "<当前项目根>" --agent auto --json，把返回的 questions 逐条问我确认。
4) 我确认后运行 cosmic_kb bootstrap apply（按 plan 的参数）：写安装清单 → 装 Skill → 建 KB → doctor → 注册 MCP → 校验 trace/bill/resolve_fields/cosmic_semantics 四工具。
5) 若直连底层库取元数据，加 --db-config 与 --prompt-db-password：数据库口令只能在终端隐藏输入，绝不要我贴进对话，也不写进任何命令/配置/日志。
6) apply 完成后提醒我重启 / 重连 Agent 使 MCP 生效。
```
<!-- INSTALL-TOKEN:END -->

口令里的版本号随每次发版由 `scripts/make_dist.ps1` 自动写入，始终与包版本一致，不用人肉维护。
`bootstrap apply` 幂等、可断点续跑：**重建 KB** 就是原地重跑一次（源码/元数据更新后），已建好的
步骤自动跳过；`bootstrap status` 看当前进度。

**国内无 VPN、访问 `pypi.org` 慢或超时时**：把上面那段安装口令照发给 agent，只需在开头补一句
「请把其中的 `https://pypi.org/simple` 换成国内镜像 `https://pypi.tuna.tsinghua.edu.cn/simple`」，
其余步骤不变（清华，或阿里云 `https://mirrors.aliyun.com/pypi/simple/`、中科大
`https://pypi.mirrors.ustc.edu.cn/simple/`、腾讯云 `https://mirrors.cloud.tencent.com/pypi/simple/`
任一均可）。镜像从 PyPI 同步新版本一般有几分钟~几小时延迟——若提示「找不到 cosmic-kb==<版本>」，
是镜像还没同步到，稍等片刻，或先只用官方源装这一个包（`-i https://pypi.org/simple`）、依赖仍走镜像。

不接 agent、想自己在终端装时，等价命令：

```powershell
pip install "cosmic-kb[complete]" -i https://pypi.tuna.tsinghua.edu.cn/simple
# 想省掉每次 -i，可把镜像设成默认源：
# pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

**两处例外，agent 代劳不了：**

- **数据库口令**：不要把密码打进对话（可能被记录）。让 agent 生成完 `cosmic_db.json` 模板后，
  自己在终端 `$env:COSMIC_DB_PASSWORD = "..."` 并跑一次 `db-meta --check` 确认连通——或直接用
  口令里的 `--prompt-db-password` 在终端隐藏输入，口令不落任何文件。
- **Qoder / Trae 这类只在图形化设置面板里粘贴 MCP JSON 的客户端**：粘贴进设置页这一步没有命令
  行入口，agent 没法替你点鼠标，按 [`接入 agent 与 MCP`](docs/参考手册/接入agent与MCP.md) 手动粘一次即可。

> 包已发布在 PyPI：<https://pypi.org/project/cosmic-kb/>（`pip install cosmic-kb`，国内用上面的镜像）。
> **纯内网、连国内镜像都上不了的离线环境**：先按 [`手动安装详细教程`](docs/参考手册/手动安装详细教程.md)
> 用本仓源码/wheel 装好包（或在有网机器 `pip download "cosmic-kb[complete]" -d ./pkgs -i <镜像>`
> 下好全部 wheel 拷过去 `pip install --no-index --find-links=./pkgs cosmic-kb`），`bootstrap` 之后的
> 建库、注册、校验流程完全一致。想弄清每一步在干什么、或不想用固定口令，也可以让 agent 直接读本
> README + 下面几篇详解照做。

---

## 装好之后怎么用？直接用大白话问你的 agent

安装完成（KB 已建、MCP 已注册、四个工具校验通过）后，**日常用法就一句话：像平时那样用中文
问你的 agent，别自己去背命令**。agent 会自己判断该调 `trace`/`bill`/`resolve_fields`/
`cosmic_semantics` 里的哪个、把返回的证据读成人话回你。你只管问业务问题，比如：

**① 字段级排障——"这个值是谁改的、落没落库"**

```text
cqkd_zkd 这张单的 cqkd_amount 字段是谁改的？改完落库了吗？在源码第几行？
应收单头上的 billstatus 状态字段，有哪些插件会写它、分别在什么事件里触发？
```
> agent 会调 `trace "单据.字段"`，回你「哪个插件类·哪个方法·什么事件·是否落库·源码行号」这条
> 完整证据链，判不准的会如实标 `unknown`，不会替你编。

**② 摸清一张单据——"这张单挂了哪些插件、都在哪些操作上"**

```text
cqkd_zkd 这张单据都绑了哪些插件？每个操作（提交/审核/…）分别触发哪些插件？
```
> agent 会调 `bill "单据标识"`，一次列出表单插件 / 操作集 / 每个操作绑定的插件，不用你在设计器里
> 来回切页面拼。

**③ 核对陌生源码——"这段代码/这个字段到底是什么意思"**

把一段看不懂的苍穹插件源码、或一串英文字段标识丢给 agent：

```text
（贴一段插件源码h让agent读取源码文件）这段代码在干什么？里面的 cqkd_zkd、srctransid 是什么字段？
这个插件绑在哪个单据的哪个操作上？afterCreateNewData 这个事件什么时候触发？
```
> agent 会调 `resolve_fields` 把英文标识核对成真实中文名 + 元数据定义（分录层级、字段类型、
> 下拉枚举的中文含义、基础资料引用指向哪张单据），再调 `cosmic_semantics` 核对苍穹私有插件类型 /
> 事件时机 / 原厂 SDK 用法——防止它拿普通 Java 经验硬猜苍穹私有框架、猜错还不自知。

**几个实用提醒：**

- **源码或元数据更新后要重建 KB**：让 agent 重跑一次 `cosmic_kb bootstrap apply`（幂等，已建步骤自动
  跳过），或直接说「源码更新了，帮我重建 KB」。改了直连底层库的元数据同理。
- **MCP 改动 / 重装后**要**重启或重连 agent** MCP 才生效（bootstrap 完成时也会提醒）。
- **想自己在终端查、不经过 agent**：全部命令见 [`命令行速查`](docs/参考手册/命令行速查.md)，常用的就是
  `cosmic_kb trace/bill/resolve/source/coverage`（`cosmic_kb --help` 看全部）。
- **接好后更细的问法示例**见 [`接入 agent 与 MCP`](docs/参考手册/接入agent与MCP.md)。

---

## 更多文档

**上手四步的详细版**（供你核对 agent 做得对不对，或想自己逐条跑时参考）：

- [`docs/参考手册/手动安装详细教程.md`](docs/参考手册/手动安装详细教程.md) —— 不接 agent 时的完整手动安装图文步骤（venv/激活/pip/可选组/常见报错）
- [`docs/参考手册/建库与更新详解.md`](docs/参考手册/建库与更新详解.md) —— 第一步：直连底层库 / dym-zip 建库、增量重扫与更新逻辑
- [`docs/参考手册/接入agent与MCP.md`](docs/参考手册/接入agent与MCP.md) —— 第二步：各宿主 MCP 配置写法、接好后怎么问、可选一键装 Skill
- [`docs/参考手册/命令行速查.md`](docs/参考手册/命令行速查.md) —— 可选：不接 agent 时自己用命令行查（段一建库 / 段二查库全命令）
- [`docs/参考手册/分发给同事.md`](docs/参考手册/分发给同事.md) —— 打 wheel / 整包 zip 发给同事
- [`docs/参考手册/发版流程.md`](docs/参考手册/发版流程.md) —— 工具改完发新版本：改版本号 → 刷安装口令 → 发 PyPI → 打 tag 全流程

**设计与参考：**

- [`CLAUDE.md`](CLAUDE.md) —— 设计红线、两段式架构、当前进度
- [`docs/参考手册/返回值字段词典.md`](docs/参考手册/返回值字段词典.md) —— 每个工具返回字段的详细含义
- [`docs/参考手册/trace返回详解.md`](docs/参考手册/trace返回详解.md) —— `trace` "完整 vs 截断" 的完整推演
- [`docs/设计方案/分发与多agent接入方案.md`](docs/设计方案/分发与多agent接入方案.md) —— 对话式安装 / 跨 agent 接入的设计决策
- [`docs/核心/开发计划.md`](docs/核心/开发计划.md) / [`docs/核心/阶段验收.md`](docs/核心/阶段验收.md) —— 分阶段交付蓝图与验收记录
- [`cosmic_kb/skills`](cosmic_kb/skills) —— 随 wheel 分发的通用 Agent Skills 与安装器
