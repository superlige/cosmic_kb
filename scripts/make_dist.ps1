#requires -Version 5.1
<#
.SYNOPSIS
  把 cosmic_kb 打包成可分发的离线 zip。

.DESCRIPTION
  cosmic_kb 已能构建自包含 wheel；本脚本保留源码整包分发方式，供离线/内网环境使用。

  本脚本做四件事：
    1. 校验关键资产确实被 git 跟踪（否则 archive 不会带上它们）；
    2. 用 git archive 导出 HEAD 的全部已提交内容（自动排除 *.db 等未跟踪/忽略产物，
       但已强制跟踪的 ok-cosmic-docs.db 会保留）；
    3. 注入面向接收者的「安装说明.md」；
    4. 压成 dist\cosmic_kb_dist_v<版本>_<日期>.zip，并打印资产清单。

.PARAMETER OutputDir
  zip 输出目录（默认仓库根的 dist\）。

.PARAMETER AllowDirty
  允许工作区有未提交改动时打包。默认拒绝——因为 git archive 只导出 HEAD，
  未提交的改动不会进包，容易给对方发出「旧版本」。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\make_dist.ps1
#>
param(
    [string]$OutputDir = "dist",
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"

function Fail($msg) { Write-Host "✗ $msg" -ForegroundColor Red; exit 1 }

# ── 版本固定安装口令：随发版自动生成、写回 README / 安装说明.md（不再人肉维护版本占位符） ──
# 单引号 here-string 逐字保留（含 ``` 与反斜杠），用 __VERSION__ 占位再替换成真实版本。
function New-InstallTokenBlock([string]$Ver) {
    $template = @'
<!-- INSTALL-TOKEN:START —— 由 scripts/make_dist.ps1 按版本自动生成，请勿手改 -->
```text
请为当前项目安装并初始化 cosmic-kb==__VERSION__。
1) 仅从 https://pypi.org/simple 安装，用 %USERPROFILE%\.cosmic_kb\runtime 用户级隔离环境（不污染系统 Python / 项目 venv）；缺 Python 3.10+ 先征得我同意再装，无 winget 则停止并给我官方安装入口。
2) 装固定版本 cosmic-kb[complete]（含 parse/encoding/mcp/postgres）。
3) 运行该环境里的 cosmic_kb bootstrap plan --project "<当前项目根>" --agent auto --json，把返回的 questions 逐条问我确认。
4) 我确认后运行 cosmic_kb bootstrap apply（按 plan 的参数）：写安装清单 → 装 Skill → 建 KB → doctor → 注册 MCP → 校验 trace/bill/resolve_fields/cosmic_semantics 四工具。
5) 若直连底层库取元数据，加 --db-config 与 --prompt-db-password：数据库口令只能在终端隐藏输入，绝不要我贴进对话，也不写进任何命令/配置/日志。
6) apply 完成后提醒我重启 / 重连 Agent 使 MCP 生效。
```
<!-- INSTALL-TOKEN:END -->
'@
    return $template.Replace('__VERSION__', $Ver)
}

# 把 $Path 里 INSTALL-TOKEN 标记对之间的内容整块替换成 $Block；改动过返回 $true。
# 纯字符串切片（不走正则），避免口令里的 $ / 反斜杠被当成替换转义。
function Update-InstallToken([string]$Path, [string]$Block) {
    if (-not (Test-Path $Path)) { Fail "找不到待写回安装口令的文件：$Path" }
    $text     = Get-Content $Path -Raw
    # 按目标文件的换行风格规整口令块，避免只因 CRLF/LF 差异就把整块判成"有改动"（否则每次打包都会脏树）
    $lf = ($Block -replace "`r`n", "`n")
    if ($text.Contains("`r`n")) { $Block = $lf -replace "`n", "`r`n" } else { $Block = $lf }
    $startTag = '<!-- INSTALL-TOKEN:START'
    $endTag   = '<!-- INSTALL-TOKEN:END -->'
    $i = $text.IndexOf($startTag)
    $j = $text.IndexOf($endTag)
    if ($i -lt 0 -or $j -lt 0 -or $j -lt $i) {
        Fail "$Path 缺少 INSTALL-TOKEN 标记对（<!-- INSTALL-TOKEN:START ... END -->），无法写回安装口令。"
    }
    $j += $endTag.Length
    $updated = $text.Substring(0, $i) + $Block + $text.Substring($j)
    if ($updated -ne $text) {
        # UTF-8 无 BOM 写回，避免污染 markdown
        [System.IO.File]::WriteAllText($Path, $updated, (New-Object System.Text.UTF8Encoding($false)))
        return $true
    }
    return $false
}

# ── 1. 定位仓库根（脚本位于 scripts\ 下），切过去 ────────────────────────────
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) { Fail "当前目录不是 git 仓库：$RepoRoot" }

# ── 2. 读版本号（cosmic_kb/__init__.py 的 __version__） ──────────────────────
$initText = Get-Content "cosmic_kb/__init__.py" -Raw
if ($initText -match '__version__\s*=\s*"([^"]+)"') {
    $Version = $Matches[1]
} else {
    Fail "无法从 cosmic_kb/__init__.py 解析 __version__"
}

# ── 2.5 按当前版本刷新安装口令，写回源仓 README / 安装说明.md ────────────────
# 幂等：口令已是当前版本则不动；版本刚 bump 过则改写，随后第 4 步脏检查会要求先提交再打包，
# 保证 archive(HEAD) 里 README 的口令与包版本一致。
$TokenBlock = New-InstallTokenBlock $Version
foreach ($f in @((Join-Path $RepoRoot "README.md"), (Join-Path $PSScriptRoot "安装说明.md"))) {
    if (Update-InstallToken $f $TokenBlock) {
        Write-Host "  已按版本 $Version 刷新安装口令：$f" -ForegroundColor Cyan
    }
}

# ── 3. 校验关键资产被 git 跟踪（archive 只导出 tracked 文件） ────────────────
$required = @(
    "pyproject.toml",
    "skill_assets/ok-cosmic-docs.db",
    "cosmic_kb/skills/cosmic-kb-understand/SKILL.md",
    "cosmic_kb/skills/cosmic-kb-setup/SKILL.md"
)
foreach ($f in $required) {
    git ls-files --error-unmatch $f *> $null
    if ($LASTEXITCODE -ne 0) {
        Fail "关键资产未被 git 跟踪，导出包会缺它：$f`n  （提示：git add -f $f 后再打包）"
    }
}

# ── 4. 工作区脏检查（archive 只导出 HEAD） ──────────────────────────────────
$dirty = git status --porcelain
if ($dirty -and -not $AllowDirty) {
    Write-Host "工作区有未提交改动（git archive 只会导出已提交的 HEAD）：" -ForegroundColor Yellow
    Write-Host $dirty
    Fail "请先 commit，或加 -AllowDirty 明确忽略未提交改动后再打包。"
}

# ── 5. git archive 导出 HEAD → 解到临时 staging 目录 ────────────────────────
$stamp     = Get-Date -Format "yyyyMMdd"
$leafName  = "cosmic_kb_v$Version"      # 解压后对方看到的顶层文件夹名
$stageRoot = Join-Path $env:TEMP ("cqkd_dist_" + [System.Guid]::NewGuid().ToString("N"))
$stageDir  = Join-Path $stageRoot $leafName

try {
    New-Item -ItemType Directory -Path $stageDir -Force | Out-Null

    $tarPath = Join-Path $stageRoot "src.tar"
    git archive --format=tar -o $tarPath HEAD
    if ($LASTEXITCODE -ne 0) { Fail "git archive 失败" }
    tar -xf $tarPath -C $stageDir
    if ($LASTEXITCODE -ne 0) { Fail "tar 解包失败（确认 Windows 自带 tar 可用）" }
    Remove-Item $tarPath -Force

    # ── 6. 校验导出树确实含关键资产，并体检 db 体积 ────────────────────────
    foreach ($f in $required) {
        if (-not (Test-Path (Join-Path $stageDir $f))) { Fail "导出包内缺失：$f" }
    }
    $dbItem = Get-Item (Join-Path $stageDir "skill_assets/ok-cosmic-docs.db")
    if ($dbItem.Length -lt 1MB) {
        Write-Host "⚠ ok-cosmic-docs.db 仅 $($dbItem.Length) 字节，疑似 LFS 指针或损坏。" -ForegroundColor Yellow
    }

    # ── 7. 注入面向接收者的安装说明 ────────────────────────────────────────
    $installDoc = Join-Path $PSScriptRoot "安装说明.md"
    if (-not (Test-Path $installDoc)) { Fail "缺少 scripts\安装说明.md（应与本脚本同目录）" }
    Copy-Item $installDoc (Join-Path $stageDir "安装说明.md") -Force

    # 兜底：staging 的 README 来自 archive(HEAD)，用 -AllowDirty 打包时口令可能还没提交 →
    # 直接把 staging 内 README/安装说明.md 的口令刷成当前版本，保证发出去的包版本一致。
    foreach ($f in @((Join-Path $stageDir "README.md"), (Join-Path $stageDir "安装说明.md"))) {
        if (Test-Path $f) { [void](Update-InstallToken $f $TokenBlock) }
    }

    # ── 8. 压成 zip（顶层带 cosmic_kb_v<版本>\ 文件夹，对方解压不散落） ──────
    $outDirFull = Join-Path $RepoRoot $OutputDir
    New-Item -ItemType Directory -Path $outDirFull -Force | Out-Null
    $zipPath = Join-Path $outDirFull "cosmic_kb_dist_v${Version}_${stamp}.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path $stageDir -DestinationPath $zipPath -CompressionLevel Optimal

    # ── 9. 汇总 ────────────────────────────────────────────────────────────
    $fileCount = (Get-ChildItem $stageDir -Recurse -File).Count
    $zipSize   = "{0:N1} MB" -f ((Get-Item $zipPath).Length / 1MB)
    $dbSize    = "{0:N1} MB" -f ($dbItem.Length / 1MB)

    Write-Host ""
    Write-Host "✓ 打包完成" -ForegroundColor Green
    Write-Host "  版本：  $Version"
    Write-Host "  输出：  $zipPath"
    Write-Host "  文件：  $fileCount 个，压缩后 $zipSize"
    Write-Host "  资产：  ok-cosmic-docs.db ($dbSize)、两份 Agent Skill、semantics 已含"
    Write-Host "  说明：  安装说明.md 已注入包根目录"
    Write-Host ""
    Write-Host "把这个 zip 发给对方；对方解压后进入 $leafName\ 目录，按「安装说明.md」操作即可。"
}
finally {
    if (Test-Path $stageRoot) { Remove-Item $stageRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
