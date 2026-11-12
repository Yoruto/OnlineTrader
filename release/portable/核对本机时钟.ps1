param([switch]$NoPrompt)
$ErrorActionPreference = 'Stop'
$report = Join-Path $PSScriptRoot '校时报告.txt'
$command = Join-Path $PSScriptRoot 'OnlineTraderCLI.exe'

do {
    $measurement = Join-Path ([IO.Path]::GetTempPath()) ('OnlineTrader-clock-' + [Guid]::NewGuid().ToString('N') + '.json')
    $lines = @('OnlineTrader · 本机时钟核对', ('时间：' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')),
               ('时区：' + (Get-TimeZone).Id))
    try {
        & $command compare clock --output $measurement | Out-Null
        if (-not (Test-Path -LiteralPath $measurement)) { throw '未取得一致的 NTP 样本。' }
        $result = Get-Content -Raw -Encoding UTF8 -LiteralPath $measurement | ConvertFrom-Json
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
        $choice = Read-Host 'S 同步 Windows 时间 / R 重新检测 / Q 退出'
        if ($choice -eq 'S') {
            try {
                $sync = Start-Process -FilePath (Join-Path $env:WINDIR 'System32/w32tm.exe') `
                    -ArgumentList '/resync' -Verb RunAs -WindowStyle Hidden -PassThru -Wait
                if ($sync.ExitCode -eq 0) { Write-Host '同步命令已完成，按 R 复测。' }
                else { Write-Host "同步失败，Windows 返回 $($sync.ExitCode)。" }
            } catch { Write-Host ('同步未完成：' + $_.Exception.Message) }
        }
    } until ($choice -in 'R','Q')
} while ($choice -eq 'R')
