$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) { throw 'Create the project .venv and install its locked dependencies first.' }
Push-Location -LiteralPath $projectRoot
try {
    & $projectPython -X utf8 (Join-Path $projectRoot 'qa\check.py')
    exit $LASTEXITCODE
} finally { Pop-Location }
