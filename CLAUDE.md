# CLAUDE.md — 开局须知（每个新会话先读这个）

> 本文件是与 Claude 协作的"开机引导"。新会话开始时先读本文件，再按需读
> `docs/核心/开发计划.md`（蓝图）和 `docs/核心/阶段验收.md`（进度台账），即可快速回到状态。
> **对话是易失的，文件才是持久记忆**——重要信息一律落盘到这三个文件和代码/测试里。

## 这是什么项目

**苍穹历史项目本地理解工具**：接手陌生的金蝶苍穹（Cosmic）老项目时，在本机上跑的
项目理解工具。两类输入 → 确定性扫描建知识库(KB) → AI 查 KB 理解项目。

- **不是**通用分析库，**是**"接手陌生苍穹老项目时本机跑的理解工具"。
- 两类输入：① 从 globalsdk 导出的元数据 dym/zip；② 公司项目 Java 源码（野生、多模块）。

## 六条硬约束（设计红线，任何改动都要守住）

1. **本地优先**：扫描建库取证全程本机离线完成；接入大模型时，**允许其直接读取本机源码全文以完整理解代码**（不再强制只传最小证据包）。唯一底线是不把 KB / 报告 / 源码发布到公网站点，Web 仅绑 `127.0.0.1`。
2. **代码是"野生的"**：可能不可编译、缺依赖、混合编码(GBK/GB2312/UTF-8±BOM)、多 ISV 前缀。
   解析器**绝不依赖编译或依赖解析**。
3. **规模大**：成百上千文件 → 要性能、进度、缓存、增量重扫。
4. **信任优先**：覆盖率/可信度报告是**一等功能**，不是事后补。
5. **接手者视角**：第一需求是"这项目长什么样"，要先能出项目地图/理解报告。
6. **两段式解耦**：「确定性扫描器(建 KB)」与「AI 理解层(查 KB)」解耦，**KB 是契约**。

派生哲学：**处处置信度 + 证据行号 + unknown**——老项目分析天生不完整，宁可标 unknown 也不臆造。

## 已拍板关键决策

- **ISV / 前缀（影响阶段 2/3/4）**：桥接源码**只认元数据 `<ClassName>` 全限定名**（它一定等于源码包路径），**不靠 ISV/前缀去猜**（ISV 与代码包前缀常对不上）。
  - 阶段 2 解析元数据必须**完整保留 `<ClassName>`**（含包路径），不可只截类名。
  - 区分**两套前缀**：元数据标识前缀（`cqkd_`，管字段/实体，防串实体）与代码包前缀（`cqspb`，管模块归属）——分别建、不混。
  - 模块识别按**代码包路径前缀**聚类，不用 ISV；前缀由工具自动发现，仅作报告产物。

## 架构（两段式）

```
段一 本地确定性扫描器(Python)：Ingestion 摄取 → Metadata 解析 → Java 静态分析 → 桥接
                              → Cosmic KB(SQLite 图谱 + FTS5 + JSON 快照) → 可信度/理解报告
段二 AI 理解层(CLI/MCP)：宿主大模型直调确定性取证工具(trace/bill/resolve_fields/cosmic_semantics)
                        → 挂苍穹 Skill → 带证据的解释
```

包结构 `cosmic_kb/`：`ingest / metadata / java / bridge / graph / semantic / report / cli`。
每个子包的 `__init__.py` 写了职责与计划模块，先读它再动手。

## 当前进度

> 一行一里程碑，**完整功能点表格在 `docs/核心/阶段验收.md`**（按模块分表：功能点/增强内容/状态），
> 本节只放"现在是什么状态"，不重复台账细节。

- ✅ 阶段 0–4：脚手架 + 源码摄取 + 元数据解析(dym/zip/底层库) + 元数据↔源码桥接 + KB 图谱存储 + 本地 Web。
- ✅ 阶段 5–7（旗舰）：字段级排障引擎——字段 → 读写插件+事件函数+是否落库+行号，按坐标分组；Java 语义层持续加固，真实库 form_key NULL 率 60.3%→34.07%。
- ✅ 阶段 10：MCP 封装，工具面 `trace`/`bill`/`resolve_fields`/`cosmic_semantics`（4 个）。
- ✅ 信任优先：`coverage`/`scan-compare`（CLI-only 可信度审计）+ `null_reason`（未定位成因诊断，成因码均附中文标签）。
- 🗑 `read_source`/`method_calls`：曾上线，均已整体退役，改走宿主自带 reader 读源码 + `resolve_fields("实体key.字段key")` 精确核对。
- 🗑 阶段 9 `ask`（NL→意图→确定性证据包）+ 其依附的 `semantic.resolver`/`context.builder`/`plugin_explain`：
  已整体退役（2026-07），改为让宿主大模型直接判断该调 trace/bill/resolve_fields 里的哪个（宿主本就
  比关键词分类器更擅长选工具），KB 收缩回纯确定性字段/分录级取证 + 源码理解辅助两类能力。
- ⬜ 阶段 8（业务流）拍板搁置；阶段 11（增量重扫+GitNexus）待开发。

当前 schema **v16**；`pytest -q` 结果以 codex 最近一次全量执行为准。详细清单/每条验收结论见
`docs/核心/阶段验收.md`。

> ⚠️ MCP server 常驻，改 MCP/取证源码后需**重连/重启 MCP** 才生效；改 schema 后需 `cosmic_kb build` 重建 KB。

## 常用命令（Windows / PowerShell）

```powershell
pip install -e ".[parse,encoding,dev,mcp]"  # 解析+编码+测试+MCP（mcp 可选）
pytest -q                                # 跑测试（当前 460 passed, 4 skipped）
cosmic_kb --version                      # 版本
cosmic_kb doctor                         # 资产体检（需 skill_assets/ok-cosmic-docs.db）
cosmic_kb ingest "<项目源码根>"          # 阶段1：摄取 + 覆盖率/可信度报告（--json 可留档）
cosmic_kb meta "<dym|cr 或整包 zip>"     # 阶段2：解析元数据(含转换规则 .cr)，分类计数/JSON 快照
cosmic_kb bridge "<项目源码根>" "<dym|zip|目录>"  # 阶段3：ClassName↔源码桥接报告（--json）
cosmic_kb build "<项目源码根>" ["<dym|zip|目录> ..."] [--db-config <配置> [--isv <ISV>]]  # 阶段4+5：建 KB（含字段级分析）；给了 --db-config 自动全量同步本项目二开 form/entity/转换规则当前内容（纯 DB 冷启动可省略 dym/zip 参数）
cosmic_kb trace "单据.字段|单据.分录.字段|单据.分录.子分录.字段"  # 旗舰：按层级精确定位字段→谁改了它/事件函数/是否落库（裸字段若跨单据有歧义会反问指定单据，不再聚合列出全部单据证据）
cosmic_kb bill "<单据标识>"              # 单据钻取：操作集/插件/字段触达/风险
cosmic_kb source "<相对源码根的源文件路径>"      # CLI 人工排障：读源码（野生编码正确解码）+ 自动标注其中字段 key 真实中文名（--lines A-B 读区间）；同名跨单据按本文件数据包来源三档消歧（unique/resolved/⚠️ambiguous 别默认当前单据）。段二（AI）已改走宿主自带 reader + resolve_fields，本命令只服务人工终端排障
cosmic_kb coverage                       # 信任优先·手段一：字段覆盖率（元数据为分母）+ 扫描质量分解
cosmic_kb scan-compare                   # 信任优先·手段二：粗精度(源码字面量) vs 高精度(field_access)对比→疑似盲点/精度增量
cosmic_kb dynwrites [--form/--cause/--cls] # 信任优先：字段 key 钉不出的读写（动态循环/拼接/外部常量/歧义/未识别）按「该读方法」去重列出，交段二大模型读源码定性（防爆上下文）
cosmic_kb resolve "<字段/分录/单据标识> ..." [--kind field|entity|form|plugin]  # 标识核对：字段/表头实体/分录/子分录/单据(表单)→真实元数据中文名+坐标（O(1) 打词典，比 trace 便宜；同 key 多坐标全摆出，钉不出回 null，防大模型按命名惯例臆断中文名）；支持复合限定符精确匹配，与 trace 同一套点号坐标写法："单据.字段"/"分录.字段"/"单据.分录.字段"，限定符不含该字段时返回 mismatched_form 诚实提示；--kind 给错种类时返回 mismatched_kind 诚实提示实际种类；支持 --kind plugin：插件类名（简单名/全限定名）→反查绑定单据/操作，替代人工靠 loadSingle/字段坐标/包名/注释猜测
cosmic_kb web                            # 本地浏览器排障（输字段→表格→跳源码；含「扫描可信度」页签：手段一+手段二）
cosmic_kb mcp                            # 阶段10：起 MCP 服务器，把取证命令暴露成 MCP 工具供 LLM 宿主调用（项目根 .mcp.json 自动识别）
```
> 若 `cosmic_kb` 脚本入口不可用，等价用 `python -m cosmic_kb.cli.main ...`。

## 编码与协作约定

- **对用户用简体中文回答**（用户偏好）。
- 代码注释/文档字符串用中文，风格与现有模块一致（务实、可解释，讲清"为什么这么做"）。
- 可选依赖分组放 `pyproject.toml` 的 optional-dependencies，避免一上来装一堆。
- **分工（2026-06-27 起）**：**Claude 负责开发 + 验收文档（`docs/核心/阶段验收.md`）更新**；
  **codex 负责测试 + git 提交**。Claude 改完代码即写/改对应验收结论，不自己跑测试套件、
  不自己 `git commit`；交给 codex 跑 `pytest -q` 与提交。
- 每个新功能仍需配 `tests/` 测试用例（Claude 写用例，codex 负责执行验证不回归）。
- **`docs/核心/阶段验收.md` 用表格记录**（按模块分表：功能点 / 增强内容 / 状态），只记当前状态，
  不留实施过程/返工细节/真实翻车案例复述（那些留在 git log、测试文件、代码注释里）——
  避免台账随时间无限膨胀。功能被后续整体退役/替代时，旧条目收窄成一行「🗑 已退役」+
  一句话去向，不保留历史迭代过程。
- 后续凡是做 `form_key` 识别率优化，测试完成后必须同步刷新
  `docs/设计方案/数据包来源与form_key解析合并.md` 的 **§2「当前识别情况」两张统计表**（总体定位率 + 来源依据分布）。
- **工作纪律：一个阶段 ≈ 一个会话。** Claude 开发 → 写测试用例 → 更新 `docs/核心/阶段验收.md`
  → 用户人工验收 → codex 跑测试 + git 提交 → 开新会话做下一阶段，保持上下文干净。
- 重要决策/架构取舍写进 `docs/`，不要只留在对话里。
