"""Use real local Git worktrees; the runtime check is path-only and starts no subprocess."""
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
_REAL_REPOSITORY_ROOT = getattr(private_storage, '_repository_root', None)


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
    state = {'sample': sample, 'repo': companion, 'data': data, 'commands': []}

    real_run = subprocess.run
    def recorded(argv, *args, **kwargs):
        state['commands'].append(argv)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, 'run', recorded)
    monkeypatch.setattr(private_storage, '_repository_root', _REAL_REPOSITORY_ROOT)
    return state


def _long_descendant(parent, sample):
    path = parent
    while len(str(path)) < sample['long_path_minimum']:
        path /= sample['long_component']
    return path


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
    assert repositories['commands'] == []


def test_target_inside_own_source_tree_is_refused(repositories):
    requested = private_storage.ROOT/'archive'/'receipt.json'
    with pytest.raises(RuntimeError, match='separate PRIVATE companion'):
        private_storage.prove(requested)
    assert repositories['commands'] == []


def test_target_outside_any_worktree_is_refused(repositories, tmp_path):
    loose = tmp_path/'loose'
    loose.mkdir()
    with pytest.raises(RuntimeError, match='not inside a Git worktree'):
        private_storage.prove(loose/'receipt.json')


def test_nested_invalid_git_marker_cannot_fall_back_to_outer_private_repo(repositories):
    nested = repositories['data']/'invalid'
    nested.mkdir()
    (nested/'.git').write_text(repositories['sample']['invalid_git_marker'], encoding='utf-8')
    with pytest.raises(RuntimeError):
        private_storage.prove(nested/'receipt.json')
    assert repositories['commands'] == []
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


def test_junction_uses_actual_destination(repositories):
    destination = repositories['data']/'inside'
    destination.mkdir()
    link = repositories['data']/'junction'
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(destination), str(link))
    else:
        link.symlink_to(destination, target_is_directory=True)
    try:
        assert private_storage.prove(link/'receipt.json') == destination/'receipt.json'
        assert not (destination/'receipt.json').exists()
    finally:
        link.rmdir() if os.name == 'nt' else link.unlink()
