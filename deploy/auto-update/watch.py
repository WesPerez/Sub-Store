#!/usr/bin/env python3
"""Update only this repository's application after exact hosted CI acceptance."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import adapters
from common import CONFIG, STATE, PROJECT, Deferred, Rejected, locks, now, read, write_json
from sources import Source
import updater


def load_config(path):
    path = Path(path)
    if not path.is_file():
        raise Rejected('Configuration file is missing: ' + str(path))
    if path.is_symlink() or path.stat().st_mode & 0o022 or path.stat().st_uid != os.geteuid():
        raise Rejected('Configuration must be owned by the deployment user and not writable by others')
    config = read(path)
    if config.get('version') != 1 or not isinstance(config.get('application'), dict) or 'applications' in config:
        raise Rejected('Unsupported deployment configuration')
    app = config['application']
    if any(app.get(key) != PROJECT[key] for key in ('id', 'repository', 'kind', 'workflow')):
        raise Rejected('This updater can manage only its own project')
    if (not Path(app.get('root', '')).is_absolute() or not app.get('locks')
            or not app.get('health') or not isinstance(app.get('enabled'), bool)):
        raise Rejected('Application contract is incomplete')
    # Required jobs belong to versioned source, not stale host configuration.
    app['required_jobs'] = PROJECT.get('required_jobs', [])
    state = Path(config.get('state_root', str(STATE)))
    if not state.is_absolute() or state.resolve() != state:
        raise Rejected('State root must be an absolute path without symlinks')
    for filename in app.get('files', []):
        if Path(filename).is_absolute() or '..' in Path(filename).parts:
            raise Rejected('Runtime file escapes the application root')
    if app['kind'] == 'watchdeck':
        app.setdefault('runtime_record', str(state / app['id'] / 'runtime.json'))
    return config


def status(directory, app, state, **detail):
    previous = read(directory / 'status.json', {})
    value = {'application': app['id'], 'state': state, **detail}
    changed = any(previous.get(k) != v for k, v in value.items())
    write_json(directory / 'status.json', {**value, 'checked_at': now()})
    if changed:
        print(json.dumps(value, ensure_ascii=False), flush=True)
    return value


def poll_app(app, state_root, *, apply):
    directory = Path(state_root) / app['id']
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    sha = None
    try:
        with locks([directory / 'poll.lock']):
            if not app['enabled']:
                return status(directory, app, 'disabled')
            transaction = read(directory / 'transaction.json', {})
            if transaction.get('state') in ('stopping', 'switching', 'success') and apply:
                adapters.recover(app, directory)
                transaction = read(directory / 'transaction.json', {})
            if transaction.get('state') in ('stopping', 'switching', 'rollback_failed'):
                return status(directory, app, 'attention', reason='Unfinished deployment requires recovery',
                              candidate_sha=transaction.get('source_sha'))
            current = adapters.current(app)
            source = Source(app, directory)
            candidate = source.candidate(current)
            if candidate is None:
                update = {}
                if apply:
                    try:
                        updater.refresh(source, current, directory)
                    except Exception as error:
                        update = {'updater': 'pending', 'updater_reason': type(error).__name__}
                return status(directory, app, 'current', deployed_sha=current, **update)
            sha = candidate['head_sha']
            failed = read(directory / 'failed.json', {})
            if failed.get('sha') == sha:
                return status(directory, app, 'quarantined', deployed_sha=current, candidate_sha=sha,
                              reason=failed['reason'])
            verified = source.verified(candidate)
            # A project can explicitly pause automatic activation for a release
            # requiring a coordinated data/config migration, without stopping CI.
            import subprocess
            override = subprocess.run(['git', '--git-dir=' + str(source.cache), 'show',
                                       sha + ':deploy/auto-deploy.json'], capture_output=True, text=True, timeout=30)
            if override.returncode == 0:
                policy = json.loads(override.stdout)
                if policy.get('version') != 1 or policy.get('automatic') is not True:
                    raise Deferred('This release declares a coordinated deployment requirement')
            if not apply:
                return status(directory, app, 'ready', deployed_sha=current, candidate_sha=sha,
                              run_id=candidate['id'], route=verified['route'])
            if shutil.disk_usage(app['root']).free < 3 * 1024**3:
                raise Deferred('Deployment needs at least 3 GiB free space')
            adapters.preflight(app, current)
            with tempfile.TemporaryDirectory(prefix='download-', dir=directory) as work:
                # Fetch and validate both deliveries before stopping the app.
                # This runs from an installed immutable snapshot, never a live
                # checkout that application activation may replace.
                update = {}
                try:
                    engine = updater.prepare(source, verified, Path(work) / 'updater')
                except Exception as error:
                    engine = None
                    update = {'updater': 'pending', 'updater_reason': type(error).__name__}
                prepared = adapters.prepare(app, source, verified, work)
                # Re-resolve after downloads: newer commits do not let an old
                # run race a newer main deployment into production.
                latest = source.candidate(current)
                if latest is None or latest['head_sha'] != sha:
                    raise Deferred('Main advanced during download; reconsider next poll')
                adapters.deploy(app, prepared, verified, source, directory, current)
                try:
                    updater.activate(engine, verified)
                except Exception as error:
                    update = {'updater': 'pending', 'updater_reason': type(error).__name__}
            (directory / 'failed.json').unlink(missing_ok=True)
            return status(directory, app, 'current', deployed_sha=sha, run_id=candidate['id'], **update)
    except Deferred as error:
        return status(directory, app, 'waiting', reason=str(error), candidate_sha=sha)
    except Rejected as error:
        if sha:
            write_json(directory / 'failed.json', {'sha': sha, 'reason': str(error), 'at': now()})
        return status(directory, app, 'attention', reason=str(error), candidate_sha=sha)
    except Exception as error:
        # Do not expose GitHub/Compose response bodies, tokens or database data.
        return status(directory, app, 'attention', reason='Updater error: ' + type(error).__name__, candidate_sha=sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('poll', 'status', 'retry', 'check'))
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    config = load_config(args.config)
    app = config['application']
    state = Path(config.get('state_root', str(STATE)))
    if args.command == 'check':
        print(json.dumps({'project': PROJECT['name'], 'configuration': 'valid', 'state_root': str(state)}))
        return
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    if args.command == 'status':
        print(json.dumps(read(state / app['id'] / 'status.json',
                             {'application': app['id'], 'state': 'not_checked'}), ensure_ascii=False, indent=2))
        return
    if args.command == 'retry':
        with locks([state / app['id'] / 'poll.lock']):
            adapters.recover(app, state / app['id'], retry=True)
            (state / app['id'] / 'failed.json').unlink(missing_ok=True)
        return
    result = poll_app(app, state, apply=args.apply)
    if result['state'] in ('attention', 'quarantined'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
