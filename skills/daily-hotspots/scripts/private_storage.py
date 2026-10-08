"""Verify each runtime write target in a separate PRIVATE GitHub companion."""
from pathlib import Path
from functools import lru_cache
import importlib.util
import sys
import os
import re
import stat
import subprocess

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
    files. Invalid nested markers and bare repositories must not fall back to
    an enclosing PRIVATE worktree.
    """
    try:
        for candidate in (existing, *existing.parents):
            marker = candidate/'.git'
            if os.path.lexists(marker):
                if not marker.is_file() and not marker.is_dir():
                    raise RuntimeError('invalid runtime Git worktree marker')
                return candidate
            if (candidate/'HEAD').is_file() and (candidate/'objects').is_dir():
                raise RuntimeError('runtime data cannot reside in a bare Git repository')
    except OSError as exc:
        raise RuntimeError('cannot inspect runtime Git worktree markers') from exc
    raise RuntimeError('runtime target is not inside a Git worktree')


def prove_report(requested):
    """Resolve runtime reports only after proving their PRIVATE companion destination."""
    try:
        return authorize_write(requested)
    except RuntimeError as exc:
        raise RuntimeError(
            "report writes require a verified PRIVATE companion; initialize a separate "
            "PRIVATE GitHub repository with a fresh visibility receipt, then select a path inside it"
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


def prove(requested, *, check_ignored=True):
    """Require version-control eligibility for every target, including locks and temps.

    Transient files receive no ignore exemption. Retained recovery locks must remain
    eligible for the companion's history just like the observations they protect.
    """
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
    proof = _prove_repository(repo)
    if Path(proof.root).resolve() != repo:
        raise RuntimeError('runtime repository differs from its nearest worktree marker')
    head = _read_repository(proof, 'rev-parse', '--verify', 'HEAD')
    if head.returncode != 0 or not head.stdout.strip():
        raise RuntimeError('PRIVATE runtime companion requires committed history')
    relative = path.relative_to(repo).as_posix()
    ignored = _read_repository(proof, 'check-ignore', '--no-index', '-q', '--', relative)
    if check_ignored and ignored.returncode != 1:
        raise RuntimeError('runtime target is ignored or version-control eligibility is unknown')
    exclusive_path(path, inspect_tree=True)
    current = _prove_repository(repo)
    if (proof.root, proof.repositories, proof.signature) != (current.root, current.repositories, current.signature):
        raise RuntimeError('PRIVATE publication state changed during target validation')
    return path


@lru_cache(maxsize=1)
def _shared_boundary():
    """Require the supported shared API; do not fall back to a consumer transport parser."""
    source = ROOT / 'guards/tools/data_boundary.py'
    try:
        spec = importlib.util.spec_from_file_location('daily_hotspots_data_boundary', source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    except (OSError, ImportError, ValueError) as exc:
        raise RuntimeError('PRIVATE proof unavailable; initialize the guards submodule') from exc
    if not all(callable(getattr(module, name, None)) for name in
               ('prove_private_companion', 'read_private_companion_git')):
        raise RuntimeError('PRIVATE proof unavailable; update the guards submodule')
    return module


def _prove_repository(repository):
    boundary = _shared_boundary()
    try:
        return boundary.prove_private_companion(repository)
    except boundary.GitError as exc:
        raise RuntimeError('runtime companion is PUBLIC or visibility is unknown; fresh PRIVATE proof required') from exc


def _read_repository(proof, *arguments):
    boundary = _shared_boundary()
    try:
        return boundary.read_private_companion_git(proof, *arguments)
    except boundary.GitError as exc:
        raise RuntimeError('cannot verify committed history or runtime target eligibility') from exc


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
    """Create a fresh transport directory governed by its proved workspace's lifetime."""
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
    proof = _prove_repository(repo)
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
        raise RuntimeError('runtime roster must share the proved PRIVATE publication repository')
    if _prove_repository(repo) != proof:
        raise RuntimeError('publication configuration changed during validation')
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


@lru_cache(maxsize=1)
def _storage_contract():
    source = ROOT / "guards/tools/storage_contract.py"
    spec = importlib.util.spec_from_file_location("daily_hotspots_storage_contract", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError) as exc:
        raise RuntimeError("update the pinned guards submodule for artifact write admission") from exc
    return module


def authorize_write(requested):
    """Prove a file destination and its declared source-owned persistence policy."""
    path = prove(requested, check_ignored=False)
    repository = _repository_root(path.parent)
    try:
        return _storage_contract().authorize_artifact_write(
            ROOT, repository, path.relative_to(repository).as_posix()).path
    except (ValueError, RuntimeError) as exc:
        raise RuntimeError("artifact write refused: " + str(exc)) from exc


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
