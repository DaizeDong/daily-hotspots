"""Validate the current agent handoff before local ledger and delivery work."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from private_storage import prove
from push_card import preview_mode
from source_rotation import atomic_json
from runstore import MAX_CANDIDATE_BYTES, finalization_dir


def validate_handoff(run_dir: Path, run_id: str, nonce: str) -> bytes:
    if not run_id or not run_id.strip():
        raise ValueError('finalization run_id must be nonempty text')
    if not re.fullmatch(r'[0-9a-f]{32}', nonce):
        raise ValueError('invalid handoff nonce')
    candidates = run_dir / 'candidates.json'
    receipt = run_dir / 'candidate-ready.json'
    if any(path.resolve().parent != run_dir for path in (candidates, receipt)):
        raise ValueError('handoff inputs must remain inside the proved run workspace')
    if candidates.stat().st_size > MAX_CANDIDATE_BYTES or receipt.stat().st_size > 8192:
        raise ValueError('handoff size limit exceeded')
    data = candidates.read_bytes()
    if len(data) > MAX_CANDIDATE_BYTES:
        raise ValueError('handoff size limit exceeded')
    value = json.loads(data)
    rows = value.get('candidates') if isinstance(value, dict) else value
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError('candidates must be a list of objects')
    proof = json.loads(receipt.read_text(encoding='utf-8-sig'))
    if (not isinstance(proof, dict) or type(proof.get('schema_version')) is not int
            or type(proof.get('ready')) is not bool or proof != {
        'schema_version': 1, 'run_id': run_id, 'nonce': nonce, 'ready': True,
        'candidate_sha256': hashlib.sha256(data).hexdigest(),
    }):
        raise ValueError('missing, stale, or mismatched candidate readiness receipt')
    return data


def prior_claim(run_dir: Path, run_id: str, claim_dir: Path, *, migrate: bool):
    """Read a durable or legacy reservation; migration never grants a retry."""
    name = 'finalization-' + hashlib.sha256(run_id.encode()).hexdigest() + '.json'
    target = prove(claim_dir/name)
    legacy = prove(run_dir/name)
    found = []
    for path in dict.fromkeys((target, legacy)):
        if not path.exists():
            continue
        if not path.is_file() or path.stat().st_size > 8192:
            raise ValueError('invalid finalization record')
        raw = path.read_bytes()
        row = json.loads(raw)
        if (not isinstance(row, dict) or row.get('schema_version') != 1 or row.get('run_id') != run_id
                or row.get('state') not in ('uncertain', 'completed')
                or not isinstance(row.get('nonce'), str) or not re.fullmatch(r'[0-9a-f]{32}', row['nonce'])
                or not isinstance(row.get('candidate_sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', row['candidate_sha256'])):
            raise ValueError('invalid finalization record')
        found.append((path, row, raw))
    if not found:
        return None
    if any(row != found[0][1] for _, row, _ in found[1:]):
        raise ValueError('conflicting legacy and durable finalization records')
    path, row, raw = found[0]
    if migrate and path != target:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        path = target
    return path, row


def claim_run(run_dir: Path, run_id: str, nonce: str, data: bytes, *, claim_dir=None):
    """Permanently reserve a logical run before the driver can attempt delivery.

    The content hash and nonce are provenance, not retry credentials. A crash,
    partial send, or failed result leaves the reservation for manual inspection.
    """
    if not run_id or not run_id.strip():
        raise ValueError('finalization run_id must be nonempty text')
    claim_dir = prove(claim_dir) if claim_dir is not None else run_dir
    prior = prior_claim(run_dir, run_id, claim_dir, migrate=True)
    path = prove(claim_dir / ('finalization-' + hashlib.sha256(run_id.encode()).hexdigest() + '.json'))
    if path.parent != claim_dir:
        raise ValueError('finalization record escaped its proved claim directory')
    record = {'schema_version': 1, 'run_id': run_id, 'nonce': nonce,
              'candidate_sha256': hashlib.sha256(data).hexdigest(), 'state': 'uncertain'}
    if prior is not None:
        conflict = prior[1]['candidate_sha256'] != record['candidate_sha256']
        raise ValueError('conflicting content for logical run; inspection required' if conflict else
                         'logical run already finalized or uncertain; inspection required')
    claim_dir.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8') as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        prior = json.loads(path.read_text(encoding='utf-8'))
        conflict = not isinstance(prior, dict) or any(
            prior.get(key) != record[key] for key in ('run_id', 'candidate_sha256'))
        raise ValueError('conflicting content for logical run; inspection required' if conflict else
                         'logical run already finalized or uncertain; inspection required') from None
    return path, record


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', required=True, type=Path)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--nonce', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    args.dry_run = preview_mode(args.dry_run)
    try:
        run_dir = prove(args.run_dir)
        data = validate_handoff(run_dir, args.run_id, args.nonce)
        if not args.dry_run:
            claim, record = claim_run(run_dir, args.run_id, args.nonce, data,
                                      claim_dir=finalization_dir())
            snapshot = prove(run_dir / ('finalize-' + args.nonce + '.json'))
            if snapshot.parent != run_dir:
                raise ValueError('finalization snapshot must remain inside the proved run workspace')
            # An uncertain prior finalization must be inspected, never replayed.
            with snapshot.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print(json.dumps({'ok': False, 'stage': 'handoff', 'error': str(exc)}))
        return 4
    command = [sys.executable, '-B', str(Path(__file__).with_name('run.py')),
               '--run-id', args.run_id]
    if args.dry_run:
        command.extend(['--dry-run', '--no-ledger'])
    else:
        command.extend(['--in', str(snapshot), '--result-out', str(run_dir/'result.json')])
    # The existing driver owns validation, dedup, delivery, artifact checks, and exit status.
    try:
        options = {'input': data} if args.dry_run else {}
        code = subprocess.run(command, check=False, **options).returncode
        if code == 0 and not args.dry_run:
            atomic_json(claim, {**record, 'state': 'completed'})
        return code
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        result = {'ok': False, 'stage': 'driver', 'error': type(exc).__name__}
        if args.dry_run:
            result['preview'] = True
        else:
            result.update(snapshot_retained=str(snapshot), retry_requires_inspection=True)
        print(json.dumps(result))
        return 5


if __name__ == '__main__':
    raise SystemExit(main())
