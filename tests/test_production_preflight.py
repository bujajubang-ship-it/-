import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from production_preflight import create_verified_backup, inspect_database


TABLES = (
    "history",
    "pipeline",
    "optimize_videos",
    "worksheet_rows",
    "chat_session",
    "knowledge",
)


class ProductionPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "history.db"
        with sqlite3.connect(self.source) as connection:
            for table in TABLES:
                connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT)')
                connection.execute(f'INSERT INTO "{table}" (value) VALUES (?)', (table,))
            connection.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_read_only_inspection_returns_only_counts_and_hashes(self):
        evidence = inspect_database(self.source)
        self.assertEqual(evidence.integrity, "ok")
        self.assertEqual(evidence.row_counts, {table: 1 for table in sorted(TABLES)})
        self.assertIsNone(evidence.file_sha256)

    def test_online_backup_is_private_verified_and_never_overwritten(self):
        target = self.root / "verified.db"
        original, backup = create_verified_backup(self.source, target)
        self.assertEqual(original.row_counts, backup.row_counts)
        self.assertEqual(original.schema_sha256, backup.schema_sha256)
        self.assertEqual(len(backup.file_sha256 or ""), 64)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(RuntimeError, "refusing overwrite"):
            create_verified_backup(self.source, target)

    def test_failure_does_not_leave_partial_backup(self):
        with sqlite3.connect(self.source) as connection:
            connection.execute("DROP TABLE knowledge")
            connection.commit()
        target = self.root / "invalid.db"
        with self.assertRaisesRegex(RuntimeError, "missing"):
            create_verified_backup(self.source, target)
        self.assertFalse(target.exists())

    def test_relative_backup_destination_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "absolute"):
            create_verified_backup(self.source, Path("relative-backup.db"))


if __name__ == "__main__":
    unittest.main()
