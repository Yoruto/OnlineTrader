param([Parameter(Mandatory=$true)][ValidateSet('Enable','Disable','Watch')][string]$Mode)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$program = Join-Path $root 'OnlineTrader.exe'
$managed = $false
$versionParent = Split-Path -Parent $root
if ($versionParent -and (Split-Path -Leaf $versionParent) -eq 'versions') {
    $managedRoot = Split-Path -Parent $versionParent
    $launcher = Join-Path $managedRoot 'bootstrap/OnlineTraderCLI.exe'
    $managed = (Test-Path -LiteralPath (Join-Path $managedRoot 'update-source.json') -PathType Leaf) -and
        (Test-Path -LiteralPath $launcher -PathType Leaf)
    if ($managed) { $program = $launcher }
}
$executableName = Split-Path -Leaf $program
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $bytes = [Text.Encoding]::UTF8.GetBytes($identity.User.Value + '|' + $program.ToUpperInvariant())
    $suffix = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-','').Substring(0,24)
} finally { $sha.Dispose() }
$name = 'OnlineTrader-Workbench-' + $suffix
$stop = Join-Path $env:LOCALAPPDATA ('OnlineTrader/watchdog/' + $suffix + '.stop')

if ($Mode -eq 'Disable') {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $stop) | Out-Null
    Set-Content -LiteralPath $stop -Value 'disabled' -Encoding ASCII
    if (Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -TaskPath '\' -Confirm:$false
    }
    Write-Host '已停用本目录的自启动和守护。已打开的工作台可从托盘退出。'
    return
}

if ($Mode -eq 'Enable') {
    if (-not (Test-Path -LiteralPath $program -PathType Leaf)) { throw '请从完整便携目录启用守护。' }
    $powershell = Join-Path $env:WINDIR 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Mode Watch' -f $PSCommandPath
    $action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $name -TaskPath '\' -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    if (Test-Path -LiteralPath $stop) { Remove-Item -LiteralPath $stop }
    Start-ScheduledTask -TaskName $name -TaskPath '\'
    Write-Host '已启用当前 Windows 用户的登录自启和工作台守护。'
    return
}

$mutex = [Threading.Mutex]::new($false,('Local\' + $name))
$ownsMutex = $false
try {
    try { $ownsMutex = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { return }
    $currentSession = (Get-Process -Id $PID).SessionId
    while ((Test-Path -LiteralPath $program -PathType Leaf) -and -not (Test-Path -LiteralPath $stop)) {
        $running = Get-CimInstance -ClassName Win32_Process -Filter "Name='$executableName' AND SessionId=$currentSession" | Where-Object {
            $_.ExecutablePath -eq $program -and $_.CommandLine -notmatch '\s--compare-fills(?:\s|$)'
        } | Select-Object -First 1
        if (-not $running -and -not (Test-Path -LiteralPath $stop)) {
            $launchArguments = if ($managed) { '--managed-launch --autostart' } else { '--autostart' }
            try { Start-Process -FilePath $program -WorkingDirectory $root -ArgumentList $launchArguments -WindowStyle Hidden }
            catch { Write-Error -ErrorAction Continue $_ }
        }
        Start-Sleep -Seconds 60
    }
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
