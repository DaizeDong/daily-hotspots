"""Initialization and doctor must establish PRIVATE storage and runtime readiness."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import private_storage
from test_source7_repairs import fixtures

ROOT = Path(__file__).resolve().parents[3]


def load_script(name):
    spec = importlib.util.spec_from_file_location('setup_'+name, ROOT/'scripts'/ (name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('visibility', ['false', 'null', ''])
def test_initializer_refuses_unproved_private_target(monkeypatch, tmp_path, visibility):
    module = load_script('init_config')
    monkeypatch.setattr(module, 'ROSTER', fixtures.roster_example())
    monkeypatch.setattr(sys, 'argv', ['init_config.py', '--out', str(tmp_path/'uncreated')])
    def refused(selected):
        raise RuntimeError('synthetic PUBLIC or unknown proof')
    monkeypatch.setattr(private_storage, '_prove_repository', refused)
    with pytest.raises(RuntimeError):
        module.main()
    assert not (tmp_path/'uncreated').exists()


def test_initializer_admits_verified_private_target(monkeypatch, tmp_path):
    module = load_script('init_config')
    monkeypatch.setattr(module, 'ROSTER', fixtures.roster_example())
    monkeypatch.setattr(sys, 'argv', ['init_config.py', '--out', str(tmp_path/'config')])
    assert module.main() == 0
    assert (tmp_path/'config/roster.json').is_file()


@pytest.mark.parametrize('missing_loader', [False, True])
def test_doctor_rejects_unproved_storage_or_unavailable_loader(monkeypatch, tmp_path, capsys, missing_loader):
    initializer = load_script('init_config')
    monkeypatch.setattr(initializer, 'ROSTER', fixtures.roster_example())
    monkeypatch.setattr(sys, 'argv', ['init_config.py', '--out', str(tmp_path/'config')])
    initializer.main()
    module = load_script('verify_config')
    monkeypatch.setattr(module, 'check_dependency_skills', lambda: [])
    if missing_loader:
        monkeypatch.setattr(module, 'lib', None)
    else:
        def refused(selected):
            raise RuntimeError('synthetic PUBLIC proof')
        monkeypatch.setattr(private_storage, '_prove_repository', refused)
    monkeypatch.setattr(sys, 'argv', ['verify_config.py', '--config-dir', str(tmp_path/'config')])
    capsys.readouterr()
    assert module.main() != 0
    output = capsys.readouterr().out
    assert 'NOT READY' in output
    assert '[FAIL] runtime loader available' in output if missing_loader else '[FAIL] PRIVATE companion verified' in output


def test_disposable_installation_from_an_unrelated_directory(tmp_path, monkeypatch, synthetic_cli_companion):
    installed = tmp_path / 'installed'
    installed.mkdir()
    alias = installed / 'daily-hotspots'
    target = (ROOT / 'skills/daily-hotspots').resolve()
    assert target.is_relative_to(ROOT.resolve())
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(alias))
    else:
        alias.symlink_to(target, target_is_directory=True)
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    try:
        preview = subprocess.run([sys.executable, str(alias / 'scripts/run.py'), '--dry-run', '--no-ledger'],
                                 input='[]', capture_output=True, text=True, encoding='utf-8',
                                 cwd=unrelated, timeout=30)
        assert preview.returncode == 0, preview.stderr
        assert json.loads(preview.stdout)['watermark_advanced'] is False
        assert list(unrelated.iterdir()) == []

        config = tmp_path / 'config'
        command = synthetic_cli_companion(ROOT / 'scripts/init_config.py') + ['--out', str(config)]
        initialized = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                     cwd=unrelated, timeout=30)
        assert initialized.returncode == 0, initialized.stderr
        assert json.loads((config / 'roster.json').read_text()) == {'schema_version': 1, 'entries': []}
        policy = json.dumps(fixtures.candidate_contract_scenario()['policy']).encode('utf-8')
        (config / 'watchlist.json').write_bytes(policy)
        repeated = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                  cwd=unrelated, timeout=30)
        assert repeated.returncode == 0, repeated.stderr
        assert (config / 'watchlist.json').read_bytes() == policy

        monkeypatch.setenv('DAILY_HOTSPOTS_SKILLS_DIR', str(installed))
        doctor = subprocess.run(synthetic_cli_companion(ROOT / 'scripts/verify_config.py') +
                                ['--config-dir', str(config)], capture_output=True, text=True,
                                encoding='utf-8', cwd=unrelated, timeout=30)
        assert doctor.returncode == 1
        assert '[PASS] PRIVATE companion verified' in doctor.stdout
        assert '[FAIL] dependency skill reachable: schedule-reminder' in doctor.stdout
        assert 'NOT READY' in doctor.stdout
    finally:
        alias.rmdir() if os.name == 'nt' else alias.unlink()
