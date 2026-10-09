"""The scheduled transport tells the agent where its procedure is and keeps what the agent said.

Both run the wrapper's real text: the prompt is sliced from the `$prompt =` assignment and the shim
is the exact here-string the wrapper writes, executed against a synthetic `llmcall` package.
"""
import os
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
        "def call(prompt, mode=None):\n"
        "    assert mode == 'agent'\n"
        "    return Result(%r)\n" % ok, encoding="utf-8")
    shim_dir = tmp_path / "transport"
    shim_dir.mkdir()
    shim = shim_dir / "dh_llmcall_agent.py"
    shim.write_text(_shim_source(), encoding="utf-8")
    prompt = shim_dir / "prompt.txt"
    prompt.write_text("synthetic prompt", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path / "site")}
    result = subprocess.run([sys.executable, "-B", str(shim), str(prompt)], capture_output=True,
                            text=True, env=env, timeout=60)
    assert result.returncode == (0 if ok else 1), result.stderr
    assert (shim_dir / "reply.txt").read_text(encoding="utf-8") == expected
