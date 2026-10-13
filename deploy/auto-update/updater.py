"""Install this project's verified updater snapshot, with one predecessor."""
import json
import os
from pathlib import Path
import shutil
import subprocess

from common import (BASE, COMMAND, CONFIG, PROJECT, Deferred, Rejected, atomic,
                    locks, read, run, switch, unpack_tar, unpack_zip, write_json)
from sources import Source, evidence

MARKER = '# Managed by ' + PROJECT['name'] + '/auto-update'
UNIT_ROOT = Path('/etc/systemd/system')
UNIT = PROJECT['name'] + '-auto-update'
IDENTITY = ('id', 'repository', 'kind', 'workflow', 'name', 'source_path')


def same_project(project):
    return isinstance(project, dict) and all(project.get(key) == PROJECT[key] for key in IDENTITY)


def installed_sha():
    path = BASE / 'current/REVISION'
    return path.read_text().strip() if path.is_file() else None


def prepare(source, verified, work, *, force=False):
    """Download only this run's digest-checked artifact, before app downtime."""
    sha = verified['source']['sha']
    if installed_sha() == sha and not force:
        return None
    if verified['source']['repository'] != PROJECT['repository']:
        raise Rejected('Updater artifact belongs to another repository')
    work = Path(work)
    work.mkdir(parents=True)
    delivery = Source({'repository': PROJECT['repository'], 'artifact': 'auto-update'}, work, source.github)
    archive = work / 'artifact.zip'
    checksum = delivery.artifact(verified, archive)
    names = unpack_zip(archive, work / 'artifact', limit=4 * 1024**2)
    if names != {'auto-update.tar.gz'}:
        raise Rejected('Updater artifact has an unexpected file inventory')
    payload = work / 'payload'
    unpack_tar(work / 'artifact/auto-update.tar.gz', payload, prefix='auto-update', limit=4 * 1024**2)
    required = {'project.json', 'REVISION', 'watch.py', 'install.py', 'updater.py',
                'common.py', 'sources.py', 'adapters.py', 'ci_evidence.py',
                'systemd/' + UNIT + '.service', 'systemd/' + UNIT + '.timer'}
    if not all((payload / name).is_file() for name in required):
        raise Rejected('Updater artifact is incomplete')
    if (payload / 'REVISION').read_text().strip() != sha or not same_project(read(payload / 'project.json')):
        raise Rejected('Updater identity differs from its verified project')
    if {p.name for p in (payload / 'systemd').iterdir()} != {UNIT + '.service', UNIT + '.timer'}:
        raise Rejected('Updater must own exactly one project service and timer')
    for unit in (payload / 'systemd').iterdir():
        if MARKER not in unit.read_text():
            raise Rejected('Updater unit has no project ownership marker')
    return {'path': payload, 'sha': sha, 'sha256': checksum}


def activate(prepared, verified, *, config_source=None, force=False):
    """Project-scoped installation; never touches a different project's timer."""
    if prepared is None:
        return
    sha, payload = prepared['sha'], prepared['path']
    if BASE.is_symlink() or BASE.resolve() != BASE:
        raise Rejected('Updater installation root must not contain symlinks')
    BASE.mkdir(parents=True, exist_ok=True)
    with locks([BASE / 'install.lock']):
        # A concurrent installer must not be overwritten by an older watcher.
        old_sha = installed_sha()
        if old_sha == sha and not force:
            return
        for path in (COMMAND, UNIT_ROOT / (UNIT + '.service'), UNIT_ROOT / (UNIT + '.timer')):
            if path.is_symlink() or path.exists() and MARKER not in path.read_text():
                raise Rejected('Another owner installed ' + path.name)
        if config_source is not None and CONFIG.exists() and CONFIG.read_bytes() != Path(config_source).read_bytes():
            raise Rejected('Host configuration already exists; update it explicitly')
        target_config = Path(config_source) if config_source is not None else CONFIG
        run(['/usr/bin/python3', '-B', payload / 'watch.py', 'check', '--config', target_config])
        release = BASE / 'releases' / sha
        release.parent.mkdir(parents=True, exist_ok=True)
        if release.exists():
            if release.is_symlink():
                raise Rejected('Updater release must not be a symlink')
            old = {str(p.relative_to(release)): p.read_bytes() for p in release.rglob('*') if p.is_file()}
            new = {str(p.relative_to(payload)): p.read_bytes() for p in payload.rglob('*') if p.is_file()}
            if any(p.is_symlink() for p in release.rglob('*')) or old != new:
                raise Rejected('Installed updater snapshot differs from CI')
        else:
            shutil.copytree(payload, release)
        changes = {COMMAND: (('#!/bin/sh\n' + MARKER + '\nexec /usr/bin/python3 -B ' +
                              str(BASE / 'current/watch.py') + ' "$@"\n').encode(), 0o755)}
        for unit in (payload / 'systemd').iterdir():
            changes[UNIT_ROOT / unit.name] = (unit.read_bytes(), 0o644)
        if config_source is not None and not CONFIG.exists():
            changes[CONFIG] = (Path(config_source).read_bytes(), 0o600)
        changes = {p: value for p, value in changes.items()
                   if not p.exists() or p.read_bytes() != value[0] or p.stat().st_mode & 0o777 != value[1]}
        before = {str(p): {'body': p.read_text(), 'mode': p.stat().st_mode & 0o777} if p.exists() else None
                  for p in changes}
        pointers = {name: str((BASE / name).resolve()) if (BASE / name).is_symlink() else None
                    for name in ('current', 'previous')}
        write_json(BASE / 'install-backup.json', {'files': before, 'pointers': pointers})
        reload_needed = any(p.parent == UNIT_ROOT for p in changes)
        try:
            for path, (data, mode) in changes.items():
                atomic(path, data, mode)
            if pointers['current'] and pointers['current'] != str(release):
                switch(BASE, 'previous', Path(pointers['current']))
            switch(BASE, 'current', release)
            if reload_needed:
                run(['systemctl', 'daemon-reload'], timeout=30)
            write_json(BASE / 'installed.json', {'sha': sha, 'source': verified['source'],
                       'builder': verified['builder'], 'artifact_sha256': prepared['sha256'],
                       'route': verified['route']})
        except BaseException:
            for path, original in before.items():
                if original is None:
                    Path(path).unlink(missing_ok=True)
                else:
                    atomic(path, original['body'].encode(), original['mode'])
            for name, target in pointers.items():
                if target:
                    switch(BASE, name, Path(target))
                else:
                    (BASE / name).unlink(missing_ok=True)
            if reload_needed:
                run(['systemctl', 'daemon-reload'], timeout=30)
            raise
        # Keep the active and immediately previous updater. App recovery lives
        # elsewhere and is never included in this installation-only cleanup.
        keep = {(BASE / name).resolve() for name in ('current', 'previous') if (BASE / name).exists()}
        for old in release.parent.iterdir():
            if old not in keep and old.is_dir() and not old.is_symlink() and (old / 'project.json').is_file():
                try:
                    if same_project(read(old / 'project.json')):
                        shutil.rmtree(old)
                except OSError:
                    pass


def refresh(source, current, directory):
    """Retry updater installation when app acceptance already committed."""
    previous = installed_sha()
    if previous == current or previous and not source.ancestor(previous, current):
        return
    workflow = PROJECT['workflow'].split('/')[-1]
    rows = source.github.api(f'repos/{source.repo}/actions/workflows/{workflow}/runs?branch={PROJECT["branch"]}&head_sha={current}&per_page=10')['workflow_runs']
    rows = [r for r in rows if r.get('head_sha') == current and r.get('event') in ('push', 'workflow_dispatch')
            and r.get('path') == PROJECT['workflow'] and r.get('head_branch') == PROJECT['branch'] and r.get('status') == 'completed']
    if not rows:
        raise Deferred('Waiting for the running revision CI evidence to update the watcher')
    rows.sort(key=lambda r: r.get('conclusion') != 'success')
    verified = source.verified(rows[0])
    import tempfile
    with tempfile.TemporaryDirectory(prefix='updater-', dir=directory) as work:
        prepared = prepare(source, verified, Path(work) / 'download')
        activate(prepared, verified)
