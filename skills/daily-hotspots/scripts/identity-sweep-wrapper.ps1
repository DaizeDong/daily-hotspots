<#
daily-hotspots MONTHLY identity-sweep wrapper for the Windows Task Scheduler (spec §9 guardrail 4).

The §9 drift/dead guardrail needs a get_user_info sweep over the rostered handles. identity_sweep.py
is a pure REST caller (twitterapi.io, NO MCP, NO LLM) that produces the sweep and feeds it to
run.py --yield --user-info --write-review, so renamed / dead handles surface in
archive/roster-review.md (report-only; a rename is a human edit, never auto-removed).

Mirrors yield-wrapper.ps1: ABSOLUTE python path (Task Scheduler PATH is minimal), notify-on-abort
via the Discord relay, and, the wrapper.ps1 lesson, the native python call runs under
$ErrorActionPreference='Continue' so a stray stderr line can't masquerade as a terminating
NativeCommandError (exit code is the only truth). Verify success by the ARTIFACT
(archive/roster-review.md's "flagged accounts" section), not just rc. Register monthly.

Env it sets for the run:
  DAILY_HOTSPOTS_CONFIG   (companion repo -> roster.json + archive live there)
#>
param(
  [string]$Python = "",
  [string]$ConfigDir = "",
  [string]$TokenFile = "",
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
    try { & $script:py $relay "[daily-hotspots:identity-sweep] ABORT: $msg" | Out-Null } catch {}
  }
}

try {
  $script:py = Resolve-Python $Python
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $stamp = Get-Date -Format "yyyy-MM"
  $log = Join-Path $LogDir "identity-sweep-$stamp.log"

  if ($ConfigDir) { $env:DAILY_HOTSPOTS_CONFIG = $ConfigDir }

  $sweeppy = Join-Path $PSScriptRoot "identity_sweep.py"
  if (-not (Test-Path $sweeppy)) { Notify-Abort "identity_sweep.py not found next to wrapper"; throw "identity_sweep.py missing" }

  "[$(Get-Date -Format o)] daily-hotspots MONTHLY identity sweep start (py=$script:py)" |
    Tee-Object -FilePath $log -Append

  $sweepArgs = @($sweeppy, "--feed-yield")
  if ($TokenFile) { $sweepArgs += @("--token-file", $TokenFile) }

  # wrapper.ps1 lesson: native call under Continue so a stderr line is not a terminating error.
  $ErrorActionPreference = "Continue"
  & $script:py @sweepArgs *>> $log
  $rc = $LASTEXITCODE
  $ErrorActionPreference = "Stop"

  "[$(Get-Date -Format o)] daily-hotspots identity sweep end rc=$rc" | Tee-Object -FilePath $log -Append
  if ($rc -ne 0) { Notify-Abort "identity_sweep.py exited rc=$rc (see $log)" }
  exit $rc
}
catch {
  Notify-Abort $_.Exception.Message
  throw
}
