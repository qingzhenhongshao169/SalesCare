param(
    [ValidateRange(1, 65535)][int]$Port = 8766,
    [string]$ListenAddress = '127.0.0.1'
)
$ErrorActionPreference = 'Stop'
$parsedAddress = $null
if (![System.Net.IPAddress]::TryParse($ListenAddress, [ref]$parsedAddress) -or $parsedAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
    throw 'ListenAddress 必须是有效的 IPv4 地址，例如 127.0.0.1 或当前电脑的内网 IP。'
}
Set-Location $PSScriptRoot
$pythonPath = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $pythonPath)) { $pythonPath = Join-Path $PSScriptRoot '../.venv/Scripts/python.exe' }
if (!(Test-Path -LiteralPath $pythonPath)) { throw '请先按 README 创建 Python 虚拟环境并安装依赖。' }
if (!(Test-Path -LiteralPath 'web/dist/index.html')) {
    Push-Location web
    try { npm.cmd ci; if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败' }; npm.cmd run build; if ($LASTEXITCODE -ne 0) { throw '前端编译失败' } }
    finally { Pop-Location }
}
if ($ListenAddress -eq '0.0.0.0') {
    Write-Host "SalesCare demo: 监听所有 IPv4 网卡，访问时请使用电脑实际内网 IP 和端口 $Port。"
} else {
    Write-Host "SalesCare demo: http://${ListenAddress}:$Port"
}
if ($ListenAddress -ne '127.0.0.1') {
    Write-Host '内网演示：当前仍可免密切换管理者，请仅向获授权的测试人员开放；防火墙需按访问范围单独配置。'
}
& $pythonPath -m uvicorn server.app:app --host $ListenAddress --port $Port
