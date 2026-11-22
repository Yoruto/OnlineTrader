param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Quick', 'Affected', 'Trading', 'Runtime', 'FullOffline')]
    [string]$Profile,
    [switch]$ListOnly,
    [switch]$ShowIds,
    [string]$BaseRef
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Create the project .venv and install its locked dependencies first.'
}

$arguments = @('-X', 'utf8', (Join-Path $projectRoot 'qa\test_runner.py'), '--profile', $Profile)
if ($ListOnly) { $arguments += '--list-only' }
if ($ShowIds) { $arguments += '--show-ids' }
if ($BaseRef) { $arguments += @('--base-ref', $BaseRef) }

Push-Location -LiteralPath $projectRoot
try {
    & $projectPython @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
