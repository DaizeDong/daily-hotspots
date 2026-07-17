<#
daily-hotspots WEEKLY signal-yield pass wrapper for the Windows Task Scheduler (spec §8/§9).

Closes the self-evolve loop: the daily radar writes the pulls-log DENOMINATOR (run.py --sources)
and archives origin-tagged cards (the NUMERATOR); this weekly pass REPLAYS both to keep the X KOL
roster honest, reversible auto-prune of dead handles + a propose-add review queue for productive
non-roster voices.

Unlike wrapper.ps1 this needs NO LLM: the yield pass is a pure deterministic archive replay
(yield.py), so it calls python DIRECTLY (cheapest, most robust, no claude -p). The MONTHLY
get_user_info identity sweep (drift/dead handles, §9) is a SEPARATE task (identity_sweep.py, pure
REST over twitterapi.io, no MCP either; registered as DailyHotspotsIdentitySweep); see
reference/cron-setup.md.

Mirrors wrapper.ps1: ABSOLUTE python path (Task Scheduler PATH is minimal), fail-fast preflight,
notify-on-abort via the Discord relay. Register with register-task.ps1 (weekly).

Behavior:
  default          -> run.py --yield --apply --write-review   (reversible prune fires; review queue written)
  -ReportOnly      -> run.py --yield --write-review           (no prune; report + review queue only)
Auto-prune is SAFE by construction: enabled=false (never a delete, un-prune from the review queue),
a no-op until 7 days of real history (cold-start), and every prune is logged with reason + stats.

Env it sets for the run:
  DAILY_HOTSPOTS_CONFIG   (if a companion repo path is given -> roster.json + archive live there)
#>
param(
  [string]$Python = "",
  [string]$ConfigDir = "",
  [switch]$ReportOnly = $false,
  [string]$LogDir = "$env:USERPROFILE\.daily-hotspots-logs"
)
$ErrorActionPreference = "Stop"

function Resolve-Python {
  param([string]$p)
  if ($p -and (Test-Path $p)) { return $p }
  $c = (Get-Command python -ErrorAction SilentlyContinue)
  if ($c) { return $c.Source }
  throw "python not found; pass -Python <abs path>"
}

function Notify-Abort {
  param([string]$msg)
  $relay = "the relay"
  if (Test-Path $relay) {
    try { & $script:py $relay "[daily-hotspots:yield] ABORT: $msg" | Out-Null } catch {}
  }
}

try {
  $script:py = Resolve-Python $Python
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $stamp = Get-Date -Format "yyyy-MM-dd"
  $log = Join-Path $LogDir "yield-$stamp.log"

  if ($ConfigDir) { $env:DAILY_HOTSPOTS_CONFIG = $ConfigDir }

  $runpy = Join-Path $PSScriptRoot "run.py"
  if (-not (Test-Path $runpy)) { Notify-Abort "run.py not found next to wrapper"; throw "run.py missing" }

  $runArgs = @($runpy, "--yield", "--write-review")
  if (-not $ReportOnly) { $runArgs += "--apply" }   # reversible auto-prune (enabled=false), cold-start-gated

  "[$(Get-Date -Format o)] daily-hotspots WEEKLY yield pass start (py=$script:py, reportOnly=$ReportOnly)" |
    Tee-Object -FilePath $log -Append

  & $script:py @runArgs *>> $log
  $rc = $LASTEXITCODE
  "[$(Get-Date -Format o)] daily-hotspots yield pass end rc=$rc" | Tee-Object -FilePath $log -Append
  if ($rc -ne 0) { Notify-Abort "run.py --yield exited rc=$rc (see $log)" }
  exit $rc
}
catch {
  Notify-Abort $_.Exception.Message
  throw
}
