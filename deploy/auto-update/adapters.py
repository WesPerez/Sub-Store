"""Application delivery adapters; never install dependencies or build on the host."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import time
import uuid

from common import (Deferred, Rejected, SHA, DIGEST, atomic, digest, locks, now, read, run,
                    seal, switch, unpack_tar, unpack_zip, verify_tree, write_json, WATCHDECK_LARGE_MEMBERS)


def systemctl(action, units):
    run(['systemctl', action, *units], timeout=120)


def probe(app, *, attempts=1):
    for attempt in range(attempts):
        try:
            for unit in app.get('units', []):
                if run(['systemctl', 'is-active', unit]).strip() != 'active':
                    raise Deferred('Application service is not active')
            check = app['health']
            args = ['curl', '--silent', '--show-error', '--fail', '--max-time', '10']
            if check.get('unix_socket'):
                args += ['--unix-socket', check['unix_socket']]
            if check.get('host'):
                args += ['-H', 'Host: ' + check['host']]
            if check.get('resolve'):
                args += ['--resolve', check['resolve']]
            args += [check['url']]
            if check.get('user'):
                args = ['runuser', '-u', check['user'], '--', *args]
            body = run(args, timeout=15)
            if 'json' in check:
                actual = json.loads(body)
                if any(actual.get(k) != v for k, v in check['json'].items()):
                    raise Deferred('Application health response is not ready')
            if check.get('contains') and check['contains'] not in body:
                raise Deferred('Application page did not return its expected marker')
            if app['kind'] == 'console':
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(15)
                    client.connect(app['control_socket'])
                    client.sendall(b'{"actor":"auto-deploy-health","method":"snapshot","data":{}}\n')
                    with client.makefile('rb') as stream:
                        response = json.loads(stream.readline(8 * 1024**2))
                if not response.get('ok'):
                    raise Deferred('Schedule control backend did not pass acceptance')
            if app['kind'] == 'compose':
                state = json.loads(run(['docker', 'inspect', '--format', '{{json .State}}', app['container']]))
                if state.get('Health', {}).get('Status') != 'healthy':
                    raise Deferred('Container is not healthy')
            return
        except (Deferred, OSError, ValueError):
            if attempt + 1 == attempts:
                raise Deferred('Application health acceptance failed') from None
            time.sleep(2)


def container(app):
    # Inspection can include secrets; only selected non-secret fields escape here.
    obj = json.loads(run(['docker', 'inspect', app['container']]))[0]
    image = json.loads(run(['docker', 'image', 'inspect', obj['Image']]))[0]
    refs = [value for value in image.get('RepoDigests', []) if value.startswith(app['image'] + '@sha256:')]
    return {'id': obj['Id'], 'image_id': obj['Image'], 'reference': refs[0] if len(refs) == 1 else obj['Image'],
            'sha': image.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.revision'),
            'mounts': sorted((m['Source'], m['Destination'], m['RW']) for m in obj['Mounts']),
            'networks': sorted(obj['NetworkSettings']['Networks']),
            'ports': obj['HostConfig']['PortBindings']}


def current(app):
    root = Path(app['root'])
    if app['kind'] == 'native':
        value = (root / 'current').resolve().name
    elif app['kind'] == 'binary':
        value = read(root / 'deployment.json', {}).get('source_sha')
    elif app['kind'] == 'watchdeck':
        value = run(['git', '-C', root, 'rev-parse', 'HEAD']).strip()
    elif app['kind'] == 'console':
        value = (root / 'REVISION').read_text().strip()
    else:
        value = container(app)['sha']
    if not SHA.fullmatch(value or ''):
        raise Deferred('Cannot identify the complete running source revision')
    return value


def idle(app):
    if app['kind'] != 'watchdeck':
        return
    # Do not interrupt a forum visit or OAuth flow. Inspect only argv and never
    # read a worker payload, environment, credentials or browser profile.
    root = Path(app['root'])
    if app.get('health', {}).get('url'):
        try:
            health = json.loads(run(['curl', '--silent', '--show-error', '--fail', '--max-time', '5', app['health']['url']], timeout=10))
            if not isinstance(health, dict) or health.get('ok') is not True:
                raise ValueError('Health response is not ready')
        except Exception:
            raise Deferred('Watchdeck health check is unavailable; retry when ready') from None
        if health.get('interactiveLogin'):
            raise Deferred('Watchdeck interactive login is active; retry when idle')
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / 'cmdline').read_bytes().split(b'\0')
            if any(arg.endswith((b'/scripts/browser_fetch.py', b'/scripts/github_connect.py')) for arg in command):
                raise Deferred('Watchdeck browser work is active; retry when idle')
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    if run(['git', '-C', root, 'status', '--porcelain']).strip():
        raise Deferred('Watchdeck source has uncommitted work; retry when clean')


def runtime_files(root):
    root = Path(root)
    return sorted([p.name for p in root.glob('*.py')] +
                  [str(p.relative_to(root)) for p in (root / 'static').rglob('*') if p.is_file()])


def snapshot_databases(app, destination):
    """Consistent recovery copy; automatic rollback never rewinds business state."""
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = []
    for index, filename in enumerate(app.get('databases', [])):
        path = Path(filename)
        if not path.is_file() or path.is_symlink():
            raise Deferred('Configured application database is missing or replaced')
        target = destination / f'{index}-{path.name}'
        deadline = time.monotonic() + 90
        def progress(*_):
            if time.monotonic() > deadline:
                raise Deferred('Database snapshot exceeded its 90 second budget')
        with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=15) as source:
            with sqlite3.connect(target) as copy:
                source.backup(copy, pages=256, sleep=0.05, progress=progress)
                copy.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
                if copy.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise Rejected('Database recovery copy failed integrity validation')
        target.chmod(0o600)
        result.append({'source': str(path), 'backup': str(target), 'sha256': digest(target)})
    return result


def snapshot_directories(app, destination):
    """Copy regular application data while stopped; never restore it automatically."""
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = []
    for index, filename in enumerate(app.get('data_directories', [])):
        root = Path(filename)
        if not root.is_absolute() or root.resolve() != root or not root.is_dir():
            raise Deferred('Configured data directory is missing or replaced')
        paths = sorted(root.rglob('*'))
        if len(paths) > 50000:
            raise Deferred('Data recovery inventory exceeds the file budget')
        for path in paths:
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
                raise Deferred('Data recovery cannot follow links or special files')
        required = sum(path.stat().st_size for path in paths if path.is_file())
        if shutil.disk_usage(destination).free < required + 1024**3:
            raise Deferred('Not enough space for a consistent data recovery copy')
        target = destination / str(index)
        target.mkdir(mode=0o700, parents=True)
        hashes = {}
        for path in paths:
            relative = path.relative_to(root)
            copy = target / relative
            if path.is_dir():
                copy.mkdir(mode=0o700, exist_ok=True)
            else:
                shutil.copyfile(path, copy, follow_symlinks=False)
                copy.chmod(0o600)
                hashes[str(relative)] = digest(copy)
                if hashes[str(relative)] != digest(path):
                    raise Deferred('Application data changed during recovery copy')
        result.append({'source': str(root), 'backup': str(target), 'files': hashes})
    return result


def preflight(app, expected):
    if current(app) != expected:
        raise Deferred('Production changed during download; reconsider next poll')
    probe(app)
    idle(app)


def prepare(app, source, verified, work):
    sha = verified['source']['sha']
    kind = app['kind']
    work = Path(work)
    if kind == 'compose':
        archive = work / 'image-metadata.zip'
        source.artifact(verified, archive, name=app['image_artifact'])
        destination = work / 'image-metadata'
        if unpack_zip(archive, destination, limit=64 * 1024) != {'image-metadata.json'}:
            raise Rejected('CI image metadata inventory differs')
        metadata = read(destination / 'image-metadata.json')
        if (metadata.get('repository') != app['repository'] or metadata.get('revision') != sha
                or metadata.get('image') != app['image'] or not DIGEST.fullmatch(metadata.get('digest', ''))):
            raise Rejected('CI image metadata differs from the verified source')
        ref = app['image'] + '@' + metadata['digest']
        run(['docker', 'pull', ref], timeout=300)
        image = json.loads(run(['docker', 'image', 'inspect', ref]))[0]
        labels = image.get('Config', {}).get('Labels') or {}
        if (labels.get('org.opencontainers.image.revision') != sha
                or labels.get('org.opencontainers.image.source') != 'https://github.com/' + app['repository']):
            raise Rejected('Published image source differs from the verified build')
        refs = [r for r in image.get('RepoDigests', []) if r.startswith(app['image'] + '@sha256:')]
        if refs != [ref]:
            raise Rejected('Published image digest differs from the CI evidence')
        return {'path': refs[0], 'sha256': refs[0].split('@sha256:')[1]}
    if kind == 'native':
        archive = work / app['archive'].replace('{sha}', sha)
        try:
            checksum = source.release(sha, archive)
        except Deferred:
            if verified['route'] != 'backup' or not app.get('artifact'):
                raise
            zipfile = work / 'artifact.zip'
            source.artifact(verified, zipfile)
            fallback = work / 'artifact'
            unpack_zip(zipfile, fallback)
            matches = list(fallback.rglob(archive.name))
            sums = list(fallback.rglob('SHA256SUMS'))
            if len(matches) != 1 or len(sums) != 1:
                raise Rejected('Preserved native artifact is incomplete')
            checksum = digest(matches[0])
            if sums[0].read_text().split() != [checksum, archive.name]:
                raise Rejected('Preserved native artifact checksum differs')
            shutil.copy2(matches[0], archive)
        # Use the already installed and reviewed installer. It stages and verifies
        # without switching current; the transaction owns activation below.
        installer = Path(app['root']) / 'current/scripts/install.py'
        run(['python3', '-B', installer, '--archive', archive, '--sha', sha,
             '--sha256', checksum, '--root', app['root']], timeout=120)
        path = Path(app['root']) / 'releases' / sha
        verify_tree(path, sha, app['repository'])
        return {'path': str(path), 'sha256': checksum}
    zipfile = work / 'artifact.zip'
    checksum = source.artifact(verified, zipfile)
    files = work / 'artifact'
    unpack_zip(zipfile, files)
    if kind == 'binary':
        binaries = [p for p in files.rglob(app['binary']) if p.is_file()]
        if len(binaries) != 1 or binaries[0].read_bytes()[:4] != b'\x7fELF':
            raise Rejected('CI artifact does not contain exactly one Linux executable')
        binaries[0].chmod(0o755)
        return {'path': str(binaries[0]), 'sha256': checksum, 'binary_sha256': digest(binaries[0])}
    if kind == 'console':
        name = 'schedule-console-' + sha + '.tar.gz'
        archives = list(files.rglob(name))
        sums = list(files.rglob('schedule-console-SHA256SUMS'))
        if len(archives) != 1 or len(sums) != 1 or sums[0].read_text().split() != [digest(archives[0]), name]:
            raise Rejected('Console artifact checksum differs from the verified build')
        payload = work / 'payload'
        unpack_tar(archives[0], payload, prefix='schedule-console', limit=16 * 1024**2)
        names = runtime_files(payload)
        tracked = source.git('ls-tree', '-r', '--name-only', sha, '--', 'tasks/schedule-console').splitlines()
        expected = sorted(name.removeprefix('tasks/schedule-console/') for name in tracked
                          if (name.removeprefix('tasks/schedule-console/').startswith('static/') or
                              '/' not in name.removeprefix('tasks/schedule-console/') and name.endswith('.py')))
        if names != expected or not {'server.py', 'control.py'}.issubset(names):
            raise Rejected('Console runtime inventory differs from the tested Git tree')
        for name in names:
            expected_hash = source.git('rev-parse', sha + ':tasks/schedule-console/' + name)
            actual_hash = run(['git', 'hash-object', payload / name]).strip()
            if expected_hash != actual_hash:
                raise Rejected('Console runtime file differs from tested Git source')
        atomic(payload / 'REVISION', (sha + '\n').encode(), 0o644)
        return {'path': str(payload), 'sha256': checksum, 'files': names,
                'file_hashes': {name: digest(payload / name) for name in names}}
    archives = list(files.rglob('watchdeck-runtime.tar.gz'))
    if len(archives) != 1:
        raise Deferred('Watchdeck CI has not produced a complete runtime package yet')
    payload = work / 'payload'
    unpack_tar(archives[0], payload, prefix='watchdeck', member_limits=WATCHDECK_LARGE_MEMBERS)
    manifest = read(payload / 'release.json', {})
    if (manifest.get('commit') != sha or manifest.get('repository') != app['repository']
            or manifest.get('python') != '3.12' or manifest.get('platform') != 'linux-x86_64'
            or (payload / '.next/BUILD_ID').read_text().strip() != manifest.get('build_id')):
        raise Rejected('Watchdeck runtime metadata differs from the CI revision')
    for name in ('package-lock.json', 'requirements-browser.txt'):
        expected = source.git('show', sha + ':' + name)
        if (payload / name).read_text().strip() != expected:
            raise Rejected('Watchdeck package locks do not match the tested source')
    if not (payload / 'node_modules/next/package.json').is_file() or not (payload / 'vendor-python/patchright').is_dir():
        raise Rejected('Watchdeck runtime dependencies are absent')
    # Existing host tools run the already verified dependencies; no installation.
    run(['node', '--input-type=module', '-e', "import('next').then(()=>import('undici'))"], cwd=payload)
    run(['python3', '-B', '-c', 'import patchright, cryptography'],
        env={**os.environ, 'PYTHONPATH': str(payload / 'vendor-python')})
    if (payload / 'scripts/github_connect.py').is_file():
        if not (payload / 'vendor-python/watchdeck-browser/runtime.json').is_file():
            raise Rejected('Watchdeck application browser is missing from the verified package')
        with (payload / 'vendor-python/watchdeck-browser/chrome').open('rb') as browser:
            if browser.read(4) != b'\x7fELF':
                raise Rejected('Packaged application browser is not a Linux executable')
        # Verify the application-owned browser without importing user state.
        run(['python3', '-B', '-c', 'from browser_runtime import application_browser; import subprocess; subprocess.run([application_browser(), "--version"], check=True)'],
            cwd=payload, env={**os.environ, 'PYTHONPATH': str(payload / 'scripts') + ':' + str(payload / 'vendor-python')})
    return {'path': str(payload), 'sha256': checksum, 'build_id': manifest['build_id']}


def code_backup(app, recovery):
    root = Path(app['root'])
    kind = app['kind']
    if kind == 'binary':
        target = recovery / app['binary']
        shutil.copy2(root / app['binary'], target)
        return {'binary_sha256': digest(target)}
    if kind == 'console':
        names = runtime_files(root)
        for name in [*names, 'REVISION']:
            target = recovery / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target)
        return {'files': names, 'hashes': {name: digest(recovery / name) for name in names}}
    if kind == 'watchdeck':
        # The deployment owner moves active build/dependencies into this recovery
        # directory after stopping the service. No duplicated webpack cache.
        if Path(app['runtime_record']).is_file():
            shutil.copy2(Path(app['runtime_record']), recovery / 'previous-runtime.json')
        return {'build_id': (root / '.next/BUILD_ID').read_text().strip(),
                'components': [name for name in ('.next', 'node_modules', 'vendor-python') if (root / name).exists()]}
    if kind == 'native':
        target = (root / 'current').resolve()
        verify_tree(target, target.name, app['repository'])
        previous = (root / 'previous').resolve() if (root / 'previous').exists() else target
        verify_tree(previous, previous.name, app['repository'])
        return {'current': str(target), 'previous': str(previous)}
    shutil.copy2(root / '.env', recovery / 'previous.env')
    return {'container': container(app), 'env_sha256': digest(recovery / 'previous.env')}


def compose(app, *args):
    # Same canonical Compose settings and deployment lock as compose-release.sh;
    # the caller holds that lock over env update, replacement and acceptance.
    environment = {k: v for k, v in os.environ.items() if k not in
                   ('COMPOSE_FILE', 'COMPOSE_PROJECT_NAME', 'COMPOSE_PROFILES', app['image_variable'])}
    return run(['docker', '--host', 'unix:///var/run/docker.sock', 'compose', '--env-file', '.env',
                '-f', 'docker-compose.yml', *args], cwd=app['root'], env=environment, timeout=240)


def set_image(app, reference):
    path = Path(app['root']) / '.env'
    text = path.read_text()
    key = app['image_variable']
    lines = text.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.startswith(key + '=')]
    if len(matches) != 1:
        raise Rejected('Compose image setting is missing or ambiguous')
    lines[matches[0]] = key + '=' + reference + '\n'
    mode = path.stat().st_mode & 0o777
    atomic(path, ''.join(lines).encode(), mode)


def apply_code(app, prepared, recovery, source, sha):
    root, payload = Path(app['root']), Path(prepared['path'])
    kind = app['kind']
    if kind == 'native':
        switch(root, 'previous', (root / 'current').resolve())
        switch(root, 'current', payload)
    elif kind == 'binary':
        atomic(root / app['binary'], payload.read_bytes(), 0o755)
    elif kind == 'console':
        for name in [*prepared['files'], 'REVISION']:
            atomic(root / name, (payload / name).read_bytes(), 0o644)
        for name in set(runtime_files(root)) - set(prepared['files']):
            (root / name).unlink()
    elif kind == 'watchdeck':
        # Move only disposable runtime components. data/, .env and the existing
        # venv/browser installations stay in place throughout forward/rollback.
        for name in ('.next', 'node_modules', 'vendor-python'):
            if (root / name).exists():
                os.rename(root / name, recovery / name)
            os.rename(payload / name, root / name)
        run(['git', '-C', root, 'fetch', '--quiet', str(source.cache), sha])
        run(['git', '-C', root, 'merge', '--ff-only', sha])
        atomic(Path(app['runtime_record']), (payload / 'release.json').read_bytes())
    else:
        set_image(app, prepared['path'])
        compose(app, 'up', '-d', '--no-build', '--pull', 'never', '--no-deps', app['compose_service'])


def restore_code(app, recovery, record):
    root, recovery = Path(app['root']), Path(recovery)
    kind = app['kind']
    if kind == 'native':
        old = Path(record['code']['current'])
        verify_tree(old, old.name, app['repository'])
        switch(root, 'current', old)
        switch(root, 'previous', Path(record['code']['previous']))
    elif kind == 'binary':
        old = recovery / app['binary']
        if digest(old) != record['code']['binary_sha256']:
            raise Rejected('Previous executable changed')
        atomic(root / app['binary'], old.read_bytes(), 0o755)
    elif kind == 'console':
        for name in [*record['code']['files'], 'REVISION']:
            atomic(root / name, (recovery / name).read_bytes(), 0o644)
        for name in set(runtime_files(root)) - set(record['code']['files']):
            (root / name).unlink()
    elif kind == 'watchdeck':
        # Restrict reversal to the deployment's recorded clean predecessor.
        # Changes were a fast-forward from a recorded clean branch. Restore only
        # this transaction's branch/head, not another worker's later commit.
        head = run(['git', '-C', root, 'rev-parse', 'HEAD']).strip()
        if head not in (record['previous_sha'], record['source_sha']):
            raise Rejected('Another writer changed Watchdeck during deployment')
        run(['git', '-C', root, 'reset', '--keep', record['previous_sha']])
        for name in ('.next', 'node_modules', 'vendor-python'):
            previous = recovery / name
            if previous.exists():
                failed = recovery / ('failed-' + name.lstrip('.'))
                if (root / name).exists():
                    if failed.exists():
                        failed = recovery / (failed.name + '-' + uuid.uuid4().hex[:8])
                    os.rename(root / name, failed)
                os.rename(previous, root / name)
            elif name not in record['code']['components'] and (root / name).exists():
                os.rename(root / name, recovery / ('failed-' + name.lstrip('.') + '-' + uuid.uuid4().hex[:8]))
        if (recovery / 'previous-runtime.json').exists():
            atomic(Path(app['runtime_record']), (recovery / 'previous-runtime.json').read_bytes())
        else:
            Path(app['runtime_record']).unlink(missing_ok=True)
    else:
        previous_env = recovery / 'previous.env'
        if digest(previous_env) != record['code']['env_sha256']:
            raise Rejected('Previous Compose settings changed')
        atomic(root / '.env', previous_env.read_bytes(), previous_env.stat().st_mode & 0o777)
        set_image(app, record['code']['container']['reference'])
        compose(app, 'up', '-d', '--no-build', '--pull', 'never', '--no-deps', app['compose_service'])


def deploy(app, prepared, verified, source, directory, expected):
    directory = Path(directory)
    sha = verified['source']['sha']
    # Retain one complete predecessor; no cleanup of pre-existing manual backups.
    recovery = directory / ('recovery-' + sha + '-' + uuid.uuid4().hex[:8])
    with locks(deploy_locks(app)):
        preflight(app, expected)
        recovery.mkdir(mode=0o700)
        record = {'state': 'prepared', 'source_sha': sha, 'previous_sha': expected,
                  'started_at': now(), 'recovery': str(recovery), 'artifact_sha256': prepared['sha256'],
                  'code': code_backup(app, recovery), 'ci': {k: v for k, v in verified.items() if k != 'artifacts'}}
        if app['kind'] == 'binary':
            record['binary_sha256'] = prepared['binary_sha256']
        write_json(directory / 'transaction.json', record)
        stop = lambda: compose(app, 'stop', app['compose_service']) if app['kind'] == 'compose' else systemctl('stop', list(reversed(app['units'])))
        start = lambda: None if app['kind'] == 'compose' else systemctl('start', app['units'])
        try:
            record['state'] = 'stopping'
            write_json(directory / 'transaction.json', record)
            stop()
            record['databases'] = snapshot_databases(app, recovery / 'databases')
            record['data_directories'] = snapshot_directories(app, recovery / 'data')
            record['state'] = 'switching'
            record['switch_started'] = True
            write_json(directory / 'transaction.json', record)
            apply_code(app, prepared, recovery, source, sha)
            start()
            probe(app, attempts=45)
            if app['kind'] == 'binary':
                if digest(Path(app['root']) / app['binary']) != prepared['binary_sha256']:
                    raise Rejected('Installed executable differs from CI')
            elif current(app) != sha:
                raise Rejected('Running source revision differs from the accepted build')
            if app['kind'] == 'watchdeck' and (Path(app['root']) / '.next/BUILD_ID').read_text().strip() != prepared['build_id']:
                raise Rejected('Watchdeck build ID differs from CI')
            if app['kind'] == 'console':
                if runtime_files(app['root']) != prepared['files'] or any(
                        digest(Path(app['root']) / name) != value for name, value in prepared['file_hashes'].items()):
                    raise Rejected('Console installed file inventory differs from CI')
            if app['kind'] == 'native':
                for unit in app['units']:
                    pid = run(['systemctl', 'show', unit, '-p', 'MainPID', '--value']).strip()
                    if Path('/proc', pid, 'cwd').resolve() != Path(prepared['path']):
                        raise Rejected('Service still runs a previous release')
            if app['kind'] == 'compose':
                after = container(app)
                before = record['code']['container']
                if any(after[k] != before[k] for k in ('mounts', 'networks', 'ports')):
                    raise Rejected('Compose changed existing data mounts, network or ports')
            for copy in record['databases']:
                if digest(copy['backup']) != copy['sha256']:
                    raise Rejected('Recovery database changed before activation was accepted')
            for copy in record['data_directories']:
                if any(digest(Path(copy['backup']) / name) != value for name, value in copy['files'].items()):
                    raise Rejected('Recovery data changed before activation was accepted')
        except BaseException as error:
            try:
                stop()
                if record['state'] == 'switching':
                    restore_code(app, recovery, record)
                elif app['kind'] == 'compose':
                    compose(app, 'start', app['compose_service'])
                start()
                probe(app, attempts=30)
                record.update(state='rolled_back', finished_at=now())
            except BaseException:
                record.update(state='rollback_failed', finished_at=now())
                write_json(directory / 'transaction.json', record)
                raise Rejected('Deployment and recovery failed; operator action required') from None
            write_json(directory / 'transaction.json', record)
            write_json(recovery / 'result.json', {**record, 'project': app['id']})
            retire_completed_recoveries(app, directory, record)
            if not record.get('switch_started') and isinstance(error, Deferred):
                shutil.rmtree(recovery)
                record['recovery_discarded'] = True
                write_json(directory / 'transaction.json', record)
                raise Deferred('Service transition was busy; predecessor restored') from None
            raise Rejected('Candidate failed acceptance; previous code restored with live data preserved') from None
        # Acceptance is the commit boundary. Metadata/retention failures must
        # never stop an accepted service or claim its code was rolled back.
        record.update(state='success', finished_at=now())
        write_json(directory / 'transaction.json', record)
        write_json(recovery / 'result.json', {**record, 'project': app['id']})
        finalize(app, directory, record)
        return record


def finalize(app, directory, record):
    """Idempotent bookkeeping after acceptance, also replayed after a crash."""
    directory = Path(directory)
    if record.get('bookkeeping_complete'):
        retry_retention(directory, record['recovery'])
        return
    matches = (digest(Path(app['root']) / app['binary']) == record['binary_sha256']
               if app['kind'] == 'binary' else current(app) == record['source_sha'])
    if not matches:
        record.update(state='superseded', bookkeeping_complete=True)
        write_json(directory / 'transaction.json', record)
        return
    if app['kind'] in ('native', 'binary'):
        deployment = Path(app['root']) / 'deployment.json'
        previous = read(deployment, {})
        if previous.get('source_sha') != record['source_sha'] and not (Path(record['recovery']) / 'previous-deployment.json').exists():
            write_json(Path(record['recovery']) / 'previous-deployment.json', previous)
        write_json(deployment, {'source_repository': app.get('repository'),
                   'source_sha': record['source_sha'], 'conclusion': 'success',
                   'source_run_id': record['ci']['source']['run_id'],
                   'verified_run_id': record['ci']['builder']['run_id'],
                   'archive_sha256': record['artifact_sha256'], 'verified_at': record['finished_at'],
                   'recovery_group': record['recovery'], 'previous_sha': record['previous_sha']})
    old = read(directory / 'deployed.json', {})
    write_json(directory / 'deployed.json', record)
    old_recovery = Path(old.get('recovery', '/nonexistent'))
    if old_recovery.parent == directory and str(old_recovery) != record['recovery'] and old.get('state') == 'success':
        try:
            shutil.rmtree(old_recovery)
        except OSError:
            # Keep the old recovery pointer so the next poll can retry cleanup.
            write_json(directory / 'retention-pending.json', {'path': str(old_recovery)})
    record['bookkeeping_complete'] = True
    write_json(directory / 'transaction.json', record)
    retry_retention(directory, record['recovery'])
    retire_completed_recoveries(app, directory, record)


def retire_completed_recoveries(app, directory, record):
    """Retire only marked completed copies after a verified replacement exists."""
    if app['kind'] != 'compose' or not record.get('data_directories'):
        return
    directory = Path(directory)
    current = Path(record['recovery'])
    try:
        if digest(current / 'previous.env') != record['code']['env_sha256']:
            return
        for copy in record['data_directories']:
            if any(digest(Path(copy['backup']) / name) != value for name, value in copy['files'].items()):
                return
        deployed = Path(read(directory / 'deployed.json', {}).get('recovery', '/nonexistent'))
        for path in directory.glob('recovery-*'):
            if path in (current, deployed) or path.is_symlink() or not path.is_dir():
                continue
            previous = read(path / 'result.json', {})
            if (previous.get('project') != app['id'] or previous.get('recovery') != str(path)
                    or previous.get('state') not in ('success', 'rolled_back')):
                continue
            if record['state'] == 'rolled_back' and (previous['state'] != 'rolled_back'
                    or previous.get('previous_sha') != record['previous_sha']):
                continue
            shutil.rmtree(path)
    except (OSError, Rejected, KeyError):
        # Retention never stops an accepted/restored service; retry next release.
        return


def retry_retention(directory, current_recovery):
    pending = read(directory / 'retention-pending.json', {})
    path = Path(pending.get('path', '/nonexistent'))
    if path.parent == directory and str(path) != current_recovery:
        try:
            if path.exists():
                shutil.rmtree(path)
            (directory / 'retention-pending.json').unlink(missing_ok=True)
        except OSError:
            pass


def deploy_locks(app):
    return [*app['locks'], *([str(Path(app['root']) / '.install.lock')] if app['kind'] == 'native' else [])]


def recover(app, directory, *, retry=False):
    """Recover a durable interrupted transaction before considering a new build."""
    directory = Path(directory)
    with locks(deploy_locks(app)):
        record = read(directory / 'transaction.json', {})
        state = record.get('state')
        if state == 'success':
            finalize(app, directory, record)
            return state
        if state not in ('stopping', 'switching') and not (retry and state == 'rollback_failed'):
            return state
        recovery = Path(record['recovery'])
        if recovery.parent != directory or not recovery.is_dir():
            raise Rejected('Interrupted deployment recovery directory is missing')
        try:
            if app['kind'] == 'compose':
                compose(app, 'stop', app['compose_service'])
            else:
                systemctl('stop', list(reversed(app['units'])))
            if state == 'switching' or state == 'rollback_failed' and record.get('switch_started'):
                restore_code(app, recovery, record)
            if app['kind'] == 'compose' and state == 'stopping':
                compose(app, 'start', app['compose_service'])
            elif app['kind'] != 'compose':
                systemctl('start', app['units'])
            probe(app, attempts=30)
            record.update(state='rolled_back', finished_at=now())
            write_json(directory / 'transaction.json', record)
            write_json(directory / 'failed.json', {'sha': record['source_sha'],
                       'reason': 'Interrupted deployment recovered; retry explicitly', 'at': now()})
            return 'rolled_back'
        except BaseException:
            record.update(state='rollback_failed', finished_at=now())
            write_json(directory / 'transaction.json', record)
            raise Rejected('Interrupted deployment could not recover; operator action required') from None
