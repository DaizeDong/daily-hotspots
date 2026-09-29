"""Verify each runtime write target in a separate PRIVATE GitHub companion."""
from pathlib import Path
import os
import re
import stat
import subprocess
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]


def bind_consumer(guard, root):
    """Bind only this privately loaded guard instance to its verified consumer root.

    The pinned resolver skips all .git files, including linked-worktree roots. The
    caller found this exact guard beneath its canonical __file__, so that consumer
    identity is stronger than another upward .git-directory search.
    """
    root = Path(root).resolve()
    if root != ROOT or Path(guard.__file__).resolve() != root/'guards/tools/datadir.py':
        raise RuntimeError('cannot establish the pinned guard consumer root')
    if not (root/'.git').exists() or not (root/'.dataclass.json').is_file():
        raise RuntimeError('consumer root lacks repository metadata')
    guard._own_repo_root = lambda: str(root)
    return guard


def _run(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=20,
                                env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('private storage verification unavailable: '+argv[0]) from exc
    if result.returncode:
        raise RuntimeError('private storage verification failed: '+argv[0])
    return result.stdout.strip()


def _repository_root(existing):
    """Verify the nearest repository without starting Git in a deep descendant.

    Windows Git may reject an initial directory that Python can traverse. Locate
    the boundary through the filesystem first, including linked-worktree .git
    files. Invalid nested markers and bare repositories must not fall back to
    an enclosing PRIVATE worktree.
    """
    try:
        for candidate in (existing, *existing.parents):
            marker = candidate/'.git'
            if os.path.lexists(marker):
                if not marker.is_file() and not marker.is_dir():
                    raise RuntimeError('invalid runtime Git worktree marker')
                repo = Path(_run(['git', '-C', str(candidate), 'rev-parse', '--show-toplevel'])).resolve()
                if repo != candidate:
                    raise RuntimeError('runtime repository differs from its nearest worktree marker')
                return repo
            if (candidate/'HEAD').is_file() and (candidate/'objects').is_dir():
                raise RuntimeError('runtime data cannot reside in a bare Git repository')
    except OSError as exc:
        raise RuntimeError('cannot inspect runtime Git worktree markers') from exc
    raise RuntimeError('runtime target is not inside a Git worktree')


def prove_report(requested):
    """Resolve runtime reports only after proving their PRIVATE companion destination."""
    try:
        return prove(requested)
    except RuntimeError as exc:
        raise RuntimeError(
            "report writes require a verified PRIVATE companion; initialize a separate "
            "PRIVATE GitHub repository with an origin and authenticated gh, then select a path inside it"
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
    path = Path(requested).expanduser()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    if any(part.lower()=='.git' or ':' in part or part.endswith((' ', '.')) for part in parts):
        raise RuntimeError('ambiguous or reserved runtime path')
    # Preserve directory-junction support: inspect the lexical path first, then
    # prove the actual destination. No descendant content is opened at this stage.
    for current in (path, *path.parents):
        try:
            state = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(state.st_mode) and state.st_nlink != 1:
            raise RuntimeError('runtime path contains a hardlink')
    path = exclusive_path(path.resolve())
    if path.is_relative_to(ROOT) or ROOT.is_relative_to(path):
        raise RuntimeError('runtime writes require a separate PRIVATE companion')
    existing = path
    while not existing.exists():
        existing = existing.parent
    if existing.is_file():
        existing = existing.parent
    repo = _repository_root(existing)
    if not path.is_relative_to(repo) or repo.is_relative_to(ROOT) or ROOT.is_relative_to(repo):
        raise RuntimeError('runtime target is not in a separate companion repository')
    remote = _run(['git','-C',str(repo),'remote','get-url','origin'])
    _prove_remote(remote)
    exclusive_path(path, inspect_tree=True)
    return path


def _prove_remote(remote):
    if '://' in remote:
        value = urlsplit(remote)
        if value.scheme not in {'https','ssh'} or value.password or value.query or value.fragment:
            raise RuntimeError('unsupported companion origin')
        host, name = value.hostname, value.path.lstrip('/')
    else:
        value = re.fullmatch(r'(?:[^@/:\s]+@)?([^/:\s]+):([^\s]+)',remote)
        if value is None:
            raise RuntimeError('companion origin must identify a GitHub repository')
        host,name=value.groups()
    if host != 'github.com':
        if remote.startswith('https://') or not re.fullmatch(r'[A-Za-z0-9_.-]+',host or ''):
            raise RuntimeError('cannot verify companion host')
        hosts=[line.split(None,1)[1].lower() for line in _run(['ssh','-G',host]).splitlines()
               if line.lower().startswith('hostname ')]
        if hosts != ['github.com']:
            raise RuntimeError('companion SSH alias must resolve to github.com')
    name=name.removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',name):
        raise RuntimeError('invalid companion identity')
    if _run(['gh','api','--hostname','github.com','repos/'+name,'--jq','.private']) != 'true':
        raise RuntimeError('runtime companion is PUBLIC or visibility is unknown')


def resolve_log_path(log_dir, name):
    """Resolve and prove a runtime log without creating any files."""
    if (not name or name in {'.', '..'} or name.lower() == '.git'
            or any(c in name for c in '/\\\\:<>"|?*') or any(ord(c) < 32 for c in name)
            or name.endswith((' ', '.'))
            or re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name)):
        raise RuntimeError('log name must be a non-reserved basename')
    if log_dir:
        directory = Path(log_dir)
    else:
        from archive import resolve_archive_dir
        directory = resolve_archive_dir() / 'logs'
    return prove_report(directory / name)


def transport_dir(run_directory):
    """Create a fresh, retained transport directory inside a proved run workspace."""
    import uuid
    workspace = prove(run_directory)
    if not workspace.is_dir():
        raise RuntimeError('PRIVATE run workspace must already exist')
    target = prove(workspace / ('transport-' + uuid.uuid4().hex))
    target.mkdir(exist_ok=False)
    return target


def publication_target(config_dir):
    """Require an attached branch with a proved PRIVATE fetch/push upstream."""
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
        _prove_remote(urls[0])
    return {'local_branch': local, 'remote': remote, 'branch': branch,
            'refspec': 'HEAD:' + merge}


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
    args = parser.parse_args(argv)
    try:
        if args.command == 'log-path':
            print(resolve_log_path(args.log_dir, args.name))
        elif args.command == 'transport-dir':
            print(transport_dir(args.run_dir))
        else:
            print(json.dumps(publication_target(args.config_dir)))
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
