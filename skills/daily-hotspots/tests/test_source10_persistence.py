"""Actual archive writes and native weekly publication with synthetic external effects."""
import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest
import archive
import lib
import private_storage
from test_source10_operational import _gen


@pytest.fixture
def archive_case(tmp_path, monkeypatch):
    base = tmp_path / 'archive'
    base.mkdir()
    card = _gen.source10_scenario()['card']
    card.update(canonical_key='synthetic-archive', independent_source_count=2,
                final_score=80, pushed=True)

    def prove(path, **kwargs):
        path = Path(path)
        assert path.is_relative_to(base)
        return path

    monkeypatch.setattr(archive, 'resolve_archive_dir', lambda selected=None: base)
    monkeypatch.setattr(private_storage, 'prove', prove)
    return base, card, copy.deepcopy(lib.DEFAULT_CONFIG)


def test_archive_success_persists_ledger_and_state(archive_case):
    base, card, config = archive_case
    status, identifier = archive.archive_card(card, cfg=config)
    records = [json.loads(line) for line in (base / 'opportunities.jsonl').read_text(encoding='utf-8').splitlines()]
    state = json.loads((base / 'dedup-state.json').read_text(encoding='utf-8'))
    assert status == 'archived' and len(records) == 1
    assert records[0]['canonical_key'] == card['canonical_key']
    assert records[0]['opportunity_id'] == identifier
    assert state[card['canonical_key']]['push_count'] == 1
    assert state[card['canonical_key']]['opportunity_id'] == identifier
    assert not list(base.glob('.dedup-state-*.tmp'))


def test_archive_replace_failure_retains_confirmed_append_and_prepared_state(archive_case, monkeypatch):
    base, card, config = archive_case
    assert archive.archive_card(card, cfg=config)[0] == 'archived'
    state_path = base / 'dedup-state.json'
    before = state_path.read_bytes()
    observed = []
    replace = archive.os.replace

    def fail_after_append(source, destination):
        if Path(destination) == state_path:
            lines = (base / 'opportunities.jsonl').read_text(encoding='utf-8').splitlines()
            prepared = json.loads(Path(source).read_text(encoding='utf-8'))
            assert len(lines) == 2
            assert prepared[card['canonical_key']]['push_count'] == 2
            observed.append(Path(source))
            raise OSError('synthetic state replacement failure after confirmed append')
        return replace(source, destination)

    monkeypatch.setattr(archive.os, 'replace', fail_after_append)
    with pytest.raises(OSError, match='after confirmed append'):
        archive.archive_card(card, cfg=config)
    assert len(observed) == 1 and observed[0].is_file()
    assert state_path.read_bytes() == before
    assert len((base / 'opportunities.jsonl').read_text(encoding='utf-8').splitlines()) == 2


@pytest.mark.parametrize('case', ['success', 'no-changes', 'report-only', 'preflight-refused',
                                 'worker-failed', 'commit-failed', 'missing-roster'])
def test_weekly_wrapper_publication_executes_scoped_roster_flow(tmp_path, case):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    private = tmp_path / 'private'
    private.mkdir()
    scenario = _gen.source10_publication_scenario()
    if case != 'missing-roster':
        (private / scenario['roster_name']).write_text('{}\n', encoding='utf-8')
    shell = shutil.which('powershell.exe') or shutil.which('pwsh')
    if shell is None:
        pytest.fail('Weekly native publication validation requires PowerShell')
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File',
                             str(Path(__file__).with_name('weekly-publication-harness.ps1')),
                             str(scripts), case, str(tmp_path)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    expected = 0 if case in {'success', 'no-changes', 'report-only'} else 9 if case == 'worker-failed' else 1
    assert result.returncode == receipt['rc'] == expected, result.stdout + result.stderr
    assert receipt['proof_calls'] == (0 if case == 'report-only' else 1)
    assert receipt['worker_calls'] == (0 if case == 'preflight-refused' else 1)
    if receipt['worker_calls']:
        assert '--yield' in receipt['worker_arguments']
        assert ('--apply' in receipt['worker_arguments']) is (case != 'report-only')
    if case in {'success', 'no-changes'}:
        calls = ['roster git add', 'roster git diff']
        if case == 'success':
            calls.append('roster git commit')
        calls.extend(['roster git pull', 'roster git push'])
        assert receipt['calls'] == calls
        assert receipt['arguments']['roster git add'] == ['add', '--', scenario['roster_name']]
        assert receipt['arguments']['roster git push'] == ['push', 'backup', 'HEAD:refs/heads/daily/archive']
    else:
        assert 'roster git push' not in receipt['calls']
        if case != 'commit-failed':
            assert receipt['calls'] == []
