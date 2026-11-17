[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Watch,
    [switch]$Apply,
    [switch]$ScopeFlatConfirmed,
    [ValidateRange(30, 3600)]
    [int]$PollSeconds = 60,
    [string]$OutputDirectory,
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $project '.venv\Scripts\python.exe' }
$runner = Join-Path $PSScriptRoot 'run_online_trader.py'
$clockScript = Join-Path $PSScriptRoot 'clock.ps1'
if (-not $OutputDirectory) {
    $tag = '{0}-{1}' -f ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')), ([Guid]::NewGuid().ToString('N').Substring(0,8))
    $OutputDirectory = Join-Path $project "output\clock-sync\$tag"
}

function Read-ClockObservation {
    param([string]$Path)
    & $Python -I $runner compare clock --output $Path | Out-Null
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

if ($Watch -and $Apply) {
    throw 'Watch is read-only. Invoke -Apply once after reconciling the stopped scope.'
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
        if ($aligned -or -not $Apply) {
            $next = if ($aligned) { 'ALIGNED' } else { 'OUTSIDE_TOLERANCE' }
            if ($next -ne $state) {
                Write-Output "$next ($($fact.reference_minus_system_ms) ms) · $sample"
                $state = $next
            }
            if (-not $Watch) {
                if ($aligned) { exit 0 }
                exit 2
            }
            Start-Sleep -Seconds $PollSeconds
            continue
        }

        if (-not $PSCmdlet.ShouldProcess('Windows Time', 'Synchronize once against the configured source')) { exit 0 }
        $after = Join-Path $directory.FullName 'after.json'
        $attemptLog = Join-Path $directory.FullName 'sync.log'
        $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $arguments = @('-NoProfile', '-File', $clockScript, '-Sync', '-Output', $after, '-Python', $Python)
        if ($ScopeFlatConfirmed) { $arguments += '-ScopeFlatConfirmed' }
        & $powershell @arguments *> $attemptLog
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
