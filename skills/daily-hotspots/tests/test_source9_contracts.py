"""Generated proposals must retain their schema, freshness, policy and storage boundaries."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys

import pytest
import lib
import private_storage
import run
import score
import verify_gate

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('source9_fixtures', ROOT/'tools/make_fixtures.py')
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)


def scenario():
    return fixtures.candidate_contract_scenario()


def test_valid_generated_candidate_still_clears_the_gate():
    card = run.build_card(scenario()['candidate'], copy.deepcopy(lib.DEFAULT_CONFIG), 'synthetic-run')
    assert verify_gate.gate_batch([card], copy.deepcopy(lib.DEFAULT_CONFIG))['pushable'] == [card]


@pytest.mark.parametrize('field', scenario()['missing_prose'])
def test_raw_missing_prose_cannot_be_defaulted_into_a_card(field):
    candidate = scenario()['candidate']
    candidate.pop(field)
    with pytest.raises(ValueError, match=field):
        run.build_card(candidate, copy.deepcopy(lib.DEFAULT_CONFIG), 'synthetic-run')


@pytest.mark.parametrize('field', score._DIMS)
def test_aggregation_preserves_missing_dimension_error(field):
    candidate = scenario()['candidate']
    candidate['score_breakdown'].pop(field)
    with pytest.raises(score.ScoreInputError, match=field):
        run.build_card(candidate, copy.deepcopy(lib.DEFAULT_CONFIG), 'synthetic-run')


@pytest.mark.parametrize('mutation', ['unknown-track', 'missing-pain', 'invalid-side'])
def test_raw_enum_and_demand_requirements_hold_the_run(mutation):
    candidate = scenario()['candidate']
    if mutation == 'unknown-track':
        candidate['track'] = scenario()['unknown_track']
    elif mutation == 'missing-pain':
        candidate['side'] = 'demand'
    else:
        candidate['side'] = 'unexpected'
    result = run.process([candidate], copy.deepcopy(lib.DEFAULT_CONFIG), dry_run=True, collection={})
    assert result.get('held') is True
    assert result['watermark_advanced'] is False
    assert result['pushed'] == result['archived'] == []


@pytest.mark.parametrize('envelope', scenario()['error_envelopes'])
def test_cli_error_envelope_fails_before_ledger_or_pipeline(monkeypatch, capsys, envelope):
    calls = []
    monkeypatch.setattr(sys, 'argv', ['run.py', '--dry-run', '--no-ledger'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(envelope)))
    monkeypatch.setattr(run, 'process', lambda *a, **k: calls.append('process') or {})
    assert run.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result['watermark_advanced'] is False and result.get('error')
    assert calls == []


@pytest.mark.parametrize('envelope', scenario()['error_envelopes'])
def test_direct_process_rejects_malformed_envelopes(envelope):
    result = run.process(envelope, copy.deepcopy(lib.DEFAULT_CONFIG), dry_run=True, collection={})
    assert result.get('held') is True and result['empty_day'] is False
    assert result['watermark_advanced'] is False


@pytest.mark.parametrize('explicit_age', [None, 0])
def test_old_evidence_cannot_claim_current_freshness(explicit_age):
    candidate = scenario()['candidate']
    if explicit_age is None:
        candidate.pop('age_hours')
    else:
        candidate['age_hours'] = explicit_age
    for evidence in candidate['evidence']:
        evidence['ts'] = scenario()['old_timestamp']
    card = run.build_card(candidate, copy.deepcopy(lib.DEFAULT_CONFIG), 'synthetic-run')
    assert card['freshness'] < 0.1
    assert verify_gate.gate_batch([card], copy.deepcopy(lib.DEFAULT_CONFIG))['pushable'] == []


def test_invalid_timestamp_is_rejected_before_scoring():
    candidate = scenario()['candidate']
    candidate['evidence'][0]['ts'] = scenario()['invalid_timestamp']
    with pytest.raises(ValueError, match='timestamp'):
        run.build_card(candidate, copy.deepcopy(lib.DEFAULT_CONFIG), 'synthetic-run')


@pytest.mark.parametrize('target_kind', ['file', 'directory'])
def test_private_proof_refuses_hardlinked_content_before_read(tmp_path, target_kind):
    private = tmp_path/'private'
    private.mkdir()
    target = private/'runtime.txt'
    target.write_text(scenario()['alias_content'], encoding='utf-8')
    os.link(target, tmp_path/'public-alias.txt')
    assert target.stat().st_nlink == 2
    with pytest.raises(RuntimeError, match='hardlink'):
        private_storage.prove(target if target_kind == 'file' else private)


@pytest.mark.parametrize('malformation', ['truncated', 'non-object', 'bad-exclusions', 'bad-scoring'])
def test_selected_malformed_policy_never_falls_back(tmp_path, malformation):
    path = tmp_path/'watchlist.json'
    policy = scenario()['policy']
    path.write_text(json.dumps(policy), encoding='utf-8')
    assert lib.load_config(str(path))['scoring']['min_score_to_push'] == 95
    if malformation == 'truncated':
        raw = json.dumps(policy)[:-1]
    elif malformation == 'non-object':
        raw = '[]'
    else:
        policy['exclude' if malformation == 'bad-exclusions' else 'scoring'] = 'invalid'
        raw = json.dumps(policy)
    path.write_text(raw, encoding='utf-8')
    with pytest.raises((ValueError, RuntimeError), match='config|watchlist'):
        lib.load_config(str(path))


def test_absent_configuration_keeps_documented_uninitialized_read(tmp_path):
    assert lib.load_config(str(tmp_path/'absent.json')) == lib.DEFAULT_CONFIG


def test_transport_workspace_is_exclusive_and_retained_below_the_run(tmp_path, monkeypatch):
    workspace = tmp_path/'private-run'
    workspace.mkdir()
    monkeypatch.setenv('TEMP', str(tmp_path/'untrusted-public-temp'))
    child = private_storage.transport_dir(workspace)
    assert child.parent == workspace and child.is_dir()
    assert list(child.iterdir()) == []
    assert private_storage.transport_dir(workspace) != child
    assert child.exists()


def test_wrapper_transport_uses_the_proved_workspace_and_keeps_evidence():
    source = (ROOT/'skills/daily-hotspots/scripts/wrapper.ps1').read_text(encoding='utf-8-sig')
    assert '"transport-dir", "--run-dir", $script:runDir' in source
    assert 'Join-Path $env:TEMP ("dh-run-"' not in source
    assert 'Remove-Item -LiteralPath $shimDir' not in source
    assert 'os.path.abspath(p) != _here' in source


def test_wrapper_exception_path_also_versions_private_diagnostics():
    source = (ROOT/'skills/daily-hotspots/scripts/wrapper.ps1').read_text(encoding='utf-8-sig')
    assert source.count('Save-PrivateRunEvidence -RunExitCode') >= 2
