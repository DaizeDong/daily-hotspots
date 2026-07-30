<#
daily-hotspots WEEKLY signal-yield pass wrapper for the Windows Task Scheduler (spec §8/§9).

Closes the self-evolve loop: the daily radar writes the pulls-log DENOMINATOR (run.py --sources)
and archives origin-tagged cards (the NUMERATOR); this weekly pass REPLAYS both to keep the X KOL
roster honest, reversible auto-prune of dead handles + a propose-add review queue for productive
non-roster voices.

Unlike wrapper.ps1 this needs NO LLM: the yield pass is a pure deterministic archive replay
(yield.py), so it calls python DIRECTLY (cheapest, most robust, no agent transport). The MONTHLY
get_user_info identity sweep (drift/dead handles, §9) is a SEPARATE task (identity_sweep.py, pure
REST over twitterapi.io, no MCP either; registered as DailyHotspotsIdentitySweep); see
reference/cron-setup.md.

Shares wrapper-common.ps1 with the other two registered wrappers: ABSOLUTE python path with the
WindowsApps alias stub REJECTED (Task Scheduler PATH is minimal), a log destination established
before anything that can fail, UTF-8 logging, and a notify path that reports its own failures
instead of swallowing them. Register with register-task.ps1 (weekly).

Behavior:
  default          -> run.py --yield --apply --write-review   (reversible prune fires; review queue written)
  -ReportOnly      -> run.py --yield --write-review           (no prune; report + review queue only)
Auto-prune is SAFE by construction: enabled=false (never a delete, un-prune from the review queue),
a no-op until 7 days of real history (cold-start), and every prune is logged with reason + stats.

Env it sets for the run:
  DAILY_HOTSPOTS_CONFIG   (if a companion repo path is given -> roster.json + archive live there)
Env it reads: DAILY_HOTSPOTS_PYTHON, DAILY_HOTSPOTS_RELAY, DAILY_HOTSPOTS_STREAM (see
wrapper-common.ps1 for each default and why each is existence-checked).
#>
param(
  [string]$Python = "",
  [string]$ConfigDir = "",
  [switch]$ReportOnly = $false,
  [string]$LogDir = "$env:USERPROFILE\.daily-hotspots-logs"
)
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'wrapper-common.ps1')

$script:STREAM = Resolve-Stream

function Notify-Abort {
  param([string]$msg)
  Send-Alert -Tag "daily-hotspots:yield" -Msg "ABORT: $msg" -Stream $script:STREAM -Python $script:py
}

try {
  # ORDER IS LOAD BEARING: establish the log BEFORE the interpreter resolution that can fail, because
  # under Task Scheduler the log is the only forensic artifact and "no usable python" is exactly the
  # failure that only happens there.
  $stamp = Get-Date -Format "yyyy-MM-dd"
  $log = Initialize-WrapperLog -LogDir $LogDir -Name "yield-$stamp.log"
  Write-Log "daily-hotspots WEEKLY yield pass start (reportOnly=$ReportOnly)"

  $script:py = Resolve-Python $Python
  Write-Log "python: $script:py"

  if ($ConfigDir) { $env:DAILY_HOTSPOTS_CONFIG = $ConfigDir }

  $runpy = Join-Path $PSScriptRoot "run.py"
  if (-not (Test-Path -LiteralPath $runpy)) { Notify-Abort "run.py not found next to wrapper"; throw "run.py missing" }

  $runArgs = @($runpy, "--yield", "--write-review")
  if (-not $ReportOnly) { $runArgs += "--apply" }   # reversible auto-prune (enabled=false), cold-start-gated

  # Invoke-ChildToLog runs the native call under ErrorActionPreference=Continue (a stray stderr line
  # must not become a terminating NativeCommandError), STREAMS the child's output into the same UTF-8
  # log the wrapper writes (`*>>` on PS 5.1 writes UTF-16 and would make the log half-unreadable, and
  # buffering would lose everything if the scheduler killed a long replay), and returns the exit code,
  # which is the only truth about whether it worked.
  $rc = Invoke-ChildToLog -Exe $script:py -Arguments $runArgs -Label "run.py --yield"
  if ($null -eq $rc) {
    Write-Loud "run.py --yield never reported an exit code; treating the pass as failed"
    $rc = 1
  }
  Write-Log "daily-hotspots yield pass end rc=$rc"
  if ($rc -ne 0) { Notify-Abort "run.py --yield exited rc=$rc (see $log)" }
  exit $rc
}
catch {
  # Write-Loud FIRST. The abort path used to notify and rethrow without ever putting the reason in
  # the log, so the failure that only happens unattended (no usable interpreter under Task
  # Scheduler's minimal PATH) also happened to be the one whose reason was never written down.
  Write-Loud "FATAL: $($_.Exception.Message)"
  Notify-Abort $_.Exception.Message
  throw
}
