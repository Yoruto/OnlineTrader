[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Watch,
    [ValidateRange(30, 3600)]
    [int]$PollSeconds = 60,
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'run_online_trader.py'
$clockScript = Join-Path $PSScriptRoot 'clock.ps1'
if (-not $OutputDirectory) {
    $tag = '{0}-{1}' -f ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')), ([Guid]::NewGuid().ToString('N').Substring(0,8))
    $OutputDirectory = Join-Path $project "output\clock-sync\$tag"
}

function Read-ClockObservation {
    param([string]$Path)
    & $python -I $runner compare clock --output $Path | Out-Null
    $code = $LASTEXITCODE
    if ($code -notin @(0, 2) -or -not (Test-Path -LiteralPath $Path)) {
        throw 'The read-only reference measurement failed.'
    }
    $fact = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($fact.system_aligned -isnot [bool] -or $fact.maximum_system_skew_ms -le 0) {
        throw 'The reference measurement is incomplete.'
    }
    return $fact
}

function Test-TradingProcess {
    $processes = Get-CimInstance Win32_Process -Filter "Name='trading.exe' OR Name='online-trader.exe' OR Name='python.exe' OR Name='pythonw.exe'"
    foreach ($process in $processes) {
        if ($process.Name -ieq 'trading.exe' -or
            ($process.CommandLine -match '(?i)online[_-]trader' -and
             $process.CommandLine -match '(?i)\b(?:simnow|dsv|simulation|simulate)\s+run\b')) {
            return $true
        }
    }
    return $false
}

function Test-ClockAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if ($Watch -and -not $WhatIfPreference -and -not (Test-ClockAdmin)) {
    throw 'Start the automatic watch from an Administrator PowerShell.'
}

$mutex = [Threading.Mutex]::new($false, 'Local\OnlineTrader.ClockSyncOnce')
$locked = $false
try {
    try { $locked = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Another OnlineTrader clock check is already running.' }
    $directory = New-Item -Path $OutputDirectory -ItemType Directory -ErrorAction Stop -WhatIf:$false
    $sample = Join-Path $directory.FullName 'latest.json'
    $state = $null

    while ($true) {
        if (Test-Path -LiteralPath $sample) { Remove-Item -LiteralPath $sample -WhatIf:$false }
        try { $fact = Read-ClockObservation -Path $sample }
        catch {
            if (-not $Watch) { throw }
            if ($state -ne 'MEASUREMENT_UNAVAILABLE') {
                Write-Warning 'Reference time is unavailable. Waiting for a valid measurement.'
                $state = 'MEASUREMENT_UNAVAILABLE'
            }
            Start-Sleep -Seconds $PollSeconds
            continue
        }
        $aligned = $fact.system_aligned
        if ($aligned -or (Test-TradingProcess)) {
            $next = if ($aligned) { 'ALIGNED' } else { 'DEFERRED_TRADING_PROCESS' }
            if ($next -ne $state) {
                Write-Output "$next ($($fact.reference_minus_system_ms) ms) · $sample"
                $state = $next
            }
            if (-not $Watch) {
                if ($aligned) { exit 0 }
                exit 3
            }
            Start-Sleep -Seconds $PollSeconds
            continue
        }

        $before = Join-Path $directory.FullName 'before.json'
        Move-Item -LiteralPath $sample -Destination $before -WhatIf:$false
        if (-not $PSCmdlet.ShouldProcess('Windows Time', 'Synchronize once against the configured source')) { exit 0 }
        if (-not (Test-ClockAdmin)) {
            throw 'Run from an Administrator PowerShell to apply the one-time correction.'
        }
        if (Test-TradingProcess) { throw 'A trading process started during the preflight. Retry when it has exited.' }

        $after = Join-Path $directory.FullName 'after.json'
        $attemptLog = Join-Path $directory.FullName 'sync.log'
        $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        & $powershell -NoProfile -File $clockScript -Sync -Output $after *> $attemptLog
        $syncExit = $LASTEXITCODE
        if ($syncExit -ne 0 -or -not (Test-Path -LiteralPath $after)) {
            throw "The single synchronization attempt did not pass. See $attemptLog"
        }
        $verified = Get-Content -LiteralPath $after -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($verified.system_aligned -ne $true) { throw "The clock is still out of tolerance. See $after" }
        Write-Output "SYNCHRONIZED ($($verified.reference_minus_system_ms) ms) · $after"
        exit 0
    }
}
finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
