[CmdletBinding(SupportsShouldProcess = $true)]
param([switch]$NoPrompt, [switch]$Sync, [switch]$ScopeFlatConfirmed)
$ErrorActionPreference = 'Stop'
$report = Join-Path $PSScriptRoot '校时报告.txt'
$command = Join-Path $PSScriptRoot 'OnlineTraderCLI.exe'
. (Join-Path $PSScriptRoot 'clock-maintenance.ps1')

function Measure-ClockReference {
    param([string]$Path)
    & $command compare clock --output $Path | Out-Null
    if ($LASTEXITCODE -notin @(0, 2) -or -not (Test-Path -LiteralPath $Path)) {
        throw '未取得一致的 NTP 样本。'
    }
    Get-Content -Raw -Encoding UTF8 -LiteralPath $Path | ConvertFrom-Json
}

if ($Sync) {
    $tag = [Guid]::NewGuid().ToString('N')
    $evidence = Join-Path $PSScriptRoot "clock-sync-$tag"
    $fact = Invoke-OnlineTraderClockMaintenance -ScopeFlatConfirmed:$ScopeFlatConfirmed `
        -Before "$evidence-before.json" -After "$evidence-after.json" `
        -Measure ${function:Measure-ClockReference} -WhatIf:$WhatIfPreference
    if ($fact) { Write-Host "同步后独立测量通过。证据：$evidence-after.json" }
    return
}

do {
    $measurement = Join-Path ([IO.Path]::GetTempPath()) ('OnlineTrader-clock-' + [Guid]::NewGuid().ToString('N') + '.json')
    $lines = @('OnlineTrader · 本机时钟核对', ('时间：' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')),
               ('时区：' + (Get-TimeZone).Id))
    try {
        $result = Measure-ClockReference -Path $measurement
        $lines += ('偏差（参考时间减本机时间）：{0} 毫秒' -f $result.reference_minus_system_ms)
        if ($result.system_aligned) { $lines += '结论：本机时间在 1 秒范围内。' }
        else { $lines += '结论：本机偏差超过 1 秒。' }
        foreach ($sample in $result.reference.samples) {
            if ($sample.valid) { $lines += ('{0}  偏差 {1} 秒  往返 {2} 秒' -f $sample.host,$sample.offset_seconds,$sample.rtt_seconds) }
            else { $lines += ('{0}  样本不可用' -f $sample.host) }
        }
    } catch {
        $lines += ('检查失败：' + $_.Exception.Message)
    } finally {
        if (Test-Path -LiteralPath $measurement) { Remove-Item -LiteralPath $measurement }
    }
    $lines | Write-Host
    $lines | Set-Content -LiteralPath $report -Encoding UTF8
    Write-Host "报告已保存：$report"
    if ($NoPrompt) { break }
    do {
        $choice = Read-Host 'S 停机校时 / R 重新检测 / Q 退出'
        if ($choice -eq 'S') {
            try {
                $flat = Read-Host '已核对无持仓、活动委托或未知结果，并退出交易程序？输入 YES 确认'
                if ($flat -ceq 'YES' -and $PSCmdlet.ShouldProcess('Windows Time', 'Start stopped-scope synchronization')) {
                    $powerShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
                    $launchArgs = @('-NoProfile', '-File', ('"{0}"' -f $PSCommandPath), '-Sync', '-ScopeFlatConfirmed')
                    $sync = Start-Process -FilePath $powerShell -ArgumentList $launchArgs -Verb RunAs -WindowStyle Hidden -PassThru -Wait
                    if ($sync.ExitCode -eq 0) { Write-Host '同步后独立测量通过，按 R 复测。' }
                    else { Write-Host "同步未通过，返回 $($sync.ExitCode)。可用管理员 PowerShell 运行 -Sync 查看原因。" }
                }
            } catch { Write-Host ('同步未完成：' + $_.Exception.Message) }
        }
    } until ($choice -in 'R','Q')
} while ($choice -eq 'R')
