"""Exercise the path-only companion checks at production atomic write seams."""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def generated():
    spec = importlib.util.spec_from_file_location("artifact_fixture_builder", ROOT / "tools/make_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.artifact_write_scenario()


def test_atomic_writer_writes_inside_companion(tmp_path, monkeypatch):
    sample = generated()
    target = tmp_path / sample["allowed"]
    import private_storage as storage
    import source_rotation
    monkeypatch.setattr(storage, "_repository_root", lambda path: tmp_path)
    source_rotation.atomic_json(target, sample["content"])
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == sample["content"]


def test_atomic_writer_refuses_own_source_tree(monkeypatch):
    sample = generated()
    import private_storage as storage
    import source_rotation
    target = storage.ROOT / "synthetic-refused" / "state.json"
    with pytest.raises(RuntimeError, match="separate PRIVATE companion"):
        source_rotation.atomic_json(target, sample["content"])
    assert not target.parent.exists()


def test_log_path_cli_resolves_declared_private_target(synthetic_cli_companion, tmp_path):
    script = ROOT / "skills/daily-hotspots/scripts/private_storage.py"
    name = generated()["log_name"]
    directory = tmp_path / "archive/logs"
    result = subprocess.run(synthetic_cli_companion(script) + [
        "log-path", "--log-dir", str(directory), "--name", name],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == directory / name
    assert not directory.exists()
