param([Parameter(Mandatory=$true)][string]$Wrapper,
      [Parameter(Mandatory=$true)][string]$CaseName)
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Wrapper, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Wrapper parse failed' }
$branches = @($ast.FindAll({param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Save-PrivateRunEvidence'
}, $true))
if ($branches.Count -ne 1) { throw 'Expected exactly one archive publication branch' }
$script:Returns = @{}
$script:ThrowLabel = ''
$script:Calls = [System.Collections.Generic.List[string]]::new()
$script:Logs = [System.Collections.Generic.List[string]]::new()
$script:runDir = $null
$script:archivePathspec = 'synthetic-archive'
$script:publicationTarget = [pscustomobject]@{local_branch='daily-work'; remote='backup'; branch='daily/archive'; refspec='HEAD:refs/heads/daily/archive'}
$script:Arguments = @{}
$ConfigDir = 'C:\synthetic-companion'
$pipelineState = 'healthy'
$rc = 0
$stamp = '2000-01-01'
$log = 'synthetic-log'
if ($CaseName -eq 'failed-run') { $rc = 9 }
elseif ($CaseName -eq 'config-missing') { $ConfigDir = '' }
elseif ($CaseName -eq 'healthy-noop') { $script:Returns['git diff --cached'] = 0 }
elseif ($CaseName -eq 'unverified-noop') {
    $script:Returns['git diff --cached'] = 0
    $pipelineState = 'unverified'
} elseif ($CaseName -match '^(add|diff|staged|commit|pull|push)-(failure|null|negative|throw)$') {
    $label = @{add='git add';diff='git diff --cached';staged='git staged';commit='git commit';pull='git pull --rebase';push='git push'}[$Matches[1]]
    switch ($Matches[2]) {
        'failure' { $script:Returns[$label] = 7 }
        'null' { $script:Returns[$label] = $null }
        'negative' { $script:Returns[$label] = -1 }
        'throw' { $script:ThrowLabel = $label }
    }
} elseif ($CaseName -notin @('success', 'not-repo', 'no-git')) { throw 'Unknown synthetic case' }
function Test-Path { param($LiteralPath) return $CaseName -ne 'not-repo' }
function Resolve-Git { if ($CaseName -ne 'no-git') { return 'synthetic-git' } }
function Write-Log { param([string]$Message) $script:Logs.Add($Message) }
function Write-Loud { param([string]$Message) $script:Logs.Add($Message) }
function Notify-Abort { param([string]$Message) $script:Logs.Add($Message) }
function Push-Location { param($LiteralPath) }
function Pop-Location {}
function Invoke-Child {
    param($Exe, $Arguments, [string]$Label)
    if ($Exe -ne 'synthetic-git') { throw 'Unexpected executable requested' }
    $script:Calls.Add($Label)
    $script:Arguments[$Label] = @($Arguments)
    if ($script:ThrowLabel -eq $Label) { throw 'Synthetic publication exception' }
    if ($script:Returns.ContainsKey($Label)) { return $script:Returns[$Label] }
    if ($Label -eq 'git diff --cached') { return 1 }
    return 0
}
# Dot-sourcing preserves the production branch's rc assignment in this scope.
. ([scriptblock]::Create($branches[0].Extent.Text))
$rc = Save-PrivateRunEvidence -RunExitCode $rc
[pscustomobject]@{case=$CaseName; rc=$rc; calls=@($script:Calls); arguments=$script:Arguments; logs=@($script:Logs)} | ConvertTo-Json -Compress -Depth 4
exit $rc
