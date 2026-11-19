param([string]$Account)

$ErrorActionPreference = 'Stop'
$runtimeDirectory = Join-Path $PSScriptRoot 'runtime'
$cli = Join-Path $PSScriptRoot 'OnlineTraderCLI.exe'
$desktop = Join-Path $PSScriptRoot 'OnlineTrader.exe'
$inspectionArguments = @('runtime', 'inspect', '--root', $runtimeDirectory)
if ($Account) { $inspectionArguments += @('--account', $Account) }
$inspection = & $cli @inspectionArguments
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the trading runtime configuration.' }
$pipe = ($inspection | ConvertFrom-Json).pipe
if (-not $pipe) { throw 'The trading runtime inspection did not provide an interface address.' }
& $desktop --runtime-pipe $pipe
exit $LASTEXITCODE
