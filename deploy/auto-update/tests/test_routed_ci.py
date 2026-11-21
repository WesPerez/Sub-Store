"""CI routing integration uses exact identities and waits for preservation."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
previous = {name: sys.modules.pop(name, None) for name in ('common', 'sources', 'ci_router')}
import common
import sources
sys.path.pop(0)
for name, value in previous.items():
    sys.modules.pop(name, None)
    if value is not None:
        sys.modules[name] = value

class RoutedCITests(unittest.TestCase):
    def source(self):
        app = {'repository': 'owner/app', 'workflow': '.github/workflows/build.yml', 'required_jobs': ['verify']}
        source = sources.Source(app, '/unused', Mock())
        source.sync = Mock(return_value='b'*40)
        source.ancestor = Mock(return_value=True)
        return source

    def test_routed_head_is_candidate_without_a_primary_run(self):
        source = self.source()
        row = {'id': 'request-token', 'router_request': 'request-token', 'head_sha': 'b'*40,
               'status': 'completed', 'conclusion': 'success'}
        with patch.object(sources.ci_router, 'candidate', return_value=row):
            self.assertEqual(source.candidate('a'*40), row)
        source.github.api.assert_not_called()

    def test_unpreserved_build_remains_deferred(self):
        source = self.source()
        with patch.object(sources.ci_router, 'candidate', return_value={'status': 'in_progress'}):
            with self.assertRaises(common.Deferred):
                source.candidate('a'*40)
        source.github.api.assert_not_called()

    def test_request_verification_keeps_exact_sha_workflow_and_jobs(self):
        source = self.source()
        row = {'router_request': 'request-token', 'head_sha': 'b'*40}
        with patch.object(sources.ci_router, 'verified', return_value={'verified': True}) as verify:
            self.assertEqual(source.verified(row), {'verified': True})
            self.assertEqual(verify.call_args.args[:2], ('owner/app', '.github/workflows/build.yml'))
            self.assertEqual(verify.call_args.args[3:], ('b'*40, 'request-token', ['verify']))
        source.github.api.assert_not_called()
