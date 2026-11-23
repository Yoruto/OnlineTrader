param(
    [Parameter(Mandatory=$true)][string]$Python,
    [string]$Archive,
    [string]$OutputDirectory,
    [string]$EvidenceDirectory
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$interpreter = (Resolve-Path -LiteralPath $Python).Path
$version = & $interpreter -I -c "import sys,tomllib; print(tomllib.load(open(sys.argv[1],'rb'))['project']['version'])" (Join-Path $project 'pyproject.toml')
if ($LASTEXITCODE -ne 0) { throw 'Cannot determine the product version.' }

if (-not $OutputDirectory) {
    if ($Archive) {
        $OutputDirectory = Split-Path -Parent ($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Archive))
    } else {
        $OutputDirectory = Join-Path $project "output/delivery/update-payload-$version"
    }
}
$outDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

if (-not $Archive) {
    $Archive = Join-Path $outDir "OnlineTrader-$version-windows-x64.zip"
}
$archivePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Archive)
if ((Test-Path -LiteralPath $archivePath) -or [IO.Path]::GetFileName($archivePath) -ne "OnlineTrader-$version-windows-x64.zip") {
    throw "Choose a new OnlineTrader-$version-windows-x64.zip path."
}

if (-not $EvidenceDirectory) {
    $EvidenceDirectory = Join-Path $outDir ('.online-trader-payload-evidence-' + [Guid]::NewGuid().ToString('N'))
}
$evidence = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($EvidenceDirectory)
New-Item -ItemType Directory -Force -Path $evidence | Out-Null

$preflight = Join-Path $evidence 'release-preflight.json'
& $interpreter -I (Join-Path $PSScriptRoot 'release_preflight.py') --candidate-source $project --delivery-mode UpdatePayload --output $preflight
if ($LASTEXITCODE -ne 0) { throw "UpdatePayload preflight failed. Inspect $preflight" }

$staging = Join-Path $outDir ('.online-trader-payload-' + [Guid]::NewGuid().ToString('N'))
$base = Join-Path $staging 'base'
New-Item -ItemType Directory -Path $staging | Out-Null
try {
    & (Join-Path $PSScriptRoot 'freeze-portable.ps1') -Python $interpreter -Output $base -BuildMode ReleaseClean -PerformanceReport (Join-Path $evidence 'build-performance.json')
    if ($LASTEXITCODE -ne 0) { throw "Portable freeze failed. Inspect $evidence" }
    & $interpreter -I (Join-Path $PSScriptRoot 'portable_delivery.py') --update-payload --version $version --bundle $base --archive $archivePath --performance-report (Join-Path $evidence 'delivery-performance.json')
    if ($LASTEXITCODE -ne 0) { throw "UpdatePayload assembly or verification failed. Inspect $evidence" }
}
finally {
    if (Test-Path -LiteralPath $staging) {
        $resolved = (Resolve-Path -LiteralPath $staging).Path
        if ([IO.Path]::GetDirectoryName($resolved) -eq [IO.Path]::GetFullPath($outDir)) {
            Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction Continue
        }
    }
}
Write-Output $archivePath
Write-Output $evidence
