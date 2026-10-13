"""Synthetic filesystem and service failures; never calls production APIs."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
NAMES = ('common', 'sources', 'adapters', 'updater', 'watch')
previous = {name: sys.modules.pop(name, None) for name in NAMES}
import common
import updater
import watch
sys.path.pop(0)
for name, value in previous.items():
    sys.modules.pop(name, None)
    if value is not None:
        sys.modules[name] = value

A, B = 'a' * 40, 'b' * 40


class ProjectIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = json.loads((HERE / 'config.example.json').read_text())
        self.config['state_root'] = str(self.root / 'state')
        self.path = self.root / 'config.json'

    def load(self):
        self.path.write_text(json.dumps(self.config))
        return watch.load_config(self.path)

    def test_own_project_is_accepted(self):
        self.assertEqual(self.load()['application']['id'], common.PROJECT['id'])

    def test_another_repository_or_project_is_rejected(self):
        for key, value in (('repository', 'other/project'), ('id', 'other'), ('kind', 'unknown')):
            with self.subTest(key=key):
                original = self.config['application'][key]
                self.config['application'][key] = value
                with self.assertRaises(common.Rejected):
                    self.load()
                self.config['application'][key] = original

    def test_multi_application_config_is_rejected(self):
        self.config['applications'] = [self.config['application']]
        with self.assertRaises(common.Rejected):
            self.load()

    def test_required_ci_jobs_cannot_be_bypassed(self):
        self.config['application']['required_jobs'] = []
        self.assertEqual(self.load()['application']['required_jobs'], common.PROJECT.get('required_jobs', []))

    def test_project_metadata_can_evolve_without_changing_ownership(self):
        project = {**common.PROJECT, 'format_note': 'new metadata', 'required_jobs': ['new-job']}
        self.assertTrue(updater.same_project(project))
        project['repository'] = 'other/project'
        self.assertFalse(updater.same_project(project))

    def test_existing_recovery_root_can_be_preserved(self):
        saved = self.root / 'legacy' / common.PROJECT['id'] / 'transaction.json'
        saved.parent.mkdir(parents=True)
        saved.write_text('{"state":"success"}')
        self.config['state_root'] = str(self.root / 'legacy')
        self.load()
        self.assertEqual(saved.read_text(), '{"state":"success"}')


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base, self.units = self.root / 'engine', self.root / 'units'
        self.units.mkdir()
        self.command = self.root / 'bin/project-auto-update'
        self.config = self.root / 'etc/config.json'
        self.config.parent.mkdir()
        self.config.write_text('{"host_specific":"preserved"}')
        self.payload = self.root / 'payload'
        (self.payload / 'systemd').mkdir(parents=True)
        (self.payload / 'project.json').write_text(json.dumps(common.PROJECT))
        (self.payload / 'REVISION').write_text(B + '\n')
        (self.payload / 'watch.py').write_text('# tested script\n')
        for suffix in ('.service', '.timer'):
            (self.payload / 'systemd' / (updater.UNIT + suffix)).write_text(updater.MARKER + '\n# new\n')
        old = self.base / 'releases' / A
        old.mkdir(parents=True)
        (old / 'REVISION').write_text(A + '\n')
        (old / 'project.json').write_text(json.dumps(common.PROJECT))
        (self.base / 'current').symlink_to(old)
        self.prepared = {'path': self.payload, 'sha': B, 'sha256': 'd' * 64}
        self.verified = {'source': {'sha': B, 'repository': common.PROJECT['repository']},
                         'builder': {'run_id': 1}, 'route': 'primary'}
        for key, value in {'BASE': self.base, 'COMMAND': self.command, 'CONFIG': self.config, 'UNIT_ROOT': self.units}.items():
            p = patch.object(updater, key, value)
            p.start()
            self.addCleanup(p.stop)

    def test_activation_is_project_scoped_and_preserves_host_settings(self):
        other = self.units / 'another-project-auto-update.timer'
        other.write_text('another project')
        with patch.object(updater, 'run'):
            updater.activate(self.prepared, self.verified)
        self.assertEqual((self.base / 'current').resolve().name, B)
        self.assertEqual((self.base / 'previous').resolve().name, A)
        self.assertEqual(self.config.read_text(), '{"host_specific":"preserved"}')
        self.assertEqual(other.read_text(), 'another project')
        self.assertIn(str(self.base / 'current/watch.py'), self.command.read_text())

    def test_failed_reload_restores_previous_pointer_and_unit_files(self):
        service = self.units / (updater.UNIT + '.service')
        service.write_text(updater.MARKER + '\n# old\n')
        def fail_once(args, **kwargs):
            if args[:2] == ['systemctl', 'daemon-reload'] and (self.base / 'current').resolve().name == B:
                raise common.Deferred('synthetic reload failure')
        with patch.object(updater, 'run', side_effect=fail_once):
            with self.assertRaises(common.Deferred):
                updater.activate(self.prepared, self.verified)
        self.assertEqual((self.base / 'current').resolve().name, A)
        self.assertEqual(service.read_text(), updater.MARKER + '\n# old\n')
        self.assertFalse(self.command.exists())
        self.assertFalse((self.base / 'previous').exists())

    def test_installer_refuses_another_owners_unit(self):
        service = self.units / (updater.UNIT + '.service')
        service.write_text('owned by someone else')
        with patch.object(updater, 'run'):
            with self.assertRaises(common.Rejected):
                updater.activate(self.prepared, self.verified)
        self.assertEqual(service.read_text(), 'owned by someone else')
        self.assertEqual((self.base / 'current').resolve().name, A)

    def test_ci_snapshot_with_same_sha_cannot_be_replaced(self):
        release = self.base / 'releases' / B
        release.mkdir()
        (release / 'watch.py').write_text('different content')
        with patch.object(updater, 'run'):
            with self.assertRaises(common.Rejected):
                updater.activate(self.prepared, self.verified)

    def test_same_sha_reinstallation_repairs_missing_unit(self):
        with patch.object(updater, 'run'):
            updater.activate(self.prepared, self.verified)
            service = self.units / (updater.UNIT + '.service')
            service.unlink()
            updater.activate(self.prepared, self.verified, force=True)
        self.assertTrue(service.is_file())
        self.assertEqual((self.base / 'previous').resolve().name, A)


class IndependentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = {'id': 'synthetic', 'enabled': True, 'root': str(self.root)}

    def test_old_application_without_updater_artifact_remains_current(self):
        source = Mock()
        source.candidate.return_value = None
        with patch.object(watch, 'Source', return_value=source), \
             patch.object(watch.adapters, 'current', return_value=A), \
             patch.object(watch.updater, 'refresh', side_effect=common.Rejected('missing artifact')):
            result = watch.poll_app(self.app, self.root, apply=True)
        self.assertEqual(result['state'], 'current')
        self.assertEqual(result['updater'], 'pending')
        self.assertFalse((self.root / 'synthetic/failed.json').exists())

    def test_updater_download_failure_does_not_quarantine_accepted_application(self):
        source = Mock()
        source.candidate.return_value = {'head_sha': B, 'id': 2}
        source.cache = self.root / 'source.git'
        source.verified.return_value = {'source': {'sha': B}, 'route': 'primary'}
        with patch.object(watch, 'Source', return_value=source), \
             patch.object(watch.adapters, 'current', return_value=A), \
             patch.object(watch.adapters, 'preflight'), \
             patch.object(watch.adapters, 'prepare'), \
             patch.object(watch.adapters, 'deploy') as deploy, \
             patch.object(watch.updater, 'prepare', side_effect=common.Rejected('missing artifact')), \
             patch.object(watch.updater, 'activate'), \
             patch.object(watch.shutil, 'disk_usage', return_value=Mock(free=10 * 1024**3)), \
             patch('subprocess.run', return_value=Mock(returncode=1)):
            result = watch.poll_app(self.app, self.root, apply=True)
        deploy.assert_called_once()
        self.assertEqual(result['state'], 'current')
        self.assertEqual(result['deployed_sha'], B)
        self.assertEqual(result['updater'], 'pending')
        self.assertFalse((self.root / 'synthetic/failed.json').exists())


if __name__ == '__main__':
    unittest.main()
