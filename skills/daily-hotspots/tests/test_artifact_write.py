"""Exercise real source-contract ownership at production atomic write seams."""
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


def generated():
    spec = importlib.util.spec_from_file_location("artifact_fixture_builder", ROOT / "tools/make_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.artifact_write_scenario()


@pytest.mark.parametrize("state", ["declared", "undeclared", "ignored"])
def test_atomic_writer_enforces_source_contract(tmp_path, monkeypatch, state):
    sample = generated()
    target = tmp_path / (sample["undeclared"] if state == "undeclared" else sample["allowed"])
    proof = SimpleNamespace(root=str(tmp_path), repositories=("example/synthetic-config",), signature="synthetic-proof")
    def read(snapshot, *args):
        ignored = state == "ignored" and args[0] == "check-ignore" and args[-1] == sample["allowed"]
        return SimpleNamespace(returncode=(0 if ignored else 1) if args[0] == "check-ignore" else 0, stdout="synthetic-head")
    boundary = SimpleNamespace(prove_private_companion=lambda *a: proof,
                               read_private_companion_git=read, GitError=RuntimeError)
    import private_storage as storage
    import source_rotation
    monkeypatch.setattr(storage, "prove", lambda path, **kwargs: Path(path))
    monkeypatch.setattr(storage, "_repository_root", lambda path: tmp_path)
    monkeypatch.setattr(storage._storage_contract(), "load_boundary", lambda: boundary)
    before = set(tmp_path.rglob("*"))
    if state == "declared":
        source_rotation.atomic_json(target, sample["content"])
        assert target.is_file()
        assert json.loads(target.read_text(encoding="utf-8")) == sample["content"]
    else:
        with pytest.raises((RuntimeError, ValueError)):
            source_rotation.atomic_json(target, sample["content"])
        assert not target.exists()
        assert set(tmp_path.rglob("*")) == before


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


def test_finalization_claim_refuses_undeclared_private_leaf(tmp_path):
    import finalize_handoff
    target = tmp_path / "unowned"
    with pytest.raises(RuntimeError, match="artifact write refused"):
        finalize_handoff.claim_run(tmp_path, "synthetic-finalization", "a" * 32,
                                   json.dumps(generated()["content"]).encode(), claim_dir=target)
    assert not target.exists()
