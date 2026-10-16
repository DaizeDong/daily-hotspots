"""Place each runtime write target in a separate companion worktree, by path checks only.

Publication state (the companion being PRIVATE) is enforced at commit time by the repository
.githooks shims, not on every write: the runtime runs no git or gh subprocess to prove visibility.
"""
from pathlib import Path
import sys
import os
import re
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[3]


def _run(argv):
    if len(argv) < 4 or argv[:2] != ['git', '-C']:
        raise RuntimeError('only local Git metadata queries are supported')
    tail = argv[3:]
    if not (tail[:3] == ['symbolic-ref', '--quiet', '--short']
            or tail[:2] == ['check-ref-format', '--branch']
            or tail == ['rev-parse', '--verify', '@{upstream}']
            or tail[:2] == ['config', '--get']
            or tail[:2] == ['remote', 'get-url']):
        raise RuntimeError('unsupported local Git metadata query')
    selectors = {'GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_IMPLICIT_WORK_TREE',
                 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                 'GIT_GRAFT_FILE', 'GIT_SHALLOW_FILE', 'GIT_PREFIX', 'GIT_INTERNAL_SUPER_PREFIX',
                 'GIT_CEILING_DIRECTORIES', 'GIT_DISCOVERY_ACROSS_FILESYSTEM', 'GIT_CONFIG'}
    environment = {key: value for key, value in os.environ.items() if key.upper() not in selectors}
    environment['GIT_OPTIONAL_LOCKS'] = '0'
    try:
        result = subprocess.run(argv[:1] + ['-c', 'core.fsmonitor=false'] + argv[1:],
                                capture_output=True, text=True, encoding='utf-8', timeout=20,
                                env=environment)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('private storage verification unavailable: '+argv[0]) from exc
    if result.returncode:
        raise RuntimeError('private storage verification failed: '+argv[0])
    return result.stdout.strip()


def _repository_root(existing):
    """Verify the nearest repository without starting Git in a deep descendant.

    Windows Git may reject an initial directory that Python can traverse. Locate
    the boundary through the filesystem first, including linked-worktree .git
    files. The nearest marker wins; invalid markers and bare repositories are
    refused rather than falling back to an enclosing one.
    """
    try:
        for candidate in (existing, *existing.parents):
            marker = candidate/'.git'
            if os.path.lexists(marker):
                if marker.is_file():
                    line = marker.read_text(encoding='utf-8', errors='replace').strip()
                    if not line.startswith('gitdir:') or not (
                            candidate/line.split(':', 1)[1].strip()).is_dir():
                        raise RuntimeError('invalid runtime Git worktree marker')
                elif not marker.is_dir():
                    raise RuntimeError('invalid runtime Git worktree marker')
                return candidate
            if (candidate/'HEAD').is_file() and (candidate/'objects').is_dir():
                raise RuntimeError('runtime data cannot reside in a bare Git repository')
    except OSError as exc:
        raise RuntimeError('cannot inspect runtime Git worktree markers') from exc
    raise RuntimeError('runtime target is not inside a Git worktree')


def prove_report(requested):
    """Resolve a runtime report destination inside a separate companion worktree."""
    try:
        return authorize_write(requested)
    except RuntimeError as exc:
        raise RuntimeError(
            "report writes require an initialized PRIVATE companion; clone the separate "
            "daily-hotspots-config repository, then select a path inside it"
        ) from exc


def exclusive_path(requested, *, inspect_tree=False):
    """Reject aliases using metadata before resolving or opening runtime content."""
    path = Path(os.path.abspath(Path(requested).expanduser()))
    pending = [path, *path.parents]
    while pending:
        current = pending.pop()
        try:
            state = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(state.st_mode) or getattr(state, 'st_file_attributes', 0) & 0x400:
            raise RuntimeError('runtime path contains a symlink or reparse point')
        if stat.S_ISREG(state.st_mode) and state.st_nlink != 1:
            raise RuntimeError('runtime path contains a hardlink')
        if inspect_tree and current.is_relative_to(path) and stat.S_ISDIR(state.st_mode):
            pending.extend(child for child in current.iterdir() if child.name.casefold() != '.git')
    return path


def prove(requested):
    """Check a runtime target by path only: a separate companion worktree, never this repo.

    Refuses reserved names, Git metadata, links and hardlinks, any target inside this source
    tree, and any target that is not inside a separate Git worktree found by its .git marker.
    No subprocess runs; the companion's PRIVATE publication is enforced at commit time.
    """
    path = Path(requested).expanduser()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    if any(part.lower()=='.git' or ':' in part or part.endswith((' ', '.')) for part in parts):
        raise RuntimeError('ambiguous or reserved runtime path')
    # Preserve directory-junction support: inspect the lexical path first, then
    # check the actual destination. No descendant content is opened at this stage.
    for current in (path, *path.parents):
        try:
            state = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(state.st_mode) and state.st_nlink != 1:
            raise RuntimeError('runtime path contains a hardlink')
    path = path.resolve()
    if path.is_relative_to(ROOT) or ROOT.is_relative_to(path):
        raise RuntimeError('runtime writes require a separate PRIVATE companion')
    existing = path
    while not existing.exists():
        existing = existing.parent
    if existing.is_file():
        if existing != path:
            raise RuntimeError('runtime directory path contains a file')
        existing = existing.parent
    repo = _repository_root(existing)
    if repo.is_relative_to(ROOT) or ROOT.is_relative_to(repo):
        raise RuntimeError('runtime target is not in a separate companion repository')
    exclusive_path(path, inspect_tree=True)
    return path


def resolve_log_path(log_dir, name):
    """Resolve and check a runtime log path without creating any files."""
    if (not name or name in {'.', '..'} or name.lower() == '.git'
            or any(c in name for c in '/\\\\:<>"|?*') or any(ord(c) < 32 for c in name)
            or name.endswith((' ', '.'))
            or re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name)):
        raise RuntimeError('log name must be a non-reserved basename')
    if name.lower().endswith('.gz') or name.lower() == 'compression.json':
        raise RuntimeError('compressed log archives are not runtime append targets')
    if log_dir:
        directory = Path(log_dir)
    else:
        from archive import resolve_archive_dir
        directory = resolve_archive_dir() / 'logs'
    return authorize_write(directory / name)


def transport_dir(run_directory):
    """Create a fresh transport directory governed by its checked workspace's lifetime."""
    import uuid
    workspace = prove(run_directory)
    if not workspace.is_dir():
        raise RuntimeError('PRIVATE run workspace must already exist')
    target = prove(workspace / ('transport-' + uuid.uuid4().hex))
    target.mkdir(exist_ok=False)
    return target


def publication_target(config_dir):
    """Require an attached branch with exactly one configured fetch/push upstream."""
    selected = prove(config_dir)
    repo = _repository_root(selected)
    git = ['git', '-C', str(repo)]
    local = _run(git + ['symbolic-ref', '--quiet', '--short', 'HEAD'])
    if not local or local.startswith('-'):
        raise RuntimeError('publication requires an attached branch')
    _run(git + ['check-ref-format', '--branch', local])
    remote = _run(git + ['config', '--get', 'branch.' + local + '.remote'])
    merge = _run(git + ['config', '--get', 'branch.' + local + '.merge'])
    if not remote or remote == '.' or remote.startswith('-') or any(c.isspace() for c in remote):
        raise RuntimeError('publication requires a configured remote upstream')
    prefix = 'refs/heads/'
    if not merge.startswith(prefix):
        raise RuntimeError('publication upstream must be a branch')
    branch = merge[len(prefix):]
    if not branch or branch.startswith('-'):
        raise RuntimeError('invalid publication upstream branch')
    _run(git + ['check-ref-format', '--branch', branch])
    if not _run(git + ['rev-parse', '--verify', '@{upstream}']):
        raise RuntimeError('publication upstream is unavailable locally')
    for options in ([], ['--push']):
        urls = _run(git + ['remote', 'get-url', *options, '--all', remote]).splitlines()
        if len(urls) != 1 or not urls[0]:
            raise RuntimeError('publication requires exactly one fetch and push destination')
    import roster
    roster_path = prove(roster.resolve_config_roster_path(str(selected)))
    if not roster_path.is_relative_to(repo) or _repository_root(roster_path) != repo:
        raise RuntimeError('runtime roster must share the PRIVATE publication repository')
    return {'local_branch': local, 'remote': remote, 'branch': branch,
            'refspec': 'HEAD:' + merge, 'repository_root': str(repo),
            'roster_pathspec': roster_path.relative_to(repo).as_posix()}


def main(argv=None):
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    log = commands.add_parser('log-path')
    log.add_argument('--log-dir')
    log.add_argument('--name', required=True)
    publication = commands.add_parser('publication')
    publication.add_argument('--config-dir', required=True)
    transport = commands.add_parser('transport-dir')
    transport.add_argument('--run-dir', required=True)
    target = commands.add_parser('prove-path')
    target.add_argument('--path', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'log-path':
            print(resolve_log_path(args.log_dir, args.name))
        elif args.command == 'transport-dir':
            print(transport_dir(args.run_dir))
        elif args.command == 'prove-path':
            print(prove(args.path))
        else:
            print(json.dumps(publication_target(args.config_dir)))
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def authorize_write(requested):
    """Check a file destination by path; kept as the writers' single entry point."""
    path = prove(requested)
    if path.is_dir():
        raise RuntimeError('file destination is a directory')
    return path


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
