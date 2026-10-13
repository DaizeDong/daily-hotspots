"""Wrapper storage and publication preflight must resolve companion targets by path."""
from pathlib import Path
import importlib.util
from types import SimpleNamespace

import pytest
import archive
import private_storage as storage
from test_rotation_commit import fixtures


def metadata(monkeypatch, tmp_path, *, branch='main', remote='origin', merge='refs/heads/main',
             fault=None, push_url=None):
    origin = 'https://github.com/AcmeCorp/synthetic-hotspots-config.git'
    queries = []
    monkeypatch.setattr(storage, '_repository_root', lambda existing: tmp_path)

    def query(argv):
        queries.append(argv)
        tail = argv[3:]
        if tail[0] == fault:
            raise RuntimeError('Synthetic Git metadata unavailable')
        if tail[:3] == ['remote', 'get-url', '--push']:
            return push_url if push_url is not None else origin
        if tail[:2] == ['remote', 'get-url']:
            return origin
        if tail[0] == 'symbolic-ref':
            return branch
        if tail[:2] == ['config', '--get']:
            return remote if tail[2].endswith('.remote') else merge
        if tail[0] == 'check-ref-format':
            if '..' in tail[-1] or tail[-1].startswith('-'):
                raise RuntimeError('Synthetic invalid branch')
            return ''
        if tail[0] == 'rev-parse':
            return 'a' * 40
        raise AssertionError(argv)

    monkeypatch.setattr(storage, '_run', query)
    return queries


@pytest.mark.parametrize('local,remote,target', [('main', 'origin', 'main'),
                                               ('daily-work', 'backup', 'daily/archive')])
def test_publication_uses_the_configured_upstream(monkeypatch, tmp_path, local, remote, target):
    metadata(monkeypatch, tmp_path, branch=local, remote=remote, merge='refs/heads/' + target)
    result = storage.publication_target(tmp_path)
    assert result == {'local_branch': local, 'remote': remote, 'branch': target,
                      'refspec': 'HEAD:refs/heads/' + target,
                      'repository_root': str(tmp_path), 'roster_pathspec': 'roster.json'}


@pytest.mark.parametrize('options', [
    {'branch': ''}, {'branch': '-bad'}, {'branch': 'bad..branch'},
    {'remote': ''}, {'remote': '.'}, {'remote': '-option'},
    {'merge': ''}, {'merge': 'refs/tags/v1'}, {'merge': 'refs/heads/bad..branch'},
    {'fault': 'rev-parse'}, {'fault': 'symbolic-ref'},
    {'push_url': ''},
    {'push_url': 'https://github.com/AcmeCorp/one.git\nhttps://github.com/AcmeCorp/two.git'},
])
def test_publication_refuses_invalid_branch_upstream_or_push_destination(monkeypatch, tmp_path, options):
    metadata(monkeypatch, tmp_path, **options)
    with pytest.raises(RuntimeError):
        storage.publication_target(tmp_path)


@pytest.mark.parametrize('explicit', [False, True])
def test_log_path_is_checked_without_creating_directories(monkeypatch, tmp_path, explicit):
    metadata(monkeypatch, tmp_path)
    monkeypatch.setattr(archive, 'resolve_archive_dir', lambda: tmp_path / 'archive')
    selected = tmp_path / 'data/archive/logs' if explicit else tmp_path / 'archive/logs'
    result = storage.resolve_log_path(str(selected) if explicit else None, 'synthetic.log')
    assert result == selected / 'synthetic.log'
    assert not selected.exists()


@pytest.mark.parametrize('name', ['', '../outside.log', 'subdir/log', 'subdir\\log', 'file:stream', '.git', 'CON.log', 'nul', 'trailing.'])
def test_log_filename_cannot_escape_the_verified_directory(monkeypatch, tmp_path, name):
    metadata(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError):
        storage.resolve_log_path(str(tmp_path / 'archive/logs'), name)


def test_runtime_log_cannot_append_compressed_diagnostics(monkeypatch, tmp_path):
    case = fixtures.log_compression_scenario()
    metadata(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match='compressed'):
        storage.resolve_log_path(str(tmp_path), case['compressed'])


def test_runtime_log_cannot_append_compression_index(monkeypatch, tmp_path):
    metadata(monkeypatch, tmp_path)
    selected = tmp_path / 'archive/logs/legacy-user-root-20261006'
    with pytest.raises(RuntimeError, match='compressed'):
        storage.resolve_log_path(str(selected), 'compression.json')
    assert not selected.exists()


def test_legacy_log_representations_have_disjoint_owners():
    case = fixtures.log_compression_scenario()
    spec = importlib.util.spec_from_file_location('hotspots_contract_kit', storage.ROOT / 'guards/tools/storage_contract.py')
    kit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kit)
    contract = kit.validate_contract(storage.ROOT)
    raw = next(a for a in contract['artifacts'] if a['artifact_id'] == 'legacy-user-root-logs')
    prefix = raw['path_pattern'].rsplit('/', 1)[0] + '/'
    for name, expected in [(case['original'], 'legacy-user-root-logs'),
                           (case['compressed'], 'legacy-log-compressed'),
                           ('compression.json', 'legacy-log-compression-index')]:
        assert [a['artifact_id'] for a in kit.owners(contract, prefix + name)] == [expected]
    assert kit.owners(contract, prefix + case['unclassified']) == []


@pytest.mark.parametrize('push_urls,expected', [
    ('https://github.com/AcmeCorp/synthetic-published.git',
     'https://github.com/AcmeCorp/synthetic-published/blob/daily/archive/archive/digest.md'),
    ('https://github.com/AcmeCorp/one.git\nhttps://github.com/AcmeCorp/two.git', ''),
])
def test_digest_link_uses_the_publication_upstream(monkeypatch, tmp_path, push_urls, expected):
    import importlib.util
    import subprocess
    import digest

    def query(argv, **kwargs):
        tail = argv[3:]
        replies = {
            ('rev-parse', '--show-toplevel'): str(tmp_path),
            ('rev-parse', '--abbrev-ref', 'HEAD'): 'daily-work',
            ('remote', 'get-url', 'origin'): 'https://github.com/AcmeCorp/synthetic-origin.git',
            ('config', '--get', 'branch.daily-work.remote'): 'backup',
            ('config', '--get', 'branch.daily-work.merge'): 'refs/heads/daily/archive',
            ('remote', 'get-url', '--push', '--all', 'backup'): push_urls,
        }
        return SimpleNamespace(returncode=0, stdout=replies[tuple(tail)])
    monkeypatch.setattr(subprocess, 'run', query)
    assert digest.digest_github_url(str(tmp_path / 'archive/digest.md')) == expected
