$ErrorActionPreference='Stop'
$mvpRoot=Split-Path -Parent $PSScriptRoot
$python=Join-Path $mvpRoot '.mvp-venv/Scripts/python.exe'
$script=Join-Path $mvpRoot 'scripts/run_mvp.py'
$scheduler=New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$folder=$scheduler.GetFolder('\')
$task=$scheduler.NewTask(0)
$task.RegistrationInfo.Description='SalesCare MVP local pilot supervisor, current user only.'
$task.Settings.Enabled=$true
$task.Settings.StartWhenAvailable=$true
$task.Settings.DisallowStartIfOnBatteries=$false
$task.Settings.StopIfGoingOnBatteries=$false
$task.Settings.ExecutionTimeLimit='PT0S'
$task.Settings.MultipleInstances=2
$task.Principal.UserId=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$task.Principal.LogonType=3
$trigger=$task.Triggers.Create(9)
$trigger.UserId=$task.Principal.UserId
$action=$task.Actions.Create(0)
$action.Path=$python
$action.Arguments=('"{0}" --host 0.0.0.0 --port 8767' -f $script)
$action.WorkingDirectory=$mvpRoot
$folder.RegisterTaskDefinition('SalesCare-MVP-Pilot',$task,6,$null,$null,3,$null) | Out-Null
Write-Output 'MVP restart on current-user sign-in registered. This is not a system boot service.'
