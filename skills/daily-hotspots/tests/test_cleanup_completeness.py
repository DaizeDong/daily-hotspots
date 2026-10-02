"""Whole-subtree cleanup proofs and the actual completeness-only wrapper branch."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import runstore

from test_private_storage_paths import fixtures


def _cache(tmp_path, monkeypatch):
    sample = fixtures.legacy_cleanup_scenario()
    root = tmp_path/'legacy-cache'
    runs = [root/name for name in sample['old_runs']]
    for directory in runs:
        directory.mkdir(parents=True)
        (directory/sample['file']).write_text(sample['content'], encoding='utf-8')
    deletions = []
    monkeypatch.setattr(runstore.shutil, 'rmtree', lambda path, **kwargs: deletions.append(Path(path)))
    return sample, root, runs, deletions


@pytest.mark.parametrize('marker', fixtures.legacy_cleanup_scenario()['marker_kinds'])
@pytest.mark.parametrize('depth', fixtures.legacy_cleanup_scenario()['depths'])
@pytest.mark.parametrize('dry_run', [False, True])
def test_nested_git_marker_blocks_every_planned_deletion(tmp_path, monkeypatch, marker, depth, dry_run):
    sample, root, runs, deletions = _cache(tmp_path, monkeypatch)
    nested = runs[1].joinpath(*sample['nested_parts'][:depth])
    nested.mkdir(parents=True)
    if marker == 'directory':
        (nested/'.git').mkdir()
    else:
        (nested/'.git').write_text(sample['pointer'], encoding='utf-8')
    with pytest.raises(runstore.RunStoreError, match='versioned|worktree|Git'):
        runstore.prune(root, retention_days=0, dry_run=dry_run)
    assert deletions == []
    assert all((directory/sample['file']).read_text(encoding='utf-8') == sample['content'] for directory in runs)


@pytest.mark.parametrize('dry_run', [False, True])
def test_failed_subtree_enumeration_blocks_every_planned_deletion(tmp_path, monkeypatch, dry_run):
    sample, root, runs, deletions = _cache(tmp_path, monkeypatch)
    nested = runs[1].joinpath(*sample['nested_parts'])
    nested.mkdir(parents=True)
    actual_scandir = os.scandir

    def deny(path):
        if Path(path) == nested:
            raise PermissionError('synthetic unreadable subtree')
        return actual_scandir(path)

    monkeypatch.setattr(os, 'scandir', deny)
    with pytest.raises(runstore.RunStoreError, match='inspect|enumerat'):
        runstore.prune(root, retention_days=0, dry_run=dry_run)
    assert deletions == []


@pytest.mark.parametrize('dry_run', [False, True])
def test_plain_unversioned_cache_remains_eligible_without_real_deletion(tmp_path, monkeypatch, dry_run):
    sample, root, runs, deletions = _cache(tmp_path, monkeypatch)
    nested = runs[1].joinpath(*sample['nested_parts'])
    nested.mkdir(parents=True)
    (nested/sample['file']).write_text(sample['content'], encoding='utf-8')
    result = runstore.prune(root, retention_days=0, dry_run=dry_run)
    assert result['removed'] == sample['old_runs']
    assert deletions == ([] if dry_run else runs)
    assert all(directory.is_dir() for directory in runs)


@pytest.mark.parametrize('layout', fixtures.legacy_cleanup_scenario()['layouts'])
def test_completeness_only_branch_uses_proved_archive_before_scanner(tmp_path, layout):
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if shell is None:
        pytest.skip('PowerShell is unavailable on this platform')
    wrapper = Path(runstore.__file__).with_name('wrapper.ps1')
    config = tmp_path/'synthetic companion'
    config.mkdir()
    archive = config/('archive' if layout == 'reject' else layout)
    archive.mkdir(parents=True)
    config_file = tmp_path/'scenario.json'
    config_file.write_text(json.dumps({'wrapper': str(wrapper), 'config': str(config),
                                      'archive': str(archive), 'reject': layout == 'reject'}), encoding='utf-8')
    harness = tmp_path/'completeness-control.ps1'
    harness.write_text('''param([string]$Scenario)
$ErrorActionPreference='Stop'
$sample=Get-Content -Raw -LiteralPath $Scenario | ConvertFrom-Json
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($sample.wrapper,[ref]$tokens,[ref]$errors)
$branch=$ast.Find({param($n) $n -is [System.Management.Automation.Language.IfStatementAst] -and $n.Clauses[0].Item1.Extent.Text -eq '$CompletenessOnly'},$true)
if (-not $branch) { throw 'completeness-only branch not found' }
$observed=@{scanned=@(); resolved=$false}
$CompletenessOnly=$true; $ConfigDir=$sample.config; $log=$null
$env:DAILY_HOTSPOTS_CONFIG='synthetic-stale-selector'
function Resolve-RunWorkspace {
  param($Python,$RunStore,$RunId,[switch]$ArchiveOnly)
  $observed.resolved=$true
  if ($env:DAILY_HOTSPOTS_CONFIG -ne $sample.config) { throw 'ConfigDir did not reach archive resolver' }
  if ($sample.reject) { throw 'synthetic archive proof refused' }
  return $sample.archive
}
function Invoke-Child {
  param($Exe,$Arguments,$Label)
  $observed.scanned=@($Arguments)
  return 0
}
function Write-Log { param($Message) }
function Write-Loud { param($Message) }
function Notify-Abort { param($Message) throw 'unexpected notification in synthetic control' }
$part=Join-Path (Split-Path -Parent $Scenario) 'exact-completeness-branch.ps1'
$prefix='$PSScriptRoot=' + "'" + (Split-Path -Parent $sample.wrapper).Replace("'","''") + "'" + [Environment]::NewLine
$prefix += '$script:py="SyntheticPython"; $script:RC_CANNOT_CHECK=2' + [Environment]::NewLine
Set-Content -LiteralPath $part -Value ($prefix+$branch.Extent.Text) -Encoding UTF8
$blocked=$false; $failure=$null
try { & $part } catch { $blocked=$true; $failure=$_ }
if (-not $observed.resolved) { throw "archive proof was bypassed: $failure" }
if ($sample.reject) {
  if (-not $blocked -or $observed.scanned.Count -ne 0) { throw 'scanner ran despite failed archive proof' }
} else {
  if ($blocked) { throw "valid PRIVATE archive was rejected: $failure" }
  $index=[Array]::IndexOf($observed.scanned,'--archive-dir')
  if ($index -lt 0 -or $observed.scanned[$index+1] -ne $sample.archive) { throw 'scanner received wrong archive layout' }
}
Write-Output 'completeness control validated'
''', encoding='utf-8')
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File', str(harness), str(config_file)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'completeness control validated' in result.stdout
