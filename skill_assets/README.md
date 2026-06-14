# skill_assets —— 复用资产

段一扫描器与段二 AI 理解层共用的本地资产。**纯本地、不外传**。

| 资产 | 说明 | 来源 |
|------|------|------|
| `ok-cosmic-docs.db` | 苍穹 SDK / 平台符号离线文档库（SQLite）。让工具"认识" `kd.bos.*` 等无源码平台 API。 | 从 ok-cosmic 迁移 |

苍穹语义 references / rules 不重复存放在这里，复用 `../comic-understand-long/references/`
与 `../comic-understand-long/rules/`，由 `cosmic_kb/_assets.py` 统一定位。

校验资产是否就位：

```powershell
cosmic_kb doctor
```
