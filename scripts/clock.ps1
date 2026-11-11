param(
    [switch]$Sync,
    [switch]$Measure,
    [string]$Output
)
$ErrorActionPreference='Stop'
$service=Get-Service -Name W32Time
$service | Select-Object Name,Status,StartType
if ($Sync) {
    if (-not $Output) { throw 'A new clock evidence output path is required.' }
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    $principal=[Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this explicit Windows clock synchronization in an Administrator PowerShell.'
    }
    if ($service.Status -ne 'Running') { Start-Service -Name W32Time }
    & w32tm /resync /rediscover
    if ($LASTEXITCODE -ne 0) { throw 'Windows time resynchronization failed; new entries stay blocked.' }
} else {
    if ($service.Status -eq 'Running') { & w32tm /query /status /verbose }
}
if ($Sync -or $Measure) {
    if (-not $Output) { throw 'A new clock evidence output path is required.' }
    $projectRoot=Split-Path -Parent $PSScriptRoot
    $python=Join-Path $projectRoot '.venv\Scripts\python.exe'
    $runner=Join-Path $PSScriptRoot 'run_online_trader.py'
    & $python -I $runner compare clock --output $Output
    exit $LASTEXITCODE
}
