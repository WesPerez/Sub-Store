#!/usr/bin/env python3
"""Upload CI ZIPs to a private draft Release on the actual GitHub-hosted builder.

Vendored unchanged by consuming projects. Never uses a primary-account publisher
token: a standby run writes only to its own disposable repository.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile


class UploadError(RuntimeError):
    pass


class MissingInput(UploadError):
    pass


def require(condition, message):
    if not condition:
        raise UploadError(message)


class GitHub:
    def __init__(self, token):
        require(bool(token), 'The builder GITHUB_TOKEN is required')
        self.env = {**os.environ, 'GH_TOKEN': token, 'GH_HOST': 'github.com', 'GH_PROMPT_DISABLED': '1'}
        for name in ('GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
            self.env.pop(name, None)

    def api(self, path, method='GET', payload=None, *, optional=False):
        args = ['gh', 'api', '--hostname', 'github.com', '--method', method, path]
        if payload is not None:
            args += ['--input', '-']
        result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                                text=True, capture_output=True, env=self.env, timeout=90)
        if result.returncode:
            status = re.search(r'\(HTTP (\d{3})\)', result.stderr)
            code = int(status[1]) if status else None
            if optional and code == 404:
                return None
            raise UploadError(f'GitHub {method} {path} failed (HTTP {code or "unknown"})')
        return json.loads(result.stdout) if result.stdout.strip() else None

    def assets(self, repository, release_id):
        rows = []
        for page in range(1, 12):
            batch = self.api(f'repos/{repository}/releases/{release_id}/assets?per_page=100&page={page}')
            require(isinstance(batch, list), 'Invalid release asset inventory')
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise UploadError('Release asset inventory exceeds the limit')

    def upload(self, repository, tag, path):
        result = subprocess.run(['gh', 'release', 'upload', tag, str(path), '--repo', repository],
                                env=self.env, capture_output=True, timeout=600)
        # A lost response can still mean the upload succeeded. The caller always
        # re-reads size and SHA-256; never overwrite an existing asset on retry.
        return result.returncode


def context(env, github):
    require(env.get('GITHUB_ACTIONS') == 'true' and env.get('RUNNER_ENVIRONMENT') == 'github-hosted',
            'Packaging and uploading require a GitHub-hosted runner')
    repository, sha = env.get('GITHUB_REPOSITORY', ''), env.get('GITHUB_SHA', '')
    require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            and re.fullmatch(r'[a-f0-9]{40}', sha), 'Invalid builder identity')
    require(re.fullmatch(r'[1-9][0-9]*', env.get('GITHUB_RUN_ID', ''))
            and re.fullmatch(r'[1-9][0-9]*', env.get('GITHUB_RUN_ATTEMPT', '')),
            'An exact run and attempt are required')
    run_id, attempt = int(env['GITHUB_RUN_ID']), int(env['GITHUB_RUN_ATTEMPT'])
    run = github.api(f'repos/{repository}/actions/runs/{run_id}')
    require(run['id'] == run_id and run['run_attempt'] == attempt and run['head_sha'] == sha
            and run['repository']['full_name'] == repository
            and str(run['repository']['id']) == env.get('GITHUB_REPOSITORY_ID'),
            'GitHub run differs from the builder environment')
    require(run.get('event') in ('push', 'workflow_dispatch'),
            'Pull-request previews use their read-only Actions artifact path')
    return {'schema_version': 1, 'kind': 'ci-release-artifacts', 'repository': repository,
            'repository_id': run['repository']['id'], 'run_id': run_id,
            'run_attempt': attempt, 'sha': sha, 'workflow_path': run['path']}


def find_release(github, repository, tag):
    # GET /releases/tags/<tag> excludes drafts. Authenticated list includes them.
    matches = []
    for page in range(1, 101):
        batch = github.api(f'repos/{repository}/releases?per_page=100&page={page}')
        require(isinstance(batch, list), 'Invalid release inventory')
        matches.extend(item for item in batch if item.get('tag_name') == tag)
        if len(batch) < 100:
            require(len(matches) <= 1, 'Multiple drafts own this CI tag')
            return matches[0] if matches else None
    raise UploadError('Release inventory exceeds the lookup limit')


def verified_release(github, proof, release_id=None):
    repository = proof['repository']
    tag = f"ci-artifacts-{proof['run_id']}-{proof['run_attempt']}"
    if release_id is not None:
        require(isinstance(release_id, int) and not isinstance(release_id, bool) and release_id > 0,
                'Invalid prepared release ID')
        release = github.api(f'repos/{repository}/releases/{release_id}')
        require(isinstance(release, dict), 'Prepared release is no longer available')
    else:
        release = find_release(github, repository, tag)
    if release is None:
        try:
            release = github.api(f'repos/{repository}/releases', 'POST', {
                'tag_name': tag, 'target_commitish': proof['sha'], 'draft': True, 'prerelease': True,
                'name': f"CI artifacts: {proof['sha'][:12]} (run {proof['run_id']})",
                'body': json.dumps(proof, sort_keys=True),
            })
        except UploadError:
            # Matrix jobs can race to create the same run-bound draft. Reuse only
            # an exact identity match; unrelated drafts and reruns are rejected.
            release = find_release(github, repository, tag)
            if release is None:
                raise
    require(release.get('draft') is True and release.get('prerelease') is True
            and release.get('target_commitish') == proof['sha'] and release.get('tag_name') == tag,
            'An unrelated release owns this CI tag')
    require(json.loads(release.get('body', '')) == proof, 'CI release identity changed')
    return release


def files(paths):
    result = []
    paths = [Path(text).absolute() for text in paths]
    for path in paths:
        if not path.exists():
            raise MissingInput('Artifact input is missing')
        require(not path.is_symlink(), 'Artifact input is a symlink')
    if not paths:
        raise MissingInput('Artifact has no files')
    root = Path(os.path.commonpath([str(path.parent if path.is_file() else path) for path in paths]))
    for path in paths:
        if path.is_file():
            result.append((path, path.relative_to(root).as_posix()))
        else:
            for member in sorted(path.rglob('*')):
                require(not member.is_symlink(), 'Artifact inputs cannot contain symlinks')
                if member.is_file():
                    result.append((member, member.relative_to(root).as_posix()))
    if not result:
        raise MissingInput('Artifact has no files')
    names = [name for _, name in result]
    require(len(set(names)) == len(names), 'Artifact filenames collide')
    return result


def publish(github, proof, name, paths, release_id=None):
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}', name), 'Invalid artifact name')
    inputs = files(paths)
    with tempfile.TemporaryDirectory(prefix='ci-release-artifact-') as temporary:
        archive = Path(temporary) / (name + '.zip')
        # Avoid recompressing already compressed runtime archives. ZIP preserves
        # the existing artifact consumer contract and needs no extra libraries.
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as output:
            for path, filename in inputs:
                output.write(path, filename)
        size = archive.stat().st_size
        require(0 < size <= 512 * 1024**2, 'Artifact exceeds the verified delivery budget (512 MiB)')
        with archive.open('rb') as stream:
            expected = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
        release = verified_release(github, proof, release_id)
        repository, tag = proof['repository'], release['tag_name']
        current = [a for a in github.assets(repository, release['id']) if a['name'] == archive.name]
        if not current:
            github.upload(repository, tag, archive)
            current = [a for a in github.assets(repository, release['id']) if a['name'] == archive.name]
        require(len(current) == 1 and current[0].get('state') == 'uploaded'
                and current[0].get('digest') == expected and current[0].get('size') == size,
                'Release upload is missing, incomplete or has a different SHA-256')
        return {'name': name, 'size': size, 'digest': expected, 'asset_id': current[0]['id'],
                'repository': repository, 'release_id': release['id'], 'tag': tag}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', required=True)
    parser.add_argument('--path', action='append', required=True)
    parser.add_argument('--if-no-files-found', choices=('error', 'ignore'), default='error')
    parser.add_argument('--release-id', type=int, help='Exact draft ID prepared once before matrix jobs')
    args = parser.parse_args()
    try:
        # Check the host boundary before reading a credential or creating files.
        require(os.environ.get('GITHUB_ACTIONS') == 'true'
                and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted',
                'This command is only for GitHub-hosted CI')
        github = GitHub(os.environ.get('GITHUB_TOKEN'))
        proof = context(os.environ, github)
        paths = args.path
        if args.if_no_files_found == 'ignore':
            paths = [path for path in paths if Path(path).exists()]
        print(json.dumps(publish(github, proof, args.name, paths, args.release_id), sort_keys=True))
    except MissingInput as error:
        if args.if_no_files_found != 'ignore':
            parser.exit(1, 'CI release artifact failed: ' + str(error) + '\n')
        print('No optional diagnostic files to upload')
    except (UploadError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        parser.exit(1, 'CI release artifact failed: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
