import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import unittest
from unittest.mock import patch, Mock
import zipfile

HERE = Path(__file__).resolve().parents[1]
# These generic module names also exist in other host tasks. Load this task as
# an isolated package for pytest's combined hosted suite.
sys.path.insert(0, str(HERE))
previous = {name: sys.modules.pop(name, None) for name in ('common', 'sources', 'adapters', 'updater', 'watch')}
import common
import sources
import adapters
import watch
sys.path.pop(0)
for name, value in previous.items():
    sys.modules.pop(name, None)
    if value is not None:
        sys.modules[name] = value

import tempfile

A, B, C = 'a' * 40, 'b' * 40, 'c' * 40


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_zip_rejects_escape_symlink_and_duplicate(self):
        for filename, symlink in (('../escape', False), ('file', True)):
            archive = self.root / ('symlink.zip' if symlink else 'escape.zip')
            with zipfile.ZipFile(archive, 'w') as out:
                info = zipfile.ZipInfo(filename)
                if symlink:
                    info.external_attr = 0o120777 << 16
                out.writestr(info, 'target')
            with self.assertRaises(common.Rejected):
                common.unpack_zip(archive, self.root / 'unpacked')
        self.assertFalse((self.root.parent / 'escape').exists())

    def test_tar_rejects_links_and_unexpected_roots(self):
        for name, link in (('other/file', False), ('app/link', True)):
            archive = self.root / 'bad.tar.gz'
            with tarfile.open(archive, 'w:gz') as out:
                member = tarfile.TarInfo(name)
                if link:
                    member.type = tarfile.SYMTYPE
                    member.linkname = '/etc/passwd'
                    out.addfile(member)
                else:
                    member.size = 1
                    out.addfile(member, io.BytesIO(b'x'))
            with self.assertRaises(common.Rejected):
                common.unpack_tar(archive, self.root / 'payload', prefix='app')

    def test_browser_size_exception_is_exact_and_bounded(self):
        # Simulate member metadata rather than allocating hundreds of MiB.
        for name, size, accepted in [
            ('vendor-python/watchdeck-browser/chrome', 300 * 1024**2, True),
            ('vendor-python/watchdeck-browser/chrome', 513 * 1024**2, False),
            ('vendor-python/other', 300 * 1024**2, False),
        ]:
            member = tarfile.TarInfo('watchdeck/' + name)
            member.size = size
            source = Mock()
            source.__enter__ = Mock(return_value=source)
            source.__exit__ = Mock(return_value=False)
            source.__iter__ = Mock(return_value=iter([member]))
            source.extractfile.return_value = io.BytesIO(b'\x7fELF')
            with patch.object(common.tarfile, 'open', return_value=source):
                target = self.root / ('payload-' + str(size) + '-' + str(accepted) + '-' + Path(name).name)
                if accepted:
                    self.assertEqual(common.unpack_tar('unused', target, prefix='watchdeck',
                        member_limits=common.WATCHDECK_LARGE_MEMBERS), {name})
                else:
                    with self.assertRaises(common.Rejected):
                        common.unpack_tar('unused', target, prefix='watchdeck', member_limits=common.WATCHDECK_LARGE_MEMBERS)

    def test_auxiliary_login_preparation_blocks_deployment_before_worker_starts(self):
        app = {'kind': 'watchdeck', 'root': str(self.root), 'health': {'url': 'http://127.0.0.1:3000/healthz'}}
        with patch.object(adapters, 'run', return_value='{"ok":true,"interactiveLogin":true}'):
            with self.assertRaisesRegex(common.Deferred, 'interactive login'):
                adapters.idle(app)

    def test_temporarily_unavailable_health_defers_without_quarantining_release(self):
        app = {'kind': 'watchdeck', 'root': str(self.root), 'health': {'url': 'http://127.0.0.1:3000/healthz'}}
        for response in ('not json', 'null', '[]', '{"ok":false}'):
            with self.subTest(response=response), patch.object(adapters, 'run', return_value=response):
                with self.assertRaisesRegex(common.Deferred, 'health check is unavailable'):
                    adapters.idle(app)
        with patch.object(adapters, 'run', side_effect=RuntimeError('temporary transport error')):
            with self.assertRaisesRegex(common.Deferred, 'health check is unavailable'):
                adapters.idle(app)

    def test_manifest_rejects_tampering_and_new_files(self):
        root = self.root / A
        root.mkdir()
        (root / 'app.py').write_text('verified')
        common.seal(root, A, 'owner/app', 'd' * 64)
        common.verify_tree(root, A, 'owner/app')
        (root / 'app.py').write_text('changed')
        with self.assertRaises(common.Rejected):
            common.verify_tree(root, A, 'owner/app')
        (root / 'app.py').write_text('verified')
        (root / 'extra.py').write_text('unexpected')
        with self.assertRaises(common.Rejected):
            common.verify_tree(root, A, 'owner/app')

    def test_locks_are_nonblocking_and_reject_symlinks(self):
        lock = self.root / 'deploy.lock'
        with common.locks([lock]):
            with self.assertRaises(common.Deferred):
                with common.locks([lock]):
                    pass
        alias = self.root / 'alias'
        alias.symlink_to(lock)
        with self.assertRaises(OSError):
            with common.locks([alias]):
                pass

    def test_json_rejects_duplicate_keys_and_symlink(self):
        path = self.root / 'state.json'
        path.write_text('{"sha":"a","sha":"b"}')
        with self.assertRaises(common.Rejected):
            common.read(path)
        path.unlink()
        path.symlink_to(self.root / 'other')
        (self.root / 'other').write_text('{}')
        with self.assertRaises(common.Rejected):
            common.read(path)


    def test_directory_recovery_is_private_and_does_not_restore_live_state(self):
        live = self.root / 'live'
        live.mkdir()
        (live / 'subscriptions.json').write_text('{"value":"before"}')
        copies = adapters.snapshot_directories({'data_directories': [str(live)]}, self.root / 'recovery')
        saved = Path(copies[0]['backup']) / 'subscriptions.json'
        self.assertEqual(saved.read_text(), '{"value":"before"}')
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        (live / 'subscriptions.json').write_text('{"value":"after"}')
        self.assertEqual(saved.read_text(), '{"value":"before"}')
        (live / 'link').symlink_to(self.root / 'outside')
        with self.assertRaises(common.Deferred):
            adapters.snapshot_directories({'data_directories': [str(live)]}, self.root / 'rejected')

    def test_compose_pulls_only_the_digest_from_verified_ci(self):
        app = {'kind': 'compose', 'repository': 'owner/app', 'image': 'ghcr.io/owner/app',
               'image_artifact': 'image-metadata'}
        metadata = {'repository': 'owner/app', 'revision': B, 'image': app['image'],
                    'digest': 'sha256:' + 'd' * 64}
        source = Mock()
        def artifact(verified, destination, *, name):
            self.assertEqual(name, 'image-metadata')
            with zipfile.ZipFile(destination, 'w') as archive:
                archive.writestr('image-metadata.json', json.dumps(metadata))
        source.artifact.side_effect = artifact
        reference = app['image'] + '@' + metadata['digest']
        inspection = [{'RepoDigests': [reference], 'Config': {'Labels': {
            'org.opencontainers.image.revision': B,
            'org.opencontainers.image.source': 'https://github.com/owner/app'}}}]
        with patch.object(adapters, 'run', side_effect=['', json.dumps(inspection)]) as run:
            prepared = adapters.prepare(app, source, {'source': {'sha': B}}, self.root)
        self.assertEqual(prepared['path'], reference)
        self.assertEqual(run.call_args_list[0].args[0], ['docker', 'pull', reference])
        metadata['revision'] = A
        other = self.root / 'wrong-source'
        other.mkdir()
        with patch.object(adapters, 'run') as run, self.assertRaises(common.Rejected):
            adapters.prepare(app, source, {'source': {'sha': B}}, other)
        run.assert_not_called()


class SelectionTests(unittest.TestCase):
    def source(self, rows):
        app = {'repository': 'owner/app', 'workflow': '.github/workflows/build.yml'}
        github = Mock()
        github.api.return_value = {'workflow_runs': rows}
        source = sources.Source(app, '/unused', github)
        source.sync = Mock(return_value=C)
        order = {A: 0, B: 1, C: 2}
        source.ancestor = lambda a, b: order[a] <= order[b]
        return source

    def row(self, sha, event='push', status='completed', branch=common.PROJECT['branch'], id=1):
        return {'id': id, 'head_sha': sha, 'event': event, 'status': status,
                'head_branch': branch, 'path': '.github/workflows/build.yml'}

    def test_skipped_documentation_head_uses_latest_verified_ancestor(self):
        source = self.source([self.row(B)])
        self.assertEqual(source.candidate(A)['head_sha'], B)

    def test_old_rerun_cannot_outrank_newer_main(self):
        source = self.source([self.row(A, id=8), self.row(B, id=5)])
        self.assertEqual(source.candidate(A)['id'], 5)

    def test_pr_and_candidate_branch_are_never_deployed(self):
        source = self.source([self.row(C, 'pull_request'), self.row(C, branch='candidate-new'), self.row(A)])
        self.assertIsNone(source.candidate(A))

    def test_other_default_branch_is_never_deployed(self):
        source = self.source([self.row(C, branch='main'), self.row(C, branch='master'), self.row(A)])
        self.assertIsNone(source.candidate(A))

    def test_active_new_build_blocks_an_older_success(self):
        source = self.source([self.row(C, status='in_progress'), self.row(B)])
        with self.assertRaisesRegex(common.Deferred, 'still running'):
            source.candidate(A)

    def test_pinned_candidate_not_in_main_is_preserved(self):
        source = self.source([self.row(B)])
        with self.assertRaisesRegex(common.Deferred, 'include the deployed'):
            source.candidate(C)

    def test_operations_only_run_does_not_hide_last_application_release(self):
        newer = {**self.row(C), 'conclusion': 'success'}
        source = self.source([newer, self.row(B)])
        source.app['nonrelease'] = {'skipped_jobs': ['publish'], 'prefixes': ['operations/']}
        source.github.pages.return_value = [{'name': 'verify', 'conclusion': 'success'},
                                           {'name': 'publish', 'conclusion': 'skipped'}]
        source.git = Mock(return_value='operations/daily.py')
        self.assertEqual(source.candidate(A)['head_sha'], B)
        source.git.return_value = 'src/app.ts'
        self.assertEqual(source.candidate(A)['head_sha'], C)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root, self.state = root / 'app', root / 'state'
        self.root.mkdir()
        self.state.mkdir()
        (self.root / 'app').write_bytes(b'old executable')
        (self.root / 'deployment.json').write_text(json.dumps({'source_sha': A}))
        payload = root / 'new-app'
        payload.write_bytes(b'new executable')
        self.prepared = {'path': str(payload), 'sha256': 'd' * 64, 'binary_sha256': common.digest(payload)}
        self.app = {'id': 'app', 'kind': 'binary', 'root': str(self.root), 'binary': 'app',
                    'units': ['synthetic.service'], 'locks': [str(root / 'lock')], 'databases': []}
        self.verified = {'source': {'sha': B, 'run_id': 2}, 'builder': {'run_id': 2}}

    def test_failed_health_restores_exact_binary_and_quarantines(self):
        with patch.object(adapters, 'preflight'), patch.object(adapters, 'systemctl'), \
             patch.object(adapters, 'probe', side_effect=[common.Deferred('bad health'), None]):
            with self.assertRaisesRegex(common.Rejected, 'previous code restored'):
                adapters.deploy(self.app, self.prepared, self.verified, None, self.state, A)
        self.assertEqual((self.root / 'app').read_bytes(), b'old executable')
        self.assertEqual(common.read(self.state / 'transaction.json')['state'], 'rolled_back')
        self.assertEqual(common.read(self.root / 'deployment.json')['source_sha'], A)

    def test_success_keeps_one_verified_predecessor(self):
        with patch.object(adapters, 'preflight'), patch.object(adapters, 'systemctl'), patch.object(adapters, 'probe'):
            result = adapters.deploy(self.app, self.prepared, self.verified, None, self.state, A)
        self.assertEqual((self.root / 'app').read_bytes(), b'new executable')
        self.assertEqual((Path(result['recovery']) / 'app').read_bytes(), b'old executable')
        self.assertEqual(common.read(self.root / 'deployment.json')['source_sha'], B)

    def test_retention_error_does_not_restart_or_quarantine_accepted_code(self):
        prior = self.state / 'recovery-prior'
        prior.mkdir()
        common.write_json(self.state / 'deployed.json', {'state': 'success', 'recovery': str(prior)})
        with patch.object(adapters, 'preflight'), patch.object(adapters, 'systemctl') as service, \
             patch.object(adapters, 'probe'), patch.object(adapters.shutil, 'rmtree', side_effect=OSError('busy')):
            adapters.deploy(self.app, self.prepared, self.verified, None, self.state, A)
        self.assertEqual((self.root / 'app').read_bytes(), b'new executable')
        self.assertEqual(common.read(self.state / 'transaction.json')['state'], 'success')
        self.assertEqual([c.args[0] for c in service.call_args_list], ['stop', 'start'])
        self.assertTrue((self.state / 'retention-pending.json').exists())

    def test_success_bookkeeping_is_recovered_without_stopping_services(self):
        with patch.object(adapters, 'preflight'), patch.object(adapters, 'systemctl'), \
             patch.object(adapters, 'probe'), patch.object(adapters, 'finalize', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                adapters.deploy(self.app, self.prepared, self.verified, None, self.state, A)
        self.assertEqual(common.read(self.state / 'transaction.json')['state'], 'success')
        self.assertEqual(common.read(self.root / 'deployment.json')['source_sha'], A)
        with patch.object(adapters, 'systemctl') as service:
            self.assertEqual(adapters.recover(self.app, self.state), 'success')
        service.assert_not_called()
        self.assertEqual(common.read(self.root / 'deployment.json')['source_sha'], B)

    def test_failed_recovery_can_be_explicitly_retried(self):
        recovery = self.state / 'recovery-failed'
        recovery.mkdir()
        (recovery / 'app').write_bytes(b'old executable')
        (self.root / 'app').write_bytes(b'bad executable')
        common.write_json(self.state / 'transaction.json', {'state': 'rollback_failed',
            'source_sha': B, 'previous_sha': A, 'switch_started': True,
            'recovery': str(recovery), 'code': {'binary_sha256': common.digest(recovery / 'app')}})
        with patch.object(adapters, 'systemctl'), patch.object(adapters, 'probe'):
            self.assertEqual(adapters.recover(self.app, self.state, retry=True), 'rolled_back')
        self.assertEqual((self.root / 'app').read_bytes(), b'old executable')

    def test_restart_recovers_interrupted_switch_before_new_deployment(self):
        recovery = self.state / 'recovery-test'
        recovery.mkdir()
        (recovery / 'app').write_bytes(b'old executable')
        (self.root / 'app').write_bytes(b'partially installed executable')
        record = {'state': 'switching', 'source_sha': B, 'previous_sha': A,
                  'recovery': str(recovery), 'code': {'binary_sha256': common.digest(recovery / 'app')}}
        common.write_json(self.state / 'transaction.json', record)
        with patch.object(adapters, 'systemctl'), patch.object(adapters, 'probe'):
            self.assertEqual(adapters.recover(self.app, self.state), 'rolled_back')
        self.assertEqual((self.root / 'app').read_bytes(), b'old executable')
        self.assertEqual(common.read(self.state / 'failed.json')['sha'], B)

    def test_compose_rollback_uses_captured_digest_not_moving_tag(self):
        app = {**self.app, 'kind': 'compose', 'image_variable': 'CHECKIN_IMAGE', 'compose_service': 'check-in'}
        recovery = self.state / 'recovery'
        recovery.mkdir()
        env = b'CHECKIN_IMAGE=repo:main\nPRIVATE_SECRET=synthetic\n'
        (recovery / 'previous.env').write_bytes(env)
        (self.root / '.env').write_bytes(b'CHECKIN_IMAGE=repo@sha256:new\nPRIVATE_SECRET=synthetic\n')
        record = {'code': {'env_sha256': common.digest(recovery / 'previous.env'),
                          'container': {'reference': 'repo@sha256:' + 'a' * 64}}}
        with patch.object(adapters, 'compose'):
            adapters.restore_code(app, recovery, record)
        after = (self.root / '.env').read_text()
        self.assertIn('CHECKIN_IMAGE=repo@sha256:' + 'a' * 64, after)
        self.assertIn('PRIVATE_SECRET=synthetic', after)

    def test_runtime_inventory_includes_new_modules(self):
        (self.root / 'server.py').write_text('')
        (self.root / 'new_module.py').write_text('')
        (self.root / 'static').mkdir()
        (self.root / 'static/new.js').write_text('')
        (self.root / 'projects').mkdir()
        (self.root / 'projects/private.json').write_text('{}')
        self.assertEqual(adapters.runtime_files(self.root), ['new_module.py', 'server.py', 'static/new.js'])


if __name__ == '__main__':
    unittest.main()
