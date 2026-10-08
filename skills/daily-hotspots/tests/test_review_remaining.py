"""Behavioral regressions for the remaining independent-review findings."""
import copy
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import collect
import completeness
import digest
import lib
import private_storage
import run
import source_rotation
import sourcehealth
from test_delivery_commit import Ledger
from test_private_storage_paths import repositories
from test_rotation_commit import fixtures
from test_verify_config import vc


@pytest.mark.parametrize('flag,value', fixtures.review_repair_scenario()['failure_flags'])
def test_appstore_failure_envelope_overrules_populated_feed(flag, value):
    raw = fixtures.source11_appstore_page(1, '123456')
    raw[flag] = value
    result = collect.parse_appstore_rss(raw)
    assert result['errors']
    assert result['signals'] == [] and result['pulled'] == 0


def test_healthy_appstore_feed_preserves_reviews_and_counts():
    result = collect.parse_appstore_rss(fixtures.source11_appstore_page(1, '123456'))
    assert result['errors'] == []
    assert len(result['signals']) == 1 and result['pulled'] == 2


@pytest.mark.parametrize('flags', [{'ok': False}, {'success': False}, {'status': 'error'}, {'isError': True}])
def test_source_health_failure_metadata_agrees_with_collection(flags):
    page = fixtures.review_source_envelope(**flags)
    health = sourcehealth.probe_source('appstore-rss', lambda _: page,
                                       sourcehealth.CONTROLS['appstore_rss'])
    assert health['state'] == sourcehealth.DOWN
    parsed = collect.parse_appstore_rss(page)
    assert parsed['errors'] and not parsed['signals'] and parsed['pulled'] == 0


def test_source_health_accepts_populated_success_envelope():
    page = fixtures.review_source_envelope(ok=True, success=True, status=200)
    health = sourcehealth.probe_source('appstore-rss', lambda _: page,
                                       sourcehealth.CONTROLS['appstore_rss'])
    assert health['state'] == sourcehealth.OK
    assert collect.parse_appstore_rss(page)['kept'] == 1


@pytest.mark.parametrize('rating', [-1, 0, 6, False, 1.5, 'invalid'])
def test_appstore_invalid_rating_never_becomes_demand_evidence(rating):
    result = collect.parse_appstore_rss(fixtures.review_source_envelope(rating))
    assert result['signals'] == [] and result['pulled'] == 2
    assert result['skipped_reasons']['invalid_rating'] == 1
    assert result['skipped_reasons']['not_a_review'] == 1


@pytest.mark.parametrize('rating', [1, 2, 3, 5])
def test_appstore_valid_rating_preserves_demand_floor(rating):
    result = collect.parse_appstore_rss(fixtures.review_source_envelope(rating))
    assert result['kept'] == int(rating <= 2)
    assert result['skipped_reasons']['rating_above_floor'] == int(rating > 2)
    assert result['skipped_reasons']['invalid_rating'] == 0


@pytest.mark.parametrize('name', fixtures.review_wrapper_write_scenario()['targets'])
@pytest.mark.parametrize('ignored', [False, True])
def test_native_wrapper_writes_require_exact_target_eligibility(tmp_path, synthetic_cli_companion, name, ignored):
    result = _native_wrapper_write(tmp_path, synthetic_cli_companion, name, ignored)
    assert result['blocked'] == ignored, result
    assert result['created'] == (not ignored)
    assert result['owned'] == (name.startswith('inflight-') and not ignored)


def test_native_inflight_failed_write_does_not_grant_cleanup_ownership(tmp_path, synthetic_cli_companion):
    name = fixtures.review_wrapper_write_scenario()['targets'][0]
    result = _native_wrapper_write(tmp_path, synthetic_cli_companion, name, False, directory=True)
    assert result['blocked'] and not result['owned'] and not result['created']


def _native_wrapper_write(tmp_path, cli, name, ignored, directory=False):
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if shell is None:
        pytest.skip('PowerShell is unavailable on this platform')
    target = tmp_path / fixtures.review_wrapper_write_scenario()['workspace'] / name
    target.parent.mkdir(parents=True)
    if directory:
        target.mkdir()
    (tmp_path / '.gitignore').write_text(name + '\n' if ignored else '', encoding='utf-8')
    storage = Path(private_storage.__file__)
    probe = subprocess.run(cli(storage) + ['log-path', '--log-dir', str(target.parent), '--name', name],
                           capture_output=True, text=True)
    assert (probe.returncode != 0) == (ignored or directory), probe.stdout + probe.stderr
    scenario = tmp_path / 'wrapper-write.json'
    scenario.write_text(json.dumps({'wrapper': str(storage.with_name('wrapper.ps1')), 'python': sys.executable,
        'target': str(target), 'name': name, 'text': fixtures.review_wrapper_write_scenario()['text']}), encoding='utf-8')
    harness = tmp_path / 'wrapper-write.ps1'
    harness.write_text(r'''param([string]$Scenario)
$ErrorActionPreference='Stop'
$sample=Get-Content -Raw -LiteralPath $Scenario | ConvertFrom-Json
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($sample.wrapper,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'wrapper syntax failure' }
$script:wrapperSourceRoot=Split-Path -Parent $sample.wrapper
$script:py=$sample.python; $script:log=$null; $script:inflightOwned=$false
function Write-Loud { param($Message) }
foreach ($functionName in @('Write-PrivateText','Write-Inflight','Clear-Inflight')) {
  $fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $functionName},$true)
  # Extracted scriptblocks have no filename; bind only the automatic source-root variable.
  if ($fn) { Invoke-Expression $fn.Extent.Text.Replace('$PSScriptRoot','$script:wrapperSourceRoot') }
}
$blocked=$false; $failure=$null
try {
  if ($sample.name.StartsWith('inflight-')) {
    Write-Inflight -Path $sample.target -BudgetSec 60
  } else {
    $promptFile=$sample.target; $pyFile=$sample.target
    $prompt=$sample.text; $pyCode=$sample.text
    $utf8NoBom=New-Object System.Text.UTF8Encoding $false
    $variable=if ($sample.name -eq 'prompt.txt') {'$promptFile'} else {'$pyFile'}
    $write=$ast.Find({param($n) $n -is [System.Management.Automation.Language.CommandAst] -and $n.GetCommandName() -eq 'Write-PrivateText' -and $n.Extent.Text.Contains($variable)},$true)
    if (-not $write) {
      $write=$ast.Find({param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $n.Extent.Text.StartsWith('[System.IO.File]::WriteAllText') -and $n.Extent.Text.Contains($variable)},$true)
    }
    if (-not $write) { throw 'production write statement unavailable' }
    Invoke-Expression $write.Extent.Text | Out-Null
  }
} catch { $blocked=$true; $failure=$_.Exception.Message }
@{blocked=$blocked; created=[System.IO.File]::Exists($sample.target); owned=$script:inflightOwned; failure=$failure} | ConvertTo-Json -Compress
''', encoding='utf-8')
    execution = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File', str(harness), str(scenario)],
                               capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert execution.returncode == 0, execution.stdout + execution.stderr
    return json.loads(execution.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize('case', ['missing-cli', 'failed', 'unknown', 'nonzero'])
def test_required_mcp_is_not_ready_without_successful_connection(case):
    scenario = fixtures.review_repair_scenario()
    def probe():
        if case == 'missing-cli':
            raise FileNotFoundError('Synthetic absent CLI')
        if case == 'nonzero':
            return subprocess.CompletedProcess(['synthetic-mcp-list'], 1, scenario['mcp_connected'])
        return scenario['mcp_' + case]
    assert all(not ok for _, ok, _ in vc.check_required_mcps(runner=probe))


def test_required_mcp_checks_native_process_status(monkeypatch):
    scenario = fixtures.review_repair_scenario()
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 1, scenario['mcp_connected'], 'Synthetic failure'))
    assert all(not ok for _, ok, _ in vc.check_required_mcps())


def test_required_mcp_accepts_connected_command_output():
    listing = fixtures.review_repair_scenario()['mcp_command_connected']
    result = subprocess.CompletedProcess(['synthetic-mcp-list'], 0, listing)
    assert all(ok for _, ok, _ in vc.check_required_mcps(runner=lambda: result))


def test_private_route_without_committed_history_is_not_runtime_storage(repositories):
    root = repositories['repo']
    reference = (root / '.git/HEAD').read_text().strip().removeprefix('ref: ')
    (root / '.git' / reference).unlink(missing_ok=True)
    with pytest.raises(RuntimeError, match='committed|history|HEAD'):
        private_storage.prove(repositories['data'] / 'report.json')


def test_private_ignored_output_is_refused_before_creation(repositories):
    fixtures.synthetic_repository_history(repositories['repo'])
    (repositories['repo'] / '.gitignore').write_text(fixtures.review_repair_scenario()['ignored_rule'])
    target = repositories['data'] / 'blocked.json'
    with pytest.raises(RuntimeError, match='ignored|eligibility'):
        private_storage.prove(target)
    assert not target.exists()


def test_standalone_weekly_writer_proves_destination_before_creation(tmp_path, monkeypatch):
    weekly = importlib.import_module('yield')
    def refuse(path, **kwargs):
        raise RuntimeError('Synthetic PRIVATE proof refusal')
    monkeypatch.setattr(private_storage, 'prove', refuse)
    target = tmp_path / 'unproved-archive'
    with pytest.raises(RuntimeError, match='PRIVATE'):
        weekly.write_review(fixtures.review_repair_scenario()['report'], str(target))
    assert not target.exists()


@pytest.mark.parametrize('writer', ['completeness', 'weekly'])
def test_report_writers_never_overwrite_a_predictable_alias(tmp_path, writer):
    case = fixtures.review_repair_scenario()
    peer = tmp_path / 'synthetic-peer.txt'
    peer.write_text(case['prior'], encoding='utf-8')
    if writer == 'completeness':
        target = tmp_path / 'completeness.json'
        alias = tmp_path / ('.' + target.name + '.' + str(os.getpid()) + '.tmp')
        invoke = lambda: completeness.write_report(target, case['document'])
    else:
        alias = tmp_path / 'roster-review.md'
        invoke = lambda: importlib.import_module('yield').write_review(case['report'], str(tmp_path))
    os.link(peer, alias)
    try:
        invoke()
    except RuntimeError:
        pass
    assert peer.read_text(encoding='utf-8') == case['prior']


def test_digest_rejects_a_changed_temporary_destination(archive_path, monkeypatch):
    case = fixtures.review_repair_scenario()
    peer = archive_path / 'synthetic-peer.txt'
    peer.write_text(case['prior'], encoding='utf-8')
    original = source_rotation.prove
    temporary_checks = 0
    def redirect(path):
        nonlocal temporary_checks
        if Path(path).suffix == '.partial':
            temporary_checks += 1
            if temporary_checks > 1:
                return peer
        return original(path)
    monkeypatch.setattr(source_rotation, 'prove', redirect)
    with pytest.raises(RuntimeError, match='destination'):
        digest.write_digest_file(case['report'], str(archive_path), date='2026-06-25')
    assert peer.read_text(encoding='utf-8') == case['prior']


def test_atomic_writer_rejects_a_changed_final_destination(tmp_path, monkeypatch):
    case = fixtures.review_repair_scenario()
    target, peer = tmp_path / 'report.md', tmp_path / 'synthetic-peer.txt'
    peer.write_text(case['prior'], encoding='utf-8')
    original, checks = private_storage.prove, []
    def redirect(path):
        if Path(path) == target:
            checks.append(path)
            if len(checks) > 1:
                return peer
        return original(path)
    monkeypatch.setattr(source_rotation, 'prove', redirect)
    with pytest.raises(RuntimeError, match='destination'):
        source_rotation.atomic_bytes(target, case['report'].encode())
    assert not target.exists()
    assert peer.read_text(encoding='utf-8') == case['prior']


def test_direct_cli_interrupted_delivery_cannot_be_replayed(archive_path, monkeypatch):
    sample, ledger = fixtures.delivery_scenario(), Ledger()
    source = archive_path / 'candidates.json'
    source.write_text(json.dumps(sample['candidates']), encoding='utf-8')
    ledger.init = lambda: None
    ledger.expire_pending = lambda: {'expired': []}
    monkeypatch.setattr(run.dd, 'LedgerClient', lambda: ledger)
    monkeypatch.setattr(run, 'load_config', lambda: copy.deepcopy(lib.DEFAULT_CONFIG))
    calls = []
    def deliver(message, dry_run=False):
        calls.append(message)
        if len(calls) == 1:
            raise KeyboardInterrupt('Synthetic interruption after transmission')
        return sample['success']
    monkeypatch.setattr(run.pc, 'deliver', deliver)
    monkeypatch.setattr(sys, 'argv', ['run.py', '--in', str(source), '--run-id', sample['run_id'],
                                    '--archive-dir', str(archive_path)])
    with pytest.raises(KeyboardInterrupt):
        run.main()
    assert not ledger.rows and not ledger.watermarks
    assert run.main() != 0
    assert len(calls) == 1 and not ledger.watermarks


@pytest.mark.parametrize('dry', [False, True])
def test_direct_cli_claim_survives_success_while_preview_reserves_nothing(archive_path, monkeypatch, dry):
    sample = fixtures.delivery_scenario()
    source = archive_path / 'candidates.json'
    source.write_text(json.dumps(sample['candidates']), encoding='utf-8')
    monkeypatch.setattr(run, 'load_config', lambda: copy.deepcopy(lib.DEFAULT_CONFIG))
    calls = []
    def deliver(message, dry_run=False):
        calls.append(dry_run)
        return sample['success']
    monkeypatch.setattr(run.pc, 'deliver', deliver)
    arguments = ['run.py', '--in', str(source), '--no-ledger', '--run-id', sample['run_id'],
                 '--archive-dir', str(archive_path)]
    if dry:
        arguments.append('--dry-run')
    monkeypatch.setattr(sys, 'argv', arguments)
    assert run.main() == 0
    assert run.main() == (0 if dry else 1)
    if dry:
        assert calls == [True, True] and list(archive_path.iterdir()) == [source]
    else:
        assert calls == [False]
        records = list((archive_path / 'delivery-claims').glob('finalization-*.json'))
        assert len(records) == 1 and json.loads(records[0].read_text())['state'] == 'completed'
