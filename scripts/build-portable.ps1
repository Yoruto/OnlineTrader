param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Output,
    [ValidateSet('Generic','Configured')][string]$DeliveryMode,
    [string]$DeliveryConfigDirectory,
    [string]$Archive
)
$ErrorActionPreference = 'Stop'
if (-not $DeliveryMode) { throw 'Specify -DeliveryMode Generic or Configured.' }
if ($DeliveryMode -eq 'Configured' -and -not $DeliveryConfigDirectory) {
    throw 'Configured delivery requires -DeliveryConfigDirectory with OnlineTrader-defaults.json.'
}
if ($DeliveryMode -eq 'Generic' -and $DeliveryConfigDirectory) {
    throw 'A delivery config directory requires -DeliveryMode Configured.'
}
$project = Split-Path -Parent $PSScriptRoot
$release = Join-Path $project 'release'
$interpreter = (Resolve-Path -LiteralPath $Python).Path
$destination = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
if (Test-Path -LiteralPath $destination) { throw 'Choose a new output directory.' }
$deliveryCheck = Join-Path $PSScriptRoot 'portable_delivery.py'
$deliveryArguments = @()
if ($DeliveryMode -eq 'Configured') {
    $DeliveryConfigDirectory = (Resolve-Path -LiteralPath $DeliveryConfigDirectory).Path
    $deliveryArguments = @('--config-directory', $DeliveryConfigDirectory)
    & $interpreter -I $deliveryCheck @deliveryArguments
    if ($LASTEXITCODE -ne 0) { throw 'Configured delivery inputs failed validation before building.' }
}
$archiveArguments = @()
if ($Archive) {
    $archivePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Archive)
    if ((Test-Path -LiteralPath $archivePath) -or
        [IO.Path]::GetExtension($archivePath) -ne '.zip' -or
        ($archivePath.EndsWith('-Configured.zip', [StringComparison]::OrdinalIgnoreCase) -ne ($DeliveryMode -eq 'Configured'))) {
        throw 'Choose a new .zip archive. Configured deliveries must end in -Configured.zip.'
    }
    $archiveArguments = @('--archive', $archivePath)
}

& $interpreter -I -c "import importlib.metadata as m,struct,sys; assert sys.platform=='win32' and sys.version_info[:2]==(3,13) and struct.calcsize('P')==8; assert all(m.version(name)==value for name,value in {'pyinstaller':'6.22.3','PySide6':'6.8.2.1','tqsdk':'3.10.4','jsonschema':'4.26.0','referencing':'0.37.0'}.items())"
if ($LASTEXITCODE -ne 0) { throw 'The portable build needs the documented Windows x64 build environment.' }

$parent = Split-Path -Parent $destination
New-Item -ItemType Directory -Force -Path $parent | Out-Null
$staging = Join-Path $parent ('.online-trader-build-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $staging | Out-Null
& $interpreter -I -m PyInstaller --noconfirm --workpath (Join-Path $staging 'work') `
    --distpath (Join-Path $staging 'dist') (Join-Path $release 'OnlineTrader.spec')
if ($LASTEXITCODE -ne 0) { throw "Portable build failed. Inspect $staging" }

$bundle = Join-Path $staging 'dist/OnlineTrader'
if (-not (Test-Path -LiteralPath (Join-Path $bundle 'OnlineTrader.exe') -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $bundle '_internal') -PathType Container)) {
    throw "Portable layout is incomplete. Inspect $staging"
}
Get-ChildItem -LiteralPath (Join-Path $release 'portable') | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $bundle -Recurse
}
if ($DeliveryConfigDirectory) {
    foreach ($name in @('OnlineTrader-defaults.json', 'OnlineTrader-parameters.json')) {
        $source = Join-Path $DeliveryConfigDirectory $name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination $bundle
        }
    }
}
$cli = Join-Path $bundle 'OnlineTraderCLI.exe'
$version = & $interpreter -I -c "import sys,tomllib; print(tomllib.load(open(sys.argv[1],'rb'))['project']['version'])" (Join-Path $project 'pyproject.toml')
if ($LASTEXITCODE -ne 0) { throw 'Cannot determine the product version.' }
@{project_id = 89047383; channel = 'preview'; portable_version = $version} | ConvertTo-Json -Compress |
    Set-Content -Encoding ASCII -LiteralPath (Join-Path $bundle 'update-source.json')
& $cli --verify-runtime $version
if ($LASTEXITCODE -ne 0) { throw "Bundled runtime identity check failed. Inspect $staging" }
& $cli --help
if ($LASTEXITCODE -ne 0) { throw "Portable CLI smoke check failed. Inspect $staging" }
& $interpreter -I $deliveryCheck --bundle $bundle @deliveryArguments @archiveArguments
if ($LASTEXITCODE -ne 0) { throw "Portable delivery verification failed. Inspect $staging" }
[IO.Directory]::Move($bundle,$destination)
$resolvedStaging = (Resolve-Path -LiteralPath $staging).Path
if ($resolvedStaging -eq [IO.Path]::GetFullPath($staging) -and
    [IO.Path]::GetDirectoryName($resolvedStaging) -eq [IO.Path]::GetFullPath($parent)) {
    Remove-Item -LiteralPath $resolvedStaging -Recurse -Force -ErrorAction Continue
}
Write-Output $destination
