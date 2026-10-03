"""Use real local Git and receipts; SSH policy outcomes use the supported proof seam."""
import importlib.util
import os
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
import subprocess

import pytest
import private_storage


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('private_path_fixtures', ROOT/'tools/make_fixtures.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
_REAL_PROVE = private_storage._prove_repository
_REAL_REPOSITORY_ROOT = getattr(private_storage, '_repository_root', None)
_REAL_BOUNDARY_LOADER = private_storage._shared_boundary


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    sample = fixtures.private_storage_path_scenario()
    home = tmp_path / 'home'
    (home / '.ssh').mkdir(parents=True)
    for key in list(os.environ):
        if key.upper().startswith('GIT_'):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setenv('PROGRAMDATA', str(home / 'programdata'))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    companion = tmp_path/'synthetic companion'
    subprocess.run(['git', 'init', '-q', str(companion)], check=True)
    subprocess.run(['git', '-C', str(companion), 'remote', 'add', 'origin', sample['origin']], check=True)
    fixtures.synthetic_repository_history(companion)
    data = companion/'data'
    data.mkdir()
    state = {'sample': sample, 'repo': companion, 'data': data,
             'proofs': {sample['slug']: 'true', sample['nested_slug']: 'false'},
             'commands': [], 'git_cwds': [], 'ssh_host': sample['ssh_hosts'][0]}
    boundary = _REAL_BOUNDARY_LOADER()
    receipt = home / 'visibility.json'

    def prove(repository):
        values = {'true': 'PRIVATE', 'false': 'PUBLIC'}
        receipt.write_text(json.dumps({'_refreshed': datetime.now(timezone.utc).isoformat(),
                                      **{name: values.get(value, 'UNKNOWN') for name, value in state['proofs'].items()}}),
                           encoding='utf-8')
        (home / '.ssh/config').write_text('Host synthetic-github\n  ' + state['ssh_host'] + '\n  User git\n', encoding='utf-8')
        return boundary.prove_private_companion(repository, receipt)

    real_run = subprocess.run
    def local_only(argv, *args, **kwargs):
        assert argv[0] == 'git', 'external process attempted in offline test'
        if '-C' in argv:
            directory = Path(argv[argv.index('-C') + 1])
        elif argv[1] == 'init':
            directory = Path(argv[-1])
        else:
            directory = Path(kwargs['cwd'])
        assert directory.resolve().is_relative_to(tmp_path), 'non-synthetic Git metadata access'
        state['commands'].append(argv)
        state['git_cwds'].append(directory)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', local_only)
    monkeypatch.setattr(private_storage, '_shared_boundary', lambda: SimpleNamespace(
        prove_private_companion=prove, read_private_companion_git=boundary.read_private_companion_git,
        GitError=boundary.GitError))
    monkeypatch.setattr(private_storage, '_prove_repository', _REAL_PROVE)
    monkeypatch.setattr(private_storage, '_repository_root', _REAL_REPOSITORY_ROOT)
    return state


def _long_descendant(parent, sample):
    path = parent
    while len(str(path)) < sample['long_path_minimum']:
        path /= sample['long_component']
    return path


def _nested(repositories):
    nested = repositories['data']/'nested'
    subprocess.run(['git', 'init', '-q', str(nested)], check=True)
    subprocess.run(['git', '-C', str(nested), 'remote', 'add', 'origin',
                    repositories['sample']['nested_origin']], check=True)
    return nested


def test_long_path_verifies_before_and_after_directory_creation(repositories):
    data, sample = repositories['data'], repositories['sample']
    descendant = _long_descendant(data, sample)
    requested = descendant/'receipt.json'
    assert private_storage.prove(requested) == requested
    assert not descendant.exists()
    descendant.mkdir(parents=True)
    repositories['commands'].clear()
    repositories['git_cwds'].clear()
    assert private_storage.prove(requested) == requested
    assert not requested.exists()
    assert repositories['git_cwds']
    assert all(path == repositories['repo'] for path in repositories['git_cwds'])


@pytest.mark.parametrize('visibility', fixtures.private_storage_path_scenario()['visibility_values'])
def test_nested_repository_needs_its_own_private_proof(repositories, visibility):
    nested = _nested(repositories)
    repositories['proofs'][repositories['sample']['nested_slug']] = visibility
    requested = nested/'uncreated'/'receipt.json'
    with pytest.raises(RuntimeError, match='PUBLIC|unknown'):
        private_storage.prove(requested)
    assert not requested.parent.exists()


def test_nested_invalid_git_marker_cannot_fall_back_to_outer_private_repo(repositories):
    nested = repositories['data']/'invalid'
    nested.mkdir()
    (nested/'.git').write_text(repositories['sample']['invalid_git_marker'], encoding='utf-8')
    with pytest.raises(RuntimeError):
        private_storage.prove(nested/'receipt.json')
    assert not any(call[0] == 'gh' for call in repositories['commands'])
    assert not (nested/'receipt.json').exists()


def test_nested_bare_repository_is_not_runtime_storage(repositories):
    bare = repositories['data']/'bare'
    subprocess.run(['git', 'init', '--bare', '-q', str(bare)], check=True)
    with pytest.raises(RuntimeError):
        private_storage.prove(bare/'receipt.json')
    assert not (bare/'receipt.json').exists()


def test_long_linked_worktree_uses_its_git_file_marker(repositories, tmp_path):
    repo, sample = repositories['repo'], repositories['sample']
    tree = subprocess.run(['git', '-C', str(repo), 'write-tree'], check=True,
                          capture_output=True, text=True, encoding='utf-8').stdout.strip()
    commit = subprocess.run(['git', '-C', str(repo), '-c', 'user.name='+sample['git_name'],
                             '-c', 'user.email='+sample['git_email'], 'commit-tree', tree,
                             '-m', sample['git_message']], check=True, capture_output=True,
                            text=True, encoding='utf-8').stdout.strip()
    linked = tmp_path/'linked companion'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(linked), commit],
                   check=True, capture_output=True, text=True, encoding='utf-8')
    assert (linked/'.git').is_file()
    descendant = _long_descendant(linked, sample)
    descendant.mkdir(parents=True)
    assert private_storage.prove(descendant/'receipt.json') == descendant/'receipt.json'


@pytest.mark.parametrize('private', [True, False])
def test_junction_uses_actual_destination_repository(repositories, tmp_path, private):
    if private:
        destination = repositories['data']/'inside'
        destination.mkdir()
    else:
        destination = tmp_path/'outside public'
        subprocess.run(['git', 'init', '-q', str(destination)], check=True)
        subprocess.run(['git', '-C', str(destination), 'remote', 'add', 'origin',
                        repositories['sample']['nested_origin']], check=True)
        repositories['proofs'][repositories['sample']['nested_slug']] = 'false'
    link = repositories['data']/'junction'
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(destination), str(link))
    else:
        link.symlink_to(destination, target_is_directory=True)
    try:
        if private:
            assert private_storage.prove(link/'receipt.json') == destination/'receipt.json'
        else:
            with pytest.raises(RuntimeError, match='PUBLIC|unknown'):
                private_storage.prove(link/'receipt.json')
        assert not (destination/'receipt.json').exists()
    finally:
        link.rmdir() if os.name == 'nt' else link.unlink()


@pytest.mark.parametrize('valid_alias', [True, False])
def test_ssh_alias_proof_outcome_is_enforced_at_supported_api(repositories, monkeypatch, valid_alias):
    """The shared kit owns native SSH policy; this consumer enforces its public result."""
    sample = repositories['sample']
    subprocess.run(['git', '-C', str(repositories['repo']), 'remote', 'set-url', 'origin',
                    sample['ssh_origin']], check=True)
    boundary = private_storage._shared_boundary()
    calls = []

    def prove(repository):
        calls.append(repository)
        if not valid_alias:
            raise boundary.GitError('synthetic SSH alias proof refused')
        return SimpleNamespace(root=str(repository), repositories=(sample['slug'],),
                               signature='synthetic-alias-proof')

    monkeypatch.setattr(private_storage, '_shared_boundary', lambda: SimpleNamespace(
        prove_private_companion=prove, GitError=boundary.GitError,
        read_private_companion_git=lambda snapshot, *arguments: SimpleNamespace(
            returncode=1 if arguments[0] == 'check-ignore' else 0, stdout='synthetic-head')))
    requested = repositories['data']/'receipt.json'
    if valid_alias:
        assert private_storage.prove(requested) == requested
    else:
        with pytest.raises(RuntimeError, match='PUBLIC|unknown'):
            private_storage.prove(requested)
    assert not requested.exists()
    assert calls == [repositories['repo']] * (2 if valid_alias else 1)


def test_environment_rewrite_cannot_relabel_physical_public_repository(repositories, monkeypatch):
    sample = repositories['sample']
    subprocess.run(['git', '-C', str(repositories['repo']), 'remote', 'set-url', 'origin',
                    sample['nested_origin']], check=True)
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'url.' + sample['origin'] + '.insteadOf')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', sample['nested_origin'])
    requested = repositories['data']/'runtime.json'
    with pytest.raises(RuntimeError):
        private_storage.prove(requested)
    assert not requested.exists()
