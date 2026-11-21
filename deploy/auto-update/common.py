"""Bounded filesystem and process operations for this project's updater."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile

SHA = re.compile(r'[a-f0-9]{40}\Z')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
PROJECT = json.loads(Path(__file__).with_name('project.json').read_text())
STATE = Path('/var/lib') / (PROJECT['name'] + '-auto-update')
CONFIG = Path('/etc') / PROJECT['name'] / 'auto-update.json'
BASE = Path('/usr/local/lib') / (PROJECT['name'] + '-auto-update')
COMMAND = Path('/usr/local/sbin') / (PROJECT['name'] + '-auto-update')
WATCHDECK_LARGE_MEMBERS = {'vendor-python/watchdeck-browser/chrome': 512 * 1024**2}


class Deferred(RuntimeError):
    """A prerequisite is not ready; leave the running release alone and retry."""


class Rejected(RuntimeError):
    """An unsafe artifact or failed acceptance; quarantine this revision."""


def now():
    return datetime.now(timezone.utc).isoformat()


def run(args, *, cwd=None, timeout=90, env=None, binary=False, input=None):
    try:
        result = subprocess.run([str(a) for a in args], cwd=cwd, timeout=timeout,
                                env=env, input=input, capture_output=True, text=not binary)
    except subprocess.TimeoutExpired:
        raise Deferred('Command timed out: ' + Path(str(args[0])).name) from None
    if result.returncode:
        # Command output can contain application state or Compose environment values.
        raise Deferred(f'{Path(str(args[0])).name} exited {result.returncode}')
    return result.stdout


def read(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024**2:
        raise Rejected('Unsafe or oversized JSON file: ' + str(path))
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise Rejected('Duplicate JSON key')
            value[key] = item
        return value
    return json.loads(path.read_text(), object_pairs_hook=unique)


def atomic(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise Rejected('Refusing to replace a symlink: ' + str(path))
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        sync(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, value):
    atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def sync(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@contextmanager
def locks(paths):
    descriptors = []
    try:
        for name in sorted(set(str(p) for p in paths)):
            path = Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.parent.resolve() != path.parent:
                raise Rejected('Deployment lock parent must not be a symlink')
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            descriptors.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o022:
                raise Rejected('Unsafe deployment lock')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Deferred('Deployment or retention is busy') from None
        yield
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def switch(root, name, target):
    root, target = Path(root), Path(target)
    if target.resolve() != target or target.parent != root / 'releases' or not target.is_dir():
        raise Rejected('Release target escapes its managed root')
    link = root / name
    if link.exists() and not link.is_symlink():
        raise Rejected('Deployment pointer is not a symlink')
    if link.is_symlink() and link.resolve().parent != root / 'releases':
        raise Rejected('Deployment pointer escapes its managed root')
    temporary = root / ('.' + name + '-auto-deploy')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)
    sync(root)


def member_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts or '\\' in name:
        raise Rejected('Archive contains an unsafe path')
    return path


def unpack_zip(archive, destination, *, limit=512 * 1024**2):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    total, names = 0, set()
    with zipfile.ZipFile(archive) as source:
        for item in source.infolist():
            path = member_path(item.filename)
            if item.is_dir():
                continue
            total += item.file_size
            if (str(path) in names or total > limit or len(names) >= 50000
                    or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1):
                raise Rejected('Archive exceeds the safe extraction contract')
            names.add(str(path))
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as stream, target.open('xb') as output:
                shutil.copyfileobj(stream, output)
            target.chmod(0o644)
    return names


def unpack_tar(archive, destination, *, prefix, limit=1536 * 1024**2, member_limits=None):
    """Only regular files; CI dereferences package-internal links before archiving."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    total, names = 0, set()
    with tarfile.open(archive, 'r:gz') as source:
        for item in source:
            path = member_path(item.name)
            if path.parts[0] != prefix:
                raise Rejected('Archive root differs from the configured application')
            if item.isdir():
                continue
            relative = PurePosixPath(*path.parts[1:])
            total += item.size
            maximum = (member_limits or {}).get(str(relative), 256 * 1024**2)
            if (not item.isfile() or len(path.parts) < 2 or str(relative) in names
                    or total > limit or item.size > maximum or len(names) >= 100000):
                raise Rejected('Archive exceeds the safe extraction contract')
            names.add(str(relative))
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(item) as stream, target.open('xb') as output:
                shutil.copyfileobj(stream, output)
            target.chmod(0o755 if item.mode & 0o111 else 0o644)
    return names


def verify_tree(root, sha, repository):
    root = Path(root)
    record = read(root / 'release.json', {})
    if (root.resolve() != root or record.get('commit') != sha or record.get('repository') != repository
            or record.get('protocol') != repository.split('/')[-1] + '/v1' or not record.get('version')
            or not re.fullmatch('[a-f0-9]{64}', (root / 'archive.sha256').read_text().strip())):
        raise Rejected('Release manifest differs from verified CI source')
    files = read(root / 'files.json', {})
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() or p.is_symlink()}
    if not files or actual != set(files) | {'files.json', 'archive.sha256'}:
        raise Rejected('Installed release file inventory changed')
    for name, expected in files.items():
        path = root / member_path(name)
        if path.resolve() != path or not path.is_file() or digest(path) != expected:
            raise Rejected('Installed release content changed')


def seal(root, sha, repository, archive_digest):
    root = Path(root)
    manifest = read(root / 'release.json', {})
    if manifest and (manifest.get('commit') != sha or manifest.get('repository') != repository):
        raise Rejected('Archive manifest differs from verified CI')
    if not manifest:
        manifest = {'commit': sha, 'repository': repository, 'version': '1',
                    'protocol': repository.split('/')[-1] + '/v1'}
        atomic(root / 'release.json', (json.dumps(manifest) + '\n').encode(), 0o644)
    files = {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob('*'))
             if p.is_file() and p.name not in ('files.json', 'archive.sha256')}
    atomic(root / 'files.json', (json.dumps(files, sort_keys=True) + '\n').encode(), 0o644)
    atomic(root / 'archive.sha256', (archive_digest + '\n').encode(), 0o644)
    verify_tree(root, sha, repository)
