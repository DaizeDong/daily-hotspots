<#
daily-hotspots MONTHLY identity-sweep wrapper for the Windows Task Scheduler (spec §9 guardrail 4).

The §9 drift/dead guardrail needs a get_user_info sweep over the rostered handles. identity_sweep.py
is a pure REST caller (twitterapi.io, NO MCP, NO LLM) that produces the sweep and feeds it to
run.py --yield --user-info --write-review, so renamed / dead handles surface in
archive/roster-review.md (report-only; a rename is a human edit, never auto-removed).

Shares wrapper-common.ps1 with the other two registered wrappers: ABSOLUTE python path with the
WindowsApps alias stub REJECTED (Task Scheduler PATH is minimal), a log destination established
before anything that can fail, UTF-8 logging, a notify path that reports its own failures instead of
swallowing them, and, the wrapper.ps1 lesson, the native python call runs under
$ErrorActionPreference='Continue' so a stray stderr line cannot masquerade as a terminating
NativeCommandError (the exit code is the only truth). Verify success by the ARTIFACT
(archive/roster-review.md's "flagged accounts" section), not just rc. Register monthly.

Env it sets for the run:
  DAILY_HOTSPOTS_CONFIG   (companion repo -> roster.json + archive live there)
Env it reads: DAILY_HOTSPOTS_PYTHON, DAILY_HOTSPOTS_RELAY, DAILY_HOTSPOTS_STREAM (see
wrapper-common.ps1 for each default and why each is existence-checked).
#>
param(
  [string]$Python = "",
  [string]$ConfigDir = "",
  [string]$TokenFile = "",
  [string]$LogDir = "$env:USERPROFILE\.daily-hotspots-logs"
)
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot 'wrapper-common.ps1')

$script:STREAM = Resolve-Stream

function Notify-Abort {
  param([string]$msg)
  Send-Alert -Tag "daily-hotspots:identity-sweep" -Msg "ABORT: $msg" -Stream $script:STREAM -Python $script:py
}

try {
  # ORDER IS LOAD BEARING: establish the log BEFORE the interpreter resolution that can fail, because
  # under Task Scheduler the log is the only forensic artifact and "no usable python" is exactly the
  # failure that only happens there.
  $stamp = Get-Date -Format "yyyy-MM"
  $log = Initialize-WrapperLog -LogDir $LogDir -Name "identity-sweep-$stamp.log"
  Write-Log "daily-hotspots MONTHLY identity sweep start"

  $script:py = Resolve-Python $Python
  Write-Log "python: $script:py"

  if ($ConfigDir) { $env:DAILY_HOTSPOTS_CONFIG = $ConfigDir }

  $sweeppy = Join-Path $PSScriptRoot "identity_sweep.py"
  if (-not (Test-Path -LiteralPath $sweeppy)) { Notify-Abort "identity_sweep.py not found next to wrapper"; throw "identity_sweep.py missing" }

  $sweepArgs = @($sweeppy, "--feed-yield")
  if ($TokenFile) { $sweepArgs += @("--token-file", $TokenFile) }

  $rc = Invoke-ChildToLog -Exe $script:py -Arguments $sweepArgs -Label "identity_sweep.py"
  if ($null -eq $rc) {
    Write-Loud "identity_sweep.py never reported an exit code; treating the sweep as failed"
    $rc = 1
  }
  Write-Log "daily-hotspots identity sweep end rc=$rc"
  if ($rc -ne 0) { Notify-Abort "identity_sweep.py exited rc=$rc (see $log)" }
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
