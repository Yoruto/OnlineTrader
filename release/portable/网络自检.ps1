param([switch]$NoPrompt)
$ErrorActionPreference = 'Stop'
$report = Join-Path $PSScriptRoot '网络自检报告.txt'
$services = [ordered]@{
    'api.shinnytech.com' = '接口'
    'auth.shinnytech.com' = '认证'
    'openmd.shinnytech.com' = '行情'
    'files.shinnytech.com' = '资源'
    'www.shinnytech.com' = '官网'
}
$lines = @('OnlineTrader · 网络自检', ('时间：' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz')))
try {
    $dns = (Get-DnsClientServerAddress -AddressFamily IPv4).ServerAddresses | Sort-Object -Unique
    if ($dns) { $lines += ('本机 DNS：' + ($dns -join ', ')) }
} catch { $lines += '本机 DNS：读取失败' }
$proxy = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
$lines += $(if ($proxy.ProxyEnable) { '系统手动代理：已启用' } else { '系统手动代理：未启用' })

$reachable = 0
foreach ($endpoint in $services.Keys) {
    $prefix = "$endpoint [$($services[$endpoint])]"
    try {
        $addresses = [Net.Dns]::GetHostAddresses($endpoint)
        if (-not $addresses) { throw 'No address' }
    } catch { $lines += "$prefix  DNS 失败"; continue }
    $connected = $null
    foreach ($address in $addresses) {
        $client = [Net.Sockets.TcpClient]::new($address.AddressFamily)
        try {
            $task = $client.ConnectAsync($address,443)
            if ($task.Wait(2000) -and $client.Connected) { $connected = $address; break }
        } catch { } finally { $client.Dispose() }
    }
    if ($null -eq $connected) { $lines += "$prefix  DNS 正常，TCP 443 不可达"; continue }
    try {
        $response = Invoke-WebRequest -Uri "https://$endpoint/" -Method Head -TimeoutSec 5 -UseBasicParsing
        $status = [int]$response.StatusCode
    } catch {
        $response = $_.Exception.Response
        $status = if ($null -ne $response) { [int]$response.StatusCode } else { 0 }
    }
    if ($status -ge 100 -and $status -lt 500) {
        $reachable++
        $lines += "$prefix  $connected  TCP 443 正常，HTTPS 响应 $status"
    }
    elseif ($status -ge 500) { $lines += "$prefix  $connected  TCP 443 正常，HTTPS 服务错误 $status" }
    else { $lines += "$prefix  $connected  TCP 443 正常，HTTPS 请求失败" }
}
$lines += ('结果：{0} / {1} 个服务正常返回 HTTPS 响应。' -f $reachable,$services.Count)
$lines | Write-Host
$lines | Set-Content -LiteralPath $report -Encoding UTF8
Write-Host "报告已保存：$report"
if (-not $NoPrompt) { Read-Host '按回车退出' | Out-Null }
