"""A fork reconstruction must bind the actual predecessor and advancing upstream."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1]
NAMES = ('common', 'sources', 'ci_router')
previous = {name: sys.modules.pop(name, None) for name in NAMES}
sys.path.insert(0, str(HERE))
try:
    import common
    import sources
finally:
    sys.path.pop(0)
    for name, value in previous.items():
        sys.modules.pop(name, None)
        if value is not None:
            sys.modules[name] = value


class UpstreamTransitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '--quiet')
        self.git('config', 'user.email', 'synthetic@example.invalid')
        self.git('config', 'user.name', 'Synthetic fixture')
        self.base = self.commit('upstream base', 'upstream.txt', 'v1')
        self.old = self.commit('old customization', 'custom.txt', 'retained')
        self.git('checkout', '--quiet', '--detach', self.base)
        self.upstream = self.commit('new upstream', 'upstream.txt', 'v2')
        self.transition = {
            'version': 1, 'repository': 'owner/app', 'branch': common.PROJECT['branch'],
            'previous_revision': self.old, 'previous_upstream': self.base,
            'upstream': self.upstream,
        }
        self.source = sources.Source({'repository': 'owner/app', 'workflow': '.github/workflows/build.yml'},
                                     self.root / 'cache', Mock())
        self.source.cache = self.repo / '.git'
        self.source.upstream_head = Mock(return_value=self.upstream)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()

    def commit(self, message, path, body):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        self.git('add', '--', path)
        self.git('commit', '--quiet', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def reconstruct(self, transition=None):
        return self.commit('reconstructed customization', 'deploy/upstream-transition.json',
                           json.dumps(self.transition if transition is None else transition))

    def test_exact_predecessor_and_advancing_upstream_are_accepted(self):
        candidate = self.reconstruct()
        self.assertFalse(self.source.ancestor(self.old, candidate))
        self.assertTrue(self.source.forward(self.old, candidate))
        self.assertTrue(self.source.forward(self.upstream, candidate))

    def test_no_manifest_cannot_authorize_a_rewrite(self):
        self.assertFalse(self.source.forward(self.old, self.upstream))

    def test_other_predecessor_repository_or_branch_is_not_authorized(self):
        for key, value in (('previous_revision', 'f' * 40), ('repository', 'other/app'),
                           ('branch', 'other'), ('version', 2)):
            with self.subTest(key=key):
                candidate = self.reconstruct({**self.transition, key: value})
                self.assertFalse(self.source.forward(self.old, candidate))

    def test_stale_or_unrelated_upstream_cannot_authorize_downgrade(self):
        candidate = self.reconstruct({**self.transition, 'upstream': self.base})
        with self.assertRaises(common.Rejected):
            self.source.forward(self.old, candidate)
        candidate = self.reconstruct({**self.transition, 'previous_upstream': self.old})
        self.assertFalse(self.source.forward(self.old, candidate))

    def test_routed_reconstruction_still_requires_completed_ci_and_exact_verification(self):
        candidate = self.reconstruct()
        self.source.sync = Mock(return_value=candidate)
        row = {'id': 'request-fixture', 'router_request': 'request-fixture',
               'head_sha': candidate, 'status': 'in_progress'}
        with patch.object(sources.ci_router, 'candidate', return_value=row):
            with self.assertRaises(common.Deferred):
                self.source.candidate(self.old)
            row['status'] = 'completed'
            self.assertEqual(self.source.candidate(self.old), row)
        self.source.github.api.assert_not_called()
        with patch.object(sources.ci_router, 'verified', side_effect=RuntimeError('proof rejected')):
            with self.assertRaises(common.Deferred):
                self.source.verified(row)
        self.source.github.api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
