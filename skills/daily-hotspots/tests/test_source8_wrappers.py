"""Native production logging and completeness branches with external effects replaced."""
import json
from pathlib import Path
import subprocess
import shutil

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def invoke(case, tmp_path):
    shell = shutil.which('powershell.exe') or shutil.which('pwsh')
    if shell is None:
        pytest.skip('PowerShell is unavailable; native Windows CI retains this coverage')
    return subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File',
                           str(Path(__file__).with_name('wrapper-storage-harness.ps1')),
                           str(SCRIPTS), case, str(tmp_path)],
                          capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)


@pytest.mark.parametrize('case', ['log-default', 'log-explicit'])
def test_log_opens_only_the_proved_private_path(tmp_path, case):
    result = invoke(case, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt['proof_calls'] == 1
    assert Path(receipt['path']) == tmp_path / 'private/archive/logs/synthetic.log'
    assert 'Synthetic child output retained' in Path(receipt['path']).read_text(encoding='utf-8')
    assert not (tmp_path / 'requested').exists()


@pytest.mark.parametrize('case', ['log-refused', 'log-empty'])
def test_log_proof_failure_stops_without_any_fallback_file(tmp_path, case):
    result = invoke(case, tmp_path)
    assert result.returncode == 1
    assert 'CONSOLE FAILURE' in result.stdout
    assert 'PROOF CALLS: 1' in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('code', [0, 1, 2, 4, 9])
def test_completeness_reports_into_archive_and_distinguishes_failures(tmp_path, code):
    result = invoke('completeness-' + str(code), tmp_path)
    assert result.returncode == code, result.stderr
    call = next(line[5:] for line in result.stdout.splitlines() if line.startswith('CALL '))
    arguments = json.loads(call)
    report = Path(arguments[arguments.index('--report') + 1])
    assert report == tmp_path / 'private/archive/completeness.json'
    assert ('HOLES' in result.stdout) is (code == 1)
    if code == 4:
        assert 'REPORT FAILURE' in result.stdout
    if code == 9:
        assert 'UNEXPECTED FAILURE' in result.stdout


@pytest.mark.parametrize('case', ['publication-success', 'publication-refused', 'publication-malformed'])
def test_publication_preflight_precedes_runtime_workspace_creation(tmp_path, case):
    result = invoke(case, tmp_path)
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt['proof_calls'] == 1
    success = case == 'publication-success'
    assert result.returncode == (0 if success else 1)
    assert receipt['workspace_calls'] == (1 if success else 0)
    if success:
        assert receipt['target']['refspec'] == 'HEAD:refs/heads/daily/archive'
