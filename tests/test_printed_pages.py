import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as webapp
from corpus import db


class PrintedPageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "corpus.db"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        webapp.app.config.update(TESTING=True)
        self.client = webapp.app.test_client()

    def test_init_db_migrates_legacy_segments_once_and_preserves_rows(self):
        con = sqlite3.connect(self.db_path)
        con.executescript(
            """
            CREATE TABLE works (id INTEGER PRIMARY KEY, title_zh TEXT NOT NULL);
            CREATE TABLE editions (
                id INTEGER PRIMARY KEY,
                work_id INTEGER NOT NULL,
                language TEXT NOT NULL,
                format TEXT NOT NULL,
                filename TEXT,
                has_pages INTEGER DEFAULT 0,
                notes TEXT,
                indexed_at TEXT
            );
            CREATE TABLE segments (
                id INTEGER PRIMARY KEY,
                edition_id INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                chapter TEXT,
                page INTEGER,
                content TEXT NOT NULL
            );
            """
        )
        con.execute("INSERT INTO works VALUES (1, '旧作品')")
        con.execute(
            """INSERT INTO editions
               (id, work_id, language, format, indexed_at)
               VALUES (1, 1, 'en', 'pdf', '2026-01-01')"""
        )
        con.execute(
            "INSERT INTO segments VALUES (1, 1, 20, NULL, 20, 'legacy segment')"
        )
        con.commit()
        con.close()

        db.init_db()
        db.init_db()

        con = db.connect()
        columns = [row["name"] for row in con.execute("PRAGMA table_info(segments)")]
        row = con.execute("SELECT page, printed_page, content FROM segments WHERE id=1").fetchone()
        con.close()
        self.assertIn("printed_page", columns)
        self.assertEqual((row["page"], row["printed_page"], row["content"]), (20, None, "legacy segment"))
        reader_body = self.client.get("/edition/1/read/1").get_data(as_text=True)
        self.assertIn("legacy segment", reader_body)
        self.assertIn("PDF 20", reader_body)

    def test_insert_segments_stores_optional_printed_page_and_defaults_to_null(self):
        db.init_db()
        con = db.connect()
        con.execute("INSERT INTO works (id, title_zh) VALUES (1, '测试作品')")
        con.execute(
            "INSERT INTO editions (id, work_id, language, format) VALUES (1, 1, 'en', 'pdf')"
        )
        db.insert_segments(
            con,
            1,
            [
                {"seq": 1, "chapter": None, "page": 20, "printed_page": "3", "content": "printed"},
                {"seq": 2, "chapter": None, "page": 21, "content": "unprinted"},
            ],
        )
        rows = con.execute(
            "SELECT page, printed_page FROM segments ORDER BY seq"
        ).fetchall()
        con.close()

        self.assertEqual([tuple(row) for row in rows], [(20, "3"), (21, None)])

    def _seed_search_and_reader_editions(self):
        db.init_db()
        con = db.connect()
        con.execute("INSERT INTO works (id, title_zh) VALUES (1, '测试作品')")
        con.executemany(
            """INSERT INTO editions (id, work_id, language, format, indexed_at)
               VALUES (?, 1, 'en', ?, '2026-01-01')""",
            [(1, "epub"), (2, "pdf")],
        )
        db.insert_segments(
            con,
            1,
            [{"seq": 1, "chapter": "Chapter 1", "page": None, "content": "needle epub"}],
        )
        db.insert_segments(
            con,
            2,
            [
                {"seq": 1, "chapter": None, "page": 20, "printed_page": "3", "content": "needle printed"},
                {"seq": 2, "chapter": None, "page": 20, "content": "needle unprinted"},
            ],
        )
        con.commit()
        return con

    def test_search_returns_and_displays_pdf_page_pairs_without_changing_epub_labels(self):
        con = self._seed_search_and_reader_editions()
        rows = db.search(con, "needle")
        grouped_rows = db.search_grouped(con, "needle")
        work_rows = db.search_work(con, "needle", 1)
        con.close()

        for result_rows in (rows, grouped_rows, work_rows):
            pdf_rows = [row for row in result_rows if row["format"] == "pdf"]
            self.assertEqual([row["printed_page"] for row in pdf_rows], ["3", None])

        body = self.client.get("/search?q=needle").get_data(as_text=True)
        self.assertIn("p.3 · PDF 20", body)
        self.assertIn("PDF 20", body)
        self.assertNotIn("p.20", body)
        self.assertIn("Chapter 1", body)

    def test_pdf_reader_shows_page_markers_and_epub_reader_does_not(self):
        self._seed_search_and_reader_editions().close()

        pdf_body = self.client.get("/edition/2/read/2").get_data(as_text=True)
        epub_body = self.client.get("/edition/1/read/1").get_data(as_text=True)

        self.assertEqual(pdf_body.count('class="pdf-reader-page"'), 2)
        self.assertIn("p.3 · PDF 20", pdf_body)
        self.assertIn("PDF 20", pdf_body)
        self.assertIn('id="pdf-page-20" class="pdf-reader-page"', pdf_body)
        self.assertNotIn('class="pdf-reader-page"', epub_body)
        self.assertIn("needle epub", epub_body)


if __name__ == "__main__":
    unittest.main()
