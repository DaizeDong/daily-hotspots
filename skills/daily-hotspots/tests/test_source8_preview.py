"""A fresh installation can render a CLI preview without inventing DATA storage."""
from pathlib import Path
import subprocess
import sys

import archive
import run


def test_actual_cli_preview_without_any_discovery_source(tmp_path):
    harness = Path(__file__).with_name('preview-cli-harness.py')
    result = subprocess.run([sys.executable, '-I', '-S', '-X', 'utf8', '-B', str(harness)],
                            input='[]', capture_output=True, text=True, encoding='utf-8',
                            cwd=tmp_path, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '"errors": []' in result.stdout
    assert '"watermark_advanced": false' in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_uninitialized_collection_read_returns_unmeasured(monkeypatch):
    monkeypatch.setattr(archive._datadir(), '_candidates', lambda skill: [])
    assert run.load_collection('synthetic-preview') is None
