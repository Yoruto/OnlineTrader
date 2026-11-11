param(
    [Parameter(Mandatory=$true)][string]$RunRoot,
    [string]$DsvTrades,
    [string]$DsvSourceAccount
)
$ErrorActionPreference='Stop'
$manifest=Get-ChildItem -LiteralPath $RunRoot -Filter manifest.json -Recurse -File | Where-Object { $_.Directory.Name -like 'comparison-*' } | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if (-not $manifest) { throw 'No comparison run has started. This command never connects an account.' }
$reportId='report-'+[DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,6)
$projectRoot=Split-Path -Parent $PSScriptRoot
$reportArgs=@('-I',(Join-Path $PSScriptRoot 'run_online_trader.py'),'compare','report','--manifest',$manifest.FullName,'--output',(Join-Path $RunRoot $reportId))
if ($DsvTrades) { $reportArgs+=@('--dsv-trades',$DsvTrades) }
if ($DsvSourceAccount) { $reportArgs+=@('--dsv-source-account',$DsvSourceAccount) }
& (Join-Path $projectRoot '.venv\Scripts\python.exe') @reportArgs
exit $LASTEXITCODE
