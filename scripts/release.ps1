#requires -Version 5.1
<#
.SYNOPSIS
  cosmic_kb 一键发版：测试、提交、打包、上传 PyPI、打 tag。

.DESCRIPTION
  面向由 Codex 代劳的标准发版入口。调用前只需审查本地改动，并准备好
  docs\核心\V<版本>发版说明.md；其余机械步骤由脚本串行完成。

  同一命令可在上传失败后续跑：若目标版本已经提交且工作区干净，会跳过测试和提交；
  若 PyPI 已存在同摘要 wheel，会跳过重复上传。

.PARAMETER Version
  目标语义版本号，例如 0.2.2。

.PARAMETER Summary
  中文提交摘要，例如“修正字段解析误报”。

.PARAMETER DryRun
  只显示计划，不修改文件、不测试、不提交、不上传。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Version 0.2.2 -Summary "修正字段解析误报"
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Summary,

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    Write-Host "✗ $Message" -ForegroundColor Red
    exit 1
}

function Check-Native([string]$Step) {
    if ($LASTEXITCODE -ne 0) { Fail "$Step 失败（exit=$LASTEXITCODE）" }
}

function Invoke-Quiet([string]$Step, [scriptblock]$Command) {
    # 许多正常 CLI（如 `python -m build`）会把进度写到 stderr。PowerShell 5.1 在
    # ErrorActionPreference=Stop 下会把这些行包装成 NativeCommandError 并提前终止，
    # 因此捕获期间临时降为 Continue，最终只按真实进程退出码判断成败。
    $oldErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = @(& $Command 2>&1)
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }
    $nonEmpty = @($output | Where-Object { ([string]$_).Trim() })
    if ($code -ne 0) {
        $nonEmpty | Select-Object -Last 120 | ForEach-Object { Write-Host $_ }
        Fail "$Step 失败（exit=$code）"
    }
    $tail = @($nonEmpty | Select-Object -Last 2)
    if ($tail.Count) {
        Write-Host "  ✓ $Step：$($tail -join ' | ')" -ForegroundColor Green
    }
    else {
        Write-Host "  ✓ $Step" -ForegroundColor Green
    }
}

function Read-Utf8([string]$Path) {
    return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
}

function ConvertTo-Text($Value) {
    if ($null -eq $Value) { return "" }
    return ([string]::Join("`n", [string[]]$Value)).Trim()
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Get-PypiRelease([string]$Ver) {
    try {
        return Invoke-RestMethod -Uri "https://pypi.org/pypi/cosmic-kb/$Ver/json"
    }
    catch {
        $response = $_.Exception.Response
        if ($response -and [int]$response.StatusCode -eq 404) { return $null }
        throw
    }
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

git rev-parse --is-inside-work-tree *> $null
Check-Native "定位 git 仓库"

$branch = ConvertTo-Text (git branch --show-current)
Check-Native "读取当前分支"
if ($branch -ne "main") { Fail "标准发版只允许从 main 执行，当前分支：$branch" }

$initPath = Join-Path $RepoRoot "cosmic_kb\__init__.py"
$initText = Read-Utf8 $initPath
if ($initText -notmatch '__version__\s*=\s*"([^"]+)"') { Fail "无法读取当前版本" }
$CurrentVersion = $Matches[1]

if ([version]$Version -lt [version]$CurrentVersion) {
    Fail "目标版本 $Version 低于当前版本 $CurrentVersion"
}

Write-Host "cosmic_kb $CurrentVersion -> $Version" -ForegroundColor Cyan
Write-Host "摘要：$Summary"

if ($DryRun) {
    Write-Host ""
    Write-Host "计划：同步版本/口令 -> pytest -q -> commit+push -> 离线包+wheel -> twine check -> PyPI -> tag"
    Write-Host "发版说明：docs\核心\V${Version}发版说明.md"
    Write-Host "DryRun 完成，未修改任何内容。" -ForegroundColor Green
    return
}

git fetch origin
Check-Native "git fetch"

$sync = (ConvertTo-Text (git rev-list --left-right --count origin/main...HEAD)) -split '\s+'
Check-Native "检查 main 同步状态"
if ($sync.Count -ne 2 -or $sync[0] -ne "0") {
    Fail "main 落后于 origin/main（远端独有=$($sync[0])），先同步后再发版"
}
if ($sync[1] -ne "0" -and $CurrentVersion -ne $Version) {
    Fail "main 有尚未推送的旧版本提交（本地独有=$($sync[1])），先处理后再发版"
}

$tagName = "v$Version"
$localTag = ConvertTo-Text (git tag --list $tagName)
Check-Native "检查本地 tag"
$remoteTag = ConvertTo-Text (git ls-remote --tags origin "refs/tags/$tagName")
Check-Native "检查远端 tag"
if ($remoteTag) { Fail "远端 $tagName 已存在，不能重复发布" }
if ($localTag) {
    $tagCommit = ConvertTo-Text (git rev-list -n 1 $tagName)
    $headCommit = ConvertTo-Text (git rev-parse HEAD)
    Check-Native "检查本地 tag 指向"
    if ($tagCommit -ne $headCommit -or $CurrentVersion -ne $Version) {
        Fail "本地 $tagName 已存在但不是当前待续跑的发布提交"
    }
    Write-Host "  检测到尚未推送的本地 $tagName，将按失败续跑处理" -ForegroundColor Yellow
}

$releaseNote = Join-Path $RepoRoot "docs\核心\V${Version}发版说明.md"
if (-not (Test-Path -LiteralPath $releaseNote)) {
    Fail "缺少发版说明：$releaseNote"
}

if ($CurrentVersion -ne $Version) {
    $versionPattern = New-Object System.Text.RegularExpressions.Regex('__version__\s*=\s*"[^"]+"')
    $updatedInit = $versionPattern.Replace($initText, "__version__ = `"$Version`"", 1)
    Write-Utf8NoBom $initPath $updatedInit

    & (Join-Path $PSScriptRoot "make_dist.ps1") -RefreshOnly
    Check-Native "刷新安装口令"
}

$dirty = git status --porcelain
Check-Native "检查工作区"
if ($dirty) {
    Write-Host "[1/5] 全量测试" -ForegroundColor Cyan
    Invoke-Quiet "pytest" { python -m pytest -q }

    git diff --check
    Check-Native "工作区格式检查"
    git add --all
    Check-Native "git add"
    git diff --cached --check
    Check-Native "暂存区格式检查"

    Write-Host "[2/5] 提交并推送 main" -ForegroundColor Cyan
    git commit -m "发布 ${tagName}：$Summary"
    Check-Native "git commit"
    git push origin main
    Check-Native "git push main"
}
else {
    Write-Host "[1/5] 工作区干净且版本已提交，按失败续跑处理：跳过测试与提交" -ForegroundColor Yellow
    if ($sync[1] -ne "0") {
        git push origin main
        Check-Native "续跑时推送 main"
    }
}

$headVersionText = Read-Utf8 $initPath
if ($headVersionText -notmatch "__version__\s*=\s*`"$([regex]::Escape($Version))`"") {
    Fail "HEAD 中版本不是 $Version"
}
if (git status --porcelain) { Fail "提交后工作区仍有改动" }

Write-Host "[3/5] 构建与校验产物" -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "make_dist.ps1")
Check-Native "构建离线包"

Invoke-Quiet "构建 wheel" { python -m build --wheel }

$wheelPath = Join-Path $RepoRoot "dist\cosmic_kb-$Version-py3-none-any.whl"
if (-not (Test-Path -LiteralPath $wheelPath)) { Fail "找不到 wheel：$wheelPath" }
Invoke-Quiet "twine check" { python -m twine check $wheelPath }

$wheelHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $wheelPath).Hash.ToLowerInvariant()
$stamp = Get-Date -Format "yyyyMMdd"
$zipPath = Join-Path $RepoRoot "dist\cosmic_kb_dist_v${Version}_${stamp}.zip"
if (-not (Test-Path -LiteralPath $zipPath)) { Fail "找不到离线包：$zipPath" }
$zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()

Write-Host "[4/5] 发布并核对 PyPI" -ForegroundColor Cyan
$published = Get-PypiRelease $Version
if ($published) {
    $publishedWheel = $published.urls | Where-Object { $_.filename -eq (Split-Path $wheelPath -Leaf) }
    if (-not $publishedWheel -or $publishedWheel.digests.sha256 -ne $wheelHash) {
        Fail "PyPI 已存在 $Version，但 wheel 摘要与本地不一致"
    }
    Write-Host "  PyPI 已有同摘要 wheel，跳过上传（失败续跑）" -ForegroundColor Yellow
}
else {
    $oldPythonUtf8 = $env:PYTHONUTF8
    try {
        $env:PYTHONUTF8 = "1"
        Invoke-Quiet "上传 PyPI" {
            python -m twine upload --non-interactive --disable-progress-bar $wheelPath
        }
    }
    finally {
        $env:PYTHONUTF8 = $oldPythonUtf8
    }
}

$verified = $null
# PyPI 上传接口成功后，公共 JSON/CDN 偶尔需要十几秒才可见。等待最多 30 秒，
# 避免已成功上传却因短暂 404 被误判为发布失败。
for ($i = 0; $i -lt 15; $i++) {
    $verified = Get-PypiRelease $Version
    if ($verified) { break }
    Start-Sleep -Seconds 2
}
if (-not $verified) { Fail "PyPI 在等待窗口内仍查不到 $Version" }
$verifiedWheel = $verified.urls | Where-Object { $_.filename -eq (Split-Path $wheelPath -Leaf) }
if (-not $verifiedWheel -or $verifiedWheel.digests.sha256 -ne $wheelHash) {
    Fail "PyPI wheel 摘要核对失败"
}

Write-Host "[5/5] 创建并推送 tag" -ForegroundColor Cyan
if (-not $localTag) {
    git tag $tagName
    Check-Native "创建 tag"
}
git push origin $tagName
Check-Native "推送 tag"

$commit = ConvertTo-Text (git rev-parse HEAD)
Write-Host ""
Write-Host "✓ $tagName 发布完成" -ForegroundColor Green
Write-Host "  commit: $commit"
Write-Host "  PyPI:   https://pypi.org/project/cosmic-kb/$Version/"
Write-Host "  wheel:  $wheelPath"
Write-Host "  sha256: $wheelHash"
Write-Host "  zip:    $zipPath"
Write-Host "  sha256: $zipHash"
