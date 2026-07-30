<#
Shared preflight / logging / notify primitives for the THREE daily-hotspots Task Scheduler wrappers:
wrapper.ps1 (daily radar), yield-wrapper.ps1 (weekly yield pass), identity-sweep-wrapper.ps1
(monthly identity sweep). All three are registered tasks; all three run unattended under a minimal
PATH with no console to read.

Why one shared file instead of a third copy. The daily wrapper was hardened alone, and the two
siblings kept the exact defects it had just shed:
  * `Resolve-Python` that accepts whatever `Get-Command python` hands back, including the
    WindowsApps App-Execution-Alias stub, which under Task Scheduler neither runs nor fails, it just
    sits there (recorded incident: a task pinned at 0.1s CPU for over an hour).
  * a notify path shaped `if (Test-Path $relay) { try { ... } catch {} }`, which turns BOTH a missing
    relay AND a failing relay into silence, so every alert the function exists to deliver is dropped
    without a trace.
Fixing that in one copy and not the others is how two of three stayed broken across three rounds of
remediation. There is now ONE copy of each.

Use it as the first statement of a wrapper:

    . (Join-Path $PSScriptRoot 'wrapper-common.ps1')

Dot-source, not Import-Module, and that is load bearing: dot-sourcing defines these functions in the
CALLER's script scope, so the `$script:log` that Initialize-WrapperLog assigns is the same variable
Write-Log reads and the same one the wrapper can see. Verified by execution, not assumed. Under
Import-Module each function would get its own module scope and the log destination would silently
never reach the wrapper.
#>

# Documented interpreter fallbacks, tried in order and existence-checked. This list exists so the
# resolver never has to hand back the bare word `python`. Override with -Python or with
# $DAILY_HOTSPOTS_PYTHON on a machine whose interpreter is somewhere else.
$script:PYTHON_FALLBACKS = @(
  "C:\ProgramData\miniconda3\python.exe",
  "C:\ProgramData\Anaconda3\python.exe",
  "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
  "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
)

function Resolve-Python {
  <#
    Order: -Python, then $DAILY_HOTSPOTS_PYTHON, then the documented fallback list, then PATH with
    WindowsApps alias stubs REJECTED. Throws if nothing usable exists.

    An EXPLICIT setting that points at a missing file throws instead of quietly sliding down to the
    next tier: silently running a different interpreter than the operator named is how a machine
    ends up "working" while importing a different site-packages than anyone believes.
  #>
  param([string]$p)
  if ($p) {
    if (Test-Path -LiteralPath $p) { return (Resolve-Path -LiteralPath $p).Path }
    throw "-Python '$p' does not exist"
  }
  if ($env:DAILY_HOTSPOTS_PYTHON) {
    if (Test-Path -LiteralPath $env:DAILY_HOTSPOTS_PYTHON) {
      return (Resolve-Path -LiteralPath $env:DAILY_HOTSPOTS_PYTHON).Path
    }
    throw "DAILY_HOTSPOTS_PYTHON points at '$env:DAILY_HOTSPOTS_PYTHON', which does not exist"
  }
  foreach ($cand in $script:PYTHON_FALLBACKS) {
    if ($cand -and (Test-Path -LiteralPath $cand)) { return (Resolve-Path -LiteralPath $cand).Path }
  }
  foreach ($c in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
    $s = $c.Source
    # The alias stub lives under ...\AppData\Local\Microsoft\WindowsApps and is a reparse point that
    # opens the Store instead of executing. Never accept it.
    if ($s -and ($s -notmatch '\\WindowsApps\\') -and (Test-Path -LiteralPath $s)) { return $s }
  }
  throw "no usable python interpreter found (WindowsApps alias stubs are rejected); pass -Python <abs path> or set DAILY_HOTSPOTS_PYTHON"
}

function Resolve-Git {
  <#
    Absolute git path or $null, never the bare word `git`.

    Same reasoning as Resolve-Python: Task Scheduler hands the task a minimal PATH, and a step that
    cannot find its executable should say so once, up front, rather than have every git call fail
    with a different symptom. Callers treat $null as "announce the skip", not as "carry on".
  #>
  foreach ($cand in @("$env:ProgramFiles\Git\cmd\git.exe",
                      "${env:ProgramFiles(x86)}\Git\cmd\git.exe",
                      "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe")) {
    if ($cand -and (Test-Path -LiteralPath $cand)) { return (Resolve-Path -LiteralPath $cand).Path }
  }
  $c = Get-Command git -ErrorAction SilentlyContinue
  if ($c -and $c.Source -and (Test-Path -LiteralPath $c.Source)) { return $c.Source }
  return $null
}

function Write-Log {
  <#
    The ONE append path for a wrapper's log lines, and the reason it is not Tee-Object: on PS 5.1
    BOTH Tee-Object and the `>>` / `*>>` redirection operators write UTF-16, so a log that also
    receives plain UTF-8 bytes ends up half one and half the other, and whichever encoding a reader
    picks, the rest comes back as mojibake. Measured, not assumed. For an unattended nightly job the
    log is the only forensic artifact left behind, so everything a wrapper writes through this
    function is UTF-8 without a BOM. Anything a wrapper wants in the log goes through here or
    through an explicit `Out-File -Encoding utf8`; nothing uses `*>>`.

    Not under our control: on the fallback leg the external agent runner is handed the same -Log path
    and appends its own lines in its own encoding, so a fallback run's log can still be mixed. Decode
    it leniently. The common case, primary leg only, is uniformly UTF-8.

    Never throws: logging must not be able to fail the run it is reporting on.
  #>
  param([string]$msg)
  $line = "[$(Get-Date -Format o)] $msg"
  try {
    if ($script:log) {
      [System.IO.File]::AppendAllText($script:log, $line + [Environment]::NewLine,
                                      (New-Object System.Text.UTF8Encoding $false))
    }
  } catch {}
  try { Write-Host $line } catch {}
}

function Write-Loud {
  # A log line that is ALSO pushed to the warning stream, for conditions that must never be
  # swallowed. Never throws, so it is safe to call from the abort path.
  param([string]$msg)
  Write-Log $msg
  try { Write-Warning $msg } catch {}
}

function Initialize-WrapperLog {
  <#
    Establishes $script:log BEFORE anything that can fail, and returns the path it settled on.

    Why the ordering matters: the wrappers used to resolve the interpreter first and name the log
    second, so the one failure that only ever happens unattended (no usable python under Task
    Scheduler's minimal PATH) threw while Write-Log was still a no-op and left NO durable evidence
    in the only environment where the evidence is all you get. Callers now call this first.

    Each candidate directory is probed by actually appending to the file, because a directory can
    exist and still be unwritable by the task's account, and discovering that at the first real log
    line is discovering it too late to fall back. Falls back LogDir -> TEMP -> USERPROFILE, then
    console only. Never throws.
  #>
  param([string]$LogDir, [string]$Name)
  $tried = @()
  foreach ($dir in @($LogDir, $env:TEMP, $env:USERPROFILE)) {
    if (-not $dir) { continue }
    $tried += $dir
    try {
      if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null
      }
      $p = Join-Path $dir $Name
      [System.IO.File]::AppendAllText($p, "", (New-Object System.Text.UTF8Encoding $false))
      $script:log = $p
      if ($dir -ne $LogDir) {
        Write-Loud "log directory '$LogDir' is unusable; this run's log fell back to '$p'"
      }
      return $p
    } catch { }
  }
  $script:log = $null
  try {
    Write-Warning ("no writable log destination (tried: " + ($tried -join ', ') +
                   "); this run reports to the console only, which under Task Scheduler is nowhere")
  } catch {}
  return $null
}

function Resolve-Stream {
  # Agent Center stream/channel key the relay routes this skill's alerts to. Lifted out of the call
  # sites so the literal appears ONCE: the name has to match a stream key the relay knows about, and
  # when it does not the relay does not error, it quietly downgrades to a direct message, so a stale
  # literal buried in call sites silently reroutes ops alerts away from the channel that is watched.
  if ($env:DAILY_HOTSPOTS_STREAM) { return $env:DAILY_HOTSPOTS_STREAM }
  return "hotspots"
}

function Send-Alert {
  <#
    Push one operator alert through the relay, and REPORT anything that stops it.

    The shape this replaces was `if (Test-Path $relay) { try { ... } catch {} }`: a missing relay and
    a failing relay both became silence, so every alert the caller wrote was dropped without a trace.
    Here every exit path logs, the relay's own exit code is checked, and the function still never
    throws, because callers invoke it from their outer catch and a throw here would mask the original
    failure. Relay calling convention: `send --stream <name> --text <msg>`.

    -Python is the caller's already-resolved interpreter, passed in because a failed interpreter
    resolution is itself an alert cause; when it is empty this makes one independent best effort
    attempt of its own before giving up loudly.
  #>
  param([string]$Tag, [string]$Msg, [string]$Stream = "", [string]$Python = "")
  if (-not $Stream) { $Stream = Resolve-Stream }
  $relay = if ($env:DAILY_HOTSPOTS_RELAY) { $env:DAILY_HOTSPOTS_RELAY } else { "$env:USERPROFILE\.local\relay.py" }
  $text  = "[$Tag] $Msg"
  if (-not (Test-Path -LiteralPath $relay)) {
    Write-Loud "ALERT NOT DELIVERED (relay '$relay' does not exist; set DAILY_HOTSPOTS_RELAY): $text"
    return
  }
  $py = $Python
  if (-not $py) {
    try { $py = Resolve-Python "" } catch { $py = $null }
  }
  if (-not $py) {
    Write-Loud "ALERT NOT DELIVERED (no usable python interpreter to run the relay): $text"
    return
  }
  try {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"     # a relay stderr line must not become a terminating error
    & $py $relay send --stream $Stream --text $text | Out-Null
    $rrc = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($rrc -ne 0) { Write-Loud "ALERT NOT DELIVERED (relay exited rc=$rrc): $text" }
  } catch {
    $ErrorActionPreference = "Stop"
    Write-Loud "ALERT NOT DELIVERED (relay threw: $($_.Exception.Message)): $text"
  }
}

function Invoke-Child {
  <#
    Run one native child, log ALL of its output through Write-Log (so it lands in the same UTF-8 the
    rest of the log is written in), and RETURN its exit code so the caller can look at it.

    Two things this exists to stop, both of which shipped:
      * `& git add archive/ *>> $log` writes UTF-16 into a UTF-8 log AND discards the exit code, so a
        failed step is invisible twice over.
      * a chain of steps where only the LAST one's code is captured, which reports success whenever
        the last step is a no-op. `git push` returning 0 for "Everything up-to-date" after a failed
        commit is exactly that.

    Runs under ErrorActionPreference=Continue on purpose: with Stop, a single stderr line from a
    native child becomes a terminating NativeCommandError, and git writes ordinary progress to
    stderr, so a step that fully succeeded would be thrown away as a failure. $LASTEXITCODE is the
    only truth here; $? is not consulted.

    $LASTEXITCODE is nulled FIRST, and that is not decoration. Under Continue, a child that cannot be
    LAUNCHED at all (missing exe, bad path) writes an error and leaves $LASTEXITCODE holding whatever
    the previous child set, so a chain whose earlier step succeeded reads a stale 0 and calls the
    unlaunchable step a success. Measured on PS 5.1, not assumed. Pre-nulling makes "never ran"
    readable as $null instead of as somebody else's 0.
  #>
  param([string]$Exe, [string[]]$Arguments, [string]$Label = "")
  if (-not $Label) { $Label = "$Exe $($Arguments -join ' ')" }
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  $out = $null
  $code = $null
  try {
    $global:LASTEXITCODE = $null
    $out  = & $Exe @Arguments 2>&1
    $code = $LASTEXITCODE
  } catch {
    $code = $null
    Write-Loud "$Label threw before reporting an exit code: $($_.Exception.Message)"
  } finally {
    $ErrorActionPreference = $prev
  }
  foreach ($line in @($out)) {
    $t = "$line".Trim()
    if ($t) { Write-Log "  $Label | $t" }
  }
  # $null means the child never reported. Callers must treat that as failure, never as 0.
  return $code
}

function Invoke-ChildToLog {
  <#
    Streaming sibling of Invoke-Child, for the LONG children (a 17-minute radar collection, a yield
    replay) where holding the output until the process exits is the wrong trade: the run needs to
    report progress live, and a run the scheduler kills mid-flight must still leave behind whatever
    it had produced. Invoke-Child buffers; this one appends line by line.

    Same encoding contract as everything else here: `Out-File -Encoding utf8`, never `*>>`, because
    PS 5.1's redirection operators write UTF-16 and half a log in each encoding is a log nobody can
    read. Returns the exit code, or $null if the child never reported one; $null is a failure and
    callers must resolve it as such rather than defaulting to 0. $LASTEXITCODE is pre-nulled for the
    same reason as in Invoke-Child: an unlaunchable child otherwise reads back the PREVIOUS child's
    exit code, which is a stale 0 exactly when the run had been going well.
  #>
  param([string]$Exe, [string[]]$Arguments, [string]$Label = "")
  if (-not $Label) { $Label = "$Exe $($Arguments -join ' ')" }
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"   # a stderr line from a native child must not terminate the run
  $code = $null
  try {
    $global:LASTEXITCODE = $null
    if ($script:log) {
      & $Exe @Arguments *>&1 | Out-File -FilePath $script:log -Append -Encoding utf8
    } else {
      & $Exe @Arguments *>&1 | ForEach-Object { try { Write-Host "  $Label | $_" } catch {} }
    }
    $code = $LASTEXITCODE
  } catch {
    $code = $null
    $ErrorActionPreference = $prev
    Write-Loud "$Label threw before reporting an exit code: $($_.Exception.Message)"
  } finally {
    $ErrorActionPreference = $prev
  }
  return $code
}
