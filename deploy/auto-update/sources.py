"""Select this project's exact production build using bundled CI verification."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import time

from common import PROJECT, Deferred, Rejected, SHA, DIGEST, digest, run

EVIDENCE_PATH = Path(__file__).with_name('ci_evidence.py')
spec = importlib.util.spec_from_file_location('auto_deploy_ci_evidence', EVIDENCE_PATH)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class GitHub(evidence.GitHub):
    def file(self, item, destination, *, limit=512 * 1024**2):
        expected = item.get('digest', '')
        if not DIGEST.fullmatch(expected):
            raise Rejected('CI asset has no SHA-256 digest')
        size = item.get('size', item.get('size_in_bytes'))
        if size is not None and (not isinstance(size, int) or not 0 < size <= limit):
            raise Rejected('CI asset exceeds the download budget')
        path = item['download_path']
        if not path.startswith('repos/') or '..' in path:
            raise Rejected('Invalid GitHub asset path')
        args = ['/usr/bin/gh', 'api', '--hostname', 'github.com', path]
        if not re.fullmatch(r'repos/[^/]+/[^/]+/actions/artifacts/[0-9]+/zip', path):
            args += ['-H', 'Accept: application/octet-stream']
        try:
            with Path(destination).open('xb') as out:
                process = subprocess.Popen(args, stdout=out, stderr=subprocess.DEVNULL,
                                           env={**os.environ, 'GH_PROMPT_DISABLED': '1'})
                deadline = time.monotonic() + 300
                try:
                    while process.poll() is None:
                        if time.monotonic() > deadline or out.tell() > limit:
                            raise Deferred('Asset download exceeded its time/size budget')
                        time.sleep(0.2)
                    if process.returncode:
                        raise Deferred('GitHub asset download failed')
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            if (size is not None and Path(destination).stat().st_size != size
                    or 'sha256:' + digest(destination) != expected):
                raise Rejected('Downloaded CI asset digest or size differs')
        except BaseException:
            Path(destination).unlink(missing_ok=True)
            raise


class Source:
    def __init__(self, app, root, github=None):
        self.app, self.root = app, Path(root)
        self.repo = app['repository']
        self.github = github or GitHub()
        self.cache = self.root / 'source.git'

    def git(self, *args):
        return run(['git', '--git-dir=' + str(self.cache), *args], timeout=120).strip()

    def sync(self):
        if not self.cache.exists():
            run(['git', 'init', '--bare', self.cache])
            self.git('remote', 'add', 'origin', 'https://github.com/' + self.repo + '.git')
        elif self.git('remote', 'get-url', 'origin') != 'https://github.com/' + self.repo + '.git':
            raise Rejected('Source cache belongs to a different repository')
        self.git('fetch', '--quiet', '--no-tags', 'origin',
                 f'+refs/heads/{PROJECT["branch"]}:refs/heads/{PROJECT["branch"]}')
        return self.git('rev-parse', 'refs/heads/' + PROJECT['branch'])

    def ancestor(self, before, after):
        if not SHA.fullmatch(before or '') or not SHA.fullmatch(after or ''):
            raise Rejected('Expected complete deployed and candidate Git revisions')
        for sha in (before, after):
            present = subprocess.run(['git', '--git-dir=' + str(self.cache), 'cat-file', '-e', sha + '^{commit}'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            if present.returncode:
                self.git('fetch', '--quiet', '--no-tags', 'origin', sha)
        result = subprocess.run(['git', '--git-dir=' + str(self.cache), 'merge-base', '--is-ancestor', before, after],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if result.returncode not in (0, 1):
            raise Deferred('Cannot prove the deployed revision ancestry')
        return result.returncode == 0

    def candidate(self, current):
        head = self.sync()
        workflow = self.app['workflow'].split('/')[-1]
        rows = self.github.api(f'repos/{self.repo}/actions/workflows/{workflow}/runs?branch={PROJECT["branch"]}&per_page=50')['workflow_runs']
        rows = [r for r in rows if r.get('event') in ('push', 'workflow_dispatch')
                and r.get('head_branch') == PROJECT['branch'] and r.get('path') == self.app['workflow']
                and SHA.fullmatch(r.get('head_sha', ''))]
        # API lists dispatch time, which is not source order. Never deploy a rerun
        # of an older revision in preference to a newer reachable main commit.
        candidates = [r for r in rows if self.ancestor(r['head_sha'], head)
                      and self.ancestor(current, r['head_sha'])]
        # A successful operations-only run intentionally has no application
        # image. Skip it only when the exact diff proves it cannot affect the
        # application and CI confirms just the configured job was skipped.
        candidates = [r for r in candidates if not self.operations_only(r)]
        if not candidates:
            raise Deferred('Waiting for main CI to include the deployed revision')
        chosen = candidates[0]
        for row in candidates[1:]:
            if chosen['head_sha'] != row['head_sha'] and self.ancestor(chosen['head_sha'], row['head_sha']):
                chosen = row
        if chosen['head_sha'] == current:
            return None
        if chosen['status'] != 'completed':
            raise Deferred('Main CI is still running')
        return chosen

    def operations_only(self, row):
        policy = self.app.get('nonrelease', {})
        if not policy or row.get('status') != 'completed' or row.get('conclusion') != 'success':
            return False
        attempt = row.get('run_attempt', 1)
        jobs = self.github.pages(f'repos/{self.repo}/actions/runs/{row["id"]}/attempts/{attempt}/jobs', 'jobs')
        skipped = {j['name'] for j in jobs if j.get('conclusion') == 'skipped'}
        if skipped != set(policy['skipped_jobs']) or any(j.get('conclusion') not in ('success', 'skipped') for j in jobs):
            return False
        names = self.git('diff', '--no-renames', '--name-only', row['head_sha'] + '^1', row['head_sha']).splitlines()
        return bool(names) and all(name in policy.get('files', []) or any(
            name.startswith(prefix) for prefix in policy.get('prefixes', [])) for name in names)

    def verified(self, candidate):
        try:
            return evidence.verified_run(self.github, self.repo, candidate['id'], self.app['workflow'],
                                         'refs/heads/' + PROJECT['branch'], candidate['head_sha'], self.app.get('required_jobs', []))
        except evidence.EvidenceError as error:
            if evidence.billing_blocked(self.github, self.repo, candidate):
                # Optional host integration. Normal successful CI needs only gh;
                # a fresh server need not install the shared standby controller.
                fallback = Path('/usr/local/sbin/github-ci-fallback')
                if not fallback.is_file() or not os.access(fallback, os.X_OK):
                    raise Deferred('Main CI is billing-blocked; waiting for a successful hosted build') from None
                try:
                    result = subprocess.run([str(fallback), 'ensure', '--repo', self.repo,
                                         '--run-id', str(candidate['id']), '--worktree', str(self.cache)],
                                        capture_output=True, text=True, timeout=150)
                except subprocess.TimeoutExpired:
                    raise Deferred('CI standby request is still being reconciled') from None
                if result.returncode:
                    raise Deferred('CI standby is not ready; inspect gci status') from None
                raise Deferred('CI standby is building or preserving this exact revision') from None
            if candidate.get('conclusion') in ('failure', 'cancelled', 'skipped', 'neutral'):
                raise Deferred('Main CI failed; waiting for a corrected main commit') from None
            raise Rejected('Verified CI contract is incomplete: ' + str(error)) from None

    def artifact(self, verified, destination, *, name=None):
        name = (name or self.app['artifact']).replace('{sha}', verified['source']['sha'])
        try:
            item = evidence.locate_artifact(verified, name)
        except evidence.EvidenceError:
            raise Rejected('The verified runtime artifact is missing, expired or ambiguous') from None
        self.github.file(item, destination)
        return item['digest'].removeprefix('sha256:')

    def release(self, sha, destination):
        tag = 'build-' + sha
        try:
            release = self.github.api(f'repos/{self.repo}/releases/tags/{tag}')
        except (evidence.EvidenceError, ValueError):
            raise Deferred('Waiting for the exact release to be published') from None
        if release.get('draft') or release.get('prerelease') or release.get('target_commitish') != sha:
            raise Rejected('Published release identity does not match CI')
        ref = self.github.api(f'repos/{self.repo}/git/ref/tags/{tag}')['object']
        for _ in range(4):
            if ref['type'] != 'tag':
                break
            ref = self.github.api(f'repos/{self.repo}/git/tags/{ref["sha"]}')['object']
        if ref['type'] != 'commit' or ref['sha'] != sha:
            raise Rejected('Release tag points to another commit')
        name = self.app['archive'].replace('{sha}', sha)
        matches = [a for a in release['assets'] if a['name'] == name]
        sums = [a for a in release['assets'] if a['name'] == 'SHA256SUMS']
        if len(matches) != 1 or len(sums) != 1:
            raise Deferred('Release assets are not ready')
        for item, target in ((matches[0], destination), (sums[0], Path(destination).with_name('SHA256SUMS'))):
            self.github.file({**item, 'download_path': f'repos/{self.repo}/releases/assets/{item["id"]}'}, target)
        checksum = digest(destination)
        if Path(destination).with_name('SHA256SUMS').read_text().split() != [checksum, name]:
            raise Rejected('Published checksum differs from the downloaded archive')
        return checksum
