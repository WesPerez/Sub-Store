#!/usr/bin/env python3
"""Install this repository's updater from its exact production-branch hosted CI run."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from common import CONFIG, STATE, PROJECT, SHA, Rejected, locks, run
from sources import Source, evidence
from watch import load_config
import updater


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha', required=True, help='Complete production-branch Git SHA')
    parser.add_argument('--run-id', required=True, type=int, help='Source repository CI run ID')
    parser.add_argument('--config', type=Path, help='Initial reviewed config; existing host config is preserved')
    parser.add_argument('--enable', action='store_true', help='Enable this project timer')
    args = parser.parse_args()
    if os.geteuid() != 0 or not SHA.fullmatch(args.sha):
        raise SystemExit('Root and a complete Git SHA are required')
    path = args.config or CONFIG
    config = load_config(path)
    if args.config and CONFIG.exists() and CONFIG.read_bytes() != args.config.read_bytes():
        raise SystemExit('Host configuration already exists; update it explicitly')
    state = Path(config.get('state_root', str(STATE)))
    directory = state / PROJECT['id']
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Same lock as the watcher. Installation never interrupts an active deploy.
    with locks([directory / 'poll.lock']):
        with tempfile.TemporaryDirectory(prefix='install-', dir=directory) as temporary:
            source = Source(config['application'], directory)
            head = source.sync()
            if not source.ancestor(args.sha, head):
                raise Rejected('Updater source must be reachable from the production branch')
            previous = updater.installed_sha()
            if previous and not source.ancestor(previous, args.sha):
                raise Rejected('Updater installation cannot move backwards')
            verified = evidence.verified_run(source.github, PROJECT['repository'], args.run_id,
                PROJECT['workflow'], 'refs/heads/' + PROJECT['branch'], args.sha, PROJECT.get('required_jobs', []))
            prepared = updater.prepare(source, verified, Path(temporary) / 'download', force=True)
            updater.activate(prepared, verified, config_source=args.config, force=True)
    if args.enable:
        run(['systemctl', 'enable', '--now', updater.UNIT + '.timer'], timeout=30)
    print(json.dumps({'project': PROJECT['name'], 'installed_sha': args.sha, 'timer_enabled': args.enable}))


if __name__ == '__main__':
    main()
