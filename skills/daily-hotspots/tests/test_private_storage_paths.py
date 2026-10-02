"""Use real local Git for path discovery; GitHub and SSH answers remain synthetic."""
import importlib.util
import os
from pathlib import Path
import subprocess

import pytest
import private_storage


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('private_path_fixtures', ROOT/'tools/make_fixtures.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
_REAL_RUN = private_storage._run
_REAL_REPOSITORY_ROOT = getattr(private_storage, '_repository_root', None)


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    sample = fixtures.private_storage_path_scenario()
    companion = tmp_path/'synthetic companion'
    subprocess.run(['git', 'init', '-q', str(companion)], check=True)
    subprocess.run(['git', '-C', str(companion), 'remote', 'add', 'origin', sample['origin']], check=True)
    data = companion/'data'
    data.mkdir()
    state = {'sample': sample, 'repo': companion, 'data': data,
             'proofs': {sample['slug']: 'true'}, 'commands': [], 'ssh_host': sample['ssh_hosts'][0]}

    def metadata(argv):
        state['commands'].append(argv)
        if argv[0] == 'git':
            requested = Path(argv[argv.index('-C')+1]).resolve()
            assert requested.is_relative_to(tmp_path), 'non-synthetic Git metadata access'
            return _REAL_RUN(argv)
        if argv[0] == 'gh':
            identity = next(arg.removeprefix('repos/') for arg in argv if arg.startswith('repos/'))
            return state['proofs'].get(identity, '')
        if argv[0] == 'ssh':
            assert argv == ['ssh', '-G', 'synthetic-github']
            return state['ssh_host']
        raise AssertionError('external process attempted in offline test')

    monkeypatch.setattr(private_storage, '_run', metadata)
    if _REAL_REPOSITORY_ROOT is not None:
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
    assert private_storage.prove(requested) == requested
    assert not requested.exists()
    git_calls = [call for call in repositories['commands'] if call[0] == 'git']
    assert all(Path(call[2]) == repositories['repo'] for call in git_calls)


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
def test_ssh_alias_validation_remains_required(repositories, valid_alias):
    sample = repositories['sample']
    subprocess.run(['git', '-C', str(repositories['repo']), 'remote', 'set-url', 'origin',
                    sample['ssh_origin']], check=True)
    repositories['ssh_host'] = sample['ssh_hosts'][0 if valid_alias else 1]
    requested = repositories['data']/'receipt.json'
    if valid_alias:
        assert private_storage.prove(requested) == requested
    else:
        with pytest.raises(RuntimeError, match='SSH alias'):
            private_storage.prove(requested)
    assert not requested.exists()
