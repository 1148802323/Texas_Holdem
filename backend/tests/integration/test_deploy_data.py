import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from deploy.sqlite_data import backup_database, restore_database


class DeploymentDataTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path("database") / f"deploy_test_{uuid4().hex}"
        self.directory.mkdir()
        self.source = self.directory / "current.sqlite3"
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("CREATE TABLE rooms (id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO rooms VALUES ('first-room')")
            connection.execute("PRAGMA user_version = 4")
            connection.commit()

    def tearDown(self):
        self.assertEqual(self.directory.resolve().parent, Path("database").resolve())
        for path in (self.directory / "backups", self.directory):
            if path.exists():
                for child in path.iterdir():
                    self.assertTrue(child.is_file())
                    child.unlink()
                path.rmdir()

    def test_online_backup_restores_a_verified_snapshot_without_overwrite(self):
        backup = backup_database(self.source, self.directory / "backups")
        self.assertTrue(backup.name.startswith("texas_holdem-"))
        with closing(sqlite3.connect(self.source)) as connection:
            connection.execute("INSERT INTO rooms VALUES ('later-room')")
            connection.commit()
        restored = self.directory / "restored.sqlite3"
        restore_database(backup, restored)
        with closing(sqlite3.connect(restored)) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT id FROM rooms").fetchall(),
                             [("first-room",)])
        with self.assertRaises(FileExistsError):
            restore_database(backup, restored)
        with closing(sqlite3.connect(self.source)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 2)

    def test_invalid_source_and_stale_sidecar_are_rejected(self):
        with self.assertRaises(FileNotFoundError):
            backup_database(self.directory / "missing.sqlite3", self.directory / "backups")
        invalid = self.directory / "invalid.sqlite3"
        invalid.touch()
        with self.assertRaises(ValueError):
            backup_database(invalid, self.directory / "backups")
        self.assertEqual(list((self.directory / "backups").iterdir()), [])
        backup = backup_database(self.source, self.directory / "backups")
        sidecar = self.directory / "restored.sqlite3-wal"
        sidecar.touch()
        with self.assertRaises(FileExistsError):
            restore_database(backup, self.directory / "restored.sqlite3")
        self.assertFalse((self.directory / "restored.sqlite3").exists())
