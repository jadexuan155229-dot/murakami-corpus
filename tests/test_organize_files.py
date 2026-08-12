import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from corpus import cli, db


class OrganizeFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.db_path = root / "corpus.db"
        self.files_dir = root / "files"
        self.files_dir.mkdir()
        for attr, value in (("DB_PATH", self.db_path), ("FILES_DIR", self.files_dir)):
            patcher = patch.object(db, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        (self.files_dir / "w1_zh_upload.epub").write_bytes(b"invented epub")
        con = db.connect()
        con.execute("INSERT INTO works (id, title_zh) VALUES (1, '测试作品')")
        con.execute(
            """INSERT INTO editions
               (id, work_id, language, format, filename, indexed_at)
               VALUES (1, 1, 'zh', 'epub', 'w1_zh_upload.epub', '2026-01-01')"""
        )
        con.execute(
            "INSERT INTO segments (id, edition_id, seq, content) VALUES (1, 1, 1, '正文')"
        )
        con.execute("INSERT INTO segments_fts (body, segment_id) VALUES ('正 文', 1)")
        con.commit()
        con.close()

    def _run(self, apply=False):
        output = io.StringIO()
        with redirect_stdout(output):
            cli.cmd_organize_files(argparse.Namespace(apply=apply))
        return output.getvalue()

    def _edition_state(self):
        con = db.connect()
        edition = tuple(con.execute("SELECT * FROM editions WHERE id=1").fetchone())
        segments = [tuple(row) for row in con.execute("SELECT * FROM segments WHERE edition_id=1")]
        fts = [tuple(row) for row in con.execute("SELECT * FROM segments_fts")]
        con.close()
        return edition, segments, fts

    def test_dry_run_does_not_write_files_or_database(self):
        before = self._edition_state()

        output = self._run()

        self.assertIn("edition 1: w1_zh_upload.epub -> w1_测试作品/w1_zh_upload.epub [DRY-RUN]", output)
        self.assertEqual(self._edition_state(), before)
        self.assertTrue((self.files_dir / "w1_zh_upload.epub").exists())
        self.assertFalse((self.files_dir / "w1_测试作品").exists())

    def test_apply_moves_legacy_file_only_updates_filename_and_is_idempotent(self):
        before = self._edition_state()

        output = self._run(apply=True)

        target = self.files_dir / "w1_测试作品" / "w1_zh_upload.epub"
        after = self._edition_state()
        self.assertIn("[moved]", output)
        self.assertFalse((self.files_dir / "w1_zh_upload.epub").exists())
        self.assertTrue(target.exists())
        self.assertEqual(after[0][4], "w1_测试作品/w1_zh_upload.epub")
        self.assertEqual(after[0][:4] + after[0][5:], before[0][:4] + before[0][5:])
        self.assertEqual(after[1:], before[1:])

        second = self._run(apply=True)
        self.assertIn("[already organized]", second)
        self.assertEqual(self._edition_state(), after)

    def test_cmd_add_uses_the_same_work_directory(self):
        source = Path(self.temp_dir.name) / "new.txt"
        source.write_text("invented source", encoding="utf-8")
        with patch.object(
            cli,
            "parse_file",
            return_value=[{"seq": 1, "chapter": None, "page": None, "content": "new"}],
        ):
            cli.cmd_add(
                argparse.Namespace(file=str(source), work=1, lang="zh", notes=None)
            )

        con = db.connect()
        edition = con.execute("SELECT filename FROM editions WHERE id=2").fetchone()
        con.close()
        self.assertEqual(edition["filename"], "w1_测试作品/w1_zh_new.txt")
        self.assertTrue((self.files_dir / edition["filename"]).is_file())


if __name__ == "__main__":
    unittest.main()
