"""Tests use synthetic texts, never the private corpus."""
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from corpus.sync import SyncError, export_snapshot, import_snapshot


def library(root, value):
    root.mkdir(parents=True)
    (root / "files").mkdir()
    (root / "files" / "sample.txt").write_text(value, encoding="utf-8")
    con = sqlite3.connect(root / "corpus.db")
    con.execute("CREATE TABLE example (content TEXT)")
    con.execute("INSERT INTO example VALUES (?)", (value,))
    con.commit()
    con.close()


def modify(root, value):
    con = sqlite3.connect(root / "corpus.db")
    con.execute("INSERT INTO example VALUES (?)", (value,))
    con.commit()
    con.close()
    (root / "files" / "new.txt").write_text(value, encoding="utf-8")


class PrivateSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.a, self.b = base / "a" / "data", base / "b" / "data"
        self.first, self.second = base / "one.zip", base / "two.zip"
        library(self.a, "猫 and 羊男")
        library(self.b, "older")
        running = patch("corpus.sync._app_likely_running", return_value=False)
        running.start()
        self.addCleanup(running.stop)

    def test_import_requires_initial_confirmation_and_preserves_backup(self):
        export_snapshot(self.first, root=self.a)
        with self.assertRaises(SyncError):
            import_snapshot(self.first, root=self.b)
        import_snapshot(self.first, root=self.b, force=True)
        self.assertEqual((self.b / "files" / "sample.txt").read_text(), "猫 and 羊男")
        backups = list((self.b.parent.parent / ".murakami-sync-backups").iterdir())
        self.assertEqual((backups[0] / "files" / "sample.txt").read_text(), "older")

    def test_forward_sync_and_divergence_guard(self):
        export_snapshot(self.first, root=self.a)
        import_snapshot(self.first, root=self.b, force=True)
        modify(self.a, "new")
        export_snapshot(self.second, root=self.a)
        modify(self.b, "unshared")
        with self.assertRaisesRegex(SyncError, "unsynced changes"):
            import_snapshot(self.second, root=self.b)

    def test_good_sync_and_tamper_refusal(self):
        export_snapshot(self.first, root=self.a)
        import_snapshot(self.first, root=self.b, force=True)
        modify(self.a, "new")
        export_snapshot(self.second, root=self.a)
        import_snapshot(self.second, root=self.b)
        self.assertTrue((self.b / "files" / "new.txt").exists())
        bad = self.second.with_name("tampered.zip")
        with zipfile.ZipFile(self.second) as inp, zipfile.ZipFile(bad, "w") as out:
            for name in inp.namelist():
                body = inp.read(name)
                out.writestr(name, b"bad" if name == "files/new.txt" else body)
        with self.assertRaises(SyncError):
            import_snapshot(bad, root=self.b, force=True)


if __name__ == "__main__":
    unittest.main()
