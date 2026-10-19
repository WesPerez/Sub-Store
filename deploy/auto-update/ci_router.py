"""Read-only bridge for project-owned update watchers; no build or deployment."""
import json
import os
from pathlib import Path
import subprocess


CONTROL = Path('/usr/local/lib/github-ci-fallback')


def candidate(repository, workflow, ref, sha=None):
    if not (CONTROL / 'router_client.py').is_file():
        return None
    args = ['/usr/bin/python3', '-B', str(CONTROL / 'router_client.py'), 'candidate',
            '--repo', repository, '--workflow', workflow, '--ref', ref]
    if sha:
        args += ['--sha', sha]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=45)
    if proc.returncode:
        raise RuntimeError('CI router evidence lookup failed: ' + proc.stderr[-300:])
    return json.loads(proc.stdout)


def verified(repository, workflow, ref, sha, request, required_jobs=()):
    args = ['/usr/bin/python3', '-B', str(CONTROL / 'router_client.py'), 'verified',
            '--repo', repository, '--workflow', workflow, '--ref', ref, '--sha', sha,
            '--request', str(request)]
    for job in required_jobs:
        args += ['--require-job', job]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if proc.returncode:
        raise RuntimeError('CI router verification failed: ' + proc.stderr[-300:])
    return json.loads(proc.stdout)
