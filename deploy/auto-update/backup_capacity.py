"""Portable actual-backup-v1 capacity check; no fixed free-space reserve.

Vendored with each project's updater. It does not depend on a host controller,
delete backups, reserve space, or replace handling of write-time ENOSPC.
"""
import contextlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat


class CapacityError(ValueError):
    pass


def sqlite_bytes(path):
    """Logical snapshot pages include committed WAL pages without counting WAL twice."""
    path = Path(path)
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro',
                                          uri=True, timeout=5)) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        pages = connection.execute('PRAGMA page_count').fetchone()[0]
        size = connection.execute('PRAGMA page_size').fetchone()[0]
        return pages * size


def filesystem(destination):
    """Inspect the target filesystem, also before a new backup directory exists."""
    path = Path(destination).absolute()
    while not path.exists():
        if path == path.parent:
            raise CapacityError('Backup destination has no existing filesystem')
        path = path.parent
    if not path.is_dir():
        raise CapacityError('Backup destination parent is not a directory')
    if hasattr(os, 'statvfs'):
        info = os.statvfs(path)
        unit = info.f_frsize or info.f_bsize
        return unit, info.f_bavail * unit, info.f_favail
    # Linux is the deployment target. Other platforms can still use byte checks
    # without pretending a guessed allocation unit or inode count is measured.
    return 1, shutil.disk_usage(path).free, None


class Plan:
    def __init__(self):
        self.entries = {'': ('directory', 0)}

    def add(self, name, kind, size=0):
        path = PurePosixPath(str(name))
        if path.is_absolute() or '..' in path.parts or kind not in ('file', 'directory', 'link'):
            raise ValueError('Invalid backup capacity member')
        name = '' if str(path) == '.' else str(path)
        if not isinstance(size, int) or size < 0:
            raise ValueError('Invalid backup capacity size')
        for parent in reversed(path.parents):
            key = '' if str(parent) == '.' else str(parent)
            previous = self.entries.setdefault(key, ('directory', 0))
            if previous[0] != 'directory':
                raise ValueError('Backup capacity parent is not a directory')
        if name in self.entries and self.entries[name][0] != kind:
            raise ValueError('Conflicting backup capacity members')
        self.entries[name] = (kind, size)
        return self

    def file(self, source, name, *, sqlite=False, follow_links=False):
        source = Path(source)
        info = source.stat() if follow_links else source.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Backup capacity input is not a regular file')
        return self.add(name, 'file', sqlite_bytes(source) if sqlite else info.st_size)

    def tree(self, source, name='', *, exclude=(), sqlite_top=False, follow_links=False):
        source = Path(source)
        ancestors = set()

        def visit(path, relative, top=False):
            info = path.stat() if follow_links else path.lstat()
            if stat.S_ISDIR(info.st_mode):
                identity = (info.st_dev, info.st_ino)
                if identity in ancestors:
                    raise ValueError('Backup source contains a directory cycle')
                self.add(relative, 'directory')
                ancestors.add(identity)
                try:
                    for child in sorted(path.iterdir()):
                        if top and (child.name in exclude or
                                    sqlite_top and child.name.endswith(('-wal', '-shm'))):
                            continue
                        visit(child, str(PurePosixPath(relative) / child.name))
                finally:
                    ancestors.remove(identity)
            elif stat.S_ISLNK(info.st_mode):
                self.add(relative, 'link', len(os.fsencode(os.readlink(path))))
            elif stat.S_ISREG(info.st_mode):
                self.file(path, relative, sqlite=sqlite_top and path.parent == source
                          and path.suffix == '.db', follow_links=follow_links)
            else:
                raise ValueError('Backup source contains a special file')

        visit(source, name, top=True)
        return self

    def document(self, name, value):
        # Matches the largest JSON formatting used by these updaters; all bytes
        # derive from the actual inventory, never an arbitrary fixed reserve.
        data = json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + '\n'
        return self.add(name, 'file', len(data.encode('utf-8')))

    def check(self, destination):
        unit, free, free_inodes = filesystem(destination)
        if not isinstance(unit, int) or unit <= 0:
            raise CapacityError('Invalid destination allocation unit')

        def allocated(size):
            return ((size + unit - 1) // unit) * unit

        # File payload is rounded on the destination filesystem, rather than
        # reusing source allocation (which is wrong for sparse/different disks).
        needed = sum(allocated(size) for kind, size in self.entries.values()
                     if kind != 'directory')
        directories = {name: 24 for name, (kind, _) in self.entries.items()
                       if kind == 'directory'}
        for name in self.entries:
            if not name:
                continue
            path = PurePosixPath(name)
            parent = '' if str(path.parent) == '.' else str(path.parent)
            # Directory entry payload grows with actual names. A directory
            # consumes at least one target allocation unit. Filesystem metadata
            # and concurrent writers still require normal write-error handling.
            directories[parent] += ((len(os.fsencode(path.name)) + 8 + 7) // 8) * 8
        root_exists = Path(destination).is_dir()
        missing_parents = 0
        parent = Path(destination).absolute().parent
        while not parent.exists():
            missing_parents += 1
            parent = parent.parent
        needed += sum(max(unit, allocated(size)) for name, size in directories.items()
                      if name or not root_exists)
        needed += missing_parents * unit
        inodes = len(self.entries) - int(root_exists) + missing_parents
        if free < needed or free_inodes is not None and 0 <= free_inodes < inodes:
            raise CapacityError('Backup needs %d bytes and %d entries; destination has %d bytes%s' %
                                (needed, inodes, free, '' if free_inodes is None else
                                 ' and %d free inodes' % free_inodes))
        return {'policy': 'actual-backup-v1', 'needed_bytes': needed,
                'needed_inodes': inodes, 'entries': len(self.entries),
                'available_bytes': free, 'allocation_unit': unit}
