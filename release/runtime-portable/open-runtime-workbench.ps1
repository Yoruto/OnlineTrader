param([string]$Account)

$ErrorActionPreference = 'Stop'
$runtimeDirectory = Join-Path $PSScriptRoot 'runtime'
$desktop = Join-Path $PSScriptRoot 'OnlineTrader.exe'
$arguments = @('--runtime-root', $runtimeDirectory)
if ($Account) { $arguments += @('--runtime-account', $Account) }
& $desktop @arguments
exit $LASTEXITCODE
