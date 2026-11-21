#!/usr/bin/env python3
"""Build the small updater artifact only on a GitHub-hosted runner."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sha = os.environ.get('GITHUB_SHA', '')
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or not re.fullmatch('[a-f0-9]{40}', sha)):
        raise SystemExit('Packaging requires an exact GitHub-hosted CI revision')
    root = Path(__file__).resolve().parent
    with tarfile.open(args.output, 'w:gz') as archive:
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root)
            if any(part in ('tests', '__pycache__', '.pytest_cache') for part in relative.parts):
                continue
            if path.is_symlink():
                raise SystemExit('Updater source cannot contain symlinks')
            if path.is_file() and path.name != 'REVISION':
                archive.add(path, arcname='auto-update/' + str(relative), recursive=False)
        value = (sha + '\n').encode()
        item = tarfile.TarInfo('auto-update/REVISION')
        item.size, item.mode = len(value), 0o644
        archive.addfile(item, io.BytesIO(value))


if __name__ == '__main__':
    main()
