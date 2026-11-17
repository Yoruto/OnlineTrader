function Assert-NoExternalTradingProcess {
    $names = @('trading.exe', 'OnlineTrader.exe', 'OnlineTraderCLI.exe', 'online-trader.exe',
               'online-trader-desktop.exe', 'python.exe', 'pythonw.exe')
    $filter = ($names | ForEach-Object { "Name='$_'" }) -join ' OR '
    foreach ($process in (Get-CimInstance Win32_Process -Filter $filter -ErrorAction Stop)) {
        if ($process.Name -in @('trading.exe', 'OnlineTrader.exe', 'online-trader-desktop.exe')) {
            throw 'Close legacy DSV and trading workbenches before Windows clock maintenance.'
        }
        if (-not $process.CommandLine) {
            throw 'A potentially relevant process could not be inspected. Clock maintenance is deferred.'
        }
        if ($process.CommandLine -match '(?i)online[_-]?trader' -and
            $process.CommandLine -match '(?i)\b(?:(?:simnow|dsv|simulation|simulate)\s+(?:replacement-)?run|desktop-start)\b') {
            throw 'A trading entry point is active. Clock maintenance is deferred.'
        }
    }
}

function Invoke-OnlineTraderClockMaintenance {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [switch]$ScopeFlatConfirmed,
        [Parameter(Mandatory=$true)][string]$Before,
        [Parameter(Mandatory=$true)][string]$After,
        [Parameter(Mandatory=$true)][scriptblock]$Measure
    )
    $gate = [Threading.Mutex]::new($false, 'Global\OnlineTrader.ClockMaintenance.Gate.v1')
    $locked = $false
    try {
        try { $locked = $gate.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $locked = $true }
        if (-not $locked) { throw 'Another OnlineTrader startup or maintenance operation owns the clock gate.' }
        $owners = $null
        try {
            $owners = [Threading.EventWaitHandle]::OpenExisting('Global\OnlineTrader.ClockMaintenance.Owners.v1')
        } catch [Threading.WaitHandleCannotBeOpenedException] {}
        if ($null -ne $owners) {
            $owners.Dispose()
            throw 'OnlineTrader has an active execution owner. Clock maintenance is deferred.'
        }
        Assert-NoExternalTradingProcess
        if (-not $PSCmdlet.ShouldProcess('Windows Time', 'Synchronize the stopped trading workstation once')) { return }
        if (-not $ScopeFlatConfirmed) {
            throw 'Confirm zero owned positions, active orders and unknown results after reconciliation with -ScopeFlatConfirmed.'
        }
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = [Security.Principal.WindowsPrincipal]::new($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw 'Run the explicit Windows clock synchronization from Administrator PowerShell.'
        }
        if ((Test-Path -LiteralPath $Before) -or (Test-Path -LiteralPath $After)) {
            throw 'Choose new clock evidence files.'
        }
        $null = & $Measure $Before
        Assert-NoExternalTradingProcess
        if ((Get-Service W32Time).Status -ne 'Running') { Start-Service W32Time }
        & w32tm /resync /rediscover | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'Windows time synchronization failed.' }
        $verification = & $Measure $After
        if ($verification.system_aligned -ne $true) {
            throw 'Resync was requested, but the independent after sample has not reached the measurement tolerance.'
        }
        $verification
    } finally {
        if ($locked) { $gate.ReleaseMutex() }
        $gate.Dispose()
    }
}
