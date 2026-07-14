<#
Register the Windows Scheduled Tasks for daily-hotspots (idempotent: re-running updates the action):
  * `DailyHotspots`      — the DAILY radar (08:07 local, off-:00 to avoid herd) -> wrapper.ps1.
  * `DailyHotspotsYield` — the WEEKLY self-evolve signal-yield pass (spec §8/§9) -> yield-wrapper.ps1.
    Replays the archive (numerator) against the pulls-log (denominator, written daily by the radar via
    run.py --sources) to keep the roster honest: reversible auto-prune + a propose-add review queue.
    WITHOUT this task the yield engine is inert (audit HARDEN r4). Pass -SkipYield to register only the
    daily task, -YieldReportOnly to have the weekly pass NOT apply prune (report + review queue only).

  powershell -ExecutionPolicy Bypass -File register-task.ps1 [-ConfigDir C:\path\daily-hotspots-config]

Unregister:  Unregister-ScheduledTask -TaskName DailyHotspots -Confirm:$false
             Unregister-ScheduledTask -TaskName DailyHotspotsYield -Confirm:$false
#>
param(
  [string]$ConfigDir = "",
  [string]$Time = "08:07",
  [string]$Python = "",
  [string]$YieldTime = "08:37",
  [string]$YieldDay = "Monday",
  [switch]$SkipYield = $false,
  [switch]$YieldReportOnly = $false
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

# WEEKLY self-evolve yield pass (spec §8/§9). Deterministic archive replay (no LLM) -> a short limit.
if (-not $SkipYield) {
  $yieldWrapper = Join-Path $PSScriptRoot "yield-wrapper.ps1"
  if (-not (Test-Path $yieldWrapper)) { throw "yield-wrapper.ps1 not found next to this script" }
  $yargline = "-ExecutionPolicy Bypass -NoProfile -File `"$yieldWrapper`""
  if ($Python)          { $yargline += " -Python `"$Python`"" }
  if ($ConfigDir)       { $yargline += " -ConfigDir `"$ConfigDir`"" }
  if ($YieldReportOnly) { $yargline += " -ReportOnly" }
  $yaction  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $yargline
  $ytrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $YieldDay -At $YieldTime
  $ysettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
  Register-ScheduledTask -TaskName "DailyHotspotsYield" -Action $yaction -Trigger $ytrigger `
    -Settings $ysettings -Description "daily-hotspots: weekly signal-yield pass (roster self-evolve)" -Force | Out-Null
  Write-Host "Registered DailyHotspotsYield at $YieldTime every $YieldDay. Wrapper: $yieldWrapper"
}
