"""Actual source-entrypoint rotation and current-handoff regressions, all generated."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import os

import pytest
import lib
import roster
import run
import finalize_handoff as finalizer
import private_storage
import source_rotation

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('rotation_fixtures', ROOT/'tools/make_fixtures.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


@pytest.fixture
def case(tmp_path, monkeypatch):
    sample = fixtures.rotation_scenario()
    roster_path = tmp_path/'roster.json'
    roster_path.write_text(json.dumps(sample['roster']), encoding='utf-8')
    cfg = copy.deepcopy(lib.DEFAULT_CONFIG)
    cfg['sources'].setdefault('twitterapi', {})['max_handles_per_run'] = 3
    monkeypatch.setattr(run, 'load_config', lambda: cfg)
    return dict(sample=sample, roster=roster_path, archive=tmp_path/'archive', cfg=cfg, root=tmp_path)


def invoke(case, monkeypatch, capsys, run_id, payload=None, dry=False):
    source = case['root']/'sources.json'
    source.write_text(json.dumps(payload if payload is not None else case['sample']['sources']), encoding='utf-8')
    argv = ['run.py','--sources',str(source),'--roster',str(case['roster']),
            '--archive-dir',str(case['archive']),'--run-id',run_id]
    if dry:
        argv.append('--dry-run')
    monkeypatch.setattr(sys,'argv',argv)
    code = run.main()
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def current(case):
    return json.loads(case['roster'].read_text(encoding='utf-8'))


def test_capped_actual_runs_cover_all_handles(case, monkeypatch, capsys):
    observed = set()
    for index in range(3):
        code, result = invoke(case, monkeypatch, capsys, f'synthetic-run-{index}')
        assert code == 0
        observed.update(result['rotation']['selected'])
        assert result['rotation']['consumed'] == 3
        assert current(case)['rotation_cursor'] == ((index+1)*3)%7
    assert observed == set(case['sample']['sources']['roster_responses'])


def test_replay_uses_original_selection_and_never_advances_twice(case, monkeypatch, capsys):
    _, first = invoke(case, monkeypatch, capsys, 'synthetic-replay')
    saved = case['roster'].read_bytes()
    code, replay = invoke(case, monkeypatch, capsys, 'synthetic-replay')
    assert code == 0
    assert replay['rotation']['selected'] == first['rotation']['selected']
    assert replay['rotation']['advanced'] is False
    assert replay['pulls_written'] == 0
    assert case['roster'].read_bytes() == saved


def test_failed_batch_waits_and_retry_consumes_once(case, monkeypatch, capsys):
    payload = copy.deepcopy(case['sample']['sources'])
    payload['roster_responses']['synth01'] = {'error':'synthetic unavailable'}
    before = case['roster'].read_bytes()
    code, result = invoke(case, monkeypatch, capsys, 'synthetic-retry',payload)
    assert code == 0 and result['sources_failed']
    assert result['rotation']['advanced'] is False
    assert case['roster'].read_bytes() == before
    _, retry = invoke(case, monkeypatch, capsys, 'synthetic-retry')
    assert retry['rotation']['advanced'] is True
    assert current(case)['rotation_cursor'] == 3
    assert retry['pulls_written'] == 1


def test_unknown_responses_and_preview_do_not_consume(case, monkeypatch, capsys):
    before = case['roster'].read_bytes()
    _, empty = invoke(case, monkeypatch, capsys, 'synthetic-unknown',
                      {'roster_responses':{'not_in_roster':{'tweets':[]}}})
    assert empty['rotation']['consumed'] == 0
    _, preview = invoke(case, monkeypatch, capsys, 'synthetic-preview',dry=True)
    assert preview['pulls_written'] == 0 and preview['rotation']['advanced'] is False
    assert case['roster'].read_bytes() == before


def test_preview_needs_no_initialized_archive(case,monkeypatch,capsys):
    monkeypatch.setattr(run.ar,'find_archive_dir',lambda *args: None)
    before=case['roster'].read_bytes()
    code,result=invoke(case,monkeypatch,capsys,'synthetic-uninitialized-preview',dry=True)
    assert code == 0 and result['rotation']['status']=='preview'
    assert case['roster'].read_bytes()==before
    assert not case['archive'].exists()


def test_roster_save_failure_can_resume_durable_receipts(case, monkeypatch, capsys):
    save = roster.save_roster
    def fail(*args,**kwargs):
        raise OSError('synthetic roster save failed')
    monkeypatch.setattr(roster,'save_roster',fail)
    code, result = invoke(case,monkeypatch,capsys,'synthetic-save-retry')
    assert code != 0 and 'error' in result
    assert current(case).get('rotation_cursor',0) == 0
    monkeypatch.setattr(roster,'save_roster',save)
    code, result = invoke(case,monkeypatch,capsys,'synthetic-save-retry')
    assert code == 0 and result['pulls_written'] == 0
    assert current(case)['rotation_cursor'] == 3


def test_failed_receipt_write_does_not_advance_and_resumes(case,monkeypatch,capsys):
    append=run._append_jsonl
    def fail(path,records):
        if Path(path).name.startswith('pulls-'):
            raise OSError('synthetic receipt write failure')
        return append(path,records)
    monkeypatch.setattr(run,'_append_jsonl',fail)
    code,_=invoke(case,monkeypatch,capsys,'synthetic-receipt-retry')
    assert code == 1 and current(case).get('rotation_cursor',0)==0
    monkeypatch.setattr(run,'_append_jsonl',append)
    code,result=invoke(case,monkeypatch,capsys,'synthetic-receipt-retry')
    assert code == 0 and result['pulls_written']==3
    assert current(case)['rotation_cursor']==3


def test_partial_recovery_needs_only_missing_successful_receipts(case,monkeypatch,capsys):
    responses=case['sample']['sources']['roster_responses']
    invoke(case,monkeypatch,capsys,'synthetic-partial',{'roster_responses':{'synth00':responses['synth00']}})
    assert current(case).get('rotation_cursor',0)==0
    _,result=invoke(case,monkeypatch,capsys,'synthetic-partial',
                    {'roster_responses':{name:responses[name] for name in ('synth01','synth02')}})
    assert result['rotation']['consumed']==3 and current(case)['rotation_cursor']==3


def test_incremental_nonempty_collection_preserves_all_evidence(case, monkeypatch, capsys):
    sample = fixtures.rotation_signal_scenario()
    responses = sample['sources']['roster_responses']
    code, first = invoke(case, monkeypatch, capsys, 'synthetic-signals',
                         {'roster_responses': {'synth00': responses['synth00']}})
    assert code == 0 and len(first['signals']) == 1
    code, final = invoke(case, monkeypatch, capsys, 'synthetic-signals',
                         {'roster_responses': {h: responses[h] for h in ('synth01', 'synth02')}})
    assert code == 0 and final['rotation']['consumed'] == 3
    coll = run.load_collection('synthetic-signals', str(case['archive']))
    assert coll['signals_collected'] == 3
    assert coll['pulls_observed'] == 3
    assert len(set(coll['signal_keys'])) == 3
    assert len(final['signals']) == 3
    omitted = [{'evidence': final['signals'][1:]}]
    coverage = run.build_coverage(omitted, [], [], [], [], {'below_floor': []}, [], coll)
    assert coverage['signals_unaccounted'] == 1
    complete = run.build_coverage([{'evidence': final['signals']}], [], [], [], [], {}, [], coll)
    assert complete['signals_unaccounted'] == 0


@pytest.mark.parametrize('replay', ['empty', 'identical'])
def test_collection_replays_preserve_effective_measurement(case, monkeypatch, capsys, replay):
    payload = fixtures.rotation_signal_scenario()['sources']
    invoke(case, monkeypatch, capsys, 'synthetic-measured-replay', payload)
    before = run.load_collection('synthetic-measured-replay', str(case['archive']))
    code, result = invoke(case, monkeypatch, capsys, 'synthetic-measured-replay',
                          {} if replay == 'empty' else payload)
    after = run.load_collection('synthetic-measured-replay', str(case['archive']))
    assert code == 0 and result['rotation']['status'] == 'replayed'
    for key in ('signals_collected', 'signal_keys', 'pulls_observed', 'sources_invoked', 'sources_failed'):
        assert after[key] == before[key]
    assert result['pulls_written'] == 0 and len(result['signals']) == 3


def test_source_recovery_removes_only_recovered_failure(case, monkeypatch, capsys):
    payload = fixtures.rotation_signal_scenario()['sources']
    first = copy.deepcopy(payload)
    first['roster_responses']['synth01'] = {'error': 'synthetic temporary failure'}
    invoke(case, monkeypatch, capsys, 'synthetic-recovered', first)
    code, result = invoke(case, monkeypatch, capsys, 'synthetic-recovered',
                          {'roster_responses': {'synth01': payload['roster_responses']['synth01']}})
    coll = run.load_collection('synthetic-recovered', str(case['archive']))
    assert code == 0 and not result['sources_failed']
    assert coll['signals_collected'] == 3 and coll['pulls_observed'] == 3
    assert coll['sources_failed'] == []


def test_failed_pull_persistence_recovers_saved_collection_without_refetch(case, monkeypatch, capsys):
    append = run._append_jsonl
    def fail(path, records):
        if Path(path).name.startswith('pulls-'):
            raise OSError('synthetic denominator failure')
        return append(path, records)
    monkeypatch.setattr(run, '_append_jsonl', fail)
    code, _ = invoke(case, monkeypatch, capsys, 'synthetic-durable-collection',
                      fixtures.rotation_signal_scenario()['sources'])
    assert code == 1 and current(case).get('rotation_cursor', 0) == 0
    monkeypatch.setattr(run, '_append_jsonl', append)
    code, result = invoke(case, monkeypatch, capsys, 'synthetic-durable-collection', {})
    assert code == 0 and result['rotation']['consumed'] == 3
    assert result['pulls_written'] == 3 and len(result['signals']) == 3


def test_failed_collection_persistence_cannot_commit_denominator(case, monkeypatch, capsys):
    append = run._append_jsonl
    def fail(path, records):
        if Path(path).name.startswith('collection-'):
            raise OSError('synthetic collection failure')
        return append(path, records)
    monkeypatch.setattr(run, '_append_jsonl', fail)
    code, _ = invoke(case, monkeypatch, capsys, 'synthetic-no-collection',
                      fixtures.rotation_signal_scenario()['sources'])
    assert code == 1 and current(case).get('rotation_cursor', 0) == 0
    assert not run._ledger_identities(case['archive'], 'pulls-')


def test_collection_run_ids_remain_separate(case, monkeypatch, capsys):
    payload = fixtures.rotation_signal_scenario()['sources']
    invoke(case, monkeypatch, capsys, 'synthetic-isolated-first', payload)
    invoke(case, monkeypatch, capsys, 'synthetic-isolated-next', {})
    assert run.load_collection('synthetic-isolated-first', str(case['archive']))['signals_collected'] == 3
    assert run.load_collection('synthetic-isolated-next', str(case['archive']))['signals_collected'] == 0


def test_successful_pull_keeps_its_first_signal_snapshot(case, monkeypatch, capsys):
    payload = fixtures.rotation_signal_scenario()['sources']
    _, first = invoke(case, monkeypatch, capsys, 'synthetic-first-observation', payload)
    changed = copy.deepcopy(payload)
    changed['roster_responses']['synth00']['tweets'][0]['url'] = 'https://example.com/later-observation'
    _, replay = invoke(case, monkeypatch, capsys, 'synthetic-first-observation', changed)
    assert replay['signals'] == first['signals']
    assert replay['pulls_written'] == 0


def test_legacy_attempt_summaries_retain_earlier_signal_keys(case):
    sample = fixtures.rotation_signal_scenario()['sources']['roster_responses']
    for handle in ('synth00', 'synth01'):
        out = run.collect_sources(roster=case['sample']['roster'], cfg=case['cfg'],
                                  roster_responses={handle: sample[handle]}, run_id='synthetic-legacy')
        coll = run.build_collection_record(out, cfg=case['cfg'])
        for field in ('signals', 'pull_records', 'source_lanes'):
            coll.pop(field)
        coll['schema_version'] = 1
        run.append_collection(coll, str(case['archive']))
    coll = run.load_collection('synthetic-legacy', str(case['archive']))
    assert coll['signals_collected'] == 2 and len(coll['signal_keys']) == 2
    assert 'pulls_observed' in coll['collection_unmeasured']
    assert 'sources_invoked' in coll['collection_unmeasured']


def test_unreadable_collection_history_prevents_further_consumption(case, monkeypatch, capsys):
    path = run.collection_log_path(str(case['archive']))
    path.parent.mkdir(parents=True)
    before = b'{"run_id":"synthetic-invalid"\n'
    path.write_bytes(before)
    code, result = invoke(case, monkeypatch, capsys, 'synthetic-invalid',
                          fixtures.rotation_signal_scenario()['sources'])
    assert code == 1 and 'invalid JSON' in result['detail']
    assert path.read_bytes() == before
    assert not run._ledger_identities(case['archive'], 'pulls-')


def collection_rows(case, *, shared=True, legacy=(False, False)):
    sample = (fixtures.rotation_shared_signal_scenario() if shared
              else fixtures.rotation_signal_scenario(2))
    rows = []
    for index, (handle, response) in enumerate(sample['sources']['roster_responses'].items()):
        out = run.collect_sources(roster=sample['roster'], cfg=case['cfg'],
                                  roster_responses={handle: response}, run_id='synthetic-metadata')
        row = run.build_collection_record(out, cfg=case['cfg'])
        if legacy[index]:
            for field in ('signals', 'pull_records', 'source_lanes'):
                row.pop(field)
            row['schema_version'] = 1
        rows.append(row)
    return rows


@pytest.mark.parametrize('legacy,shared,count,uncertain', [
    ((False, False), True, 2, False),
    ((True, False), True, 1, True),
    ((False, True), True, 1, True),
    ((True, True), True, 1, True),
    ((True, True), False, 2, False),
    ((False, False), False, 2, False),
])
def test_legacy_metadata_precision_reaches_coverage(case, legacy, shared, count, uncertain):
    for row in collection_rows(case, shared=shared, legacy=legacy):
        run.append_collection(row, str(case['archive']))
    merged = run.load_collection('synthetic-metadata', str(case['archive']))
    coverage = run.build_coverage([], [], [], [], [], {}, [], merged)
    assert merged['signals_collected'] == count
    assert coverage['signals_unaccounted'] == count
    for field in ('signals_collected', 'signals_unaccounted'):
        assert (field in merged.get('collection_unmeasured', [])) is uncertain
        assert (field in coverage['unmeasured']) is uncertain


def test_legacy_metadata_replay_does_not_invent_precision_or_multiplicity(case):
    row = collection_rows(case, legacy=(True, True))[0]
    merged = run.merge_collection_records(row, copy.deepcopy(row))
    assert merged['signals_collected'] == 1
    assert merged['signal_keys'] == row['signal_keys']
    assert {'signals_collected', 'signals_unaccounted'} <= set(merged['collection_unmeasured'])


def test_legacy_metadata_uncertainty_survives_current_and_empty_continuation(case):
    first, second = collection_rows(case, legacy=(True, True))
    merged = run.merge_collection_records(first, second)
    complete = collection_rows(case)[0]
    empty = run.build_collection_record({'signals': [], 'pulls': []}, cfg=case['cfg'],
                                        run_id=first['run_id'])
    for continuation in (complete, empty, copy.deepcopy(merged)):
        merged = run.merge_collection_records(merged, continuation)
        coverage = run.build_coverage([], [], [], [], [], {}, [], merged)
        assert merged['signals_collected'] == 1
        assert {'signals_collected', 'signals_unaccounted'} <= set(coverage['unmeasured'])


def test_legacy_metadata_count_only_folds_retain_lower_bound(case):
    first, second = collection_rows(case, legacy=(True, True))
    first['signal_keys'] = []
    second['signal_keys'] = []
    second['signals_collected'] = 2
    merged = run.merge_collection_records(first, second)
    assert merged['signals_collected'] == 2
    coverage = run.build_coverage([], [], [], [], [], {}, [], merged)
    assert {'signals_collected', 'signals_unaccounted'} <= set(coverage['unmeasured'])


def test_legacy_metadata_empty_continuation_preserves_supported_exact_count(case):
    first = collection_rows(case, legacy=(True, True))[0]
    empty = run.build_collection_record({'signals': [], 'pulls': []}, cfg=case['cfg'],
                                        run_id=first['run_id'])
    merged = run.merge_collection_records(first, empty)
    assert merged['signals_collected'] == 1
    coverage = run.build_coverage([], [], [], [], [], {}, [], merged)
    assert not {'signals_collected', 'signals_unaccounted'} & set(coverage['unmeasured'])


def test_pending_old_batch_cannot_advance_after_another_run(case,monkeypatch,capsys):
    invoke(case,monkeypatch,capsys,'synthetic-pending',{'roster_responses':{}})
    invoke(case,monkeypatch,capsys,'synthetic-complete')
    before=case['roster'].read_bytes()
    code,result=invoke(case,monkeypatch,capsys,'synthetic-pending')
    assert code == 1 and 'position changed' in result['detail']
    assert case['roster'].read_bytes()==before


def test_roster_edits_are_preserved_by_next_successful_batch(case,monkeypatch,capsys):
    invoke(case,monkeypatch,capsys,'synthetic-before-edit')
    changed=current(case)
    extra=fixtures.rotation_scenario(8)['roster']['entries'][-1]
    changed['entries']=[extra,*reversed(changed['entries'][1:])]
    changed['notes']='Synthetic operator annotation'
    case['roster'].write_text(json.dumps(changed),encoding='utf-8')
    payload=fixtures.rotation_scenario(8)['sources']
    selected=set()
    for index in range(3):
        code,result=invoke(case,monkeypatch,capsys,f'synthetic-after-edit-{index}',payload)
        assert code == 0
        selected.update(result['rotation']['selected'])
    assert selected=={row['handle'] for row in changed['entries']}
    assert current(case)['notes']==changed['notes']


def test_duplicate_roster_identity_rejects_without_receipts(case,monkeypatch,capsys):
    value=current(case)
    value['entries'][1]['handle']=value['entries'][0]['handle'].upper()
    case['roster'].write_text(json.dumps(value),encoding='utf-8')
    code,result=invoke(case,monkeypatch,capsys,'synthetic-duplicate')
    assert code == 1 and 'duplicate handle' in result['detail']
    assert not list(case['archive'].glob('pulls-*.jsonl'))


@pytest.mark.parametrize('visibility',['false','null',''])
@pytest.mark.parametrize('writer',['roster','pulls','rotation'])
def test_each_write_target_requires_private_proof(case,monkeypatch,visibility,writer):
    def refused(selected):
        raise RuntimeError('synthetic PUBLIC or unknown proof')
    monkeypatch.setattr(private_storage, '_prove_repository', refused)
    target=case['root']/'new-output.json'
    with pytest.raises(RuntimeError,match='PUBLIC|unknown'):
        if writer=='roster':
            roster.save_roster(case['sample']['roster'],str(target))
        elif writer=='pulls':
            run._append_jsonl(target,[{'run_id':'synthetic','handle':'synth00','pulled':0}])
        else:
            source_rotation.atomic_json(target,{'run_id':'synthetic'})
    assert not target.exists()


def test_junction_to_tool_repo_is_rejected(case,monkeypatch):
    tool_root=case['root']/'synthetic-tool'
    tool_root.mkdir()
    guard=roster._datadir()
    monkeypatch.setattr(guard,'_own_repo_root',lambda: str(tool_root))
    monkeypatch.setattr(roster,'_datadir',lambda: guard)
    monkeypatch.setattr(private_storage,'ROOT',tool_root)
    alias=case['root']/'source-alias'
    if os.name=='nt':
        import _winapi
        _winapi.CreateJunction(str(tool_root),str(alias))
    else:
        alias.symlink_to(tool_root,target_is_directory=True)
    try:
        with pytest.raises(roster._datadir().DataDirInsideOwnRepo):
            roster.find_roster_path(str(alias/'roster.json'))
        with pytest.raises(RuntimeError,match='separate PRIVATE'):
            private_storage.prove(alias/'source-rotation.json')
    finally:
        alias.rmdir() if os.name=='nt' else alias.unlink()


def test_nested_public_destination_is_checked_independently(case,monkeypatch):
    nested=case['archive']/'source-rotation'
    nested.mkdir(parents=True)
    def refused(selected):
        raise RuntimeError('synthetic PUBLIC or unknown proof')
    monkeypatch.setattr(private_storage, '_prove_repository', refused)
    with pytest.raises(RuntimeError,match='PUBLIC'):
        source_rotation.atomic_json(nested/'batch.json',{'run_id':'synthetic'})
    assert not (nested/'batch.json').exists()


def test_receipt_ledger_repairs_only_a_complete_missing_newline(case):
    target=case['archive']/'pulls-2031-01.jsonl'
    target.parent.mkdir(parents=True)
    first={'run_id':'synthetic-first','handle':'synth00','pulled':0}
    second={'run_id':'synthetic-second','handle':'synth01','pulled':0}
    target.write_text(json.dumps(first),encoding='utf-8')
    run._append_jsonl(target,[second])
    assert [json.loads(line) for line in target.read_text(encoding='utf-8').splitlines()]==[first,second]
    corrupt=b'{"run_id":"synthetic-incomplete"'
    target.write_bytes(corrupt)
    with pytest.raises(ValueError,match='incomplete tail'):
        run._append_jsonl(target,[second])
    assert target.read_bytes()==corrupt


def test_failed_atomic_receipt_promotion_preserves_old_bytes(case,monkeypatch):
    target=case['archive']/'pulls-2031-01.jsonl'
    target.parent.mkdir(parents=True)
    before=b'{"run_id":"synthetic-first","handle":"synth00","pulled":0}\n'
    target.write_bytes(before)
    replace=os.replace
    def deny(source,destination):
        if Path(destination)==target:
            raise OSError('synthetic replacement failure')
        return replace(source,destination)
    monkeypatch.setattr(os,'replace',deny)
    with pytest.raises(OSError,match='replacement'):
        run._append_jsonl(target,[{'run_id':'synthetic-second','handle':'synth01','pulled':0}])
    assert target.read_bytes()==before
    assert not list(case['root'].glob('*.partial'))


@pytest.mark.parametrize('mutation',['nonce','hash','run-id','previous-digest','schema-bool','ready-int'])
def test_finalizer_refuses_mismatched_current_artifacts(case,monkeypatch,mutation):
    root = case['root']
    blob = json.dumps(case['sample']['candidates']).encode()
    (root/'candidates.json').write_bytes(blob)
    nonce = case['sample']['nonce']
    proof = dict(schema_version=1,run_id='synthetic-finalize',nonce=nonce,ready=True,
                 candidate_sha256=hashlib.sha256(blob).hexdigest())
    if mutation == 'previous-digest':
        (root/'old-digest.md').write_text('Synthetic earlier digest',encoding='utf-8')
    else:
        if mutation == 'schema-bool':
            proof['schema_version'] = True
        elif mutation == 'ready-int':
            proof['ready'] = 1
        else:
            proof[{'nonce':'nonce','hash':'candidate_sha256','run-id':'run_id'}[mutation]] = 'wrong'
        (root/'candidate-ready.json').write_text(json.dumps(proof),encoding='utf-8')
    monkeypatch.setattr(subprocess,'run',lambda *a,**k: pytest.fail('invalid handoff reached driver'))
    assert finalizer.main(['--run-dir',str(root),'--run-id','synthetic-finalize','--nonce',nonce]) == 4


def test_finalizer_valid_current_handoff_is_executed_once(case,monkeypatch):
    root = case['archive'] / 'workspaces' / 'synthetic-finalize'
    root.mkdir(parents=True)
    blob = json.dumps(case['sample']['candidates']).encode()
    (root/'candidates.json').write_bytes(blob)
    nonce = case['sample']['nonce']
    proof = dict(schema_version=1,run_id='synthetic-finalize',nonce=nonce,ready=True,
                 candidate_sha256=hashlib.sha256(blob).hexdigest())
    (root/'candidate-ready.json').write_text(json.dumps(proof),encoding='utf-8')
    calls=[]
    def execute(argv,**kwargs):
        calls.append(argv)
        assert Path(argv[argv.index('--in')+1]).read_bytes() == blob
        return subprocess.CompletedProcess(argv,0)
    monkeypatch.setattr(subprocess,'run',execute)
    args=['--run-dir',str(root),'--run-id','synthetic-finalize','--nonce',nonce]
    assert finalizer.main(args) == 0
    assert finalizer.main(args) == 4
    assert len(calls) == 1
