param([string]$LocalAddress='192.168.0.236',[string]$AllowedSubnet='192.168.0.0/23',[ValidateSet('Public','Private','Domain')][string]$NetworkProfile='Public')
$ErrorActionPreference='Stop'
$result=Join-Path $PSScriptRoot '.mvp-runtime/firewall-result.json'
try {
    $principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator privilege required.' }
    if (!(Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -eq $LocalAddress)) { throw 'LAN address is not assigned.' }
    $ruleName='SalesCare-MVP-LAN-8767'
    $description='SalesCare MVP: company subnet only, TCP 8767.'
    $existing=Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($existing -and $existing.Description -ne $description) { throw 'Unrelated firewall rule; no change made.' }
    if ($existing) { Set-NetFirewallRule -Name $ruleName -Enabled True -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8767 -LocalAddress $LocalAddress -RemoteAddress $AllowedSubnet -Profile $NetworkProfile | Out-Null }
    else { New-NetFirewallRule -Name $ruleName -DisplayName 'SalesCare MVP LAN 8767' -Description $description -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8767 -LocalAddress $LocalAddress -RemoteAddress $AllowedSubnet -Profile $NetworkProfile | Out-Null }
    [ordered]@{success=$true;address=$LocalAddress;subnet=$AllowedSubnet;port=8767} | ConvertTo-Json | Set-Content -LiteralPath $result -Encoding UTF8
} catch { [ordered]@{success=$false;error=$_.Exception.Message} | ConvertTo-Json | Set-Content -LiteralPath $result -Encoding UTF8; exit 1 }
