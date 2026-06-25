<#
Register the Windows Scheduled Task `DailyHotspots` (08:07 local, off-:00 to avoid herd).
Idempotent: re-running updates the action. Pass -ConfigDir to bind a companion config repo.

  powershell -ExecutionPolicy Bypass -File register-task.ps1 [-ConfigDir C:\path\daily-hotspots-config]

Unregister:  Unregister-ScheduledTask -TaskName DailyHotspots -Confirm:$false
#>
param(
  [string]$ConfigDir = "",
  [string]$Time = "08:07",
  [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$wrapper = Join-Path $PSScriptRoot "wrapper.ps1"
if (-not (Test-Path $wrapper)) { throw "wrapper.ps1 not found next to this script" }

$argline = "-ExecutionPolicy Bypass -NoProfile -File `"$wrapper`""
if ($Python)    { $argline += " -Python `"$Python`"" }
if ($ConfigDir) { $argline += " -ConfigDir `"$ConfigDir`"" }

$action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argline
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
  -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "DailyHotspots" -Action $action -Trigger $trigger `
  -Settings $settings -Description "daily-hotspots: frontier business-opportunity radar" -Force | Out-Null
Write-Host "Registered DailyHotspots at $Time daily. Wrapper: $wrapper"
