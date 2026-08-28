<#
Register the Windows Scheduled Tasks for daily-hotspots (idempotent: re-running updates the action):
  * `DailyHotspots`, the DAILY radar (08:07 local, off-:00 to avoid herd) -> wrapper.ps1.
  * `DailyHotspotsYield`, the WEEKLY self-evolve signal-yield pass (spec §8/§9) -> yield-wrapper.ps1.
    Replays the archive (numerator) against the pulls-log (denominator, written daily by the radar via
    run.py --sources) to keep the roster honest: reversible auto-prune + a propose-add review queue.
    WITHOUT this task the yield engine is inert (audit HARDEN r4). Pass -SkipYield to register only the
    daily task, -YieldReportOnly to have the weekly pass NOT apply prune (report + review queue only).
  * `DailyHotspotsCompleteness`, the DAILY archive completeness scan -> wrapper.ps1 -CompletenessOnly.
    This is a SEPARATE question from "did today run". The task-health monitor watches the newest
    artifact's mtime, which is liveness only, so a single good day hides every older hole forever.
    WITHOUT this task the scanner is inert. Pass -SkipCompleteness to register only the other tasks.

  powershell -ExecutionPolicy Bypass -File register-task.ps1 [-ConfigDir C:\path\daily-hotspots-config]

Unregister:  Unregister-ScheduledTask -TaskName DailyHotspots -Confirm:$false
             Unregister-ScheduledTask -TaskName DailyHotspotsYield -Confirm:$false
             Unregister-ScheduledTask -TaskName DailyHotspotsCompleteness -Confirm:$false
#>
param(
  [string]$ConfigDir = "",
  [string]$Time = "08:07",
  [string]$Python = "",
  [string]$YieldTime = "08:37",
  [string]$YieldDay = "Monday",
  [switch]$SkipYield = $false,
  [switch]$YieldReportOnly = $false,
  [string]$CompletenessTime = "09:11",
  [switch]$SkipCompleteness = $false
)
$ErrorActionPreference = "Stop"
$wrapper = Join-Path $PSScriptRoot "wrapper.ps1"
if (-not (Test-Path $wrapper)) { throw "wrapper.ps1 not found next to this script" }

$argline = "-ExecutionPolicy Bypass -NoProfile -File `"$wrapper`""
if ($Python)    { $argline += " -Python `"$Python`"" }
if ($ConfigDir) { $argline += " -ConfigDir `"$ConfigDir`"" }

# ---- the execution limit is DERIVED from the wrapper's transport budget, never guessed ----------
# The wrapper spends DAILY_HOTSPOTS_AGENT_TIMEOUT (default 2400s) on the primary orchestration leg
# and can then spend a second leg of the same size on the fallback transport. A limit below that sum
# guillotines a run mid-flight, and a scheduler-terminated run is the one failure shape that writes
# no exit code and raises no alert, so it is invisible in exactly the unattended case it happens in.
# The file used to say `New-TimeSpan -Hours 1`, so re-running this script CUT the live PT2H limit.
# These are literal so the relation is readable, and the throw below is what enforces it: editing one
# number without the others stops the script instead of silently re-registering a fatal limit.
$AgentTimeoutSec = 2400
$TransportBudgetSec = 4800
$ExecutionLimitSec = 5400
if ($TransportBudgetSec -lt (2 * $AgentTimeoutSec)) {
  throw "TransportBudgetSec ($TransportBudgetSec) must cover both transport legs (2 x $AgentTimeoutSec); refusing to register a limit derived from a budget that already understates the run"
}
if ($ExecutionLimitSec -le $TransportBudgetSec) {
  throw "ExecutionLimitSec ($ExecutionLimitSec) must exceed the transport budget ($TransportBudgetSec); registering it would guillotine runs mid-flight with no exit code and no alert"
}

$action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argline
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# -AllowStartIfOnBatteries / -DontStopIfGoingOnBatteries: the defaults are the opposite, and they
# dropped whole days with the machine up and awake. A task that never started leaves no wrapper log
# and no skill-side alert, so those days were only found by counting holes in the archive.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Seconds $ExecutionLimitSec)

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
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
  Register-ScheduledTask -TaskName "DailyHotspotsYield" -Action $yaction -Trigger $ytrigger `
    -Settings $ysettings -Description "daily-hotspots: weekly signal-yield pass (roster self-evolve)" -Force | Out-Null
  Write-Host "Registered DailyHotspotsYield at $YieldTime every $YieldDay. Wrapper: $yieldWrapper"
}

# DAILY archive completeness scan. Runs AFTER the radar so it sees today's digest if there is one,
# and it is deliberately its own task rather than a tail of the daily run: a run that dies, or that
# the scheduler terminates, never reaches its own tail, which is precisely when a hole gets created.
# The check that finds missing days must not share a fate with the thing that goes missing.
if (-not $SkipCompleteness) {
  $cargline = "-ExecutionPolicy Bypass -NoProfile -File `"$wrapper`" -CompletenessOnly"
  if ($Python)    { $cargline += " -Python `"$Python`"" }
  if ($ConfigDir) { $cargline += " -ConfigDir `"$ConfigDir`"" }
  $caction  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $cargline
  $ctrigger = New-ScheduledTaskTrigger -Daily -At $CompletenessTime
  $csettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
  Register-ScheduledTask -TaskName "DailyHotspotsCompleteness" -Action $caction -Trigger $ctrigger `
    -Settings $csettings -Description "daily-hotspots: daily archive completeness scan" -Force | Out-Null
  Write-Host "Registered DailyHotspotsCompleteness at $CompletenessTime daily. Wrapper: $wrapper -CompletenessOnly"
}
