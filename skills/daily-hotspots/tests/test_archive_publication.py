"""Execute the selected production publication branch with only synthetic effect stubs."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

TESTS = Path(__file__).resolve().parent
WRAPPER = TESTS.parent / 'scripts/wrapper.ps1'
CASES = ['success', 'healthy-noop', 'failed-run', 'config-missing', 'not-repo', 'no-git',
         'unverified-noop'] + [stage+'-'+fault for stage in ['add', 'diff', 'staged', 'commit', 'pull', 'push']
                              for fault in ['failure', 'null', 'throw']] + ['diff-negative']


@pytest.mark.parametrize('case', CASES)
def test_archive_publication_controls_native_exit(case):
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if shell is None:
        pytest.skip('PowerShell is required for the wrapper execution contract')
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File',
                             str(TESTS/'archive-publication-harness.ps1'), str(WRAPPER), case],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt['case'] == case
    expected = 0 if case in ['success', 'healthy-noop'] else 9 if case == 'failed-run' else 1
    assert result.returncode == receipt['rc'] == expected
    if case.startswith(('add-', 'diff-', 'staged-', 'commit-', 'pull-')):
        assert 'git push' not in receipt['calls']
    if case in ['config-missing', 'not-repo', 'no-git']:
        assert receipt['calls'] == []

    if case == 'failed-run':
        assert 'git commit' in receipt['calls'] and 'git push' in receipt['calls']
        assert 'failed run evidence' in ' '.join(receipt['arguments']['git commit'])

    if case == 'success':
        assert receipt['arguments']['git pull --rebase'] == ['pull', '--rebase', '--autostash', 'backup', 'daily/archive']
        assert receipt['arguments']['git push'] == ['push', 'backup', 'HEAD:refs/heads/daily/archive']
