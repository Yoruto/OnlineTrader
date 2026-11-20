param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Output,
    [ValidateSet('RuntimeConfigured')][string]$DeliveryMode = 'RuntimeConfigured',
    [Parameter(Mandatory=$true)][string]$DeliveryConfigDirectory,
    [string]$Archive,
    [string]$EvidenceDirectory
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$interpreter = (Resolve-Path -LiteralPath $Python).Path
$destination = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
$config = (Resolve-Path -LiteralPath $DeliveryConfigDirectory).Path
if (Test-Path -LiteralPath $destination) { throw 'Choose a new output directory.' }
$parent = Split-Path -Parent $destination
New-Item -ItemType Directory -Force -Path $parent | Out-Null

$version = & $interpreter -I -c "import sys,tomllib; print(tomllib.load(open(sys.argv[1],'rb'))['project']['version'])" (Join-Path $project 'pyproject.toml')
if ($LASTEXITCODE -ne 0) { throw 'Cannot determine the product version.' }
if (-not $Archive) { $Archive = Join-Path $parent "OnlineTrader-$version-windows-x64-Runtime-Configured.zip" }
$archivePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Archive)
if ((Test-Path -LiteralPath $archivePath) -or [IO.Path]::GetExtension($archivePath) -ne '.zip' -or -not $archivePath.EndsWith('-Configured.zip', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Choose a new Runtime -Configured.zip archive.'
}

if (-not $EvidenceDirectory) { $EvidenceDirectory = Join-Path $parent ('.online-trader-evidence-' + [Guid]::NewGuid().ToString('N')) }
$evidence = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($EvidenceDirectory)
New-Item -ItemType Directory -Force -Path $evidence | Out-Null
$preflight = Join-Path $evidence 'release-preflight.json'
& $interpreter -I (Join-Path $PSScriptRoot 'release_preflight.py') --candidate-source $project --delivery-mode RuntimeConfigured --config-directory $config --output $preflight
if ($LASTEXITCODE -ne 0) { throw "Release preflight failed. Inspect $preflight" }

$staging = Join-Path $parent ('.online-trader-release-' + [Guid]::NewGuid().ToString('N'))
$base = Join-Path $staging 'base'
New-Item -ItemType Directory -Path $staging | Out-Null
try {
    & (Join-Path $PSScriptRoot 'freeze-portable.ps1') -Python $interpreter -Output $base -BuildMode ReleaseClean -PerformanceReport (Join-Path $evidence 'build-performance.json')
    if ($LASTEXITCODE -ne 0) { throw "Portable freeze failed. Inspect $evidence" }
    $preflightDocument = Get-Content -Raw -LiteralPath $preflight | ConvertFrom-Json
    $baseManifestPath = Join-Path $base 'base-build-manifest.json'
    $baseManifest = Get-Content -Raw -LiteralPath $baseManifestPath | ConvertFrom-Json
    $baseManifest | Add-Member -NotePropertyName candidate -NotePropertyValue $preflightDocument.candidate -Force
    $baseManifest | Add-Member -NotePropertyName preflight_sha256 -NotePropertyValue ((Get-FileHash -Algorithm SHA256 -LiteralPath $preflight).Hash.ToLowerInvariant()) -Force
    $baseManifest | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 -LiteralPath $baseManifestPath
    Get-ChildItem -LiteralPath (Join-Path $project 'release/runtime-portable') -File | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $base }
    & $interpreter -I (Join-Path $PSScriptRoot 'portable_delivery.py') --bundle $base --archive $archivePath --output $destination --config-directory $config --runtime-kind fresh --performance-report (Join-Path $evidence 'delivery-performance.json')
    if ($LASTEXITCODE -ne 0) { throw "Portable assembly or verification failed. Inspect $evidence" }
}
finally {
    if (Test-Path -LiteralPath $staging) {
        $resolved = (Resolve-Path -LiteralPath $staging).Path
        if ([IO.Path]::GetDirectoryName($resolved) -eq [IO.Path]::GetFullPath($parent)) { Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction Continue }
    }
}
Write-Output $destination
Write-Output $archivePath
Write-Output $evidence
