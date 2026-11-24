param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Unit', 'Quick', 'Affected', 'Trading', 'Runtime', 'FullOffline', 'SmokeE2E', 'TradingE2E', 'TradingExtended', 'DesktopE2E', 'FaultE2E', 'StatefulE2E', 'SimAll', 'ReleaseGate', 'LivePreflight')]
    [string]$Profile,
    [switch]$ListOnly,
    [switch]$ShowIds,
    [string]$BaseRef,
    [switch]$AuthorizeLive
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
if ($AuthorizeLive) { $arguments += '--authorize-live' }

Push-Location -LiteralPath $projectRoot
try {
    & $projectPython @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
