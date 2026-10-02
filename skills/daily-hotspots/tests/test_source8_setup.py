"""Setup and doctor use the same runtime roster location as collection."""
import json
import os
import sys

import pytest
import roster
from test_setup_contract import load_script
from test_source7_repairs import fixtures


@pytest.mark.parametrize('data_layout', [False, True])
def test_initializer_uses_the_effective_runtime_roster_path(monkeypatch, tmp_path, data_layout):
    config = tmp_path / 'config'
    runtime = config / 'data' if data_layout else config
    runtime.mkdir(parents=True)
    initializer = load_script('init_config')
    monkeypatch.setattr(initializer, 'ROSTER', fixtures.roster_example())
    monkeypatch.setattr(sys, 'argv', ['init_config.py', '--out', str(config)])
    original_env = os.environ.get('DAILY_HOTSPOTS_CONFIG')
    assert initializer.main() == 0
    assert os.environ.get('DAILY_HOTSPOTS_CONFIG') == original_env
    assert (runtime / 'roster.json').is_file()
    assert (config / 'watchlist.json').is_file()
    if data_layout:
        assert not (config / 'roster.json').exists()
    monkeypatch.setenv('DAILY_HOTSPOTS_CONFIG', str(config))
    assert roster.resolve_roster_path() == runtime / 'roster.json'


def test_existing_data_roster_is_preserved_without_seeding_a_shadow(monkeypatch, tmp_path):
    config = tmp_path / 'config'
    (config / 'data').mkdir(parents=True)
    selected = config / 'data/roster.json'
    content = json.dumps(fixtures.roster_example()).encode('utf-8')
    selected.write_bytes(content)
    initializer = load_script('init_config')
    monkeypatch.setattr(initializer, 'ROSTER', {'schema_version': 1, 'entries': []})
    monkeypatch.setattr(sys, 'argv', ['init_config.py', '--out', str(config)])
    assert initializer.main() == 0
    assert selected.read_bytes() == content
    assert not (config / 'roster.json').exists()


@pytest.mark.parametrize('runtime_roster', [None, 'malformed'])
def test_doctor_checks_data_roster_even_when_root_roster_is_valid(monkeypatch, tmp_path, capsys, runtime_roster):
    config = tmp_path / 'config'
    initializer = load_script('init_config')
    monkeypatch.setattr(initializer, 'ROSTER', fixtures.roster_example())
    monkeypatch.setattr(sys, 'argv', ['init_config.py', '--out', str(config)])
    initializer.main()
    (config / 'data').mkdir()
    if runtime_roster:
        (config / 'data/roster.json').write_text(runtime_roster, encoding='utf-8')
    doctor = load_script('verify_config')
    monkeypatch.setattr(doctor, 'check_dependency_skills', lambda: [])
    monkeypatch.setattr(sys, 'argv', ['verify_config.py', '--config-dir', str(config)])
    capsys.readouterr()
    assert doctor.main() == 1
    assert '[FAIL] roster.json' in capsys.readouterr().out


def test_initial_live_roster_has_no_fictional_or_observed_accounts(monkeypatch, tmp_path):
    config = tmp_path / 'config'
    initializer = load_script('init_config')
    monkeypatch.setattr(sys, 'argv', ['init_config.py', '--out', str(config)])
    assert initializer.main() == 0
    assert json.loads((config / 'roster.json').read_text(encoding='utf-8')) == {
        'schema_version': 1, 'entries': []}
