"""Generated delivery acknowledgement and logical-run replay controls."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest
import dedup
import finalize_handoff
import lib
import run

from test_rotation_commit import fixtures
from test_run_workspace import _ready


class Ledger:
    def __init__(self):
        self.rows = []
        self.seen = {}
        self.arms = None
        self.watermarks = []
        self.digest_items = []

    def list_active(self):
        return []

    def upsert(self, card, ext):
        self.rows.append(copy.deepcopy(ext))

    def get_pulse_seen(self):
        return {}

    def set_pulse_seen(self, seen):
        self.seen = seen

    def set_bandit_arms(self, arms):
        self.arms = arms

    def add_watermark(self, value):
        self.watermarks.append(value)

    def _run(self, *args):
        self.digest_items.append(args)
        return {}


def pipeline(tmp_path, monkeypatch, acknowledgement, *, dry=False, raises=False):
    sample = fixtures.delivery_scenario()
    ledger, calls = Ledger(), []

    def deliver(message, dry_run=False):
        calls.append((message, dry_run))
        if raises:
            raise RuntimeError(sample['exception'])
        return acknowledgement

    monkeypatch.setattr(run.pc, 'deliver', deliver)
    result = run.process(sample['candidates'], copy.deepcopy(lib.DEFAULT_CONFIG), ledger,
                         dry_run=dry, run_id=sample['run_id'], archive_dir=str(tmp_path),
                         bandit_arms={}, persist_bandit=True, collection={})
    return result, ledger, calls


def assert_held(result, ledger, tmp_path):
    assert result['errors'] and any(error['stage'] == 'delivery' for error in result['errors'])
    assert not result['watermark_advanced'] and not ledger.watermarks
    assert ledger.arms is None and not ledger.seen and not ledger.digest_items
    assert result['pushed'] == [] and result['coverage']['pushed'] == 0
    assert result['archived'] and Path(result['digest_path']).is_file()
    assert all(row.get(dedup.EXT_PREFIX + 'push_count', 0) == 0 for row in ledger.rows)
    records = [json.loads(line) for line in (tmp_path/'opportunities.jsonl').read_text().splitlines()]
    assert all(not row['pushed'] and row['push_count'] == 0 for row in records)
    state = json.loads((tmp_path/'dedup-state.json').read_text())
    assert all(row['push_count'] == 0 for row in state.values())


@pytest.mark.parametrize('ack', fixtures.delivery_scenario()['bad_acknowledgements'])
def test_missing_failed_or_malformed_acknowledgement_holds_success(tmp_path, monkeypatch, ack):
    result, ledger, calls = pipeline(tmp_path, monkeypatch, ack)
    assert len(calls) == 1
    assert_held(result, ledger, tmp_path)


def test_transport_exception_is_reported_and_holds_success(tmp_path, monkeypatch):
    result, ledger, calls = pipeline(tmp_path, monkeypatch, None, raises=True)
    assert len(calls) == 1
    assert_held(result, ledger, tmp_path)


def test_explicit_acknowledgement_commits_success(tmp_path, monkeypatch):
    result, ledger, calls = pipeline(tmp_path, monkeypatch, fixtures.delivery_scenario()['success'])
    assert len(calls) == 1 and not result['errors']
    assert result['pushed'] and result['coverage']['pushed'] == 1
    assert result['watermark_advanced'] and ledger.watermarks
    assert ledger.arms and ledger.seen and ledger.digest_items
    assert ledger.rows[0][dedup.EXT_PREFIX + 'push_count'] == 1


def test_preview_observer_none_is_not_a_production_acknowledgement(tmp_path, monkeypatch):
    result, ledger, calls = pipeline(tmp_path, monkeypatch, None, dry=True)
    assert not result['errors'] and result['pushed'] and calls[0][1] is True
    assert not ledger.rows and not ledger.watermarks and ledger.arms is None
    assert not list(tmp_path.iterdir())


def test_success_counters_are_uncommitted_during_delivery(tmp_path, monkeypatch):
    sample, ledger = fixtures.delivery_scenario(), Ledger()

    def deliver(message, dry_run=False):
        assert not ledger.rows and not ledger.watermarks and not ledger.seen
        assert not (tmp_path/'opportunities.jsonl').exists()
        assert not (tmp_path/'dedup-state.json').exists()
        return sample['success']

    monkeypatch.setattr(run.pc, 'deliver', deliver)
    result = run.process(sample['candidates'], copy.deepcopy(lib.DEFAULT_CONFIG), ledger,
                         archive_dir=str(tmp_path), collection={})
    assert not result['errors'] and result['pushed']


def test_failed_acknowledgement_makes_cli_fail(tmp_path, monkeypatch):
    sample = fixtures.delivery_scenario()
    source = tmp_path/'candidates.json'
    source.write_text(json.dumps(sample['candidates']), encoding='utf-8')
    monkeypatch.setattr(run, 'load_config', lambda: copy.deepcopy(lib.DEFAULT_CONFIG))
    monkeypatch.setattr(run.pc, 'deliver', lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, 'argv', ['run.py', '--in', str(source), '--no-ledger',
                                    '--archive-dir', str(tmp_path)])
    assert run.main() == 1


def finalize(directory, sample):
    return finalize_handoff.main(['--run-dir', str(directory), '--run-id', sample['run_id'],
                                 '--nonce', sample['nonce']])


@pytest.mark.parametrize('outcome', ['complete', 'failed', 'exception'])
@pytest.mark.parametrize('changed_content', [False, True])
def test_fresh_nonce_cannot_replay_a_logical_run(tmp_path, monkeypatch, outcome, changed_content):
    sample = fixtures.delivery_scenario()
    original = _ready(tmp_path, sample)
    calls = []

    def driver(argv, **kwargs):
        calls.append(argv)
        if outcome == 'exception':
            raise OSError(sample['exception'])
        return subprocess.CompletedProcess(argv, int(outcome == 'failed'))

    monkeypatch.setattr(finalize_handoff.subprocess, 'run', driver)
    assert finalize(tmp_path, sample) == {'complete': 0, 'failed': 1, 'exception': 5}[outcome]
    sample['nonce'] = sample['next_nonce']
    if changed_content:
        sample['candidates'] = []
    _ready(tmp_path, sample)
    assert finalize(tmp_path, sample) != 0
    assert len(calls) == 1
    assert Path(calls[0][calls[0].index('--in') + 1]).read_bytes() == original


def test_concurrent_new_nonce_is_excluded_before_driver(tmp_path, monkeypatch):
    sample = fixtures.delivery_scenario()
    _ready(tmp_path, sample)
    calls = []

    def driver(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            retry = {**sample, 'nonce': sample['next_nonce']}
            _ready(tmp_path, retry)
            assert finalize(tmp_path, retry) != 0
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(finalize_handoff.subprocess, 'run', driver)
    assert finalize(tmp_path, sample) == 0
    assert len(calls) == 1


def test_new_logical_run_is_allowed(tmp_path, monkeypatch):
    sample = fixtures.delivery_scenario()
    calls = []

    def driver(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(finalize_handoff.subprocess, 'run', driver)
    _ready(tmp_path, sample)
    assert finalize(tmp_path, sample) == 0
    sample.update(run_id=sample['next_run_id'], nonce=sample['next_nonce'])
    _ready(tmp_path, sample)
    assert finalize(tmp_path, sample) == 0 and len(calls) == 2
