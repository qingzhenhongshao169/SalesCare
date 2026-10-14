$ErrorActionPreference = 'Stop'
$serviceRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$servicePython = (Resolve-Path (Join-Path $serviceRoot '.mvp-venv/Scripts/python.exe')).Path
$pythonHomeEntry = Get-Content -LiteralPath (Join-Path $serviceRoot '.mvp-venv/pyvenv.cfg') | Where-Object { $_ -match '^home = ' } | Select-Object -First 1
$serviceBasePython = Join-Path ($pythonHomeEntry -replace '^home = ', '') 'python.exe'
$serviceExecutables = @($servicePython, $serviceBasePython)
$servicePids = Get-Content -LiteralPath (Join-Path $serviceRoot '.mvp-runtime/pids.json') | ConvertFrom-Json
$allServiceProcesses = @(Get-CimInstance Win32_Process)
$serviceTargets = @()
foreach ($serviceName in @('supervisor', 'api', 'worker', 'channels', 'knowledge')) {
    if (-not $servicePids.$serviceName) { continue }
    $serviceIdentity = [int]$servicePids.$serviceName
    $servicePattern = @{supervisor='scripts.run_mvp.py.*--port 8767';api='uvicorn mvp.app:app.*--port 8767';worker='-m mvp.worker';channels='-m mvp.channel_gateway';knowledge='-m mvp.knowledge_worker'}[$serviceName]
    $serviceProcess = $allServiceProcesses | Where-Object ProcessId -eq $serviceIdentity
    if (-not $serviceProcess) { continue }
    if ($serviceProcess.ExecutablePath -notin $serviceExecutables -or $serviceProcess.CommandLine -notmatch $servicePattern -or -not $serviceProcess.CommandLine.Contains($servicePython)) { throw "Unexpected $serviceName process; no service has been stopped." }
    $serviceChildren = @()
    if ($serviceName -ne 'supervisor') { $serviceChildren = @($allServiceProcesses | Where-Object { $_.ParentProcessId -eq $serviceIdentity -and $_.ExecutablePath -in $serviceExecutables }) }
    foreach ($serviceChild in $serviceChildren) {
        if ($serviceChild.CommandLine -notmatch $servicePattern -or -not $serviceChild.CommandLine.Contains($servicePython)) { throw 'Unexpected Python child; no service has been stopped.' }
    }
    $serviceTargets += @{name=$serviceName;root=$serviceProcess;children=$serviceChildren}
}
$supervisorTarget = $serviceTargets | Where-Object name -eq 'supervisor'
if ($supervisorTarget) { Stop-Process -Id $supervisorTarget.root.ProcessId }
foreach ($serviceTarget in $serviceTargets | Where-Object name -ne 'supervisor') {
    foreach ($serviceChildTarget in @($serviceTarget.children)+@($serviceTarget.root)) {
        $currentServiceProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($serviceChildTarget.ProcessId)"
        if ($currentServiceProcess -and $currentServiceProcess.CommandLine -eq $serviceChildTarget.CommandLine) { Stop-Process -Id $currentServiceProcess.ProcessId -ErrorAction SilentlyContinue }
    }
}
Start-Process -FilePath $servicePython -ArgumentList @('scripts/run_mvp.py','--host','0.0.0.0','--port','8767') -WorkingDirectory $serviceRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $serviceRoot '.mvp-runtime/startup.out.log') -RedirectStandardError (Join-Path $serviceRoot '.mvp-runtime/startup.err.log')
Write-Output 'Verified SalesCare MVP restarted. Database, other services and company data retained.'
