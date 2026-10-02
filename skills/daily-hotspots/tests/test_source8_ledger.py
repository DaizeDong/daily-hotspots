"""Unreadable adapter responses must never become an empty dedup history."""
import copy
import json
from subprocess import CompletedProcess

import pytest

import dedup
import lib
import run
from test_source7_repairs import fixtures, sink_spies


INVALID_PAGES = [
    '', '  ', '{}', 'null', '[]',
    '{"error":"Synthetic unavailable history"}',
    '{"items":[],"error":"Synthetic failure"}',
    '{"items":[],"errors":["Synthetic failure"]}',
    '{"items":[],"ok":false}',
    '{"items":[],"success":false}',
    '{"items":[],"status":"error"}',
    '{"items":null}', '{"items":{}}', '{"items":"invalid"}',
    '{"items":[null]}', '{"items":[[]]}', '{"items":["invalid"]}',
    '{"items":[],"next_cursor":1}', '{"items":[],"next_cursor":true}',
    '{"items":[],"next_cursor":{}}', '{"items":[],"next_cursor":""}',
]


def responses(monkeypatch, pages):
    pending = iter(pages)
    calls = []

    def invoke(argv, **kwargs):
        calls.append(argv)
        return CompletedProcess(argv, 0, next(pending), '')

    monkeypatch.setattr(dedup.subprocess, 'run', invoke)
    return calls


@pytest.mark.parametrize('page', INVALID_PAGES)
def test_adapter_rejects_unreadable_history_before_the_runner_sees_rows(monkeypatch, page):
    responses(monkeypatch, [page])
    client = dedup.LedgerClient(cmd=['synthetic-reminder'], cfg=copy.deepcopy(lib.DEFAULT_CONFIG))
    with pytest.raises(RuntimeError, match='reminder|history|response|cursor'):
        client.list_active(window=False)


@pytest.mark.parametrize('page', INVALID_PAGES)
def test_real_adapter_parse_failure_holds_run_before_every_sink(monkeypatch, page):
    calls, _ = sink_spies(monkeypatch)
    responses(monkeypatch, [page])
    client = dedup.LedgerClient(cmd=['synthetic-reminder'], cfg=copy.deepcopy(lib.DEFAULT_CONFIG))
    result = run.process(fixtures.delivery_scenario()['candidates'], copy.deepcopy(lib.DEFAULT_CONFIG),
                         ledger=client, dry_run=False, collection={})
    assert result.get('held') is True
    assert result['errors'][0]['stage'] == 'dedup_history'
    assert result['watermark_advanced'] is False
    assert result['pushed'] == result['archived'] == []
    assert calls == []


@pytest.mark.parametrize('terminal', [{'items': []}, {'items': [], 'next_cursor': None}])
def test_explicit_empty_items_are_valid_terminal_history(monkeypatch, terminal):
    responses(monkeypatch, [json.dumps(terminal)])
    client = dedup.LedgerClient(cmd=['synthetic-reminder'])
    assert client.list_active(window=False) == []


def test_multiple_pages_keep_rows_and_forward_the_exact_cursor(monkeypatch):
    calls = responses(monkeypatch, [
        '{"items":[{"id":"synthetic-a","ext":{}}],"next_cursor":"synthetic-a"}',
        '{"items":[{"id":"synthetic-b","ext":{}}],"next_cursor":null}',
    ])
    client = dedup.LedgerClient(cmd=['synthetic-reminder'])
    assert [row['id'] for row in client.list_active(window=False)] == ['synthetic-a', 'synthetic-b']
    assert calls[1][-2:] == ['--cursor', 'synthetic-a']


def test_malformed_later_page_does_not_return_partial_history(monkeypatch):
    responses(monkeypatch, ['{"items":[{"id":"synthetic-a"}],"next_cursor":"synthetic-a"}', '{}'])
    client = dedup.LedgerClient(cmd=['synthetic-reminder'])
    with pytest.raises(RuntimeError, match='items|history|response'):
        client.list_active(window=False)


def test_repeated_cursor_is_rejected_without_a_third_read(monkeypatch):
    calls = responses(monkeypatch, [
        '{"items":[{"id":"synthetic-a"}],"next_cursor":"synthetic-a"}',
        '{"items":[{"id":"synthetic-a"}],"next_cursor":"synthetic-a"}',
    ])
    client = dedup.LedgerClient(cmd=['synthetic-reminder'])
    with pytest.raises(RuntimeError, match='cursor'):
        client.list_active(window=False)
    assert len(calls) == 2
