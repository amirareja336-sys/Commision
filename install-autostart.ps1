#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Start the Reconciliation Console at boot. Leaves Ethernet at 192.168.1.2.

.DESCRIPTION
  - Does not change the PC IP (keep 192.168.1.2)
  - Allows inbound TCP 8000 through Windows Firewall
  - Registers a Scheduled Task that launches start-autostart.bat at device startup
#>

$ErrorActionPreference = "Stop"

$StaticIp = "192.168.1.2"
$Port = 8000
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "ReconciliationConsole"
$Launcher = Join-Path $AppDir "start-autostart.bat"

Write-Host "Leaving Ethernet at $StaticIp (no IP change)."
Write-Host "Opening Windows Firewall for TCP $Port..."
$ruleName = "Reconciliation Console"
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null

Write-Host "Registering startup task '$TaskName'..."
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Launcher`"" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT30S"
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

Write-Host ""
Write-Host "Done. This PC is  $StaticIp"
Write-Host "The console will start at boot and listen on http://${StaticIp}:${Port}"
Write-Host "A permanent 'Reconciliation Console' log window stays open with server output."
Write-Host "Log in after reboot (or keep auto-logon) so the task can launch the server."
