"""Regression contracts for held runs, score inputs, source envelopes and report destinations."""
import copy
import importlib.util
from pathlib import Path

import pytest
import collect
import completeness
import identity_sweep
import lib
import private_storage
import run
import score
import sourcehealth

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location('repair_fixtures', ROOT / 'tools/make_fixtures.py')
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)
REAL_REPOSITORY_ROOT = private_storage._repository_root


def sink_spies(monkeypatch):
    calls = []
    def record(name, result=None):
        def called(*args, **kwargs):
            calls.append(name)
            return result
        return called
    monkeypatch.setattr(run.pc, 'deliver', record('delivery', (True, 'Synthetic acknowledgement')))
    monkeypatch.setattr(run.dg, 'write_digest_file', record('digest', 'synthetic-digest.md'))
    monkeypatch.setattr(run.dg, 'digest_github_url', lambda *a: '')
    monkeypatch.setattr(run.ar, 'archive_card', record('archive', ('archived', 'Synthetic acknowledgement')))
    monkeypatch.setattr(run.dg, 'register_digest_item', record('digest_item'))
    return calls, record


@pytest.mark.parametrize('history', ['raises', None, {}, [None]])
@pytest.mark.parametrize('dry_run', [False, True])
def test_required_history_failure_holds_every_sink(monkeypatch, history, dry_run):
    calls, record = sink_spies(monkeypatch)
    class Ledger:
        def list_active(self):
            if history == 'raises':
                raise OSError('Synthetic unavailable history')
            return history
        def get_pulse_seen(self):
            return {}
        def __getattr__(self, name):
            return record(name)
    result = run.process(fixtures.delivery_scenario()['candidates'], copy.deepcopy(lib.DEFAULT_CONFIG),
                         ledger=Ledger(), dry_run=dry_run, collection={})
    assert result['errors'] and result['errors'][0]['stage'] == 'dedup_history'
    assert result.get('held') is True
    assert result['new'] == result['resurface'] == 0
    assert result['pushed'] == result['archived'] == []
    assert result['watermark_advanced'] is False
    assert result['empty_day'] is False
    assert calls == []


@pytest.mark.parametrize('ledger_present', [False, True])
def test_no_ledger_preview_and_successful_empty_history_remain_valid(monkeypatch, ledger_present):
    calls, record = sink_spies(monkeypatch)
    class EmptyLedger:
        def list_active(self):
            return []
        def get_pulse_seen(self):
            return {}
    result = run.process(fixtures.delivery_scenario()['candidates'], copy.deepcopy(lib.DEFAULT_CONFIG),
                         ledger=EmptyLedger() if ledger_present else None, dry_run=True, collection={})
    assert result['errors'] == []
    assert result['new'] > 0
    assert 'delivery' in calls


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf'), 'NaN', 'Infinity', '-Infinity'])
@pytest.mark.parametrize('dimension', score._DIMS)
def test_nonfinite_score_dimension_is_rejected_before_clamping(value, dimension):
    dimensions = {key: 90 for key in score._DIMS}
    dimensions[dimension] = value
    with pytest.raises(ValueError, match='finite'):
        score.score_opportunity(dimensions, 2, 0, cfg=copy.deepcopy(lib.DEFAULT_CONFIG))


@pytest.mark.parametrize('value,expected', [(-10, 0), (0, 0), ('45', 45), (100, 100), (110, 100)])
def test_finite_dimension_clamping_is_preserved(value, expected):
    result = score.score_opportunity({key: value for key in score._DIMS}, 2, 0,
                                     cfg=copy.deepcopy(lib.DEFAULT_CONFIG))
    assert all(v == expected for v in result['score_breakdown'].values())


def test_invalid_score_is_reported_and_holds_the_run(monkeypatch):
    calls, _ = sink_spies(monkeypatch)
    candidates = fixtures.delivery_scenario()['candidates']
    candidates[0]['score_breakdown']['timing'] = 'NaN'
    result = run.process(candidates, copy.deepcopy(lib.DEFAULT_CONFIG), collection={})
    assert result['errors'][0]['stage'] == 'candidate_score'
    assert result.get('held') is True
    assert result['watermark_advanced'] is False
    assert calls == []


LANES = [('trustpilot', 'reviews'), ('sec_fulltext', 'hits'),
         ('federal_register', 'results'), ('usaspending', 'results'), ('muse_jobs', 'results')]


@pytest.mark.parametrize('lane,path', LANES)
@pytest.mark.parametrize('shape', ['missing', 'null', 'string', 'object'])
def test_missing_or_malformed_collection_never_records_a_successful_pull(lane, path, shape):
    raw = {} if shape == 'missing' else {path: {'null': None, 'string': 'invalid', 'object': {}}[shape]}
    result = collect.collect_new_source(lane, raw, run_id=fixtures.delivery_scenario()['run_id'])
    assert result['pulls']
    assert all(lib.is_failed_pull(pull) for pull in result['pulls'])
    assert result['signals'] == []


@pytest.mark.parametrize('lane,path', LANES)
def test_recognized_empty_collection_is_a_successful_pull(lane, path):
    result = collect.collect_new_source(lane, {path: []}, run_id=fixtures.delivery_scenario()['run_id'])
    assert result['pulls'] and all(not lib.is_failed_pull(pull) for pull in result['pulls'])
    assert result['signals'] == []


def test_nested_sec_empty_and_appstore_empty_feed_keep_their_contracts():
    assert collect.parse_sec_fulltext({'hits': {'hits': []}})['errors'] == []
    assert collect.parse_appstore_rss({'feed': {}})['errors'] == []


@pytest.mark.parametrize('writer', [sourcehealth.write_report, completeness.write_report])
@pytest.mark.parametrize('visibility', ['false', 'null', 'raises'])
def test_report_writer_rejects_unproved_destination_before_creating_parents(monkeypatch, tmp_path, writer, visibility):
    def refused(selected):
        raise RuntimeError('synthetic PRIVATE proof unavailable: ' + visibility)
    monkeypatch.setattr(private_storage, '_prove_repository', refused)
    out = tmp_path / 'not-created' / 'report.json'
    with pytest.raises(RuntimeError, match='PRIVATE|private|verification|initialize'):
        writer(out, {'verdict': 'synthetic'})
    assert not out.parent.exists()


@pytest.mark.parametrize('writer', [sourcehealth.write_report, completeness.write_report])
@pytest.mark.parametrize('kind', ['unversioned', 'tool'])
def test_report_writer_rejects_unversioned_and_tool_destinations(monkeypatch, tmp_path, writer, kind):
    def unversioned(existing):
        raise RuntimeError('runtime target is not inside a Git worktree')
    monkeypatch.setattr(private_storage, '_repository_root',
                        REAL_REPOSITORY_ROOT if kind == 'tool' else unversioned)
    out = (ROOT if kind == 'tool' else tmp_path) / 'must-not-create-source7' / 'report.json'
    with pytest.raises(RuntimeError):
        writer(out, {'verdict': 'synthetic'})
    assert not out.parent.exists()


@pytest.mark.parametrize('writer', [sourcehealth.write_report, completeness.write_report])
def test_report_writer_admits_verified_private_destination(tmp_path, writer):
    out = tmp_path / 'nested' / 'report.json'
    writer(out, {'verdict': 'synthetic'})
    assert 'synthetic' in out.read_text(encoding='utf-8')


def test_atomic_report_keeps_prior_file_on_replace_failure(monkeypatch, tmp_path):
    out = tmp_path / 'report.json'
    out.write_text('synthetic prior report', encoding='utf-8')
    def fail(*args):
        raise OSError('Synthetic replace failure')
    monkeypatch.setattr(completeness.os, 'replace', fail)
    with pytest.raises(OSError):
        completeness.write_report(out, {'verdict': 'synthetic new report'})
    assert out.read_text(encoding='utf-8') == 'synthetic prior report'
    assert list(tmp_path.glob('*.tmp')) == []


@pytest.mark.parametrize('explicit', [False, True])
def test_identity_proves_destination_before_token_or_live_sweep(monkeypatch, tmp_path, explicit):
    calls = []
    monkeypatch.setattr(identity_sweep.R, 'resolve_roster_path', lambda *a: tmp_path / 'roster.json')
    monkeypatch.setattr(identity_sweep.R, 'load_roster', lambda *a: fixtures.roster_example())
    monkeypatch.setattr(identity_sweep, 'load_token', lambda *a: calls.append('token') or 'synthetic-token')
    monkeypatch.setattr(identity_sweep, 'sweep', lambda *a, **kw: calls.append('sweep') or {})
    monkeypatch.setattr(identity_sweep, 'summarize', lambda *a: {'flags': [], 'dead': [], 'drift': []})
    monkeypatch.setattr(identity_sweep.subprocess, 'call', lambda *a, **kw: calls.append('feed') or 0)
    def refused(selected):
        raise RuntimeError('synthetic PUBLIC proof')
    monkeypatch.setattr(private_storage, '_prove_repository', refused)
    args = ['--feed-yield'] + (['--out', str(tmp_path / 'archive/out.json')] if explicit else [])
    with pytest.raises(RuntimeError):
        identity_sweep.main(args)
    assert calls == []
    assert not (tmp_path / 'archive').exists()


@pytest.mark.parametrize('explicit', [False, True])
def test_identity_admits_private_output_and_preserves_feed_flow(monkeypatch, tmp_path, explicit):
    calls = []
    monkeypatch.setattr(identity_sweep.R, 'resolve_roster_path', lambda *a: tmp_path / 'roster.json')
    monkeypatch.setattr(identity_sweep.R, 'load_roster', lambda *a: fixtures.roster_example())
    monkeypatch.setattr(identity_sweep, 'load_token', lambda *a: calls.append('token') or 'synthetic-token')
    monkeypatch.setattr(identity_sweep, 'sweep', lambda *a, **kw: calls.append('sweep') or {})
    monkeypatch.setattr(identity_sweep, 'summarize', lambda *a: {'flags': [], 'dead': [], 'drift': []})
    monkeypatch.setattr(identity_sweep.subprocess, 'call', lambda *a, **kw: calls.append('feed') or 0)
    args = ['--feed-yield'] + (['--out', str(tmp_path / 'archive/out.json')] if explicit else [])
    assert identity_sweep.main(args) == 0
    assert calls == ['token', 'sweep', 'feed']
    assert len(list((tmp_path / 'archive').glob('*.json'))) == 1
