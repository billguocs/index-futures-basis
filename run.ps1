#requires -version 5.1
<#
.SYNOPSIS
    中信期货 · 股指期货持仓分析看板 一键启动脚本
.DESCRIPTION
    1) 自动寻找可用的 Python 3.10+ 解释器（本机虚拟环境 / DSH 内置运行时 / PATH）
    2) 数据库为空时先抓取数据（2022-01-03 至今，约 2~6 分钟）
    3) 启动本地 Web 服务并打开 http://127.0.0.1:8788/
.PARAMETER Port
    监听端口，默认 8788。
.PARAMETER NoBrowser
    不自动打开浏览器。
.PARAMETER Sync
    强制先同步一次数据（增量，通常很快）。
.PARAMETER Check
    只做离线自检，不启动服务。
.EXAMPLE
    .\run.ps1
.EXAMPLE
    .\run.ps1 -Port 9000 -Sync
#>
[CmdletBinding()]
param(
    [int]$Port = 8788,
    [switch]$NoBrowser,
    [switch]$Sync,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Test-Python($exe) {
    if (-not $exe) { return $false }
    try {
        $out = & $exe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $parts = ($out -split '\.')
        $major = [int]$parts[0]; $minor = [int]$parts[1]
        return ($major -eq 3 -and $minor -ge 10)
    } catch { return $false }
}

$candidates = New-Object System.Collections.Generic.List[string]
if ($env:PYTHON) { $candidates.Add($env:PYTHON) }
$candidates.Add((Join-Path $Root '.venv\Scripts\python.exe'))
$candidates.Add('C:\Users\billg\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe')
$candidates.Add('python.exe')
$candidates.Add('python3.exe')

$Py = $null
foreach ($c in $candidates) {
    if (Test-Python $c) { $Py = $c; break }
}
if (-not $Py) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            & py -3 -c "import sys;assert sys.version_info>=(3,10)" 2>$null
            if ($LASTEXITCODE -eq 0) { $Py = 'py'; $PyArgs = @('-3') }
        } catch { }
    }
}
if (-not $Py) {
    Write-Host '[错误] 未找到 Python 3.10+。请安装 Python 或设置环境变量 PYTHON 指向 python.exe。' -ForegroundColor Red
    exit 1
}
Write-Host "[环境] Python: $Py" -ForegroundColor DarkGray

$checkDeps = & $Py -c "import numpy,pandas;print('deps ok')" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '[提示] 缺少 numpy/pandas，尝试自动安装…' -ForegroundColor Yellow
    & $Py -m pip install --quiet --disable-pip-version-check numpy pandas
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[错误] numpy/pandas 安装失败，请手动执行: python -m pip install numpy pandas' -ForegroundColor Red
        exit 1
    }
}

$appArgs = @('app.py', '--port', "$Port")
if ($Check) { $appArgs = @('app.py', '--check') }
else {
    if (-not $NoBrowser) { $appArgs += '--open' }
    if ($Sync) { $appArgs += '--sync-if-empty' }
    else { $appArgs += '--sync-if-empty' }
}

& $Py @appArgs
exit $LASTEXITCODE
