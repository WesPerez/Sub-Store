#!/usr/bin/env python3
"""Publish only the image exercised by hosted CI; bind its digest to the run."""
import json
import os
from pathlib import Path
import re
import subprocess

if (os.environ.get('GITHUB_ACTIONS') != 'true'
        or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
        or os.environ.get('GITHUB_REPOSITORY') != 'WesPerez/Sub-Store'
        or os.environ.get('GITHUB_REF') != 'refs/heads/mine'):
    raise SystemExit('Publishing requires this repository production branch on hosted CI')

sha = os.environ['GITHUB_SHA']
if not re.fullmatch('[a-f0-9]{40}', sha):
    raise SystemExit('Expected a complete Git SHA')
image = 'ghcr.io/wesperez/sub-store'
reference = image + ':sha-' + sha
subprocess.run(['docker', 'push', reference], check=True)
inspected = json.loads(subprocess.check_output(['docker', 'image', 'inspect', reference]))[0]
digests = [value for value in inspected.get('RepoDigests', []) if value.startswith(image + '@sha256:')]
if len(digests) != 1 or not re.fullmatch('sha256:[a-f0-9]{64}', digests[0].split('@')[1]):
    raise SystemExit('Published image digest is absent or ambiguous')
subprocess.run(['docker', 'tag', reference, image + ':mine'], check=True)
subprocess.run(['docker', 'push', image + ':mine'], check=True)
record = {'repository': os.environ['GITHUB_REPOSITORY'], 'revision': sha,
          'image': image, 'digest': digests[0].split('@')[1],
          'frontend_revision': inspected['Config']['Labels']['io.sub-store.frontend.revision']}
Path('image-metadata.json').write_text(json.dumps(record, indent=2) + '\n')
print(json.dumps(record))
