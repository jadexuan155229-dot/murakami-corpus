import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from corpus import db
from tools import import_ocr_jsonl as importer


class ImportOcrJsonlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.db_path = root / "corpus.db"
        self.files_dir = root / "files"
        self.jsonl_path = root / "book.ocr.jsonl"
        for attr, value in (("DB_PATH", self.db_path), ("DATA_DIR", root), ("FILES_DIR", self.files_dir)):
            patcher = patch.object(db, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.page_count = patch.object(importer, "pdf_page_count", return_value=3)
        self.page_count.start()
        self.addCleanup(self.page_count.stop)
        db.init_db()
        self._seed_editions()

    def _seed_editions(self):
        self.files_dir.mkdir(parents=True, exist_ok=True)
        (self.files_dir / "scan.pdf").write_bytes(b"test PDF placeholder")
        (self.files_dir / "book.epub").write_bytes(b"test EPUB placeholder")
        con = db.connect()
        con.execute("INSERT INTO works (id, title_zh) VALUES (1, '且听风吟')")
        con.executemany(
            """INSERT INTO editions
               (id, work_id, language, format, filename, indexed_at)
               VALUES (?, 1, 'zh', ?, ?, NULL)""",
            [(1, "pdf", "scan.pdf"), (2, "epub", "book.epub")],
        )
        con.commit()
        con.close()

    def write_jsonl(self, records):
        self.jsonl_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )

    def records(self, pages=(1, 2, 3)):
        return [
            {
                "pdf_page": page,
                "printed_page_candidate": "3" if page == 3 else None,
                "text": "" if page == 2 else f"第 {page} 页正文",
                "blocks": [],
            }
            for page in pages
        ]

    def plan(self, edition_id=1):
        return importer.build_import_plan(edition_id, self.jsonl_path)

    def counts(self, edition_id=1):
        con = db.connect()
        segments = con.execute(
            "SELECT COUNT(*) FROM segments WHERE edition_id=?", (edition_id,)
        ).fetchone()[0]
        fts = con.execute(
            """SELECT COUNT(*) FROM segments_fts f JOIN segments s ON s.id=f.segment_id
               WHERE s.edition_id=?""",
            (edition_id,),
        ).fetchone()[0]
        con.close()
        return segments, fts

    def test_dry_run_plan_does_not_write_database(self):
        self.write_jsonl(self.records())

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = importer.main([
                "--edition-id", "1", "--input", str(self.jsonl_path),
            ])
        plan = self.plan()

        self.assertEqual(exit_code, 0)
        self.assertEqual(plan.empty_page_count, 1)
        self.assertEqual(plan.printed_page_count, 1)
        self.assertEqual(self.counts(), (0, 0))
        self.assertIn("Safe to apply: yes.", output.getvalue())
        self.assertIn("DRY RUN ONLY — database unchanged.", output.getvalue())

    def test_rejects_missing_and_non_pdf_editions(self):
        self.write_jsonl(self.records())

        with self.assertRaisesRegex(importer.OcrImportError, "does not exist"):
            self.plan(999)
        with self.assertRaisesRegex(importer.OcrImportError, "PDF edition"):
            self.plan(2)

    def test_plan_accepts_pdf_in_work_directory(self):
        folder = self.files_dir / "w1_且听风吟"
        folder.mkdir()
        source = folder / "scan.pdf"
        (self.files_dir / "scan.pdf").replace(source)
        con = db.connect()
        con.execute(
            "UPDATE editions SET filename=? WHERE id=1", ("w1_且听风吟/scan.pdf",)
        )
        con.commit()
        con.close()
        self.write_jsonl(self.records())

        plan = self.plan()

        self.assertEqual(plan.filename, "w1_且听风吟/scan.pdf")
        self.assertEqual(plan.pdf_page_count, 3)

    def test_rejects_duplicate_pages_and_malformed_jsonl(self):
        self.write_jsonl([{"pdf_page": 1, "text": "one"}, {"pdf_page": 1, "text": "two"}])
        with self.assertRaisesRegex(importer.OcrImportError, "重复"):
            self.plan()

        self.jsonl_path.write_text('{"pdf_page": 1, "text": "one"}\nnot json\n', encoding="utf-8")
        with self.assertRaisesRegex(importer.OcrImportError, "无法解析"):
            self.plan()

    def test_empty_text_is_not_inserted_and_page_fields_are_preserved(self):
        self.write_jsonl([
            {"pdf_page": 3, "printed_page_candidate": "3", "text": "three"},
            {"pdf_page": 1, "printed_page_candidate": None, "text": "one"},
            {"pdf_page": 2, "printed_page_candidate": None, "text": ""},
        ])

        result = importer.apply_import(self.plan(), replace_existing=False, allow_partial=False)

        con = db.connect()
        rows = con.execute(
            "SELECT seq, page, printed_page, content FROM segments WHERE edition_id=1 ORDER BY seq"
        ).fetchall()
        con.close()
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(
            [tuple(row) for row in rows],
            [(1, 1, None, "one"), (2, 3, "3", "three")],
        )

    def test_optional_corpus_fields_control_order_chapter_and_inclusion(self):
        self.write_jsonl([
            {
                "pdf_page": 1,
                "text": "first in PDF, second in reading order",
                "chapter": "第一章",
                "reading_seq": 2,
            },
            {
                "pdf_page": 2,
                "text": "kept only for PDF coverage",
                "chapter": "扉页",
                "reading_seq": 3,
                "include_in_corpus": False,
            },
            {
                "pdf_page": 3,
                "text": "third in PDF, first in reading order",
                "chapter": "序章",
                "reading_seq": 1,
            },
        ])

        plan = self.plan()
        result = importer.apply_import(plan, replace_existing=False, allow_partial=False)

        con = db.connect()
        rows = con.execute(
            "SELECT seq, chapter, page, content FROM segments WHERE edition_id=1 ORDER BY seq"
        ).fetchall()
        con.close()
        self.assertTrue(plan.has_complete_pdf_coverage)
        self.assertEqual(len(plan.nonempty_records), 3)
        self.assertEqual(len(plan.included_nonempty_records), 2)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (1, "序章", 3, "third in PDF, first in reading order"),
                (2, "第一章", 1, "first in PDF, second in reading order"),
            ],
        )

    def test_existing_segments_refuse_without_replace_and_replace_clears_fts(self):
        con = db.connect()
        db.insert_segments(con, 1, [{"seq": 1, "chapter": None, "page": 1, "content": "old"}])
        con.commit()
        con.close()
        self.write_jsonl(self.records())
        plan = self.plan()

        with self.assertRaisesRegex(importer.ImportSafetyError, "Refusing"):
            importer.apply_import(plan, replace_existing=False, allow_partial=False)
        self.assertEqual(self.counts(), (1, 1))

        result = importer.apply_import(plan, replace_existing=True, allow_partial=False)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(self.counts(), (2, 2))

    def test_dry_run_with_replace_existing_reports_safe_for_complete_coverage(self):
        con = db.connect()
        db.insert_segments(con, 1, [{"seq": 1, "chapter": None, "page": 1, "content": "old"}])
        con.commit()
        con.close()
        self.write_jsonl(self.records())

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = importer.main([
                "--edition-id", "1", "--input", str(self.jsonl_path), "--replace-existing",
            ])

        self.assertEqual(exit_code, 0)
        self.assertIn("Safe to apply: yes.", output.getvalue())
        self.assertNotIn("existing segments require", output.getvalue())
        self.assertIn("DRY RUN ONLY — database unchanged.", output.getvalue())
        self.assertEqual(self.counts(), (1, 1))

    def test_partial_data_is_valid_for_dry_run_but_apply_requires_allow_partial(self):
        self.write_jsonl(self.records((2, 3)))
        plan = self.plan()

        self.assertEqual(plan.missing_pages, {1})
        self.assertFalse(plan.has_complete_pdf_coverage)
        self.assertEqual(self.counts(), (0, 0))
        with self.assertRaisesRegex(importer.ImportSafetyError, "partial OCR"):
            importer.apply_import(plan, replace_existing=False, allow_partial=False)

        result = importer.apply_import(plan, replace_existing=False, allow_partial=True)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.counts(), (1, 1))

    def test_full_coverage_apply_keeps_fts_in_sync_and_updates_indexed_at(self):
        self.write_jsonl(self.records())

        result = importer.apply_import(self.plan(), replace_existing=False, allow_partial=False)

        self.assertEqual((result["inserted"], result["fts_rows"]), (2, 2))
        self.assertEqual(self.counts(), (2, 2))
        con = db.connect()
        indexed_at = con.execute("SELECT indexed_at FROM editions WHERE id=1").fetchone()[0]
        epub_segments = con.execute("SELECT COUNT(*) FROM segments WHERE edition_id=2").fetchone()[0]
        con.close()
        self.assertIsNotNone(indexed_at)
        self.assertEqual(epub_segments, 0)

    def test_apply_failure_rolls_back_segments_fts_and_index_timestamp(self):
        self.write_jsonl(self.records())
        plan = self.plan()
        original_insert = db.insert_segments

        def insert_then_fail(con, edition_id, segments):
            original_insert(con, edition_id, segments[:1])
            raise RuntimeError("simulated insert failure")

        with patch.object(importer.db, "insert_segments", side_effect=insert_then_fail):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                importer.apply_import(plan, replace_existing=False, allow_partial=False)

        self.assertEqual(self.counts(), (0, 0))
        con = db.connect()
        self.assertIsNone(con.execute("SELECT indexed_at FROM editions WHERE id=1").fetchone()[0])
        con.close()


if __name__ == "__main__":
    unittest.main()
