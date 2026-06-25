<#
daily-hotspots headless wrapper for the Windows Task Scheduler.

Mirrors the refresh-market-intel pattern: ABSOLUTE python/git paths (Task Scheduler PATH is
minimal — a bare `python` half-runs and silently fails), fail-fast preflight, notify-on-abort.
It does NOT use the in-session CronCreate tool (session-only = wrong primitive).

Register once with register-task.ps1 (08:07 local). It invokes `claude -p` headless so the SKILL
orchestration (LLM multi-source collection) runs, then the deterministic run.py disposes.

Env it sets for the run:
  DAILY_HOTSPOTS_CONFIG       (if a companion repo path is given)
  SCHEDULE_DB_PATH            (local NTFS ledger db; never OneDrive/network = WAL corruption)
#>
param(
  [string]$Python = "",
  [string]$ConfigDir = "",
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
    try { & $script:py $relay "[daily-hotspots] ABORT: $msg" | Out-Null } catch {}
  }
}

try {
  $script:py = Resolve-Python $Python
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $stamp = Get-Date -Format "yyyy-MM-dd"
  $log = Join-Path $LogDir "run-$stamp.log"

  # preflight: claude CLI present?
  $claude = (Get-Command claude -ErrorAction SilentlyContinue)
  if (-not $claude) { Notify-Abort "claude CLI not on PATH"; throw "claude CLI missing" }

  if ($ConfigDir) { $env:DAILY_HOTSPOTS_CONFIG = $ConfigDir }
  # ledger on local NTFS (default under home; override via SCHEDULE_DB_PATH before calling)
  if (-not $env:SCHEDULE_DB_PATH) {
    $env:SCHEDULE_DB_PATH = "$env:USERPROFILE\.schedule-reminder\schedule.db"
  }

  "[$(Get-Date -Format o)] daily-hotspots run start (py=$script:py)" | Tee-Object -FilePath $log -Append
  # headless: ask the skill to run today's radar end-to-end
  & $claude.Source -p "Run the daily-hotspots skill now: collect today's frontier business opportunities across all configured sources, score, dedup, push to Discord, and archive." --dangerously-skip-permissions *>> $log
  $rc = $LASTEXITCODE
  "[$(Get-Date -Format o)] daily-hotspots run end rc=$rc" | Tee-Object -FilePath $log -Append
  if ($rc -ne 0) { Notify-Abort "claude -p exited rc=$rc (see $log)" }
  exit $rc
}
catch {
  Notify-Abort $_.Exception.Message
  throw
}
