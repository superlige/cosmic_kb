---
name: cosmic-kb-setup
description: "安装、初始化、重建、更新和诊断金蝶云苍穹项目的 cosmic_kb。用于检查 Python 与 cosmic_kb 命令、通过只读数据库或 dym/cr/zip 元数据构建 KB、生成和检查 cosmic_db.json、运行 doctor/coverage、定位 KB 不存在或版本不符、配置或检查 MCP 并提醒重连。不要用于解释字段读写、插件行为、操作影响或业务链路；KB 和 MCP 可用后的理解任务使用 cosmic-kb-understand。"
---

# Cosmic KB 安装与初始化

把当前苍穹项目准备成可由 Agent 取证的本地 KB。完成安装检查、元数据选择、建库、诊断和 MCP 接入；不要在本 Skill 中解释业务字段或插件行为。

## 安全边界

1. 在本机处理源码、元数据、KB 和配置，不要上传公司代码或报告。
2. 数据库连接必须使用只读账号，只读取工具支持的元数据表。
3. 不要要求用户在对话中发送数据库口令，也不要替用户把口令写进命令、配置或日志。
4. 让用户在自己的终端设置 `COSMIC_DB_PASSWORD`，并避免回显其值。
5. 重建前确认目标 KB 路径。默认原地更新当前项目 KB，不创建来源不明的临时库。
6. 命令参数以当前 `cosmic_kb --help` 和子命令帮助为准；遇到版本差异先检查帮助，不要猜参数。

## 固定流程

### 1. 检查环境

1. 确认当前工作目录是用户要处理的苍穹项目根或明确记录项目根路径。
2. 运行 `python --version`，要求 Python 3.10 或更高版本。
3. 运行 `cosmic_kb --version`。命令不存在时，根据当前分发物安装带解析和 MCP 能力的包。
4. 运行 `cosmic_kb --help`，确认 `build`、`db-meta`、`doctor` 和 MCP 入口存在。
5. 检查项目内已有的 `cosmic_kb.db`、`cosmic_db.json` 和 MCP 配置，避免无意覆盖用户自定义路径。

### 2. 选择元数据来源

优先使用只读数据库，因为它能补齐扩展单据继承的原厂字段。用户没有数据库权限时，使用本地导出的二开应用全量 dym/zip 和转换规则 cr。

只读数据库路径需要：

- Java 源码根目录。
- 数据库 host、port、schema 和只读账号。
- 用户在终端自行设置的 `COSMIC_DB_PASSWORD`。
- 多 ISV 无法唯一确定时，由工具列出候选后显式选择 ISV。

本地文件路径需要：

- Java 源码根目录。
- 包含 dym、cr、zip 的目录或 zip 路径。
- 明确告知用户：缺少数据库元数据时，原厂标准字段可能形成半盲区，相关结论会降为 `unknown`。

### 3. 使用只读数据库建库

1. 需要新配置时运行：

   ```powershell
   cosmic_kb db-meta --init-config
   ```

2. 根据用户提供的非敏感连接信息填写 `cosmic_db.json`，不要填写口令。
3. 要求用户在自己的终端执行：

   ```powershell
   $env:COSMIC_DB_PASSWORD = "<由用户自行输入>"
   ```

4. 用户设置完成后检查只读连通性：

   ```powershell
   cosmic_kb db-meta --check --config cosmic_db.json
   ```

5. 连通后构建：

   ```powershell
   cosmic_kb build "<Java源码根>" --db-config cosmic_db.json
   ```

6. 工具报告多个 ISV、缺失原厂单据或其他可操作候选时，按实际提示补充参数后重跑，不要自行猜选。

### 4. 使用导出文件建库

确认导出目录包含可用的 dym、cr 或 zip，然后运行：

```powershell
cosmic_kb build "<Java源码根>" "<导出文件目录或zip路径>"
```

源码或元数据更新时，对同一项目和 KB 路径原地重跑。以后获得只读数据库权限时，可以改用数据库配置重建以提高覆盖率。

### 5. 校验 KB

1. 确认预期 KB 文件存在且不是零字节。
2. 运行：

   ```powershell
   cosmic_kb doctor
   ```

3. 根据 `doctor` 的真实输出报告随包资产和接线问题；不要把 `doctor` 成功扩大解释成 schema、解析质量或字段覆盖率均已通过。
4. 仅当用户关心覆盖率、扫描盲点或建库质量时运行：

   ```powershell
   cosmic_kb coverage
   ```

5. 若需要更深入的扫描质量审计，再按用户目标选择 `scan-compare` 或 `dynwrites`，不要作为每次安装的强制步骤。

### 6. 配置并检查 MCP

1. 确认 MCP server 启动命令使用当前环境里可执行的 `cosmic_kb` 或 Python 模块入口。
2. 确认 MCP 指向刚验证的 KB；使用非默认路径时设置 `COSMIC_KB_DB` 或相应 `--db` 参数。
3. 根据当前 Agent 支持的项目配置文件或设置界面注册 `cosmic_kb` MCP。不要假定所有 Agent 使用同一个配置路径。
4. 能进行实际调用时，至少检查工具列表包含 `resolve_fields`、`trace`、`bill` 和 `cosmic_semantics`。
5. 首次建库、重建 KB 或修改 MCP 代码后，明确提醒用户重连 MCP 或重启当前 Agent；常驻进程不会自动加载新内容。

## 故障处理

- `cosmic_kb` 命令不存在：确认虚拟环境是否激活、安装是否成功、Scripts 目录是否在 PATH。
- KB 不存在或版本不符：核对工作目录和 `COSMIC_KB_DB`，随后对目标项目重新执行 `build`。
- 数据库检查失败：报告准确错误文本，区分网络、认证、schema、权限和驱动问题；不要让用户公开口令。
- 元数据为空或覆盖率异常：核对 ISV、schema、源码根和导出文件范围，再决定是否重建。
- MCP 能启动但没有新数据：先确认它指向的 KB 路径，再重连或重启 MCP。
- Agent 看不到 MCP：检查宿主实际加载的配置位置、启动命令和工作目录，不要仅检查配置文件是否存在。

## 结果格式

```text
安装状态：<成功/部分成功/失败>
项目根：<绝对路径>
源码根：<绝对路径>
元数据来源：<只读数据库/本地 dym-cr-zip>
KB 路径：<绝对路径>

校验结果：
  - Python：<版本与状态>
  - cosmic_kb：<版本与状态>
  - build：<结果摘要>
  - doctor：<结果摘要>
  - coverage：<按需填写或省略>
  - MCP：<已注册/待用户配置/连接失败>

未完成/存疑：
  - <权限、路径、覆盖率或连接问题；没有写“无”>

用户必须执行：
  - <设置口令、粘贴配置、重连 MCP 等；没有写“无”>

下一步：<重连后可提出的字段或插件理解问题>
```

## 完成检查

- 没有在对话、配置或日志中暴露数据库口令。
- 元数据来源、源码根和 KB 目标路径均已明确。
- `build` 后实际运行了 `doctor`，没有只检查文件存在。
- MCP 指向已验证的 KB，并已提示重连或重启。
- 没有在 setup 阶段臆测字段、插件或业务链路；后续理解任务交给 `cosmic-kb-understand`。
