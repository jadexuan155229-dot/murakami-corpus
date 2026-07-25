import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as webapp
from corpus import db


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.db_path = root / "corpus.db"
        self.files_dir = root / "files"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.files_patch = patch.object(db, "FILES_DIR", self.files_dir)
        self.db_patch.start()
        self.files_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.files_patch.stop)

        # 这些用例测的是上传本身，模拟本地运行（写操作免口令）。
        # 公开部署下守卫的行为由 WriteGuardTests 覆盖。
        self.local_patch = patch.object(webapp, "LOCAL_DEV", True)
        self.local_patch.start()
        self.addCleanup(self.local_patch.stop)

        db.init_db()
        con = db.connect()
        con.execute("INSERT INTO works (id, title_zh) VALUES (1, '测试作品')")
        con.commit()
        con.close()
        webapp.app.config.update(TESTING=True, SECRET_KEY="test-only-secret")
        self.client = webapp.app.test_client()

    def _upload(self, filename: str, language: str = "zh"):
        return self.client.post(
            "/work/1/upload",
            data={
                "lang": language,
                "file": (io.BytesIO(b"invented test file"), filename),
            },
            content_type="multipart/form-data",
        )

    def _edition(self):
        con = db.connect()
        row = con.execute("SELECT * FROM editions ORDER BY id").fetchone()
        con.close()
        return row

    def _successful_parse(self):
        return patch.object(
            webapp,
            "parse_file",
            return_value=[{
                "seq": 1,
                "chapter": "Test Chapter",
                "page": None,
                "content": "A tiny invented line.",
            }],
        )

    def test_chinese_epub_name_preserves_suffix(self):
        with self._successful_parse() as parse:
            response = self._upload("虚构短篇.epub")

        edition = self._edition()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(edition["filename"], "w1_zh_upload.epub")
        self.assertEqual(edition["format"], "epub")
        self.assertTrue((self.files_dir / edition["filename"]).is_file())
        self.assertEqual(parse.call_args.args[0].suffix, ".epub")

    def test_japanese_epub_name_preserves_suffix(self):
        with self._successful_parse():
            response = self._upload("架空物語.epub", language="ja")

        edition = self._edition()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(edition["filename"], "w1_ja_upload.epub")
        self.assertTrue((self.files_dir / edition["filename"]).is_file())

    def test_english_pdf_name_keeps_clean_stem_and_suffix(self):
        with self._successful_parse():
            response = self._upload("Field Notes.pdf", language="en")

        edition = self._edition()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(edition["filename"], "w1_en_Field_Notes.pdf")
        self.assertEqual(edition["format"], "pdf")
        self.assertEqual(edition["has_pages"], 1)

    def test_empty_secure_stem_falls_back_to_upload_with_original_suffix(self):
        with (
            patch.object(webapp, "secure_filename", return_value=""),
            self._successful_parse(),
        ):
            self._upload("anything.epub")

        edition = self._edition()
        self.assertEqual(edition["filename"], "w1_zh_upload.epub")

    def test_parse_failure_leaves_no_edition_and_connection_is_usable(self):
        with patch.object(webapp, "parse_file", side_effect=ValueError("invalid test file")):
            with self.assertRaises(ValueError):
                self._upload("失败样本.epub")

        con = db.connect()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM editions").fetchone()[0], 0)
        con.execute("INSERT INTO works (id, title_zh) VALUES (2, '后续写入测试')")
        con.commit()
        con.close()

    def test_parse_failure_removes_saved_orphan_file(self):
        with patch.object(webapp, "parse_file", side_effect=ValueError("invalid test file")):
            with self.assertRaises(ValueError):
                self._upload("壊れた本.epub", language="ja")

        self.assertEqual(list(self.files_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
