"""Real runstore and finalizer boundaries with synthetic repositories and GitHub answers."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import finalize_handoff
import private_storage
import runstore

from test_private_storage_paths import repositories, fixtures


def _ready(directory, sample):
    directory.mkdir(parents=True, exist_ok=True)
    data = json.dumps(sample['candidates']).encode()
    (directory/'candidates.json').write_bytes(data)
    (directory/'candidate-ready.json').write_text(json.dumps({
        'schema_version': 1, 'ready': True, 'run_id': sample['run_id'],
        'nonce': sample['nonce'], 'candidate_sha256': hashlib.sha256(data).hexdigest()}), encoding='utf-8')
    return data


def _finalize(directory, sample):
    return finalize_handoff.main(['--run-dir', str(directory), '--run-id', sample['run_id'],
                                 '--nonce', sample['nonce'], '--dry-run'])


def _driver(monkeypatch):
    original = subprocess.run
    calls = []

    def run(argv, *args, **kwargs):
        if any(Path(str(arg)).name == 'run.py' for arg in argv):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0)
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', run)
    return calls


@pytest.mark.parametrize('visibility', fixtures.run_workspace_scenario()['denied_visibility'])
def test_unproved_run_root_creates_nothing(repositories, monkeypatch, visibility):
    sample = fixtures.run_workspace_scenario()
    root = repositories['repo']/'uncreated'/'workspaces'
    repositories['proofs'][sample['slug']] = visibility
    monkeypatch.setenv('DAILY_HOTSPOTS_RUN_ROOT', str(root))
    with pytest.raises(runstore.RunStoreError, match='PUBLIC|unknown'):
        runstore.run_dir(sample['run_id'])
    assert not root.parent.exists()


@pytest.mark.parametrize('visibility', fixtures.run_workspace_scenario()['denied_visibility'])
def test_finalizer_proves_destination_before_snapshot_or_driver(repositories, monkeypatch, visibility):
    sample = fixtures.run_workspace_scenario()
    root = repositories['data']/'run'
    _ready(root, sample)
    repositories['proofs'][sample['slug']] = visibility
    calls = _driver(monkeypatch)
    assert _finalize(root, sample) == 4
    assert not list(root.glob('finalize-*'))
    assert calls == []


def test_finalizer_refuses_own_consumer_before_snapshot_or_driver(repositories, monkeypatch):
    sample = fixtures.run_workspace_scenario()
    root = repositories['data']/'run'
    _ready(root, sample)
    monkeypatch.setattr(private_storage, 'ROOT', repositories['repo'])
    calls = _driver(monkeypatch)
    assert _finalize(root, sample) == 4
    assert not list(root.glob('finalize-*'))
    assert calls == []


@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('data_layout', [False, True])
def test_private_runstore_to_finalizer_workflow(repositories, monkeypatch, explicit, data_layout):
    sample = fixtures.run_workspace_scenario()
    if not data_layout:
        repositories['data'].rmdir()
    archive = (repositories['data'] if data_layout else repositories['repo'])/'archive'
    archive.mkdir()
    monkeypatch.setenv('DAILY_HOTSPOTS_CONFIG', str(repositories['repo']))
    monkeypatch.delenv('DAILY_HOTSPOTS_RUN_ROOT', raising=False)
    expected = archive/'workspaces'/sample['run_id']
    if explicit:
        monkeypatch.setenv('DAILY_HOTSPOTS_RUN_ROOT', str(archive/'custom-workspaces'))
        expected = archive/'custom-workspaces'/sample['run_id']
    directory = runstore.run_dir(sample['run_id'])
    assert directory == expected
    data = _ready(directory, sample)
    calls = _driver(monkeypatch)
    assert _finalize(directory, sample) == 0
    assert _finalize(directory, sample) == 0
    assert len(calls) == 2
    assert not list(directory.glob('finaliz*'))
    for argv, options in calls:
        assert options['input'] == data
        assert '--dry-run' in argv and '--no-ledger' in argv and '--in' not in argv


def test_archive_cli_matches_workspace_parent(repositories, monkeypatch, capsys):
    monkeypatch.setenv('DAILY_HOTSPOTS_CONFIG', str(repositories['repo']))
    assert runstore._cli(['archive']) == 0
    assert Path(capsys.readouterr().out.strip()) == repositories['data']/'archive'
    assert runstore._cli(['archive', '--relative-to', str(repositories['repo'])]) == 0
    assert capsys.readouterr().out.strip() == 'data/archive'


def test_archive_pathspec_cannot_cross_private_worktrees(repositories, tmp_path, monkeypatch):
    companion = repositories['repo']
    nested = companion/'nested'
    subprocess.run(['git', 'init', '-q', str(nested)], check=True)
    subprocess.run(['git', '-C', str(nested), 'remote', 'add', 'origin',
                    repositories['sample']['nested_origin']], check=True)
    fixtures.synthetic_repository_history(nested)
    repositories['proofs'][repositories['sample']['nested_slug']] = 'true'
    monkeypatch.setenv('DAILY_HOTSPOTS_CONFIG', str(nested))
    with pytest.raises(runstore.RunStoreError, match='same PRIVATE worktree'):
        runstore._cli(['archive', '--relative-to', str(companion)])


def test_uninitialized_workspace_does_not_fall_back_to_temp(repositories, monkeypatch):
    import archive
    sample = fixtures.run_workspace_scenario()
    monkeypatch.delenv('DAILY_HOTSPOTS_RUN_ROOT', raising=False)
    monkeypatch.setattr(archive, 'find_archive_dir', lambda: None)
    with pytest.raises(runstore.RunStoreError, match='initialized|companion'):
        runstore.run_dir(sample['run_id'])


def test_private_linked_worktree_is_a_valid_run_workspace(repositories, tmp_path, monkeypatch):
    sample, repo = fixtures.run_workspace_scenario(), repositories['repo']
    tree = subprocess.run(['git', '-C', str(repo), 'write-tree'], check=True,
                          capture_output=True, text=True).stdout.strip()
    commit = subprocess.run(['git', '-C', str(repo), '-c', 'user.name='+sample['git_name'],
                             '-c', 'user.email='+sample['git_email'], 'commit-tree', tree,
                             '-m', sample['git_message']], check=True, capture_output=True, text=True).stdout.strip()
    linked = tmp_path/'linked workspace'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(linked), commit],
                   check=True, capture_output=True)
    monkeypatch.setenv('DAILY_HOTSPOTS_RUN_ROOT', str(linked/'archive'/'workspaces'))
    directory = runstore.run_dir(sample['run_id'])
    _ready(directory, sample)
    calls = _driver(monkeypatch)
    assert _finalize(directory, sample) == 0
    assert len(calls) == 1


@pytest.mark.parametrize('visibility', ['true', 'false'])
def test_junction_workspace_uses_resolved_private_proof(repositories, tmp_path, monkeypatch, visibility):
    sample = fixtures.run_workspace_scenario()
    destination = repositories['data']/'workspace'
    destination.mkdir()
    link = tmp_path/'workspace link'
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(destination), str(link))
    else:
        link.symlink_to(destination, target_is_directory=True)
    repositories['proofs'][sample['slug']] = visibility
    monkeypatch.setenv('DAILY_HOTSPOTS_RUN_ROOT', str(link))
    try:
        if visibility == 'true':
            assert runstore.run_dir(sample['run_id']) == destination/sample['run_id']
        else:
            with pytest.raises(runstore.RunStoreError):
                runstore.run_dir(sample['run_id'])
            assert not (destination/sample['run_id']).exists()
        _ready(destination, sample)
        calls = _driver(monkeypatch)
        assert _finalize(link, sample) == (0 if visibility == 'true' else 4)
        assert len(calls) == (1 if visibility == 'true' else 0)
        assert not list(destination.glob('finaliz*'))
    finally:
        link.rmdir() if os.name == 'nt' else link.unlink()


def test_prune_refuses_private_workspaces_even_through_junction(repositories, tmp_path):
    root = repositories['data']/'legacy'
    (root/'daily-2020-01-01').mkdir(parents=True)
    link = tmp_path/'legacy link'
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(root), str(link))
    else:
        link.symlink_to(root, target_is_directory=True)
    try:
        with pytest.raises(runstore.RunStoreError, match='git worktree'):
            runstore.prune(link, retention_days=0)
        assert (root/'daily-2020-01-01').is_dir()
    finally:
        link.rmdir() if os.name == 'nt' else link.unlink()


def test_wrapper_workspace_failure_stops_before_agent(tmp_path):
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if shell is None:
        pytest.skip('PowerShell is unavailable on this platform')
    wrapper = Path(runstore.__file__).with_name('wrapper.ps1')
    harness = tmp_path/'wrapper-control.ps1'
    harness.write_text('''param([string]$Wrapper)
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Wrapper,[ref]$tokens,[ref]$errors)
$fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Resolve-RunWorkspace'},$true)
if (-not $fn) { throw 'workspace resolution has no fail-closed boundary' }
Invoke-Expression $fn.Extent.Text
function SyntheticPython { $global:LASTEXITCODE=2; 'synthetic proof unavailable' }
$continued=$false; $blocked=$false
try { Resolve-RunWorkspace -Python SyntheticPython -RunStore synthetic.py -RunId synthetic; $continued=$true } catch { $blocked=$true }
if (-not $blocked -or $continued) { throw 'resolution failure continued into agent work' }
''', encoding='utf-8')
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File', str(harness), str(wrapper)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def _completed_workspace(repositories, monkeypatch, *, state='completed', legacy=False):
    sample = fixtures.workspace_retirement_scenario()
    monkeypatch.setenv('DAILY_HOTSPOTS_CONFIG', str(repositories['repo']))
    archive = repositories['data']/'archive'
    directory = archive/'workspaces'/sample['run_id']
    data = _ready(directory, sample)
    (directory/'result.json').write_text(json.dumps(sample['result']), encoding='utf-8')
    (directory/sample['bulk_name']).write_text(sample['bulk'], encoding='utf-8')
    claim_dir = directory if legacy else archive/'finalizations'
    claim_dir.mkdir(parents=True, exist_ok=True)
    name = 'finalization-' + hashlib.sha256(sample['run_id'].encode()).hexdigest() + '.json'
    claim = claim_dir/name
    record = {'schema_version': 1, 'run_id': sample['run_id'], 'nonce': sample['nonce'],
              'candidate_sha256': hashlib.sha256(data).hexdigest(), 'state': state}
    claim.write_text(json.dumps(record), encoding='utf-8')
    return sample, archive, directory, claim


@pytest.fixture
def retirement_repository(tmp_path):
    """Behavioral cases use the suite's synthetic proof; native positives use real Git."""
    data = tmp_path/'data'
    data.mkdir()
    return {'repo': tmp_path, 'data': data}


def test_candidate_accepted_by_handoff_survives_promotion_above_old_cap(retirement_repository, monkeypatch):
    repositories = retirement_repository
    sample = fixtures.workspace_retirement_scenario()
    sample['candidates'] = sample['large_candidates']
    monkeypatch.setenv('DAILY_HOTSPOTS_CONFIG', str(repositories['repo']))
    archive = repositories['data']/'archive'
    directory = archive/'workspaces'/sample['run_id']
    expected = _ready(directory, sample)
    assert len(expected) > 4 * 1024 * 1024
    assert finalize_handoff.validate_handoff(directory, sample['run_id'], sample['nonce']) == expected
    report = runstore.promote(directory, archive, sample['run_id'])
    assert 'candidates.json' in [row['name'] for row in report['promoted']]
    assert (Path(report['dest'])/'candidates.json').read_bytes() == expected


@pytest.mark.parametrize('legacy', [False, True])
def test_completed_workspace_compacts_without_reopening_logical_run(repositories, monkeypatch, legacy):
    sample, archive, directory, _ = _completed_workspace(repositories, monkeypatch, legacy=legacy)
    data = (directory/'candidates.json').read_bytes()
    report = runstore.compact(directory, archive, sample['run_id'])
    assert report['removed'] is True
    assert not directory.exists()
    kept = runstore.keep_dir(archive, sample['run_id'])
    assert sorted(path.name for path in kept.iterdir()) == ['candidates.json', 'result.json']
    assert (kept/'candidates.json').read_bytes() == data
    _ready(directory, sample)
    calls = _driver(monkeypatch)
    monkeypatch.delenv('DAILY_HOTSPOTS_DRYRUN', raising=False)
    assert finalize_handoff.main(['--run-dir', str(directory), '--run-id', sample['run_id'],
                                  '--nonce', sample['nonce']]) == 4
    assert calls == []


@pytest.mark.parametrize('state', ['uncertain', 'missing'])
def test_workspace_with_unresolved_delivery_is_never_compacted(retirement_repository, monkeypatch, state):
    sample, archive, directory, claim = _completed_workspace(retirement_repository, monkeypatch, state=state)
    if state == 'missing':
        claim.unlink()
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    report = runstore.compact(directory, archive, sample['run_id'])
    assert report['removed'] is False
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


@pytest.mark.parametrize('failure', ['missing_candidates', 'missing_result', 'oversized_result',
                                     'different_archive', 'wrong_candidate_hash', 'invalid_result', 'active_lock'])
def test_incomplete_or_changed_core_prevents_workspace_deletion(retirement_repository, monkeypatch, failure):
    sample, archive, directory, claim = _completed_workspace(retirement_repository, monkeypatch)
    if failure.startswith('missing_'):
        (directory/('candidates.json' if failure == 'missing_candidates' else 'result.json')).unlink()
    elif failure == 'oversized_result':
        (directory/'result.json').write_text('x' * (1024 * 1024 + 1), encoding='utf-8')
    elif failure == 'different_archive':
        kept = runstore.keep_dir(archive, sample['run_id'])
        kept.mkdir(parents=True)
        (kept/'candidates.json').write_text('[{}]', encoding='utf-8')
    elif failure == 'wrong_candidate_hash':
        (directory/'candidates.json').write_text('[{}]', encoding='utf-8')
    elif failure == 'invalid_result':
        (directory/'result.json').write_text(json.dumps({'run_id': sample['run_id']}), encoding='utf-8')
    else:
        (directory/'writer.lock').write_text('Synthetic active writer', encoding='utf-8')
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    report = runstore.compact(directory, archive, sample['run_id'])
    assert report['removed'] is False
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


def test_copy_failure_keeps_workspace_and_claim(retirement_repository, monkeypatch):
    sample, archive, directory, claim = _completed_workspace(retirement_repository, monkeypatch)
    def fail(*args, **kwargs):
        raise OSError('Synthetic copy failure')
    monkeypatch.setattr(runstore.shutil, 'copy2', fail)
    with pytest.raises(OSError, match='Synthetic'):
        runstore.compact(directory, archive, sample['run_id'])
    assert (directory/'candidates.json').exists() and (directory/'result.json').exists()
    assert claim.exists()


def test_changed_promoted_core_keeps_workspace(retirement_repository, monkeypatch):
    sample, archive, directory, claim = _completed_workspace(retirement_repository, monkeypatch)
    original = runstore.shutil.copy2
    def changed_copy(source, target):
        original(source, target)
        Path(target).write_bytes(b'{}')
    monkeypatch.setattr(runstore.shutil, 'copy2', changed_copy)
    report = runstore.compact(directory, archive, sample['run_id'])
    assert report['removed'] is False
    assert (directory/'candidates.json').exists() and (directory/'result.json').exists()
    assert claim.exists()


def test_compaction_preview_keeps_all_bytes_and_creates_no_core(retirement_repository, monkeypatch):
    sample, archive, directory, _ = _completed_workspace(retirement_repository, monkeypatch, legacy=True)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    report = runstore.compact(directory, archive, sample['run_id'], dry_run=True)
    assert report['eligible'] is True and report['removed'] is False
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    assert not (archive/'runs').exists() and not (archive/'finalizations').exists()


def test_nested_repository_prevents_compaction(retirement_repository, monkeypatch):
    sample, archive, directory, claim = _completed_workspace(retirement_repository, monkeypatch)
    (directory/'nested'/'.git').mkdir(parents=True)
    report = runstore.compact(directory, archive, sample['run_id'])
    assert report['removed'] is False
    assert (directory/'candidates.json').exists() and claim.exists()


@pytest.mark.parametrize('kind', ['hardlink', 'directory_link'])
def test_alias_prevents_compaction_without_touching_target(retirement_repository, monkeypatch, tmp_path, kind):
    sample, archive, directory, claim = _completed_workspace(retirement_repository, monkeypatch)
    target = tmp_path/'outside'
    target.mkdir()
    data = target/'synthetic.txt'
    data.write_text(sample['bulk'], encoding='utf-8')
    link = directory/'linked'
    if kind == 'hardlink':
        os.link(data, link)
    elif os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises(RuntimeError, match='hardlink|reparse|symlink'):
            runstore.compact(directory, archive, sample['run_id'])
        assert data.read_text(encoding='utf-8') == sample['bulk']
        assert (directory/'candidates.json').exists() and claim.exists()
    finally:
        link.rmdir() if kind == 'directory_link' and os.name == 'nt' else link.unlink()
