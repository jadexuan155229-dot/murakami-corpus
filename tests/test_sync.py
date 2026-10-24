"""Tests use synthetic texts, never the private corpus."""
import sqlite3
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from corpus.sync import (SyncError, export_snapshot, import_snapshot, adopt_baseline,
                         _read_state, _logical_fingerprint)


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
        self.assertEqual(_read_state(self.b)["logical_fingerprint"], _logical_fingerprint(self.b))
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
        self.assertEqual(_read_state(self.b)["logical_fingerprint"],
                         _logical_fingerprint(self.b))
        bad = self.second.with_name("tampered.zip")
        with zipfile.ZipFile(self.second) as inp, zipfile.ZipFile(bad, "w") as out:
            for name in inp.namelist():
                body = inp.read(name)
                out.writestr(name, b"bad" if name == "files/new.txt" else body)
        with self.assertRaises(SyncError):
            import_snapshot(bad, root=self.b, force=True)


    def test_logical_hash_stable_when_sqlite_pages_change(self):
        export_snapshot(self.first, root=self.a)
        baseline = _read_state(self.a)["logical_fingerprint"]
        self.assertEqual(_logical_fingerprint(self.a), baseline)
        con = sqlite3.connect(self.a / "corpus.db")
        con.execute("INSERT INTO example VALUES ('temporary')")
        con.execute("DELETE FROM example WHERE content = 'temporary'")
        con.commit()
        con.close()
        # Page freelists and byte layout may change without changing records.
        self.assertEqual(_logical_fingerprint(self.a), baseline)
        con = sqlite3.connect(self.a / "corpus.db")
        con.execute("UPDATE example SET content = 'genuine edit'")
        con.commit()
        con.close()
        self.assertNotEqual(_logical_fingerprint(self.a), baseline)

    def test_legacy_state_requires_verified_adoption(self):
        export_snapshot(self.first, root=self.a)
        state_file = self.a / ".sync-state.json"
        state = json.loads(state_file.read_text())
        state.pop("logical_fingerprint")
        state_file.write_text(json.dumps(state))
        with self.assertRaisesRegex(SyncError, "adopt-baseline"):
            export_snapshot(self.second, root=self.a)
        self.assertEqual(adopt_baseline(self.first, root=self.a), state["snapshot_id"])
        self.assertEqual(_read_state(self.a)["logical_fingerprint"],
                         _logical_fingerprint(self.a))
        con = sqlite3.connect(self.a / "corpus.db")
        con.execute("UPDATE example SET content='later unsynced edit'")
        con.commit()
        con.close()
        with self.assertRaisesRegex(SyncError, "changed"):
            adopt_baseline(self.first, root=self.a)

    def test_verified_legacy_import_state_can_continue(self):
        export_snapshot(self.first, root=self.a)
        import_snapshot(self.first, root=self.b, force=True)
        state_file = self.b / ".sync-state.json"
        state = json.loads(state_file.read_text())
        state.pop("logical_fingerprint")
        state_file.write_text(json.dumps(state))
        adopt_baseline(self.first, root=self.b)
        modify(self.a, "new after adoption")
        export_snapshot(self.second, root=self.a)
        import_snapshot(self.second, root=self.b)
        self.assertEqual(_read_state(self.b)["snapshot_id"],
                         _read_state(self.a)["snapshot_id"])

    def test_schema_and_fts_changes_detected(self):
        con = sqlite3.connect(self.a / "corpus.db")
        con.execute("CREATE VIRTUAL TABLE search_test USING fts5(text)")
        con.execute("INSERT INTO search_test(text) VALUES ('古い図書館')")
        con.commit()
        con.close()
        initial = _logical_fingerprint(self.a)
        con = sqlite3.connect(self.a / "corpus.db")
        con.execute("INSERT INTO search_test(text) VALUES ('a new text')")
        con.commit()
        con.close()
        self.assertNotEqual(initial, _logical_fingerprint(self.a))


if __name__ == "__main__":
    unittest.main()
