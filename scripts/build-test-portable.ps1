param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Output,
    [Parameter(Mandatory=$true)][string]$DeliveryConfigDirectory,
    [string]$EvidenceDirectory,
    [switch]$CleanCache
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$interpreter = (Resolve-Path -LiteralPath $Python).Path
$destination = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
$config = (Resolve-Path -LiteralPath $DeliveryConfigDirectory).Path
if (Test-Path -LiteralPath $destination) { throw 'Choose a new TEST-ONLY output directory.' }
$parent = Split-Path -Parent $destination
New-Item -ItemType Directory -Force -Path $parent | Out-Null
if (-not $EvidenceDirectory) { $EvidenceDirectory = Join-Path $parent ('.online-trader-test-evidence-' + [Guid]::NewGuid().ToString('N')) }
$evidence = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($EvidenceDirectory)
New-Item -ItemType Directory -Force -Path $evidence | Out-Null

$preflight = Join-Path $evidence 'release-preflight.json'
& $interpreter -I (Join-Path $PSScriptRoot 'release_preflight.py') --candidate-source $project --delivery-mode RuntimeConfigured --config-directory $config --output $preflight
if ($LASTEXITCODE -ne 0) { throw "Test artifact preflight failed. Inspect $evidence" }
& (Join-Path $PSScriptRoot 'freeze-portable.ps1') -Python $interpreter -Output $destination -BuildMode CachedTest -PerformanceReport (Join-Path $evidence 'build-performance.json') -CleanCache:$CleanCache
$preflightDocument = Get-Content -Raw -LiteralPath $preflight | ConvertFrom-Json
$baseManifestPath = Join-Path $destination 'base-build-manifest.json'
$baseManifest = Get-Content -Raw -LiteralPath $baseManifestPath | ConvertFrom-Json
$baseManifest | Add-Member -NotePropertyName candidate -NotePropertyValue $preflightDocument.candidate -Force
$baseManifest | Add-Member -NotePropertyName preflight_sha256 -NotePropertyValue ((Get-FileHash -Algorithm SHA256 -LiteralPath $preflight).Hash.ToLowerInvariant()) -Force
$baseManifest | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 -LiteralPath $baseManifestPath
Get-ChildItem -LiteralPath (Join-Path $project 'release/runtime-portable') -File | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $destination }
& $interpreter -I (Join-Path $PSScriptRoot 'portable_delivery.py') --bundle $destination --config-directory $config --runtime-kind fresh --performance-report (Join-Path $evidence 'delivery-performance.json')
if ($LASTEXITCODE -ne 0) {
    if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force -ErrorAction Continue }
    throw "TEST-ONLY bundle assembly failed. Inspect $evidence"
}
$manifest = Join-Path $destination 'base-build-manifest.json'
$document = Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json
$document | Add-Member -NotePropertyName artifact_class -NotePropertyValue 'TEST-ONLY' -Force
$document | Add-Member -NotePropertyName deliverable -NotePropertyValue $false -Force
$document | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 -LiteralPath $manifest
Set-Content -Encoding ASCII -LiteralPath (Join-Path $destination 'TEST-ONLY.txt') -Value 'TEST-ONLY - NOT A DELIVERY ARTIFACT'
Write-Output $destination
Write-Output $evidence
