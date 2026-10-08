"""Freeze source batches and commit rotation only after durable pull receipts."""
from contextlib import contextmanager
import hashlib
import json
import os
import stat
from pathlib import Path
import tempfile

import roster as rt
from private_storage import authorize_write as prove


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2)+'\n').encode('utf-8'))


def atomic_bytes(path, data):
    path = prove(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import uuid
    from private_storage import _repository_root
    temporary = prove(_repository_root(path.parent) / ".staging" / ("atomic-" + uuid.uuid4().hex + ".partial"))
    temporary.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    identity = os.fstat(fd)

    def owns_temporary():
        try:
            current = temporary.lstat()
        except FileNotFoundError:
            return False
        return (stat.S_ISREG(current.st_mode) and current.st_nlink == 1
                and os.path.samestat(identity, current))

    def verify_temporary():
        if prove(temporary) != temporary or not owns_temporary():
            raise RuntimeError('atomic temporary file changed identity or destination')

    try:
        with os.fdopen(fd, 'wb') as stream:
            verify_temporary()
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if prove(path) != path:
            raise RuntimeError('atomic destination changed during validation')
        verify_temporary()
        os.replace(temporary, path)
    finally:
        if owns_temporary():
            temporary.unlink()


@contextmanager
def collection_lock(archive, roster_path=None, dry_run=False):
    """Serialize the receipt ledger and cursor; a stale lock requires inspection."""
    if dry_run:
        yield
        return
    paths = [Path(archive)/'.sources.lock']
    if roster_path is not None:
        path = Path(roster_path)
        paths.append(path.with_name(path.name+'.rotation.lock'))
    acquired = []
    try:
        for path in sorted(set(paths)):
            path = prove(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = path.open('x', encoding='utf-8')
            acquired.append(path)
            with stream:
                stream.write('Source receipt and rotation transaction in progress\n')
        yield
    finally:
        failures = []
        for path in reversed(acquired):
            try:
                path.unlink()
            except OSError:
                failures.append(str(path))
        if failures:
            raise RuntimeError('source transaction cleanup required: '+', '.join(failures))


def position(roster):
    return {'handles': [rt.normalize_handle(row['handle']).lower()
                        for row in rt.select_handles(roster)],
            'cursor': rt.rotation_cursor(roster)}


def prepare(roster, cfg, run_id, archive, roster_path, dry_run=False):
    """Persist the original plan before any receipt, so retries cannot select a later batch."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError('source run_id must be nonempty text')
    path = (Path(archive)/'source-rotation'/(hashlib.sha256(run_id.encode()).hexdigest()+'.json')
            if archive is not None else None)
    if path is not None and path.exists():
        record = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(record, dict) or type(record.get('schema_version')) is not int
                or record.get('schema_version') != 1
                or record.get('run_id') != run_id or record.get('roster_path') != str(Path(roster_path).resolve())
                or record.get('plan_sha256') != digest(record.get('plan'))
                or record.get('snapshot_sha256') != digest({key:record.get(key) for key in ('roster','position','plan')})
                or not isinstance(record.get('roster'), dict)
                or not isinstance(record.get('position'), dict)
                or not isinstance(record.get('plan'), list)):
            raise ValueError('invalid source rotation record')
        handles = [task.get('handle') for task in record['plan'] if isinstance(task, dict)]
        if not all(isinstance(handle, str) and handle for handle in handles):
            raise ValueError('invalid source rotation plan identities')
        if len(handles) != len(record['plan']) or len({handle.lower() for handle in handles}) != len(handles):
            raise ValueError('invalid source rotation plan identities')
        return record
    ok, errors = rt.validate_roster(roster)
    if not ok:
        raise ValueError('invalid roster: '+'; '.join(errors))
    plan = rt.plan_pulls_report(roster, cfg)['plan']
    frozen_roster = {key:value for key,value in roster.items() if key != 'rotation_runs'}
    record = dict(schema_version=1, run_id=run_id, roster_path=str(Path(roster_path).resolve()) if roster_path else None,
                  roster=frozen_roster, position=position(roster), plan=plan, plan_sha256=digest(plan))
    record['snapshot_sha256'] = digest({key:record[key] for key in ('roster','position','plan')})
    if plan and not dry_run:
        if path is None:
            raise ValueError('rotation requires an initialized private archive')
        atomic_json(path, record)
    return record


def commit(record, confirmed, roster_path, dry_run=False):
    """Commit a whole confirmed batch once; partial batches wait for their missing receipts."""
    selected = [task['handle'] for task in record['plan']]
    expected = {handle.lower() for handle in selected}
    result = dict(selected=selected, confirmed=sorted(expected & confirmed), consumed=0,
                  advanced=False, status='preview' if dry_run else 'pending')
    if dry_run or not selected:
        if not selected:
            result['status'] = 'no_roster_batch'
        return result
    current, error = rt._read_roster_file(Path(roster_path))
    if error is not None:
        raise ValueError('cannot commit rotation to an unreadable roster: '+error)
    runs = current.get('rotation_runs', {})
    if not isinstance(runs, dict):
        raise ValueError('invalid committed rotation receipts')
    previous = runs.get(record['run_id'])
    receipt = dict(plan_sha256=record['plan_sha256'], consumed=len(selected))
    if previous is not None:
        if not isinstance(previous, dict) or type(previous.get('consumed')) is not int or previous != receipt:
            raise ValueError('conflicting committed rotation receipt')
        if not expected <= confirmed:
            raise ValueError('committed rotation receipt is missing successful pull records')
        result.update(status='replayed', consumed=len(selected), cursor=rt.rotation_cursor(current))
        return result
    if not expected <= confirmed:
        return result
    if position(current) != record['position']:
        raise ValueError('roster position changed since this batch was planned; use a new run_id')
    rt.advance_rotation(current, len(selected))
    current['rotation_runs'] = dict(runs, **{record['run_id']: receipt})
    rt.save_roster(current, path=str(roster_path))
    result.update(status='committed', consumed=len(selected), advanced=True, cursor=rt.rotation_cursor(current))
    return result
