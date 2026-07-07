# cosmic_kb —— 苍穹老项目本地排障导航工具

> 接手一个陌生的金蝶云苍穹（Cosmic）老项目，本工具**指向它的源码和元数据，纯本地扫一遍**，
> 建成一个知识库（KB）。之后你的 AI agent（Claude Code / Codex / CodeBuddy / Qoder / Trae …）
> 接上这个 KB，就能回答「这个字段是谁改的、在哪个插件的哪个事件函数、改完落不落库、源码第几行」
> 这类问题——所有结论都带类/方法/事件/行号证据，判不准就标 `unknown`，**绝不臆造**。

纯本地运行、不外传源码；专治「老旧」苍穹项目。

---

## 能做什么

核心两件事，对应老项目排障最常见的两个场景：

**① 字段级排障**：字段出了问题（"这个值是谁改的、改完落没落库"），不用自己先从元数据翻出
一堆插件全路径、再逐个肉眼翻源码拼证据——直接查 KB 拿到"插件/方法/事件/是否落库/源码行号"
这条完整证据链。元数据和 Java 源码已经**一起解析进同一份 KB**；扩展单据的原厂继承字段也能
自动补齐，不会结构性半盲（见「第一步」）。

**② 源码解析核对**：反过来，大模型拿到一段陌生源码/插件类（"这段代码是干什么的"），**不能
只凭命名习惯和通用 Java/Spring 经验瞎猜业务含义**——必须能核对清楚：这个插件绑定在哪个单据
的哪个页面上、挂在哪个操作上、什么时候触发；代码里出现的字段/单据英文标识对应的真实中文名
是什么；用到的苍穹私有插件类型/生命周期/SDK 是什么用法。这三层核对靠的是把苍穹这套私有二开
框架的**元数据事实**和**领域知识**（插件类型/事件时机/原厂 SDK 用法）也备进同一份 KB——防止
通用 agent 见到苍穹代码套一套普通 Java 经验硬编业务逻辑，猜错了还不自知。

工具本身**不调用大模型、不下结论**——只产出确定性证据（含苍穹领域语义文档原文），讲成人话
靠你接的 agent（见下）。接手一个陌生苍穹老项目，没有本工具时大概率是这样排障的：

### 场景一：字段级排障

**`trace "单据.字段"`** —— 一次列出所有读写它的插件/方法/事件/是否落库/源码行号。

> 没有时：先从元数据翻出这张单绑定的一堆插件全路径，逐个翻源码肉眼找，还要自己判断落没落
> 库；如果是**跨单据写入**（别的单据插件间接改的），元数据翻不出这层关系，只能全局搜索源码；
> 单据标识魔法值/常量混用，同一张单甚至被建了好几个不同的常量，搜常量名都搜不全，几十个
> 插件翻到崩溃。

### 场景二：源码解析核对

**`bill "单据标识"`** —— 大模型拿到一段源码/插件类时最先要核对的问题就是"它绑定在哪个页面
上、挂在哪个操作上、什么时候触发"；`bill` 一次列出表单插件/操作/每个操作绑定的插件，三类
插件全在一份结果里，直接把源码里看到的插件类对回元数据里的绑定关系，不用凭类名/包名猜它是
干什么用的。agent 默认只拿单据概览+插件绑定（`fields`/`entity_touch` 这两段有专职工具顶替，
默认带出来是冗余），真要看逐字段元数据/按实体分组的读写触达再单独取，不多占上下文。

> 没有时：苍穹设计器里这三处分散在不同页面（插件绑定页/具体操作/列表设计视图），来回切
> 好几个视图才能拼起来，并且大模型不知道插件绑定位置，无法准确定义范围。

**`resolve_fields`** —— agent 读到源码里的字段/分录/单据这类英文标识时，一次核对出真实
中文名 + 元数据定义（所属分录层级、字段类型、下拉/枚举取值的中文含义、基础资料引用字段
指向的目标单据等），查不到/有歧义如实说，绝不瞎编。

> 没有时：大模型读到源码 `cqkd_zkd`、`srctransid` 这类英文标识，只能按命名习惯或拼音瞎猜中文
> 含义和类型（真实翻车：「转款单」被猜成"转账单"）；碰到下拉字段存的 `1`/`2` 这类枚举值、
> 或基础资料字段究竟引用哪张单据，更是只能瞎蒙，容易凭表面写法编一套业务含义。

**`cosmic_semantics`** —— agent 读到源码里的苍穹私有插件类型/生命周期用法时，查权威文档核对：
插件类型、事件触发时机、原厂 SDK 用法、入库判断规则、反模式黑名单。

> 没有时：见到 `AbstractBillPlugIn`、`afterCreateNewData`、`BusinessDataServiceHelper` 这些
> 苍穹私有的插件类型/生命周期/SDK，通用大模型没学过，只能套通用 Java 经验硬猜，猜错也意识
> 不到。

## 谁来读这些证据：你，还是你的 agent？

**推荐做法：接上 agent，直接用自然语言提问**，不用自己跑命令看原始返回。

原因很直接：`trace` 这类查询命中多个插件/坐标时，一次返回就是几十条 JSON（类名、方法名、
行号、置信度、截断游标……），肉眼扫读很累也容易看漏。正常用法是装好 MCP（见下一节），
让 agent 帮你调工具、读证据、组织成"谁改的/在哪/落不落库"这样的中文结论。命令行（`trace`/
`bill`/`resolve`/`web`）仍然保留，适合你想自己写脚本、或不方便接 agent 的场合，但不是日常首选。

---

## 安装（Windows / PowerShell，Python ≥ 3.10）

本工具是个 **Python 命令行程序**（不是 jar/exe）。如果你平时只写 Java，可以按下面的
类比理解每一步——不需要真的懂 Python，跟着敲命令即可。

> **先确认你拿到的是哪种形态，两种装法不能混用：**
> - **A. 一整个源码文件夹**（`git clone` 本仓库，或解压得到的源码包，目录里能看到
>   `pyproject.toml`）—— 按下面 0~4 步来，核心命令是 `pip install -e ".[...]"`。
> - **B. 只有一个 `.whl` 文件**（比如别人用 `python -m build --wheel` 产出、发给你的
>   `cosmic_kb-<版本>-py3-none-any.whl`）—— **跳过下面的 `-e "."` 命令**，改用
>   `pip install "<whl文件路径>[parse,mcp]"`（把方括号 extras 直接接在
>   文件路径后面），不需要整个仓库，也不需要 `pyproject.toml`。详见文末「分发给同事」一节。

### 0）确认电脑上有 Python

```powershell
python --version
```

- 要求 **3.10 及以上**。没装或版本太低，去 <https://www.python.org/downloads/> 下载安装，
  **安装向导里务必勾选 "Add python.exe to PATH"**（不然下面的 `python` / `pip` 命令在终端里找不到，
  这一步等价于 Java 装完 JDK 后要把 `JAVA_HOME`/`bin` 加进 `PATH`）。

### 1）建一个"虚拟环境"（venv）

```powershell
python -m venv .venv
```

- `python -m venv` 是 Python 自带的功能，作用是在当前目录下新建一个**独立、干净的 Python 运行环境**，
  文件夹名叫 `.venv`。
- 为什么要建它：Python 的包默认会装到**全局**，装多个项目容易互相踩版本（类似 Java 里两个项目分别
  要 JDK 8 和 JDK 17，混在一起会打架）。`.venv` 相当于给这个项目单独开一个"沙箱"，装的包只在这个
  沙箱里生效，删掉 `.venv` 文件夹就等于完全卸载、不影响其他项目或系统环境。
- 这一步只需要做一次，`.venv` 文件夹会出现在当前目录下（不用手动打开它）。

### 2）激活虚拟环境

```powershell
.venv\Scripts\Activate.ps1
```

- "激活"的意思是：让接下来在**这个终端窗口**里敲的 `python` / `pip` 命令，都指向刚才建的那个沙箱，
  而不是电脑全局的 Python。激活成功后，命令行提示符前面会多出一个 `(.venv)` 前缀。
- 每次**新开一个终端窗口**、要用本工具之前，都要重新执行一次这条命令（这是当前终端会话级别的开关，
  不是永久设置）。
- 如果报错提示"因为在此系统上禁止运行脚本"，是 Windows 默认拦截了脚本执行，先执行一次：

  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
  ```

  这只放开"当前用户"的脚本执行权限，不影响系统级安全策略；执行完再重新运行激活命令即可。

### 3）安装本工具

```powershell
pip install -e ".[parse,mcp]"
```

> 这条命令只适用于**场景 A**（你手上是整个源码文件夹）。如果你拿到的是**场景 B** 的
> `.whl` 文件，跳过这条命令，改用文末「分发给同事」一节里的 wheel 安装命令。

逐段拆解这条命令（类比 Maven/Gradle 会更好懂）：

- `pip`：Python 的包管理器，相当于 Java 世界的 Maven/Gradle，负责"下载并安装依赖库"。
- `install`：动作是"安装"。
- `-e`：全称 *editable*（可编辑安装）。意思是"不要把代码复制/打包到别处，直接引用当前
  这个文件夹里的源码来运行"。这样以后如果这个工具有源码更新（比如同事发了新版本代码给你），
  你只需要覆盖文件、**不用重新执行 install** 就能生效；反之如果不加 `-e`，行为上更像打成一个
  jar 装进仓库，源码变了要重新装一次。
- `"."`：代表"当前目录"，也就是要安装**当前这个项目自己**（它旁边有个 `pyproject.toml`，相当于
  Java 项目里的 `pom.xml`，声明了这个包叫什么、依赖什么）。
- `"[parse,mcp]"`：方括号里是**可选功能组**（类似 Maven 的 optional dependency /
  profile），逗号分隔，只有列在这里的组才会被装上。下表说明每组是干什么的、要不要装：

| 可选组 | 作用 | 建议 |
|--------|------|------|
| `parse` | Java 静态分析引擎（字段级排障、`trace`/`bill` 等旗舰功能都靠它解析 Java 源码） | **必装** |
| `encoding` | 字符编码自动探测，个别文件不是 UTF-8 时的兜底（现在源码基本都是 UTF-8，用不上也不影响主流程） | 可选 |
| `mcp` | 让本工具能被 AI agent（Claude Code/Desktop、Cursor、Codex…）当作 MCP 工具调用 | **要接 agent 就必装** |
| `dev` | 跑本工具自带的自测（`pytest`），只有你要改本工具源码/验收时才需要 | 仅开发/验收用 |

按需增减方括号里的内容即可，比如只想先跑核心功能不接 agent：`pip install -e ".[parse]"`。

> 这条命令要联网（去 PyPI 下载依赖包），执行时间取决于网速，通常一两分钟。

### 4）验证安装成功

```powershell
cosmic_kb --version                             # 打印版本号，能打印说明命令能找到、装成功了
cosmic_kb doctor                                 # 资产体检：semantics/templates 应显示 OK（随包自带）
```

- `cosmic_kb` 是这个工具安装后注册的命令名（写在 `pyproject.toml` 里，类似 Java 里 jar 包的
  `Main-Class` 入口）。只要上一步 `pip install` 成功、且**当前终端已激活 `.venv`**，就能直接用。
- 若提示找不到 `cosmic_kb` 命令，等价用：`python -m cosmic_kb.cli.main --version`
  （意思是"用 Python 直接运行这个模块"，绕开命令注册，效果一样）。

> **每次开新终端窗口用本工具前，记得先执行第 2 步的激活命令**（`.venv\Scripts\Activate.ps1`），
> 否则 `python`/`cosmic_kb` 又会指回全局环境，可能提示找不到包。

---

## 最快路径：把准备工作交给你的 agent

如果你已经在用**带终端/shell 权限的 AI 编程 agent**（Claude Code、Codex CLI、Cursor、
CodeBuddy CLI 等），上面这些安装命令、以及下面「初始化 KB → 接 MCP」这一整条流水线，都
**不需要你自己逐条敲**——打开这个项目文件夹，让 agent 读到这份 README，跟它说：

> 帮我按这份 README 把 cosmic_kb 装起来：源码根是 `<你的苍穹项目源码路径>`。优先走直连底层库
> 的方式取元数据，底层库只读账号信息我等下补，先帮我生成配置模板；如果暂时申请不到只读账号，
> 再退回用元数据文件夹 `<导出的 dym/cr/zip 所在文件夹>` 这条备选路径。装完帮我把 MCP 也注册好。

它能替你跑完：`pip install` → `db-meta --init-config` → 建库 `build --db-config` → 把 MCP
配置写进这个 agent 自己认的位置（Claude Code 是 `.mcp.json`、Codex 是 `~/.codex/config.toml`
……）。**两处例外，agent 代劳不了**：

- **数据库口令**：不要把密码直接打在对话里（可能被记录）。让 agent 生成完 `cosmic_db.json`
  模板后，自己在终端里执行 `$env:COSMIC_DB_PASSWORD = "..."` 并跑一次 `db-meta --check` 确认
  连通，这一步手动做。
- **Qoder / Trae 这类只在图形化设置面板里粘贴 MCP JSON 的客户端**：粘贴进设置页这一步没有
  命令行入口，agent 没法替你点鼠标——按下方「第二步」手动粘一次即可，这不是本工具的限制，
  是那类客户端本身没开放配置文件接口。

想让 agent 一步步照着做、而不是自己去读长文档，也可以直接把 `scripts/安装说明.md` 甩给它——
内容和下面几节是同一套。

---

## 第一步：初始化你的项目（建 KB）

`build` 需要两样输入：**你的二开 Java 源码根**，和**元数据**。元数据**推荐直连底层库现取**，
登录苍穹开发平台手动导出 dym/zip 只作为没有库权限时的备选方案（见下）。

### 推荐做法：直连底层库，元数据不用手动导出

找底层库管理员申请一个**只读**账号，配好之后 `build` 时**不用给任何 dym/zip 文件**，元数据
（包括你自己的二开单据，也包括扩展单据继承的原厂标准字段）全部由工具自动现取：

```powershell
cosmic_kb db-meta --init-config                      # 生成配置模板 cosmic_db.json，填 host/port/账号/schema
$env:COSMIC_DB_PASSWORD = "..."                       # 口令走环境变量，不写进配置文件
cosmic_kb db-meta --check --config cosmic_db.json     # 测试只读连接

cosmic_kb build "D:\项目源码根" --db-config cosmic_db.json   # meta 位置参数留空，纯连库建库
```

- 连接**强制只读**（多重防线，见 `cosmic_kb/dbmeta/`），只取 `t_meta_formdesign` /
  `t_meta_entitydesign` / `t_botp_convertrule` 三张表的数据，不落公网、结果照旧只进本机 KB——
  不违背"本地优先"红线。
- 为什么推荐这条路而不是手动导出：手动导出的 ISV 包**只含你自己二开的部分**，继承来的原厂
  标准字段根本不在包里，跳过直连库这一步，扩展单据的字段级排障会**结构性半盲**（原厂那部分
  字段连 `unknown` 都标不出来，因为元数据里压根没有这条记录）；直连库能把这两类一次性都补
  齐，还免去每次去开发平台手动导出、解压、归类文件的麻烦。

### 更新逻辑是什么

> **当前限制：不支持增量同步**。本项目自己的二开元数据（`fisv` = 你项目自己的实施方标识，
> 也就是 isv 开头这部分）每次 `build` 都会**全量重新拉取**该 ISV 下当前完整的
> form/entity/转换规则集合，不是"只拉自上次同步以来变更过的部分"。原厂标准单据不受此
> 影响，仍按三类确定性信号**精确发现**代码里引用到的 key 再现取，不会整表拉取库里所有
> 原厂表单。

直连库拉元数据分两类，规则不一样：

| 类别 | 拉取范围 | 更新逻辑 |
|---|---|---|
| **你自己的二开**（form/entity/转换规则，`fisv` = 你项目自己的实施方标识） | 每次 `build` 都拉该 ISV 下**当前完整**的记录集合 | **同 key 整条替换**（不是字段级合并，删掉/改名的字段不会残留），不支持只拉增量变更 |
| **原厂标准单据**（继承母体，如 `bd_customer`） | 每次 `build` 都按三类确定性信号（扩展母体 / ORM 查询 / 操作执行）**自动发现**代码里实际引用到的 key，现取这些 key 的最新值 | 不会不加区分地拉库里所有原厂表单（原厂页面太多，会引入大量噪声）；只精确现取代码里真正引用到的那几个 key |

也就是说：
- **日常重扫**（源码或元数据有更新后），原地重跑同一条命令即可，自己的二开部分每次都会
  全量校验一遍最新内容、原厂部分照旧精确现取：
  ```powershell
  cosmic_kb build "D:\项目源码根" --db-config cosmic_db.json
  ```
- **库里有多个二开 ISV、工具无法唯一确定该同步谁**时会报错，显式指定：
  ```powershell
  cosmic_kb build "D:\项目源码根" --db-config cosmic_db.json --isv <你的ISV标识>
  ```
- **代码里有原厂标准单据被自动发现漏掉**（没被三信号命中），用 `--vendor` 手动追加：
  ```powershell
  cosmic_kb build "D:\项目源码根" --db-config cosmic_db.json --vendor bd_customer bd_supplier
  ```
  想先看会自动拉哪些 key、不实际摄取：
  `cosmic_kb db-meta --discover "D:\项目源码根" --db cosmic_kb.db`。

### 备选方案：本地导出 dym/zip（没有库权限 / 离线环境时用）

拿不到底层库账号（网络隔离、权限申请不下来等），退回到登录苍穹**开发平台**手动导出：

- 找到开发平台的应用导出功能，把你要排障的**二开应用全量导出**，会得到整包 `.zip`。
- 找到单据转换开发导出二开的**转换规则**（单据间取数映射，后缀 `.cr`）——本工具同样支持解析，
  建议**一并全量导出**，覆盖率会更完整。
- 把导出的这些文件（`.dym` / `.cr` / `.zip`，不管几个）统一放进**同一个文件夹**，不用手动
  解压、不用分类。

```powershell
cosmic_kb build "D:\项目源码根" "D:\元数据"
```

- 第一个参数是 Java 源码根；第二个参数直接指向**刚才存放导出文件的那个文件夹**（也可以是单
  个 `.zip` 包路径）——工具会自动扫描识别里面的 `.dym` / `.cr` / 压缩包，不需要你手动区分类型
  或逐个指定。
- **这条路径拿不到原厂标准字段**，扩展单据的排障结论只能诚实标"半盲/unknown"——如果之后能
  申请到只读账号，随时可以在这份 KB 基础上补一句 `--db-config` 重跑 `build`，两条路径可以
  叠加使用，不冲突。
- KB（`cosmic_kb.db`）**默认随源码根落盘**，一项目一库、互不覆盖。项目源码或元数据有更新时，
  重新导出一次、原地重跑 `build` 即可重新扫描（这条路径没有时间戳增量，每次都是用当次导出的
  文件重新解析）。

---

## 第二步：接入你的大模型 agent（推荐的日常用法）

启动命令 `cosmic_kb-mcp`（stdio）。KB 路径优先级：环境变量 `COSMIC_KB_DB` > 启动目录就近向上
发现 `cosmic_kb.db` > 当前目录。多项目时**给每个项目一份配置、用 `COSMIC_KB_DB` 指到该项目的
KB** 最稳。

- **Claude Code**：项目根已带 [`.mcp.json`](.mcp.json)，在该项目里启动 `claude` 自动识别、批准即用；
  或 `claude mcp add cosmic_kb -- cosmic_kb-mcp`。
- **CodeBuddy / Qoder / Trae**（国产 AI 编程工具，同样认通用 `mcpServers` JSON，把下面这段
  粘贴进各自的 MCP 设置里）：

  ```json
  {
    "mcpServers": {
      "cosmic_kb": {
        "command": "cosmic_kb-mcp",
        "env": { "COSMIC_KB_DB": "D:\\你的项目源码根\\cosmic_kb.db" }
      }
    }
  }
  ```
  > **CodeBuddy**（IDE 版）：对话面板右上角「CodeBuddy Settings」→ MCP 标签页 → 右侧
  > 「Add MCP」粘贴 JSON；CLI 版本也支持 `codebuddy mcp add-json --scope project cosmic_kb
  > '{"command":"cosmic_kb-mcp","env":{"COSMIC_KB_DB":"<KB路径>"}}'`，或直接在项目根写
  > `.mcp.json` 文件（CodeBuddy CLI 与 Claude Code 一样会自动识别）。
  > **Qoder**：右上角头像 →「Qoder 设置」→ MCP →「我的服务」→ 右上角「+ 添加」，粘贴 JSON。
  > **Trae**：左下角头像 → 设置 → MCP →「MCP Servers 管理」右上角「创建」→「手动配置」，
  > 粘贴 JSON 后点击「确认」。
  > 若 `cosmic_kb-mcp` 不在 PATH，把 `command` 换成 venv 里的绝对路径，或用
  > `"command": "python", "args": ["-m","cosmic_kb.cli.main","mcp","--db","<KB路径>"]`。

- **Codex**（`~/.codex/config.toml`）：

  ```toml
  [mcp_servers.cosmic_kb]
  command = "cosmic_kb-mcp"
  env = { COSMIC_KB_DB = "D:\\你的项目源码根\\cosmic_kb.db" }
  ```

### 怎么在 agent 里用：直接说人话，不用记工具名/命令

接好 MCP 之后，**你不需要知道背后有哪些工具、更不用手敲 `cosmic_kb` 命令**——像跟同事描述
排障需求一样，直接用自然语言跟 agent（Claude Code/Desktop、Cursor…）说话即可，它会自己判断
该调哪个 MCP 工具、要不要读源码、要不要核对中文名。常见问法举例：

- 「`cqkd_amount` 这个金额字段是谁改的、在哪个插件哪个事件、有没有落库？」→ agent 会调 `trace`。
- 「这张 `cqkd_assetcard` 单据有哪些操作和插件，有没有风险点？」→ agent 会调 `bill`。
- **让 agent 读一段源码、顺手核对里面的中文名**：「帮我看看这个文件/这个类是干什么的，里面涉及
  的单据、分录、字段中文名都是什么」——agent 会一边读源码，一边用 `resolve_fields` 把碰到的英文
  标识核对成元数据里的真实中文名，而不是凭命名习惯自己翻译/瞎猜；如果你怀疑它是不是真的查过、
  还是在凭经验翻译，可以直接追问一句「这个中文名你是查出来的还是猜的？有歧义吗？」，它应该能
  说清楚查证过程，查不到/有歧义也会如实告诉你，不会硬编一个像样的答案。
- 「这个项目的字段扫描覆盖率怎么样，有没有扫不到的地方？」→ agent 会调 `coverage`（信任优先）。

这些都是**普通对话**，不是固定命令模板——只要问题里带着字段/单据/插件这类信息，agent 就有
线索去调工具取证。它会自动调 `trace/bill/resolve_fields/cosmic_semantics` 这 4 个 MCP 工具
取证、带类·方法·行号·三态置信度作答——**苍穹领域纪律（三态置信度、不臆造、入库
判断）已经随 MCP `instructions` 注入宿主，任意 agent 都自带，不需要额外装 Skill。**

> Claude Code 用户如果想多要一层增强（更结构化的排障模板），可以手动把
> [`comic-understand-long/`](comic-understand-long/SKILL.md) 复制到 `.claude/skills/` 下，
> 详见 `scripts/安装说明.md` §7；这是可选项，不装也不影响上面的问答能力。

---

## （可选）自己用命令行查

按"建库"和"查库"两段整理，对应设计上的两段式解耦——段一确定性扫描建 KB，段二只读 KB 查证据。

### 段一：确定性扫描器（建 KB）

| 命令 | 作用 |
|---|---|
| `cosmic_kb ingest <源码根>` | 只做源码摄取 + 解析可信度报告（不建库） |
| `cosmic_kb meta <dym\|cr\|zip>` | 只解析元数据，看分类计数/JSON 快照 |
| `cosmic_kb bridge <源码根> <元数据>` | 元数据 `<ClassName>` ↔ 源码桥接命中率报告 |
| `cosmic_kb build <源码根> <元数据>` | **主入口**：摄取 + 解析 + 桥接 + Java 字段级静态分析，产出 KB |
| `cosmic_kb db-meta` | 只读直连底层库，补齐 ISV 元数据缺的原厂标准字段（`--init-config`/`--check`/`--discover`） |
| `cosmic_kb doctor` | 资产体检（semantics/templates 是否随包 OK） |

### 段二：查 KB 排障

| 命令 | 作用 |
|---|---|
| `cosmic_kb trace "单据.[分录.[子分录.]]字段"` | **旗舰**：字段→谁读/写了它、哪个事件函数、是否落库、源码行号 |
| `cosmic_kb bill "单据标识"` | 单据钻取：表单/操作/列表插件绑定一次列全 + 字段触达/风险点 |
| `cosmic_kb resolve <标识>...` | 字段/表头实体/分录/子分录/单据(表单)→真实中文名+坐标（堵命名惯例瞎猜，钉不出回 `null`） |
| `cosmic_kb report map` | 项目地图：多信号模块识别 + 包结构健康度 |
| `cosmic_kb report overview` | 排障概览：字段级定位入口/规模/风险热点 |
| `cosmic_kb source <相对源文件路径>` | 人工终端读源码（野生编码正确解码 + 自动标注字段中文名）；仅 CLI，段二 agent 走 MCP 已改用宿主自带 reader + `resolve_fields` |
| `cosmic_kb coverage` | **信任优先**：以元数据为分母的字段覆盖率 + 扫描质量分解 |
| `cosmic_kb scan-compare` | **信任优先**：粗扫(字面量) vs 高精度扫描对比→疑似盲点/精度增量 |
| `cosmic_kb dynwrites` | **信任优先**：字段 key 钉不出的动态读写，按"该读方法"去重列出 |
| `cosmic_kb web` | 本地浏览器（仅 `127.0.0.1`）：输字段→表格→跳源码，含「扫描可信度」页签 |

`coverage`/`scan-compare`/`dynwrites`/`source` 只在 CLI 提供；MCP 只精简暴露给 agent 4 个主
路径工具（`trace`/`bill`/`resolve_fields`/`cosmic_semantics`），见上一节。

命令行输出的是原始证据，字段较多、需要自己对照理解（含义见
[`docs/参考手册/返回值字段词典.md`](docs/参考手册/返回值字段词典.md)）；`cosmic_kb --help` / `cosmic_kb <子命令> --help`
看完整参数。日常排障还是建议走上一节的 agent 方式。

---

## 分发给同事

本工具已是**自包含包**（语义文档、模板等随包），可当普通 wheel 分发：

```powershell
pip install build
python -m build --wheel     # 产出 dist\cosmic_kb-<版本>-py3-none-any.whl
```

对方 `pip install "cosmic_kb-<版本>-py3-none-any.whl[parse,mcp]"` 即可，
不需要整仓库。离线/内网场景的整包 zip 兜底见 `scripts/make_dist.ps1` + `scripts/安装说明.md`。

---

## 更多文档

- [`CLAUDE.md`](CLAUDE.md) —— 设计红线、两段式架构、当前进度
- [`docs/参考手册/返回值字段词典.md`](docs/参考手册/返回值字段词典.md) —— 每个工具返回字段的详细含义
- [`docs/参考手册/trace返回详解.md`](docs/参考手册/trace返回详解.md) —— `trace` "完整 vs 截断" 的完整推演
- [`docs/设计方案/分发与多agent接入方案.md`](docs/设计方案/分发与多agent接入方案.md) —— 分发/跨 agent 接入的设计决策
- [`docs/核心/开发计划.md`](docs/核心/开发计划.md) / [`docs/核心/阶段验收.md`](docs/核心/阶段验收.md) —— 分阶段交付蓝图与验收记录
- [`comic-understand-long/SKILL.md`](comic-understand-long/SKILL.md) —— Claude Code Skill 增强入口
