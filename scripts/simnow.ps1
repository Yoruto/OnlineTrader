param(
    [Parameter(Mandatory=$true)][string]$Scope,
    [Parameter(Mandatory=$true)][Alias('Profile')][string]$ProfilePath,
    [Parameter(Mandatory=$true)][string]$Calendar,
    [Parameter(Mandatory=$true)][string]$Output,
    [string]$Credentials,
    [string]$DsvConfig,
    [string]$DsvLogDir,
    [switch]$Run,
    [switch]$Arm
)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$runner=Join-Path $PSScriptRoot 'run_online_trader.py'
if ($Run) {
    if (-not $Credentials) { throw 'Explicit SimNow credential reference required for run.' }
    $python=Join-Path $projectRoot 'output\online\sdk-venv\Scripts\python.exe'
    $scopeValue=Get-Content -LiteralPath $Scope -Raw -Encoding UTF8 | ConvertFrom-Json
    $invokeArgs=@('-I',$runner,'simnow','run','--credentials',$Credentials,'--pipe',('OnlineTrader.SimNow.'+$scopeValue.account_fingerprint))
    if ($Arm) { $invokeArgs+='--arm' }
} else {
    if ($Arm) { throw 'Arming requires an explicit run.' }
    $python=Join-Path $projectRoot '.venv\Scripts\python.exe'
    $invokeArgs=@('-I',$runner,'simnow','prepare')
}
$invokeArgs+=@('--scope',$Scope,'--profile',$ProfilePath,'--calendar',$Calendar,'--output',$Output)
if ($DsvConfig) { $invokeArgs+=@('--dsv-config',$DsvConfig) }
if ($DsvLogDir) { $invokeArgs+=@('--dsv-log-dir',$DsvLogDir) }
& $python @invokeArgs
exit $LASTEXITCODE
