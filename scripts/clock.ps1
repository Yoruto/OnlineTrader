[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Sync,
    [switch]$Measure,
    [switch]$ScopeFlatConfirmed,
    [string]$Output,
    [string]$Python
)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python=Join-Path $projectRoot '.venv\Scripts\python.exe' }
$runner=Join-Path $PSScriptRoot 'run_online_trader.py'
. (Join-Path $projectRoot 'release/portable/clock-maintenance.ps1')

function Measure-ClockReference {
    param([string]$Path)
    & $Python -I $runner compare clock --output $Path | Out-Null
    if ($LASTEXITCODE -notin @(0, 2) -or -not (Test-Path -LiteralPath $Path)) {
        throw 'Independent read-only clock measurement unavailable.'
    }
    Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

$service=Get-Service -Name W32Time
$service | Select-Object Name,Status,StartType
if ($Sync) {
    if (-not $Output) { throw 'A new clock evidence output path is required.' }
    Invoke-OnlineTraderClockMaintenance -ScopeFlatConfirmed:$ScopeFlatConfirmed -Before "$Output.before.json" -After $Output `
        -Measure ${function:Measure-ClockReference} -WhatIf:$WhatIfPreference
} elseif ($Measure) {
    if (-not $Output) { throw 'A new clock evidence output path is required.' }
    $fact = Measure-ClockReference -Path $Output
    $fact
    if (-not $fact.system_aligned) { exit 2 }
} elseif ($service.Status -eq 'Running') {
    & w32tm /query /status /verbose
}
