param(
    [string]$LocalAddress = '192.168.0.236',
    [string]$AllowedSubnet = '192.168.0.0/23',
    [ValidateSet('Public', 'Private', 'Domain')][string]$NetworkProfile = 'Public'
)
$ErrorActionPreference = 'Stop'
$resultPath = Join-Path $PSScriptRoot 'runtime/lan-firewall-result.json'
$ruleName = 'SalesCare-LAN-8766'
try {
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This operation requires Windows administrator privileges.'
    }
    $assigned = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -eq $LocalAddress })
    if (!$assigned.Count) { throw 'The configured LAN IP is no longer assigned to this computer.' }
    $existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    $description = 'SalesCare local demo: TCP 8766, approved company subnet only.'
    if ($existing) {
        if ($existing.Description -ne $description) { throw 'An unrelated rule uses this name; no changes made.' }
        Set-NetFirewallRule -Name $ruleName -Enabled True -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8766 -LocalAddress $LocalAddress -RemoteAddress $AllowedSubnet -Profile $NetworkProfile | Out-Null
    } else {
        New-NetFirewallRule -Name $ruleName -DisplayName 'SalesCare LAN Demo 8766' -Description $description -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8766 -LocalAddress $LocalAddress -RemoteAddress $AllowedSubnet -Profile $NetworkProfile | Out-Null
    }
    $rule = Get-NetFirewallRule -Name $ruleName
    $ports = $rule | Get-NetFirewallPortFilter
    $addresses = $rule | Get-NetFirewallAddressFilter
    [ordered]@{success=$true;rule=$rule.Name;enabled=[string]$rule.Enabled;profile=[string]$rule.Profile;port=[string]$ports.LocalPort;local_address=@($addresses.LocalAddress);allowed_sources=@($addresses.RemoteAddress);at=(Get-Date).ToString('o')} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $resultPath -Encoding UTF8
} catch {
    [ordered]@{success=$false;error=$_.Exception.Message;at=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $resultPath -Encoding UTF8
    exit 1
}
