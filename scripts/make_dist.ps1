#requires -Version 5.1
<#
.SYNOPSIS
  把 cosmic_kb 打包成可分发的 zip（方案1：整包分发 + 可编辑安装）。

.DESCRIPTION
  本工具靠目录布局定位资产：skill_assets/ 与 comic-understand-long/ 必须是 cosmic_kb/
  的同级目录（cosmic_kb/_assets.py 用 parents[1] 定位）。所以它不能当普通 wheel 装进
  site-packages，只能「整包给对方 + pip install -e .」。

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

# ── 3. 校验关键资产被 git 跟踪（archive 只导出 tracked 文件） ────────────────
$required = @(
    "pyproject.toml",
    "skill_assets/ok-cosmic-docs.db",
    "comic-understand-long/SKILL.md"
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
    Write-Host "  资产：  ok-cosmic-docs.db ($dbSize)、SKILL.md、references/ 已含"
    Write-Host "  说明：  安装说明.md 已注入包根目录"
    Write-Host ""
    Write-Host "把这个 zip 发给对方；对方解压后进入 $leafName\ 目录，按「安装说明.md」操作即可。"
}
finally {
    if (Test-Path $stageRoot) { Remove-Item $stageRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
