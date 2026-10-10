"""The scheduled transport tells the agent where its procedure is and keeps what the agent said.

Both run the wrapper's real text: the prompt is sliced from the `$prompt =` assignment and the shim
is the exact here-string the wrapper writes, executed against a synthetic `llmcall` package. The
parent's source-receipt step is the wrapper's own function, lifted out of its AST and run against
stubs.
"""
import os
import shutil
from pathlib import Path
import re
import subprocess
import sys

import pytest

from test_security import REPO, _wrapper_prompt_block

WRAPPER = REPO / "skills/daily-hotspots/scripts/wrapper.ps1"


def _shim_source():
    src = WRAPPER.read_text(encoding="utf-8")
    match = re.search(r"\$pyCode = @'\r?\n(.*?)\r?\n'@", src, re.S)
    assert match, "the transport shim here-string is no longer locatable in wrapper.ps1"
    return match.group(1)


def test_prompt_names_the_skill_procedure_and_forbids_home_search():
    src = WRAPPER.read_text(encoding="utf-8")
    block = _wrapper_prompt_block(src)
    assert len(block) > 200 and "run.py" in block
    assert "$skillMd" in block, "the prompt must hand the agent the SKILL.md path"
    assert "do not search" in block.lower() and "home directory" in block.lower()
    assert 'Join-Path (Split-Path -Parent $PSScriptRoot) "SKILL.md"' in src
    assert (REPO / "skills/daily-hotspots/SKILL.md").is_file()


@pytest.mark.parametrize("ok, expected", [(True, "synthetic final message"), (False, "synthetic provider failure")])
def test_shim_retains_the_agent_reply_next_to_the_prompt(tmp_path, ok, expected):
    package = tmp_path / "site" / "llmcall"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "class Result:\n"
        "    def __init__(self, ok):\n"
        "        self.ok = ok\n"
        "        self.provider = 'synthetic' if ok else None\n"
        "        self.text = 'synthetic final message' if ok else ''\n"
        "        self.error = None if ok else 'synthetic provider failure'\n"
        "    def __bool__(self):\n"
        "        return self.ok\n"
        "def call(prompt, mode=None, timeout=None):\n"
        "    assert mode == 'agent'\n"
        "    assert timeout == 3600.0, timeout\n"
        "    return Result(%r)\n" % ok, encoding="utf-8")
    shim_dir = tmp_path / "transport"
    shim_dir.mkdir()
    shim = shim_dir / "dh_llmcall_agent.py"
    shim.write_text(_shim_source(), encoding="utf-8")
    prompt = shim_dir / "prompt.txt"
    prompt.write_text("synthetic prompt", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "site")}
    result = subprocess.run([sys.executable, "-B", str(shim), str(prompt), "3600"], capture_output=True,
                            text=True, env=env, timeout=60)
    assert result.returncode == (0 if ok else 1), result.stderr
    assert (shim_dir / "reply.txt").read_text(encoding="utf-8") == expected


def test_wrapper_passes_its_agent_timeout_to_llmcall():
    """llmcall's agent default (1800s) cut the 2026-10-10 collector at 1368s while it was scoring.
    The wrapper hands the shim an explicit chain timeout that is longer than that and below its own
    budget, and the shim forwards exactly that number."""
    src = WRAPPER.read_text(encoding="utf-8")
    agent = int(re.search(r"^\s*\$agentTimeoutSec\s*=\s*(\d+)\s*$", src, re.M).group(1))
    budget = int(re.search(r"^\s*\$budgetSec\s*=\s*(\d+)\s*$", src, re.M).group(1))
    assert 1800 < agent < budget
    assert "@($pyFile, $promptFile, [string]$agentTimeoutSec)" in src
    assert "timeout=timeout" in _shim_source() and "float(sys.argv[2])" in _shim_source()


def test_prompt_keeps_durable_source_writes_out_of_the_sandbox():
    """The agent's sandbox has no git transport, so the PRIVATE proof refuses its durable writes.
    The prompt must send the payload to the run workspace and allow only the --dry-run preview."""
    block = _wrapper_prompt_block(WRAPPER.read_text(encoding="utf-8"))
    assert "'sources.json'" in block and "$script:runDir" in block
    assert "--dry-run" in block and "Always pass --dry-run" in block
    assert "with --sources to record the denominator" not in block


def test_parent_records_sources_after_the_agent_and_before_the_finalizer():
    src = WRAPPER.read_text(encoding="utf-8")
    body = src[src.index("$handoffNonce = "):]
    transport = body.index('-Label "llmcall"')
    receipts = body.index("Save-SourceReceipts -Python")
    finalizer = body.index('-Label "local deterministic finalizer"')
    assert transport < receipts < finalizer
    # not gated on the agent's exit code: the pulls happened either way
    between = body[body.index('daily-hotspots transport end'):receipts]
    assert "if ($rc -eq 0)" not in between


_HARNESS = r"""param([string]$Wrapper, [string]$RunDir, [int]$ChildRc)
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Wrapper,[ref]$tokens,[ref]$errors)
$fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Save-SourceReceipts'},$true)
if (-not $fn) { throw 'the wrapper has no parent-side source receipt step' }
Invoke-Expression $fn.Extent.Text
$global:calls=@(); $global:aborts=@()
function Invoke-ChildToLog { param([string]$Exe, [string[]]$Arguments, [string]$Label) $global:calls += ((@($Exe) + $Arguments) -join '|'); return $ChildRc }
function Write-Log { param($m) }
function Write-Loud { param($m) }
function Notify-Abort { param($m) $global:aborts += $m }
$r = Save-SourceReceipts -Python 'synthetic-python' -RunPy 'synthetic-run.py' -RunDir $RunDir -RunId 'daily-2026-01-02'
[pscustomobject]@{ rc = $r; calls = [string[]]@($global:calls); aborts = $global:aborts.Count } | ConvertTo-Json -Depth 4 -Compress
"""


def _run_receipts(tmp_path, *, payload, child_rc):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is unavailable on this platform")
    import json
    run_dir = tmp_path / "workspace"
    run_dir.mkdir()
    if payload:
        (run_dir / "sources.json").write_text('{"roster_responses": {}}', encoding="utf-8")
    harness = tmp_path / "receipts.ps1"
    harness.write_text(_HARNESS, encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(harness), str(WRAPPER),
                             str(run_dir), str(child_rc)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    calls = out["calls"] or []
    if isinstance(calls, str):
        calls = [calls]
    return run_dir, out["rc"], [call.split("|") for call in calls], out["aborts"]


def test_parent_records_the_agent_payload_durably(tmp_path):
    run_dir, rc, calls, aborts = _run_receipts(tmp_path, payload=True, child_rc=0)
    assert rc == 0 and aborts == 0
    assert calls == [["synthetic-python", "-B", "synthetic-run.py", "--sources", str(run_dir / "sources.json"),
                      "--run-id", "daily-2026-01-02"]]


def test_a_failed_parent_record_alerts(tmp_path):
    _, rc, calls, aborts = _run_receipts(tmp_path, payload=True, child_rc=3)
    assert rc == 3 and len(calls) == 1 and aborts == 1


def test_a_missing_payload_alerts_without_a_call(tmp_path):
    _, rc, calls, aborts = _run_receipts(tmp_path, payload=False, child_rc=0)
    assert rc is None and calls == [] and aborts == 1
