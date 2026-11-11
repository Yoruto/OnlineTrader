$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) { throw '请先建立本工程独立的 .venv 并安装锁定依赖。' }
Push-Location -LiteralPath $projectRoot
try {
    & $projectPython -X utf8 (Join-Path $projectRoot 'qa\check.py')
    exit $LASTEXITCODE
} finally { Pop-Location }
