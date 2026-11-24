[CmdletBinding()]
param(
    [string]$RuntimeRoot,
    [string]$Account
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Split-Path -Parent $PSScriptRoot)
$sourceRoot = Join-Path $projectRoot 'src'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$launcherDirectory = Join-Path $env:LOCALAPPDATA 'OnlineTrader\dev-launcher'
$launcherLog = Join-Path $launcherDirectory 'launcher.log'
$stdoutLog = Join-Path $launcherDirectory 'workbench.stdout.log'
$stderrLog = Join-Path $launcherDirectory 'workbench.stderr.log'

function Write-LauncherLog {
    param([string]$Message)
    $stamp = [DateTimeOffset]::Now.ToString('yyyy-MM-ddTHH:mm:ss.fffzzz')
    Add-Content -LiteralPath $launcherLog -Encoding UTF8 -Value "$stamp $Message"
}

function Show-LauncherError {
    param([string]$Message)
    try {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            $Message,
            'OnlineTrader 源码启动失败',
            [System.Windows.MessageBoxButton]::OK,
            [System.Windows.MessageBoxImage]::Error
        ) | Out-Null
    }
    catch {
        # The launcher log remains the diagnostic fallback when Windows UI is unavailable.
    }
}

function Resolve-RuntimeRoot {
    param([string]$RequestedRoot)

    if ($RequestedRoot) {
        return (Resolve-Path -LiteralPath $RequestedRoot).Path
    }
    if ($env:ONLINE_TRADER_DEV_RUNTIME_ROOT) {
        return (Resolve-Path -LiteralPath $env:ONLINE_TRADER_DEV_RUNTIME_ROOT).Path
    }

    $desktop = [Environment]::GetFolderPath('Desktop')
    $candidate = Get-ChildItem -LiteralPath $desktop -Directory -ErrorAction Stop |
        Where-Object { $_.Name -like 'OnlineTrader-*-Runtime-Configured' } |
        ForEach-Object {
            $root = Join-Path $_.FullName 'runtime'
            if (Test-Path -LiteralPath (Join-Path $root 'runtime.json') -PathType Leaf) {
                $developmentVersion = 0
                if ($_.Name -match '\.dev(?<number>[0-9]+)-') {
                    $developmentVersion = [int]$Matches.number
                }
                [pscustomobject]@{
                    Root = (Get-Item -LiteralPath $root).FullName
                    DevelopmentVersion = $developmentVersion
                    LastWriteTimeUtc = $_.LastWriteTimeUtc
                }
            }
        } |
        Sort-Object DevelopmentVersion, LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if ($null -eq $candidate) {
        throw '桌面上没有找到 OnlineTrader-*-Runtime-Configured\runtime 配置目录。'
    }
    return $candidate.Root
}

function Assert-RuntimeConfiguration {
    param([string]$Root)

    $required = @(
        'runtime.json',
        'OnlineTrader-parameters.json',
        'private\OnlineTrader-defaults.json'
    )
    $missing = @($required | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $Root $_) -PathType Leaf)
    })
    if ($missing.Count -gt 0) {
        throw ('Runtime 配置不完整，缺少：' + ($missing -join '、'))
    }
}

function Get-OnlineTraderProcesses {
    @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        ($_.Name -in @('OnlineTrader.exe', 'OnlineTraderCLI.exe')) -or
        (($_.Name -in @('python.exe', 'pythonw.exe')) -and
            $_.CommandLine -and $_.CommandLine.Contains('online_trader'))
    })
}

function Assert-CurrentSourceProcesses {
    $sourcePython = @(
        [IO.Path]::GetFullPath($python).ToLowerInvariant(),
        [IO.Path]::GetFullPath($pythonw).ToLowerInvariant()
    )
    $latestSourceWrite = Get-ChildItem -LiteralPath $sourceRoot -Recurse -File |
        Where-Object { $_.Extension -in @('.py', '.json', '.sql') } |
        Measure-Object -Property LastWriteTimeUtc -Maximum |
        Select-Object -ExpandProperty Maximum
    $projectFileWrite = (Get-Item -LiteralPath (Join-Path $projectRoot 'pyproject.toml')).LastWriteTimeUtc
    if ($projectFileWrite -gt $latestSourceWrite) {
        $latestSourceWrite = $projectFileWrite
    }

    foreach ($process in Get-OnlineTraderProcesses) {
        $executable = if ($process.ExecutablePath) {
            [IO.Path]::GetFullPath($process.ExecutablePath).ToLowerInvariant()
        }
        else {
            ''
        }
        if ($executable -notin $sourcePython) {
            throw "检测到打包版或其他环境的 OnlineTrader 仍在运行（PID $($process.ProcessId)）。请先从托盘安全退出 OnlineTrader，再双击源码启动器。"
        }
        $created = if ($process.CreationDate -is [DateTime]) {
            $process.CreationDate.ToUniversalTime()
        }
        else {
            [Management.ManagementDateTimeConverter]::ToDateTime([string]$process.CreationDate).ToUniversalTime()
        }
        if ($created -lt $latestSourceWrite) {
            throw "源码在当前 OnlineTrader 进程启动后已有更新（PID $($process.ProcessId)）。请先从托盘安全退出，再重新双击源码启动器。"
        }
    }
}

try {
    New-Item -ItemType Directory -Path $launcherDirectory -Force | Out-Null
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
        throw "项目 Python 环境不存在：$projectRoot\.venv\Scripts"
    }
    if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
        throw "项目源码目录不存在：$sourceRoot"
    }

    $resolvedRuntimeRoot = Resolve-RuntimeRoot -RequestedRoot $RuntimeRoot
    Assert-RuntimeConfiguration -Root $resolvedRuntimeRoot
    Assert-CurrentSourceProcesses

    $previousPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = if ($previousPythonPath) {
        "$sourceRoot;$previousPythonPath"
    }
    else {
        $sourceRoot
    }
    $loadedSource = @(& $python -X utf8 -c 'from pathlib import Path; import online_trader; from online_trader.desktop.entrypoint import main; print(Path(online_trader.__file__).resolve())' 2>> $stderrLog)
    if ($LASTEXITCODE -ne 0 -or -not $loadedSource) {
        throw "无法从项目环境导入 OnlineTrader，详见：$stderrLog"
    }
    $expectedPackage = [IO.Path]::GetFullPath((Join-Path $sourceRoot 'online_trader')).TrimEnd('\')
    $actualPackage = Split-Path -Parent ([IO.Path]::GetFullPath($loadedSource[-1]))
    if (-not $actualPackage.Equals($expectedPackage, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Python 加载的不是当前工作区源码：$actualPackage"
    }

    Write-LauncherLog "START source=$projectRoot runtime_root=$resolvedRuntimeRoot"
    $launchCode = 'from online_trader.desktop.entrypoint import main; raise SystemExit(main())'
    $launchArguments = @('-X', 'utf8', '-c', $launchCode, '--runtime-root', $resolvedRuntimeRoot)
    if ($Account) {
        $launchArguments += @('--runtime-account', $Account)
    }
    Set-Location -LiteralPath $projectRoot
    & $pythonw @launchArguments 1>> $stdoutLog 2>> $stderrLog
    $exitCode = $LASTEXITCODE
    Write-LauncherLog "EXIT code=$exitCode"
    exit $exitCode
}
catch {
    try {
        if (-not (Test-Path -LiteralPath $launcherDirectory -PathType Container)) {
            New-Item -ItemType Directory -Path $launcherDirectory -Force | Out-Null
        }
        Write-LauncherLog "ERROR type=$($_.Exception.GetType().FullName) message=$($_.Exception.Message)"
    }
    catch {
        # Keep the original failure visible even if diagnostic storage is unavailable.
    }
    Show-LauncherError ($_.Exception.Message + "`n`n启动日志：" + $launcherLog)
    exit 2
}
