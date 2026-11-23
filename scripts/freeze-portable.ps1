param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Output,
    [ValidateSet('CachedTest','ReleaseClean')][string]$BuildMode = 'CachedTest',
    [string]$PerformanceReport,
    [switch]$CleanCache
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$release = Join-Path $project 'release'
$interpreter = (Resolve-Path -LiteralPath $Python).Path
$destination = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
if (Test-Path -LiteralPath $destination) { throw 'Choose a new base-bundle output directory.' }

& $interpreter -I -c "import importlib.metadata as m,struct,sys; assert sys.platform=='win32' and sys.version_info[:2]==(3,13) and struct.calcsize('P')==8; assert all(m.version(name)==value for name,value in {'pyinstaller':'6.22.3','PySide6':'6.8.2.1','tqsdk':'3.10.4','jsonschema':'4.26.0','referencing':'0.37.0'}.items())"
if ($LASTEXITCODE -ne 0) { throw 'The portable build needs the documented Windows x64 build environment.' }

$cacheRoot = Join-Path $project 'output/build-cache/pyinstaller'
$fingerprintText = (& $interpreter -I -c "import hashlib,importlib.metadata as m,pathlib,platform,sys; p=pathlib.Path(sys.argv[1]); values=[platform.platform(),sys.executable,sys.version,m.version('pyinstaller'),m.version('PySide6'),m.version('tqsdk'),hashlib.sha256(p.read_bytes()).hexdigest(),'v1']; print(hashlib.sha256(chr(0).join(values).encode()).hexdigest()[:20])" (Join-Path $release 'OnlineTrader.spec'))
if ($LASTEXITCODE -ne 0) { throw 'Cannot calculate the PyInstaller environment fingerprint.' }
$cache = Join-Path $cacheRoot ("win-x64-py313-" + $fingerprintText)
if ($CleanCache -and (Test-Path -LiteralPath $cache)) {
    $resolvedCache = (Resolve-Path -LiteralPath $cache).Path
    $resolvedRoot = [IO.Path]::GetFullPath($cacheRoot).TrimEnd('\') + '\'
    if (-not $resolvedCache.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Refusing to clean a cache outside the project cache root.' }
    Remove-Item -LiteralPath $resolvedCache -Recurse -Force
}

$parent = Split-Path -Parent $destination
New-Item -ItemType Directory -Force -Path $parent | Out-Null
$staging = Join-Path $parent ('.online-trader-freeze-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $staging | Out-Null
$workPath = if ($BuildMode -eq 'CachedTest') { Join-Path $cache 'work' } else { Join-Path $staging 'work' }
$analysisBefore = Test-Path -LiteralPath (Join-Path $workPath 'OnlineTrader/Analysis-00.toc')
$lock = $null
$stopwatch = [Diagnostics.Stopwatch]::StartNew()
try {
    if ($BuildMode -eq 'CachedTest') {
        New-Item -ItemType Directory -Force -Path $cache | Out-Null
        $lockPath = Join-Path $cache 'build.lock'
        try { $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
        catch { throw 'The selected PyInstaller cache is already in use.' }
    }
    $buildLog = if ($PerformanceReport) { $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($PerformanceReport) + '.pyinstaller.log' } else { Join-Path $staging 'pyinstaller.log' }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $buildLog) | Out-Null
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $interpreter -I -m PyInstaller --noconfirm --workpath $workPath --distpath (Join-Path $staging 'dist') (Join-Path $release 'OnlineTrader.spec') 2>&1 | Tee-Object -FilePath $buildLog
        $pyinstallerExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    if ($pyinstallerExit -ne 0) { throw "Portable freeze failed. Inspect $buildLog" }
    $bundle = Join-Path $staging 'dist/OnlineTrader'
    if (-not (Test-Path -LiteralPath (Join-Path $bundle 'OnlineTrader.exe') -PathType Leaf) -or -not (Test-Path -LiteralPath (Join-Path $bundle '_internal') -PathType Container)) { throw "Portable layout is incomplete. Inspect $buildLog" }
    Get-ChildItem -LiteralPath (Join-Path $release 'portable') | ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $bundle -Recurse }
    $version = & $interpreter -I -c "import sys,tomllib; print(tomllib.load(open(sys.argv[1],'rb'))['project']['version'])" (Join-Path $project 'pyproject.toml')
    @{project_id=89047383; channel='preview'; portable_version=$version} | ConvertTo-Json -Compress | Set-Content -Encoding ASCII -LiteralPath (Join-Path $bundle 'update-source.json')
    & (Join-Path $bundle 'OnlineTraderCLI.exe') --verify-runtime $version
    if ($LASTEXITCODE -ne 0) { throw "Bundled runtime identity check failed. Inspect $staging" }
    $stopwatch.Stop()
    $logText = Get-Content -Raw -LiteralPath $buildLog
    $analysisRebuilt = $logText -match '(?im)Building Analysis|Building because .*changed|Analysis-00\.toc is non existent'
    $cacheStatus = if ($BuildMode -eq 'ReleaseClean') { 'FORCED_CLEAN' } elseif (-not $analysisBefore) { 'COLD' } elseif ($analysisRebuilt) { 'SOURCE_INVALIDATED' } else { 'WARM_HIT' }
    @{schema_version='ONLINE_TRADER_BASE_BUILD/1'; version=$version; build_mode=$BuildMode; cache_key=$fingerprintText; cache_status=$cacheStatus; duration_seconds=[Math]::Round($stopwatch.Elapsed.TotalSeconds,3); deliverable=$false} | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $bundle 'base-build-manifest.json')
    Move-Item -LiteralPath $bundle -Destination $destination
    if ($PerformanceReport) {
        $reportPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($PerformanceReport)
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $reportPath) | Out-Null
        @{schema_version='ONLINE_TRADER_BUILD_PERFORMANCE/1'; mode=$BuildMode; cache_key=$fingerprintText; cache_status=$cacheStatus; analysis_existed_before=$analysisBefore; analysis_rebuilt=$analysisRebuilt; total_seconds=[Math]::Round($stopwatch.Elapsed.TotalSeconds,3); pyinstaller_log=$buildLog; result='PASS'} | ConvertTo-Json | Set-Content -Encoding UTF8 -LiteralPath $reportPath
    }
}
finally {
    if ($lock) { $lock.Dispose() }
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction Continue }
}
Write-Output $destination
