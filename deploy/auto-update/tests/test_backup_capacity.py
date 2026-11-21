"""Synthetic backup-capacity contracts; no production paths or network."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_capacity as capacity


class BackupCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'new-backup'

    def test_small_backup_has_no_fixed_gib_reserve(self):
        plan = capacity.Plan().add('a', 'file', 1)
        with patch.object(capacity, 'filesystem', return_value=(4096, 8192, 2)):
            self.assertEqual(plan.check(self.target)['needed_bytes'], 8192)
        with patch.object(capacity, 'filesystem', return_value=(4096, 8191, 2)):
            with self.assertRaises(capacity.CapacityError):
                plan.check(self.target)

    def test_multiple_files_round_separately_on_destination_disk(self):
        plan = capacity.Plan().add('a', 'file', 1).add('b', 'file', 4097)
        with patch.object(capacity, 'filesystem', return_value=(4096, 16384, 3)):
            self.assertEqual(plan.check(self.target)['needed_bytes'], 16384)
        with patch.object(capacity, 'filesystem', return_value=(8192, 24576, 3)):
            self.assertEqual(plan.check(self.target)['needed_bytes'], 24576)

    def test_inode_shortage_is_independent_of_bytes(self):
        plan = capacity.Plan().add('empty', 'file', 0)
        with patch.object(capacity, 'filesystem', return_value=(4096, 1024**3, 1)):
            with self.assertRaises(capacity.CapacityError):
                plan.check(self.target)

    def test_new_destination_uses_nearest_existing_filesystem_without_writing(self):
        destination = self.root / 'not-created' / 'backup'
        unit, free, inodes = capacity.filesystem(destination)
        self.assertGreater(unit, 0)
        self.assertGreaterEqual(free, 0)
        self.assertFalse(destination.parent.exists())

    def test_sparse_source_uses_logical_payload_not_source_allocated_blocks(self):
        source = self.root / 'sparse'
        with source.open('wb') as stream:
            stream.truncate(2 * 1024**2)
        plan = capacity.Plan().file(source, 'sparse')
        with patch.object(capacity, 'filesystem', return_value=(4096, 3 * 1024**2, 2)):
            self.assertEqual(plan.check(self.target)['needed_bytes'], 2 * 1024**2 + 4096)

    def test_sqlite_estimate_includes_committed_wal_pages(self):
        source = self.root / 'source.db'
        db = sqlite3.connect(source)
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('CREATE TABLE payload(value BLOB)')
        db.execute('INSERT INTO payload VALUES (zeroblob(262144))')
        db.commit()
        expected = db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
        self.assertGreater(expected, source.stat().st_size)
        self.assertEqual(capacity.sqlite_bytes(source), expected)
        plan = capacity.Plan().tree(self.root, sqlite_top=True)
        self.assertNotIn('source.db-wal', plan.entries)
        self.assertNotIn('source.db-shm', plan.entries)
        self.assertEqual(plan.entries['source.db'], ('file', expected))

    def test_distinct_target_copies_of_same_source_are_counted_twice(self):
        source = self.root / 'file'
        source.write_bytes(b'x')
        plan = capacity.Plan().file(source, 'one').file(source, 'two')
        with patch.object(capacity, 'filesystem', return_value=(4096, 12288, 3)):
            self.assertEqual(plan.check(self.target)['needed_bytes'], 12288)


if __name__ == '__main__':
    unittest.main()
