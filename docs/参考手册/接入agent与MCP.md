# 接入你的大模型 agent（第二步：配 MCP + 可选 Skill）

> 本文是 [`README.md`](../../README.md)「对话式完成」里"配 MCP"那一步的详细版——各宿主的手动
> 配置写法、接好后怎么问、以及可选的一键装 Skill。用对话式安装口令时 agent 会替你做完这些。

启动命令 `cosmic_kb-mcp`（stdio）。KB 路径优先级：环境变量 `COSMIC_KB_DB` > 启动目录就近向上
发现 `cosmic_kb.db` > 当前目录。多项目时**给每个项目一份配置、用 `COSMIC_KB_DB` 指到该项目的
KB** 最稳。

- **Claude Code**：项目根已带 [`.mcp.json`](../../.mcp.json)，在该项目里启动 `claude` 自动识别、批准即用；
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

## 怎么在 agent 里用：直接说人话，不用记工具名/命令

接好 MCP 之后，**你不需要知道背后有哪些工具、更不用手敲 `cosmic_kb` 命令**——像跟同事描述
排障需求一样，直接用自然语言跟 agent（Claude Code/Desktop、Cursor…）说话即可，它会自己判断
该调哪个 MCP 工具、要不要读源码、要不要核对中文名。常见问法举例：

- 「`cqkd_amount` 这个金额字段是谁改的、在哪个插件哪个事件、有没有落库？」→ agent 会调 `trace`。
- 「这张 `cqkd_assetcard` 单据有哪些操作和插件，有没有风险点？」→ agent 会调 `bill`。
- 「谁调用了 `ContractService.updateRlateAssets`，它是不是死代码？」→ agent 会调 `callers`，并把
  0 结果与符号覆盖率一起解释；符号层降级时不会据此断言死代码。
- **让 agent 读一段源码、顺手核对里面的中文名**：「帮我看看这个文件/这个类是干什么的，里面涉及
  的单据、分录、字段中文名都是什么」——agent 会一边读源码，一边用 `resolve_fields` 把碰到的英文
  标识核对成元数据里的真实中文名，而不是凭命名习惯自己翻译/瞎猜；如果你怀疑它是不是真的查过、
  还是在凭经验翻译，可以直接追问一句「这个中文名你是查出来的还是猜的？有歧义吗？」，它应该能
  说清楚查证过程，查不到/有歧义也会如实告诉你，不会硬编一个像样的答案。
- 「这个项目的字段扫描覆盖率怎么样，有没有扫不到的地方？」→ agent 会调 `coverage`（信任优先）。

这些都是**普通对话**，不是固定命令模板——只要问题里带着字段/单据/插件这类信息，agent 就有
线索去调工具取证。它会自动调 `trace/bill/resolve_fields/callers/cosmic_semantics` 这 5 个 MCP 工具
取证、带类·方法·行号·三态置信度作答——**苍穹领域纪律（三态置信度、不臆造、入库
判断）已经随 MCP `instructions` 注入宿主，任意 agent 都自带，不需要额外装 Skill。**

## 一键安装 Skills：CodeBuddy / Qoder / TRAE

上面接好 MCP 就已经能带证据、带三态置信度作答——语义纪律随 MCP `instructions` 自动注入宿主，
任意 agent 不装额外东西也能用。CodeBuddy、Qoder 和 TRAE 还可以安装本包自带的两份通用 Skill：

- `cosmic-kb-understand`：字段追溯、插件解释、操作影响和项目理解。
- `cosmic-kb-setup`：安装检查、建库、数据库元数据配置、诊断和 MCP 重连。

- **固定回答模板**：字段排障用"结论先行 → 写入点按置信度排序 → 未定位部分单列 → 下一步
  建议"，插件解释用"结论 → 写入字段 → 风险点 → 下一步建议"，不用每次自己现组织怎么呈现证据。
- **理解工作流顺序建议**：先建项目全貌（`report map`/`report overview`）→ 字段追溯
  （`resolve_fields` 核对标识 → `trace` 取证 → 判事件/入库）→ 插件解释（`bill` 核对绑定 →
  读源码 → 核对字段标识）→ 操作影响（枚举 `bill` 操作集，逐个套字段模板），照着顺序调工具，
  不用自己摸索先查哪个、要不要补哪一步。
- **易错纪律**：入库判断标准（`setValue`/`DynamicObject.set` 不等于入库）、DynamicObject
  路径判定（避免同名字段跨主实体/分录/子分录/基础资料串实体）、多 ISV 前缀不一致时的归属
  判断，都写进了固定纪律里，不靠临场记忆。

这些都是**呈现层增强**，不改变取证工具本身的调用方式，也不是必需——不装 Skill，`trace`/
`bill`/`resolve_fields`/`callers`/`cosmic_semantics` 照样能用，只是回答格式和查询顺序靠 agent 自己临场
组织。Skill 负责编排工作流，领域事实仍以 MCP `instructions` 和 `cosmic_semantics` 为准。

默认自动检测本机宿主并安装到用户级目录：

```powershell
cosmic_kb skill install

# 显式安装全部宿主；CodeBuddy/Qoder 直接写入，TRAE 生成官方 UI 导入包
cosmic_kb skill install --agent all

# 仅当前项目生效
cosmic_kb skill install --agent codebuddy qoder --scope project --project "<项目根>"

# 查看缺失/过期状态
cosmic_kb skill status --agent all

# 卸载两份 Skill
cosmic_kb skill uninstall --agent all
```

同名 Skill 总是更新为当前包内版本。TRAE 没有使用未公开的磁盘目录；命令会生成导入包并打印
`Settings → Rule & Skills → Skills → Create` 的手动导入步骤。Skill 安装不会自动修改 MCP 配置，
仍需按上一节注册 `cosmic_kb` MCP 并重连或重启 Agent。卸载时 CodeBuddy/Qoder 只删除这两份
Skill 的 `SKILL.md`，保留其他 Skill 和同目录下的额外文件；TRAE 会清理导入包，并提示在设置中
手动删除已导入的两份 Skill。可给安装或卸载命令加 `--dry-run` 预览目标。
