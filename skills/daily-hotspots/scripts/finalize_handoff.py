"""Validate the current agent handoff before local ledger and delivery work."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


def validate_handoff(run_dir: Path, run_id: str, nonce: str) -> bytes:
    if not re.fullmatch(r'[0-9a-f]{32}', nonce):
        raise ValueError('invalid handoff nonce')
    candidates = run_dir / 'candidates.json'
    receipt = run_dir / 'candidate-ready.json'
    if candidates.stat().st_size > 20_000_000 or receipt.stat().st_size > 8192:
        raise ValueError('handoff size limit exceeded')
    data = candidates.read_bytes()
    value = json.loads(data)
    rows = value.get('candidates') if isinstance(value, dict) else value
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError('candidates must be a list of objects')
    proof = json.loads(receipt.read_text(encoding='utf-8-sig'))
    if not isinstance(proof, dict) or proof != {
        'schema_version': 1, 'run_id': run_id, 'nonce': nonce, 'ready': True,
        'candidate_sha256': hashlib.sha256(data).hexdigest(),
    }:
        raise ValueError('missing, stale, or mismatched candidate readiness receipt')
    return data


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--nonce', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        data = validate_handoff(args.run_dir, args.run_id, args.nonce)
        snapshot = args.run_dir / ('finalize-' + args.nonce + '.json')
        # An uncertain prior finalization must be inspected, never replayed.
        with snapshot.open('xb') as stream:
            stream.write(data)
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({'ok': False, 'stage': 'handoff', 'error': str(exc)}))
        return 4
    command = [sys.executable, '-B', str(Path(__file__).with_name('run.py')),
               '--in', str(snapshot), '--run-id', args.run_id]
    if args.dry_run:
        command.append('--dry-run')
    # The existing driver owns validation, dedup, delivery, artifact checks, and exit status.
    return subprocess.run(command, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
