"""Installer request proofs use the same fail-closed path as the watcher."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1]
NAMES = ('common', 'sources', 'adapters', 'updater', 'watch', 'browser_drain', 'ci_router')
previous = {name: sys.modules.pop(name, None) for name in NAMES}
sys.path.insert(0, str(HERE))
try:
    spec = importlib.util.spec_from_file_location('project_install_routed', HERE / 'install.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
finally:
    sys.path.pop(0)
    for name, value in previous.items():
        sys.modules.pop(name, None)
        if value is not None:
            sys.modules[name] = value


class InstallerProofTests(unittest.TestCase):
    def test_request_keeps_exact_identity_and_does_not_use_old_reader(self):
        source = Mock()
        request, sha = 'request-11111111-1111-4111-8111-111111111111', 'a' * 40
        with patch.object(installer.evidence, 'verified_run') as native:
            self.assertIs(installer.verified_installation(source, request, sha), source.verified.return_value)
            source.verified.assert_called_once_with({'id': request, 'router_request': request, 'head_sha': sha})
            native.assert_not_called()
            source.verified.side_effect = RuntimeError('proof unavailable')
            with self.assertRaisesRegex(RuntimeError, 'proof unavailable'):
                installer.verified_installation(source, request, sha)
            native.assert_not_called()

    def test_numeric_run_keeps_standalone_proof(self):
        source = Mock()
        with patch.object(installer.evidence, 'verified_run') as native:
            self.assertIs(installer.verified_installation(source, 123, 'a' * 40), native.return_value)
            native.assert_called_once_with(source.github, installer.PROJECT['repository'], 123,
                installer.PROJECT['workflow'], 'refs/heads/' + installer.PROJECT.get('branch', 'main'), 'a' * 40, installer.PROJECT.get('required_jobs', []))
            source.verified.assert_not_called()


if __name__ == '__main__':
    unittest.main()
