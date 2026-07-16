#requires -Version 5.1
<#
.SYNOPSIS
  重建符号解析 JVM 微工具 fat jar 并拷入 cosmic_kb/java/vendor/symsolver.jar。

.DESCRIPTION
  开发者侧脚本（**使用者永远不需要跑**——vendor/symsolver.jar 已入 git 随包分发，
  使用者只需本机有 java 运行时）。改动 tools/symsolver/ 的 Java 源码后跑本脚本：
    1. gradle 构建 fat jar（需本机 Gradle 7.x + JDK 8+，首次需联网拉 JavaParser 依赖）；
    2. 校验产物是可用的 jar（zip 头）；
    3. 拷入 cosmic_kb/java/vendor/symsolver.jar（git tracked，记得连同源码一起提交）。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\build_symsolver.ps1
#>
param()

$ErrorActionPreference = "Stop"

function Fail($msg) { Write-Host "✗ $msg" -ForegroundColor Red; exit 1 }

$RepoRoot  = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ToolDir   = Join-Path $RepoRoot "tools\symsolver"
$JarBuilt  = Join-Path $ToolDir "build\libs\symsolver.jar"
$VendorDir = Join-Path $RepoRoot "cosmic_kb\java\vendor"

if (-not (Test-Path (Join-Path $ToolDir "build.gradle"))) { Fail "找不到微工具工程：$ToolDir" }
if (-not (Get-Command gradle -ErrorAction SilentlyContinue)) { Fail "本机没有 gradle（开发者侧构建需要 Gradle 7.x）" }

Write-Host "== 构建 symsolver fat jar（tools\symsolver）==" -ForegroundColor Cyan
& gradle -p $ToolDir --console=plain -q clean jar
if ($LASTEXITCODE -ne 0) { Fail "gradle 构建失败（退出码 $LASTEXITCODE）" }
if (-not (Test-Path $JarBuilt)) { Fail "构建完成但找不到产物：$JarBuilt" }

# jar 就是 zip：校验魔数 PK\x03\x04，防拷进去一个坏文件
$head = [System.IO.File]::ReadAllBytes($JarBuilt)[0..3]
if (-not ($head[0] -eq 0x50 -and $head[1] -eq 0x4B)) { Fail "产物不是合法 jar（zip 魔数校验失败）：$JarBuilt" }

New-Item -ItemType Directory -Force $VendorDir | Out-Null
Copy-Item $JarBuilt (Join-Path $VendorDir "symsolver.jar") -Force

$size = "{0:N1} MB" -f ((Get-Item (Join-Path $VendorDir "symsolver.jar")).Length / 1MB)
Write-Host ""
Write-Host "✓ 已更新 cosmic_kb\java\vendor\symsolver.jar（$size）" -ForegroundColor Green
Write-Host "  记得把 vendor jar 与 tools/symsolver 源码改动一起交 codex 提交。"
