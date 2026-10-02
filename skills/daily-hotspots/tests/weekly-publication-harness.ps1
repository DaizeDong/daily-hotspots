param([string]$SourceRoot, [string]$Scenario, [string]$Scratch)
$ErrorActionPreference = 'Stop'
. (Join-Path $SourceRoot 'wrapper-common.ps1')
$script:ProofCalls = 0
$script:WorkerCalls = 0
$script:WorkerArguments = @()
$script:Calls = [System.Collections.Generic.List[string]]::new()
$script:Logs = [System.Collections.Generic.List[string]]::new()
$script:Arguments = @{}
$script:ObservedExit = 1
$ConfigDir = Join-Path $Scratch 'private'
$ReportOnly = $Scenario -eq 'report-only'
$Python = 'Invoke-SyntheticProof'
$LogDir = ''
$script:STREAM = 'synthetic-stream'
function Initialize-WrapperLog {
    param($LogDir, $Name, $Python)
    $script:log = Join-Path $Scratch 'synthetic.log'
    return $script:log
}
function Resolve-Python { param($Selected) return 'Invoke-SyntheticProof' }
function Resolve-Git { return 'synthetic-git' }
function Write-Log { param($Message) $script:Logs.Add([string]$Message) }
function Write-Loud { param($Message) $script:Logs.Add([string]$Message) }
function Notify-Abort { param($Message) $script:Logs.Add([string]$Message) }
function Invoke-SyntheticProof {
    $script:ProofCalls++
    $global:LASTEXITCODE = 0
    if ($Scenario -eq 'preflight-refused') {
        $global:LASTEXITCODE = 1
        return 'Synthetic publication proof refused'
    }
    return ([pscustomobject]@{local_branch='daily-work'; remote='backup'; branch='daily/archive';
        refspec='HEAD:refs/heads/daily/archive'; repository_root=$ConfigDir; roster_pathspec='roster.json'} |
        ConvertTo-Json -Compress)
}
function Invoke-ChildToLog {
    param($Exe, $Arguments, $Label)
    if ($Exe -ne 'Invoke-SyntheticProof') { throw 'Unexpected worker executable' }
    $script:WorkerCalls++
    $script:WorkerArguments = @($Arguments)
    if ($Scenario -eq 'worker-failed') { return 9 }
    return 0
}
function Invoke-Child {
    param($Exe, $Arguments, $Label)
    if ($Exe -ne 'synthetic-git') { throw 'Unexpected publication executable' }
    $script:Calls.Add([string]$Label)
    $script:Arguments[$Label] = @($Arguments)
    if ($Label -eq 'roster git diff') {
        if ($Scenario -eq 'no-changes') { return 0 }
        return 1
    }
    if ($Scenario -eq 'commit-failed' -and $Label -eq 'roster git commit') { return 7 }
    return 0
}
$tokens = $null; $parseErrors = $null
$wrapper = Join-Path $SourceRoot 'yield-wrapper.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($wrapper, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Weekly wrapper parse failed' }
$blocks = @($ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.TryStatementAst] })
if ($blocks.Count -ne 1) { throw 'Expected one weekly wrapper execution block' }
$body = $blocks[0].Extent.Text.Replace('$PSScriptRoot', '$SourceRoot')
# Capture the production exit value so this fixed harness can emit a receipt before exiting.
if ([regex]::Matches($body, 'exit \$rc').Count -ne 1) { throw 'Expected one weekly exit statement' }
$body = $body.Replace('exit $rc', '$script:ObservedExit = $rc')
try { . ([scriptblock]::Create($body)) }
catch { $script:ObservedExit = 1; $script:Logs.Add($_.Exception.Message) }
[pscustomobject]@{case=$Scenario; rc=$script:ObservedExit; proof_calls=$script:ProofCalls;
    worker_calls=$script:WorkerCalls; worker_arguments=@($script:WorkerArguments);
    calls=@($script:Calls); arguments=$script:Arguments; logs=@($script:Logs)} |
    ConvertTo-Json -Compress -Depth 5
exit $script:ObservedExit
