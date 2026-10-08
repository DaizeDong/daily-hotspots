"""Wrapper storage and publication preflight must resolve proved PRIVATE targets."""
from pathlib import Path
from types import SimpleNamespace

import pytest
import archive
import private_storage as storage


def metadata(monkeypatch, tmp_path, *, branch='main', remote='origin', merge='refs/heads/main',
             fault=None, visibility='true', push_url=None, push_visibility=None):
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

    def proof(selected):
        if visibility != 'true' or push_visibility not in (None, 'true'):
            raise RuntimeError('synthetic PUBLIC or unknown proof')
        if push_url is not None and (not push_url or 'example.com' in push_url):
            raise RuntimeError('synthetic unproven publication route')
        return SimpleNamespace(root=str(selected), repositories=('AcmeCorp/synthetic-hotspots-config',),
                               signature='synthetic-publication')

    monkeypatch.setattr(storage, '_prove_repository', proof)
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
    {'visibility': 'false'}, {'visibility': 'null'},
    {'push_url': ''}, {'push_url': 'https://example.com/synthetic.git'},
    {'push_url': 'https://github.com/AcmeCorp/push-target.git', 'push_visibility': 'false'},
    {'push_url': 'https://github.com/AcmeCorp/push-target.git', 'push_visibility': 'null'},
    {'push_url': 'https://github.com/AcmeCorp/one.git\nhttps://github.com/AcmeCorp/two.git'},
])
def test_publication_refuses_unverified_branch_upstream_or_push_destination(monkeypatch, tmp_path, options):
    metadata(monkeypatch, tmp_path, **options)
    with pytest.raises(RuntimeError):
        storage.publication_target(tmp_path)


@pytest.mark.parametrize('explicit', [False, True])
def test_log_path_is_proved_without_creating_directories(monkeypatch, tmp_path, explicit):
    metadata(monkeypatch, tmp_path)
    monkeypatch.setattr(archive, 'resolve_archive_dir', lambda: tmp_path / 'archive')
    selected = tmp_path / 'data/archive/logs' if explicit else tmp_path / 'archive/logs'
    result = storage.resolve_log_path(str(selected) if explicit else None, 'synthetic.log')
    assert result == selected / 'synthetic.log'
    assert not selected.exists()


def test_log_path_refuses_an_undeclared_directory(monkeypatch, tmp_path):
    metadata(monkeypatch, tmp_path)
    selected = tmp_path / 'chosen-logs'
    with pytest.raises(RuntimeError) as failure:
        storage.resolve_log_path(str(selected), 'synthetic.log')
    assert 'undeclared' in str(failure.value.__cause__)
    assert not selected.exists()


@pytest.mark.parametrize('name', ['', '../outside.log', 'subdir/log', 'subdir\\log', 'file:stream', '.git', 'CON.log', 'nul', 'trailing.'])
def test_log_filename_cannot_escape_the_verified_directory(monkeypatch, tmp_path, name):
    metadata(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError):
        storage.resolve_log_path(str(tmp_path / 'archive/logs'), name)


@pytest.mark.parametrize('push_urls,expected', [
    ('https://github.com/AcmeCorp/synthetic-published.git',
     'https://github.com/AcmeCorp/synthetic-published/blob/daily/archive/archive/digest.md'),
    ('https://github.com/AcmeCorp/one.git\nhttps://github.com/AcmeCorp/two.git', ''),
])
def test_digest_link_uses_the_publication_upstream(monkeypatch, tmp_path, push_urls, expected):
    from types import SimpleNamespace
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
