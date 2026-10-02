"""Initialization and doctor must establish PRIVATE storage and runtime readiness."""
import importlib.util
from pathlib import Path
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
    monkeypatch.setattr(private_storage, '_run', lambda argv:
                        str(tmp_path) if '--show-toplevel' in argv else
                        fixtures.private_storage_path_scenario()['origin'] if argv[0] == 'git' else visibility)
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
        monkeypatch.setattr(private_storage, '_run', lambda argv:
                            str(tmp_path) if '--show-toplevel' in argv else
                            fixtures.private_storage_path_scenario()['origin'] if argv[0] == 'git' else 'false')
    monkeypatch.setattr(sys, 'argv', ['verify_config.py', '--config-dir', str(tmp_path/'config')])
    capsys.readouterr()
    assert module.main() != 0
    output = capsys.readouterr().out
    assert 'NOT READY' in output
    assert '[FAIL] runtime loader available' in output if missing_loader else '[FAIL] PRIVATE companion verified' in output
