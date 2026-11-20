param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Output,
    [ValidateSet('Generic','Configured','RuntimeConfigured')][string]$DeliveryMode,
    [string]$DeliveryConfigDirectory,
    [string]$Archive
)
$ErrorActionPreference = 'Stop'
if (-not $DeliveryMode) { throw 'Specify -DeliveryMode Generic, Configured or RuntimeConfigured.' }
$configuredDelivery = $DeliveryMode -ne 'Generic'
$runtimeDelivery = $DeliveryMode -eq 'RuntimeConfigured'
if ($configuredDelivery -and -not $DeliveryConfigDirectory) {
    throw 'Configured delivery requires an explicitly confirmed -DeliveryConfigDirectory.'
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
if ($configuredDelivery) {
    $DeliveryConfigDirectory = (Resolve-Path -LiteralPath $DeliveryConfigDirectory).Path
    $deliveryArguments = @('--config-directory', $DeliveryConfigDirectory)
    if ($runtimeDelivery) {
        $deliveryArguments += @('--runtime-kind', 'fresh')
    }
    & $interpreter -I $deliveryCheck @deliveryArguments
    if ($LASTEXITCODE -ne 0) { throw 'Configured delivery inputs failed validation before building.' }
}
& $interpreter -I -c "import importlib.metadata as m,struct,sys; assert sys.platform=='win32' and sys.version_info[:2]==(3,13) and struct.calcsize('P')==8; assert all(m.version(name)==value for name,value in {'pyinstaller':'6.22.3','PySide6':'6.8.2.1','tqsdk':'3.10.4','jsonschema':'4.26.0','referencing':'0.37.0'}.items())"
if ($LASTEXITCODE -ne 0) { throw 'The portable build needs the documented Windows x64 build environment.' }
$version = & $interpreter -I -c "import sys,tomllib; print(tomllib.load(open(sys.argv[1],'rb'))['project']['version'])" (Join-Path $project 'pyproject.toml')
if ($LASTEXITCODE -ne 0) { throw 'Cannot determine the product version.' }

$parent = Split-Path -Parent $destination
if (-not $Archive) {
    $suffix = switch ($DeliveryMode) {
        'RuntimeConfigured' { '-Runtime-Configured' }
        'Configured' { '-Configured' }
        default { '' }
    }
    $Archive = Join-Path $parent "OnlineTrader-$version-windows-x64$suffix.zip"
}
$archivePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Archive)
if ((Test-Path -LiteralPath $archivePath) -or
    [IO.Path]::GetExtension($archivePath) -ne '.zip' -or
    ($archivePath.EndsWith('-Configured.zip', [StringComparison]::OrdinalIgnoreCase) -ne $configuredDelivery)) {
    throw 'Choose a new .zip archive. Configured deliveries must end in -Configured.zip.'
}
$comparison = [StringComparison]::OrdinalIgnoreCase
if ($archivePath.Equals($destination, $comparison) -or
    $archivePath.StartsWith($destination.TrimEnd('\') + '\', $comparison) -or
    $destination.StartsWith($archivePath + '\', $comparison)) {
    throw 'The portable directory and ZIP must use separate, non-nested paths.'
}

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
if ($runtimeDelivery) {
    Get-ChildItem -LiteralPath (Join-Path $release 'runtime-portable') -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $bundle
    }
}
if ($DeliveryConfigDirectory -and -not $runtimeDelivery) {
    foreach ($name in @('OnlineTrader-defaults.json', 'OnlineTrader-parameters.json')) {
        $source = Join-Path $DeliveryConfigDirectory $name
        Copy-Item -LiteralPath $source -Destination $bundle
    }
}
$cli = Join-Path $bundle 'OnlineTraderCLI.exe'
@{project_id = 89047383; channel = 'preview'; portable_version = $version} | ConvertTo-Json -Compress |
    Set-Content -Encoding ASCII -LiteralPath (Join-Path $bundle 'update-source.json')
& $cli --verify-runtime $version
if ($LASTEXITCODE -ne 0) { throw "Bundled runtime identity check failed. Inspect $staging" }
& $interpreter -I $deliveryCheck --bundle $bundle --archive $archivePath --output $destination @deliveryArguments
if ($LASTEXITCODE -ne 0) { throw "Portable delivery verification failed. Inspect $staging" }
$resolvedStaging = (Resolve-Path -LiteralPath $staging).Path
if ($resolvedStaging -eq [IO.Path]::GetFullPath($staging) -and
    [IO.Path]::GetDirectoryName($resolvedStaging) -eq [IO.Path]::GetFullPath($parent)) {
    Remove-Item -LiteralPath $resolvedStaging -Recurse -Force -ErrorAction Continue
}
Write-Output $destination
Write-Output $archivePath
