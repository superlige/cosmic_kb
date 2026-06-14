# 苍穹 AI 工程知识底座 (cqkd_ai)

> **定位**：不是"一个分析库"，而是 **"接手陌生苍穹老项目时，在本机上跑的项目理解工具"**。
> 纯本地、不外传；面向多模块、可能不可编译、中文编码、多 ISV 前缀的金蝶云苍穹历史项目。

接手一个苍穹老项目时，本工具帮你回答：这项目干嘛的、有哪些模块、某字段谁改的、
某 bug 先查哪、改一处影响哪 —— **所有结论带类/方法/事件/行号证据与置信度，不臆造。**

详见 [`项目企划.md`](docs/项目企划.md)（9 层目标架构）与 [`开发计划.md`](docs/开发计划.md)（分阶段交付）。

---

## 两段式架构（KB 是契约）

```
段一：本地确定性扫描器  cosmic_kb/（Python 包）
      摄取 → 元数据解析 → Java 静态分析 → 桥接 → SQLite KB + 覆盖率/理解报告
                          ↓  KB 是两段之间的契约
段二：AI 理解层  comic-understand-long/（苍穹理解 Skill）
      查 KB 取证 → 挂苍穹语义(references/rules) → 输出带证据的解释 / 排查建议
```

- **段一 `cosmic_kb`**：纯本地、确定性地建 KB。绝不依赖编译或依赖解析。
- **段二 `comic-understand-long`**：Claude/Codex 调用的苍穹**理解** Skill（非代码生成），
  提供语义解释规则与现成只读取证脚本。详见 [`comic-understand-long/SKILL.md`](comic-understand-long/SKILL.md)。

---

## 目录结构

真实产品在根目录；开发计划、示例、上游原件分别归入 `docs/ samples/ vendor/`。

```
cqkd_ai/
│  # ── 真实产品 ──────────────────────────────
├── pyproject.toml            # cosmic_kb 可安装包定义（pip install -e .）
├── README.md                 # 本文件
│
├── cosmic_kb/                # 段一：确定性扫描器（骨架按开发阶段分包）
│   ├── cli/                  #   命令行入口（cosmic_kb --version / doctor / plan）
│   ├── _assets.py            #   skill_assets 资产定位
│   ├── ingest/               # 阶段1  源码摄取（扫目录 / 编码探测）
│   ├── metadata/             # 阶段2  元数据解析（dym / 整包 zip）
│   ├── java/                 # 阶段1/5-7  Java 静态分析 / 路径追踪 / 入库
│   ├── bridge/               # 阶段3  元数据 ↔ 代码桥接
│   ├── graph/                # 阶段4/8  SQLite 图谱 + 业务流
│   ├── semantic/             # 阶段9  NL→意图 / 模糊候选
│   ├── context/              # 阶段9  Context Builder
│   └── report/               # 阶段1/4  覆盖率 + 项目地图 / 理解报告
│
├── comic-understand-long/    # 段二：苍穹理解 Skill
│   ├── SKILL.md              #   AI 操作手册（理解纪律 / 工作流 / 语义路由）
│   ├── references/           #   苍穹插件/SDK 语义文档（已迁移）
│   ├── rules/                #   anti-patterns 幻觉黑名单
│   ├── scripts/              #   只读取证脚本（refs / scan-field / api / meta）
│   └── .cosmic-understand/   #   配置（config.example.json → config.json）
│
├── skill_assets/             # 复用资产：ok-cosmic-docs.db（SDK 离线文档库）
├── tests/                    # pytest 冒烟测试
│
│  # ── 计划 / 示例 / 上游（非产品）─────────────
├── docs/                     # 基准文档：项目企划.md、开发计划.md
├── samples/                  # 示例 dym / 整包 zip（苍穹元数据示例）
└── vendor/                   # 上游 ok-cosmic 原件（迁移来源，参考用）
    ├── ok-cosmic/            #   解压后的上游 Skill
    └── ok-cosmic-main.zip
```

---

## 安装与冒烟（阶段 0 验收）

需 Python ≥ 3.10。在项目根执行：

```powershell
python -m pip install -e .

cosmic_kb --version    # → cosmic_kb 0.1.0      （验收点）
cosmic_kb doctor       # 检查 skill_assets 资产接线
cosmic_kb plan         # 列出规划中的子命令与阶段
```

跑测试：

```powershell
python -m pytest          # 或： pip install -e ".[dev]" 后 pytest
```

> 阶段 0 不引入任何运行期硬依赖：`--version` / `doctor` 必须零依赖可跑。
> tree-sitter / rapidfuzz 等随阶段经 `pip install -e ".[parse]"` 等 extras 按需引入。

---

## 段二 Skill 的现有命令（阶段 0 已迁移）

理解层的只读取证脚本，入口 `comic-understand-long/scripts/cqkd_cosmic_understand.py`：

```powershell
cd comic-understand-long

python scripts\cqkd_cosmic_understand.py doctor                 # Skill 资产自检
python scripts\cqkd_cosmic_understand.py refs list              # 列出语义参考主题
python scripts\cqkd_cosmic_understand.py refs read dynamic-object
python scripts\cqkd_cosmic_understand.py scan-field --field-key cqkd_mortgagestatus --project-root <项目根>
```

### 启用 `api` / `meta`（SDK 文档查询 / 元数据查询）

这两条依赖 SDK 文档库与配置：

1. 复制配置模板：`comic-understand-long/.cosmic-understand/config.example.json` → `config.json`，
   把 `defaultProjectRoot` 改成你的苍穹项目根。
2. SDK 文档库 `ok-cosmic-docs.db` 已放在 `skill_assets/`；如脚本需要 `ok-cosmic.json`，
   在 `.cosmic-understand/` 下按 `graph.dbName` 指向该库（参考 `vendor/ok-cosmic/setup/ok-cosmic.json`）。
3. 之后即可：
   ```powershell
   python scripts\cqkd_cosmic_understand.py api search BusinessDataServiceHelper
   python scripts\cqkd_cosmic_understand.py meta get --form-id cqkd_assetcard --fuzzy mortgage
   ```

---

## 路线图

| 里程碑 | 阶段 | 标志 |
|-------|------|------|
| **M0 脚手架** | 0 | ✅ `cosmic_kb` 包骨架 + 资产复用 + `--version` 冒烟 + Skill 文档 |
| **M1 信任地基** | 1–4 | 指向乱目录 → 证明解析可信 → 出项目地图与理解报告 |
| **M2 行为分析** | 5–8 | Java 行为 + 路径追踪 + 入库 + 业务流 |
| **M3 AI 问答** | 9–10 | 端到端自然语言问答 + MCP |
| **M4 增强** | 11 | 增量重扫 + GitNexus 调用链 |

阶段拆分与验收命令详见 [`开发计划.md`](docs/开发计划.md)。

---

## 硬约束（贯穿全程）

本地离线 · 代码"野生"不依赖编译 · 规模大要性能/增量 · 信任优先（覆盖率报告是一等功能）·
接手者视角（先出项目地图）· 两段式解耦（KB 是契约）· 处处置信度 + 证据行号 + `unknown`。
