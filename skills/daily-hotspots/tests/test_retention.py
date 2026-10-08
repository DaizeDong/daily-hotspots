"""Lifecycle regression tests; all records come from the fixture generator."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import pytest
import dedup as dd
import digest

path = Path(__file__).resolve().parents[3] / 'tools/make_fixtures.py'
spec = importlib.util.spec_from_file_location('retention_fixtures', path)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class Ledger(dd.LedgerClient):
    def __init__(self):
        super().__init__(cmd=['example-reminder'])
        self.rows = {r['id']: r for r in fixtures.retention_rows()}
        self.calls = []

    def _run(self, verb, args):
        import json
        self.calls.append((verb, args))
        def val(flag):
            return args[args.index(flag)+1]
        if verb == 'list':
            rows = list(self.rows.values())
            if '--active' in args:
                rows = [r for r in rows if r['state'] in ('pending','doing','blocked')]
            return {'items': deepcopy(rows)}
        if verb == 'get':
            return {'item': deepcopy(self.rows[val('--id')])}
        if verb == 'add':
            row = next(r for r in self.rows.values() if r['idempotency_key']==val('--idempotency-key'))
            row['ext'].update(json.loads(val('--ext')))
        else:
            row = self.rows[val('--id')]
            if verb == 'update':
                row['ext'].update(json.loads(val('--ext')))
            elif verb == 'transition':
                assert row['state'] == val('--expect')
                row['state'] = val('--to')
        return {'item': deepcopy(row)}


def test_read_is_read_only_and_expiry_preserves_adopted_and_undated():
    ledger = Ledger()
    before = deepcopy(ledger.rows)
    ledger.list_active()
    assert ledger.rows == before
    result = ledger.expire_pending()
    assert result['expired'] == ['old']
    for key in before.keys()-{'old'}:
        assert ledger.rows[key] == before[key]
    assert ledger.rows['old']['state'] == 'cancelled'
    assert ledger.rows['old']['ext']['x_other_value'] == 42
    assert ledger.expire_pending()['expired'] == []


def test_new_evidence_reopens_same_autoexpired_id_and_retains_history():
    ledger = Ledger()
    ledger.expire_pending()
    ext = {'x_daily_hotspots_last_seen': '2026-06-26T00:00:00Z',
           'x_daily_hotspots_first_seen': '2026-06-26T00:00:00Z',
           'x_daily_hotspots_samples': [{'ts':'2026-06-26T00:00:00Z'}],
           'x_daily_hotspots_push_count': 1}
    result = ledger.upsert({'canonical_key':'example:old', 'title':'Example updated'}, ext)
    assert result['item']['id'] == 'old'
    assert result['item']['state'] == 'pending'
    saved = result['item']['ext']
    assert saved['x_daily_hotspots_first_seen'] == '2026-05-01T00:00:00Z'
    assert len(saved['x_daily_hotspots_samples']) == 2
    assert saved['x_daily_hotspots_push_count'] == 3
    assert saved['x_other_value'] == 42
    assert saved['x_daily_hotspots_expiry'] is None


@pytest.mark.parametrize('key', ['closed','done'])
def test_manual_terminal_rows_are_not_reopened(key):
    ledger = Ledger()
    previous = ledger.rows[key]['state']
    result = ledger.upsert({'canonical_key':'example:'+key},
                           {'x_daily_hotspots_last_seen':'2026-06-26T00:00:00Z'})
    assert result['item']['state'] == previous


def test_old_evidence_does_not_reopen_archive():
    ledger = Ledger()
    ledger.expire_pending()
    result = ledger.upsert({'canonical_key':'example:old'},
                           {'x_daily_hotspots_last_seen':'2026-06-01T00:00:00Z'})
    assert result['item']['state'] == 'cancelled'


def test_digest_registration_is_completed_information():
    class Capture:
        def _run(self, verb, args):
            return args
    args = digest.register_digest_item(Capture(), date='2026-06-01')
    assert args[args.index('--kind')+1] == 'event'
    assert args[args.index('--state')+1] == 'done'


def test_yield_registration_is_completed_information():
    from importlib import import_module
    class Capture:
        def _run(self, verb, args):
            return args
    args = import_module('yield').register_yield_item(Capture(), week='2026-W26')
    assert args[args.index('--kind')+1] == 'event'
    assert args[args.index('--state')+1] == 'done'


def test_expiry_failure_is_visible():
    ledger = Ledger()
    original = ledger._run
    def fail(verb, args):
        if verb == 'transition':
            raise RuntimeError('example failure')
        return original(verb, args)
    ledger._run = fail
    with pytest.raises(RuntimeError, match='example failure'):
        ledger.expire_pending()


def test_reopen_retry_after_state_failure_does_not_duplicate_history():
    ledger = Ledger()
    ledger.expire_pending()
    original = ledger._run
    def fail(verb, args):
        if verb == 'transition': raise RuntimeError('example state failure')
        return original(verb, args)
    ledger._run = fail
    candidate = {'canonical_key':'example:old'}
    ext = {'x_daily_hotspots_last_seen':'2026-06-26T00:00:00Z',
           'x_daily_hotspots_samples':[{'ts':'2026-06-26T00:00:00Z'}],
           'x_daily_hotspots_push_count':1}
    with pytest.raises(RuntimeError): ledger.upsert(candidate, ext)
    ledger._run = original
    ledger._prior_by_key = None
    result = ledger.upsert(candidate, ext)['item']
    assert result['state'] == 'pending'
    assert result['ext']['x_daily_hotspots_push_count'] == 3
    assert len(result['ext']['x_daily_hotspots_samples']) == 2


def test_driver_runs_retention_only_on_real_runs(monkeypatch, archive_path):
    import run
    calls = []
    class DriverLedger:
        def init(self): pass
        def expire_pending(self): calls.append('expire'); return {'expired': []}
    monkeypatch.setattr(run.dd, 'LedgerClient', DriverLedger)
    monkeypatch.setattr(run, 'process', lambda *a, **kw: {})
    monkeypatch.setattr('sys.argv', ['run.py', '--archive-dir', str(archive_path)])
    class Input:
        buffer = __import__('io').BytesIO(b'[]')
    monkeypatch.setattr('sys.stdin', Input())
    assert run.main() == 0
    assert calls == ['expire']
    calls.clear()
    Input.buffer = __import__('io').BytesIO(b'[]')
    monkeypatch.setattr('sys.argv', ['run.py', '--dry-run', '--archive-dir', str(archive_path)])
    assert run.main() == 0
    assert calls == []
