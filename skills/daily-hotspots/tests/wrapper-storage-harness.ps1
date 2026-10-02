param([string]$SourceRoot, [string]$Scenario, [string]$Scratch)
$ErrorActionPreference = 'Stop'
$script:ProofCalls = 0
$script:log = $null
$privateLog = Join-Path $Scratch 'private/archive/logs/synthetic.log'
if ($Scenario.StartsWith('log-')) {
    . (Join-Path $SourceRoot 'wrapper-common.ps1')
    function Resolve-Python { return 'Invoke-SyntheticProof' }
    function Invoke-SyntheticProof {
        $script:ProofCalls++
        if (Test-Path -LiteralPath $privateLog) { throw 'Log opened before proof' }
        $global:LASTEXITCODE = 0
        if ($Scenario -eq 'log-refused') { $global:LASTEXITCODE = 1; return 'Synthetic PRIVATE proof failure' }
        if ($Scenario -eq 'log-empty') { return }
        return $privateLog
    }
    try {
        $selected = if ($Scenario -eq 'log-default') { '' } else { Join-Path $Scratch 'requested' }
        $result = Initialize-WrapperLog -LogDir $selected -Name 'synthetic.log' -Python 'synthetic-python'
        Write-Log 'Synthetic child output retained'
        [pscustomobject]@{proof_calls=$script:ProofCalls; path=$result} | ConvertTo-Json -Compress
        exit 0
    } catch {
        [Console]::WriteLine('CONSOLE FAILURE: ' + $_.Exception.Message)
        [Console]::WriteLine('PROOF CALLS: ' + $script:ProofCalls)
        exit 1
    }
}
if ($Scenario.StartsWith('publication-')) {
    . (Join-Path $SourceRoot 'wrapper-common.ps1')
    function Invoke-SyntheticProof {
        $script:ProofCalls++
        $global:LASTEXITCODE = 0
        if ($Scenario -eq 'publication-refused') { $global:LASTEXITCODE = 1; return 'Synthetic upstream unavailable' }
        if ($Scenario -eq 'publication-malformed') { return '{}' }
        return '{"local_branch":"daily-work","remote":"backup","branch":"daily/archive","refspec":"HEAD:refs/heads/daily/archive"}'
    }
    $script:py = 'Invoke-SyntheticProof'
    $script:WorkspaceCalls = 0
    $script:runId = 'synthetic-run'
    $ConfigDir = Join-Path $Scratch 'private'
    function Resolve-RunWorkspace {
        param($Python, $RunStore, $RunId, [switch]$ArchiveOnly, $RelativeTo)
        if ($RunId) { $script:WorkspaceCalls++ }
        return (Join-Path $Scratch 'private/archive')
    }
    function Write-Log { param($Message) }
    $tokens = $null; $parseErrors = $null
    $wrapper = Join-Path $SourceRoot 'wrapper.ps1'
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($wrapper, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count) { throw 'Wrapper parse failed' }
    $gates = @($ast.FindAll({param($node)
        $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $node.Left.Extent.Text -eq '$script:publicationTarget'
    }, $true))
    if ($gates.Count -ne 1) { throw 'Expected one publication preflight' }
    $statements = @($gates[0].Parent.Statements)
    $start = -1; $finish = -1
    for ($i = 0; $i -lt $statements.Count; $i++) {
        if ($statements[$i].Extent.Text.StartsWith('$script:archiveDir =')) { $start = $i }
        if ($statements[$i].Extent.Text.StartsWith('$script:runDir = Resolve-RunWorkspace')) { $finish = $i }
    }
    if ($start -lt 0 -or $finish -le $start) { throw 'Expected production storage preflight range' }
    $code = 0
    try {
        foreach ($statement in $statements[$start..$finish]) {
            . ([scriptblock]::Create($statement.Extent.Text.Replace('$PSScriptRoot', '$SourceRoot')))
        }
    } catch { $code = 1 }
    [pscustomobject]@{proof_calls=$script:ProofCalls; workspace_calls=$script:WorkspaceCalls; target=$script:publicationTarget} | ConvertTo-Json -Compress
    exit $code
}
$tokens = $null; $parseErrors = $null
$wrapper = Join-Path $SourceRoot 'wrapper.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($wrapper, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Wrapper parse failed' }
$branches = @($ast.FindAll({param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Clauses[0].Item1.Extent.Text -eq '$CompletenessOnly'
}, $true))
if ($branches.Count -ne 1) { throw 'Expected one completeness branch' }
$CompletenessOnly = $true
$ConfigDir = Join-Path $Scratch 'private'
$script:py = 'synthetic-python'
$script:RC_CANNOT_CHECK = 2
$log = Join-Path $Scratch 'separate-log/synthetic.log'
function Resolve-RunWorkspace { return (Join-Path $Scratch 'private/archive') }
function Test-Path { return $true }
function Notify-Abort { param($Message) [Console]::WriteLine($Message) }
function Write-Log { param($Message) [Console]::WriteLine($Message) }
function Write-Loud { param($Message) [Console]::WriteLine($Message) }
function Invoke-Child {
    param($Exe, $Arguments, $Label)
    [Console]::WriteLine('CALL ' + (ConvertTo-Json -Compress -InputObject @($Arguments)))
    return [int]($Scenario.Replace('completeness-', ''))
}
$branchText = $branches[0].Extent.Text.Replace('$PSScriptRoot', '$SourceRoot')
. ([scriptblock]::Create($branchText))
